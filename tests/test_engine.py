"""Testy obchodní logiky - příprava zadání, nákup, prodej a runner."""

from __future__ import annotations

from tests.fake_ib import OPTION_CONID, UNDERLYING_CONID
from tests.zaklad import ZakladEnginu
from tws_rucne.models import SELL_SCOPE_ALL, SELL_SCOPE_BASE, PositionState


class TestPripravaZadani(ZakladEnginu):
    """Strike a expirace se určují z aktuální ceny podkladu."""

    async def test_call_dostane_strike_nad_cenou(self):
        self.ib.price_underlying = 231.0
        nahled = await self.engine.prepare("AAPL", 3, "C")
        # Rastr je po 2,5 bodu, první strike mimo peníze nad 231 je 232,5
        self.assertEqual(nahled.strike, 232.5)
        self.assertEqual(nahled.right, "C")
        self.assertTrue(nahled.ready)

    async def test_put_dostane_strike_pod_cenou(self):
        self.ib.price_underlying = 231.0
        nahled = await self.engine.prepare("AAPL", 3, "P")
        self.assertEqual(nahled.strike, 230.0)

    async def test_rezim_atm_bere_nejblizsi_strike(self):
        self.cfg.strike.mode = "atm"
        self.ib.price_underlying = 231.0
        nahled = await self.engine.prepare("AAPL", 3, "C")
        self.assertEqual(nahled.strike, 230.0)

    async def test_nedostupny_strike_nahradi_dalsi_v_poradi(self):
        self.ib.price_underlying = 231.0
        self.ib.unavailable_strikes = {232.5}
        nahled = await self.engine.prepare("AAPL", 3, "C")
        self.assertEqual(nahled.strike, 230.0)
        # Náhrada se musí obchodníkovi ohlásit
        self.assertTrue(any("není pro expiraci" in v for v in nahled.warnings))

    async def test_siroky_spread_je_jen_varovani(self):
        self.cfg.trading.max_spread_pct = 2.0
        nahled = await self.engine.prepare("AAPL", 3, "C")
        self.assertTrue(nahled.ready)
        self.assertTrue(any("Spread" in v for v in nahled.warnings))

    async def test_novy_nahled_uvolni_odbery_toho_predchoziho(self):
        await self.engine.prepare("AAPL", 3, "C")
        await self.engine.prepare("AAPL", 3, "P")
        # Odebírá se právě jeden podklad a jedna opce - ta z posledního náhledu
        self.assertEqual(self.ib.subscribed.get(UNDERLYING_CONID), 1)
        self.assertEqual(self.ib.subscribed.get(OPTION_CONID), 1)

    async def test_bez_spojeni_priprava_selze(self):
        self.ib.connected_flag = False
        with self.assertRaises(RuntimeError):
            await self.engine.prepare("AAPL", 3, "C")


class TestNakup(ZakladEnginu):
    """Nákupní tlačítka zadávají limitní příkaz podle kotace."""

    async def test_nakup_za_ask_zada_limit_na_ask(self):
        position = await self.nakup(quantity=3, kind="ask")
        prikaz = self.ib.placed[-1].order
        self.assertEqual(prikaz.action, "BUY")
        self.assertEqual(prikaz.orderType, "LMT")
        self.assertEqual(prikaz.totalQuantity, 3)
        self.assertAlmostEqual(prikaz.lmtPrice, 3.20)
        self.assertEqual(prikaz.orderRef, f"TWSRUCNE:{position.id}:buy")
        self.assertEqual(position.state, PositionState.BUYING)

    async def test_nakup_za_mid_zada_limit_na_stred(self):
        await self.nakup(kind="mid")
        # Střed 3,10 už na rastru 0,05 leží, zaokrouhlení jej nezmění
        self.assertAlmostEqual(self.ib.placed[-1].order.lmtPrice, 3.10)

    async def test_limit_se_zaokrouhli_na_minimalni_tik(self):
        self.ib.price_bid, self.ib.price_ask = 3.00, 3.17
        await self.nakup(kind="mid")
        # Střed 3,085 se zaokrouhlí na nejbližší násobek pěti centů
        self.assertAlmostEqual(self.ib.placed[-1].order.lmtPrice, 3.10)

    async def test_nakup_bez_kotace_selze(self):
        self.ib.price_bid = self.ib.price_ask = None
        with self.assertRaises(ValueError):
            await self.nakup()
        self.assertEqual(self.ib.placed, [])

    async def test_mnozstvi_mimo_meze_selze(self):
        self.cfg.trading.max_quantity = 5
        with self.assertRaises(ValueError):
            await self.nakup(quantity=6)

    async def test_zmena_smeru_pripravi_kontrakt_znovu(self):
        # Náhled patří CALLu, nákup PUTu si musí vyžádat vlastní kontrakt
        await self.engine.prepare("AAPL", 3, "C")
        position = await self.nakup(right="P")
        self.assertEqual(position.right, "P")
        self.assertLess(position.strike, self.ib.price_underlying)

    async def test_vyplneny_nakup_otevre_pozici(self):
        position = await self.nakup_vyplnen(quantity=3, cena=3.20)
        self.assertEqual(position.state, PositionState.OPEN)
        self.assertEqual(position.filled_quantity, 3)
        self.assertEqual(position.open_quantity, 3)
        self.assertAlmostEqual(position.fill_price, 3.20)
        self.assertIsNotNone(position.fill_time)

    async def test_castecne_vyplneny_nakup_po_zruseni_drzi_mensi_pozici(self):
        position = await self.nakup(quantity=5)
        self.ib.fill(position.buy_trade, 2, 3.20, status="Cancelled")
        await self.tik()
        self.assertEqual(position.state, PositionState.OPEN)
        self.assertEqual(position.open_quantity, 2)
        # Základní pozice se počítá z toho, co se skutečně nakoupilo
        self.assertEqual(position.sell_quantity_for(SELL_SCOPE_BASE), 1)

    async def test_zruseny_nakup_bez_vyplneni_konci_zrusenim(self):
        position = await self.nakup()
        await self.engine.cancel_order(position.id)
        await self.tik()
        self.assertEqual(position.state, PositionState.CANCELLED)
        # Zrušená pozice svůj odběr dat uvolnila; zbylý drží náhled formuláře
        self.assertEqual(self.ib.subscribed.get(OPTION_CONID), 1)
        self.engine.release_preview()
        self.assertNotIn(OPTION_CONID, self.ib.subscribed)


class TestProdej(ZakladEnginu):
    """Prodejní tlačítka - celá pozice i základní část s runnerem."""

    async def test_prodej_vseho_za_bid(self):
        position = await self.nakup_vyplnen(quantity=3)
        await self.engine.sell(position.id, "bid", SELL_SCOPE_ALL)
        prikaz = self.ib.placed[-1].order
        self.assertEqual(prikaz.action, "SELL")
        self.assertEqual(prikaz.totalQuantity, 3)
        self.assertAlmostEqual(prikaz.lmtPrice, 3.00)
        self.assertEqual(prikaz.orderRef, f"TWSRUCNE:{position.id}:sell1")
        self.assertEqual(position.state, PositionState.SELLING)

    async def test_prodej_vseho_za_mid(self):
        position = await self.nakup_vyplnen(quantity=3)
        await self.engine.sell(position.id, "mid", SELL_SCOPE_ALL)
        self.assertAlmostEqual(self.ib.placed[-1].order.lmtPrice, 3.10)

    async def test_prodej_zakladni_pozice_nechava_runner(self):
        position = await self.nakup_vyplnen(quantity=3)
        await self.prodej_vyplnen(position, "bid", SELL_SCOPE_BASE, cena=3.60)
        # Ze tří kusů odešly dva, runner zůstal
        self.assertEqual(self.ib.placed[-1].order.totalQuantity, 2)
        self.assertEqual(position.state, PositionState.OPEN)
        self.assertEqual(position.open_quantity, 1)
        self.assertTrue(position.is_runner_only)
        # Dál lze prodat už jen zbytek
        self.assertTrue(position.can_sell_all)
        self.assertFalse(position.can_sell_base)

    async def test_runner_se_doprodava_celou_pozici(self):
        position = await self.nakup_vyplnen(quantity=3)
        await self.prodej_vyplnen(position, "bid", SELL_SCOPE_BASE, cena=3.60)
        await self.prodej_vyplnen(position, "mid", SELL_SCOPE_ALL, cena=4.10)
        self.assertEqual(position.state, PositionState.CLOSED)
        self.assertEqual(position.open_quantity, 0)
        # Dva kusy za 3,60 a jeden za 4,10 proti nákupu za 3,20
        self.assertAlmostEqual(position.realized_pnl, 170.0)

    async def test_prodej_vseho_uzavre_pozici(self):
        position = await self.nakup_vyplnen(quantity=2, cena=3.20)
        await self.prodej_vyplnen(position, "bid", SELL_SCOPE_ALL, cena=3.50)
        self.assertEqual(position.state, PositionState.CLOSED)
        self.assertAlmostEqual(position.realized_pnl, 60.0)
        # Uzavřená pozice už tržní data nepotřebuje; zbylý odběr drží náhled
        self.assertEqual(self.ib.subscribed.get(OPTION_CONID), 1)
        self.engine.release_preview()
        self.assertNotIn(OPTION_CONID, self.ib.subscribed)

    async def test_jediny_kontrakt_nelze_prodat_jako_zakladni_pozici(self):
        position = await self.nakup_vyplnen(quantity=1)
        self.assertFalse(position.can_sell_base)
        with self.assertRaises(ValueError):
            await self.engine.sell(position.id, "bid", SELL_SCOPE_BASE)

    async def test_prodej_pred_nakupem_selze(self):
        position = await self.nakup()
        with self.assertRaises(ValueError):
            await self.engine.sell(position.id, "bid", SELL_SCOPE_ALL)

    async def test_castecne_vyplneny_prodej_zuctuje_jen_prodane_kusy(self):
        position = await self.nakup_vyplnen(quantity=4)
        await self.engine.sell(position.id, "bid", SELL_SCOPE_ALL)
        # Nejdřív odejdou dva kusy, příkaz běží dál
        self.ib.fill(position.sell_trade, 2, 3.60, status="Submitted")
        await self.tik()
        self.assertEqual(position.state, PositionState.SELLING)
        self.assertEqual(position.sold_quantity, 2)
        self.assertEqual(position.open_quantity, 2)
        # Zbytek se pak vyplní za jinou cenu, průměr příkazu se posune
        self.ib.fill(position.sell_trade, 4, 3.70)
        await self.tik()
        self.assertEqual(position.state, PositionState.CLOSED)
        self.assertEqual(position.sold_quantity, 4)
        # Celková hodnota prodeje odpovídá průměru 3,70 za čtyři kusy
        self.assertAlmostEqual(position.sold_value, 14.8)
        self.assertAlmostEqual(position.realized_pnl, 200.0)

    async def test_zruseny_prodej_vraci_pozici_do_drzeni(self):
        position = await self.nakup_vyplnen(quantity=3)
        await self.engine.sell(position.id, "mid", SELL_SCOPE_ALL)
        await self.engine.cancel_order(position.id)
        await self.tik()
        self.assertEqual(position.state, PositionState.OPEN)
        self.assertEqual(position.open_quantity, 3)
        self.assertTrue(position.can_sell_base)

    async def test_prodej_bez_kotace_selze(self):
        position = await self.nakup_vyplnen(quantity=3)
        self.ib.price_bid = self.ib.price_ask = None
        with self.assertRaises(ValueError):
            await self.engine.sell(position.id, "bid", SELL_SCOPE_ALL)


class TestProvize(ZakladEnginu):
    """Provize se přebírají z TWS a snižují výsledek pozice."""

    async def test_provize_se_promitnou_do_ciste_hodnoty(self):
        position = await self.nakup(quantity=2)
        self.ib.fill(position.buy_trade, 2, 3.20, commission=2.0)
        await self.tik()
        await self.engine.sell(position.id, "bid", SELL_SCOPE_ALL)
        self.ib.fill(position.sell_trade, 2, 3.50, commission=2.5)
        await self.tik()
        self.assertAlmostEqual(position.gross_pnl, 60.0)
        self.assertAlmostEqual(position.commission_total, 4.5)
        self.assertAlmostEqual(position.net_pnl, 55.5)


class TestPreceneniPrikazu(ZakladEnginu):
    """
    Trh limitnímu příkazu utekl: opakovaný stisk tlačítka nezakládá druhý
    příkaz, jen přecení ten stávající na aktuální cenu.
    """

    async def test_opakovany_nakup_preceni_prikaz_misto_druheho(self):
        position = await self.nakup(quantity=3, kind="mid")
        puvodni_id = self.ib.placed[-1].order.orderId

        # Trh se pohnul výš, obchodník zkouší nákup znovu
        self.ib.price_bid, self.ib.price_ask = 3.40, 3.60
        znovu = await self.engine.buy("AAPL", 3, "C", "mid", reprice_id=position.id)

        self.assertIs(znovu, position)
        self.assertEqual(len(self.engine.positions), 1)
        # V TWS je pořád jediný příkaz, jen s novou cenou
        self.assertEqual(len(self.ib.placed), 1)
        self.assertEqual(self.ib.placed[-1].order.orderId, puvodni_id)
        self.assertAlmostEqual(self.ib.placed[-1].order.lmtPrice, 3.50)
        self.assertAlmostEqual(position.buy_limit, 3.50)
        self.assertEqual(position.state, PositionState.BUYING)

    async def test_preceneni_nakupu_umi_zmenit_i_mnozstvi(self):
        position = await self.nakup(quantity=3, kind="ask")
        await self.engine.buy("AAPL", 5, "C", "ask", reprice_id=position.id)
        self.assertEqual(self.ib.placed[-1].order.totalQuantity, 5)
        self.assertEqual(position.quantity, 5)

    async def test_nakup_pres_druhy_cekajici_prikaz_neprojde(self):
        # Bez odkazu na čekající příkaz se druhý nákup téže opce odmítne
        await self.nakup(quantity=3)
        with self.assertRaises(ValueError):
            await self.nakup(quantity=3)
        self.assertEqual(len(self.ib.placed), 1)

    async def test_opacny_smer_zalozi_vlastni_pozici(self):
        # Čekající CALL nebrání nákupu PUTu na stejném tickeru
        await self.nakup(quantity=3, right="C")
        put = await self.nakup(quantity=3, right="P")
        self.assertEqual(put.right, "P")
        self.assertEqual(len(self.engine.positions), 2)

    async def test_opakovany_prodej_preceni_prikaz(self):
        position = await self.nakup_vyplnen(quantity=3)
        await self.engine.sell(position.id, "bid", SELL_SCOPE_ALL)
        prikaz_id = position.sell_trade.order.orderId

        # Trh klesl, obchodník tlačítko zmáčkne znovu
        self.ib.price_bid, self.ib.price_ask = 2.80, 3.00
        await self.engine.sell(position.id, "bid", SELL_SCOPE_ALL, reprice=True)

        self.assertEqual(position.sell_trade.order.orderId, prikaz_id)
        self.assertAlmostEqual(position.sell_limit, 2.80)
        self.assertEqual(position.state, PositionState.SELLING)
        # Nákup a jeden prodej - druhý prodejní příkaz nevznikl
        self.assertEqual(len(self.ib.placed), 2)

    async def test_preceneni_prodeje_umi_zmenit_rozsah(self):
        position = await self.nakup_vyplnen(quantity=3)
        await self.engine.sell(position.id, "bid", SELL_SCOPE_BASE)
        self.assertEqual(position.sell_trade.order.totalQuantity, 2)

        # Obchodník si to rozmyslel a prodává celou pozici
        await self.engine.sell(position.id, "mid", SELL_SCOPE_ALL, reprice=True)
        self.assertEqual(position.sell_trade.order.totalQuantity, 3)
        self.assertEqual(position.sell_scope, SELL_SCOPE_ALL)
        self.assertEqual(len(self.ib.placed), 2)

    async def test_prodej_bez_priznaku_preceneni_druhy_prikaz_nezada(self):
        position = await self.nakup_vyplnen(quantity=3)
        await self.engine.sell(position.id, "bid", SELL_SCOPE_ALL)
        with self.assertRaises(ValueError):
            await self.engine.sell(position.id, "mid", SELL_SCOPE_ALL)
        self.assertEqual(len(self.ib.placed), 2)


class TestOchranaProtiDvojimuPrikazu(ZakladEnginu):
    """
    Vyplnění příkazu a stisk tlačítka se mohou potkat. Stisk nesmí skončit
    druhým nákupem ani prodejem.
    """

    async def test_mezitim_vyplneny_nakup_se_neopakuje(self):
        position = await self.nakup(quantity=3, kind="mid")
        # TWS příkaz vyplnila dřív, než se rozhraní stihlo překreslit
        self.ib.fill(position.buy_trade, 3, 3.10)

        with self.assertRaises(ValueError):
            await self.engine.buy("AAPL", 3, "C", "mid", reprice_id=position.id)

        # Nakoupeno je jen jednou a pozice se mezitím správně otevřela
        self.assertEqual(len(self.ib.placed), 1)
        self.assertEqual(position.state, PositionState.OPEN)
        self.assertEqual(position.open_quantity, 3)

    async def test_mezitim_vyplneny_prodej_se_neopakuje(self):
        position = await self.nakup_vyplnen(quantity=3)
        await self.engine.sell(position.id, "bid", SELL_SCOPE_ALL)
        # Prodej se v TWS vyplnil těsně před stiskem tlačítka
        self.ib.fill(position.sell_trade, 3, 3.60)

        with self.assertRaises(ValueError):
            await self.engine.sell(position.id, "bid", SELL_SCOPE_ALL, reprice=True)

        self.assertEqual(len(self.ib.placed), 2)
        self.assertEqual(position.state, PositionState.CLOSED)
        self.assertEqual(position.sold_quantity, 3)

    async def test_castecne_vyplneny_nakup_lze_precenit(self):
        # Z trojice se vyplnily dva kusy a trh utekl - zbytek se přecení
        position = await self.nakup(quantity=3, kind="mid")
        self.ib.fill(position.buy_trade, 2, 3.19, status="Submitted")
        self.ib.price_bid, self.ib.price_ask = 3.30, 3.50

        await self.engine.buy("AAPL", 3, "C", "ask", reprice_id=position.id)

        prikaz = self.ib.placed[-1].order
        self.assertAlmostEqual(prikaz.lmtPrice, 3.50)
        # Celkový objem zůstává tři kusy, dva z nich jsou už nakoupené
        self.assertEqual(prikaz.totalQuantity, 3)
        self.assertEqual(position.state, PositionState.BUYING)
        self.assertEqual(position.filled_quantity, 2)
        self.assertIn("zbývá 1 z 3 ks", position.message)
        self.assertEqual(len(self.ib.placed), 1)

    async def test_nakup_nelze_zmensit_pod_uz_vyplnene_mnozstvi(self):
        position = await self.nakup(quantity=4, kind="mid")
        self.ib.fill(position.buy_trade, 3, 3.10, status="Submitted")
        with self.assertRaises(ValueError):
            await self.engine.buy("AAPL", 2, "C", "ask", reprice_id=position.id)
        # Příkaz zůstal nedotčený
        self.assertEqual(self.ib.placed[-1].order.totalQuantity, 4)
        self.assertAlmostEqual(self.ib.placed[-1].order.lmtPrice, 3.10)

    async def test_castecne_vyplneny_prodej_lze_precenit(self):
        position = await self.nakup_vyplnen(quantity=4)
        await self.engine.sell(position.id, "bid", SELL_SCOPE_ALL)
        # Dva kusy odešly, zbytek visí v trhu
        self.ib.fill(position.sell_trade, 2, 3.60, status="Submitted")
        await self.tik()
        self.assertEqual(position.sold_quantity, 2)

        self.ib.price_bid, self.ib.price_ask = 3.20, 3.40
        await self.engine.sell(position.id, "bid", SELL_SCOPE_ALL, reprice=True)

        prikaz = self.ib.placed[-1].order
        self.assertAlmostEqual(prikaz.lmtPrice, 3.20)
        # Objem příkazu musí pokrýt i dva už prodané kusy, jinak jej TWS odmítne
        self.assertEqual(prikaz.totalQuantity, 4)
        self.assertEqual(position.open_quantity, 2)

    async def test_preceneny_castecny_prodej_dopocita_zbytek(self):
        position = await self.nakup_vyplnen(quantity=4, cena=3.00)
        await self.engine.sell(position.id, "bid", SELL_SCOPE_ALL)
        self.ib.fill(position.sell_trade, 2, 3.60, status="Submitted")
        await self.tik()
        await self.engine.sell(position.id, "bid", SELL_SCOPE_ALL, reprice=True)

        # Doběhne celý objem: dva kusy za 3,60 a dva za nižší průměr příkazu
        self.ib.fill(position.sell_trade, 4, 3.40)
        await self.tik()
        self.assertEqual(position.state, PositionState.CLOSED)
        self.assertEqual(position.sold_quantity, 4)
        self.assertAlmostEqual(position.sold_value, 13.6)
        self.assertAlmostEqual(position.realized_pnl, 160.0)

    async def test_preceneni_uz_zruseneho_prikazu_neprojde(self):
        position = await self.nakup(quantity=3)
        await self.engine.cancel_order(position.id)
        with self.assertRaises(ValueError):
            await self.engine.buy("AAPL", 3, "C", "ask", reprice_id=position.id)
        self.assertEqual(position.state, PositionState.CANCELLED)

    async def test_preceneni_z_karty_take_hlida_mezitim_vyplneny_nakup(self):
        # Tlačítko u pozice volá přecenění přímo, i tam musí ochrana platit
        position = await self.nakup(quantity=3, kind="mid")
        self.ib.fill(position.buy_trade, 3, 3.10)
        with self.assertRaises(ValueError):
            await self.engine.reprice_buy(position.id, "ask")
        self.assertEqual(len(self.ib.placed), 1)
        self.assertEqual(position.state, PositionState.OPEN)

    async def test_shodne_preceneni_prikaz_do_tws_neposila(self):
        # Trh se nepohnul - modifikace by příkaz jen zbytečně poslala do fronty
        position = await self.nakup(quantity=3, kind="ask")
        await self.engine.buy("AAPL", 3, "C", "ask", reprice_id=position.id)
        self.assertEqual(len(self.ib.placed), 1)
        self.assertIn("beze změny", position.message)
        self.assertEqual(position.state, PositionState.BUYING)

    async def test_shodne_preceneni_prodeje_prikaz_neposila(self):
        position = await self.nakup_vyplnen(quantity=3)
        await self.engine.sell(position.id, "bid", SELL_SCOPE_ALL)
        await self.engine.sell(position.id, "bid", SELL_SCOPE_ALL, reprice=True)
        self.assertEqual(len(self.ib.placed), 2)
        self.assertIn("beze změny", position.message)


class TestProdejNadPoptavkou(ZakladEnginu):
    """Tlačítka ASK a ASK +1 % až +5 % nabízejí prodej nad středem trhu."""

    async def test_prodej_za_ask(self):
        position = await self.nakup_vyplnen(quantity=3)
        await self.engine.sell(position.id, "ask", SELL_SCOPE_ALL)
        self.assertAlmostEqual(self.ib.placed[-1].order.lmtPrice, 3.20)
        self.assertIn("(ASK)", position.message)

    async def test_prodej_s_prirazkou_zvedne_limit(self):
        position = await self.nakup_vyplnen(quantity=3)
        # ASK 3,20 zvednutý o 5 % je 3,36, na rastru 0,05 tedy 3,35
        await self.engine.sell(position.id, "ask", SELL_SCOPE_ALL, markup_pct=5.0)
        self.assertAlmostEqual(self.ib.placed[-1].order.lmtPrice, 3.35)
        self.assertAlmostEqual(position.sell_markup_pct, 5.0)
        self.assertIn("ASK +5 %", position.message)

    async def test_prirazka_jde_i_u_zakladni_pozice(self):
        position = await self.nakup_vyplnen(quantity=3)
        await self.engine.sell(position.id, "ask", SELL_SCOPE_BASE, markup_pct=3.0)
        prikaz = self.ib.placed[-1].order
        self.assertEqual(prikaz.totalQuantity, 2)
        # ASK 3,20 o 3 % výš je 3,296, na rastru 0,05 tedy 3,30
        self.assertAlmostEqual(prikaz.lmtPrice, 3.30)

    async def test_preceneni_umi_prejit_na_jinou_prirazku(self):
        position = await self.nakup_vyplnen(quantity=3)
        await self.engine.sell(position.id, "ask", SELL_SCOPE_ALL, markup_pct=5.0)
        await self.engine.sell(
            position.id, "ask", SELL_SCOPE_ALL, reprice=True, markup_pct=1.0
        )
        # ASK 3,20 o 1 % výš je 3,232, na rastru 0,05 tedy 3,25
        self.assertAlmostEqual(position.sell_limit, 3.25)
        self.assertAlmostEqual(position.sell_markup_pct, 1.0)
        self.assertEqual(len(self.ib.placed), 2)

    async def test_prodej_bez_prirazky_zustava_na_stredu(self):
        position = await self.nakup_vyplnen(quantity=3)
        await self.engine.sell(position.id, "mid", SELL_SCOPE_ALL)
        self.assertAlmostEqual(self.ib.placed[-1].order.lmtPrice, 3.10)
        self.assertIn("(MID)", position.message)


class TestUklidPrehledu(ZakladEnginu):
    """Hromadné odstranění ukončených pozic z přehledu."""

    async def test_uklid_smaze_jen_ukoncene_pozice(self):
        # Uzavřená pozice
        uzavrena = await self.nakup_vyplnen(quantity=2)
        await self.prodej_vyplnen(uzavrena, "bid", SELL_SCOPE_ALL, cena=3.50)
        # Zrušený nákup
        zruseny = await self.nakup(quantity=1)
        await self.engine.cancel_order(zruseny.id)
        await self.tik()
        # Otevřená pozice, které se úklid nesmí dotknout
        otevrena = await self.nakup_vyplnen(quantity=3)

        self.assertEqual(self.engine.remove_finished(), 2)
        self.assertEqual(list(self.engine.positions), [otevrena.id])

    async def test_uklid_bez_ukoncenych_pozic_nic_nedela(self):
        await self.nakup_vyplnen(quantity=2)
        self.assertEqual(self.engine.remove_finished(), 0)
        self.assertEqual(len(self.engine.positions), 1)

    async def test_uklid_uvolni_odbery_dat(self):
        position = await self.nakup(quantity=1)
        await self.engine.cancel_order(position.id)
        await self.tik()
        self.engine.remove_finished()
        self.engine.release_preview()
        self.assertEqual(self.ib.subscribed, {})
