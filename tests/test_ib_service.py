"""
Testy ukazatele kvality spojení - odezva TWS a stáří tržních dat.

Ticker se plní ručně a spojení s TWS není potřeba; ověřuje se, že aplikace
používá pole a metody ib_async správně a že hlavička hodnoty vypisuje tak,
jak se od ní čekají.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_async import Stock, Ticker

from tws_rucne.config import AppConfig
from tws_rucne.ib_service import IBService
from tws_rucne.ui import linka_varuje, stari_text, stav_linky_text


def vloz_ticker(sluzba: IBService, conid: int = 265598, **hodnoty) -> Stock:
    """
    Vloží do služby kontrakt s předvyplněnými tržními daty.
    Hodnoty se nastavují až po vytvoření Tickeru - ib_async je v __post_init__
    přepisuje na nevyplněné, takže konstruktorem je předat nelze.

    conid odlišuje kontrakty, když test potřebuje víc odběrů naráz.
    """
    kontrakt = Stock("AAPL", "SMART", "USD")
    kontrakt.conId = conid
    ticker = Ticker(contract=kontrakt)
    for nazev, hodnota in hodnoty.items():
        setattr(ticker, nazev, hodnota)
    sluzba._tickers[kontrakt.conId] = ticker
    return kontrakt


class TestStariKotaci(unittest.TestCase):
    """Stáří tržních dat pro ukazatel kvality spojení v hlavičce."""

    def setUp(self) -> None:
        self.sluzba = IBService(AppConfig())

    def test_bez_odberu_neni_co_merit(self):
        self.assertIsNone(self.sluzba.quotes_age())

    def test_ticker_bez_casu_se_preskoci(self):
        vloz_ticker(self.sluzba, bid=3.00)
        self.assertIsNone(self.sluzba.quotes_age())

    def test_rozhoduje_nejnovejsi_kotace(self):
        # Nelikvidní opce se aktualizuje zřídka i při zcela zdravém spojení,
        # proto rozhoduje nejčerstvější čas, ne nejstarší
        ted = datetime.now()
        vloz_ticker(self.sluzba, conid=1, time=ted - timedelta(seconds=90))
        vloz_ticker(self.sluzba, conid=2, time=ted - timedelta(seconds=2))
        self.assertLess(self.sluzba.quotes_age(), 10)

    def test_cas_s_casovou_zonou(self):
        # ib_async dodává časy kotací v UTC s časovou zónou
        vloz_ticker(self.sluzba, time=datetime.now(timezone.utc) - timedelta(seconds=3))
        stari = self.sluzba.quotes_age()
        self.assertGreaterEqual(stari, 2.0)
        self.assertLess(stari, 10)

    def test_hodiny_tws_napred_davaji_nulu(self):
        # Záporné stáří by v hlavičce mátlo víc než nula
        vloz_ticker(self.sluzba, time=datetime.now() + timedelta(seconds=30))
        self.assertEqual(self.sluzba.quotes_age(), 0.0)


class TestOdezvyTws(unittest.IsolatedAsyncioTestCase):
    """Měření odezvy TWS dotazem na aktuální čas."""

    def setUp(self) -> None:
        self.sluzba = IBService(AppConfig())

    def predstirej_spojeni(self, odpoved) -> None:
        """Podvrhne spojení i dotaz na čas, aby test nepotřeboval TWS."""
        self.sluzba.ib.isConnected = lambda: True
        self.sluzba.ib.reqCurrentTimeAsync = odpoved

    async def test_bez_spojeni_se_nemeri(self):
        # Stará hodnota se musí zahodit, ať v hlavičce nestraší
        self.sluzba.rtt_ms = 12.0
        self.assertIsNone(await self.sluzba.measure_rtt())
        self.assertIsNone(self.sluzba.rtt_ms)

    async def test_zmerena_odezva_se_zapamatuje(self):
        async def odpoved():
            await asyncio.sleep(0.05)
            return datetime.now()

        self.predstirej_spojeni(odpoved)
        rtt = await self.sluzba.measure_rtt()
        self.assertGreaterEqual(rtt, 40.0)
        self.assertEqual(self.sluzba.rtt_ms, rtt)

    async def test_chyba_dotazu_vraci_none(self):
        # Nedostupná odpověď je údaj o spojení, ne chyba k vyhození
        async def selze():
            raise ConnectionError("spojení spadlo")

        self.predstirej_spojeni(selze)
        self.sluzba.rtt_ms = 12.0
        self.assertIsNone(await self.sluzba.measure_rtt())
        self.assertIsNone(self.sluzba.rtt_ms)


class TestPopisuLinky(unittest.TestCase):
    """Text ukazatele kvality spojení v hlavičce."""

    def test_odezva_i_stari(self):
        self.assertEqual(stav_linky_text(0.84, 0.42), "TWS 0,8 ms · data 0,4 s")

    def test_nezmerena_odezva_je_pomlcka(self):
        self.assertEqual(stav_linky_text(None, 1.0), "TWS - · data 1,0 s")

    def test_bez_odberu_je_pomlcka_u_dat(self):
        self.assertEqual(stav_linky_text(3.0, None), "TWS 3,0 ms · data -")

    def test_stari_se_zaokrouhluje_podle_velikosti(self):
        # Do deseti sekund je vidět desetina, výš už jen celé sekundy
        self.assertEqual(stari_text(0.42), "0,4 s")
        self.assertEqual(stari_text(9.94), "9,9 s")
        self.assertEqual(stari_text(42.4), "42 s")
        self.assertEqual(stari_text(185.0), "3 min")
        self.assertEqual(stari_text(None), "-")


class TestVarovaniLinky(unittest.TestCase):
    """Kdy se ukazatel kvality spojení zvýrazní."""

    def test_zdrava_linka_nevaruje(self):
        self.assertFalse(linka_varuje(1.0, 0.5, trh_otevren=True))

    def test_pomala_odezva_varuje_i_po_zavreni_trhu(self):
        # Nestíhající TWS je problém bez ohledu na denní dobu
        self.assertTrue(linka_varuje(900.0, 0.5, trh_otevren=False))

    def test_stojici_kotace_varuji_jen_behem_seance(self):
        self.assertTrue(linka_varuje(1.0, 120.0, trh_otevren=True))
        # Mimo obchodní hodiny trh nic neposílá - varování by svítilo pořád
        self.assertFalse(linka_varuje(1.0, 120.0, trh_otevren=False))

    def test_chybejici_hodnoty_nevaruji(self):
        # Nezměřená odezva ani žádný odběr nejsou známkou potíží
        self.assertFalse(linka_varuje(None, None, trh_otevren=True))


if __name__ == "__main__":
    unittest.main()
