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
    kandidati = strikes_beyond(serazene, price, right)

    # Celý řetězec leží na opačné straně - zbývá nejbližší strike k ceně
    if not kandidati:
        return nearest_strike(serazene, price)

    return kandidati[min(steps, len(kandidati)) - 1]


def strikes_beyond(strikes: list[float], strike: float, right: str) -> list[float]:
    """
    Strike ceny ležící od zadaného striku dál mimo peníze, seřazené od něj.

    U CALL jsou to vyšší strike vzestupně, u PUT nižší sestupně. Slouží
    k posunu na levnější opci, když je vybraná dražší než limit ceny.
    """
    if right == "C":
        return sorted(s for s in strikes if s > strike)
    return sorted((s for s in strikes if s < strike), reverse=True)


def strike_violation(
    cost: float | None, delta: float | None, max_cost: float, min_delta: float
) -> float | None:
    """
    Míra, o kolik opce porušuje limity výběru striku - nula znamená žádné porušení.

    cost je cena jednoho kontraktu v USD, delta delta opce (u PUT záporná,
    porovnává se absolutní hodnota). Překročení ceny i nedostatek delty se
    měří relativně k limitu, takže jdou porovnat: cena 550 USD při limitu
    500 USD je porušení 0,1, delta 0,18 při minimu 0,20 také 0,1. Výsledkem
    je horší z obou. Nulový limit je vypnutý. Neznámá delta se nepočítá jako
    porušení (nelze ji posoudit), neznámá cena při zapnutém limitu ceny
    vrací None - taková opce se posoudit nedá vůbec.
    """
    prekroceni = 0.0
    if max_cost > 0:
        if cost is None:
            return None
        prekroceni = max(0.0, cost / max_cost - 1.0)

    nedostatek = 0.0
    if min_delta > 0 and delta is not None:
        nedostatek = max(0.0, 1.0 - abs(delta) / min_delta)

    return max(prekroceni, nedostatek)


def pick_strike_candidate(
    candidates: list[tuple[float | None, float | None]], max_cost: float, min_delta: float
) -> int:
    """
    Vybere nejvýhodnějšího kandidáta striku a vrátí jeho index.

    candidates jsou dvojice (cena kontraktu v USD, delta) seřazené od peněz
    dál mimo peníze. Vyhrává kandidát s nejmenším porušením limitů (viz
    strike_violation); při shodě ten blíž penězům, protože má vyšší deltu.
    Splňuje-li limity víc kandidátů, vyhraje tedy první z nich. Nedá-li se
    posoudit žádný kandidát, vrací se první (původní výběr).
    """
    # Dvojice (porušení, index) - při shodě porušení vyhraje nižší index,
    # tedy kandidát blíž penězům
    posouzene = [
        (skore, index)
        for index, (cost, delta) in enumerate(candidates)
        if (skore := strike_violation(cost, delta, max_cost, min_delta)) is not None
    ]
    return min(posouzene)[1] if posouzene else 0


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


# ---------------------------------------------------------------------------
# Odhad delty z ceny opce
#
# TWS model greeks (a s nimi deltu) u opcí neposílá spolehlivě - podle
# nastavení účtu a předplatného dat nemusí dorazit vůbec. Delta je přitom
# potřeba pro kontrolu minimální delty při výběru striku, proto se dá
# dopočítat z ceny opce: nejprve se z tržní ceny odvodí implikovaná
# volatilita, z ní pak delta podle modelu Black-Scholes. Převzato ze
# sousedního projektu TWS-opce.
# ---------------------------------------------------------------------------

# Bezriziková sazba pro odhad delty v procentech. U krátkých expirací,
# na které aplikace míří, má na deltu zanedbatelný vliv.
RISK_FREE_RATE_PCT = 4.0
# Meze pro hledání implikované volatility (0,1 % až 500 %)
_IV_MIN = 0.001
_IV_MAX = 5.0
# Dnešní expirace se počítá jako půl dne, aby vzorce nedělily nulou
_MIN_YEARS = 0.5 / 365.0


def norm_cdf(x: float) -> float:
    """Distribuční funkce standardního normálního rozdělení."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1(spot: float, strike: float, years: float, rate: float, sigma: float) -> float:
    """Člen d1 modelu Black-Scholes; years i sigma musí být kladné."""
    return (math.log(spot / strike) + (rate + sigma * sigma / 2.0) * years) / (
        sigma * math.sqrt(years)
    )


def black_scholes_price(
    spot: float, strike: float, years: float, rate: float, sigma: float, right: str
) -> float:
    """
    Teoretická cena opce podle modelu Black-Scholes.
    rate je bezriziková sazba v desetinném tvaru (0.04 = 4 %), sigma volatilita;
    years i sigma musí být kladné.
    """
    d1 = _d1(spot, strike, years, rate, sigma)
    d2 = d1 - sigma * math.sqrt(years)
    discount = math.exp(-rate * years)

    if right == "C":
        return spot * norm_cdf(d1) - strike * discount * norm_cdf(d2)
    return strike * discount * norm_cdf(-d2) - spot * norm_cdf(-d1)


def black_scholes_delta(
    spot: float, strike: float, years: float, rate: float, sigma: float, right: str
) -> float:
    """Delta opce podle modelu Black-Scholes (u PUT záporná)."""
    delta_call = norm_cdf(_d1(spot, strike, years, rate, sigma))
    return delta_call if right == "C" else delta_call - 1.0


def implied_volatility(
    option_price: float, spot: float, strike: float, years: float, rate: float, right: str
) -> float | None:
    """
    Implikovaná volatilita odvozená z tržní ceny opce metodou půlení intervalu.
    Vrací None, pokud cena leží mimo rozsah, který model dokáže vysvětlit.
    """
    if strike <= 0:
        return None

    # Cena pod vnitřní hodnotou nebo nad cenou podkladu je pro model neřešitelná
    intrinsic = max(0.0, spot - strike) if right == "C" else max(0.0, strike - spot)
    if option_price < intrinsic or option_price > spot:
        return None

    low, high = _IV_MIN, _IV_MAX
    if black_scholes_price(spot, strike, years, rate, high, right) < option_price:
        return None

    # Padesát půlení dává přesnost hluboko pod rozlišení tržních cen
    for _ in range(50):
        mid = (low + high) / 2.0
        if black_scholes_price(spot, strike, years, rate, mid, right) < option_price:
            low = mid
        else:
            high = mid

    return (low + high) / 2.0


def estimate_delta(
    option_price: float | None,
    spot: float | None,
    strike: float,
    expiration: str,
    right: str,
) -> float | None:
    """
    Odhad delty opce z její tržní ceny (u PUT záporná).
    Slouží jako náhrada, když TWS nepošle model greeks.
    Vrací None, pokud odhad nelze spolehlivě provést.
    """
    if not is_price(option_price) or not is_price(spot):
        return None

    # Doba do expirace v letech, nejméně půl dne
    years = max(days_to_expiry(expiration), 0) / 365.0 + _MIN_YEARS
    rate = RISK_FREE_RATE_PCT / 100.0

    sigma = implied_volatility(option_price, spot, strike, years, rate, right)
    if sigma is None:
        return None

    return black_scholes_delta(spot, strike, years, rate, sigma, right)


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


def premium_divergence(
    right: str,
    entry_underlying: float | None,
    underlying: float | None,
    entry_premium: float | None,
    premium: float | None,
    min_underlying_pct: float,
    min_premium_pct: float,
) -> tuple[float, float] | None:
    """
    Rozpor mezi pohybem podkladu a prémií opce od vstupu do pozice.

    Rozpor nastává, když se podklad od vstupu pohnul ve prospěch opce
    (u CALL nahoru, u PUT dolů) aspoň o min_underlying_pct procent a prémie
    přitom klesla aspoň o min_premium_pct procent. Opce pak nesleduje
    podklad - typicky ji rozpouští časový rozpad nebo pokles implikované
    volatility - a další pohyb podkladu ji nemusí zachránit.

    right je 'C' nebo 'P', entry_* hodnoty v okamžiku vstupu, ostatní
    aktuální. Vrací dvojici (změna podkladu v %, změna prémie v %)
    se znaménkem skutečného pohybu; bez rozporu nebo bez některé z cen None.
    """
    if not all(is_price(c) for c in (entry_underlying, underlying, entry_premium, premium)):
        return None

    podklad_pct = (underlying / entry_underlying - 1.0) * 100.0
    premie_pct = (premium / entry_premium - 1.0) * 100.0

    # Pohyb ve prospěch opce: CALL vydělává na růstu podkladu, PUT na poklesu.
    # Nulový pohyb rozporem není ani při nulových prazích
    prospech_pct = podklad_pct if right == "C" else -podklad_pct
    if prospech_pct <= 0 or premie_pct >= 0:
        return None
    if prospech_pct < min_underlying_pct or -premie_pct < min_premium_pct:
        return None
    return podklad_pct, premie_pct


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
