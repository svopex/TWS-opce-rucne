"""Testy čtení kontraktů zamluvených obchody jiné aplikace."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.zaklad import cizi_obchod as obchod, cizi_stav as stav
from tws_rucne.reservations import ReservedContracts


class TestRezervace(unittest.TestCase):
    """Rezervace se čtou z uloženého stavu druhé aplikace."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.soubor = Path(self.tmp.name) / "state.json"

    def zapis(self, data) -> None:
        """Zapíše stavový soubor druhé aplikace."""
        self.soubor.write_text(json.dumps(data), encoding="utf-8")

    def ctecka(self, own_state: str | None = None) -> ReservedContracts:
        """Čtečka nastavená na testovací soubor."""
        return ReservedContracts([str(self.soubor)], own_state)

    def test_cekajici_obchod_kontrakt_zamlouva(self):
        self.zapis(stav(obchod()))
        rezervace = self.ctecka().conids()
        self.assertEqual(list(rezervace), [900001])
        self.assertIn("AAPL 20260916 CALL 232.50", rezervace[900001])
        self.assertIn("AAPL-39", rezervace[900001])

    def test_ukoncene_obchody_kontrakt_neblokuji(self):
        self.zapis(
            stav(
                obchod(conid=900001, state="CLOSED"),
                obchod(conid=900002, state="CANCELLED"),
                obchod(conid=900003, state="MISSED"),
                obchod(conid=900004, state="ERROR"),
            )
        )
        self.assertEqual(self.ctecka().conids(), {})


    def test_drzena_pozice_druhe_aplikace_zamlouva(self):
        self.zapis(stav(obchod(state="FILLED")))
        self.assertIn(900001, self.ctecka().conids())

    def test_chybejici_soubor_nic_nezamlouva(self):
        self.assertEqual(self.ctecka().conids(), {})

    def test_zaznam_bez_conid_se_preskoci(self):
        self.zapis(stav(obchod(conid=0), {"id": "AAPL-40", "state": "NEW"}))
        self.assertEqual(self.ctecka().conids(), {})

    def test_zmena_souboru_se_projevi(self):
        self.zapis(stav(obchod()))
        ctecka = self.ctecka()
        self.assertIn(900001, ctecka.conids())
        self.zapis(stav(obchod(state="CANCELLED")))
        self.assertEqual(ctecka.conids(), {})

    def test_poskozeny_soubor_ponecha_posledni_stav(self):
        # Výpadek čtení nesmí ochranu tiše vypnout
        self.zapis(stav(obchod()))
        ctecka = self.ctecka()
        self.assertIn(900001, ctecka.conids())
        self.soubor.write_text("{tohle není JSON", encoding="utf-8")
        with self.assertLogs("tws_rucne.reservations", level="ERROR"):
            self.assertIn(900001, ctecka.conids())

    def test_vlastni_stav_se_vynechava(self):
        # Vlastní pozice strike neomezují, do svého kontraktu se dokupuje dál
        self.zapis(stav(obchod()))
        with self.assertLogs("tws_rucne.reservations", level="WARNING"):
            ctecka = self.ctecka(own_state=str(self.soubor))
        self.assertEqual(ctecka.conids(), {})

    def test_cizi_format_souboru_se_ohlasi(self):
        # Tiché prázdno by ochranu vypnulo a nikdo by se to nedozvěděl
        self.zapis({"polozky": [obchod()]})
        with self.assertLogs("tws_rucne.reservations", level="ERROR"):
            self.assertEqual(self.ctecka().conids(), {})

    def test_neznamy_stav_drzi_kontrakt_a_ohlasi_se(self):
        # Přibude-li v druhé aplikaci nový stav, ochrana se tím nesmí vypnout
        self.zapis(stav(obchod(state="NECO_NOVEHO")))
        with self.assertLogs("tws_rucne.reservations", level="WARNING"):
            self.assertIn(900001, self.ctecka().conids())

    def test_stejna_verze_souboru_se_hlasi_jen_jednou(self):
        # Příprava zadání se volá po každé změně formuláře; opakované hlášení
        # téhož poškozeného souboru by log zaplavilo
        self.soubor.write_text("{tohle není JSON", encoding="utf-8")
        ctecka = self.ctecka()
        with self.assertLogs("tws_rucne.reservations", level="ERROR") as zaznam:
            ctecka.conids()
            ctecka.conids()
        self.assertEqual(len(zaznam.records), 1)


if __name__ == "__main__":
    unittest.main()
