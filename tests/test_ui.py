"""
Testy napojení rozhraní na engine.

Rozhraní se tu nevykresluje - ověřuje se, že obsluha tlačítek přijme přesně
ty argumenty, se kterými ji karta pozice a formulář volají. Právě tady vznikla
chyba, kdy engine už uměl přirážku nad středem trhu, ale obsluha v rozhraní
ještě ne, a stisk tlačítka skončil výjimkou až v prohlížeči.
"""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tws_rucne import calc
from tws_rucne.config import AppConfig
from tws_rucne.engine import ManualEngine
from tws_rucne.models import SELL_SCOPE_BASE
from tws_rucne.ui import TradingUI, format_countdown

# Přirážky nabízené tlačítky - stejné hodnoty, jaké vezme rozhraní z konfigurace
ENTRY_MARKUPS = AppConfig().trading.entry_markups_pct


class TestObsluhaTlacitek(unittest.TestCase):
    """Argumenty tlačítek musí projít přes rozhraní až do enginu."""

    def test_prodej_prijme_vse_co_karta_posila(self):
        # Karta předává: pozici, druh ceny, rozsah, příznak přecenění a přirážku
        inspect.signature(TradingUI.sell).bind(
            None, "AAPL-1", "entry", SELL_SCOPE_BASE, True, 5.0
        )

    def test_engine_prijme_od_rozhrani_totez(self):
        inspect.signature(ManualEngine.sell).bind(
            None, "AAPL-1", "mid", SELL_SCOPE_BASE, True, 5.0
        )

    def test_prodej_ma_v_rozhrani_i_enginu_stejne_parametry(self):
        v_rozhrani = list(inspect.signature(TradingUI.sell).parameters)
        v_enginu = list(inspect.signature(ManualEngine.sell).parameters)
        self.assertEqual(v_rozhrani, v_enginu)

    def test_nakup_prijme_argumenty_z_formulare(self):
        inspect.signature(TradingUI.buy).bind(None, "ask")

    def test_preceneni_nakupu_prijme_argumenty_z_karty(self):
        inspect.signature(TradingUI.reprice_buy).bind(None, "AAPL-1", "ask")
        inspect.signature(ManualEngine.reprice_buy).bind(None, "AAPL-1", "ask")

    def test_sprava_pozice_prijme_identifikator(self):
        inspect.signature(TradingUI.cancel_order).bind(None, "AAPL-1")
        inspect.signature(TradingUI.remove_position).bind(None, "AAPL-1")


class TestNabizenePrirazky(unittest.TestCase):
    """Každá přirážka z nabídky musí dávat vyšší cenu než samotný nákup."""

    def test_prirazky_rostou_a_lezi_nad_vstupni_cenou(self):
        # Nákup 2,01 při kotaci 1,94 / 1,96 - pozice je ve ztrátě, přirážky
        # přesto míří nad nákupní cenu, ne nad poptávku
        predchozi = 2.01
        for prirazka in ENTRY_MARKUPS:
            cena = calc.sell_limit_price("entry", 1.94, 1.96, 0.0, prirazka, 2.01)
            self.assertGreater(cena, 2.01)
            self.assertGreater(cena, predchozi - 1e-9)
            predchozi = cena

    def test_prodej_za_kotaci_jde_od_nejjistejsiho_vyplneni(self):
        ceny = [
            calc.sell_limit_price(kind, 3.00, 3.20)
            for kind in ("bid", "mid", "ask")
        ]
        self.assertEqual(ceny, sorted(ceny))


class TestFormatuOdpoctu(unittest.TestCase):
    """Odpočet v hlavičce se zkracuje podle toho, kolik času zbývá."""

    def test_pod_hodinu_jen_minuty_a_sekundy(self):
        self.assertEqual(format_countdown(95), "01:35")

    def test_do_dne_i_s_hodinami(self):
        self.assertEqual(format_countdown(3 * 3600 + 5 * 60 + 7), "3:05:07")

    def test_pres_den_i_s_poctem_dni(self):
        self.assertEqual(format_countdown(64 * 3600), "2 d 16:00:00")

    def test_zaporny_cas_je_nula(self):
        self.assertEqual(format_countdown(-5), "00:00")


if __name__ == "__main__":
    unittest.main()
