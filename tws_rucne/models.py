"""Datové modely ruční opční pozice a jejího stavu."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from . import calc

# Popisky typu opce pro zobrazení v rozhraní
RIGHT_LABELS = {"C": "CALL", "P": "PUT"}

# Rozsah prodeje: celá držená pozice, nebo jen základní část bez runneru
SELL_SCOPE_ALL = "all"
SELL_SCOPE_BASE = "base"

# Přirážky k poptávané ceně nabízené prodejními tlačítky (v procentech).
# Prodej nad ASK vynese víc, ale vyplní se s menší pravděpodobností.
ASK_MARKUPS = (1.0, 2.0, 3.0, 4.0, 5.0, 7.0, 9.0)


def cislo_text(hodnota: float, desetin: int = 2) -> str:
    """Číslo pro zobrazení - tisíce oddělené mezerou, desetinná tečka."""
    return f"{hodnota:,.{desetin}f}".replace(",", " ")


class PositionState(str, Enum):
    """Stavy životního cyklu jedné ručně obchodované pozice."""

    BUYING = "BUYING"
    OPEN = "OPEN"
    SELLING = "SELLING"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"

    @property
    def label(self) -> str:
        """Český popisek stavu pro přehled pozic."""
        return {
            PositionState.BUYING: "Nakupuje se",
            PositionState.OPEN: "Otevřená pozice",
            PositionState.SELLING: "Prodává se",
            PositionState.CLOSED: "Uzavřeno",
            PositionState.CANCELLED: "Zrušeno",
            PositionState.ERROR: "Chyba",
        }[self]

    @property
    def css_class(self) -> str:
        """CSS třída pro barevné odlišení stavu v přehledu."""
        return {
            PositionState.BUYING: "stav-nakupuje",
            PositionState.OPEN: "stav-otevreno",
            PositionState.SELLING: "stav-prodava",
            PositionState.CLOSED: "stav-uzavreno",
            PositionState.CANCELLED: "stav-zruseno",
            PositionState.ERROR: "stav-chyba",
        }[self]

    @property
    def is_active(self) -> bool:
        """Aktivní pozice se monitorují ve smyčce a zůstávají nahoře v přehledu."""
        return self in (PositionState.BUYING, PositionState.OPEN, PositionState.SELLING)


@dataclass
class Position:
    """
    Jedna ručně obchodovaná opční pozice - od nákupního příkazu
    přes držení kontraktů až po úplný prodej.

    Pole s runtime objekty TWS (kontrakty, příkazy, kotace) se neukládají;
    po restartu se obnovují dotazem do TWS.
    """

    id: str = ""
    symbol: str = ""
    right: str = "C"
    # Množství zadané obchodníkem při nákupu
    quantity: int = 1
    expiration: str = ""
    strike: float = 0.0
    option_conid: int = 0
    underlying_conid: int = 0
    min_tick: float = 0.01
    # Kolik kontraktů má zůstat jako runner při prodeji základní pozice
    runner_quantity: int = 1

    state: PositionState = PositionState.BUYING
    message: str = ""

    # --- nákup ---
    # 'ask' nebo 'mid' podle stisknutého tlačítka
    buy_kind: str = "ask"
    buy_limit: float | None = None
    filled_quantity: int = 0
    fill_price: float | None = None
    fill_time: datetime | None = None

    # --- prodeje ---
    # Souhrn všech dokončených i probíhajících prodejů
    sold_quantity: int = 0
    # Suma prodejních cen krát počty kusů (cena za kontrakt, ne za pozici)
    sold_value: float = 0.0
    # Právě zadaný prodejní příkaz
    sell_kind: str = ""
    sell_scope: str = ""
    # Přirážka ke středu trhu, se kterou byl příkaz zadán (v procentech)
    sell_markup_pct: float = 0.0
    sell_limit: float | None = None
    sell_quantity: int = 0
    # Pořadové číslo prodeje - odlišuje značky příkazů v TWS po restartu
    sell_seq: int = 0
    # Kolik kusů a za jakou hodnotu už bylo z běžícího prodeje zúčtováno
    sell_settled_quantity: int = 0
    sell_settled_value: float = 0.0

    # Provize účtované TWS podle identifikátoru exekuce
    commissions: dict[str, float] = field(default_factory=dict)

    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    # --- runtime, neukládá se ---
    option_contract: Any = None
    underlying_contract: Any = None
    buy_trade: Any = None
    sell_trade: Any = None
    # Pozice drží vlastní odběr tržních dat, dokud není ukončená
    subscribed: bool = False
    underlying_price: float | None = None
    option_bid: float | None = None
    option_ask: float | None = None
    delta: float | None = None

    # ------------------------------------------------------------------
    # Popisky
    # ------------------------------------------------------------------

    @property
    def right_label(self) -> str:
        """CALL / PUT pro zobrazení."""
        return RIGHT_LABELS.get(self.right, self.right)

    @property
    def contract_label(self) -> str:
        """Popis kontraktu, například 'AAPL 20260918 CALL 230'."""
        return f"{self.symbol} {self.expiration} {self.right_label} {cislo_text(self.strike)}"

    # ------------------------------------------------------------------
    # Množství
    # ------------------------------------------------------------------

    @property
    def open_quantity(self) -> int:
        """Počet kontraktů, které pozice právě drží."""
        return max(0, self.filled_quantity - self.sold_quantity)

    @property
    def base_quantity(self) -> int:
        """
        Počet kontraktů základní pozice - tedy to, co lze prodat, aby
        v pozici zůstal runner. Je-li v držení už jen runner (nebo méně),
        vychází nula a prodej základní pozice se nenabízí.
        """
        return max(0, self.open_quantity - self.runner_quantity)

    @property
    def is_runner_only(self) -> bool:
        """Pozice je po odprodeji základní části - drží se už jen runner."""
        return self.filled_quantity > 0 and 0 < self.open_quantity <= self.runner_quantity

    def sell_quantity_for(self, scope: str) -> int:
        """Kolik kontraktů se prodá pro daný rozsah ('all' nebo 'base')."""
        if scope == SELL_SCOPE_ALL:
            return self.open_quantity
        if scope == SELL_SCOPE_BASE:
            return self.base_quantity
        raise ValueError(f"Neznámý rozsah prodeje: {scope}")

    # ------------------------------------------------------------------
    # Dostupnost tlačítek
    # ------------------------------------------------------------------

    @property
    def has_pending_order(self) -> bool:
        """V trhu je nevyřízený nákupní nebo prodejní příkaz aplikace."""
        return self.state in (PositionState.BUYING, PositionState.SELLING)

    @property
    def sell_pending(self) -> bool:
        """V trhu je nevyřízený prodejní příkaz této pozice."""
        return self.state == PositionState.SELLING

    @property
    def can_sell_all(self) -> bool:
        """
        Prodej celé držené pozice.

        Nabízí se i s prodejním příkazem v trhu - opakovaný stisk tlačítka
        pak nezakládá druhý příkaz, ale přecení ten stávající na aktuální cenu.
        """
        return (
            self.state in (PositionState.OPEN, PositionState.SELLING)
            and self.open_quantity >= 1
        )

    @property
    def can_sell_base(self) -> bool:
        """
        Prodej základní pozice (množství snížené o runner).
        Nenabízí se u pozice o velikosti runneru - typicky nákup jediného
        kontraktu - ani po odprodeji základní části, kdy už zbývá jen runner.
        """
        return (
            self.state in (PositionState.OPEN, PositionState.SELLING)
            and self.base_quantity >= 1
        )

    @property
    def can_reprice_buy(self) -> bool:
        """Nevyplněný nákupní příkaz lze přecenit na aktuální cenu."""
        return self.state == PositionState.BUYING

    @property
    def can_cancel(self) -> bool:
        """Nevyřízený příkaz lze stáhnout z trhu."""
        return self.has_pending_order

    @property
    def can_remove(self) -> bool:
        """Ukončenou pozici lze odstranit z přehledu."""
        return not self.state.is_active

    # ------------------------------------------------------------------
    # Ceny a výsledek
    # ------------------------------------------------------------------

    @property
    def mid(self) -> float | None:
        """Střed trhu opce z aktuální kotace."""
        return calc.mid_price(self.option_bid, self.option_ask)

    @property
    def spread_pct(self) -> float | None:
        """Spread opce v procentech ze středu trhu."""
        return calc.spread_pct(self.option_bid, self.option_ask)

    @property
    def commission_total(self) -> float:
        """Součet provizí, které TWS k pozici zatím naúčtovala."""
        return sum(self.commissions.values())

    @property
    def realized_pnl(self) -> float | None:
        """Realizovaný výsledek z už prodaných kontraktů (bez provizí)."""
        return calc.realized_pnl(self.fill_price, self.sold_quantity, self.sold_value)

    @property
    def unrealized_pnl(self) -> float | None:
        """
        Nerealizovaný výsledek držených kontraktů, oceněný středem trhu.
        Střed je férovější odhad než BID, skutečný prodej ale proběhne
        za cenu, kterou trh v daný okamžik nabídne.
        """
        return calc.position_pnl(self.fill_price, self.mid, self.open_quantity)

    @property
    def gross_pnl(self) -> float | None:
        """Výsledek pozice před provizemi - realizovaná i otevřená část."""
        casti = [c for c in (self.realized_pnl, self.unrealized_pnl) if c is not None]
        if not casti:
            return None
        return sum(casti)

    @property
    def net_pnl(self) -> float | None:
        """Výsledek pozice po odečtení provizí účtovaných TWS."""
        hruby = self.gross_pnl
        if hruby is None:
            return None
        return hruby - self.commission_total

    @property
    def pnl_split(self) -> bool:
        """
        Výsledek se ukazuje dvěma čísly: zvlášť držený zbytek a v závorce celek.

        Platí, jakmile je něco prodáno a něco se ještě drží - typicky po
        odprodeji základní pozice, kdy v trhu zůstal runner. Jeho vlastní
        výsledek je to, podle čeho se obchodník rozhoduje, celek za celou
        pozici ale musí zůstat na očích.
        """
        return self.sold_quantity > 0 and self.open_quantity > 0

    @property
    def cost(self) -> float | None:
        """Zaplacená prémie za nakoupené kontrakty v USD."""
        return calc.order_value(self.fill_price, self.filled_quantity)

    # ------------------------------------------------------------------
    # Stav
    # ------------------------------------------------------------------

    def set_state(self, state: PositionState, message: str = "") -> None:
        """Nastaví stav pozice a doprovodnou hlášku."""
        self.state = state
        if message:
            self.message = message
        self.updated_at = datetime.now()

    def touch(self, message: str = "") -> None:
        """Zaznamená změnu bez přechodu do jiného stavu."""
        if message:
            self.message = message
        self.updated_at = datetime.now()


def pnl_text(otevreny: float | None, celkovy: float | None, split: bool) -> str:
    """
    Výsledek pozice pro kartu v přehledu.

    Drží-li pozice zbytek po částečném prodeji (typicky runner), uvede se
    nejdřív výsledek tohoto zbytku a v závorce celek za celou pozici včetně
    už prodaných kusů a provizí - například '53.35 (153.66) USD'. Jinak
    se ukazuje jen celek.
    """
    celek = "-" if celkovy is None else cislo_text(celkovy)
    if not split:
        return "-" if celkovy is None else f"{celek} USD"

    zbytek = "-" if otevreny is None else cislo_text(otevreny)
    return f"{zbytek} ({celek}) USD"


def ukoncene_pozice_text(pocet: int) -> str:
    """
    Hláška o odstraněných pozicích se správnými tvary slov
    (1 pozice, 3 pozice, 5 pozic).
    """
    if pocet == 1:
        return "odstraněna 1 ukončená pozice"
    if 2 <= pocet <= 4:
        return f"odstraněny {pocet} ukončené pozice"
    return f"odstraněno {pocet} ukončených pozic"


def price_kind_label(kind: str, markup_pct: float = 0.0) -> str:
    """
    Popis ceny příkazu: 'ASK', 'BID', 'MID', nebo s přirážkou 'MID +1 %'.
    Přirážka se píše jen tehdy, když je nenulová.
    """
    nazev = {"ask": "ASK", "bid": "BID", "mid": "MID"}[kind]
    if not markup_pct:
        return nazev
    return f"{nazev} +{markup_pct:g} %"


def buy_button_label(kind: str, quantity: int) -> str:
    """Popisek nákupního tlačítka, například '3 ks za ASK'."""
    return f"{quantity} ks za {price_kind_label(kind)}"


def sell_button_label(kind: str, quantity: int, markup_pct: float = 0.0) -> str:
    """
    Popisek prodejního tlačítka - cena a počet kusů, které odejdou.

    Tvar je v obou řádcích stejný ('BID (3 ks)' nad 'BID (1 ks)'), takže
    tlačítka stejného druhu stojí ve sloupcích nad sebou. Který řádek prodává
    celou pozici a který nechává runner, říká barva a bublina s nápovědou.
    U přirážek nad poptávkou se počet nepíše - popisek by byl zbytečně
    dlouhý a v řádku je jasný z tlačítek vedle.
    """
    cena = price_kind_label(kind, markup_pct)
    if markup_pct:
        return cena
    return f"{cena} ({quantity} ks)"
