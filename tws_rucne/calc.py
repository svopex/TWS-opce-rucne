"""
Výpočty pro ruční obchodování opcí.

Modul je záměrně bez závislosti na TWS - obsahuje jen čisté funkce
(výběr strike a expirace, limitní ceny, spread, výsledek pozice),
takže je lze samostatně otestovat.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

# Jeden opční kontrakt kryje 100 kusů podkladu
OPTION_MULTIPLIER = 100

# Druhy limitní ceny nabízené tlačítky rozhraní.
# Prodávat lze i za ASK - kdo nespěchá, nechá příkaz čekat na poptávku
# a spread nezaplatí, ale inkasuje. Druh 'markup' prodává s přirážkou
# nad vyšší z cen vstup / ASK (viz markup_sell_price).
BUY_KINDS = ("ask", "mid")
QUOTE_SELL_KINDS = ("bid", "mid", "ask")
MARKUP_SELL_KIND = "markup"
SELL_KINDS = QUOTE_SELL_KINDS + (MARKUP_SELL_KIND,)


def is_price(value: float | None) -> bool:
    """Ověří, že hodnota je použitelná cena - konečné kladné číslo."""
    if value is None or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and value > 0


def mid_price(bid: float | None, ask: float | None) -> float | None:
    """Střed trhu z obou stran kotace. Bez úplné kotace vrací None."""
    if not (is_price(bid) and is_price(ask)):
        return None
    if ask < bid:
        return None
    return (bid + ask) / 2.0


def spread_pct(bid: float | None, ask: float | None) -> float | None:
    """
    Spread v procentech ze středu trhu: (ASK - BID) / MID * 100.
    Bez použitelné kotace vrací None.
    """
    stred = mid_price(bid, ask)
    if stred is None or stred <= 0:
        return None
    return (ask - bid) / stred * 100.0


def round_to_tick(price: float, min_tick: float) -> float:
    """
    Zaokrouhlí cenu na nejbližší násobek minimálního tiku kontraktu.

    Počítá se přes Decimal, protože v plovoucí čárce vychází například
    1,665 / 0,01 jako 166,49999... - výsledek by spadl o celý tik níž,
    než jakou cenu má obchodník před sebou v náhledu.

    Přesná půlka tiku se zaokrouhluje nahoru (1,665 při tiku 0,01 dá 1,67),
    ne na sudou hodnotu, jak to dělá vestavěné round(). Cena na tlačítku
    tak odpovídá střední ceně vypsané v náhledu.
    """
    if not min_tick or min_tick <= 0 or not math.isfinite(min_tick):
        # Bez známého tiku se počítá s centy - jemnější rastr opce nemají
        min_tick = 0.01

    tik = Decimal(str(min_tick))
    kroku = (Decimal(str(price)) / tik).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(kroku * tik)


def buy_limit_price(
    kind: str, bid: float | None, ask: float | None, ask_tolerance_pct: float = 0.0
) -> float | None:
    """
    Limitní cena nákupu podle zvoleného tlačítka.

    kind = 'ask' kupuje na poptávané ceně (volitelně navýšené o toleranci,
    aby příkaz prošel i při drobném pohybu trhu), kind = 'mid' na středu trhu.
    Bez potřebné kotace vrací None - příkaz se pak nezadává.
    """
    if kind == "ask":
        if not is_price(ask):
            return None
        return ask * (1.0 + max(0.0, ask_tolerance_pct) / 100.0)

    if kind == "mid":
        return mid_price(bid, ask)

    raise ValueError(f"Neznámý druh nákupní ceny: {kind}")


def sell_limit_price(
    kind: str,
    bid: float | None,
    ask: float | None,
    bid_tolerance_pct: float = 0.0,
) -> float | None:
    """
    Limitní cena prodeje za kotaci podle zvoleného tlačítka.

    kind = 'bid' prodává na nabízené ceně (volitelně snížené o toleranci),
    kind = 'mid' na středu trhu, kind = 'ask' na poptávané ceně.
    Bez potřebné kotace vrací None.
    """
    if kind == "bid":
        if not is_price(bid):
            return None
        return bid * (1.0 - max(0.0, bid_tolerance_pct) / 100.0)

    if kind == "mid":
        return mid_price(bid, ask)

    if kind == "ask":
        return ask if is_price(ask) else None

    raise ValueError(f"Neznámý druh prodejní ceny za kotaci: {kind}")


def markup_sell_price(
    entry_price: float | None, ask: float | None, markup_pct: float
) -> float | None:
    """
    Limitní cena prodeje s přirážkou nad vyšší z cen vstup / ASK.

    entry_price je průměrná cena vyplněného nákupu, markup_pct přirážka
    v procentech (nabídku určuje trading.sell_markups_pct v konfiguraci).

    Pozice ve ztrátě (ASK pod vstupem) tak míří na zisk daný procenty
    z nákupu. Pozice v zisku staví přirážky nad ASK - přirážka nad vstupem
    by ležela pod trhem, příkaz by se vyplnil hned za tržní cenu a řada
    tlačítek by nerostla. Bez kotace zbývá jako základ vstupní cena,
    bez vstupní ceny (nevyplněný nákup) vrací None.
    """
    if not is_price(entry_price):
        return None
    zaklad = max(entry_price, ask) if is_price(ask) else entry_price
    return zaklad * (1.0 + markup_pct / 100.0)


def nearest_strike(strikes: list[float], target: float) -> float | None:
    """Najde strike nejbližší zadané ceně. Vrací None při prázdném seznamu."""
    if not strikes:
        return None
    return min(strikes, key=lambda s: (abs(s - target), s))


def otm_strike(strikes: list[float], price: float, right: str, steps: int) -> float | None:
    """
    Strike odsazený od aktuální ceny podkladu na stranu mimo peníze (OTM).

    Mimo peníze leží u CALL strike nad cenou podkladu, u PUT pod ní.
    Parametr steps udává, kolikátý takový strike se vybere: 1 je první
    strike za cenou, 2 druhý a tak dál. Odsazení se počítá v krocích rastru
    řetězce, ne v bodech - u SPY (rastr 1) je krok dolar, u AAPL (rastr 2,5)
    dva a půl, takže jedno nastavení platí pro všechny tickery.

    steps <= 0 vrací nejbližší strike k ceně podkladu (ATM), ať leží kdekoliv.
    Nemá-li řetězec dost striků, vrací ten nejvzdálenější dostupný.
    """
    if not strikes:
        return None

    serazene = sorted(strikes)
    if steps <= 0:
        return nearest_strike(serazene, price)

    # Kandidáti mimo peníze seřazení od ceny podkladu dál: u CALL vzestupně
    # nad cenou, u PUT sestupně pod ní
    if right == "C":
        kandidati = [s for s in serazene if s > price]
    else:
        kandidati = [s for s in reversed(serazene) if s < price]

    # Celý řetězec leží na opačné straně - zbývá nejbližší strike k ceně
    if not kandidati:
        return nearest_strike(serazene, price)

    return kandidati[min(steps, len(kandidati)) - 1]


def days_to_expiry(expiration: str, today: date | None = None) -> int:
    """Počet dní do expirace ze zápisu YYYYMMDD."""
    ref = today or date.today()
    exp = datetime.strptime(expiration, "%Y%m%d").date()
    return (exp - ref).days


def select_expiration(
    expirations: list[str],
    mode: str,
    min_dte: int,
    fixed_date: str = "",
    today: date | None = None,
) -> str | None:
    """
    Vybere expiraci podle konfigurace.
    mode = 'fixed' vrátí zadané datum, pokud je v nabídce.
    mode = 'nearest' vrátí nejbližší expiraci s počtem dní >= min_dte;
    pokud taková neexistuje, vrátí nejvzdálenější dostupnou.
    """
    if not expirations:
        return None

    dostupne = sorted(expirations)

    if mode == "fixed":
        return fixed_date if fixed_date in dostupne else None

    ref = today or date.today()
    # Již proběhlé expirace se přeskakují, dál platí minimální počet dní
    vhodne = [e for e in dostupne if days_to_expiry(e, ref) >= max(0, min_dte)]
    if vhodne:
        return vhodne[0]
    return dostupne[-1]


def order_value(price: float | None, quantity: int) -> float | None:
    """Hodnota příkazu v USD: cena opce krát multiplikátor krát počet kontraktů."""
    if not is_price(price) or quantity < 1:
        return None
    return price * OPTION_MULTIPLIER * quantity


def position_pnl(
    fill_price: float | None,
    market_price: float | None,
    quantity: int,
) -> float | None:
    """
    Nerealizovaný výsledek držených kontraktů v USD.
    Bez nákupní nebo aktuální ceny vrací None.
    """
    if fill_price is None or market_price is None or quantity < 1:
        return None
    return (market_price - fill_price) * OPTION_MULTIPLIER * quantity


def realized_pnl(
    fill_price: float | None,
    sold_quantity: int,
    sold_value: float,
) -> float | None:
    """
    Realizovaný výsledek prodaných kontraktů v USD.

    sold_value je suma prodejních cen krát počty kusů (tedy cena za kontrakt,
    ne za pozici), aby se do výsledku promítly různé ceny z více prodejů.
    """
    if fill_price is None or sold_quantity < 1:
        return None
    return (sold_value - fill_price * sold_quantity) * OPTION_MULTIPLIER
