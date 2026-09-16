"""Testy dostupnosti tlačítek a popisků podle stavu pozice."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tws_rucne.models import (
    SELL_SCOPE_ALL,
    SELL_SCOPE_BASE,
    SELL_SCOPE_ONE,
    Position,
    PositionState,
    buy_button_label,
    order_pnl_text,
    pnl_text,
    price_kind_label,
    sell_button_text,
    ukoncene_pozice_text,
)


def pozice(quantity: int = 3, filled: int = 3, sold: int = 0, runner: int = 1) -> Position:
    """Otevřená pozice s daným množstvím pro testy dostupnosti tlačítek."""
    return Position(
        id="AAPL-1",
        symbol="AAPL",
        right="C",
        quantity=quantity,
        filled_quantity=filled,
        sold_quantity=sold,
        runner_quantity=runner,
        fill_price=3.20,
        state=PositionState.OPEN,
    )


class TestDostupnostTlacitek(unittest.TestCase):
    """Která tlačítka smí obchodník v daném stavu vidět."""

    def test_pred_nakupem_zadny_prodej(self):
        p = pozice(filled=0)
        p.state = PositionState.BUYING
        self.assertFalse(p.can_sell_all)
        self.assertFalse(p.can_sell_base)
        # Nevyřízený nákup lze stáhnout z trhu
        self.assertTrue(p.can_cancel)

    def test_po_nakupu_jsou_dostupne_oba_druhy_prodeje(self):
        p = pozice()
        self.assertTrue(p.can_sell_all)
        self.assertTrue(p.can_sell_base)
        self.assertEqual(p.sell_quantity_for(SELL_SCOPE_ALL), 3)
        # Základní pozice je množství snížené o runner
        self.assertEqual(p.sell_quantity_for(SELL_SCOPE_BASE), 2)

    def test_jeden_kontrakt_nenabizi_prodej_zakladni_pozice(self):
        p = pozice(quantity=1, filled=1)
        self.assertTrue(p.can_sell_all)
        self.assertFalse(p.can_sell_base)
        self.assertEqual(p.sell_quantity_for(SELL_SCOPE_BASE), 0)

    def test_cerstvy_nakup_o_velikosti_runneru_jeste_runnerem_neni(self):
        # Runnerem je až zbytek po odprodeji základní části, ne nedotčený nákup
        p = pozice(quantity=1, filled=1)
        self.assertEqual(p.open_quantity, 1)
        self.assertFalse(p.is_runner_only)

    def test_po_prodeji_zakladni_pozice_zbyva_jen_prodej_vseho(self):
        # Ze tří kusů se prodaly dva, v pozici zůstal runner
        p = pozice(sold=2)
        self.assertEqual(p.open_quantity, 1)
        self.assertTrue(p.is_runner_only)
        self.assertTrue(p.can_sell_all)
        self.assertFalse(p.can_sell_base)

    def test_s_prodejnim_prikazem_zustavaji_tlacitka_pro_preceneni(self):
        # Příkaz v trhu tlačítka neschovává - opakovaný stisk jej přecení
        p = pozice()
        p.state = PositionState.SELLING
        self.assertTrue(p.sell_pending)
        self.assertTrue(p.can_sell_all)
        self.assertTrue(p.can_sell_base)
        self.assertTrue(p.can_cancel)

    def test_nevyplneny_nakup_lze_precenit(self):
        p = pozice(filled=0)
        p.state = PositionState.BUYING
        self.assertTrue(p.can_reprice_buy)
        # Otevřená pozice už nákupní příkaz v trhu nemá
        self.assertFalse(pozice().can_reprice_buy)

    def test_stisknute_prodejni_tlacitko(self):
        # Zvýrazní se jen tlačítko, kterým příkaz vznikl - druh, rozsah i přirážka
        p = pozice()
        p.state = PositionState.SELLING
        p.sell_kind, p.sell_scope, p.sell_markup_pct = "markup", SELL_SCOPE_ALL, 5.0
        self.assertTrue(p.is_pressed_sell_button("markup", SELL_SCOPE_ALL, 5.0))
        self.assertFalse(p.is_pressed_sell_button("markup", SELL_SCOPE_ALL, 3.0))
        self.assertFalse(p.is_pressed_sell_button("markup", SELL_SCOPE_BASE, 5.0))
        self.assertFalse(p.is_pressed_sell_button("bid", SELL_SCOPE_ALL))

    def test_bez_prikazu_v_trhu_neni_nic_stisknuto(self):
        # Po vyplnění prodeje zůstávají údaje o příkazu, zvýraznění ale mizí
        p = pozice()
        p.sell_kind, p.sell_scope = "bid", SELL_SCOPE_ALL
        self.assertFalse(p.is_pressed_sell_button("bid", SELL_SCOPE_ALL))
        self.assertFalse(p.is_pressed_buy_button("ask"))

    def test_stisknute_nakupni_tlacitko(self):
        p = pozice(filled=0)
        p.state = PositionState.BUYING
        p.buy_kind = "mid"
        self.assertTrue(p.is_pressed_buy_button("mid"))
        self.assertFalse(p.is_pressed_buy_button("ask"))

    def test_uzavrenou_pozici_lze_odstranit_z_prehledu(self):
        p = pozice(sold=3)
        p.state = PositionState.CLOSED
        self.assertEqual(p.open_quantity, 0)
        self.assertFalse(p.can_sell_all)
        self.assertFalse(p.can_cancel)
        self.assertTrue(p.can_remove)

    def test_vetsi_runner_zvetsuje_zbytek(self):
        # Runner dva kusy: z pěti se prodají tři
        p = pozice(quantity=5, filled=5, runner=2)
        self.assertEqual(p.sell_quantity_for(SELL_SCOPE_BASE), 3)

    def test_prodej_jednoho_kusu(self):
        # Ze tří kusů (runner 1) lze odprodat jediný kontrakt
        p = pozice()
        self.assertTrue(p.can_sell_one)
        self.assertEqual(p.sell_quantity_for(SELL_SCOPE_ONE), 1)

    def test_jeden_kus_se_nenabizi_u_jednokontraktove_pozice(self):
        # Prodej jednoho kusu by dělal totéž co prodej všeho
        self.assertFalse(pozice(quantity=1, filled=1).can_sell_one)

    def test_jeden_kus_se_nenabizi_kdyz_je_shodny_se_zakladni_pozici(self):
        # Ze dvou kusů s runnerem 1 vychází základní pozice také na 1 ks
        p = pozice(quantity=2, filled=2)
        self.assertEqual(p.sell_quantity_for(SELL_SCOPE_BASE), 1)
        self.assertFalse(p.can_sell_one)

    def test_vetsi_runner_prodej_jednoho_kusu_neblokuje(self):
        # Ze čtyř kusů s runnerem 2 vychází základní pozice na 2 ks
        p = pozice(quantity=4, filled=4, runner=2)
        self.assertTrue(p.can_sell_one)

    def test_neznamy_rozsah_je_chyba(self):
        with self.assertRaises(ValueError):
            pozice().sell_quantity_for("polovina")


class TestVysledekPozice(unittest.TestCase):
    """Výpočet výsledku pozice z nákupu, prodejů a provizí."""

    def test_nerealizovany_vysledek_ze_stredu_trhu(self):
        p = pozice()
        p.option_bid, p.option_ask = 3.60, 3.80
        # Střed 3,70 proti nákupu za 3,20 na třech kontraktech
        self.assertAlmostEqual(p.unrealized_pnl, 150.0)

    def test_realizovany_a_celkovy_vysledek(self):
        p = pozice(sold=2)
        p.sold_value = 2 * 3.70
        p.option_bid, p.option_ask = 3.60, 3.80
        self.assertAlmostEqual(p.realized_pnl, 100.0)
        # Zbylý runner se oceňuje středem trhu
        self.assertAlmostEqual(p.unrealized_pnl, 50.0)
        self.assertAlmostEqual(p.gross_pnl, 150.0)

    def test_vysledek_se_deli_az_po_castecnem_prodeji(self):
        # Dokud se nic neprodalo, není co dělit
        self.assertFalse(pozice().pnl_split)
        # Runner po odprodeji základní pozice se ukazuje zvlášť
        self.assertTrue(pozice(sold=2).pnl_split)
        # Uzavřená pozice už nic nedrží
        self.assertFalse(pozice(sold=3).pnl_split)

    def test_provize_snizuji_cisty_vysledek(self):
        p = pozice(sold=3)
        p.sold_value = 3 * 3.70
        p.buy_commissions = {"EXEC-1": 2.0}
        p.sell_commissions = {"EXEC-2": 2.5}
        self.assertAlmostEqual(p.gross_pnl, 150.0)
        self.assertAlmostEqual(p.net_pnl, 145.5)

    def test_provize_se_deli_mezi_prodanou_a_drzenou_cast(self):
        # Ze čtyř nakoupených kusů jsou dva prodané: nákupní provize se dělí
        # napůl, prodejní patří celá k realizované části
        p = pozice(quantity=4, filled=4, sold=2)
        p.buy_commissions = {"EXEC-1": 4.0}
        p.sell_commissions = {"EXEC-2": 2.0}
        self.assertAlmostEqual(p.open_commission, 2.0)
        self.assertAlmostEqual(p.realized_commission, 4.0)
        self.assertAlmostEqual(p.commission_total, 6.0)

    def test_bez_nakupu_zadna_provize_na_otevrenou_cast(self):
        p = pozice(quantity=2, filled=0)
        p.buy_commissions = {"EXEC-1": 1.0}
        self.assertAlmostEqual(p.open_commission, 0.0)
        self.assertAlmostEqual(p.realized_commission, 1.0)


class TestPopiskyTlacitek(unittest.TestCase):
    """Popisky musí obchodníkovi říct, za jakou cenu a kolik kusů odejde."""

    def test_popisek_nakupu(self):
        self.assertEqual(buy_button_label("ask", 3), "3 ks za ASK")
        self.assertEqual(buy_button_label("mid", 1), "1 ks za MID")

    def test_popisek_prodeje(self):
        # Bez kotace zůstane na tlačítku jen druh ceny a počet kusů
        self.assertEqual(sell_button_text("bid", 3, None, None), "BID (3 ks)")
        self.assertEqual(sell_button_text("mid", 1, None, None), "MID (1 ks)")

    def test_oba_radky_maji_stejny_tvar_popisku(self):
        # Stejná délka popisku drží tlačítka obou řádků ve sloupcích nad sebou
        vse = sell_button_text("bid", 3, None, None)
        runner = sell_button_text("bid", 1, None, None)
        self.assertEqual(len(vse), len(runner))

    def test_popis_ceny(self):
        self.assertEqual(price_kind_label("ask"), "ASK")
        self.assertEqual(price_kind_label("bid"), "BID")
        self.assertEqual(price_kind_label("mid"), "MID")
        self.assertEqual(price_kind_label("markup", 2.0), "přirážka +2 %")
        # Desetinná přirážka se nepíše se zbytečnou nulou
        self.assertEqual(price_kind_label("markup", 2.5), "přirážka +2.5 %")

    def test_neznamy_druh_ceny_hlasi_srozumitelnou_chybu(self):
        # Stejně jako výpočty limitních cen, ne holým KeyError
        with self.assertRaises(ValueError):
            price_kind_label("lmt")


class TestVysledkuNaTlacitku(unittest.TestCase):
    """Za limitní cenou nese tlačítko zisk nebo ztrátu, kterou prodej přinese."""

    def _pozice(self) -> Position:
        """Otevřená pozice se třemi nakoupenými kontrakty za 2.23."""
        p = Position(id="NVDA-1", symbol="NVDA", quantity=3)
        p.filled_quantity = 3
        p.fill_price = 2.23
        return p

    def test_zisk_za_prodavane_mnozstvi(self):
        # Kontrakt kryje 100 kusů podkladu: (2.30 - 2.23) * 100 * 2 = 14 USD
        self.assertAlmostEqual(self._pozice().sell_pnl_at(2.30, 2), 14.0)

    def test_ztrata_pod_nakupni_cenou(self):
        self.assertAlmostEqual(self._pozice().sell_pnl_at(2.20, 1), -3.0)

    def test_bez_nakupni_ceny_neni_co_pocitat(self):
        # Nevyplněný nákup ještě cenu nemá
        self.assertIsNone(Position(id="NVDA-2").sell_pnl_at(2.30, 1))

    def test_bez_kotace_neni_co_pocitat(self):
        self.assertIsNone(self._pozice().sell_pnl_at(None, 1))

    def test_drobny_vysledek_se_pise_jako_nula(self):
        # Znaménko u dvaceti centů by se pletlo se skutečným ziskem
        self.assertEqual(
            sell_button_text("bid", 1, 3.00, 0.2), "BID (1 ks) · 3.00 · 0 USD"
        )
        self.assertEqual(
            sell_button_text("bid", 1, 3.00, -0.2), "BID (1 ks) · 3.00 · 0 USD"
        )

    def test_vysledek_do_vety_o_prikazu(self):
        # Ve větě nese znaménko slovo, číslo zůstává bez něj
        self.assertEqual(order_pnl_text(45.0), "zisk 45 USD")
        self.assertEqual(order_pnl_text(-60.0), "ztráta 60 USD")

    def test_vyrovnany_prodej_ve_vete(self):
        self.assertEqual(order_pnl_text(0.2), "bez zisku i ztráty")

    def test_chybejici_vysledek_vetu_nesklada(self):
        self.assertEqual(order_pnl_text(None), "")

    def test_cely_popisek_tlacitka(self):
        # Prodej tří kusů za BID 3.10 při nákupu za 3.00
        self.assertEqual(
            sell_button_text("bid", 3, 3.10, 30.0), "BID (3 ks) · 3.10 · +30 USD"
        )

    def test_cely_popisek_tlacitka_s_prirazkou(self):
        # Tlačítko s přirážkou nese místo druhu ceny procenta - základ (vstup,
        # nebo ASK) se mění podle trhu a počet kusů je jasný z tlačítek vedle
        self.assertEqual(
            sell_button_text("markup", 2, 3.30, 60.0, 3.0), "+3 % · 3.30 · +60 USD"
        )

    def test_popisek_bez_nakupni_ceny_nese_jen_cenu(self):
        self.assertEqual(sell_button_text("ask", 1, 3.20, None), "ASK (1 ks) · 3.20")


if __name__ == "__main__":
    unittest.main()


class TestHlaskaOUklidu(unittest.TestCase):
    """Hláška o odstraněných pozicích musí být česky správně."""

    def test_jedna_pozice(self):
        self.assertEqual(ukoncene_pozice_text(1), "odstraněna 1 ukončená pozice")

    def test_dve_az_ctyri_pozice(self):
        self.assertEqual(ukoncene_pozice_text(2), "odstraněny 2 ukončené pozice")
        self.assertEqual(ukoncene_pozice_text(4), "odstraněny 4 ukončené pozice")

    def test_pet_a_vic_pozic(self):
        self.assertEqual(ukoncene_pozice_text(5), "odstraněno 5 ukončených pozic")
        self.assertEqual(ukoncene_pozice_text(12), "odstraněno 12 ukončených pozic")


class TestZobrazeniVysledku(unittest.TestCase):
    """U pozice s runnerem se ukazuje jeho výsledek a v závorce celek."""

    def test_bez_runneru_jen_celek(self):
        self.assertEqual(pnl_text(None, 153.66, False), "153.66 USD")

    def test_s_runnerem_zbytek_a_v_zavorce_celek(self):
        self.assertEqual(pnl_text(53.35, 153.66, True), "53.35 (153.66) USD")

    def test_zaporny_vysledek(self):
        self.assertEqual(pnl_text(-12.5, 144.88, True), "-12.50 (144.88) USD")

    def test_tisice_se_oddeluji_mezerou(self):
        self.assertEqual(pnl_text(1234.5, 2345.67, True), "1 234.50 (2 345.67) USD")

    def test_bez_znamych_cen_pomlcka(self):
        self.assertEqual(pnl_text(None, None, False), "-")
        self.assertEqual(pnl_text(None, None, True), "- (-) USD")
