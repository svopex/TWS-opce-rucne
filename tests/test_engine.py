"""Testy obchodní logiky - příprava zadání, nákup, prodej a runner."""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from tests.fake_ib import NAN, OPTION_CONID, UNDERLYING_CONID
from tests.zaklad import ZakladEnginu, cizi_obchod, cizi_stav
from tws_rucne.models import (
    SELL_SCOPE_ALL,
    SELL_SCOPE_BASE,
    SELL_SCOPE_ONE,
    SELL_SCOPE_THREE,
    PositionState,
)


class TestPripravaZadani(ZakladEnginu):
    """Strike a expirace se určují z aktuální ceny podkladu."""

    async def test_call_dostane_strike_nad_cenou(self):
        self.ib.price_underlying = 231.0
        nahled = await self.engine.prepare("AAPL", "C")
        # Rastr je po 2,5 bodu, první strike mimo peníze nad 231 je 232,5
        self.assertEqual(nahled.strike, 232.5)
        self.assertEqual(nahled.right, "C")
        self.assertTrue(nahled.ready)

    async def test_put_dostane_strike_pod_cenou(self):
        self.ib.price_underlying = 231.0
        nahled = await self.engine.prepare("AAPL", "P")
        self.assertEqual(nahled.strike, 230.0)

    async def test_rezim_atm_bere_nejblizsi_strike(self):
        self.cfg.strike.mode = "atm"
        self.ib.price_underlying = 231.0
        nahled = await self.engine.prepare("AAPL", "C")
        self.assertEqual(nahled.strike, 230.0)

    async def test_nedostupny_strike_nahradi_dalsi_v_poradi(self):
        self.ib.price_underlying = 231.0
        self.ib.unavailable_strikes = {232.5}
        nahled = await self.engine.prepare("AAPL", "C")
        self.assertEqual(nahled.strike, 230.0)
        # Náhrada se musí obchodníkovi ohlásit
        self.assertTrue(any("není pro expiraci" in v for v in nahled.warnings))

    async def test_siroky_spread_je_jen_varovani(self):
        self.cfg.trading.max_spread_pct = 2.0
        nahled = await self.engine.prepare("AAPL", "C")
        self.assertTrue(nahled.ready)
        self.assertTrue(any("Spread" in v for v in nahled.warnings))

    async def test_novy_nahled_uvolni_odbery_toho_predchoziho(self):
        await self.engine.prepare("AAPL", "C")
        await self.engine.prepare("AAPL", "P")
        # Odebírá se právě jeden podklad a jedna opce - ta z posledního náhledu
        self.assertEqual(self.ib.subscribed.get(UNDERLYING_CONID), 1)
        self.assertEqual(self.ib.subscribed.get(OPTION_CONID), 1)

    async def test_bez_spojeni_priprava_selze(self):
        self.ib.connected_flag = False
        with self.assertRaises(RuntimeError):
            await self.engine.prepare("AAPL", "C")


class TestObsazenyStrike(ZakladEnginu):
    """Kontrakt obsazený cizím příkazem nebo pozicí se při výběru přeskočí."""

    def setUp(self) -> None:
        super().setUp()
        self.nastav_rastr_striku()

    async def test_cizi_prikaz_posune_strike_dal_mimo_penize(self):
        self.ib.foreign_orders = {800001: "AAPL 250103C00232500"}
        nahled = await self.engine.prepare("AAPL", "C")
        self.assertEqual(nahled.strike, 235.0)
        self.assertTrue(any("už obsadil cizí příkaz" in v for v in nahled.warnings))

    async def test_neridena_pozice_posune_strike(self):
        self.ib.held_positions = {800001: 5}
        nahled = await self.engine.prepare("AAPL", "C")
        self.assertEqual(nahled.strike, 235.0)

    async def test_obsazeno_vic_striku_za_sebou_ustoupi_dal(self):
        self.ib.foreign_orders = {800001: "cizi", 800002: "cizi"}
        nahled = await self.engine.prepare("AAPL", "C")
        self.assertEqual(nahled.strike, 237.5)

    async def test_nedostupny_posunuty_strike_hledani_neukonci(self):
        # Pro 235,0 kontrakt v TWS není, takže ověření spadne zpátky na 232,5,
        # který už se zkoušel. Hledání kvůli tomu nesmí skončit - o krok dál
        # je 237,5 volné i dostupné.
        self.ib.foreign_orders = {800001: "cizi"}
        self.ib.unavailable_strikes = {235.0}
        nahled = await self.engine.prepare("AAPL", "C")
        self.assertEqual(nahled.strike, 237.5)

    async def test_cizi_prikaz_bez_popisu_kontrakt_obsazuje(self):
        # TWS nemusí popis kontraktu poslat; prázdný popis ale neznamená volno
        nahled = await self.engine.prepare("AAPL", "C")
        self.assertEqual(nahled.strike, 232.5)
        self.ib.foreign_orders = {800001: ""}
        with self.assertRaises(ValueError) as chyba:
            await self.engine.buy("AAPL", 3, "C", "ask")
        self.assertIn("mezitím obsadil", str(chyba.exception))
        self.assertEqual(self.ib.placed, [])

    async def test_vlastni_pozice_strike_neobsazuje(self):
        # Do svého kontraktu musí jít dokupovat dál, jinak by druhý nákup
        # do téže pozice skončil na jiném striku
        position = await self.nakup_vyplnen(quantity=3)
        self.ib.held_positions = {position.option_conid: 3}
        nahled = await self.engine.prepare("AAPL", "C")
        self.assertEqual(nahled.strike, 232.5)
        self.assertEqual(nahled.warnings, [])

    async def test_vypnute_vyhybani_nechava_puvodni_strike(self):
        self.cfg.strike.avoid_occupied = False
        self.ib.foreign_orders = {800001: "cizi"}
        nahled = await self.engine.prepare("AAPL", "C")
        self.assertEqual(nahled.strike, 232.5)
        self.assertEqual(nahled.warnings, [])

    async def test_bez_volneho_striku_zustane_prvni_volba_s_varovanim(self):
        self.ib.foreign_orders = {conid: "cizi" for conid in self.ib.option_conids.values()}
        nahled = await self.engine.prepare("AAPL", "C")
        # Nabídne se původní výběr, ale kolize musí být vidět
        self.assertEqual(nahled.strike, 232.5)
        self.assertTrue(nahled.ready)
        self.assertEqual(nahled.option.conId, 800001)
        self.assertTrue(any("volný strike se poblíž nenašel" in v for v in nahled.warnings))

    async def test_nakup_odmitne_kontrakt_obsazeny_az_po_priprave(self):
        nahled = await self.engine.prepare("AAPL", "C")
        self.assertEqual(nahled.strike, 232.5)
        # Cizí příkaz vznikl mezi přípravou náhledu a stiskem tlačítka
        self.ib.foreign_orders = {800001: "cizi"}
        with self.assertRaises(ValueError) as chyba:
            await self.engine.buy("AAPL", 3, "C", "ask")
        self.assertIn("mezitím obsadil", str(chyba.exception))
        # Do trhu nesmělo nic odejít a náhled už nabízí volný strike
        self.assertEqual(self.ib.placed, [])
        self.assertEqual(self.engine.preview.strike, 235.0)

    async def test_nakup_projde_kdyz_nahled_kolizi_uz_ohlasil(self):
        self.ib.foreign_orders = {conid: "cizi" for conid in self.ib.option_conids.values()}
        await self.engine.prepare("AAPL", "C")
        # Náhled kolizi ohlásil a obchodník kontrakt ponechal - nákup projde
        position = await self.engine.buy("AAPL", 3, "C", "ask")
        self.assertEqual(position.strike, 232.5)
        self.assertEqual(len(self.ib.placed), 1)


class TestZamluvenyStrikeJinouAplikaci(ZakladEnginu):
    """Kontrakt, který si drží čekající obchod druhé aplikace, se přeskočí."""

    def setUp(self) -> None:
        super().setUp()
        self.nastav_rastr_striku()

        docasny = tempfile.TemporaryDirectory()
        self.addCleanup(docasny.cleanup)
        self.stav = Path(docasny.name) / "state.json"
        self.cfg.strike.reserved_state_files = [str(self.stav)]
        # Čtečka rezervací vzniká s enginem, konfigurace tedy musí platit dřív
        self.prestav_engine()

    def zapis_obchod(self, conid: int = 800001, state: str = "SPREAD_BLOCKED") -> None:
        """Uloží do stavu druhé aplikace jeden obchod na daném kontraktu."""
        self.stav.write_text(
            json.dumps(cizi_stav(cizi_obchod(conid=conid, state=state))), encoding="utf-8"
        )

    async def test_obchod_blokovany_spreadem_posune_strike(self):
        # Druhá aplikace čeká na zúžení spreadu, v TWS proto žádný příkaz
        # nevisí - kontrakt si přesto drží
        self.zapis_obchod()
        nahled = await self.engine.prepare("AAPL", "C")
        self.assertEqual(nahled.strike, 235.0)
        self.assertTrue(any("obchod AAPL-39 jiné aplikace" in v for v in nahled.warnings))

    async def test_ukonceny_obchod_strike_neblokuje(self):
        self.zapis_obchod(state="CLOSED")
        nahled = await self.engine.prepare("AAPL", "C")
        self.assertEqual(nahled.strike, 232.5)
        self.assertEqual(nahled.warnings, [])

    async def test_bez_nastaveneho_souboru_se_nic_nemeni(self):
        self.cfg.strike.reserved_state_files = []
        self.prestav_engine()
        self.zapis_obchod()
        nahled = await self.engine.prepare("AAPL", "C")
        self.assertEqual(nahled.strike, 232.5)

    async def test_vypnute_vyhybani_rezervace_ignoruje(self):
        self.cfg.strike.avoid_occupied = False
        self.zapis_obchod()
        nahled = await self.engine.prepare("AAPL", "C")
        self.assertEqual(nahled.strike, 232.5)

    async def test_nakup_odmitne_kontrakt_zamluveny_az_po_priprave(self):
        nahled = await self.engine.prepare("AAPL", "C")
        self.assertEqual(nahled.strike, 232.5)
        # Druhá aplikace si kontrakt zamluvila mezi přípravou a stiskem tlačítka
        self.zapis_obchod()
        with self.assertRaises(ValueError) as chyba:
            await self.engine.buy("AAPL", 1, "C", "ask")
        self.assertIn("mezitím obsadil", str(chyba.exception))
        self.assertEqual(self.ib.placed, [])
        self.assertEqual(self.engine.preview.strike, 235.0)


class TestZmenyNabidkyPredNakupem(ZakladEnginu):
    """Stisk nákupního tlačítka zadání přepočítá a změnu nabídky ohlásí."""

    def setUp(self) -> None:
        super().setUp()
        self.nastav_rastr_striku()

    async def test_uvolneny_strike_nakup_zastavi(self):
        # Obchodník má na obrazovce 235, protože 232,5 držel cizí příkaz
        self.ib.foreign_orders = {800001: "cizi"}
        nahled = await self.engine.prepare("AAPL", "C")
        self.assertEqual(nahled.strike, 235.0)
        # Cizí příkaz mezitím zmizel
        self.ib.foreign_orders = {}

        with self.assertRaises(ValueError) as chyba:
            await self.engine.buy("AAPL", 1, "C", "ask")

        self.assertIn("Nabídka se změnila", str(chyba.exception))
        # Do trhu nesmí odejít nic a v náhledu čeká uvolněný strike
        self.assertEqual(self.ib.placed, [])
        self.assertEqual(self.engine.preview.strike, 232.5)

    async def test_druhy_stisk_uz_koupi_novy_strike(self):
        self.ib.foreign_orders = {800001: "cizi"}
        await self.engine.prepare("AAPL", "C")
        self.ib.foreign_orders = {}
        with self.assertRaises(ValueError):
            await self.engine.buy("AAPL", 1, "C", "ask")

        position = await self.engine.buy("AAPL", 1, "C", "ask")

        self.assertEqual(position.strike, 232.5)
        self.assertEqual(len(self.ib.placed), 1)

    async def test_nezmenena_nabidka_koupi_hned(self):
        nahled = await self.engine.prepare("AAPL", "C")
        self.assertEqual(nahled.strike, 232.5)

        position = await self.engine.buy("AAPL", 1, "C", "ask")

        self.assertEqual(position.strike, 232.5)
        self.assertEqual(len(self.ib.placed), 1)


class TestPrenosuNaJinyStrike(ZakladEnginu):
    """Uvolní-li se strike, stisk tlačítka přenese příkaz na něj."""

    def setUp(self) -> None:
        super().setUp()
        self.nastav_rastr_striku()

    async def nakup_na_obsazenem(self):
        """Zadá nákup ve chvíli, kdy je první strike obsazený cizím příkazem."""
        self.ib.foreign_orders = {800001: "cizi"}
        nahled = await self.engine.prepare("AAPL", "C")
        # Kontraktu 232,5 se ustoupilo, příkaz jde na 235
        self.assertEqual(nahled.strike, 235.0)
        return await self.engine.buy("AAPL", 1, "C", "ask")

    async def test_uvolneny_strike_prepise_pozici(self):
        position = await self.nakup_na_obsazenem()
        puvodni_prikaz = position.buy_trade
        # Cizí příkaz zmizel a náhled nabízí zpátky 232,5
        self.ib.foreign_orders = {}
        nahled = await self.engine.prepare("AAPL", "C")
        self.assertEqual(nahled.strike, 232.5)

        prepsana = await self.engine.buy("AAPL", 1, "C", "ask", reprice_id=position.id)

        # Táž pozice jen změnila kontrakt - druhá karta v přehledu nevzniká
        self.assertIs(prepsana, position)
        self.assertEqual(len(self.engine.positions), 1)
        self.assertEqual(position.strike, 232.5)
        self.assertEqual(position.option_conid, 800001)
        self.assertEqual(position.state, PositionState.BUYING)
        # Původní příkaz musí z trhu zmizet dřív, než vznikne nový
        self.assertEqual(self.ib.cancelled, [puvodni_prikaz])
        self.assertEqual(len(self.ib.placed), 2)
        self.assertIs(position.buy_trade, self.ib.placed[-1])

    async def test_zmena_nabidky_prikaz_prepise_bez_upozorneni(self):
        # Obchodník má na obrazovce pořád 235; přepočet při stisku najde
        # uvolněný strike a příkaz se má rovnou přepsat, ne zastavit
        position = await self.nakup_na_obsazenem()
        self.ib.foreign_orders = {}

        prepsana = await self.engine.buy("AAPL", 1, "C", "ask", reprice_id=position.id)

        self.assertIs(prepsana, position)
        self.assertEqual(position.strike, 232.5)
        self.assertEqual(position.state, PositionState.BUYING)
        self.assertEqual(len(self.engine.positions), 1)

    async def test_prepsany_prikaz_dostane_vlastni_znacku(self):
        # Pod značkou zrušeného příkazu by obnova po restartu našla oba
        position = await self.nakup_na_obsazenem()
        self.ib.foreign_orders = {}
        await self.engine.prepare("AAPL", "C")

        await self.engine.buy("AAPL", 1, "C", "ask", reprice_id=position.id)

        znacky = [t.order.orderRef for t in self.ib.placed]
        self.assertEqual(znacky, [f"TWSRUCNE:{position.id}:buy", f"TWSRUCNE:{position.id}:buy2"])

    async def test_prepis_prezije_tik_monitoringu(self):
        # Zrušení uprostřed přepisu nesmí pozici uzavřít
        position = await self.nakup_na_obsazenem()
        self.ib.foreign_orders = {}
        await self.engine.prepare("AAPL", "C")

        await self.engine.buy("AAPL", 1, "C", "ask", reprice_id=position.id)
        await self.tik()

        self.assertEqual(position.state, PositionState.BUYING)
        self.assertEqual(position.strike, 232.5)

    async def test_stejny_kontrakt_se_dal_jen_preceni(self):
        position = await self.nakup_na_obsazenem()
        # Náhled nabízí tentýž kontrakt, na kterém příkaz visí
        self.ib.quote_bid, self.ib.quote_ask = 3.50, 3.70
        await self.engine.prepare("AAPL", "C")

        nova = await self.engine.buy("AAPL", 1, "C", "ask", reprice_id=position.id)

        self.assertIs(nova, position)
        self.assertEqual(self.ib.cancelled, [])
        self.assertEqual(len(self.ib.placed), 1)

    async def test_mezitim_vyplneny_prikaz_se_neprepisuje(self):
        position = await self.nakup_na_obsazenem()
        self.ib.foreign_orders = {}
        await self.engine.prepare("AAPL", "C")
        # Příkaz se vyplnil dřív, než obchodník stiskl tlačítko
        self.ib.fill(position.buy_trade, 1, 3.20)

        with self.assertRaises(ValueError) as chyba:
            await self.engine.buy("AAPL", 1, "C", "ask", reprice_id=position.id)

        self.assertIn("už v trhu není", str(chyba.exception))
        self.assertEqual(self.ib.cancelled, [])
        self.assertEqual(len(self.ib.placed), 1)


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
        await self.engine.prepare("AAPL", "C")
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
        self.assertIn("(MID)", position.message)

    async def test_prodej_vseho_za_ask(self):
        # Kdo nespěchá, nechá příkaz čekat na poptávku a spread inkasuje
        position = await self.nakup_vyplnen(quantity=3)
        await self.engine.sell(position.id, "ask", SELL_SCOPE_ALL)
        self.assertAlmostEqual(self.ib.placed[-1].order.lmtPrice, 3.20)
        self.assertIn("(ASK)", position.message)

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


class TestZuctovaniProdejniCeny(ZakladEnginu):
    """
    Skutečnou průměrnou cenu posílá TWS o kousek později než hlášení
    o vyplnění. Do realizovaného výsledku musí nakonec dorazit ona,
    ne odhad podle limitní ceny.
    """

    async def test_pozice_se_neuzavre_dokud_nedorazi_skutecna_cena(self):
        position = await self.nakup_vyplnen(quantity=3)
        await self.engine.sell(position.id, "bid", SELL_SCOPE_ALL)
        prikaz = position.sell_trade
        # Vyplněno je, průměrná cena zatím ne
        self.ib.fill(prikaz, 3, NAN)
        await self.tik()
        self.assertEqual(position.state, PositionState.SELLING)

        # Jakmile cena dorazí, zúčtuje se s ní a pozice se uzavře
        prikaz.orderStatus.avgFillPrice = 3.55
        await self.tik()
        self.assertEqual(position.state, PositionState.CLOSED)
        self.assertAlmostEqual(position.sold_value, 3 * 3.55)
        # Tři kusy za 3,55 proti nákupu za 3,20
        self.assertAlmostEqual(position.realized_pnl, 105.0)

    async def test_odhad_se_opravi_i_po_castecnem_vyplneni(self):
        position = await self.nakup_vyplnen(quantity=3)
        await self.engine.sell(position.id, "bid", SELL_SCOPE_ALL)
        prikaz = position.sell_trade
        # Částečné vyplnění bez ceny se zúčtuje odhadem podle limitu
        self.ib.fill(prikaz, 2, NAN, status="Submitted")
        await self.tik()
        self.assertEqual(position.sold_quantity, 2)
        self.assertAlmostEqual(position.sold_value, 2 * position.sell_limit)

        # Skutečná cena přepíše odhad, i když se počet kusů nezměnil
        prikaz.orderStatus.avgFillPrice = 3.40
        await self.tik()
        self.assertEqual(position.sold_quantity, 2)
        self.assertAlmostEqual(position.sold_value, 2 * 3.40)

    async def test_bez_ceny_z_tws_se_pozice_po_odkladu_uzavre_odhadem(self):
        # Kdyby cena nikdy nedorazila, nesmí pozice zůstat navěky v prodeji
        position = await self.nakup_vyplnen(quantity=3)
        await self.engine.sell(position.id, "bid", SELL_SCOPE_ALL)
        limit = position.sell_limit
        self.ib.fill(position.sell_trade, 3, NAN)
        with mock.patch("tws_rucne.engine.FILL_PRICE_WAIT_SEC", 0.0):
            await self.tik()
            await self.tik()
        self.assertEqual(position.state, PositionState.CLOSED)
        self.assertAlmostEqual(position.sold_value, 3 * limit)


class TestNeridenePozice(ZakladEnginu):
    """Pruh s neřízenými pozicemi musí sledovat účet i mezi obnovami."""

    async def test_pozice_otevrena_v_tws_se_objevi_uz_pri_tiku(self):
        self.ib.held_positions = {999999: 5}
        await self.tik()
        self.assertIn(999999, self.engine.unmanaged)

    async def test_pozice_prodana_v_tws_z_pruhu_zmizi(self):
        self.ib.held_positions = {999999: 5}
        await self.tik()
        self.ib.held_positions = {}
        await self.tik()
        self.assertEqual(self.engine.unmanaged, {})

    async def test_pozice_rizena_aplikaci_se_mezi_neridene_nepocita(self):
        await self.nakup_vyplnen(quantity=3)
        self.ib.held_positions = {OPTION_CONID: 3}
        await self.tik()
        self.assertEqual(self.engine.unmanaged, {})


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


class TestVysledkuVHlasce(ZakladEnginu):
    """
    Hláška o prodejním příkazu nese i zisk nebo ztrátu, se kterou příkaz
    do trhu jde - v průběhu obchodu je pak vidět, co který příkaz vynese.
    Kotace náhrady TWS je 3,00 / 3,20, nákup se vyplní za 3,20.
    """

    async def test_ztrata_pri_prodeji_za_poptavku(self):
        position = await self.nakup_vyplnen(quantity=3)
        await self.engine.sell(position.id, "bid", SELL_SCOPE_ALL)
        self.assertIn("ztráta 60 USD", position.message)

    async def test_prodej_za_nakupni_cenu_je_vyrovnany(self):
        position = await self.nakup_vyplnen(quantity=3)
        await self.engine.sell(position.id, "ask", SELL_SCOPE_ALL)
        self.assertIn("bez zisku i ztráty", position.message)

    async def test_vysledek_pocita_jen_prodavane_kusy(self):
        # Základní pozice jsou dva kusy, runner zůstává v trhu
        position = await self.nakup_vyplnen(quantity=3)
        await self.engine.sell(position.id, "bid", SELL_SCOPE_BASE)
        self.assertIn("ztráta 40 USD", position.message)

    async def test_preceneni_nese_vysledek_nove_ceny(self):
        position = await self.nakup_vyplnen(quantity=3)
        await self.engine.sell(position.id, "markup", SELL_SCOPE_ALL, markup_pct=5.0)
        await self.engine.sell(position.id, "bid", SELL_SCOPE_ALL, reprice=True)
        self.assertIn("přeceněn", position.message)
        self.assertIn("ztráta 60 USD", position.message)


class TestProdejSPrirazkou(ZakladEnginu):
    """
    Přirážková tlačítka počítají limit z vyšší z cen vstup / ASK.
    Nákup se vyplní mimo kotaci náhrady TWS 3,00 / 3,20 - za 4,00 je pozice
    ve ztrátě a základem je vstup, za 2,60 v zisku a základem je ASK.
    """

    async def test_prirazka_zvedne_nakupni_cenu(self):
        position = await self.nakup_vyplnen(quantity=3, cena=4.00)
        # Nákup 4,00 o 5 % výš je 4,20, zisk 0,20 krát 300
        await self.engine.sell(position.id, "markup", SELL_SCOPE_ALL, markup_pct=5.0)
        self.assertAlmostEqual(self.ib.placed[-1].order.lmtPrice, 4.20)
        self.assertAlmostEqual(position.sell_markup_pct, 5.0)
        self.assertIn("přirážka +5 %", position.message)
        self.assertIn("zisk 60 USD", position.message)

    async def test_pozice_v_zisku_stavi_prirazku_nad_ask(self):
        position = await self.nakup_vyplnen(quantity=3, cena=2.60)
        # ASK 3,20 o 5 % výš je 3,36, na rastru 0,05 tedy 3,35; zisk 0,75 krát 300
        await self.engine.sell(position.id, "markup", SELL_SCOPE_ALL, markup_pct=5.0)
        self.assertAlmostEqual(self.ib.placed[-1].order.lmtPrice, 3.35)
        self.assertIn("zisk 225 USD", position.message)

    async def test_prirazka_jde_i_u_zakladni_pozice(self):
        position = await self.nakup_vyplnen(quantity=3, cena=4.00)
        await self.engine.sell(position.id, "markup", SELL_SCOPE_BASE, markup_pct=3.0)
        prikaz = self.ib.placed[-1].order
        self.assertEqual(prikaz.totalQuantity, 2)
        # Nákup 4,00 o 3 % výš je 4,12, na rastru 0,05 tedy 4,10
        self.assertAlmostEqual(prikaz.lmtPrice, 4.10)

    async def test_preceneni_umi_prejit_na_jinou_prirazku(self):
        position = await self.nakup_vyplnen(quantity=3, cena=4.00)
        await self.engine.sell(position.id, "markup", SELL_SCOPE_ALL, markup_pct=5.0)
        await self.engine.sell(
            position.id, "markup", SELL_SCOPE_ALL, reprice=True, markup_pct=1.0
        )
        # Nákup 4,00 o 1 % výš je 4,04, na rastru 0,05 tedy 4,05
        self.assertAlmostEqual(position.sell_limit, 4.05)
        self.assertAlmostEqual(position.sell_markup_pct, 1.0)
        self.assertEqual(len(self.ib.placed), 2)

    async def test_prirazka_u_prodeje_za_kotaci_se_odmitne(self):
        # Limit by přirážka nezměnila, jen by se zapsala do hlášky
        position = await self.nakup_vyplnen(quantity=3)
        with self.assertRaises(ValueError):
            await self.engine.sell(position.id, "ask", SELL_SCOPE_ALL, markup_pct=5.0)
        self.assertEqual(len(self.ib.placed), 1)

    async def test_prirazka_nepotrebuje_kotaci(self):
        # Bez kotace zbývá jako základ nákupní cena z vyplněného nákupu
        position = await self.nakup_vyplnen(quantity=3, cena=4.00)
        self.ib.price_bid = self.ib.price_ask = None
        await self.engine.sell(position.id, "markup", SELL_SCOPE_ALL, markup_pct=5.0)
        self.assertAlmostEqual(self.ib.placed[-1].order.lmtPrice, 4.20)


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


class TestProdejPoKusech(ZakladEnginu):
    """Řádek s jedním kusem umožňuje odprodávat pozici postupně."""

    async def test_prodej_jednoho_kusu(self):
        position = await self.nakup_vyplnen(quantity=4)
        await self.prodej_vyplnen(position, "bid", SELL_SCOPE_ONE, cena=3.60)
        self.assertEqual(self.ib.placed[-1].order.totalQuantity, 1)
        self.assertEqual(position.open_quantity, 3)
        self.assertEqual(position.state, PositionState.OPEN)

    async def test_prodej_tri_kusu(self):
        position = await self.nakup_vyplnen(quantity=10)
        await self.prodej_vyplnen(position, "bid", SELL_SCOPE_THREE, cena=3.60)
        self.assertEqual(self.ib.placed[-1].order.totalQuantity, 3)
        self.assertEqual(position.open_quantity, 7)
        self.assertEqual(position.state, PositionState.OPEN)

    async def test_opakovanym_prodejem_lze_pozici_vyprodat(self):
        position = await self.nakup_vyplnen(quantity=3)
        for zbyva in (2, 1, 0):
            await self.prodej_vyplnen(position, "bid", SELL_SCOPE_ONE, cena=3.60)
            self.assertEqual(position.open_quantity, zbyva)
        self.assertEqual(position.state, PositionState.CLOSED)
        self.assertEqual(position.sold_quantity, 3)

    async def test_hlaska_o_zbytku_nemluvi_o_runneru(self):
        # Runner se jmenuje jen tam, kde o něj skutečně jde
        position = await self.nakup_vyplnen(quantity=4)
        await self.engine.sell(position.id, "bid", SELL_SCOPE_ONE)
        self.assertIn("v pozici zbývá 3 ks", position.message)
        self.assertNotIn("runner", position.message)

    async def test_hlaska_u_zakladni_pozice_runner_zminuje(self):
        position = await self.nakup_vyplnen(quantity=4)
        await self.engine.sell(position.id, "bid", SELL_SCOPE_BASE)
        self.assertIn("runner", position.message)

    async def test_neznamy_rozsah_engine_odmitne(self):
        position = await self.nakup_vyplnen(quantity=3)
        with self.assertRaises(ValueError):
            await self.engine.sell(position.id, "bid", "polovina")


class TestOdpoctuOtevreniBurzy(ZakladEnginu):
    """Odpočet do otevření burzy pro hlavičku rozhraní."""

    def burza(self, hodina: int, minuta: int, den: int = 19) -> None:
        """Podvrhne čas burzy - srpen 2026, výchozí den je středa 19. 8."""
        self.engine._exchange_now = lambda: datetime(
            2026, 8, den, hodina, minuta, tzinfo=ZoneInfo("America/New_York")
        )

    def test_pred_otevrenim_zbyva_cas_do_dnesni_seance(self):
        self.burza(8, 30)
        self.assertAlmostEqual(self.engine.market_open_seconds(), 3600.0)

    def test_behem_seance_se_odpocet_nemeri(self):
        self.burza(11, 0)
        self.assertIsNone(self.engine.market_open_seconds())

    def test_po_zavreni_miri_odpocet_na_dalsi_den(self):
        self.burza(17, 30)
        self.assertAlmostEqual(self.engine.market_open_seconds(), 16 * 3600.0)

    def test_o_vikendu_se_ceka_na_pondeli(self):
        # Pátek 21. 8. 2026 po zavření - nejbližší otevření je až v pondělí
        self.burza(17, 30, den=21)
        self.assertAlmostEqual(self.engine.market_open_seconds(), 64 * 3600.0)

        # Sobota 22. 8. 2026 dopoledne - stále se čeká na pondělní otevření
        self.burza(9, 0, den=22)
        self.assertAlmostEqual(self.engine.market_open_seconds(), (48 + 0.5) * 3600.0)

    def test_hodiny_burzy_se_berou_z_konfigurace(self):
        self.cfg.trading.exchange_open_time = "10:00"
        self.burza(8, 30)
        self.assertAlmostEqual(self.engine.market_open_seconds(), 5400.0)


class TestVelikostiUctu(ZakladEnginu):
    """Velikost účtu se přebírá z TWS a slouží k procentům v přehledu."""

    async def tik_ucet(self) -> None:
        """
        Protočí smyčku a počká na doběhnutí dotazu na velikost účtu.
        Ten běží ve vlastní úloze, aby smyčku nezdržel, takže samotný
        průchod o jeho výsledku ještě nic neví.
        """
        await self.tik()
        if self.engine._account_task is not None:
            await self.engine._account_task

    async def test_prvni_pruchod_prevezme_hodnotu_z_tws(self):
        self.assertEqual(self.engine.account_size, 0.0)
        await self.tik_ucet()
        self.assertAlmostEqual(self.engine.account_size, 12345.0)

    async def test_prevzeti_se_ohlasi_v_prubehu(self):
        await self.tik_ucet()
        self.assertTrue(any("Velikost účtu" in text for _, text in self.engine.events))
        # Další průchody už rutinní obnovu nehlásí
        pocet = len(self.engine.events)
        self.engine._account_checked = 0.0
        await self.tik_ucet()
        self.assertEqual(len(self.engine.events), pocet)

    async def test_hodnota_se_obnovuje_az_po_uplynuti_intervalu(self):
        await self.tik_ucet()
        self.ib.net_liquidation_value = 20000.0

        # Hned po převzetí se do TWS znovu nesahá
        await self.tik_ucet()
        self.assertAlmostEqual(self.engine.account_size, 12345.0)

        # Po uplynutí intervalu se hodnota převezme znovu
        self.engine._account_checked = 0.0
        await self.tik_ucet()
        self.assertAlmostEqual(self.engine.account_size, 20000.0)

    async def test_bez_hodnoty_z_tws_zustava_nula(self):
        # TWS souhrn účtu neposlala - procenta se v přehledu nepočítají
        self.ib.net_liquidation_value = None
        await self.tik_ucet()
        self.assertEqual(self.engine.account_size, 0.0)

    async def test_nulovy_interval_prebirani_vypne(self):
        self.cfg.engine.account_refresh_sec = 0.0
        await self.tik_ucet()
        self.assertEqual(self.engine.account_size, 0.0)

    async def test_dotaz_na_ucet_nezdrzi_smycku(self):
        # Mlčící TWS nesmí zastavit monitoring pozic - průchod smyčkou proto
        # na dotaz nečeká a nechá ho běžet vedle
        zdrzeni = asyncio.Event()

        async def pomala_odpoved() -> float | None:
            await zdrzeni.wait()
            return 12345.0

        self.ib.net_liquidation = pomala_odpoved
        await asyncio.wait_for(self.tik(), 1.0)
        self.assertEqual(self.engine.account_size, 0.0)
        self.assertFalse(self.engine._account_task.done())

        # Jakmile TWS odpoví, hodnota dorazí i bez dalšího průchodu smyčkou
        zdrzeni.set()
        await self.engine._account_task
        self.assertAlmostEqual(self.engine.account_size, 12345.0)

    async def test_druhy_dotaz_nevznikne_dokud_prvni_bezi(self):
        zdrzeni = asyncio.Event()

        async def pomala_odpoved() -> float | None:
            await zdrzeni.wait()
            return 12345.0

        self.ib.net_liquidation = pomala_odpoved
        await self.tik()
        uloha = self.engine._account_task
        # Další průchod nesmí založit druhý dotaz na týž údaj
        self.engine._account_checked = 0.0
        await self.tik()
        self.assertIs(self.engine._account_task, uloha)

        zdrzeni.set()
        await uloha
