"""
Testy validace konfiguračního souboru.

Chybná hodnota musí padnout hned při startu se srozumitelnou hláškou.
Hodiny burzy se totiž čtou až v periodické obnově rozhraní - neodhalený
překlep by tam vyhazoval výjimku každého půl sekundy a shodil by celé
překreslení stránky, tedy i přehled pozic a ceny na tlačítkách.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tws_rucne.config import AppConfig, validate_config


class TestVychoziKonfigurace(unittest.TestCase):
    """Výchozí hodnoty musí validací projít."""

    def test_vychozi_konfigurace_je_platna(self):
        validate_config(AppConfig())


class TestObchodniHodinyBurzy(unittest.TestCase):
    """Časová zóna a obchodní hodiny se ověřují už při načtení konfigurace."""

    def _chyba(self, cfg: AppConfig) -> str:
        """Spustí validaci a vrátí text vyhozené chyby."""
        with self.assertRaises(ValueError) as chyba:
            validate_config(cfg)
        return str(chyba.exception)

    def test_neznama_casova_zona(self):
        cfg = AppConfig()
        cfg.trading.exchange_timezone = "Europe/Prahaa"
        self.assertIn("exchange_timezone", self._chyba(cfg))

    def test_prazdna_casova_zona(self):
        cfg = AppConfig()
        cfg.trading.exchange_timezone = ""
        self.assertIn("exchange_timezone", self._chyba(cfg))

    def test_cas_otevreni_v_jinem_tvaru(self):
        cfg = AppConfig()
        cfg.trading.exchange_open_time = "9:30:00"
        self.assertIn("exchange_open_time", self._chyba(cfg))

    def test_cas_zavreni_mimo_rozsah(self):
        cfg = AppConfig()
        cfg.trading.exchange_close_time = "25:00"
        self.assertIn("exchange_close_time", self._chyba(cfg))

    def test_zavreni_pred_otevrenim(self):
        cfg = AppConfig()
        cfg.trading.exchange_open_time = "16:00"
        cfg.trading.exchange_close_time = "09:30"
        self.assertIn("později", self._chyba(cfg))

    def test_jednociferna_hodina_projde(self):
        # Zápis '9:30' je platný čas, jen bez vedoucí nuly
        cfg = AppConfig()
        cfg.trading.exchange_open_time = "9:30"
        validate_config(cfg)


if __name__ == "__main__":
    unittest.main()
