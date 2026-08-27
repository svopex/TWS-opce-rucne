"""
Společné základy testů - konfigurace a engine s náhradou TWS.

Sdílí je víc testovacích souborů, aby se stejná příprava nepsala pokaždé
znovu a případná změna platila všude naráz.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fake_ib import FakeIBService
from tws_rucne.config import AppConfig
from tws_rucne.engine import ManualEngine
from tws_rucne.models import Position


def vychozi_config() -> AppConfig:
    """Konfigurace pro testy - bez zápisu stavu na disk, runner jeden kontrakt."""
    cfg = AppConfig()
    cfg.state.enabled = False
    cfg.trading.runner_quantity = 1
    cfg.trading.ask_tolerance_pct = 0.0
    cfg.trading.bid_tolerance_pct = 0.0
    return cfg


class ZakladEnginu(unittest.IsolatedAsyncioTestCase):
    """Engine s náhradou TWS a bez zápisu stavu na disk."""

    def setUp(self) -> None:
        self.cfg = vychozi_config()
        self.ib = FakeIBService(self.cfg)
        self.engine = ManualEngine(self.cfg, self.ib)
        # Testy si stav proti TWS srovnávají samy, obnova se ve smyčce nespouští
        self.engine._synced = True

    async def tik(self) -> None:
        """Protočí jeden průchod monitorovací smyčky."""
        await self.engine._tick()

    async def nakup(
        self, symbol: str = "AAPL", quantity: int = 3, right: str = "C", kind: str = "ask"
    ) -> Position:
        """Zadá nákupní příkaz a vrátí založenou pozici."""
        return await self.engine.buy(symbol, quantity, right, kind)

    async def nakup_vyplnen(
        self,
        symbol: str = "AAPL",
        quantity: int = 3,
        right: str = "C",
        cena: float = 3.20,
        kind: str = "ask",
    ) -> Position:
        """Zadá nákup, nechá jej v TWS vyplnit a protočí monitorovací smyčku."""
        position = await self.nakup(symbol, quantity, right, kind)
        self.ib.fill(position.buy_trade, quantity, cena)
        await self.tik()
        return position

    async def prodej_vyplnen(
        self, position: Position, kind: str, scope: str, cena: float, quantity: int | None = None
    ) -> Position:
        """Zadá prodej, nechá jej vyplnit a protočí monitorovací smyčku."""
        await self.engine.sell(position.id, kind, scope)
        kusu = quantity if quantity is not None else position.sell_quantity
        self.ib.fill(position.sell_trade, kusu, cena)
        await self.tik()
        return position


class ZakladSeStavem(ZakladEnginu):
    """Engine se zapnutým ukládáním stavu do dočasného souboru."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg.state.enabled = True
        self.cfg.state.file = str(Path(self.tmp.name) / "state.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()
