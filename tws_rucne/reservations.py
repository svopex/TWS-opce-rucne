"""
Kontrakty zamluvené obchody jiné aplikace.

Sousední aplikace TWS-opce zakládá obchody, které na kontrakt čekají, aniž
by o nich TWS věděla - typicky obchod blokovaný širokým spreadem. Vyhýbání
se obsazeným kontraktům se dívá do TWS, takže takový obchod tam nenajde
a obě aplikace by zamířily na týž strike; jakmile spread povolí, sečetly by
se jejich nákupy v TWS do jediné pozice.

Jediné, co o čekajícím obchodu vypovídá, je uložený stav druhé aplikace.
Čte se pouze pro čtení a nezávisle na tom, jestli druhá aplikace běží.
Selhání čtení ochranu nevypne - drží se poslední přečtený obsah, stejně
jako u cizích příkazů v ib_service.foreign_order_conids.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .models import contract_label

log = logging.getLogger(__name__)

# Stavy obchodu, po kterých už kontrakt nikdo nedrží. Je to doplněk
# vlastnosti FlowState.is_active v tws_opce/models.py druhé aplikace;
# cizí enum se odsud importovat nedá, proto ta kopie. Cokoliv jiného -
# včetně stavu, který tato aplikace nezná - se počítá za rezervaci, aby
# nově přidaný stav v sousední aplikaci ochranu tiše nevypnul.
RELEASED_STATES = frozenset({"CLOSED", "CANCELLED", "MISSED", "ERROR"})


class ReservedContracts:
    """
    Čte kontrakty zamluvené obchody jiných aplikací z jejich uloženého stavu.

    Parametr paths jsou cesty ke stavovým souborům (state.json) jiných
    aplikací, own_state je vlastní stavový soubor - ten se ze seznamu
    vyřadí, protože podle vlastních pozic se strike neomezuje.

    Obsah každého souboru se drží v paměti a znovu se čte teprve při jeho
    změně na disku: příprava zadání se volá po každé změně formuláře
    a opakované parsování téhož souboru nemá smysl.
    """

    def __init__(self, paths: Sequence[str], own_state: str | None = None) -> None:
        vlastni = Path(own_state).resolve() if own_state else None
        self.paths: list[Path] = []
        for zadana in paths:
            cesta = Path(zadana)
            # Vlastní stav mezi cizími rezervacemi by aplikaci zablokoval
            # její vlastní kontrakty, do kterých musí jít dokupovat dál
            if vlastni is not None and cesta.resolve() == vlastni:
                log.warning(
                    "Soubor %s je vlastní stav aplikace - z rezervací se vynechává.", zadana
                )
                continue
            self.paths.append(cesta)

        # Otisk naposledy zpracovaného souboru (čas změny a velikost) a k němu
        # rezervace z posledního úspěšného čtení, obojí podle cesty
        self._stamps: dict[Path, tuple[int, int]] = {}
        self._reserved: dict[Path, dict[int, str]] = {}

    def conids(self) -> dict[int, str]:
        """
        Zamluvené opční kontrakty: conId -> popis pro hlášení obchodníkovi.

        Sloučí rezervace ze všech nastavených souborů. Prázdný výsledek
        znamená, že se žádný soubor nečte, nebo že v nich žádný živý obchod
        není - výběr strike pak nic neomezuje.
        """
        nalezene: dict[int, str] = {}
        for cesta in self.paths:
            for conid, popis in self._read(cesta).items():
                nalezene.setdefault(conid, popis)
        return nalezene

    def _read(self, cesta: Path) -> dict[int, str]:
        """
        Přečte rezervace z jednoho souboru, nezměněný soubor vezme z paměti.

        Chybějící soubor není chyba: druhá aplikace nemusí běžet ani nemusela
        zatím nic uložit. Po nečitelném, poškozeném nebo cizím souboru se
        vrátí naposledy přečtený obsah - výpadek čtení nesmí ochranu tiše
        vypnout - a hlásí se jednou na každou verzi souboru.
        """
        posledni = self._reserved.get(cesta, {})
        try:
            stat = cesta.stat()
            otisk = (stat.st_mtime_ns, stat.st_size)
            if self._stamps.get(cesta) == otisk:
                return posledni
            # Otisk se zapíše ještě před čtením, aby se selhání na téže verzi
            # souboru ohlásilo jednou, a ne při každé přípravě zadání
            self._stamps[cesta] = otisk
            rezervace = _parse(json.loads(cesta.read_text(encoding="utf-8")), cesta)
        except FileNotFoundError:
            return {}
        except Exception:
            log.exception("Stav jiné aplikace %s se nepodařilo přečíst.", cesta)
            return posledni

        self._reserved[cesta] = rezervace
        return rezervace


def _parse(data: Any, cesta: Path) -> dict[int, str]:
    """
    Vytáhne z načteného stavu conId kontraktů, které si drží živé obchody.

    Očekává formát sousední aplikace: {"flows": [{...}]}. Záznam bez conId
    a záznam v ukončeném stavu se přeskakuje, ostatní se berou za zamluvené.
    Nerozpoznaný tvar souboru vyhazuje ValueError: tiché prázdno by ochranu
    vypnulo a obchodník by se to nedozvěděl.
    """
    zaznamy = data.get("flows") if isinstance(data, dict) else None
    if not isinstance(zaznamy, list):
        raise ValueError(f"Soubor {cesta} nemá očekávaný tvar stavu se seznamem 'flows'.")

    nalezene: dict[int, str] = {}
    for zaznam in zaznamy:
        if not isinstance(zaznam, dict):
            continue
        conid = zaznam.get("option_conid")
        if not isinstance(conid, int) or conid <= 0:
            continue
        stav = str(zaznam.get("state", "")).upper()
        if stav in RELEASED_STATES:
            continue
        # Neznámý stav kontrakt drží, ale je to signál, že se druhá aplikace
        # posunula a seznam ukončených stavů si zaslouží projít
        if stav not in ZNAME_STAVY:
            log.warning("Obchod ve stavu '%s' v %s aplikace nezná - kontrakt drží.", stav, cesta)
        nalezene.setdefault(conid, _label(zaznam))
    return nalezene


# Stavy živého obchodu, které druhá aplikace používala v době psaní - slouží
# jen k rozpoznání, že jí přibyl nový (viz hlášení v _parse)
ZNAME_STAVY = frozenset(
    {"NEW", "ARMED", "SPREAD_BLOCKED", "NO_QUOTES", "FILLED", "EXIT_ARMED", "CLOSING"}
)


def _label(zaznam: dict[str, Any]) -> str:
    """
    Popis zamluveného kontraktu do hlášky pro obchodníka.

    Kontrakt se skládá stejně jako v kartách pozic, k němu se připojuje
    identifikátor obchodu, aby šlo kolizi dohledat v druhé aplikaci.
    Chybí-li v záznamu údaje o kontraktu, zbude samotný identifikátor.
    """
    strike = zaznam.get("strike")
    kontrakt = (
        contract_label(
            str(zaznam.get("symbol") or ""),
            str(zaznam.get("expiration") or ""),
            str(zaznam.get("right") or ""),
            float(strike),
        )
        if isinstance(strike, (int, float))
        else ""
    )
    obchod = str(zaznam.get("id") or "").strip()
    casti = [kontrakt, f"obchod {obchod} jiné aplikace" if obchod else ""]
    return " - ".join(c for c in casti if c) or "obchod jiné aplikace"
