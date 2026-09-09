"""
Popup s přehledem výsledků obchodního dne.

Ukazuje na jedné obrazovce, co se dnes obchodovalo, co se ještě drží a s jakým
výsledkem: souhrnné dlaždice, seznam běžících a ukončených pozic a dva grafy.
Rozvržení je navržené tak, aby se vešlo bez posuvníku - posouvají se nejvýš
samotné seznamy uvnitř svých panelů.

Přehled žije souběžně s přehledem pozic: otevřený popup se obnovuje ze stejné
periodické smyčky, takže držené pozice v něm tikají živě.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime
from typing import Any

from nicegui import ui

from . import report
from .engine import ManualEngine
from .models import Position, PositionState, cislo_text

# Popisky přepínače rozsahu přehledu
ROZSAHY = {report.ROZSAH_DNES: "Dnes", report.ROZSAH_VSE: "Vše"}

# Všechny stavové třídy pohromadě - při přepisu stavu je nutné odebrat
# tu předchozí, ať už byla kterákoliv
VSECHNY_STAVY = " ".join(sorted({stav.css_class for stav in PositionState}))

# Definice souhrnných dlaždic nad přehledem: klíč a nadpis. Hodnoty do nich
# doplňuje _hodnoty_dlazdic podle spočítaného souhrnu
DLAZDICE = (
    ("celkem", "Výsledek dne"),
    ("z_uctu", "Z účtu"),
    ("realizovano", "Realizováno"),
    ("otevreno", "Otevřené pozice"),
    ("uspesnost", "Úspěšnost"),
    ("factor", "Profit factor"),
    ("pozice", "Pozice"),
)


def penize(hodnota: float | None, znamenko: bool = True) -> str:
    """
    Částka v USD pro přehled - formát čísla řeší cislo_text, tady se navíc
    ošetří chybějící hodnota (pomlčka). Se znaménkem se zobrazují výsledky,
    aby byl zisk na první pohled patrný, bez něj ceny.
    """
    if hodnota is None:
        return "-"
    return cislo_text(hodnota, znamenko=znamenko)


def _zavorka_bez_provizi(
    cisty: float | None, hruby: float | None, zapis: Callable[[float | None], str]
) -> str:
    """
    Hodnota před provizemi do závorky za hlavní číslo.

    Vrací jen text závorky; hlavní hodnotu vypisuje volající zvlášť, aby si ji
    mohl obarvit podle výsledku. Bez zaplacené provize (obě hodnoty stejné na
    zobrazovaná místa) je závorka prázdná - opakovat totéž číslo dvakrát nemá
    smysl. Zápis čísla dodává volající, pravidlo je pro částky i procenta totéž.
    """
    if cisty is None or hruby is None:
        return ""
    if abs(hruby - cisty) < 0.005:
        return ""
    return f"({zapis(hruby)})"


def penize_s_provizi(cisty: float | None, hruby: float | None) -> str:
    """Tatáž částka bez provizí do závorky - '-92.00 (-85.00)'."""
    return _zavorka_bez_provizi(cisty, hruby, penize)


def procenta(hodnota: float | None, desetin: int = 2) -> str:
    """
    Podíl z účtu v procentech - '+0.62 %'. Znaménko se uvádí vždy, aby se
    zisk od ztráty poznal stejně jako u částek. Chybějící hodnota (neznámá
    velikost účtu) je pomlčka.
    """
    if hodnota is None:
        return "-"
    return f"{cislo_text(hodnota, desetin, znamenko=True)} %"


def procenta_s_provizi(cisty: float | None, hruby: float | None) -> str:
    """Tentýž podíl z účtu bez provizí do závorky - '+0.62 % (+0.68 %)'."""
    return _zavorka_bez_provizi(cisty, hruby, procenta)


def trida_vysledku(hodnota: float | None) -> str:
    """CSS třída pro obarvení částky - zisk zeleně, ztráta červeně."""
    if hodnota is None or abs(hodnota) < 0.005:
        return ""
    return "zisk" if hodnota > 0 else "ztrata"


def sklonuj(pocet: int, jednotne: str, mnozne: str, genitiv: str) -> str:
    """
    Počet se správným tvarem: 1 pozice, 2-4 pozice, 5 a víc pozic.
    Tvary se předávají celé včetně přívlastku ('otevřená pozice').
    """
    if pocet == 1:
        return f"{pocet} {jednotne}"
    if 2 <= pocet <= 4:
        return f"{pocet} {mnozne}"
    return f"{pocet} {genitiv}"


def doba_drzeni(sekundy: float | None) -> str:
    """Délka držení pozice ve zkráceném tvaru - '48 s', '12 min', '1:05 h'."""
    if sekundy is None:
        return "-"
    celkem = int(sekundy)
    if celkem < 60:
        return f"{celkem} s"
    minuty, _ = divmod(celkem, 60)
    if minuty < 60:
        return f"{minuty} min"
    hodiny, zbytek_minut = divmod(minuty, 60)
    return f"{hodiny}:{zbytek_minut:02d} h"


def mez_osy(hodnoty: list[float]) -> tuple[float, float]:
    """
    Spodní a horní mez svislé osy s rezervou na popisky nad a pod sloupci.

    Meze se zaokrouhlují na kulatý krok odvozený z řádu největší hodnoty,
    aby na ose nestála čísla jako 354. Osa vždy obsahuje nulu, jinak by
    sloupce neměly společný základ.
    """
    dolni = min(min(hodnoty), 0.0)
    horni = max(max(hodnoty), 0.0)
    rozsah = max(abs(dolni), abs(horni))
    if rozsah <= 0:
        return -1.0, 1.0

    # Krok je polovina řádu největší hodnoty - u stovek tedy 50
    krok = 10 ** math.floor(math.log10(rozsah)) / 2
    rezerva = rozsah * 0.18
    # Rezerva se přidává jen na té straně, kde nějaké sloupce skutečně jsou -
    # jinak by graf se samými zisky měl pod nulou prázdné pásmo
    return (
        math.floor((dolni - rezerva) / krok) * krok if dolni < 0 else 0.0,
        math.ceil((horni + rezerva) / krok) * krok if horni > 0 else 0.0,
    )


def pomer_pruh(podil: float, trida: str) -> None:
    """
    Jedna polovina pruhu porovnání - podíl z nejlepšího výsledku dne.

    Délka pruhu je údaj z dat, proto se do stylů předává proměnnou --pomer
    (0 až 1); barvu, výšku i zaoblení obou konců má na starosti styles.css.

    Pruh nekreslí ui.linear_progress schválně: Quasar roztahuje vnitřní pruh
    transformací scaleX, která spolu s ním vodorovně smrskne i poloměr rohů -
    krátký pruh pak vyjde hranatý. Šířka v procentech tímhle netrpí.
    """
    ui.element("div").classes(f"pomer-pruh {trida}").style(f"--pomer: {podil:.4f}")


def kontrakt_text(position: Position) -> str:
    """Popis opčního kontraktu bez tickeru - ten stojí ve vlastním sloupci."""
    if not position.expiration:
        return "-"
    return f"{position.right_label} {position.expiration} @ {position.strike:g}"


class ReportDialog:
    """
    Popup s přehledem výsledků. Vzniká jednou při stavbě stránky (aby jeho
    prvky patřily danému klientovi) a otevírá se tlačítkem nad přehledem
    pozic; obsah se plní až při otevření a pak průběžně obnovuje.
    """

    def __init__(
        self,
        engine: ManualEngine,
        je_tmavy: Callable[[], bool],
        prepni_vzhled: Callable[[], None],
    ) -> None:
        self.engine = engine
        # Vzhled grafů se řídí přepínačem světlý/tmavý režim. Dialog přes celou
        # obrazovku hlavičku překrývá, proto má vlastní tlačítko, které sahá
        # na tentýž přepínač - obojí tak zůstává v jednom stavu
        self.je_tmavy = je_tmavy
        self.prepni_vzhled = prepni_vzhled
        self.rozsah = report.ROZSAH_DNES

        # Podpisy naposledy vykreslených dat. Seznamy a grafy se překreslují
        # jen při skutečné změně, aby přehled každou vteřinu neblikal.
        # None znamená „ještě nevykresleno" - prázdná n-tice je platný podpis
        # prázdného seznamu a nesmí se s tím zaměnit
        self._podpis_bezici: tuple | None = None
        self._podpis_uzavrene: tuple | None = None
        self._podpis_krivka: tuple | None = None
        self._podpis_tickery: tuple | None = None
        # Prvky řádků běžících pozic podle id - do nich se zapisují živé
        # hodnoty (kotace, P/L, stav), aniž by se řádek stavěl znovu
        self._radky_bezici: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Sestavení dialogu
    # ------------------------------------------------------------------

    def build(self) -> None:
        """Vykreslí prázdnou kostru dialogu; obsah doplní až otevření."""
        with ui.dialog().props("maximized").classes("dialog-report-obal") as self.dialog:
            with ui.card().classes("dialog-report"):
                self._build_hlavicka()

                # Pás souhrnných dlaždic
                self.dlazdice: dict[str, dict[str, Any]] = {}
                with ui.element("div").classes("report-dlazdice"):
                    for klic, nadpis in DLAZDICE:
                        self.dlazdice[klic] = self._build_dlazdice(klic, nadpis)

                # Hlavní plocha: vlevo seznamy pozic, vpravo grafy
                with ui.element("div").classes("report-mrizka"):
                    with ui.element("div").classes("report-sloupec"):
                        self.telo_bezici, self.podnadpis_bezici = self._build_panel(
                            "Drží se teď", "report-panel-bezici"
                        )
                        self.telo_uzavrene, self.podnadpis_uzavrene = self._build_panel(
                            "Ukončené pozice", "report-panel-uzavrene"
                        )
                    with ui.element("div").classes("report-sloupec"):
                        telo_krivka, self.podnadpis_krivka = self._build_panel(
                            "Průběh dne", "report-panel-graf"
                        )
                        with telo_krivka:
                            self.graf_krivka = ui.echart(
                                self._prazdny_graf()
                            ).classes("report-graf")
                            self.prazdno_krivka = ui.label(
                                "Křivka se objeví s první uzavřenou pozicí."
                            ).classes("report-prazdno")
                        telo_tickery, self.podnadpis_tickery = self._build_panel(
                            "Výsledek podle tickeru", "report-panel-graf"
                        )
                        with telo_tickery:
                            self.graf_tickery = ui.echart(
                                self._prazdny_graf()
                            ).classes("report-graf")
                            self.prazdno_tickery = ui.label(
                                "Sloupce se objeví s prvním nákupem."
                            ).classes("report-prazdno")

    def _build_hlavicka(self) -> None:
        """Horní lišta dialogu - nadpis, přepínač rozsahu a zavření."""
        with ui.element("div").classes("report-hlavicka"):
            ui.label("Výsledky").classes("report-nadpis")
            self.datum_label = ui.label("").classes("report-datum")
            ui.element("div").classes("report-vypln")

            # Rozsah přehledu: dnešní obchodní den, nebo vše, co drží přehled
            self.prepinac_rozsahu = (
                ui.toggle(ROZSAHY, value=self.rozsah, on_change=self._on_rozsah)
                .props("dense no-caps unelevated toggle-color=primary")
                .classes("report-prepinac")
                .tooltip(
                    "Dnes: pozice založené dnešního dne a všechny, které stále "
                    "běží. Vše: celý obsah přehledu bez ohledu na datum."
                )
            )
            self.tlacitko_vzhled = (
                ui.button(on_click=self._prepni_vzhled)
                .props("flat round dense")
                .classes("report-vzhled")
                .tooltip("Přepnout světlý/tmavý vzhled")
            )
            self._obnov_tlacitko_vzhledu()
            ui.button(icon="close", on_click=self.dialog.close).props(
                "flat round dense"
            ).classes("report-zavrit").tooltip("Zavřít přehled")

    def _prepni_vzhled(self) -> None:
        """Přepne vzhled celé aplikace a srovná ikonu tlačítka."""
        self.prepni_vzhled()
        self._obnov_tlacitko_vzhledu()

    def _obnov_tlacitko_vzhledu(self) -> None:
        """Ikona ukazuje režim, do kterého se lze přepnout."""
        ikona = "light_mode" if self.je_tmavy() else "dark_mode"
        self.tlacitko_vzhled.props(f"icon={ikona}")

    def _build_dlazdice(self, klic: str, nadpis: str) -> dict[str, Any]:
        """
        Jedna souhrnná dlaždice - nadpis, velká hodnota a popisek pod ní.
        Vedle hodnoty stojí drobnější závorka s toutéž částkou bez provizí;
        u dlaždic, kterých se provize netýkají, zůstává prázdná.
        """
        with ui.element("div").classes(f"report-dlazdice-polozka dlazdice-{klic}"):
            ui.label(nadpis).classes("dlazdice-nadpis")
            with ui.element("div").classes("dlazdice-hodnota"):
                hodnota = ui.label("-").classes("dlazdice-cislo")
                hrube = ui.label("").classes("hodnota-hrube")
            popis = ui.label("").classes("dlazdice-popis")
        return {"hodnota": hodnota, "hrube": hrube, "popis": popis}

    def _build_panel(self, nadpis: str, trida: str) -> tuple[Any, Any]:
        """
        Panel přehledu - hlavička s nadpisem a doplňujícím popiskem a prázdné
        tělo. Vrací tělo (do něj se vykresluje obsah) a popisek v hlavičce.
        """
        with ui.element("div").classes(f"report-panel {trida}"):
            with ui.element("div").classes("report-panel-hlavicka"):
                ui.label(nadpis).classes("report-panel-nadpis")
                podnadpis = ui.label("").classes("report-panel-podnadpis")
            telo = ui.element("div").classes("report-panel-telo")
        return telo, podnadpis

    # ------------------------------------------------------------------
    # Otevření a obnova
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Otevře přehled a rovnou jej naplní aktuálními daty."""
        self.dialog.open()
        self.refresh()

    def _on_rozsah(self, event: Any) -> None:
        """Přepnutí rozsahu (dnes / vše) překreslí celý přehled."""
        self.rozsah = event.value or report.ROZSAH_DNES
        # Podpisy se zahodí, aby se seznamy i grafy postavily znovu
        self._podpis_bezici = None
        self._podpis_uzavrene = None
        self._podpis_krivka = None
        self._podpis_tickery = None
        self.refresh()

    def refresh(self) -> None:
        """
        Obnoví obsah přehledu z aktuálního stavu enginu.
        Volá se z periodické smyčky rozhraní; zavřený dialog se přeskakuje.
        """
        if not self.dialog.value:
            return

        podklad = report.sestav(
            list(self.engine.positions.values()),
            self.rozsah,
            # Procenta z účtu počítá souhrn, potřebuje k tomu jeho velikost
            account_size=self.engine.account_size,
        )
        self.datum_label.set_text(f"{datetime.now():%d.%m.%Y %H:%M:%S}")
        self._obnov_tlacitko_vzhledu()
        self._vykresli_dlazdice(podklad)
        self._vykresli_bezici(podklad)
        self._vykresli_uzavrene(podklad)
        self._vykresli_krivku(podklad)
        self._vykresli_tickery(podklad)

    # ------------------------------------------------------------------
    # Souhrnné dlaždice
    # ------------------------------------------------------------------

    def _hodnoty_dlazdic(
        self, podklad: report.DenniReport
    ) -> dict[str, tuple[str, str, str, str]]:
        """
        Text, hodnota bez provizí, popisek a barevná třída pro každou dlaždici.
        Vrací slovník klíč -> (hodnota, závorka bez provizí, popis, třída);
        hlavní hodnota je vždy ta po provizích, tedy skutečný výsledek.
        """
        s = podklad.souhrn

        # Úspěšnost a profit factor dávají smysl až u ukončených pozic
        if s.uspesnost is None:
            uspesnost = ("-", "", "zatím nic uzavřeného", "")
        else:
            uspesnost = (
                f"{s.uspesnost:.0f} %",
                "",
                f"{s.ziskovych}× zisk / {s.ztratovych}× ztráta",
                "zisk" if s.uspesnost >= 50 else "ztrata",
            )

        if s.uzavrenych_s_vysledkem == 0:
            factor = ("-", "", "zatím nic uzavřeného", "")
        elif s.profit_factor is None:
            # Bez jediné ztráty se poměr nedá spočítat - řekne se to natvrdo
            factor = ("∞", "", "žádná ztráta", "zisk")
        else:
            factor = (
                f"{s.profit_factor:.2f}".replace(".", ","),
                "",
                f"Ø {penize(s.prumerny_zisk)} / Ø {penize(-(s.prumerna_ztrata or 0))}",
                "zisk" if s.profit_factor >= 1 else "ztrata",
            )

        bez_nakupu = f" · {s.bez_obchodu} bez nákupu" if s.bez_obchodu else ""

        # Část realizovaného výsledku může pocházet z pozic, které dosud drží
        # zbytek. Bez zmínky by souhrn nesouhlasil se seznamem ukončených;
        # porovnává se po provizích, stejně jako dlaždice sama
        z_bezicich_castka = s.realizovano_s_provizi - sum(
            (position.realized_pnl or 0.0) - position.commission_total
            for position in podklad.ukoncene
            if position.traded
        )
        z_bezicich = (
            f" · {penize(z_bezicich_castka)} z běžících"
            if abs(z_bezicich_castka) >= 0.005
            else ""
        )
        # Zaplacené provize stojí v popisku celkového výsledku na prvním místě.
        # Popisek se do dlaždice nemusí vejít celý a to, co se z něj ořízne
        # (realizováno, v pozicích), má stejně vlastní dlaždici vedle
        provize = f"provize {penize(-s.provize)} · " if s.provize else ""

        # Výsledek dne v procentech účtu. Bez známé velikosti účtu (z TWS
        # zatím nic nedorazilo) není z čeho počítat a dlaždice jen řekne proč.
        # Popisek rozpadá procenta na realizovaná a držená, ať je vidět,
        # kolik z nich ještě visí v trhu
        pct_celkem = s.procento_uctu(s.celkem_s_provizi)
        if pct_celkem is None:
            z_uctu = ("-", "", "velikost účtu není známa", "")
        else:
            v_pozicich = (
                f" · v pozicích {procenta(s.procento_uctu(s.otevreno_s_provizi))}"
                if s.otevrenych_pozic
                else ""
            )
            z_uctu = (
                procenta(pct_celkem),
                procenta_s_provizi(pct_celkem, s.procento_uctu(s.celkem)),
                f"účet {cislo_text(s.account_size, 0)} USD · realizováno "
                f"{procenta(s.procento_uctu(s.realizovano_s_provizi))}{v_pozicich}",
                trida_vysledku(pct_celkem),
            )

        return {
            "celkem": (
                penize(s.celkem_s_provizi),
                penize_s_provizi(s.celkem_s_provizi, s.celkem),
                f"{provize}realizováno {penize(s.realizovano_s_provizi)} · "
                f"v pozicích {penize(s.otevreno_s_provizi)}",
                trida_vysledku(s.celkem_s_provizi),
            ),
            "z_uctu": z_uctu,
            "realizovano": (
                penize(s.realizovano_s_provizi),
                penize_s_provizi(s.realizovano_s_provizi, s.realizovano),
                sklonuj(
                    s.uzavrenych_s_vysledkem,
                    "uzavřená pozice",
                    "uzavřené pozice",
                    "uzavřených pozic",
                )
                + z_bezicich,
                trida_vysledku(s.realizovano_s_provizi),
            ),
            "otevreno": (
                penize(s.otevreno_s_provizi) if s.otevrenych_pozic else "-",
                penize_s_provizi(s.otevreno_s_provizi, s.otevreno)
                if s.otevrenych_pozic
                else "",
                sklonuj(s.otevrenych_pozic, "pozice", "pozice", "pozic")
                + f" · {s.otevrenych_kusu} ks",
                trida_vysledku(s.otevreno_s_provizi) if s.otevrenych_pozic else "",
            ),
            "uspesnost": uspesnost,
            "factor": factor,
            "pozice": (
                str(s.bezicich + s.ukoncenych),
                "",
                f"{s.bezicich} běží · {s.ukoncenych} ukončeno{bez_nakupu}",
                "",
            ),
        }

    def _vykresli_dlazdice(self, podklad: report.DenniReport) -> None:
        """Přepíše hodnoty souhrnných dlaždic."""
        for klic, (hodnota, hrube, popis, trida) in self._hodnoty_dlazdic(podklad).items():
            prvky = self.dlazdice[klic]
            prvky["hodnota"].set_text(hodnota)
            prvky["hodnota"].classes(remove="zisk ztrata", add=trida)
            prvky["hrube"].set_text(hrube)
            prvky["popis"].set_text(popis)

    # ------------------------------------------------------------------
    # Seznam běžících pozic
    # ------------------------------------------------------------------

    def _hlavicka_seznamu(self, trida: str, popisky: tuple[str, ...]) -> None:
        """Řádek s názvy sloupců seznamu."""
        with ui.element("div").classes(f"report-radek report-hlavicka-seznamu {trida}"):
            for popisek in popisky:
                ui.label(popisek).classes("bunka bunka-popisek")

    def _radek_bezici(self, position: Position) -> dict[str, Any]:
        """
        Postaví řádek běžící pozice a vrátí prvky, do kterých se při obnově
        zapisují živé hodnoty. Ticker a směr se během pozice nemění, proto
        se vyplní rovnou.
        """
        prvky: dict[str, Any] = {}
        with ui.element("div").classes("report-radek report-radek-bezici"):
            with ui.element("div").classes("bunka bunka-ticker"):
                ui.label(position.symbol).classes("report-ticker")
                ui.label(position.right_label).classes(
                    f"odznak-smer {'smer-long' if position.right == 'C' else 'smer-short'}"
                )
            prvky["kontrakt"] = ui.label("").classes("bunka bunka-kontrakt")
            prvky["ks"] = ui.label("").classes("bunka bunka-cislo")
            prvky["nakup"] = ui.label("").classes("bunka bunka-cislo")
            prvky["ted"] = ui.label("").classes("bunka bunka-cislo")
            prvky["drzi"] = ui.label("").classes("bunka bunka-cislo")
            # P/L držené části: hlavní hodnota po provizích, vedle ní
            # drobněji tatáž částka bez nich
            with ui.element("div").classes("bunka bunka-pnl bunka-vysledek"):
                prvky["pnl"] = ui.label("")
                prvky["pnl_hrube"] = ui.label("").classes("hodnota-hrube")
            with ui.element("div").classes("bunka bunka-stav"):
                prvky["stav"] = ui.label("").classes("odznak")
        return prvky

    def _napln_bezici(self, prvky: dict[str, Any], position: Position) -> None:
        """Zapíše do řádku běžící pozice aktuální hodnoty."""
        prvky["kontrakt"].set_text(kontrakt_text(position))
        prvky["ks"].set_text(f"{position.open_quantity}/{position.quantity}")
        prvky["nakup"].set_text(penize(position.fill_price, znamenko=False))
        prvky["ted"].set_text(penize(position.option_bid, znamenko=False))
        prvky["drzi"].set_text(doba_drzeni(position.holding_seconds))

        # Držená pozice má zaplacenou jen nákupní provizi - prodejní vznikne
        # až prodejem, proto v jejím P/L ještě není
        pnl = position.open_pnl_net
        prvky["pnl"].set_text(penize(pnl))
        prvky["pnl"].classes(remove="zisk ztrata", add=trida_vysledku(pnl))
        prvky["pnl_hrube"].set_text(penize_s_provizi(pnl, position.unrealized_pnl))

        prvky["stav"].set_text(position.state.label)
        prvky["stav"].classes(remove=VSECHNY_STAVY, add=position.state.css_class)

    def _vykresli_bezici(self, podklad: report.DenniReport) -> None:
        """
        Vykreslí seznam běžících pozic. Řádky se staví znovu jen tehdy,
        když se změnilo složení seznamu; jinak se do nich jen zapisují hodnoty.
        """
        podpis = tuple(position.id for position in podklad.bezici)
        if podpis != self._podpis_bezici:
            self._podpis_bezici = podpis
            self._radky_bezici = {}
            self.telo_bezici.clear()
            with self.telo_bezici:
                if not podklad.bezici:
                    ui.label("Právě neběží žádná pozice.").classes("report-prazdno")
                else:
                    self._hlavicka_seznamu(
                        "report-radek-bezici",
                        ("Ticker", "Kontrakt", "Ks", "Nákup", "BID", "Drží", "P/L", "Stav"),
                    )
                    for position in podklad.bezici:
                        self._radky_bezici[position.id] = self._radek_bezici(position)

        for position in podklad.bezici:
            self._napln_bezici(self._radky_bezici[position.id], position)

        souhrn = podklad.souhrn
        self.podnadpis_bezici.set_text(
            sklonuj(
                souhrn.otevrenych_pozic,
                "otevřená pozice",
                "otevřené pozice",
                "otevřených pozic",
            )
            + f" · {penize(souhrn.otevreno_s_provizi)} USD"
            if souhrn.otevrenych_pozic
            else "žádná otevřená pozice"
        )

    # ------------------------------------------------------------------
    # Seznam ukončených pozic
    # ------------------------------------------------------------------

    def _radek_uzavreny(self, position: Position, meritko: float) -> None:
        """
        Řádek ukončené pozice. Hodnoty se už nemění, proto se zapisují rovnou;
        měřítko je největší výsledek dne v absolutní hodnotě a určuje délku
        pruhu, kterým se pozice porovnávají mezi sebou.
        """
        # Ukončená pozice už zaplatila obě strany provize, výsledek je proto
        # celý realizovaný a hodnota v závorce ukazuje, kolik z něj provize vzaly
        vysledek = position.realized_pnl_net
        with ui.element("div").classes("report-radek report-radek-uzavreny"):
            ui.label(f"{position.updated_at:%H:%M}").classes("bunka bunka-cas")
            with ui.element("div").classes("bunka bunka-ticker"):
                ui.label(position.symbol).classes("report-ticker")
                ui.label(position.right_label).classes(
                    f"odznak-smer {'smer-long' if position.right == 'C' else 'smer-short'}"
                )
            ui.label(kontrakt_text(position)).classes("bunka bunka-kontrakt")
            # Píše se skutečně nakoupené množství; u zrušeného příkazu tedy
            # nula, ne počet kusů, které se jen zadávaly. Drží-li pozice i po
            # ukončení kontrakty (typicky po chybě), přibude před lomítkem
            # zbytek v pozici - stejně jako u běžících řádků
            ks = (
                f"{position.open_quantity}/{position.filled_quantity}"
                if position.open_quantity
                else str(position.filled_quantity)
            )
            ui.label(ks).classes("bunka bunka-cislo")
            # Nákupní a průměrná prodejní cena opce vedle sebe; bez nákupu pomlčka
            if position.fill_price is None:
                obchod = "-"
            else:
                obchod = (
                    f"{penize(position.fill_price, znamenko=False)} → "
                    f"{penize(position.avg_sell_price, znamenko=False)}"
                )
            ui.label(obchod).classes("bunka bunka-obchod")
            ui.label(doba_drzeni(position.holding_seconds)).classes("bunka bunka-cislo")
            ui.label(position.state.label).classes("bunka bunka-duvod")
            with ui.element("div").classes(
                f"bunka bunka-pnl bunka-vysledek {trida_vysledku(vysledek)}"
            ):
                ui.label(penize(vysledek))
                ui.label(penize_s_provizi(vysledek, position.realized_pnl)).classes(
                    "hodnota-hrube"
                )

            # Rozvážený pruh: ztráta roste doleva od středu, zisk doprava.
            # Kreslí se vlastními prvky, ne komponentou Quasaru - ta zaobluje
            # jen svůj obal, takže vnitřní konec pruhu zůstával hranatý
            with ui.element("div").classes("bunka bunka-pomer"):
                podil = abs(vysledek or 0.0) / meritko if meritko > 0 else 0.0
                with ui.element("div").classes("pomer-pulka"):
                    pomer_pruh(podil if (vysledek or 0) < 0 else 0.0, "pomer-pruh-ztrata")
                with ui.element("div").classes("pomer-pulka"):
                    pomer_pruh(podil if (vysledek or 0) > 0 else 0.0, "pomer-pruh-zisk")

    def _vykresli_uzavrene(self, podklad: report.DenniReport) -> None:
        """Vykreslí seznam ukončených pozic; mění se jen s novým výsledkem."""
        podpis = tuple(
            (position.id, position.state.value, round(position.realized_pnl_net or 0.0, 2))
            for position in podklad.ukoncene
        )
        if podpis != self._podpis_uzavrene:
            self._podpis_uzavrene = podpis
            self.telo_uzavrene.clear()
            # Měřítko pruhů je největší výsledek dne v absolutní hodnotě
            meritko = max(
                (abs(p.realized_pnl_net or 0.0) for p in podklad.ukoncene),
                default=0.0,
            )
            with self.telo_uzavrene:
                if not podklad.ukoncene:
                    ui.label("Zatím není ukončená žádná pozice.").classes("report-prazdno")
                else:
                    self._hlavicka_seznamu(
                        "report-radek-uzavreny",
                        (
                            "Čas",
                            "Ticker",
                            "Kontrakt",
                            "Ks",
                            "Nákup → prodej",
                            "Držení",
                            "Stav",
                            "Výsledek",
                            "Porovnání",
                        ),
                    )
                    for position in podklad.ukoncene:
                        self._radek_uzavreny(position, meritko)

        # Extrémy dne stojí v hlavičce panelu - rychlý pohled na to,
        # co den nejvíc vytáhlo nahoru a co dolů
        s = podklad.souhrn
        casti = []
        if s.nejlepsi:
            casti.append(f"nejlepší {s.nejlepsi[0]} {penize(s.nejlepsi[1])}")
        if s.nejhorsi and s.nejhorsi != s.nejlepsi:
            casti.append(f"nejhorší {s.nejhorsi[0]} {penize(s.nejhorsi[1])}")
        self.podnadpis_uzavrene.set_text(" · ".join(casti))

    # ------------------------------------------------------------------
    # Grafy
    # ------------------------------------------------------------------

    def _barvy(self) -> dict[str, str]:
        """Barvy grafů podle zvoleného vzhledu - grafy kreslí canvas, ne CSS."""
        if self.je_tmavy():
            return {
                "text": "#94a3b8",
                "mrizka": "#334155",
                "osa": "#475569",
                "zisk": "#4ade80",
                "ztrata": "#f87171",
                "zisk_slabe": "rgba(74, 222, 128, 0.25)",
                "ztrata_slabe": "rgba(248, 113, 113, 0.25)",
            }
        return {
            "text": "#475569",
            "mrizka": "#e2e8f0",
            "osa": "#cbd5e1",
            "zisk": "#16a34a",
            "ztrata": "#dc2626",
            "zisk_slabe": "rgba(22, 163, 74, 0.20)",
            "ztrata_slabe": "rgba(220, 38, 38, 0.20)",
        }

    def _prazdny_graf(self) -> dict[str, Any]:
        """Výchozí (prázdná) konfigurace grafu, než dorazí data."""
        return {"series": []}

    def _osy(self, kategorie: list[str], barvy: dict[str, str]) -> dict[str, Any]:
        """Společné nastavení os obou grafů - popisky, mřížka a formát čísel."""
        return {
            "xAxis": {
                "type": "category",
                "data": kategorie,
                "axisLabel": {"color": barvy["text"], "fontSize": 10},
                "axisLine": {"lineStyle": {"color": barvy["osa"]}},
                "axisTick": {"show": False},
            },
            "yAxis": {
                "type": "value",
                "axisLabel": {"color": barvy["text"], "fontSize": 10},
                "splitLine": {"lineStyle": {"color": barvy["mrizka"]}},
            },
        }

    def _vykresli_krivku(self, podklad: report.DenniReport) -> None:
        """
        Křivka kumulovaného realizovaného výsledku po provizích - jak se den
        vyvíjel. Začíná v nule, každý další bod patří jedné ukončené pozici.
        """
        # Prázdný graf by ukazoval jen holé osy - místo něj se zobrazí hláška
        self.graf_krivka.set_visibility(bool(podklad.krivka))
        self.prazdno_krivka.set_visibility(not podklad.krivka)

        podpis = (self.je_tmavy(),) + tuple(
            round(hodnota, 2) for _, hodnota in podklad.krivka
        )
        if podpis == self._podpis_krivka:
            return
        self._podpis_krivka = podpis

        barvy = self._barvy()
        kategorie = ["start"] + [f"{cas:%H:%M}" for cas, _ in podklad.krivka]
        hodnoty = [0.0] + [round(hodnota, 2) for _, hodnota in podklad.krivka]
        # Barva křivky se řídí tím, jak den skončil
        kladny = hodnoty[-1] >= 0
        barva = barvy["zisk"] if kladny else barvy["ztrata"]
        vypln = barvy["zisk_slabe"] if kladny else barvy["ztrata_slabe"]

        moznosti: dict[str, Any] = {
            "grid": {"left": 58, "right": 18, "top": 18, "bottom": 26},
            "tooltip": {"trigger": "axis"},
            **self._osy(kategorie, barvy),
            "series": [
                {
                    "type": "line",
                    "data": hodnoty,
                    "smooth": True,
                    # Bez monotónního prokládání by křivka mezi body přestřelovala
                    # a den by chvílemi vypadal ztrátověji, než ve skutečnosti byl
                    "smoothMonotone": "x",
                    "symbolSize": 7,
                    "lineStyle": {"width": 2.5, "color": barva},
                    "itemStyle": {"color": barva},
                    "areaStyle": {
                        "color": {
                            "type": "linear",
                            "x": 0,
                            "y": 0,
                            "x2": 0,
                            "y2": 1,
                            "colorStops": [
                                {"offset": 0, "color": vypln},
                                {"offset": 1, "color": "rgba(0, 0, 0, 0)"},
                            ],
                        }
                    },
                    # Nulová osa odděluje ziskový den od ztrátového
                    "markLine": {
                        "silent": True,
                        "symbol": "none",
                        "label": {"show": False},
                        "lineStyle": {"color": barvy["osa"], "type": "dashed"},
                        "data": [{"yAxis": 0}],
                    },
                }
            ],
        }
        self.graf_krivka.options.clear()
        self.graf_krivka.options.update(moznosti)
        self.graf_krivka.update()

        self.podnadpis_krivka.set_text(
            "kumulovaný realizovaný výsledek po provizích · "
            + sklonuj(len(podklad.krivka), "pozice", "pozice", "pozic")
            if podklad.krivka
            else "zatím není co vykreslit"
        )

    def _vykresli_tickery(self, podklad: report.DenniReport) -> None:
        """
        Sloupcový graf výsledku po tickerech - realizovaný i otevřený,
        po odečtení provizí zaplacených pozicemi daného tickeru.
        """
        self.graf_tickery.set_visibility(bool(podklad.podle_tickeru))
        self.prazdno_tickery.set_visibility(not podklad.podle_tickeru)

        # Zaokrouhluje se na stejný počet míst, jaký se vypisuje v popisku
        # sloupce - jinak by popisek u drobných pohybů zůstal viset na starém
        podpis = (self.je_tmavy(),) + tuple(
            (polozka.symbol, round(polozka.celkem_s_provizi, 2))
            for polozka in podklad.podle_tickeru
        )
        if podpis == self._podpis_tickery:
            return
        self._podpis_tickery = podpis

        barvy = self._barvy()
        kategorie = [polozka.symbol for polozka in podklad.podle_tickeru]
        # Každý sloupec si nese vlastní barvu i umístění popisku: zisk nahoru
        # od osy, ztráta dolů, popisek vždy na vnější straně sloupce
        data = []
        for polozka in podklad.podle_tickeru:
            hodnota = round(polozka.celkem_s_provizi, 2)
            kladny = hodnota >= 0
            data.append(
                {
                    "value": hodnota,
                    "itemStyle": {
                        "color": barvy["zisk"] if kladny else barvy["ztrata"],
                        "borderRadius": [3, 3, 0, 0] if kladny else [0, 0, 3, 3],
                    },
                    "label": {"position": "top" if kladny else "bottom"},
                }
            )

        # Popisky sloupců stojí vně sloupce, proto osa potřebuje rezervu -
        # jinak by se nejnižší hodnota překrývala s názvem tickeru pod grafem
        hodnoty = [
            polozka.celkem_s_provizi for polozka in podklad.podle_tickeru
        ] or [0.0]
        osy = self._osy(kategorie, barvy)
        osy["yAxis"]["min"], osy["yAxis"]["max"] = mez_osy(hodnoty)

        moznosti: dict[str, Any] = {
            "grid": {"left": 58, "right": 18, "top": 24, "bottom": 26},
            "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
            **osy,
            "series": [
                {
                    "type": "bar",
                    "data": data,
                    "barMaxWidth": 42,
                    "label": {
                        "show": True,
                        "color": barvy["text"],
                        "fontSize": 10,
                        "fontWeight": "bold",
                    },
                }
            ],
        }
        self.graf_tickery.options.clear()
        self.graf_tickery.options.update(moznosti)
        self.graf_tickery.update()

        self.podnadpis_tickery.set_text(
            sklonuj(len(podklad.podle_tickeru), "ticker", "tickery", "tickerů")
            + " · výsledek po provizích"
            if podklad.podle_tickeru
            else "zatím není co vykreslit"
        )
