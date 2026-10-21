"""
Přepínač vzhledu aplikace - světlý, tmavý, nebo podle systému.

Stránka i CSS se v režimu „podle systému" přepínají samy (Quasar sleduje
prefers-color-scheme). Grafy ale kreslí canvas a barvy jim dodává Python,
proto server systémovou preferenci zjišťuje i on (static/system_theme.js).
"""

from __future__ import annotations

from nicegui import app, ui

# Pořadí režimů při klikání na přepínač; None je režim podle systému
NEXT_MODE: dict[bool | None, bool | None] = {False: True, True: None, None: False}
# Ikona a popis aktuálního režimu
MODE_ICONS = {False: "light_mode", True: "dark_mode", None: "brightness_auto"}
MODE_LABELS = {False: "světlý", True: "tmavý", None: "podle systému"}

# Klíč v trvalém úložišti - volba obchodníka se pamatuje mezi spuštěními
STORAGE_KEY = "dark_mode"


def _tooltip_text(rezim: bool | None) -> str:
    """Nápověda tlačítka - aktuální režim a ten, na který klik přepne."""
    return f"Vzhled: {MODE_LABELS[rezim]} (klik: {MODE_LABELS[NEXT_MODE[rezim]]})"


class ThemeSwitch:
    """
    Stav vzhledu jedné stránky a tlačítka, která jej přepínají.

    :param default: výchozí režim z konfigurace (True tmavý, False světlý,
        None podle systému); uložená volba obchodníka má přednost
    """

    def __init__(self, default: bool | None) -> None:
        self.dark_mode = ui.dark_mode(app.storage.general.get(STORAGE_KEY, default))
        # Systémová preference prohlížeče; do první odpovědi prohlížeče
        # se předpokládá světlý systém
        self.system_dark = False
        # Počáteční stav se zjistí při každém (i opětovném) připojení,
        # další změny prohlížeč hlásí sám událostí
        self.dark_mode.client.on_connect(self._read_system_dark)
        ui.on("system_dark", self._on_system_dark)

    def is_dark(self) -> bool:
        """Zda stránka právě skutečně svítí tmavě - i v režimu podle systému."""
        return self.system_dark if self.dark_mode.value is None else self.dark_mode.value

    def button(self) -> ui.button:
        """
        Vytvoří tlačítko přepínače v aktuálním kontextu. Ikona i nápověda
        jsou navázané na stav, takže přepnutí kterýmkoliv tlačítkem srovná
        všechna.
        """
        tlacitko = ui.button(on_click=self.cycle).props("flat round dense")
        tlacitko.bind_icon_from(self.dark_mode, "value", backward=MODE_ICONS.get)
        with tlacitko:
            ui.tooltip().bind_text_from(self.dark_mode, "value", backward=_tooltip_text)
        return tlacitko

    def cycle(self) -> None:
        """Přepne na další režim v pořadí a volbu si zapamatuje."""
        self.dark_mode.value = NEXT_MODE[self.dark_mode.value]
        app.storage.general[STORAGE_KEY] = self.dark_mode.value

    async def _read_system_dark(self) -> None:
        """Po připojení se zeptá prohlížeče na systémovou preferenci."""
        self.system_dark = bool(
            await self.dark_mode.client.run_javascript("systemDarkQuery.matches")
        )

    def _on_system_dark(self, e) -> None:
        """Prohlížeč nahlásil změnu systémové preference."""
        self.system_dark = bool(e.args)
