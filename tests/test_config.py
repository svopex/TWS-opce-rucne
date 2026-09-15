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


class TestRezervacnichSouboru(unittest.TestCase):
    """Cesty ke stavům jiných aplikací se ověřují jako seznam."""

    def test_seznam_cest_projde(self):
        cfg = AppConfig()
        cfg.strike.reserved_state_files = ["../TWS-opce/state.json"]
        validate_config(cfg)

    def test_jedina_cesta_misto_seznamu_neprojde(self):
        # Zápis bez pomlčky v YAML by se jinak četl po písmenech
        cfg = AppConfig()
        cfg.strike.reserved_state_files = "../TWS-opce/state.json"
        with self.assertRaises(ValueError) as chyba:
            validate_config(cfg)
        self.assertIn("reserved_state_files", str(chyba.exception))


class TestPrirazekNadAsk(unittest.TestCase):
    """Přirážky prodejních tlačítek se ověřují jako seznam kladných procent."""

    def _chyba(self, cfg: AppConfig) -> str:
        """Spustí validaci a vrátí text vyhozené chyby."""
        with self.assertRaises(ValueError) as chyba:
            validate_config(cfg)
        return str(chyba.exception)

    def test_desetinne_prirazky_projdou(self):
        cfg = AppConfig()
        cfg.trading.ask_markups_pct = [0.5, 1.0, 1.5, 2.25]
        validate_config(cfg)

    def test_prazdny_seznam_prirazky_vypne(self):
        cfg = AppConfig()
        cfg.trading.ask_markups_pct = []
        validate_config(cfg)

    def test_jedine_cislo_misto_seznamu_neprojde(self):
        cfg = AppConfig()
        cfg.trading.ask_markups_pct = 1.0
        self.assertIn("ask_markups_pct", self._chyba(cfg))

    def test_zaporna_prirazka_neprojde(self):
        # Prodej pod poptávanou cenou nabízí tlačítko BID, ne přirážka
        cfg = AppConfig()
        cfg.trading.ask_markups_pct = [1.0, -2.0]
        self.assertIn("ask_markups_pct", self._chyba(cfg))

    def test_nula_neprojde(self):
        # Nulová přirážka by jen zdvojila tlačítko ASK
        cfg = AppConfig()
        cfg.trading.ask_markups_pct = [0.0]
        self.assertIn("ask_markups_pct", self._chyba(cfg))

    def test_text_misto_cisla_neprojde(self):
        cfg = AppConfig()
        cfg.trading.ask_markups_pct = ["1 %"]
        self.assertIn("ask_markups_pct", self._chyba(cfg))

    def test_pravdivostni_hodnota_neprojde(self):
        # true je v Pythonu podtyp int a jinak by prošlo jako 1 %
        cfg = AppConfig()
        cfg.trading.ask_markups_pct = [True]
        self.assertIn("ask_markups_pct", self._chyba(cfg))


if __name__ == "__main__":
    unittest.main()
