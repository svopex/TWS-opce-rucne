"""
Počítadlo Order Efficiency Ratio (OER) pro hlavičku aplikace.

Interactive Brokers hodnotí každý obchodní den poměr

    OER = (odeslané příkazy + úpravy + zrušení) / (vyplněné příkazy + 1)

a očekává hodnotu nejvýš kolem 20. Třída počítá zprávy, které tato aplikace
do TWS odeslala, a její příkazy, které se (i jen zčásti) vyplnily. Údaj je
jen informativní - aplikace podle něj nic neomezuje. Den se určuje v časové
zóně burzy; s novým dnem se počítadlo zpráv nuluje.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date, datetime
from functools import partial
from typing import Any
from zoneinfo import ZoneInfo


def efficiency_ratio(messages: int, executed: int) -> float:
    """OER podle vzorce IBKR; jednička ve jmenovateli brání dělení nulou."""
    return messages / (executed + 1)


class OrderEfficiency:
    """
    Denní počítadlo zpráv a vyplněných příkazů pro výpočet OER.

    timezone - časová zóna burzy, ve které se určuje obchodní den
    fills    - zdroj vyplnění příkazů aplikace (objekty Fill z ib_async);
               ib_async si po připojení vyžádá exekuce celého dne, takže
               vyplněné příkazy není potřeba počítat ani ukládat zvlášť
    now      - zdroj aktuálního času (testy si jím podvrhují den)
    """

    def __init__(
        self,
        timezone: ZoneInfo,
        fills: Callable[[], Iterable[Any]],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.timezone = timezone
        self._fills = fills
        self._now = now or partial(datetime.now, timezone)
        self._day: date = self._today()
        self._messages: int = 0

    def _today(self) -> date:
        """Dnešní obchodní den v časové zóně burzy."""
        return self._now().astimezone(self.timezone).date()

    def _roll(self) -> None:
        """S novým obchodním dnem vynuluje počítadlo zpráv - IBKR hodnotí každý den zvlášť."""
        dnes = self._today()
        if dnes != self._day:
            self._day = dnes
            self._messages = 0

    @property
    def messages(self) -> int:
        """Počet dnes odeslaných zpráv (nové příkazy, úpravy, zrušení)."""
        self._roll()
        return self._messages

    @property
    def executed(self) -> int:
        """
        Počet dnes vyplněných příkazů. Částečně vyplněný příkaz má víc
        exekucí, ale do OER se počítá jednou; exekuce z jiného dne se
        přeskakují. Čas bez časové zóny se bere jako místní čas počítače.
        """
        self._roll()
        return len(
            {
                self._execution_key(fill.execution)
                for fill in self._fills()
                if fill.time.astimezone(self.timezone).date() == self._day
            }
        )

    def record_message(self) -> None:
        """Započte odeslanou zprávu - nový příkaz, jeho úpravu nebo zrušení."""
        self._roll()
        self._messages += 1

    @staticmethod
    def _execution_key(execution: Any) -> str:
        """
        Klíč vyplněného příkazu. permId je v TWS trvalý napříč spojeními,
        orderId stačí jako náhrada; bez obou se exekuce počítá samostatně.
        """
        if execution.permId:
            return f"perm:{execution.permId}"
        if execution.orderId:
            return f"order:{execution.orderId}"
        return f"exec:{execution.execId}"

    def to_dict(self) -> dict[str, Any]:
        """Počítadlo zpráv k uložení na disk - přežije tak restart aplikace."""
        self._roll()
        return {"day": self._day.isoformat(), "messages": self._messages}

    def load(self, data: dict[str, Any] | None) -> None:
        """
        Obnoví počítadlo zpráv uložené dříve téhož dne. Záznam z jiného dne
        (nebo poškozený) se zahodí - IBKR počítá každý den znovu.
        """
        self._roll()
        if not isinstance(data, dict) or data.get("day") != self._day.isoformat():
            return
        zpravy = data.get("messages")
        # Počítadlo se slučuje s tím, co se napočítalo před obnovou - zprávy
        # odeslané po připojení mohou předběhnout načtení uloženého stavu
        if isinstance(zpravy, int) and zpravy > 0:
            self._messages += zpravy
