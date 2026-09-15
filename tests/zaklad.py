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


def cizi_obchod(
    conid: int = 900001,
    state: str = "SPREAD_BLOCKED",
    strike: float = 232.5,
    **dalsi,
) -> dict:
    """
    Jeden záznam obchodu ve formátu stavu sousední aplikace.
    Výchozí je obchod čekající na zúžení spreadu, tedy ten, o kterém TWS neví.
    """
    zaznam = {
        "id": "AAPL-39",
        "symbol": "AAPL",
        "expiration": "20260916",
        "right": "C",
        "strike": strike,
        "option_conid": conid,
        "state": state,
    }
    zaznam.update(dalsi)
    return zaznam


def cizi_stav(*obchody: dict) -> dict:
    """Obsah stavového souboru sousední aplikace v jejím formátu."""
    return {"version": 1, "saved_at": "2026-09-15T07:34:16", "flows": list(obchody)}


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
        self.prestav_engine()

    def prestav_engine(self) -> None:
        """
        Založí engine nad stávající konfigurací a náhradou TWS.

        Volá se znovu v testech, které konfiguraci mění až po setUp - engine
        si část nastavení přebírá už v konstruktoru.
        """
        self.engine = ManualEngine(self.cfg, self.ib)
        # Testy si stav proti TWS srovnávají samy, obnova se ve smyčce nespouští
        self.engine._synced = True

    def nastav_rastr_striku(self) -> None:
        """
        Řetězec po 2,5 bodu s rozlišitelnými kontrakty.

        Při ceně podkladu 230 padne první strike mimo peníze u CALL na 232,5
        a každý ústupek obsazenému kontraktu je o 2,5 bodu dál.
        """
        self.ib.price_underlying = 230.0
        self.ib.option_conids = {
            232.5: 800001,
            235.0: 800002,
            237.5: 800003,
            240.0: 800004,
            242.5: 800005,
        }

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
