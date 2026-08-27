"""
Souhrn obchodního dne pro přehled výsledků.

Modul jen počítá - ze seznamu pozic sestaví rozdělení na běžící a ukončené,
souhrnná čísla, křivku průběhu dne a součty podle tickeru. Formátování
a vykreslení má na starosti report_dialog.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from .models import Position

# Rozsahy přehledu - dnešní obchodní den, nebo vše, co je v přehledu pozic
ROZSAH_DNES = "dnes"
ROZSAH_VSE = "vse"


@dataclass
class TickerSouhrn:
    """Výsledek jednoho tickeru napříč jeho pozicemi."""

    symbol: str
    realizovano: float = 0.0
    otevreno: float = 0.0
    # Provize zaplacené pozicemi tohoto tickeru (kladné číslo)
    provize: float = 0.0

    @property
    def celkem(self) -> float:
        """Realizovaný i otevřený výsledek dohromady, ještě bez provizí."""
        return self.realizovano + self.otevreno

    @property
    def celkem_s_provizi(self) -> float:
        """Výsledek tickeru po odečtení provizí - co ticker skutečně přinesl."""
        return self.celkem - self.provize


@dataclass
class Souhrn:
    """Souhrnná čísla obchodního dne pro dlaždice nad přehledem."""

    # Výsledek už prodaných kusů (i u pozic, které dosud drží zbytek), bez provizí
    realizovano: float = 0.0
    # Výsledek dosud držených kontraktů oceněný trhem, bez provizí
    otevreno: float = 0.0
    # Provize skutečně účtované TWS, rozdělené podle toho, ke které části
    # pozice patří (obojí kladné číslo). Držené části patří jen provize
    # za nákup - prodejní vznikne až prodejem.
    provize_realizovane: float = 0.0
    provize_otevrene: float = 0.0
    # Počty pozic podle stavu
    bezicich: int = 0
    otevrenych_pozic: int = 0
    ukoncenych: int = 0
    # Ukončené pozice rozdělené podle výsledku
    ziskovych: int = 0
    ztratovych: int = 0
    nulovych: int = 0
    # Bez nákupu, tedy bez výsledku (zrušené dřív, než se cokoliv vyplnilo)
    bez_obchodu: int = 0
    # Součty pro profit factor a průměry - už po odečtení provizí, protože
    # provize umí těsný zisk otočit ve ztrátu a statistika by pak lhala
    hruby_zisk: float = 0.0
    hruba_ztrata: float = 0.0
    nejlepsi: tuple[str, float] | None = None
    nejhorsi: tuple[str, float] | None = None
    # Kolik kontraktů se právě drží
    otevrenych_kusu: int = 0

    @property
    def celkem(self) -> float:
        """Výsledek dne dohromady bez provizí - realizovaný i otevřený."""
        return self.realizovano + self.otevreno

    @property
    def provize(self) -> float:
        """Provize zaplacené za celý den (kladné číslo)."""
        return self.provize_realizovane + self.provize_otevrene

    @property
    def realizovano_s_provizi(self) -> float:
        """Realizovaný výsledek po odečtení provizí, které na něj připadají."""
        return self.realizovano - self.provize_realizovane

    @property
    def otevreno_s_provizi(self) -> float:
        """Výsledek držených pozic snížený o už zaplacenou nákupní provizi."""
        return self.otevreno - self.provize_otevrene

    @property
    def celkem_s_provizi(self) -> float:
        """Výsledek dne po odečtení všech zaplacených provizí."""
        return self.celkem - self.provize

    @property
    def uzavrenych_s_vysledkem(self) -> int:
        """Ukončené pozice, které skutečně nakoupily, a mají tedy výsledek."""
        return self.ziskovych + self.ztratovych + self.nulovych

    @property
    def uspesnost(self) -> float | None:
        """
        Podíl ziskových pozic v procentech.
        Nulové pozice (break even) se do jmenovatele počítají, pozice
        bez nákupu ne - ty se nikdy neodehrály.
        """
        celkem = self.uzavrenych_s_vysledkem
        if celkem <= 0:
            return None
        return self.ziskovych / celkem * 100.0

    @property
    def profit_factor(self) -> float | None:
        """
        Poměr součtu ziskových pozic k součtu ztrátových, obojí po provizích.
        Bez jediné ztráty nemá smysl (dělení nulou), proto None.
        """
        if self.hruba_ztrata <= 0:
            return None
        return self.hruby_zisk / self.hruba_ztrata

    @property
    def prumerny_zisk(self) -> float | None:
        """Průměrný zisk ziskové pozice po provizích."""
        if self.ziskovych <= 0:
            return None
        return self.hruby_zisk / self.ziskovych

    @property
    def prumerna_ztrata(self) -> float | None:
        """Průměrná ztráta ztrátové pozice po provizích (kladné číslo)."""
        if self.ztratovych <= 0:
            return None
        return self.hruba_ztrata / self.ztratovych


@dataclass
class DenniReport:
    """Kompletní podklad pro přehled výsledků."""

    rozsah: str = ROZSAH_DNES
    bezici: list[Position] = field(default_factory=list)
    ukoncene: list[Position] = field(default_factory=list)
    souhrn: Souhrn = field(default_factory=Souhrn)
    # Kumulovaný realizovaný výsledek po provizích v čase - body křivky dne
    krivka: list[tuple[datetime, float]] = field(default_factory=list)
    podle_tickeru: list[TickerSouhrn] = field(default_factory=list)


def vyber(positions: list[Position], rozsah: str, den: date | None = None) -> list[Position]:
    """
    Vybere pozice spadající do zvoleného rozsahu.

    V rozsahu „dnes" projdou pozice založené nebo naposledy změněné dnešního
    dne a navíc všechny dosud aktivní - ty vyžadují pozornost bez ohledu na
    to, kdy vznikly (aplikace může běžet přes noc nebo obnovit stav
    z předchozího dne). Samotný čas založení nestačí: pozice otevřená před
    půlnocí a prodaná ráno je výsledkem dnešního dne a v přehledu chybět nesmí.

    Zbývá jedno omezení - běžící pozice si nese celý svůj realizovaný výsledek,
    i když část odprodala už předchozí den; časy jednotlivých prodejů aplikace
    neeviduje.
    """
    if rozsah == ROZSAH_VSE:
        return list(positions)

    dnesek = den or date.today()
    return [
        position
        for position in positions
        if position.state.is_active
        or position.created_at.date() == dnesek
        or position.updated_at.date() == dnesek
    ]


def _serad_ukoncene(positions: list[Position]) -> list[Position]:
    """Ukončené pozice od nejnovější - poslední výsledek dne je nahoře."""
    return sorted(positions, key=lambda p: p.updated_at, reverse=True)


def _serad_bezici(positions: list[Position]) -> list[Position]:
    """
    Běžící pozice: nejprve ty s nakoupenými kontrakty (na těch záleží nejvíc),
    uvnitř skupiny abecedně podle tickeru. Rozhoduje skutečné vyplnění, ne
    zapsaná cena - tu nese i nevyplněný příkaz podle svého limitu.
    """
    return sorted(positions, key=lambda p: (not p.traded, p.symbol, p.id))


def _spocti_souhrn(bezici: list[Position], ukoncene: list[Position]) -> Souhrn:
    """
    Sečte výsledky pozic do souhrnných čísel.

    Realizovaná i otevřená část se berou ze všech pozic. Běžící pozice mívá
    obojí (odprodaná část je hotový výsledek, i když zbytek pokračuje), ale
    ani ukončená nemusí být prázdná - skončí-li chybou nebo zrušením prodeje,
    zůstanou v ní kontrakty a jejich hodnota z přehledu zmizet nesmí.

    Statistiky úspěšnosti počítají jen doobchodované pozice, aby je
    nezkresloval výsledek, který se ještě může otočit.
    """
    souhrn = Souhrn(bezicich=len(bezici), ukoncenych=len(ukoncene))

    for position in bezici + ukoncene:
        realizovano = position.realized_pnl
        if realizovano:
            souhrn.realizovano += realizovano
        otevreno = position.unrealized_pnl
        if otevreno is not None:
            souhrn.otevreno += otevreno
        # Provize se dělí stejně jako pozice sama: co je doprodané, patří
        # k realizovanému výsledku, zbytek nese držená část
        souhrn.provize_realizovane += position.realized_commission
        souhrn.provize_otevrene += position.open_commission
        if position.open_quantity > 0:
            souhrn.otevrenych_pozic += 1
            souhrn.otevrenych_kusu += position.open_quantity

    for position in ukoncene:
        if not position.traded:
            # Pozice skončila dřív, než se vůbec nakoupilo
            souhrn.bez_obchodu += 1
            continue
        if position.open_quantity > 0:
            # Pozice sice skončila, ale kontrakty drží dál a jejich výsledek
            # se s trhem mění - mezi hotové obchody dne proto nepatří
            continue

        # O tom, jestli pozice skončila v zisku, rozhoduje výsledek po provizích
        cisty = position.realized_pnl_net or 0.0
        if cisty > 0:
            souhrn.ziskovych += 1
            souhrn.hruby_zisk += cisty
        elif cisty < 0:
            souhrn.ztratovych += 1
            souhrn.hruba_ztrata += abs(cisty)
        else:
            souhrn.nulovych += 1

        # Nejlepší a nejhorší obchod dne pro dlaždici s extrémy
        if souhrn.nejlepsi is None or cisty > souhrn.nejlepsi[1]:
            souhrn.nejlepsi = (position.symbol, cisty)
        if souhrn.nejhorsi is None or cisty < souhrn.nejhorsi[1]:
            souhrn.nejhorsi = (position.symbol, cisty)

    return souhrn


def _krivka(ukoncene: list[Position]) -> list[tuple[datetime, float]]:
    """
    Kumulovaný realizovaný výsledek v čase - jak se den vyvíjel.

    Sčítá se realizovaný výsledek po provizích, které na něj připadají - tedy
    totéž číslo, jaké u pozice ukazuje seznam i souhrnné dlaždice. Body
    vznikají v čase ukončení pozice (updated_at) a řadí se vzestupně; pozice
    bez nákupu se přeskakují, protože výsledkem nepohnuly.
    """
    body: list[tuple[datetime, float]] = []
    soucet = 0.0
    for position in sorted(ukoncene, key=lambda p: p.updated_at):
        if not position.traded:
            continue
        soucet += position.realized_pnl_net or 0.0
        body.append((position.updated_at, soucet))
    return body


def _podle_tickeru(bezici: list[Position], ukoncene: list[Position]) -> list[TickerSouhrn]:
    """
    Součty výsledků po tickerech, seřazené od nejlepšího po nejhorší.
    Ticker se objeví, jen když má co ukázat - pozice bez nákupu se vynechá.
    """
    souhrny: dict[str, TickerSouhrn] = {}

    def zaznam(symbol: str) -> TickerSouhrn:
        """Souhrn tickeru; při prvním výskytu jej založí."""
        if symbol not in souhrny:
            souhrny[symbol] = TickerSouhrn(symbol=symbol)
        return souhrny[symbol]

    for position in bezici + ukoncene:
        realizovano = position.realized_pnl
        # Držené kusy může mít i ukončená pozice; bez nich vychází None sama
        otevreno = position.unrealized_pnl
        provize = position.commission_total
        # Pozice bez výsledku i bez provize nemá co ukázat; zaplacená provize
        # se ale objevit musí, i když sám výsledek vyšel nulový
        if not realizovano and otevreno is None and not provize:
            continue
        polozka = zaznam(position.symbol)
        polozka.realizovano += realizovano or 0.0
        polozka.otevreno += otevreno or 0.0
        polozka.provize += provize

    return sorted(souhrny.values(), key=lambda s: s.celkem_s_provizi, reverse=True)


def sestav(
    positions: list[Position], rozsah: str = ROZSAH_DNES, den: date | None = None
) -> DenniReport:
    """
    Sestaví kompletní přehled výsledků ze seznamu pozic.

    Parametr rozsah rozhoduje, co se do přehledu dostane (ROZSAH_DNES /
    ROZSAH_VSE), den umožňuje testům určit „dnešek" napevno.
    """
    vybrane = vyber(positions, rozsah, den)
    bezici = [p for p in vybrane if p.state.is_active]
    ukoncene = [p for p in vybrane if not p.state.is_active]

    return DenniReport(
        rozsah=rozsah,
        bezici=_serad_bezici(bezici),
        ukoncene=_serad_ukoncene(ukoncene),
        souhrn=_spocti_souhrn(bezici, ukoncene),
        krivka=_krivka(ukoncene),
        podle_tickeru=_podle_tickeru(bezici, ukoncene),
    )
