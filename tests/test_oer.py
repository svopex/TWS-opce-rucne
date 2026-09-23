"""
Testy počítadla Order Efficiency Ratio.

Ověřuje se výpočet poměru podle vzorce IBKR, počítání zpráv při odeslání,
úpravě i zrušení příkazu, nulování s novým obchodním dnem a uložení
počítadla na disk. Spojení s TWS není potřeba - vyplnění se sestavují ručně
a příkazy jdou přes náhradu TWS.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_async import CommissionReport, Contract, Execution, Fill

from tests.zaklad import ZakladEnginu
from tws_rucne import store
from tws_rucne.oer import OrderEfficiency, efficiency_ratio, exceeds_limits
from tws_rucne.ui import oer_popis, oer_text

NEW_YORK = ZoneInfo("America/New_York")


def vyplneni(cas: datetime, perm_id: int, ref: str = "TWSRUCNE:AAPL-1:buy") -> Fill:
    """Jedno vyplnění příkazu s daným permId, jak je hlásí ib_async."""
    return Fill(
        contract=Contract(),
        execution=Execution(permId=perm_id, orderRef=ref),
        commissionReport=CommissionReport(),
        time=cas,
    )


class Hodiny:
    """Nastavitelný čas pro počítadlo - test jím přetáčí obchodní den."""

    def __init__(self, cas: datetime) -> None:
        self.cas = cas

    def __call__(self) -> datetime:
        return self.cas


class TestVypoctuOer(unittest.TestCase):
    """Počty zpráv a vyplněných příkazů za obchodní den."""

    def setUp(self) -> None:
        self.hodiny = Hodiny(datetime(2026, 9, 21, 11, 0, tzinfo=NEW_YORK))
        self.fills: list[Fill] = []
        self.oer = OrderEfficiency(NEW_YORK, fills=lambda: self.fills, now=self.hodiny)

    def test_vzorec_ibkr(self):
        # Bez vyplnění se dělí jedničkou
        self.assertEqual(efficiency_ratio(4, 0), 4.0)
        self.assertEqual(efficiency_ratio(6, 2), 2.0)

    def test_castecne_vyplneny_prikaz_se_pocita_jednou(self):
        # Dvě exekuce téhož příkazu (stejné permId) a jedna jiného příkazu
        self.fills += [
            vyplneni(self.hodiny.cas, perm_id=11),
            vyplneni(self.hodiny.cas, perm_id=11),
            vyplneni(self.hodiny.cas, perm_id=12),
        ]
        self.assertEqual(self.oer.executed, 2)

    def test_vyplneni_z_jineho_dne_se_nepocita(self):
        self.fills.append(vyplneni(self.hodiny.cas - timedelta(days=1), perm_id=11))
        self.assertEqual(self.oer.executed, 0)

    def test_den_se_urcuje_v_casove_zone_burzy(self):
        # 2:00 UTC dalšího dne je v New Yorku ještě 21. září večer
        pozde = datetime(2026, 9, 22, 2, 0, tzinfo=ZoneInfo("UTC"))
        self.fills.append(vyplneni(pozde, perm_id=11))
        self.assertEqual(self.oer.executed, 1)

    def test_novy_den_vynuluje_zpravy(self):
        self.oer.record_message()
        self.hodiny.cas += timedelta(days=1)
        self.assertEqual(self.oer.messages, 0)


class TestUlozeniOer(unittest.TestCase):
    """Počítadlo zpráv přežije restart téhož dne, jiný den se zahodí."""

    def setUp(self) -> None:
        self.hodiny = Hodiny(datetime(2026, 9, 21, 11, 0, tzinfo=NEW_YORK))
        self.oer = OrderEfficiency(NEW_YORK, fills=list, now=self.hodiny)

    def test_obnova_tehoz_dne_se_pricte(self):
        # Zpráva odeslaná po připojení ještě před načtením stavu se neztratí
        self.oer.record_message()
        self.oer.load({"day": "2026-09-21", "messages": 10})
        self.assertEqual(self.oer.messages, 11)

    def test_zaznam_z_jineho_dne_se_zahodi(self):
        self.oer.load({"day": "2026-09-18", "messages": 10})
        self.assertEqual(self.oer.messages, 0)

    def test_poskozeny_zaznam_se_zahodi(self):
        for zaznam in (None, [], {"day": "vcera"}, {"day": "2026-09-21", "messages": "x"}):
            self.oer.load(zaznam)
        self.assertEqual(self.oer.messages, 0)

    def test_ulozeni_a_nacteni_ze_souboru(self):
        for _ in range(3):
            self.oer.record_message()
        with tempfile.TemporaryDirectory() as adresar:
            cesta = Path(adresar) / "state.json"
            store.save([], cesta, order_stats=self.oer.to_dict())
            _, statistiky = store.load_state(cesta)
        novy = OrderEfficiency(NEW_YORK, fills=list, now=self.hodiny)
        novy.load(statistiky)
        self.assertEqual(novy.messages, 3)

    def test_chybejici_soubor_nevrati_pocitadlo(self):
        with tempfile.TemporaryDirectory() as adresar:
            self.assertEqual(store.load_state(Path(adresar) / "state.json"), ([], None))


class TestZpravEnginu(ZakladEnginu):
    """Příkazy z enginu projdou ostrou službou, která je započte do OER."""

    async def test_nakup_preceneni_a_zruseni_jsou_tri_zpravy(self):
        position = await self.nakup()
        # Přecenění je nové odeslání téhož příkazu
        await self.engine.reprice_buy(position.id, "mid")
        await self.engine.cancel_order(position.id)
        self.assertEqual(self.ib.oer.messages, 3)

    async def test_vyplneny_prikaz_se_nerusi_ani_nepocita(self):
        position = await self.nakup()
        self.ib.fill(position.buy_trade, 3, 3.20)
        self.ib.cancel(position.buy_trade)
        self.assertEqual(self.ib.oer.messages, 1)

    async def test_pocitaji_se_jen_vyplneni_aplikace(self):
        position = await self.nakup()
        # Dvě části téhož příkazu aplikace a jedno cizí vyplnění
        self.ib.fill(position.buy_trade, 1, 3.20, status="PartiallyFilled", commission=0.65)
        self.ib.fill(position.buy_trade, 3, 3.20, commission=0.65)
        self.ib.fills.append(vyplneni(datetime.now(NEW_YORK), perm_id=99, ref=""))
        self.assertEqual(self.ib.oer.executed, 1)


class TestPopisuOer(unittest.TestCase):
    """Hlavička vypisuje poměr s desetinnou čárkou, počet zpráv a tooltip s počty."""

    def test_pomer_a_pocet_zprav(self):
        # 31 zpráv na 8 vyplněných příkazů dá poměr 31 / 9 = 3,44
        self.assertEqual(oer_text(31, 8), "OER 3,4 · zprávy 31")

    def test_tooltip_uvadi_pocty(self):
        popis = oer_popis(7, 1)
        self.assertIn("zprávy 7 / (vyplněné příkazy 1 + 1)", popis)


class TestVarovaniOer(unittest.TestCase):
    """OER se zvýrazní jen při velkém objemu zpráv a zároveň vysokém poměru."""

    def test_maly_objem_nevaruje(self):
        # Poměr 400 je vysoký, ale 400 zpráv je pod hranicí objemu
        self.assertFalse(exceeds_limits(400, 0))

    def test_velky_objem_s_nizkym_pomerem_nevaruje(self):
        # 2000 zpráv na 100 vyplněných příkazů dá poměr 19,8
        self.assertFalse(exceeds_limits(2000, 100))

    def test_velky_objem_s_vysokym_pomerem_varuje(self):
        self.assertTrue(exceeds_limits(2000, 50))


if __name__ == "__main__":
    unittest.main()
