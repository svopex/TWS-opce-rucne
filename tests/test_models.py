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
    pnl_text,
    price_kind_label,
    sell_button_label,
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
        self.assertEqual(sell_button_label("bid", 3), "BID (3 ks)")
        self.assertEqual(sell_button_label("mid", 1), "MID (1 ks)")

    def test_oba_radky_maji_stejny_tvar_popisku(self):
        # Stejná délka popisku drží tlačítka obou řádků ve sloupcích nad sebou
        vse = sell_button_label("bid", 3)
        runner = sell_button_label("bid", 1)
        self.assertEqual(len(vse), len(runner))

    def test_popisek_prirazky_nad_poptavkou(self):
        # U přirážek se počet nepíše - v řádku je jasný z tlačítek vedle
        self.assertEqual(sell_button_label("ask", 3, 1.0), "ASK +1 %")
        self.assertEqual(sell_button_label("ask", 2, 5.0), "ASK +5 %")

    def test_popis_ceny(self):
        self.assertEqual(price_kind_label("ask"), "ASK")
        self.assertEqual(price_kind_label("bid"), "BID")
        self.assertEqual(price_kind_label("mid"), "MID")
        self.assertEqual(price_kind_label("ask", 2.0), "ASK +2 %")
        # Desetinná přirážka se nepíše se zbytečnou nulou
        self.assertEqual(price_kind_label("ask", 2.5), "ASK +2.5 %")


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
