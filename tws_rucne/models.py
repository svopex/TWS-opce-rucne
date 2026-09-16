"""Datové modely ruční opční pozice a jejího stavu."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from . import calc

# Popisky typu opce pro zobrazení v rozhraní
RIGHT_LABELS = {"C": "CALL", "P": "PUT"}

# Rozsah prodeje: celá držená pozice, základní část bez runneru,
# nebo jediný kontrakt - pro postupné odprodávání po kusech
SELL_SCOPE_ALL = "all"
SELL_SCOPE_BASE = "base"
SELL_SCOPE_ONE = "one"

# Hranice v USD, pod kterou se výsledek prodeje hlásí jako vyrovnaný.
# Zisk se na tlačítkách i v zápisu o příkazu zaokrouhluje na celé dolary,
# takže menší částka by stejně vyšla jako nula - jen by ji znaménko vydávalo
# za zisk nebo ztrátu.
VYROVNANY_VYSLEDEK_USD = 0.5


def contract_label(symbol: str, expiration: str, right: str, strike: float) -> str:
    """
    Popis opčního kontraktu pro obchodníka, například 'AAPL 20260918 CALL 230.00'.

    Jediné místo, kde se popis skládá - čtou ho karty pozic, náhled zadání
    i hlášení o kontraktu zamluveném jinou aplikací, takže všude vypadá stejně.
    """
    return f"{symbol} {expiration} {RIGHT_LABELS.get(right, right)} {cislo_text(strike)}"


def cislo_text(hodnota: float, desetin: int = 2, znamenko: bool = False) -> str:
    """
    Číslo pro zobrazení - tisíce oddělené mezerou, desetinná tečka.

    Se znaménkem se vypisují výsledky obchodů, aby byl zisk i ztráta patrné
    na první pohled; ceny se píšou bez něj.
    """
    predpis = f"{{:{'+' if znamenko else ''},.{desetin}f}}"
    return predpis.format(hodnota).replace(",", " ")


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
    # Pořadové číslo nákupního příkazu - roste s každým přepsáním pozice
    # na jiný strike a odlišuje značky příkazů v TWS po restartu
    buy_seq: int = 1
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
    # Přirážka, se kterou byl příkaz zadán (v procentech, jen u druhu 'markup')
    sell_markup_pct: float = 0.0
    sell_limit: float | None = None
    sell_quantity: int = 0
    # Pořadové číslo prodeje - odlišuje značky příkazů v TWS po restartu
    sell_seq: int = 0
    # Kolik kusů a za jakou hodnotu už bylo z běžícího prodeje zúčtováno
    sell_settled_quantity: int = 0
    sell_settled_value: float = 0.0

    # Provize účtované TWS podle identifikátoru exekuce, zvlášť za nákup
    # a za prodeje - přehled výsledků je rozděluje mezi uzavřenou a otevřenou
    # část pozice a bez tohoto rozlišení by to nešlo
    buy_commissions: dict[str, float] = field(default_factory=dict)
    sell_commissions: dict[str, float] = field(default_factory=dict)

    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    # --- runtime, neukládá se ---
    option_contract: Any = None
    underlying_contract: Any = None
    buy_trade: Any = None
    sell_trade: Any = None
    # Pozice drží vlastní odběr tržních dat, dokud není ukončená
    subscribed: bool = False
    # Právě probíhá přepis na jiný kontrakt: příkaz je zrušený a hned bude
    # zadaný nový, takže se zrušení nesmí vyhodnotit jako konec pozice.
    # Do uloženého stavu nepatří - po restartu žádný přepis neběží
    transferring: bool = False
    # Od kdy se u dokončeného prodeje čeká na skutečnou cenu z TWS
    # (monotónní čas); None znamená, že se zatím nečeká
    settle_wait_since: float | None = None
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
        return contract_label(self.symbol, self.expiration, self.right, self.strike)

    def apply_preview(self, preview: Any) -> None:
        """
        Převezme z náhledu kontrakty a jejich údaje.

        Používá se při založení pozice i při přepisu na jiný strike, aby se
        obojí plnilo stejně a nové pole náhledu se nemuselo doplňovat dvakrát.
        Identity pozice (id, ticker, směr) se to netýká - ta se nemění.
        """
        self.expiration = preview.expiration
        self.strike = preview.strike or 0.0
        self.option_conid = preview.option.conId
        self.option_contract = preview.option
        self.underlying_conid = preview.underlying.conId
        self.underlying_contract = preview.underlying
        self.min_tick = preview.min_tick
        self.option_bid = preview.option_bid
        self.option_ask = preview.option_ask
        self.delta = preview.delta
        self.underlying_price = preview.current_price

    @property
    def sell_ref_kind(self) -> str:
        """Druh značky prodejního příkazu v TWS - 'sell1', 'sell2'..."""
        return f"sell{self.sell_seq}"

    @property
    def buy_ref_kind(self) -> str:
        """
        Druh značky nákupního příkazu v TWS - 'buy', 'buy2', 'buy3'...

        Přepis pozice na jiný strike zadává pod toutéž pozicí nový příkaz.
        Kdyby nesl stejnou značku jako ten zrušený, obnova po restartu by
        pozici spárovala se zrušeným příkazem. První nákup zůstává 'buy',
        aby dřívější uložené stavy i příkazy v TWS seděly dál.
        """
        return "buy" if self.buy_seq <= 1 else f"buy{self.buy_seq}"

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
        """
        Pozice je po odprodeji základní části - drží se už jen runner.

        Podmínka na prodané kusy je zásadní: bez ní by se za runner označil
        i čerstvě nakoupený jediný kontrakt, ze kterého se ještě nic neprodalo.
        """
        return self.sold_quantity > 0 and 0 < self.open_quantity <= self.runner_quantity

    def sell_quantity_for(self, scope: str) -> int:
        """Kolik kontraktů se prodá pro daný rozsah ('all', 'base' nebo 'one')."""
        if scope == SELL_SCOPE_ALL:
            return self.open_quantity
        if scope == SELL_SCOPE_BASE:
            return self.base_quantity
        if scope == SELL_SCOPE_ONE:
            return 1 if self.open_quantity >= 1 else 0
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
    def can_sell_one(self) -> bool:
        """
        Prodej jediného kontraktu - pro odprodávání pozice po kusech.

        Nenabízí se tam, kde by dělal totéž co jiný řádek: u pozice o jednom
        kontraktu (to je prodej všeho) ani tehdy, když základní pozice vychází
        právě na jeden kus.
        """
        return (
            self.state in (PositionState.OPEN, PositionState.SELLING)
            and self.open_quantity > 1
            and self.base_quantity != 1
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
    def buy_commission(self) -> float:
        """Provize zaplacené za nákup kontraktů."""
        return sum(self.buy_commissions.values())

    @property
    def sell_commission(self) -> float:
        """Provize zaplacené za dosavadní prodeje."""
        return sum(self.sell_commissions.values())

    @property
    def commission_total(self) -> float:
        """Součet provizí, které TWS k pozici zatím naúčtovala."""
        return self.buy_commission + self.sell_commission

    @property
    def open_commission(self) -> float:
        """
        Část nákupní provize připadající na dosud držené kusy.

        Prodejní provize se otevřené části netýká - ta se zaplatí až při
        prodeji, a dokud k němu nedojde, není co započítat.
        """
        koupeno = self.filled_quantity
        if koupeno <= 0:
            return 0.0
        return self.buy_commission * self.open_quantity / koupeno

    @property
    def realized_commission(self) -> float:
        """
        Provize připadající na už prodanou část pozice - nákup prodaných
        kusů a všechny prodeje. Zbytek nákupní provize drží otevřená část.
        """
        return self.commission_total - self.open_commission

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
    def realized_pnl_net(self) -> float | None:
        """Realizovaný výsledek po odečtení provizí, které na něj připadají."""
        hruby = self.realized_pnl
        if hruby is None:
            return None
        return hruby - self.realized_commission

    @property
    def open_pnl_net(self) -> float | None:
        """
        Výsledek držené části po odečtení nákupní provize, která na ni
        připadá. Prodejní provize v něm být nemůže - ta ještě nevznikla.
        """
        hruby = self.unrealized_pnl
        if hruby is None:
            return None
        return hruby - self.open_commission

    def sell_pnl_at(self, price: float | None, quantity: int) -> float | None:
        """
        Hrubý výsledek prodeje daného množství za zadanou cenu v USD - tedy
        co obchodník na tlačítku vydělá nebo ztratí oproti nákupní ceně.

        Provize se nezapočítávají: prodejní se dopředu nezná a nákupní by
        číslo posunula jen o zlomek, takže výsledek vychází čistě z cen -
        stejně jako limitní cena napsaná na témže tlačítku.
        """
        return calc.position_pnl(self.fill_price, price, quantity)

    @property
    def avg_sell_price(self) -> float | None:
        """Průměrná cena, za kterou prodané kontrakty odešly."""
        if self.sold_quantity <= 0:
            return None
        return self.sold_value / self.sold_quantity

    @property
    def traded(self) -> bool:
        """Pozice skutečně nakoupila, má tedy výsledek."""
        return self.filled_quantity > 0

    @property
    def holding_seconds(self) -> float | None:
        """
        Jak dlouho se pozice drží, u ukončené jak dlouho se držela.
        Bez nákupu není co měřit.
        """
        if self.fill_time is None:
            return None
        konec = datetime.now() if self.state.is_active else self.updated_at
        return max(0.0, (konec - self.fill_time).total_seconds())

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


def zbytek_text(position: Position, scope: str, quantity: int) -> str:
    """
    Popis toho, co v pozici po prodeji zbude - třeba 'v pozici zbývá runner
    (2 ks)'. Nezbývá-li nic, vrací prázdný řetězec a volající větu neskládá.

    Runnerem se zbytek nazve jen tam, kde o něj skutečně jde - tedy při
    prodeji základní pozice. Po prodeji jednoho kusu zbývá prostě zbytek.
    """
    zbyva = position.open_quantity - quantity
    if zbyva <= 0:
        return ""
    if scope == SELL_SCOPE_BASE:
        return f"v pozici zbývá runner ({zbyva} ks)"
    return f"v pozici zbývá {zbyva} ks"


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


def markup_text(markup_pct: float) -> str:
    """Přirážka pro zobrazení, například '+1 %' nebo '+2.5 %' (bez zbytečné nuly)."""
    return f"+{markup_pct:g} %"


def price_kind_label(kind: str, markup_pct: float = 0.0) -> str:
    """
    Popis ceny příkazu: 'ASK', 'BID', 'MID', nebo s přirážkou 'přirážka +1 %'.
    Přirážka se píše jen tehdy, když je nenulová.
    """
    nazev = {"ask": "ASK", "bid": "BID", "mid": "MID", "markup": "přirážka"}.get(kind)
    # Srozumitelná chyba místo holého KeyError - stejně jako v calc.*_limit_price
    if nazev is None:
        raise ValueError(f"Neznámý druh ceny: {kind}")
    if not markup_pct:
        return nazev
    return f"{nazev} {markup_text(markup_pct)}"


def buy_button_label(kind: str, quantity: int) -> str:
    """Popisek nákupního tlačítka, například '3 ks za ASK'."""
    return f"{quantity} ks za {price_kind_label(kind)}"


def order_pnl_text(pnl: float | None) -> str:
    """
    Zisk nebo ztráta prodejního příkazu do zápisu o obchodu, například
    'zisk 12 USD' nebo 'ztráta 3 USD'.

    Znaménko nese slovo, ne číslo - ve větě se čte líp než '-3 USD'.
    Zaokrouhluje se na celé dolary stejně jako na tlačítkách, drobný rozdíl
    se hlásí jako vyrovnaný (VYROVNANY_VYSLEDEK_USD). Bez nákupní ceny není
    co počítat a volající větu o výsledku neskládá.
    """
    if pnl is None:
        return ""
    if abs(pnl) < VYROVNANY_VYSLEDEK_USD:
        return "bez zisku i ztráty"
    return f"{'zisk' if pnl > 0 else 'ztráta'} {cislo_text(abs(pnl), 0)} USD"


def sell_button_text(
    kind: str,
    quantity: int,
    markup_pct: float,
    limit: float | None,
    pnl: float | None,
) -> str:
    """
    Popisek prodejního tlačítka: druh ceny, limitní cena a výsledek prodeje -
    například 'BID (3 ks) · 3.10 · +30 USD'.

    Tvar je ve všech řádcích stejný ('BID (3 ks)' nad 'BID (1 ks)'), takže
    tlačítka stejného druhu stojí ve sloupcích nad sebou. Který řádek prodává
    celou pozici a který nechává runner, říká barva a pořadí řádku.

    Tlačítko s přirážkou nese místo druhu ceny jen procenta ('+1 % · 2.18 ·
    +17 USD'): základ přirážky se mění podle trhu (vstup, nebo ASK), takže
    pevný název by nesouhlasil. Počet kusů se u něj nepíše - je jasný
    z tlačítek vedle.

    Bez použitelné kotace chybí cena a tlačítko se stejně nedá stisknout,
    bez nákupní ceny chybí výsledek; taková část se do popisku prostě nedá,
    aby na tlačítku nesvítila pomlčka.
    """
    if kind == calc.MARKUP_SELL_KIND:
        casti = [markup_text(markup_pct)]
    else:
        casti = [f"{price_kind_label(kind)} ({quantity} ks)"]

    if limit is not None:
        casti.append(cislo_text(limit))
        # Zisk se zaokrouhluje na celé dolary - tlačítek stojí v řádku deset
        # a centy by je jen prodloužily. Drobný rozdíl se píše jako '0 USD',
        # ať se nepatrná ztráta netváří znaménkem jako zisk
        if pnl is not None:
            zaokrouhleny = f"{cislo_text(pnl, 0, znamenko=True)} USD"
            vyrovnany = abs(pnl) < VYROVNANY_VYSLEDEK_USD
            casti.append("0 USD" if vyrovnany else zaokrouhleny)

    return " · ".join(casti)
