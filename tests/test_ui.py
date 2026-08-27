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
from tws_rucne.engine import ManualEngine
from tws_rucne.models import ASK_MARKUPS, SELL_SCOPE_BASE
from tws_rucne.ui import TradingUI


class TestObsluhaTlacitek(unittest.TestCase):
    """Argumenty tlačítek musí projít přes rozhraní až do enginu."""

    def test_prodej_prijme_vse_co_karta_posila(self):
        # Karta předává: pozici, druh ceny, rozsah, příznak přecenění a přirážku
        inspect.signature(TradingUI.sell).bind(
            None, "AAPL-1", "ask", SELL_SCOPE_BASE, True, 5.0
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
    """Každá přirážka z nabídky musí dávat vyšší cenu než samotná poptávka."""

    def test_prirazky_rostou_a_lezi_nad_poptavkou(self):
        poptavka = calc.sell_limit_price("ask", 3.00, 3.20)
        predchozi = poptavka
        for prirazka in ASK_MARKUPS:
            cena = calc.sell_limit_price("ask", 3.00, 3.20, 0.0, prirazka)
            self.assertGreater(cena, poptavka)
            self.assertGreater(cena, predchozi - 1e-9)
            predchozi = cena

    def test_nabidka_jde_od_nejjistejsiho_vyplneni_k_nejvyssi_cene(self):
        ceny = [
            calc.sell_limit_price(kind, 3.00, 3.20, 0.0, markup)
            for kind, markup in [("bid", 0.0), ("mid", 0.0), ("ask", 0.0)]
            + [("ask", p) for p in ASK_MARKUPS]
        ]
        self.assertEqual(ceny, sorted(ceny))


if __name__ == "__main__":
    unittest.main()
