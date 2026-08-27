"""
Ukládání a načítání otevřených pozic na disk.

Stav se zapisuje po každé změně, aby po restartu nebo pádu aplikace
nezůstala pozice bez přehledu. Uložený soubor je jen vodítko - skutečný
stav příkazů a kontraktů se vždy ověřuje proti TWS.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import Position, PositionState

log = logging.getLogger(__name__)

# Verze formátu souboru - při nekompatibilní změně se uložený stav ignoruje
FORMAT_VERSION = 1

# Pole pozice, která se ukládají. Tržní data (kotace, delta) a runtime
# objekty TWS se neukládají, po startu se načtou znovu.
SAVED_FIELDS = (
    "id",
    "symbol",
    "right",
    "quantity",
    "expiration",
    "strike",
    "option_conid",
    "underlying_conid",
    "min_tick",
    "runner_quantity",
    "message",
    "buy_kind",
    "buy_limit",
    "filled_quantity",
    "fill_price",
    "sold_quantity",
    "sold_value",
    "sell_kind",
    "sell_scope",
    "sell_markup_pct",
    "sell_limit",
    "sell_quantity",
    "sell_seq",
    "sell_settled_quantity",
    "sell_settled_value",
    # Provize účtované TWS - po restartu je TWS pošle jen za dnešní den,
    # u starších pozic by se tedy bez uložení ztratily
    "buy_commissions",
    "sell_commissions",
)

# Pole s časovým údajem se ukládají v textovém tvaru ISO
TIME_FIELDS = ("created_at", "updated_at", "fill_time")


def position_to_dict(position: Position) -> dict[str, Any]:
    """Převede pozici na slovník vhodný k uložení."""
    data: dict[str, Any] = {name: getattr(position, name) for name in SAVED_FIELDS}
    data["state"] = position.state.value
    for name in TIME_FIELDS:
        hodnota = getattr(position, name)
        data[name] = hodnota.isoformat() if isinstance(hodnota, datetime) else None
    return data


def dict_to_position(data: dict[str, Any]) -> Position:
    """Sestaví pozici z uloženého slovníku."""
    kwargs = {name: data.get(name) for name in SAVED_FIELDS if data.get(name) is not None}
    position = Position(**kwargs)

    # Starší zápis vedl provize v jediném slovníku bez rozlišení druhu.
    # Berou se jako nákupní - u uzavřené pozice na tom nezáleží (otevřená
    # část je nulová), u běžící se rozdělení srovná první novou provizí.
    for exec_id, castka in (data.get("commissions") or {}).items():
        position.buy_commissions.setdefault(exec_id, castka)

    # Stav se obnoví z uloženého zápisu, neznámý stav se považuje za chybový
    try:
        position.state = PositionState(data.get("state", ""))
    except ValueError:
        position.state = PositionState.ERROR
        position.message = "Uložený stav pozice nebylo možné rozpoznat."

    for name in TIME_FIELDS:
        hodnota = data.get(name)
        if hodnota:
            try:
                setattr(position, name, datetime.fromisoformat(hodnota))
            except ValueError:
                pass

    return position


def save(positions: list[Position], path: str | Path) -> None:
    """
    Uloží stav pozic do souboru.
    Zápis probíhá přes dočasný soubor a přejmenování, aby při pádu aplikace
    nezůstal soubor rozepsaný.
    """
    cesta = Path(path)
    obsah = {
        "version": FORMAT_VERSION,
        "saved_at": datetime.now().isoformat(),
        "positions": [position_to_dict(p) for p in positions],
    }

    try:
        cesta.parent.mkdir(parents=True, exist_ok=True)
        # Dočasný soubor musí ležet ve stejném adresáři, aby šlo přejmenovat atomicky
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=cesta.parent, prefix=cesta.name, suffix=".tmp", delete=False
        ) as fh:
            json.dump(obsah, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
            docasny = fh.name
        os.replace(docasny, cesta)
    except Exception:
        log.exception("Stav pozic se nepodařilo uložit do %s.", cesta)


def load(path: str | Path) -> list[Position]:
    """
    Načte uložený stav pozic.
    Chybějící, poškozený nebo neznámou verzí zapsaný soubor vrací prázdný seznam.
    """
    cesta = Path(path)
    if not cesta.exists():
        return []

    try:
        with cesta.open("r", encoding="utf-8") as fh:
            obsah = json.load(fh)
    except Exception:
        log.exception("Uložený stav v %s se nepodařilo načíst.", cesta)
        return []

    if obsah.get("version") != FORMAT_VERSION:
        log.warning("Uložený stav v %s má neznámou verzi formátu - ignoruji jej.", cesta)
        return []

    pozice: list[Position] = []
    for zaznam in obsah.get("positions", []):
        try:
            pozice.append(dict_to_position(zaznam))
        except Exception:
            log.exception("Pozici se nepodařilo obnovit: %s", zaznam)
    return pozice
