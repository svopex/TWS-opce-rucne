"""Testy výpočetních funkcí - výběr strike, expirace a limitní ceny."""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tws_rucne import calc


class TestLimitCeny(unittest.TestCase):
    """Výběr striku podle ceny opce a minimální delty."""

    def test_strike_za_strikem_mimo_penize(self):
        strikes = [225.0, 227.5, 230.0, 232.5, 235.0]
        self.assertEqual(calc.strikes_beyond(strikes, 230.0, "C"), [232.5, 235.0])
        self.assertEqual(calc.strikes_beyond(strikes, 230.0, "P"), [227.5, 225.0])

    def test_poruseni_limitu(self):
        self.assertEqual(calc.strike_violation(450.0, 0.3, 500.0, 0.2), 0.0)
        self.assertAlmostEqual(calc.strike_violation(550.0, 0.3, 500.0, 0.2), 0.1)
        self.assertAlmostEqual(calc.strike_violation(450.0, -0.18, 500.0, 0.2), 0.1)
        # Neznámá cena se posoudit nedá, neznámá delta se nepočítá
        self.assertIsNone(calc.strike_violation(None, 0.3, 500.0, 0.2))
        self.assertEqual(calc.strike_violation(450.0, None, 500.0, 0.2), 0.0)

    def test_vyhrava_prvni_bez_poruseni(self):
        kandidati = [(1000.0, 0.5), (450.0, 0.3), (200.0, 0.21)]
        self.assertEqual(calc.pick_strike_candidate(kandidati, 500.0, 0.2), 1)

    def test_kompromis_s_mensim_porusenim(self):
        kandidati = [(1000.0, 0.5), (520.0, 0.25), (300.0, 0.12)]
        self.assertEqual(calc.pick_strike_candidate(kandidati, 500.0, 0.2), 1)

    def test_odhad_delty_z_ceny_opce(self):
        expirace = (date.today() + timedelta(days=1)).strftime("%Y%m%d")
        # Opce u peněz má deltu kolem 0,5, mimo peníze nižší
        u_penez = calc.estimate_delta(10.25, 512.0, 512.5, expirace, "C")
        mimo = calc.estimate_delta(2.0, 512.0, 525.0, expirace, "C")
        self.assertAlmostEqual(u_penez, 0.5, delta=0.05)
        self.assertLess(mimo, u_penez)
        self.assertLess(calc.estimate_delta(2.0, 512.0, 500.0, expirace, "P"), 0)
        self.assertIsNone(calc.estimate_delta(None, 512.0, 500.0, expirace, "C"))


class TestVyberStrike(unittest.TestCase):
    """Strike se určuje z aktuální ceny podkladu."""

    def setUp(self) -> None:
        # Rastr po 2,5 bodu kolem ceny 230
        self.strikes = [225.0, 227.5, 230.0, 232.5, 235.0]

    def test_call_bere_prvni_strike_nad_cenou(self):
        self.assertEqual(calc.otm_strike(self.strikes, 231.0, "C", 1), 232.5)

    def test_put_bere_prvni_strike_pod_cenou(self):
        self.assertEqual(calc.otm_strike(self.strikes, 231.0, "P", 1), 230.0)

    def test_druhy_krok_jde_dal_od_penez(self):
        self.assertEqual(calc.otm_strike(self.strikes, 231.0, "C", 2), 235.0)
        self.assertEqual(calc.otm_strike(self.strikes, 231.0, "P", 2), 227.5)

    def test_nulovy_krok_je_atm(self):
        # Nula kroků znamená nejbližší strike aktuální ceně
        self.assertEqual(calc.otm_strike(self.strikes, 231.0, "C", 0), 230.0)
        self.assertEqual(calc.otm_strike(self.strikes, 233.0, "P", 0), 232.5)

    def test_cena_presne_na_striku_lezi_dal(self):
        # Strike shodný s cenou není mimo peníze, bere se až ten následující
        self.assertEqual(calc.otm_strike(self.strikes, 230.0, "C", 1), 232.5)
        self.assertEqual(calc.otm_strike(self.strikes, 230.0, "P", 1), 227.5)

    def test_bez_striku_mimo_penize_zbyva_nejblizsi(self):
        # Celý řetězec leží pod cenou, CALL mimo peníze neexistuje
        self.assertEqual(calc.otm_strike(self.strikes, 300.0, "C", 1), 235.0)

    def test_prazdny_retezec_vraci_nic(self):
        self.assertIsNone(calc.otm_strike([], 230.0, "C", 1))


class TestVyberExpirace(unittest.TestCase):
    """Expirace se vybírá podle režimu z konfigurace."""

    def setUp(self) -> None:
        dnes = date.today()
        self.expirace = [
            (dnes + timedelta(days=dnu)).strftime("%Y%m%d") for dnu in (0, 2, 9, 30)
        ]

    def test_nejblizsi_respektuje_minimalni_pocet_dni(self):
        vybrana = calc.select_expiration(self.expirace, "nearest", 3)
        self.assertEqual(calc.days_to_expiry(vybrana), 9)

    def test_nulove_dte_bere_i_dnesni_expiraci(self):
        vybrana = calc.select_expiration(self.expirace, "nearest", 0)
        self.assertEqual(calc.days_to_expiry(vybrana), 0)

    def test_pevne_datum_mimo_nabidku_vraci_nic(self):
        self.assertIsNone(calc.select_expiration(self.expirace, "fixed", 0, "20200101"))


class TestLimitniCeny(unittest.TestCase):
    """Limitní ceny tlačítek pro nákup i prodej."""

    def test_nakup_za_ask(self):
        self.assertAlmostEqual(calc.buy_limit_price("ask", 3.00, 3.20), 3.20)

    def test_nakup_za_ask_s_toleranci(self):
        # Tolerance 5 % zvedne limit nad poptávanou cenu
        self.assertAlmostEqual(calc.buy_limit_price("ask", 3.00, 3.20, 5.0), 3.36)

    def test_nakup_za_mid(self):
        self.assertAlmostEqual(calc.buy_limit_price("mid", 3.00, 3.20), 3.10)

    def test_prodej_za_bid(self):
        self.assertAlmostEqual(calc.sell_limit_price("bid", 3.00, 3.20), 3.00)

    def test_prodej_za_bid_s_toleranci(self):
        # Tolerance 5 % sníží limit pod nabízenou cenu
        self.assertAlmostEqual(calc.sell_limit_price("bid", 3.00, 3.20, 5.0), 2.85)

    def test_prodej_za_mid(self):
        self.assertAlmostEqual(calc.sell_limit_price("mid", 3.00, 3.20), 3.10)

    def test_prodej_za_ask(self):
        # Kdo nespěchá, nechá příkaz čekat na poptávku a spread inkasuje
        self.assertAlmostEqual(calc.sell_limit_price("ask", 3.00, 3.20), 3.20)

    def test_prirazka_ve_ztrate_od_vstupu(self):
        # ASK 1,96 pod nákupem 2,01: 2,01 o 1 % dá 2,0301, o 9 % pak 2,1909
        self.assertAlmostEqual(calc.markup_sell_price(2.01, 1.96, 1.0), 2.0301)
        self.assertAlmostEqual(calc.markup_sell_price(2.01, 1.96, 9.0), 2.1909)

    def test_prirazka_v_zisku_od_ask(self):
        # ASK 2,16 nad nákupem 2,01 - přirážka nad vstupem by ležela pod trhem,
        # základem je proto ASK: 2,16 o 1 % dá 2,1816
        self.assertAlmostEqual(calc.markup_sell_price(2.01, 2.16, 1.0), 2.1816)

    def test_prirazka_bez_kotace_od_vstupu(self):
        self.assertAlmostEqual(calc.markup_sell_price(2.01, None, 1.0), 2.0301)

    def test_bez_vstupni_ceny_neni_limit(self):
        # Nevyplněný nákup ještě vstupní cenu nemá, výsledek by nešlo spočítat
        self.assertIsNone(calc.markup_sell_price(None, 2.16, 1.0))

    def test_mid_bez_uplne_kotace_neexistuje(self):
        self.assertIsNone(calc.buy_limit_price("mid", None, 3.20))
        self.assertIsNone(calc.sell_limit_price("mid", 3.00, None))

    def test_ask_bez_kotace_neexistuje(self):
        self.assertIsNone(calc.buy_limit_price("ask", 3.00, None))
        self.assertIsNone(calc.sell_limit_price("ask", 3.00, None))

    def test_neznamy_druh_je_chyba(self):
        # Nakupuje se za ASK nebo MID, za kotaci se prodává za BID, MID nebo ASK
        with self.assertRaises(ValueError):
            calc.buy_limit_price("bid", 3.00, 3.20)
        with self.assertRaises(ValueError):
            calc.sell_limit_price("last", 3.00, 3.20)


class TestZaokrouhleniNaTik(unittest.TestCase):
    """Limitní cena musí sedět na rastru kontraktu."""

    def test_zaokrouhleni_na_pet_centu(self):
        self.assertAlmostEqual(calc.round_to_tick(3.13, 0.05), 3.15)
        self.assertAlmostEqual(calc.round_to_tick(3.11, 0.05), 3.10)

    def test_bez_tiku_se_zaokrouhli_na_centy(self):
        self.assertAlmostEqual(calc.round_to_tick(3.126, 0), 3.13)

    def test_pulka_tiku_jde_nahoru(self):
        # Střed trhu 1,64 / 1,69 je přesně 1,665; v náhledu se zobrazuje 1,67
        # a stejnou cenu musí nabídnout i tlačítko
        self.assertAlmostEqual(calc.round_to_tick(1.665, 0.01), 1.67)
        self.assertAlmostEqual(calc.round_to_tick(3.125, 0.05), 3.15)

    def test_cena_na_tlacitku_sedi_se_zobrazenym_stredem(self):
        # Kontrola proti dřívější chybě: dělení v plovoucí čárce srazilo
        # cenu o celý tik dolů, takže tlačítko ukazovalo 1,66 místo 1,67
        stred = calc.mid_price(1.64, 1.69)
        self.assertEqual(f"{stred:.2f}", f"{calc.round_to_tick(stred, 0.01):.2f}")


class TestSpreadAVysledek(unittest.TestCase):
    """Spread a výsledek pozice."""

    def test_spread_v_procentech(self):
        # (3,20 - 3,00) / 3,10 = 6,45 %
        self.assertAlmostEqual(calc.spread_pct(3.00, 3.20), 6.4516, places=3)

    def test_spread_bez_kotace(self):
        self.assertIsNone(calc.spread_pct(None, 3.20))

    def test_hodnota_prikazu(self):
        # 3,10 USD za kontrakt krát 100 krát 3 kontrakty
        self.assertAlmostEqual(calc.order_value(3.10, 3), 930.0)

    def test_nerealizovany_vysledek(self):
        self.assertAlmostEqual(calc.position_pnl(3.00, 3.50, 2), 100.0)

    def test_realizovany_vysledek_z_vice_prodeju(self):
        # Dva kusy za 3,50 a jeden za 4,00 proti nákupu za 3,00
        hodnota = 2 * 3.50 + 1 * 4.00
        self.assertAlmostEqual(calc.realized_pnl(3.00, 3, hodnota), 200.0)

    def test_vysledek_bez_nakupni_ceny_neexistuje(self):
        self.assertIsNone(calc.position_pnl(None, 3.50, 2))
        self.assertIsNone(calc.realized_pnl(None, 1, 3.5))


class TestRozporPremieSPodkladem(unittest.TestCase):
    """Podklad jde ve prospěch opce, prémie přesto klesá."""

    def rozpor(self, right, podklad, premie, min_podklad=0.1, min_premie=2.0):
        """Rozpor proti vstupu s podkladem 200 a prémií 2,00."""
        return calc.premium_divergence(right, 200.0, podklad, 2.00, premie, min_podklad, min_premie)

    def test_call_podklad_nahoru_premie_dolu(self):
        vysledek = self.rozpor("C", 201.0, 1.90)
        self.assertIsNotNone(vysledek)
        podklad_pct, premie_pct = vysledek
        self.assertAlmostEqual(podklad_pct, 0.5)
        self.assertAlmostEqual(premie_pct, -5.0)

    def test_put_podklad_dolu_premie_dolu(self):
        podklad_pct, premie_pct = self.rozpor("P", 199.0, 1.90)
        # Znaménko odpovídá skutečnému pohybu podkladu
        self.assertAlmostEqual(podklad_pct, -0.5)
        self.assertAlmostEqual(premie_pct, -5.0)

    def test_podklad_proti_opci_neni_rozpor(self):
        # Prémie klesá, protože podklad jde proti opci - to je běžná ztráta
        self.assertIsNone(self.rozpor("C", 199.0, 1.90))
        self.assertIsNone(self.rozpor("P", 201.0, 1.90))

    def test_rostouci_premie_neni_rozpor(self):
        self.assertIsNone(self.rozpor("C", 201.0, 2.10))

    def test_pohyb_pod_prahy_neni_rozpor(self):
        # Podklad +0,05 % je pod prahem 0,1 %
        self.assertIsNone(self.rozpor("C", 200.1, 1.90))
        # Prémie -1 % je pod prahem 2 %
        self.assertIsNone(self.rozpor("C", 201.0, 1.98))

    def test_nulove_prahy_vyzaduji_skutecny_pohyb(self):
        self.assertIsNone(self.rozpor("C", 200.0, 1.99, 0.0, 0.0))
        self.assertIsNone(self.rozpor("C", 200.5, 2.00, 0.0, 0.0))
        self.assertIsNotNone(self.rozpor("C", 200.5, 1.99, 0.0, 0.0))

    def test_bez_vstupu_nebo_kotace_neni_rozpor(self):
        self.assertIsNone(calc.premium_divergence("C", None, 201.0, 2.00, 1.90, 0.1, 2.0))
        self.assertIsNone(calc.premium_divergence("C", 200.0, 201.0, None, 1.90, 0.1, 2.0))
        self.assertIsNone(self.rozpor("C", 201.0, None))


if __name__ == "__main__":
    unittest.main()
