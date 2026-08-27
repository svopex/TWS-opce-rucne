"""Webové rozhraní aplikace postavené na NiceGUI - zadání nákupu a přehled pozic."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from nicegui import app, ui

from . import calc
from .config import AppConfig
from .engine import ManualEngine, Preview
from .ib_service import IBService
from .report_dialog import ReportDialog
from .models import (
    ASK_MARKUPS,
    SELL_SCOPE_ALL,
    SELL_SCOPE_BASE,
    SELL_SCOPE_ONE,
    Position,
    buy_button_label,
    pnl_text,
    price_kind_label,
    sell_button_label,
    ukoncene_pozice_text,
    zbytek_text,
)

log = logging.getLogger(__name__)


def format_countdown(sekundy: float) -> str:
    """
    Zbývající čas odpočtu v hlavičce. Pod hodinu vyjde MM:SS, do dne
    H:MM:SS a přes den se přidá počet dní - odpočet do otevření trhu běží
    i přes víkend, takže může jít o desítky hodin.
    """
    celkem = max(0, int(sekundy))
    dny, zbytek = divmod(celkem, 86400)
    hodiny, zbytek = divmod(zbytek, 3600)
    minuty, sek = divmod(zbytek, 60)
    if dny:
        return f"{dny} d {hodiny}:{minuty:02d}:{sek:02d}"
    if hodiny:
        return f"{hodiny}:{minuty:02d}:{sek:02d}"
    return f"{minuty:02d}:{sek:02d}"


STATIC_DIR = Path(__file__).parent / "static"

# Kolik událostí se nejvýš vypisuje v panelu průběhu
LOG_ZOBRAZENO = 40


def staticky_soubor(nazev: str) -> str:
    """
    URL statického souboru doplněná o čas jeho poslední úpravy.
    Prohlížeč tak po změně načte novou verzi místo té z keše.
    """
    soubor = STATIC_DIR / nazev
    stamp = int(soubor.stat().st_mtime) if soubor.exists() else 0
    return f"/static/{nazev}?v={stamp}"


def fmt(value: float | None, digits: int = 2, suffix: str = "") -> str:
    """Naformátuje číslo pro zobrazení, při chybějící hodnotě vrátí pomlčku."""
    if value is None:
        return "-"
    return f"{value:,.{digits}f}{suffix}".replace(",", " ")


def dnu_text(pocet: int) -> str:
    """Počet dní do expirace se správným tvarem slova (1 den, 3 dny, 8 dní)."""
    if pocet == 1:
        return "1 den"
    if 2 <= pocet <= 4:
        return f"{pocet} dny"
    return f"{pocet} dní"


def pnl_class(hodnota: float | None) -> str:
    """CSS třída pro barvu výsledku - zisk zeleně, ztráta červeně."""
    if hodnota is None or abs(hodnota) < 0.005:
        return "vysledek-nula"
    return "vysledek-zisk" if hodnota > 0 else "vysledek-ztrata"


class PositionCard:
    """
    Karta jedné pozice v přehledu.

    Prvky se zakládají jednou a při každém překreslení se jim jen mění text
    a viditelnost - tabulka se tak nepřestavuje a kliknutí nepřicházejí vniveč.
    """

    def __init__(self, parent: "TradingUI", position: Position) -> None:
        self.parent = parent
        self.position_id = position.id
        # Stav, ve kterém byla tlačítka naposledy vykreslena. Obsluha kliknutí
        # z něj pozná, zda šlo o přecenění příkazu v trhu, nebo o nový příkaz;
        # když se stav mezitím změní, engine akci odmítne a k druhému
        # nákupu ani prodeji nedojde.
        self.sell_reprice = False
        self._build(position)

    def _build(self, position: Position) -> None:
        """Vykreslí kartu s údaji pozice a tlačítky pro prodej."""
        self.karta = ui.card().classes("karta karta-pozice")
        with self.karta:
            with ui.row().classes("radek-pozice-hlavicka"):
                self.kontrakt = ui.label().classes("popis-kontraktu")
                self.smer = ui.label().classes("odznak-smer")
                ui.space()
                self.stav = ui.label().classes("odznak")

            with ui.row().classes("radek-pozice-udaje"):
                self.drzeno = ui.label().classes("udaj")
                self.nakup = ui.label().classes("udaj")
                self.kotace = ui.label().classes("udaj")
                self.podklad = ui.label().classes("udaj")
                self.vysledek = ui.label().classes("udaj udaj-vysledek")
                with self.vysledek:
                    self.tip_vysledek = ui.tooltip("")

            self.hlaska = ui.label().classes("hlaska-pozice")

            # Tlačítka stojí ve dvojicích pod sebou: nejdřív nákup, pak prodej
            # celé pozice a teprve pod ním prodej se zachováním runneru.
            # Ceny BID a MID tak leží vedle sebe a rozsahy nad sebou.
            with ui.row().classes("radek-pozice-tlacitka") as self.radek_nakup:
                # Přecenění nevyplněného nákupu na aktuální cenu
                self.btn_nakup_ask = ui.button(
                    on_click=lambda: self.parent.reprice_buy(self.position_id, "ask")
                ).props("dense color=primary")
                with self.btn_nakup_ask:
                    self.tip_nakup_ask = ui.tooltip("")
                self.btn_nakup_mid = ui.button(
                    on_click=lambda: self.parent.reprice_buy(self.position_id, "mid")
                ).props("dense outline color=primary")
                with self.btn_nakup_mid:
                    self.tip_nakup_mid = ui.tooltip("")

            # Oba řádky prodeje stojí v mřížce se stejně širokými sloupci,
            # takže tlačítka stejného druhu leží přesně nad sebou. Proto je to
            # obyčejný div, ne ui.row() - ten si nese vlastní flex rozvržení.
            self.radek_prodej_vse = ui.element("div").classes("radek-prodej")
            with self.radek_prodej_vse:
                self.btn_vse = self._sell_buttons(SELL_SCOPE_ALL, "red-8")

            # Prodej základní pozice - v trhu zůstane runner
            self.radek_prodej_zaklad = ui.element("div").classes("radek-prodej")
            with self.radek_prodej_zaklad:
                self.btn_zaklad = self._sell_buttons(SELL_SCOPE_BASE, "orange-8")

            # Prodej jediného kontraktu - pro odprodávání pozice po kusech
            self.radek_prodej_kus = ui.element("div").classes("radek-prodej")
            with self.radek_prodej_kus:
                self.btn_kus = self._sell_buttons(SELL_SCOPE_ONE, "teal-7")

            # Správa pozice - stažení příkazu z trhu a úklid přehledu
            with ui.row().classes("radek-pozice-tlacitka") as self.radek_sprava:
                self.btn_zrusit = ui.button(
                    "Zrušit příkaz v trhu",
                    on_click=lambda: self.parent.cancel_order(self.position_id),
                ).props("dense outline color=grey-7")
                self.btn_odstranit = ui.button(
                    "Odstranit z přehledu",
                    on_click=lambda: self.parent.remove_position(self.position_id),
                ).props("dense outline color=grey-7")

    def _sell_buttons(self, scope: str, barva: str) -> list[tuple[Any, Any, str, float]]:
        """
        Vykreslí řádek prodejních tlačítek pro daný rozsah pozice.

        Nabídka jde od nejjistějšího vyplnění k nejvyšší ceně: BID, MID, ASK
        a nad ním přirážky ASK +1 % až +5 %. Čím výš, tím víc za kontrakt
        přijde, ale tím menší je šance, že se příkaz vyplní. Vrací čtveřice
        (tlačítko, nápověda, druh ceny, přirážka), ze kterých se při každém
        překreslení obnovují popisky i ceny.
        """
        tlacitka: list[tuple[Any, Any, str, float]] = []
        varianty = [("bid", 0.0), ("mid", 0.0), ("ask", 0.0)]
        varianty += [("ask", p) for p in ASK_MARKUPS]

        for kind, markup in varianty:
            # Plnou barvou je jen první tlačítko, ostatní jsou obtažená -
            # jinak by řádek se sedmi barevnými plochami nešel přečíst
            vzhled = "dense" if (kind, markup) == ("bid", 0.0) else "dense outline"
            # Hodnoty se do obsluhy předávají výchozími argumenty, jinak by si
            # všechna tlačítka pamatovala poslední průchod cyklem
            tlacitko = ui.button(
                on_click=lambda k=kind, m=markup: self.parent.sell(
                    self.position_id, k, scope, self.sell_reprice, m
                )
            ).props(f"{vzhled} color={barva}")
            with tlacitko:
                napoveda = ui.tooltip("")
            tlacitka.append((tlacitko, napoveda, kind, markup))

        return tlacitka

    def update(self, position: Position) -> None:
        """Promítne do karty aktuální stav pozice."""
        self.kontrakt.set_text(position.contract_label)
        self.smer.set_text(position.right_label)
        self.smer.classes(replace=f"odznak-smer smer-{'long' if position.right == 'C' else 'short'}")
        self.stav.set_text(position.state.label)
        self.stav.classes(replace=f"odznak {position.state.css_class}")

        # Drženo / zadáno; po odprodeji základní části se doplní poznámka o runneru
        popis_drzeno = f"Drženo: {position.open_quantity} z {position.quantity} ks"
        if position.is_runner_only:
            popis_drzeno += " (runner)"
        self.drzeno.set_text(popis_drzeno)
        self.nakup.set_text(f"Nákup: {fmt(position.fill_price)}")
        self.kotace.set_text(
            f"BID/ASK: {fmt(position.option_bid)} / {fmt(position.option_ask)}"
            f" (spread {fmt(position.spread_pct, 1, ' %')})"
        )
        self.podklad.set_text(f"Podklad: {fmt(position.underlying_price)}")

        # U pozice se zbytkem po částečném prodeji se ukazuje nejdřív výsledek
        # drženého zbytku a v závorce celek; barvu určuje to první číslo
        self.vysledek.set_text(
            f"P/L: {pnl_text(position.unrealized_pnl, position.net_pnl, position.pnl_split)}"
        )
        vysledek = position.unrealized_pnl if position.pnl_split else position.net_pnl
        self.vysledek.classes(replace=f"udaj udaj-vysledek {pnl_class(vysledek)}")
        if position.pnl_split:
            self.tip_vysledek.set_text(
                f"Výsledek drženého zbytku ({position.open_quantity} ks); v závorce "
                f"celá pozice včetně {position.sold_quantity} už prodaných ks a provizí."
            )
        else:
            self.tip_vysledek.set_text(
                "Výsledek pozice po provizích; držené kontrakty jsou oceněné "
                "středem trhu."
            )

        self.hlaska.set_text(position.message)
        self.hlaska.set_visibility(bool(position.message))

        self._update_buttons(position)

    def _update_buttons(self, position: Position) -> None:
        """
        Nastaví popisky i dostupnost tlačítek podle stavu pozice.

        S příkazem v trhu tlačítka nemizí, jen změní popisek na přecenění -
        trh mohl limitnímu příkazu utéct a opakovaný stisk jej posune
        na aktuální cenu, místo aby zakládal druhý příkaz.
        """
        engine = self.parent.engine
        self.sell_reprice = position.sell_pending

        # Nevyplněný nákup lze přecenit; jinak nákupní tlačítka na kartě nejsou
        for tlacitko, napoveda, kind in (
            (self.btn_nakup_ask, self.tip_nakup_ask, "ask"),
            (self.btn_nakup_mid, self.tip_nakup_mid, "mid"),
        ):
            tlacitko.set_visibility(position.can_reprice_buy)
            if not position.can_reprice_buy:
                continue
            limit = engine.position_buy_limit(position, kind)
            popisek = buy_button_label(kind, position.quantity)
            tlacitko.set_text(popisek if limit is None else f"{popisek} · {fmt(limit)}")
            tlacitko.set_enabled(limit is not None)
            napoveda.set_text(
                f"Přecení nevyplněný nákupní příkaz na aktuální "
                f"{price_kind_label(kind)}; druhý příkaz nevzniká."
            )

        for seznam, scope, mnozstvi, dostupne in (
            (self.btn_vse, SELL_SCOPE_ALL, position.open_quantity, position.can_sell_all),
            (self.btn_zaklad, SELL_SCOPE_BASE, position.base_quantity, position.can_sell_base),
            (self.btn_kus, SELL_SCOPE_ONE, 1, position.can_sell_one),
        ):
            for tlacitko, napoveda, kind, markup in seznam:
                tlacitko.set_visibility(dostupne)
                if not dostupne:
                    continue
                limit = engine.position_sell_limit(position, kind, markup)
                popisek = sell_button_label(kind, mnozstvi, markup)
                # Bez kotace se cena zobrazit nedá a příkaz by stejně neprošel
                tlacitko.set_text(popisek if limit is None else f"{popisek} · {fmt(limit)}")
                tlacitko.set_enabled(limit is not None)

                # Popisek je kvůli délce úsporný, nápověda proto říká, co
                # tlačítko udělá a co v pozici zbude
                popis_zbytku = zbytek_text(position, scope, mnozstvi)
                zbytek = f"; {popis_zbytku}." if popis_zbytku else "."
                napoveda.set_text(
                    f"{'Přecení příkaz v trhu na' if position.sell_pending else 'Prodá'} "
                    f"{mnozstvi} ks za {price_kind_label(kind, markup)}{zbytek}"
                )

        self.btn_zrusit.set_visibility(position.can_cancel)
        self.btn_odstranit.set_visibility(position.can_remove)

        # Řádek bez jediného tlačítka by po sobě nechal prázdnou mezeru
        self.radek_nakup.set_visibility(position.can_reprice_buy)
        self.radek_prodej_vse.set_visibility(position.can_sell_all)
        self.radek_prodej_zaklad.set_visibility(position.can_sell_base)
        self.radek_prodej_kus.set_visibility(position.can_sell_one)
        self.radek_sprava.set_visibility(position.can_cancel or position.can_remove)

    def remove(self) -> None:
        """Odstraní kartu ze stránky."""
        self.karta.delete()


class TradingUI:
    """Sestavuje a obsluhuje uživatelské rozhraní nad obchodním enginem."""

    def __init__(self, cfg: AppConfig, engine: ManualEngine, ib: IBService) -> None:
        self.cfg = cfg
        self.engine = engine
        self.ib = ib

    # ------------------------------------------------------------------
    # Sestavení stránky
    # ------------------------------------------------------------------

    def build(self) -> None:
        """Vykreslí celou stránku - hlavičku, formulář, přehled pozic a průběh."""
        ui.add_head_html(f'<link rel="stylesheet" href="{staticky_soubor("styles.css")}">')

        # Pořadové číslo přípravy zadání - rozlišuje souběžně běžící požadavky
        self.preview_seq: int = 0
        # Pozice s nevyplněným nákupním příkazem, kterou formulář naposledy
        # vykreslil. Opakovaný stisk nákupu pak příkaz přecení místo toho,
        # aby zakládal druhou pozici.
        self.pending_buy_id: str = ""
        # Zámek proti překlikání: dokud se příkaz odesílá, další stisk se
        # zahodí, aby z dvojkliku nevznikly dva příkazy
        self._akce_bezi: bool = False
        # Karty pozic podle identifikátoru
        self.cards: dict[str, PositionCard] = {}
        # Čas poslední vypsané události - průběh se překresluje jen při změně
        self.last_log_stamp: datetime | None = None
        # Naposledy zobrazené pozice bez dozoru; None znamená, že pruh ještě
        # nebyl vykreslen - prázdná množina je platný stav a nesmí se zaměnit
        self.last_unmanaged: set[int] | None = None

        self._build_header()

        # Popup s přehledem výsledků dne; grafy v něm se řídí zvoleným
        # vzhledem, proto dostane přístup k přepínači z hlavičky
        self.report_dialog = ReportDialog(
            self.engine, lambda: bool(self.dark_mode.value), self._toggle_dark
        )
        self.report_dialog.build()

        # Pruh s upozorněním na opční pozice, které aplikace neřídí
        self.warning_bar = ui.row().classes("pruh-varovani")
        self.warning_bar.set_visibility(False)

        with ui.row().classes("obsah"):
            with ui.column().classes("panel-formular"):
                self._build_form()
                self._build_config()
            with ui.column().classes("panel-prehled"):
                self._build_positions()
                self._build_log()

        self._refresh()
        ui.timer(self.cfg.ui.refresh_interval_sec, self._refresh)

    def _build_header(self) -> None:
        """Hlavička s názvem aplikace, přepínačem vzhledu a stavem spojení na TWS."""
        # Tmavý režim: výchozí hodnota z konfigurace, poslední volba
        # obchodníka se pamatuje mezi spuštěními
        self.dark_mode = ui.dark_mode(bool(app.storage.general.get("dark_mode", self.cfg.ui.dark)))
        with ui.header().classes("hlavicka"):
            ui.label("Ruční obchodování opcí – TWS").classes("nazev")
            ui.space()
            # Odpočet do otevření burzy - během seance se skrývá
            self.market_open_label = ui.label().classes("odpocet-otevreni")
            self.market_open_label.set_visibility(False)
            self.dark_button = ui.button(on_click=self._toggle_dark).props("flat round dense")
            with self.dark_button:
                ui.tooltip("Přepnout světlý/tmavý vzhled")
            self._refresh_dark_button()
            self.status_label = ui.label().classes("stav-spojeni")
            self.connect_button = ui.button("Připojit", on_click=self._toggle_connection).props(
                "flat"
            )

    def _toggle_dark(self) -> None:
        """Přepne světlý/tmavý vzhled a volbu si zapamatuje."""
        self.dark_mode.value = not self.dark_mode.value
        app.storage.general["dark_mode"] = self.dark_mode.value
        self._refresh_dark_button()

    def _refresh_dark_button(self) -> None:
        """Ikona přepínače ukazuje režim, do kterého se lze přepnout."""
        ikona = "light_mode" if self.dark_mode.value else "dark_mode"
        self.dark_button.props(f"icon={ikona}")

    def _build_form(self) -> None:
        """Formulář pro zadání nákupu - ticker, množství a typ opce."""
        trading = self.cfg.trading
        with ui.card().classes("karta"):
            ui.label("Nákup opce").classes("nadpis-sekce")

            with ui.row().classes("radek-formular"):
                self.ticker_input = (
                    ui.input("Ticker")
                    .props("outlined dense autofocus")
                    .classes("pole-ticker")
                )
                # Kontrakt se hledá po opuštění pole a po potvrzení klávesou Enter,
                # aby se do TWS neposílal dotaz po každém napsaném znaku
                self.ticker_input.on("blur", lambda _: self._naplanuj_nahled())
                self.ticker_input.on("keydown.enter", lambda _: self._naplanuj_nahled())

                self.quantity_input = (
                    ui.number(
                        "Množství (ks)",
                        value=trading.default_quantity,
                        min=trading.min_quantity,
                        max=trading.max_quantity,
                        step=1,
                        format="%.0f",
                        on_change=lambda _: self._refresh_buy_buttons(),
                    )
                    .props("outlined dense")
                    .classes("pole-mnozstvi")
                )

            # Směr obchodu volí obchodník - program jej z ničeho neodvozuje
            self.right_toggle = ui.toggle(
                {"C": "CALL", "P": "PUT"},
                value=trading.default_right,
                on_change=lambda _: self._naplanuj_nahled(),
            ).props("dense")

            ui.separator()

            # Náhled vybraného kontraktu
            with ui.column().classes("nahled"):
                self.nahled_kontrakt = ui.label("Zadejte ticker.").classes("nahled-kontrakt")
                self.nahled_podklad = ui.label().classes("nahled-radek")
                self.nahled_kotace = ui.label().classes("nahled-radek")
                self.nahled_naklady = ui.label().classes("nahled-radek")
            self.nahled_varovani = ui.column().classes("nahled-varovani")
            self.loading_label = ui.label("Načítám data z TWS…").classes("nahled-nacitani")
            self.loading_label.set_visibility(False)

            # Nápověda se mění podle toho, zda tlačítko zadává nový příkaz,
            # nebo přeceňuje ten, který v trhu už čeká
            with ui.row().classes("radek-tlacitka-nakup"):
                self.btn_ask = ui.button(on_click=lambda: self.buy("ask")).props(
                    "dense color=primary"
                )
                with self.btn_ask:
                    self.tip_ask = ui.tooltip("")
                self.btn_mid = ui.button(on_click=lambda: self.buy("mid")).props(
                    "dense outline color=primary"
                )
                with self.btn_mid:
                    self.tip_mid = ui.tooltip("")

    def _build_config(self) -> None:
        """Přehled podstatných hodnot z konfiguračního souboru."""
        with ui.card().classes("karta karta-config"):
            ui.label("Nastavení").classes("nadpis-sekce")
            self.config_label = ui.label().classes("config-text")
            self._refresh_config()

    def _build_positions(self) -> None:
        """Panel s kartami otevřených i ukončených pozic."""
        with ui.card().classes("karta karta-prehled"):
            with ui.row().classes("radek-nadpis-prehled"):
                ui.label("Pozice").classes("nadpis-sekce")
                ui.space()
                # Souhrn obchodního dne v popupu - dlaždice, seznamy a grafy
                ui.button("Výsledky", on_click=self.report_dialog.open).props(
                    "dense outline color=primary"
                ).classes("tlacitko-vysledky").tooltip(
                    "Přehled výsledků dne: souhrn, seznam pozic a grafy."
                )
                # Úklid obrazovky - ukončené pozice už není co hlídat
                self.btn_uklid = ui.button(
                    "Odstranit ukončené", on_click=self.remove_finished
                ).props("dense outline color=grey-7")
                with self.btn_uklid:
                    ui.tooltip(
                        "Vyklidí z přehledu uzavřené, zrušené i chybové pozice. "
                        "Otevřených se nedotkne a do TWS neposílá nic."
                    )
            self.prazdny_prehled = ui.label("Zatím žádná pozice.").classes("prazdny-prehled")
            self.positions_container = ui.column().classes("seznam-pozic")

    def _build_log(self) -> None:
        """Panel s provozním průběhem aplikace."""
        with ui.card().classes("karta karta-log"):
            ui.label("Průběh").classes("nadpis-sekce")
            self.log_area = ui.column().classes("log-obsah")

    # ------------------------------------------------------------------
    # Obsluha akcí
    # ------------------------------------------------------------------

    async def _toggle_connection(self) -> None:
        """Připojí nebo odpojí aplikaci od TWS."""
        try:
            if self.ib.connected:
                # Ruční odpojení vypne i automatické obnovování spojení ve smyčce
                self.engine.auto_connect = False
                await self.ib.disconnect()
                ui.notify("Spojení s TWS ukončeno.", type="warning")
            else:
                await self.ib.connect()
                self.engine.auto_connect = True
                # Po ručním připojení se dohledají pozice z předchozího běhu
                await self.engine.restore()
                ui.notify("Spojení s TWS navázáno.", type="positive")
        except Exception as exc:
            ui.notify(f"Spojení se nezdařilo: {exc}", type="negative")
        self._refresh()

    def _naplanuj_nahled(self) -> None:
        """
        Spustí přípravu zadání až po doběhnutí právě probíhající obsluhy.

        Úloha založená přes background_tasks běží bez kontextu prvku a
        ui.notify v ní končí chybou "current slot cannot be determined";
        časovač vytvořený v kontextu formuláře jej naopak má.
        """
        with self.ticker_input:
            ui.timer(0, self._load_preview, once=True)

    async def _load_preview(self) -> None:
        """Připraví zadání podle formuláře - vybere kontrakt a načte jeho kotace."""
        symbol = (self.ticker_input.value or "").upper().strip()
        self.ticker_input.value = symbol

        if not symbol:
            self.engine.release_preview()
            self._apply_preview(None)
            return

        if not self.ib.connected:
            self._apply_preview(None, "Bez spojení s TWS nelze kontrakt vybrat.")
            return

        # Souběžně spuštěné přípravy se rozlišují pořadovým číslem - do rozhraní
        # se zapíše jen výsledek té poslední
        self.preview_seq += 1
        poradi = self.preview_seq
        self._set_loading(True)
        try:
            preview = await self.engine.prepare(
                symbol, self._quantity(), self.right_toggle.value
            )
        except Exception as exc:
            if poradi == self.preview_seq:
                self._set_loading(False)
                self._apply_preview(None, str(exc))
                ui.notify(f"Zadání se nepodařilo připravit: {exc}", type="negative")
            return

        if poradi != self.preview_seq:
            return
        self._set_loading(False)
        self._apply_preview(preview)

    def _zamek(self) -> bool:
        """
        Uzamkne rozhraní na dobu odesílání příkazu.

        Vrací False, pokud už jiný příkaz odchází - druhý stisk se zahodí,
        aby z dvojkliku nevznikly dva příkazy v trhu.
        """
        if self._akce_bezi:
            ui.notify("Předchozí příkaz se ještě odesílá do TWS.", type="warning")
            return False
        self._akce_bezi = True
        return True

    async def buy(self, kind: str) -> None:
        """
        Zadá nákupní příkaz podle stisknutého tlačítka (ASK nebo MID).

        Čeká-li na tickeru nevyplněný nákup, stisk jej jen přecení na
        aktuální cenu; druhá pozice tak z opakovaného stisku nevznikne.
        """
        symbol = (self.ticker_input.value or "").upper().strip()
        if not symbol:
            ui.notify("Zadejte ticker.", type="warning")
            return
        if not self._zamek():
            return
        try:
            position = await self.engine.buy(
                symbol,
                self._quantity(),
                self.right_toggle.value,
                kind,
                reprice_id=self.pending_buy_id,
            )
            ui.notify(f"{position.id}: {position.message}", type="positive")
        except Exception as exc:
            ui.notify(f"Nákup se nezdařil: {exc}", type="negative")
        finally:
            self._akce_bezi = False
        self._refresh()

    async def reprice_buy(self, position_id: str, kind: str) -> None:
        """Přecení nevyplněný nákupní příkaz pozice na aktuální cenu."""
        if not self._zamek():
            return
        try:
            position = await self.engine.reprice_buy(position_id, kind)
            ui.notify(f"{position.id}: {position.message}", type="positive")
        except Exception as exc:
            ui.notify(f"Přecenění se nezdařilo: {exc}", type="negative")
        finally:
            self._akce_bezi = False
        self._refresh()

    async def sell(
        self,
        position_id: str,
        kind: str,
        scope: str,
        reprice: bool = False,
        markup_pct: float = 0.0,
    ) -> None:
        """
        Zadá prodejní příkaz podle stisknutého tlačítka.

        reprice nese stav, ve kterém bylo tlačítko vykresleno: True znamená,
        že pozice měla prodejní příkaz v trhu a stisk jej má jen přecenit.
        Vyplnil-li se mezitím, engine akci odmítne a nic se neprodá znovu.
        markup_pct je přirážka nad zvolenou cenou (tlačítka MID +1 % a dál).
        """
        if not self._zamek():
            return
        try:
            position = await self.engine.sell(
                position_id, kind, scope, reprice, markup_pct
            )
            ui.notify(f"{position.id}: {position.message}", type="positive")
        except Exception as exc:
            ui.notify(f"Prodej se nezdařil: {exc}", type="negative")
        finally:
            self._akce_bezi = False
        self._refresh()

    async def cancel_order(self, position_id: str) -> None:
        """Stáhne z trhu nevyřízený příkaz pozice."""
        if not self._zamek():
            return
        try:
            await self.engine.cancel_order(position_id)
            ui.notify("Zrušení příkazu odesláno do TWS.", type="info")
        except Exception as exc:
            ui.notify(f"Příkaz se nepodařilo zrušit: {exc}", type="negative")
        finally:
            self._akce_bezi = False
        self._refresh()

    def remove_finished(self) -> None:
        """Odstraní z přehledu všechny ukončené pozice najednou."""
        try:
            pocet = self.engine.remove_finished()
        except Exception as exc:
            ui.notify(str(exc), type="negative")
            return
        if pocet:
            ui.notify(f"Z přehledu {ukoncene_pozice_text(pocet)}.", type="info")
        else:
            ui.notify("V přehledu není žádná ukončená pozice.", type="warning")
        self._refresh()

    def remove_position(self, position_id: str) -> None:
        """Odstraní ukončenou pozici z přehledu."""
        try:
            self.engine.remove_position(position_id)
        except Exception as exc:
            ui.notify(str(exc), type="negative")
        self._refresh()

    # ------------------------------------------------------------------
    # Formulář a náhled
    # ------------------------------------------------------------------

    def _quantity(self) -> int:
        """Množství z formuláře jako celé číslo, nejméně jeden kontrakt."""
        try:
            return max(1, int(self.quantity_input.value or 1))
        except (TypeError, ValueError):
            return 1

    def _set_loading(self, active: bool) -> None:
        """Zapne nebo vypne hlášku o načítání dat z TWS."""
        self.loading_label.set_visibility(active)

    def _apply_preview(self, preview: Preview | None, chyba: str = "") -> None:
        """Vypíše do formuláře údaje připraveného zadání, nebo jej vyprázdní."""
        self.nahled_varovani.clear()

        if preview is None or not preview.ready:
            self.nahled_kontrakt.set_text(chyba or "Zadejte ticker.")
            for popisek in (self.nahled_podklad, self.nahled_kotace, self.nahled_naklady):
                popisek.set_text("")
            self._refresh_buy_buttons()
            return

        self._refresh_preview_values(preview)
        with self.nahled_varovani:
            for text in preview.warnings:
                ui.label(text).classes("varovani-radek")
        self._refresh_buy_buttons()

    def _refresh_preview_values(self, preview: Preview) -> None:
        """Přepíše v náhledu údaje závislé na tržních datech."""
        dte = ""
        if preview.expiration:
            try:
                dte = f" ({dnu_text(calc.days_to_expiry(preview.expiration))})"
            except ValueError:
                dte = ""
        self.nahled_kontrakt.set_text(f"{preview.contract_label}{dte}")
        self.nahled_podklad.set_text(
            f"Podklad: {fmt(preview.current_price)} | delta: {fmt(preview.delta)}"
        )
        self.nahled_kotace.set_text(
            f"BID/ASK: {fmt(preview.option_bid)} / {fmt(preview.option_ask)} | "
            f"MID: {fmt(preview.mid)} | spread: {fmt(preview.spread_pct, 1, ' %')}"
        )
        naklady = calc.order_value(preview.mid, self._quantity())
        self.nahled_naklady.set_text(
            f"Odhad nákladů za {self._quantity()} ks (MID): {fmt(naklady, 2, ' USD')}"
        )

    def _refresh_buy_buttons(self) -> None:
        """
        Nastaví popisky nákupních tlačítek podle aktuální kotace a množství.

        Cena se na tlačítku ukáže jen tehdy, když připravený kontrakt odpovídá
        formuláři - po přepsání tickeru by šlo o cenu jiné opce. Tlačítko
        přesto zůstává aktivní: nákup si kontrakt v takovém případě připraví sám.
        """
        mnozstvi = self._quantity()
        symbol = (self.ticker_input.value or "").upper().strip()
        right = self.right_toggle.value
        preview = self.engine.preview
        pripraveno = preview is not None and preview.ready
        sedi = pripraveno and preview.symbol == symbol and preview.right == right

        # Čeká-li na tickeru nevyplněný nákup, tlačítka příkaz přeceňují
        cekajici = self.engine.pending_buy(symbol, right) if symbol else None
        self.pending_buy_id = cekajici.id if cekajici is not None else ""

        for tlacitko, napoveda, kind in (
            (self.btn_ask, self.tip_ask, "ask"),
            (self.btn_mid, self.tip_mid, "mid"),
        ):
            popisek = buy_button_label(kind, mnozstvi)
            if cekajici is not None:
                limit = self.engine.position_buy_limit(cekajici, kind)
                napoveda.set_text(
                    f"Nevyplněný příkaz se v TWS přepíše na aktuální "
                    f"{price_kind_label(kind)}; druhý příkaz nevzniká."
                )
            else:
                limit = self.engine.preview_buy_limit(kind) if sedi else None
                napoveda.set_text(
                    "Limitní nákup na poptávané ceně - projde hned, ale zaplatí se "
                    "celý spread."
                    if kind == "ask"
                    else "Limitní nákup na středu trhu - levnější, ale nemusí se vyplnit."
                )
            tlacitko.set_text(popisek if limit is None else f"{popisek} · {fmt(limit)}")
            # Bez spojení nebo bez tickeru nemá nákup smysl
            tlacitko.set_enabled(bool(symbol) and self.ib.connected)

    # ------------------------------------------------------------------
    # Periodické překreslení
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        """Obnoví všechny části stránky podle aktuálního stavu."""
        self._refresh_status()
        self._refresh_market_open()
        self._refresh_warning()

        # Náhled si drží vlastní odběr dat, takže se ceny hýbou i bez nákupu
        self.engine.refresh_preview()
        preview = self.engine.preview
        if preview is not None and preview.ready:
            self._refresh_preview_values(preview)
        self._refresh_buy_buttons()

        self._refresh_positions()
        self._refresh_log()
        # Otevřený přehled výsledků tiká živě ze stejné smyčky
        self.report_dialog.refresh()

    def _refresh_status(self) -> None:
        """Vypíše stav spojení a přepíše popisek tlačítka pro připojení."""
        if self.ib.connected:
            ucet = self.ib.account or "neurčen"
            self.status_label.set_text(f"TWS připojeno ({ucet})")
            self.status_label.classes(replace="stav-spojeni spojeni-ok")
            self.connect_button.set_text("Odpojit")
        else:
            self.status_label.set_text("TWS odpojeno")
            self.status_label.classes(replace="stav-spojeni spojeni-chyba")
            self.connect_button.set_text("Připojit")

    def _refresh_market_open(self) -> None:
        """Odpočet do otevření burzy v hlavičce - během seance se skrývá."""
        sekundy = self.engine.market_open_seconds()
        if sekundy is None:
            self.market_open_label.set_visibility(False)
            return

        self.market_open_label.set_visibility(True)
        self.market_open_label.set_text(f"Otevření trhu za {format_countdown(sekundy)}")

    def _refresh_warning(self) -> None:
        """Vypíše pruh s opčními pozicemi na účtu, které aplikace neřídí."""
        aktualni = set(self.engine.unmanaged)
        if aktualni == self.last_unmanaged:
            return
        self.last_unmanaged = aktualni

        self.warning_bar.clear()
        self.warning_bar.set_visibility(bool(aktualni))
        if not aktualni:
            return

        with self.warning_bar:
            ui.label("Neřízené opční pozice na účtu - prodávejte je v TWS:").classes(
                "pruh-text"
            )
            # Pozice stojí v jediném výčtu, aby pruh zabral jen jeden řádek
            vypis = " · ".join(
                f"{info.label} ({info.quantity:g} ks)"
                for info in (self.engine.unmanaged[conid] for conid in sorted(aktualni))
            )
            ui.label(vypis).classes("pruh-text pruh-vypis")

    def _refresh_positions(self) -> None:
        """Založí karty nových pozic, zruší karty odstraněných a ostatní obnoví."""
        aktualni = {p.id: p for p in self.engine.sorted_positions()}

        for position_id in list(self.cards):
            if position_id not in aktualni:
                self.cards.pop(position_id).remove()

        # Karty se zakládají od nejstarší a každá se posouvá na začátek seznamu,
        # takže výsledné pořadí odpovídá řazení enginu - nejnovější nahoře
        for position in reversed(list(aktualni.values())):
            if position.id in self.cards:
                continue
            with self.positions_container:
                karta = PositionCard(self, position)
            karta.karta.move(self.positions_container, target_index=0)
            self.cards[position.id] = karta

        for position in aktualni.values():
            self.cards[position.id].update(position)

        self.prazdny_prehled.set_visibility(not aktualni)
        # Úklid má smysl nabízet, jen když je co uklidit
        self.btn_uklid.set_visibility(
            any(not p.state.is_active for p in aktualni.values())
        )

    def _refresh_log(self) -> None:
        """
        Vypíše poslední události aplikace, nejnovější nahoře.
        Překresluje se pouze při nové události, aby seznam zbytečně neblikal.
        """
        udalosti = list(reversed(self.engine.events))[:LOG_ZOBRAZENO]
        nejnovejsi = udalosti[0][0] if udalosti else None
        if nejnovejsi == self.last_log_stamp:
            return
        self.last_log_stamp = nejnovejsi

        self.log_area.clear()
        with self.log_area:
            for cas, zprava in udalosti:
                ui.label(f"{cas:%H:%M:%S}  {zprava}").classes("log-radek")

    def _refresh_config(self) -> None:
        """Zobrazí podstatná nastavení z konfiguračního souboru."""
        t = self.cfg.trading
        e = self.cfg.expiration
        s = self.cfg.strike
        expirace = e.fixed_date if e.mode == "fixed" else f"nejbližší (min. {e.min_dte} dní)"
        strike = (
            f"{s.otm_steps}. strike mimo peníze"
            if s.mode == "otm_offset"
            else "nejbližší aktuální ceně (ATM)"
        )
        self.config_label.set_text(
            f"Expirace: {expirace}\n"
            f"Strike: {strike}\n"
            f"Runner: {t.runner_quantity} ks | max. spread {t.max_spread_pct:g} %\n"
            f"Tolerance: ASK +{t.ask_tolerance_pct:g} % | BID -{t.bid_tolerance_pct:g} %\n"
            f"Platnost příkazů: {t.tif}"
        )


def create_ui(cfg: AppConfig, engine: ManualEngine, ib: IBService) -> None:
    """Zaregistruje statické soubory a hlavní stránku aplikace."""
    # Keš se u lokální aplikace vypíná, aby se úpravy stylů projevily
    # hned po obnovení stránky
    app.add_static_files("/static", str(STATIC_DIR), max_cache_age=0)

    # Bubliny s nápovědou vyskakují nad prvkem, ne pod ním - pod tlačítky
    # by zakrývaly další ovládání
    ui.tooltip.default_props('anchor="top middle" self="bottom middle"')

    @ui.page("/")
    def index() -> None:
        """Hlavní stránka - každý klient dostane vlastní instanci ovládacích prvků."""
        TradingUI(cfg, engine, ib).build()
