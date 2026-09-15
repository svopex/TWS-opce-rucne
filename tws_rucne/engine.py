"""
Obchodní logika ručního obchodování opcí.

Engine drží připravené zadání (náhled) a otevřené pozice, zadává nákupní
a prodejní příkazy do TWS a v monitorovací smyčce sleduje jejich vyplnění.
Žádné automatické cíle ani stop-lossy nezadává - o každém příkazu rozhoduje
obchodník stiskem tlačítka.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import calc, store
from .config import AppConfig
from .ib_service import IBService, PositionInfo, order_ref, valid_price
from .models import (
    RIGHT_LABELS,
    SELL_SCOPE_ALL,
    SELL_SCOPE_BASE,
    SELL_SCOPE_ONE,
    Position,
    PositionState,
    cislo_text,
    order_pnl_text,
    contract_label,
    price_kind_label,
    ukoncene_pozice_text,
    zbytek_text,
)
from .reservations import ReservedContracts

log = logging.getLogger(__name__)

# Stavy, ve kterých už příkaz v TWS nežije
DEAD_ORDER_STATES = ("Cancelled", "ApiCancelled", "Inactive")

# Jak často se zkouší převzít velikost účtu, dokud ji TWS ani jednou neposlala
ACCOUNT_RETRY_SEC = 5.0

# Stavy, ve kterých TWS dovolí příkaz upravit (přecenit). V ostatních
# stavech se příkaz vyplňuje nebo ruší a modifikace by skončila hlášením
# "too late to replace".
MODIFIABLE_ORDER_STATES = ("PreSubmitted", "Submitted")

# Kolik strike cen se nejvýš zkusí ověřit, než příprava vzdá hledání kontraktu
MAX_STRIKE_ATTEMPTS = 8

# O kolik kroků mimo peníze se nejvýš ustoupí, je-li vybraný kontrakt obsazený
# cizím příkazem nebo neřízenou pozicí. Dál od peněz klesá delta, takže
# ustupovat donekonečna nemá smysl - pak se kolize radši jen ohlásí.
MAX_OCCUPIED_SHIFTS = 4

# Jak dlouho se u dokončeného prodeje čeká na skutečnou průměrnou cenu z TWS,
# než se pozice uzavře s odhadem podle limitní ceny
FILL_PRICE_WAIT_SEC = 3.0

# Přenos nevyplněného nákupu na jiný strike: jak dlouho se nejvýš čeká, než
# TWS potvrdí zrušení původního příkazu, a jak často se stav mezitím přebírá.
# Bez potvrzení se nový příkaz nezadává - v trhu by mohly viset oba.
TRANSFER_CANCEL_WAIT_SEC = 3.0
TRANSFER_POLL_SEC = 0.2


def _popis_vysledku(position: Position, limit: float, quantity: int) -> str:
    """
    Věta o výsledku prodeje do hlášky o příkazu, například ', zisk 12 USD'.

    Počítá se z limitní ceny příkazu, ne z kotace: obchodník tak v průběhu
    vidí, co mu příkaz vynese, když se vyplní za cenu, se kterou byl zadán.
    Bez nákupní ceny není co počítat a hláška zůstane bez výsledku.
    """
    vysledek = order_pnl_text(position.sell_pnl_at(limit, quantity))
    return f", {vysledek}" if vysledek else ""


@dataclass
class Preview:
    """
    Připravené zadání - kontrakt vybraný podle aktuální ceny podkladu
    a jeho kotace. Vzniká při každé změně formuláře, sám nic nezadává.
    """

    symbol: str
    right: str
    underlying: Any = None
    option: Any = None
    current_price: float | None = None
    expiration: str = ""
    strike: float | None = None
    option_bid: float | None = None
    option_ask: float | None = None
    delta: float | None = None
    min_tick: float = 0.01
    warnings: list[str] = field(default_factory=list)
    # Náhled drží vlastní odběry tržních dat, dokud jej nenahradí novější
    owns_subscription: bool = False

    @property
    def ready(self) -> bool:
        """Náhled má ověřený kontrakt, takže z něj lze zadat nákup."""
        return self.option is not None

    @property
    def right_label(self) -> str:
        """CALL / PUT pro zobrazení."""
        return RIGHT_LABELS.get(self.right, self.right)

    @property
    def mid(self) -> float | None:
        """Střed trhu opce."""
        return calc.mid_price(self.option_bid, self.option_ask)

    @property
    def spread_pct(self) -> float | None:
        """Spread opce v procentech ze středu trhu."""
        return calc.spread_pct(self.option_bid, self.option_ask)

    @property
    def contract_label(self) -> str:
        """Popis vybraného kontraktu pro rozhraní."""
        if not self.ready or self.strike is None:
            return ""
        return contract_label(self.symbol, self.expiration, self.right, self.strike)


class ManualEngine:
    """Řídí ruční nákupy a prodeje opcí a sleduje jejich stav v TWS."""

    def __init__(self, cfg: AppConfig, ib: IBService) -> None:
        self.cfg = cfg
        self.ib = ib
        # Pozice podle identifikátoru, v pořadí vzniku
        self.positions: dict[str, Position] = {}
        # Události pro výpis v rozhraní (čas, text)
        self.events: list[tuple[datetime, str]] = []
        # Opční pozice na účtu, které aplikace neřídí
        self.unmanaged: dict[int, PositionInfo] = {}
        # Kontrakty zamluvené obchody jiných aplikací, čtené z jejich
        # uloženého stavu - vlastní stav se přitom vynechává
        self.reserved = ReservedContracts(
            cfg.strike.reserved_state_files,
            cfg.state.file if cfg.state.enabled else None,
        )
        # Naposledy připravené zadání
        self._preview: Preview | None = None
        self._ids = itertools.count(1)
        self._task: asyncio.Task | None = None
        self._restore_lock = asyncio.Lock()
        # Uložený stav se čte jen při prvním spuštění
        self._restored = False
        # Po každém novém spojení se pozice musí znovu spárovat s příkazy v TWS
        self._synced = False
        self._last_reconnect = 0.0
        # Automatické připojování lze vypnout přepínačem --no-connect
        self.auto_connect = True
        # Čas posledního neúspěšného srovnání s TWS - další pokus se odkládá,
        # aby se selhávající obnova nedotazovala TWS při každém průchodu smyčkou
        self._last_restore_error = 0.0
        # Velikost účtu převzatá z TWS (NetLiquidation) a čas posledního
        # dotazu; None znamená, že hodnota zatím nedorazila
        self._live_account_size: float | None = None
        self._account_checked = 0.0
        # Běžící dotaz na velikost účtu; drží se, aby ho nesebral garbage
        # collector a aby se nespouštěl druhý, dokud první neskončil
        self._account_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Události a stav
    # ------------------------------------------------------------------

    def log_event(self, message: str) -> None:
        """Zapíše událost do přehledu i do logu aplikace."""
        self.events.append((datetime.now(), message))
        # Historie se drží jen v rozsahu, který rozhraní zobrazuje
        prebytek = len(self.events) - self.cfg.ui.log_lines
        if prebytek > 0:
            del self.events[:prebytek]
        log.info(message)

    def _persist(self) -> None:
        """Uloží stav pozic na disk, je-li ukládání zapnuté."""
        if not self.cfg.state.enabled:
            return
        store.save(list(self.positions.values()), self.cfg.state.file)

    @property
    def is_monitoring(self) -> bool:
        """Monitorovací smyčka běží."""
        return self._task is not None and not self._task.done()

    @property
    def account_size(self) -> float:
        """
        Skutečná velikost účtu v USD podle TWS (NetLiquidation).

        Slouží k přepočtu výsledku dne na procenta účtu. Nula znamená, že
        hodnota zatím není známa - konfigurace ji nemá a z TWS nedorazila.
        """
        return self._live_account_size or 0.0

    def sorted_positions(self) -> list[Position]:
        """
        Pozice pro přehled: nejdřív aktivní, pak ukončené,
        uvnitř obou skupin od nejnovější.
        """
        return sorted(
            self.positions.values(),
            key=lambda p: (0 if p.state.is_active else 1, -p.created_at.timestamp()),
        )

    def position(self, position_id: str) -> Position:
        """Vrátí pozici podle identifikátoru, jinak vyhodí chybu."""
        pozice = self.positions.get(position_id)
        if pozice is None:
            raise ValueError(f"Pozice '{position_id}' neexistuje.")
        return pozice

    def _next_id(self, symbol: str) -> str:
        """Sestaví identifikátor pozice, například 'AAPL-3'."""
        return f"{symbol}-{next(self._ids)}"

    # ------------------------------------------------------------------
    # Obchodní hodiny burzy
    # ------------------------------------------------------------------

    def _exchange_now(self) -> datetime:
        """Aktuální čas v časové zóně burzy (řeší letní i zimní čas)."""
        return datetime.now(ZoneInfo(self.cfg.trading.exchange_timezone))

    def _exchange_time(self, ted: datetime, hodnota: str) -> datetime:
        """Čas dnešního dne podle zápisu HH:MM v časové zóně burzy."""
        hodina, minuta = (int(cast) for cast in hodnota.split(":"))
        return ted.replace(hour=hodina, minute=minuta, second=0, microsecond=0)

    def market_open_seconds(self) -> float | None:
        """
        Počet sekund do nejbližšího otevření burzy.

        None znamená, že burza právě obchoduje - odpočet nemá co měřit.
        Po zavření a o víkendu se míří na otevření následujícího obchodního
        dne; svátky ani zkrácené obchodní dny aplikace nezná.
        """
        ted = self._exchange_now()
        otevreni = self._exchange_time(ted, self.cfg.trading.exchange_open_time)

        # V obchodní den před otevřením stačí odpočet do dnešní seance
        if ted.weekday() < 5 and ted < otevreni:
            return (otevreni - ted).total_seconds()

        # Uvnitř dnešní seance se odpočet nezobrazuje
        if ted.weekday() < 5 and ted < self._exchange_time(
            ted, self.cfg.trading.exchange_close_time
        ):
            return None

        # Po zavření a o víkendu se hledá nejbližší další obchodní den.
        # Přičítání dnů běží v nástěnném čase burzy, takže přechod mezi
        # letním a zimním časem otevírací hodinu neposune
        cil = otevreni + timedelta(days=1)
        while cil.weekday() >= 5:
            cil += timedelta(days=1)
        # Rozdíl přes timestamp počítá skutečně uplynulé sekundy i tehdy,
        # když mezi dneškem a cílem přeskočí hodina letního času
        return cil.timestamp() - ted.timestamp()

    # ------------------------------------------------------------------
    # Příprava zadání
    # ------------------------------------------------------------------

    async def prepare(self, symbol: str, right: str) -> Preview:
        """
        Připraví zadání: načte cenu podkladu, vybere expiraci a strike podle
        aktuální ceny a ověří opční kontrakt v TWS. Nic nezadává do trhu.

        Výsledek se uloží jako aktuální náhled - tlačítka nákupu z něj pak
        berou kontrakt i kotace.
        """
        if not self.ib.connected:
            raise RuntimeError("Není navázáno spojení s TWS.")

        symbol = symbol.upper().strip()
        if not symbol:
            raise ValueError("Zadejte ticker.")
        if right not in ("C", "P"):
            raise ValueError("Zvolte typ opce CALL nebo PUT.")

        preview = Preview(symbol=symbol, right=right)
        # Odběry zakládá příprava sama; nedoběhne-li (chyba nebo novější zadání),
        # musí je zase uvolnit, jinak by kontrakty zůstaly odebírané až do restartu
        try:
            preview.underlying = await self.ib.qualify_stock(symbol)
            self.ib.subscribe(preview.underlying)
            preview.owns_subscription = True
            await self.ib.wait_for_quotes(
                preview.underlying, self.cfg.engine.market_data_timeout_sec
            )
            preview.current_price = self.ib.underlying_price(preview.underlying)

            # Bez ceny podkladu nelze určit strike - vrací se aspoň prázdný náhled
            if preview.current_price is None:
                preview.warnings.append(
                    "Z TWS zatím nedorazila cena podkladu - zkontrolujte odběr tržních dat."
                )
                self._replace_preview(preview)
                return preview

            # Expirace podle konfigurace
            retezec = await self.ib.option_chain(preview.underlying)
            expirace = calc.select_expiration(
                list(retezec.expirations),
                self.cfg.expiration.mode,
                self.cfg.expiration.min_dte,
                self.cfg.expiration.fixed_date,
            )
            if expirace is None:
                raise ValueError(
                    f"Pro ticker {symbol} nebyla nalezena vhodná expirace "
                    f"(režim '{self.cfg.expiration.mode}')."
                )
            preview.expiration = expirace

            # Strike podle aktuální ceny podkladu - odsazený mimo peníze, nebo ATM
            kroky = self.cfg.strike.otm_steps if self.cfg.strike.mode == "otm_offset" else 0
            strikes = list(retezec.strikes)
            cil = calc.otm_strike(strikes, preview.current_price, right, kroky)
            if cil is None:
                raise ValueError(f"Pro ticker {symbol} nejsou dostupné strike ceny.")

            # Kontrakt obsazený cizím příkazem nebo neřízenou pozicí se přeskočí:
            # TWS vede pozice po kontraktech, takže nákup téhož kontraktu by se
            # s cizí pozicí sečetl v jedinou
            obsazene = await self._occupied_conids()

            strike, option, detaily = await self._qualify_free_option(
                preview, strikes, cil, kroky, obsazene, retezec.tradingClass
            )
            preview.strike = strike
            preview.option = option
            preview.min_tick = detaily.minTick or 0.01

            # Kotace vybrané opce - z nich se počítají limitní ceny tlačítek
            self.ib.subscribe(option)
            await self.ib.wait_for_quotes(
                option,
                self.cfg.engine.market_data_timeout_sec,
                self.cfg.engine.quotes_grace_sec,
            )
            self._read_quotes(preview)

            if preview.mid is None:
                preview.warnings.append(
                    "Opce nemá úplnou kotaci BID/ASK - nákup zatím nelze zadat."
                )
            elif (preview.spread_pct or 0.0) > self.cfg.trading.max_spread_pct:
                preview.warnings.append(
                    f"Spread {preview.spread_pct:.1f} % přesahuje limit "
                    f"{self.cfg.trading.max_spread_pct:g} % - nákup se prodraží."
                )

            self._replace_preview(preview)
            return preview
        except BaseException:
            # Chyba i zrušení přípravy musí odběry vrátit zpět
            self._release_preview(preview)
            raise

    async def _qualify_free_option(
        self,
        preview: Preview,
        strikes: list[float],
        cil: float,
        kroky: int,
        obsazene: dict[int, str],
        trading_class: str,
    ) -> tuple[float, Any, Any]:
        """
        Ověří opční kontrakt pro vybraný strike a vyhne se obsazeným.

        Je-li vybraný kontrakt obsazený (cizí příkaz v TWS nebo neřízená
        pozice na účtu), zkusí se strike o krok dál mimo peníze, pak další -
        nejvýš MAX_OCCUPIED_SHIFTS kroků. Nenajde-li se volno ani tam, vrátí
        se první volba a kolize se jen ohlásí: rozhodnutí patří obchodníkovi
        stejně jako u širokého spreadu. Prázdný slovník obsazene vyhýbání
        vypíná a výběr proběhne jako dřív.

        Varování o náhradním i posunutém striku zapisuje metoda rovnou
        do náhledu. Vrací trojici (strike, kontrakt, detaily).
        """
        # Volba podle konfigurace; teprve od ní se případně ustupuje
        prvni = await self._qualify_nearest_option(
            preview.symbol, preview.expiration, strikes, cil, preview.right, trading_class
        )
        prvni_strike, prvni_option, _ = prvni

        # Náhradní strike se hlásí, aby bylo jasné, proč kontrakt neodpovídá výběru
        if prvni_strike != cil:
            preview.warnings.append(
                f"Strike {cil:g} není pro expiraci {preview.expiration} v TWS "
                f"dostupný, použit nejbližší obchodovatelný {prvni_strike:g}."
            )

        prvni_obsazeno = obsazene.get(prvni_option.conId)
        if prvni_obsazeno is None:
            return prvni

        # Řetězec na kraji dojde a vrací pořád tentýž kontrakt - bez této
        # paměti by se posouvání točilo naprázdno až do konce rozsahu
        vyzkousene = {prvni_option.conId}

        for posun in range(1, MAX_OCCUPIED_SHIFTS + 1):
            # Ustupuje se dál mimo peníze, tedy stejným směrem, jakým
            # odsazuje strike.otm_steps; režim atm začíná prvním OTM
            cilovy = calc.otm_strike(
                strikes, preview.current_price, preview.right, kroky + posun
            )
            if cilovy is None:
                break

            strike, option, detaily = await self._qualify_nearest_option(
                preview.symbol, preview.expiration, strikes, cilovy, preview.right, trading_class
            )
            # Není-li posunutý strike v TWS obchodovatelný, vrátí hledání
            # kandidáta blíž penězům, kterého už zkoušelo. Takový krok se
            # přeskočí - dál od peněz ještě volno být může.
            if option.conId in vyzkousene:
                continue
            vyzkousene.add(option.conId)

            if option.conId not in obsazene:
                preview.warnings.append(
                    f"Strike {prvni_strike:g} už obsadil {prvni_obsazeno} "
                    f"- vybrán {strike:g}."
                )
                return strike, option, detaily

        # Volný kontrakt se nenašel. Nabídne se první volba, ale obchodník
        # musí vědět, že se nákup v TWS sečte s cizí pozicí. Stisk tlačítka
        # projde: příprava vrátí tentýž kontrakt, takže se nabídka nezměnila.
        preview.warnings.append(
            f"POZOR: strike {prvni_strike:g} už obsadil {prvni_obsazeno} "
            f"a volný strike se poblíž nenašel. Nákup se v TWS sečte s cizí "
            f"pozicí - zkontrolujte ji před zadáním."
        )
        return prvni

    async def _qualify_nearest_option(
        self,
        symbol: str,
        expiration: str,
        strikes: list[float],
        target: float,
        right: str,
        trading_class: str = "",
    ) -> tuple[float, Any, Any]:
        """
        Ověří v TWS opční kontrakt se strike nejblíže vybrané hodnotě.

        Opční řetězec vrací strike ceny pro všechny expirace dohromady, takže
        vybraný strike nemusí být pro zvolenou expiraci obchodovatelný
        (například půlbodové strike jen u týdenních expirací). Kandidáti se
        proto zkoušejí v pořadí podle vzdálenosti od cíle, dokud se některý
        neověří. Vrací trojici (strike, kontrakt, detaily).
        """
        kandidati = sorted(strikes, key=lambda s: (abs(s - target), s))[:MAX_STRIKE_ATTEMPTS]
        if not kandidati:
            raise ValueError(f"Pro ticker {symbol} nejsou dostupné strike ceny.")

        posledni_chyba: Exception | None = None
        for strike in kandidati:
            try:
                option, detaily = await self.ib.qualify_option(
                    symbol, expiration, strike, right, trading_class
                )
                return strike, option, detaily
            except ValueError as exc:
                # Kontrakt pro tuto expiraci neexistuje - zkusí se další strike
                posledni_chyba = exc

        raise ValueError(
            f"Pro ticker {symbol} {expiration} se poblíž ceny {target:g} nepodařilo najít "
            f"obchodovatelný strike. Poslední chyba: {posledni_chyba}"
        )

    def _read_quotes(self, preview: Preview) -> None:
        """Načte do náhledu aktuální kotace a deltu vybrané opce."""
        bid, ask, delta = self.ib.option_quotes(preview.option)
        preview.option_bid = bid
        preview.option_ask = ask
        if delta is not None:
            preview.delta = delta

    def _replace_preview(self, preview: Preview) -> None:
        """Nahradí aktuální náhled novým a odběry toho starého uvolní."""
        if self._preview is not None and self._preview is not preview:
            self._release_preview(self._preview)
        self._preview = preview

    def _release_preview(self, preview: Preview | None) -> None:
        """Uvolní odběry tržních dat, které náhled držel."""
        if preview is None or not preview.owns_subscription:
            return
        preview.owns_subscription = False
        self.ib.unsubscribe(preview.option)
        self.ib.unsubscribe(preview.underlying)

    def release_preview(self) -> None:
        """Zahodí aktuální náhled - volá rozhraní při vyprázdnění formuláře."""
        self._release_preview(self._preview)
        self._preview = None

    @property
    def preview(self) -> Preview | None:
        """Aktuálně připravené zadání."""
        return self._preview

    def refresh_preview(self) -> None:
        """Načte do náhledu čerstvé kotace - volá se z překreslování rozhraní."""
        if self._preview is None or not self._preview.ready:
            return
        self._preview.current_price = (
            self.ib.underlying_price(self._preview.underlying) or self._preview.current_price
        )
        self._read_quotes(self._preview)

    def preview_buy_limit(self, kind: str) -> float | None:
        """Limitní cena nákupu pro dané tlačítko podle aktuálních kotací náhledu."""
        preview = self._preview
        if preview is None or not preview.ready:
            return None
        cena = calc.buy_limit_price(
            kind, preview.option_bid, preview.option_ask, self.cfg.trading.ask_tolerance_pct
        )
        return None if cena is None else calc.round_to_tick(cena, preview.min_tick)

    def position_buy_limit(self, position: Position, kind: str) -> float | None:
        """Limitní cena nákupu podle aktuálních kotací už založené pozice."""
        cena = calc.buy_limit_price(
            kind, position.option_bid, position.option_ask, self.cfg.trading.ask_tolerance_pct
        )
        return None if cena is None else calc.round_to_tick(cena, position.min_tick)

    @property
    def ask_markups_pct(self) -> list[float]:
        """
        Přirážky nad poptávanou cenou nabízené prodejními tlačítky (v procentech).

        Rozhraní si nabídku bere odtud, ne z konfigurace: ceny tlačítek už
        počítá engine (position_sell_limit), takže obojí pochází z jednoho místa.
        """
        return self.cfg.trading.ask_markups_pct

    def position_sell_limit(
        self, position: Position, kind: str, markup_pct: float = 0.0
    ) -> float | None:
        """
        Limitní cena prodeje pro dané tlačítko podle aktuálních kotací pozice.
        markup_pct je přirážka nad zvolenou cenou (tlačítka s přirážkou nad
        ASK, nabídku určuje trading.ask_markups_pct v konfiguraci).
        """
        cena = calc.sell_limit_price(
            kind,
            position.option_bid,
            position.option_ask,
            self.cfg.trading.bid_tolerance_pct,
            markup_pct,
        )
        return None if cena is None else calc.round_to_tick(cena, position.min_tick)

    # ------------------------------------------------------------------
    # Nákup
    # ------------------------------------------------------------------

    async def buy(
        self, symbol: str, quantity: int, right: str, kind: str, reprice_id: str = ""
    ) -> Position:
        """
        Zadá limitní nákupní příkaz na opci vybranou podle aktuální ceny podkladu.

        kind = 'ask' nakupuje na poptávané ceně (příkaz projde hned, je-li
        kotace stabilní), kind = 'mid' na středu trhu (levnější, ale nemusí
        se vyplnit).

        reprice_id nese identifikátor pozice s nevyplněným nákupním příkazem,
        kterou rozhraní vidělo v okamžiku vykreslení tlačítka. Je-li vyplněný,
        opakovaný stisk příkaz jen přecení na aktuální cenu - trh mu mohl mezi
        zadáním a teď utéct. Změnil-li se mezitím stav pozice (příkaz se
        vyplnil, nebo naopak nový vznikl), nákup se odmítne: stisk tlačítka
        nesmí skončit druhým, nechtěným nákupem.
        """
        if kind not in calc.BUY_KINDS:
            raise ValueError(f"Neznámý druh nákupu: {kind}")
        if not self.ib.connected:
            raise RuntimeError("Není navázáno spojení s TWS.")

        symbol = symbol.upper().strip()
        quantity = int(quantity)
        trading = self.cfg.trading
        if quantity < trading.min_quantity or quantity > trading.max_quantity:
            raise ValueError(
                f"Množství musí být mezi {trading.min_quantity} a {trading.max_quantity} kontrakty."
            )

        # Stav příkazů se srovná s TWS ještě před rozhodnutím - vyplnění, které
        # právě dorazilo, se tak projeví dřív, než by vznikl další příkaz
        self._refresh_orders()

        # Čerstvá obsazenost jednou za stisk: příprava si ji vezme z krátké
        # paměti a kontrola nabídky z téhož slovníku, takže do TWS jde
        # jediný dotaz, ne dva krátce po sobě
        obsazene = await self._occupied_conids(refresh=True)

        # Nabídka se mohla mezi vykreslením tlačítka a stiskem změnit - strike
        # se mohl obsadit i uvolnit. Zadání se proto připraví znovu.
        videny = self.shown_preview(symbol, right)
        preview = await self.prepare(symbol, right)
        if not preview.ready:
            raise ValueError(
                f"Kontrakt pro {symbol} se nepodařilo připravit - nákup nelze zadat."
            )

        if reprice_id:
            cekajici = self.position(reprice_id)
            if cekajici.state != PositionState.BUYING:
                raise ValueError(
                    f"Nákupní příkaz pozice {reprice_id} už v trhu není "
                    f"({cekajici.state.label}) - druhý nákup se nezadává."
                )

            # Náhled mezitím mohl nabídnout jiný kontrakt - typicky když se
            # uvolnil strike, kterému se při zadání ustoupilo. Přecenění
            # kontraktem nehne, mění jen cenu, proto se příkaz přenese.
            preview = self.transfer_preview(cekajici)
            if preview is not None:
                return await self._transfer_buy(cekajici, quantity, kind, preview)
            return await self.reprice_buy(reprice_id, kind, quantity)

        cekajici = self.pending_buy(symbol, right)
        if cekajici is not None:
            raise ValueError(
                f"Na {symbol} {RIGHT_LABELS.get(right, right)} už čeká nevyplněný nákupní "
                f"příkaz ({cekajici.id}) - druhý se nezadává. Přeceňte jej tlačítkem "
                f"u pozice, nebo jej nejdřív zrušte."
            )

        # Nabízí-li příprava jiný kontrakt, než jaký měl obchodník na obrazovce,
        # do trhu nejde nic: v náhledu zůstane ten nový a rozhoduje druhý stisk.
        # Čekající příkaz se sem nedostane - ten se rovnou přepíše (viz výše).
        if videny is not None and preview.option.conId != videny.option.conId:
            # Proč se nabídka změnila, řekne obsazenost: kolize je pro
            # obchodníka jiná zpráva než posun striku za pohybem podkladu
            popis = obsazene.get(videny.option.conId)
            duvod = (
                f"Kontrakt {videny.contract_label} mezitím obsadil {popis}"
                if popis is not None
                else f"Nabídka se změnila: místo {videny.contract_label} "
                f"je připravený {preview.contract_label}"
            )
            raise ValueError(
                f"{duvod} - nákup se nezadává. V náhledu je připravený "
                f"{preview.contract_label}, zkontrolujte jej a stiskněte znovu."
            )

        # Limitní cena se počítá z čerstvých kotací, ne z těch v náhledu
        self._read_quotes(preview)
        limit = self.preview_buy_limit(kind)
        if limit is None:
            raise ValueError(
                "Z TWS nedorazila potřebná kotace opce - limitní cenu nákupu nelze určit."
            )

        position = Position(
            id=self._next_id(symbol),
            symbol=symbol,
            right=right,
            quantity=quantity,
            runner_quantity=trading.runner_quantity,
            buy_kind=kind,
            buy_limit=limit,
        )
        position.apply_preview(preview)
        # Pozice si drží vlastní odběr dat, nezávisle na osudu náhledu
        self._subscribe(position)

        order = self.ib.build_buy_order(
            quantity, limit, order_ref(position.id, position.buy_ref_kind)
        )
        position.buy_trade = self.ib.place(position.option_contract, order)
        position.set_state(
            PositionState.BUYING,
            f"Nákupní příkaz v trhu: {quantity} ks za LMT {cislo_text(limit)} "
            f"({price_kind_label(kind)}).",
        )

        self.positions[position.id] = position
        self.log_event(f"{position.id}: {position.message}")
        self._persist()
        return position

    def pending_buy(self, symbol: str, right: str) -> Position | None:
        """
        Najde pozici s nevyplněným nákupním příkazem na daný ticker a typ opce.

        Rozhraní podle ní pozná, že opakovaný stisk nákupního tlačítka nemá
        zakládat druhou pozici, ale přecenit příkaz, který v trhu už čeká.
        """
        for position in self.positions.values():
            if position.state == PositionState.BUYING and position.symbol == symbol:
                if position.right == right:
                    return position
        return None

    def shown_preview(self, symbol: str, right: str) -> Preview | None:
        """
        Náhled, podle kterého se obchodník rozhodoval, když mačkal tlačítko.

        Slouží k porovnání s tím, co vybere příprava při stisku. Náhled
        patřící jinému tickeru nebo směru se nepočítá - o tom, co je teď
        na obrazovce, nevypovídá.
        """
        preview = self._preview
        if preview is None or not preview.ready:
            return None
        if preview.symbol != symbol or preview.right != right:
            return None
        return preview

    def transfer_preview(self, position: Position) -> Preview | None:
        """
        Náhled míří na jiný kontrakt, než na kterém visí nevyplněný příkaz.

        Vrací připravený náhled, pokud se příkaz má přenést, a None, když se
        má jen přecenit - tedy chybí-li náhled, patří-li jinému tickeru nebo
        směru, nebo míří-li na tentýž kontrakt. Jediné místo, kde se mezi
        přecenění a přenosem rozhoduje: ptá se odsud stisk tlačítka i
        rozhraní, které podle toho ukazuje cenu a nápovědu.
        """
        preview = self.shown_preview(position.symbol, position.right)
        if preview is None or preview.option.conId == position.option_conid:
            return None
        return preview

    async def reprice_buy(
        self, position_id: str, kind: str, quantity: int | None = None
    ) -> Position:
        """
        Přecení nevyplněný nákupní příkaz na aktuální cenu, případně změní
        i počet kontraktů.

        Řeší situaci, kdy limitnímu příkazu utekl trh: příkaz se v TWS jen
        upraví (stejné orderId), nezakládá se druhý. Vyplněný ani rušený
        příkaz upravit nelze - v takovém případě metoda skončí chybou
        a nic dalšího do trhu neposílá.
        """
        if kind not in calc.BUY_KINDS:
            raise ValueError(f"Neznámý druh nákupu: {kind}")
        if not self.ib.connected:
            raise RuntimeError("Není navázáno spojení s TWS.")

        position = self.position(position_id)
        # Stav příkazu se srovná s TWS ještě před úpravou - vyplněný příkaz
        # se přeceňovat nesmí, jinak by z přecenění vznikl druhý nákup
        self._refresh_orders()
        if position.state != PositionState.BUYING:
            raise ValueError(
                f"Nákupní příkaz pozice {position_id} už v trhu není "
                f"({position.state.label}) - druhý nákup se nezadává."
            )

        # Přecenění vychází z čerstvých kotací, ne z ceny při zadání
        self._refresh_market_data(position)
        limit = self.position_buy_limit(position, kind)
        if limit is None:
            raise ValueError(
                "Z TWS nedorazila potřebná kotace opce - novou limitní cenu nákupu "
                "nelze určit."
            )

        vyplneno = self._require_modifiable(position.buy_trade, "Nákupní")

        mnozstvi = position.quantity if quantity is None else int(quantity)
        if mnozstvi < vyplneno:
            raise ValueError(
                f"Z příkazu je už nakoupeno {vyplneno} ks - na méně jej zmenšit nelze. "
                f"Zadejte alespoň {vyplneno} ks, nebo příkaz zrušte."
            )

        order = position.buy_trade.order
        position.buy_kind = kind
        # Beze změny ceny i počtu není co upravovat. Zbytečná modifikace by
        # příkaz v TWS jen znovu poslala do fronty a mohl by ztratit pořadí.
        if order.lmtPrice == limit and int(order.totalQuantity) == mnozstvi:
            position.touch(
                f"Nákupní příkaz zůstává beze změny: {mnozstvi} ks za LMT "
                f"{cislo_text(limit)}."
            )
            self.log_event(f"{position.id}: {position.message}")
            # Do TWS se nic neposílá, touch() ale přepsal hlášku i updated_at -
            # bez uložení by se po pádu aplikace obnovil starší stav
            self._persist()
            return position

        order.lmtPrice = limit
        order.totalQuantity = mnozstvi
        # Odeslání příkazu se stejným orderId znamená v TWS jeho modifikaci
        position.buy_trade = self.ib.place(position.option_contract, order)
        position.quantity = mnozstvi
        position.buy_limit = limit

        # U částečně vyplněného příkazu se přeceňuje jen zbytek, což musí být vidět
        popis_zbytku = (
            f"zbývá {mnozstvi - vyplneno} z {mnozstvi} ks (nakoupeno {vyplneno} ks)"
            if vyplneno
            else f"{mnozstvi} ks"
        )
        position.touch(
            f"Nákupní příkaz přeceněn: {popis_zbytku} za LMT {cislo_text(limit)} "
            f"({price_kind_label(kind)})."
        )
        self.log_event(f"{position.id}: {position.message}")
        self._persist()
        return position

    @staticmethod
    def _require_modifiable(trade: Any, popis: str) -> int:
        """
        Ověří, že příkaz v TWS lze ještě přecenit, a vrátí počet už vyplněných kusů.

        Částečně vyplněný příkaz se přecenit smí - mění se limitní cena
        zbývajícího množství, vyplněné kusy zůstávají nakoupené (resp. prodané)
        za svou cenu. Nový celkový počet ale nesmí klesnout pod už vyplněný,
        to hlídá volající.

        TWS může modifikaci odmítnout hlášením "too late to replace", pokud
        příkaz zrovna dobíhá. Chyba se objeví v logu a příkaz zůstane v trhu
        v původní podobě - nic se tím nerozbije, jen se nepřecení.
        """
        if trade is None:
            raise ValueError(f"{popis} příkaz v TWS nenalezen - přecenit jej nelze.")

        status = trade.orderStatus
        if status.status not in MODIFIABLE_ORDER_STATES:
            raise ValueError(
                f"{popis} příkaz je ve stavu {status.status} - TWS jeho úpravu nedovolí."
            )
        return int(status.filled)

    def _refresh_orders(self) -> bool:
        """
        Srovná stav nevyřízených příkazů se skutečností v TWS.

        Volá se před každým zadáním příkazu: vyplnění, které z TWS právě
        dorazilo, se tak stihne promítnout dřív, než by stisk tlačítka
        založil druhý nákup nebo prodej.
        """
        zmena = False
        for position in list(self.positions.values()):
            try:
                if position.state == PositionState.BUYING:
                    zmena |= self._handle_buying(position)
                elif position.state == PositionState.SELLING:
                    zmena |= self._handle_selling(position)
            except Exception:
                log.exception("Stav příkazů pozice %s se nepodařilo obnovit.", position.id)
        if zmena:
            self._persist()
        return zmena

    # ------------------------------------------------------------------
    # Prodej
    # ------------------------------------------------------------------

    async def sell(
        self,
        position_id: str,
        kind: str,
        scope: str,
        reprice: bool = False,
        markup_pct: float = 0.0,
    ) -> Position:
        """
        Zadá limitní prodejní příkaz na drženou pozici.

        kind  = 'bid' prodává na nabízené ceně, 'mid' na středu trhu.
        scope = 'all' prodává celou drženou pozici, 'base' jen základní část,
        takže v trhu zůstane runner (počet kontraktů z konfigurace), a 'one'
        jediný kontrakt - pro odprodávání pozice po kusech.
        markup_pct zvedne limitní cenu o zadaná procenta nad zvolenou cenou -
        prodej nad středem trhu vynese víc, ale vyplní se hůř.

        reprice říká, že rozhraní vykreslilo tlačítko nad pozicí, která už
        prodejní příkaz v trhu měla. Opakovaný stisk pak příkaz jen přecení
        na aktuální cenu (a případně změní počet kusů), místo aby zakládal
        druhý. Pokud se stav pozice mezitím změnil - příkaz se vyplnil, nebo
        naopak nový vznikl - prodej se odmítne, aby stisk tlačítka neskončil
        druhým, nechtěným prodejem.
        """
        if kind not in calc.SELL_KINDS:
            raise ValueError(f"Neznámý druh prodeje: {kind}")
        if scope not in (SELL_SCOPE_ALL, SELL_SCOPE_BASE, SELL_SCOPE_ONE):
            raise ValueError(f"Neznámý rozsah prodeje: {scope}")
        if not self.ib.connected:
            raise RuntimeError("Není navázáno spojení s TWS.")

        position = self.position(position_id)
        # Stav příkazů se srovná s TWS ještě před rozhodnutím, aby se právě
        # doručené vyplnění projevilo dřív, než by vznikl další příkaz
        self._refresh_orders()

        if reprice and position.state != PositionState.SELLING:
            raise ValueError(
                f"Prodejní příkaz pozice {position_id} už v trhu není "
                f"({position.state.label}) - znovu se neprodává. "
                f"Zkontrolujte pozici a případně zadejte prodej znovu."
            )
        if not reprice and position.state == PositionState.SELLING:
            raise ValueError(
                f"Pozice {position_id} má v trhu prodejní příkaz - druhý se nezadává. "
                f"Opakovaným stiskem téhož tlačítka jej lze přecenit."
            )
        if position.state not in (PositionState.OPEN, PositionState.SELLING):
            raise ValueError(
                f"Pozice {position_id} není otevřená ({position.state.label}) - "
                f"prodej nelze zadat."
            )

        mnozstvi = position.sell_quantity_for(scope)
        if mnozstvi < 1:
            raise ValueError("Není co prodat - pozice nedrží dost kontraktů.")

        # Prodej vychází z čerstvých kotací, ne z hodnot uložených při nákupu
        self._refresh_market_data(position)
        limit = self.position_sell_limit(position, kind, markup_pct)
        if limit is None:
            raise ValueError(
                "Z TWS nedorazila potřebná kotace opce - limitní cenu prodeje nelze určit."
            )

        if position.state == PositionState.SELLING:
            return self._reprice_sell(position, kind, scope, mnozstvi, limit, markup_pct)

        position.sell_seq += 1
        position.sell_kind = kind
        position.sell_scope = scope
        position.sell_markup_pct = markup_pct
        position.sell_limit = limit
        position.sell_quantity = mnozstvi
        # Zúčtování běžícího prodeje začíná od nuly, součty za dřívější
        # prodeje zůstávají v sold_quantity a sold_value
        position.sell_settled_quantity = 0
        position.sell_settled_value = 0.0
        position.settle_wait_since = None

        order = self.ib.build_sell_order(
            mnozstvi, limit, order_ref(position.id, position.sell_ref_kind)
        )
        position.sell_trade = self.ib.place(position.option_contract, order)

        zbytek = zbytek_text(position, scope, mnozstvi)
        popis_zbytku = f", {zbytek}" if zbytek else ""
        position.set_state(
            PositionState.SELLING,
            f"Prodejní příkaz v trhu: {mnozstvi} ks za LMT {cislo_text(limit)} "
            f"({price_kind_label(kind, markup_pct)}){_popis_vysledku(position, limit, mnozstvi)}"
            f"{popis_zbytku}.",
        )
        self.log_event(f"{position.id}: {position.message}")
        self._persist()
        return position

    def _reprice_sell(
        self,
        position: Position,
        kind: str,
        scope: str,
        quantity: int,
        limit: float,
        markup_pct: float = 0.0,
    ) -> Position:
        """
        Přecení nevyplněný prodejní příkaz na aktuální cenu.

        Umí i přejít mezi rozsahy - z prodeje základní pozice na prodej všeho
        a zpět; příkaz se v TWS jen upraví (stejné orderId), druhý nevzniká.

        quantity je počet kusů, které se mají teprve prodat. Celkový objem
        příkazu proto musí zahrnout i kusy, které z něj už odešly - jinak by
        se příkaz zmenšil pod vyplněné množství a TWS by úpravu odmítla.
        """
        self._require_modifiable(position.sell_trade, "Prodejní")

        celkem = position.sell_settled_quantity + quantity
        order = position.sell_trade.order
        position.sell_kind = kind
        position.sell_scope = scope
        position.sell_markup_pct = markup_pct
        # Beze změny ceny i počtu se do TWS nic neposílá - viz přecenění nákupu
        if order.lmtPrice == limit and int(order.totalQuantity) == celkem:
            position.touch(
                f"Prodejní příkaz zůstává beze změny: {quantity} ks za LMT "
                f"{cislo_text(limit)}{_popis_vysledku(position, limit, quantity)}."
            )
            self.log_event(f"{position.id}: {position.message}")
            # Viz přecenění nákupu - změněná hláška a čas patří na disk
            self._persist()
            return position

        order.lmtPrice = limit
        order.totalQuantity = celkem
        position.sell_trade = self.ib.place(position.option_contract, order)
        position.sell_limit = limit
        position.sell_quantity = celkem

        zbytek = zbytek_text(position, scope, quantity)
        popis_zbytku = f", {zbytek}" if zbytek else ""
        prodano = (
            f" (prodáno {position.sell_settled_quantity} ks)"
            if position.sell_settled_quantity
            else ""
        )
        position.touch(
            f"Prodejní příkaz přeceněn: {quantity} ks za LMT {cislo_text(limit)} "
            f"({price_kind_label(kind, markup_pct)})"
            f"{_popis_vysledku(position, limit, quantity)}{prodano}{popis_zbytku}."
        )
        self.log_event(f"{position.id}: {position.message}")
        self._persist()
        return position

    async def _transfer_buy(
        self, position: Position, quantity: int, kind: str, preview: Preview
    ) -> Position:
        """
        Přepíše nevyplněný nákup na kontrakt, který právě nabízí náhled.

        Strike se u zadaného příkazu měnit nedá, proto se v TWS příkaz zruší
        a zadá znovu. V přehledu ale zůstává táž pozice - jen změní kontrakt,
        takže se obchod neroztrhne na zrušenou a novou kartu.

        Nový příkaz odchází teprve po potvrzení zrušení z TWS: jinak by v trhu
        mohly viset oba naráz. Vyplní-li se ten původní dřív, pozice zůstane
        na svém kontraktu a nepřepisuje se.
        """
        if position.buy_trade is None:
            raise ValueError(
                f"Pozice {position.id} nemá v trhu nákupní příkaz - "
                f"na {preview.contract_label} se nepřepisuje."
            )

        puvodni = position.contract_label
        novy = preview.contract_label

        # Limit se počítá z čerstvých kotací ještě před zrušením - bez ceny
        # by se pozice ocitla bez příkazu a nový by nebylo z čeho zadat
        self._read_quotes(preview)
        limit = self.preview_buy_limit(kind)
        if limit is None:
            raise ValueError(
                f"Z TWS nedorazila kotace {novy} - příkaz na {puvodni} zůstává "
                f"beze změny."
            )

        # Po dobu přepisu se zrušení nevyhodnocuje jako konec pozice; kdyby
        # mezitím tikla monitorovací smyčka, uzavřela by ji
        position.transferring = True
        try:
            self.ib.cancel(position.buy_trade)
            position.touch(f"Ruší se nákupní příkaz, strike se přepisuje na {novy}.")
            self.log_event(f"{position.id}: {position.message}")

            # Stav se čte před každým čekáním, takže potvrzené zrušení
            # nezdrží ani o jedno kolo
            status = position.buy_trade.orderStatus
            for _ in range(int(TRANSFER_CANCEL_WAIT_SEC / TRANSFER_POLL_SEC)):
                if status.filled >= 1 or status.status in DEAD_ORDER_STATES:
                    break
                await asyncio.sleep(TRANSFER_POLL_SEC)

            if status.filled >= 1:
                raise ValueError(
                    f"Příkaz se mezitím vyplnil - pozice drží {puvodni} a na "
                    f"{novy} se nepřepisuje."
                )
            if status.status not in DEAD_ORDER_STATES:
                raise ValueError(
                    f"TWS zrušení příkazu nepotvrdila - pozice zůstává na "
                    f"{puvodni}. Zkuste to za chvíli znovu."
                )

            # Odsud dál se pozice jen přepisuje na nový kontrakt a hned dostane
            # svůj příkaz; mezi tím se nečeká, aby nezůstala bez příkazu
            self._release(position)
            position.apply_preview(preview)
            position.quantity = quantity
            position.buy_kind = kind
            position.buy_limit = limit
            # Nový příkaz musí mít vlastní značku, jinak by obnova po restartu
            # našla pod toutéž značkou i ten zrušený
            position.buy_seq += 1
            self._subscribe(position)

            order = self.ib.build_buy_order(
                quantity, limit, order_ref(position.id, position.buy_ref_kind)
            )
            position.buy_trade = self.ib.place(position.option_contract, order)
            position.set_state(
                PositionState.BUYING,
                f"Strike přepsán z {puvodni} na {novy}: nákupní příkaz v trhu, "
                f"{quantity} ks za LMT {cislo_text(limit)} "
                f"({price_kind_label(kind)}).",
            )
        finally:
            position.transferring = False

        self.log_event(f"{position.id}: {position.message}")
        self._persist()
        return position

    async def cancel_order(self, position_id: str) -> Position:
        """
        Stáhne z trhu nevyřízený příkaz pozice.

        Skutečné zrušení potvrdí až TWS, proto se stav mění teprve
        v monitorovací smyčce podle hlášení z TWS.
        """
        position = self.position(position_id)
        if position.state == PositionState.BUYING:
            self.ib.cancel(position.buy_trade)
            position.touch("Ruší se nákupní příkaz, čeká se na potvrzení z TWS.")
        elif position.state == PositionState.SELLING:
            self.ib.cancel(position.sell_trade)
            position.touch("Ruší se prodejní příkaz, čeká se na potvrzení z TWS.")
        else:
            raise ValueError(f"Pozice {position_id} nemá v trhu žádný příkaz ke zrušení.")

        self.log_event(f"{position.id}: {position.message}")
        self._persist()
        return position

    def remove_finished(self) -> int:
        """
        Odstraní z přehledu všechny ukončené pozice - uzavřené, zrušené
        i chybové. Aktivních se nedotkne a do TWS neposílá nic; jde čistě
        o úklid obrazovky. Vrací počet odstraněných záznamů.
        """
        ukoncene = [p for p in self.positions.values() if not p.state.is_active]
        for position in ukoncene:
            self._release(position)
            del self.positions[position.id]

        if ukoncene:
            self.log_event(f"Z přehledu {ukoncene_pozice_text(len(ukoncene))}.")
            self._persist()
        return len(ukoncene)

    def remove_position(self, position_id: str) -> None:
        """Odstraní ukončenou pozici z přehledu."""
        position = self.position(position_id)
        if position.state.is_active:
            raise ValueError("Aktivní pozici nelze z přehledu odstranit.")
        self._release(position)
        del self.positions[position_id]
        self.log_event(f"{position_id}: pozice odstraněna z přehledu.")
        self._persist()

    # ------------------------------------------------------------------
    # Odběry tržních dat
    # ------------------------------------------------------------------

    def _subscribe(self, position: Position) -> None:
        """Zahájí odběr dat kontraktů pozice, pokud už neběží."""
        if position.subscribed:
            return
        self.ib.subscribe(position.underlying_contract)
        self.ib.subscribe(position.option_contract)
        position.subscribed = True

    def _release(self, position: Position) -> None:
        """Uvolní odběr dat kontraktů ukončené pozice."""
        if not position.subscribed:
            return
        position.subscribed = False
        self.ib.unsubscribe(position.option_contract)
        self.ib.unsubscribe(position.underlying_contract)

    # ------------------------------------------------------------------
    # Monitorovací smyčka
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spustí periodickou monitorovací smyčku."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Zastaví monitorovací smyčku i rozdělaný dotaz na velikost účtu."""
        for uloha in (self._task, self._account_task):
            if uloha is None:
                continue
            uloha.cancel()
            try:
                await uloha
            except asyncio.CancelledError:
                pass
        self._task = None
        self._account_task = None

    async def _run(self) -> None:
        """Hlavní smyčka - periodicky prochází aktivní pozice a hlídá spojení."""
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Chyba v monitorovací smyčce.")
            await asyncio.sleep(self.cfg.engine.poll_interval_sec)

    async def _tick(self) -> None:
        """Jeden průchod monitoringem všech aktivních pozic."""
        # Během obnovy se nemonitoruje - příkazy z minulého spojení neplatí
        if self._restore_lock.locked():
            return

        if not self.ib.connected:
            # Po obnovení spojení se pozice musí znovu spárovat s příkazy v TWS
            self._synced = False
            # Selhání obnovy patřilo ke ztracenému spojení; na novém se hlásí znovu
            self._last_restore_error = 0.0
            if self.cfg.connection.auto_reconnect and self.auto_connect:
                await self._try_reconnect()
            return

        # Velikost účtu se přebírá bez ohledu na stav srovnání pozic - přehled
        # výsledků ji potřebuje i tehdy, když se obnova nedaří
        self._start_account_refresh()

        # Po (znovu)navázání spojení se stav srovná se skutečností v TWS
        if not self._synced:
            await self._sync_with_tws()
            return

        # Provize dorazí z TWS až po vyplnění příkazu, proto se dobírají průběžně
        zmena = self._sync_commissions()

        # Neřízené pozice se objevují i mizí za běhu - obchodník s nimi hýbe
        # přímo v TWS a zavřít se může i pozice řízená aplikací. Čte se paměť
        # spojení, takže to do TWS neposílá žádný dotaz.
        self._warn_unmanaged(self.ib.known_positions())

        for position in list(self.positions.values()):
            if not position.state.is_active:
                continue
            try:
                zmena |= self._monitor(position)
            except Exception as exc:
                log.exception("Chyba při monitoringu pozice %s.", position.id)
                position.set_state(PositionState.ERROR, f"Chyba monitoringu: {exc}")
                zmena = True

        if zmena:
            self._persist()

    def _start_account_refresh(self) -> None:
        """
        Je-li na čase, spustí převzetí velikosti účtu vedle smyčky.

        Dotaz do TWS čeká na odpověď až ACCOUNT_TIMEOUT_SEC; kdyby se na něj
        ve smyčce čekalo, o tu dobu by se zdrželo sledování pozic, na kterém
        záleží víc. Hodnota se mění s každým obchodem i s pohybem otevřených
        pozic, proto se obnovuje v tempu engine.account_refresh_sec; nula
        přebírání vypne. Dokud hodnota není známa, zkouší se to častěji - bez
        ní přehled výsledků procenta z účtu nespočítá.
        """
        interval = self.cfg.engine.account_refresh_sec
        if interval <= 0:
            return
        # Nový dotaz nemá smysl, dokud předchozí čeká na odpověď
        if self._account_task is not None and not self._account_task.done():
            return
        if self._live_account_size is None:
            interval = min(interval, ACCOUNT_RETRY_SEC)

        ted = time.monotonic()
        if self._account_checked and ted - self._account_checked < interval:
            return
        self._account_checked = ted
        self._account_task = asyncio.create_task(self._refresh_account_size())

    async def _refresh_account_size(self) -> None:
        """
        Převezme z TWS skutečnou velikost účtu (NetLiquidation).
        O tom, kdy se ptát, rozhoduje _start_account_refresh.
        """
        # Běží se mimo smyčku, která chyby loguje za celý průchod - chybu
        # dotazu proto musí zachytit tahle metoda sama
        try:
            hodnota = await self.ib.net_liquidation()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Velikost účtu se nepodařilo z TWS převzít.")
            return

        if hodnota is None:
            return
        # O převzetí se hlásí jen poprvé - další obnovy jsou rutina
        if self._live_account_size is None:
            self.log_event(f"Velikost účtu převzata z TWS: {cislo_text(hodnota)} USD.")
        self._live_account_size = hodnota

    async def _sync_with_tws(self) -> None:
        """
        Srovná pozice se skutečností v TWS po (znovu)navázání spojení.

        Neúspěch se nesmí umlčet - dokud srovnání neproběhne, aplikace neví,
        co účet skutečně drží. Pokus se proto opakuje, ale ne při každém
        průchodu smyčkou: obnova sahá do TWS a při trvalé chybě by odtud
        tahala data každou sekundu.
        """
        ted = time.monotonic()
        if (
            self._last_restore_error
            and ted - self._last_restore_error < self.cfg.connection.reconnect_delay_sec
        ):
            return

        try:
            await self.restore()
            self._last_restore_error = 0.0
        except Exception as exc:
            log.exception("Stav pozic se nepodařilo srovnat s TWS.")
            # Do přehledu se hlásí jen první selhání, opakované pokusy by jej zaplavily
            if not self._last_restore_error:
                self.log_event(
                    f"Stav pozic se nepodařilo srovnat s TWS: {exc} - zkouším to dál."
                )
            self._last_restore_error = ted

    async def _try_reconnect(self) -> None:
        """Pokusí se obnovit spojení, ne však častěji než jednou za nastavený interval."""
        ted = time.monotonic()
        if ted - self._last_reconnect < self.cfg.connection.reconnect_delay_sec:
            return
        self._last_reconnect = ted
        try:
            await self.ib.connect()
            self.log_event("Spojení s TWS obnoveno.")
        except Exception as exc:
            log.debug("Obnovení spojení s TWS se nezdařilo: %s", exc)

    def _monitor(self, position: Position) -> bool:
        """
        Jeden krok stavového automatu pozice.
        Vrací True, pokud došlo ke změně, která se má promítnout do rozhraní.
        """
        # Kotace se jen načtou. Do uloženého stavu nepatří a rozhraní se
        # překresluje vlastním časovačem, takže samy o sobě nejsou změnou,
        # kvůli které by se měl přepisovat soubor se stavem.
        self._refresh_market_data(position)

        zmena = False
        if position.state == PositionState.BUYING:
            zmena |= self._handle_buying(position)
        elif position.state == PositionState.SELLING:
            zmena |= self._handle_selling(position)

        return zmena

    def _refresh_market_data(self, position: Position) -> None:
        """Načte do pozice aktuální cenu podkladu a kotace opce."""
        cena = self.ib.underlying_price(position.underlying_contract)
        bid, ask, delta = self.ib.option_quotes(position.option_contract)

        if cena is not None:
            position.underlying_price = cena
        position.option_bid = bid
        position.option_ask = ask
        if delta is not None:
            position.delta = delta

    def _handle_buying(self, position: Position) -> bool:
        """Sleduje nákupní příkaz - vyplnění, částečné vyplnění i zrušení."""
        trade = position.buy_trade
        if trade is None:
            return False

        # Přepis na jiný strike zrušený příkaz vzápětí nahradí novým; kdyby se
        # zrušení vyhodnotilo teď, pozice by se uzavřela uprostřed přepisu
        if position.transferring:
            return False

        status = trade.orderStatus
        vyplneno = int(status.filled)
        cena = valid_price(status.avgFillPrice) or position.buy_limit

        zmena = False
        if vyplneno != position.filled_quantity:
            position.filled_quantity = vyplneno
            position.fill_price = cena
            if position.fill_time is None and vyplneno > 0:
                position.fill_time = datetime.now()
            zmena = True

        if vyplneno >= position.quantity or status.status == "Filled":
            if vyplneno >= 1:
                position.set_state(
                    PositionState.OPEN,
                    f"Nakoupeno {vyplneno} ks za {cislo_text(cena or 0.0)}.",
                )
                self.log_event(f"{position.id}: {position.message}")
                return True

        if status.status in DEAD_ORDER_STATES:
            if vyplneno >= 1:
                # Částečné vyplnění je platná pozice, jen menší, než se zadávalo
                position.set_state(
                    PositionState.OPEN,
                    f"Nákup ukončen po částečném vyplnění - drží se {vyplneno} z "
                    f"{position.quantity} ks za {cislo_text(cena or 0.0)}.",
                )
            else:
                position.set_state(
                    PositionState.CANCELLED,
                    f"Nákupní příkaz byl zrušen v TWS ({status.status}), nic se nenakoupilo.",
                )
                self._release(position)
            self.log_event(f"{position.id}: {position.message}")
            return True

        return zmena

    def _handle_selling(self, position: Position) -> bool:
        """Sleduje prodejní příkaz a zúčtovává prodané kontrakty."""
        trade = position.sell_trade
        if trade is None:
            return False

        status = trade.orderStatus
        vyplneno = int(status.filled)
        # Dokud skutečná průměrná cena nedorazí, počítá se s limitní cenou
        skutecna = valid_price(status.avgFillPrice)
        cena = skutecna or position.sell_limit
        zmena = self._settle_sell(position, vyplneno, cena)

        hotovo = vyplneno >= position.sell_quantity or status.status == "Filled"
        if not hotovo and status.status not in DEAD_ORDER_STATES:
            return zmena

        # Cena vyplnění chodí z TWS o kousek později než hlášení o vyplnění.
        # Uzavřít pozici hned by znamenalo napsat do realizovaného výsledku
        # odhad podle limitu a už jej nikdy neopravit - ze stavu 'prodává se'
        # pozice odejde a _handle_selling se na ni znovu nepodívá.
        if vyplneno >= 1 and skutecna is None and not self._settle_wait_over(position):
            return zmena

        # Příkaz doběhl (vyplněn, nebo z trhu stažen) - pozice se vrací
        # do klidového stavu podle toho, co v ní zůstalo
        position.sell_trade = None
        zbyva = position.open_quantity

        if zbyva <= 0:
            position.set_state(
                PositionState.CLOSED,
                f"Pozice uzavřena, prodáno celkem {position.sold_quantity} ks "
                f"za průměr {cislo_text(position.sold_value / max(1, position.sold_quantity))}.",
            )
            self._release(position)
        elif vyplneno >= 1:
            position.set_state(
                PositionState.OPEN,
                f"Prodáno {vyplneno} ks za {cislo_text(cena or 0.0)}, "
                f"v pozici zůstává {zbyva} ks.",
            )
        else:
            position.set_state(
                PositionState.OPEN,
                f"Prodejní příkaz byl zrušen ({status.status}), pozice dál drží {zbyva} ks.",
            )
        self.log_event(f"{position.id}: {position.message}")
        return True

    @staticmethod
    def _settle_wait_over(position: Position) -> bool:
        """
        Vypršel odklad, po který se čeká na skutečnou prodejní cenu z TWS?

        Odklad musí být omezený: kdyby cena nikdy nedorazila (zvláštní stav
        příkazu, výpadek spojení), zůstala by pozice navěky ve stavu
        'Prodává se' a nešlo by s ní nic dělat.
        """
        if position.settle_wait_since is None:
            position.settle_wait_since = time.monotonic()
            return False
        return time.monotonic() - position.settle_wait_since >= FILL_PRICE_WAIT_SEC

    def _settle_sell(self, position: Position, vyplneno: int, cena: float | None) -> bool:
        """
        Zúčtuje nově prodané kontrakty běžícího příkazu.

        TWS hlásí průměrnou cenu celého příkazu, proto se z ní počítá celková
        hodnota prodeje a do součtů pozice se přidává jen rozdíl oproti tomu,
        co už zúčtováno bylo. Částečné plnění po částech tak sedí přesně.

        Skutečná průměrná cena dorazí z TWS často až po hlášení o vyplnění;
        do té doby se počítá s odhadem podle limitní ceny. Zúčtování se proto
        opakuje i při nezměněném počtu kusů, změnila-li se hodnota - jinak by
        v realizovaném výsledku natrvalo zůstal odhad místo skutečné ceny.
        """
        if cena is None or vyplneno < position.sell_settled_quantity:
            return False

        hodnota_celkem = vyplneno * cena
        # Beze změny počtu i hodnoty není co zúčtovat
        if (
            vyplneno == position.sell_settled_quantity
            and abs(hodnota_celkem - position.sell_settled_value) < 1e-9
        ):
            return False

        position.sold_quantity += vyplneno - position.sell_settled_quantity
        position.sold_value += hodnota_celkem - position.sell_settled_value
        position.sell_settled_quantity = vyplneno
        position.sell_settled_value = hodnota_celkem
        return True

    def _sync_commissions(self) -> bool:
        """
        Převezme z TWS provize účtované k příkazům aplikace.

        Podle druhu příkazu se ukládají zvlášť nákupní a prodejní - přehled
        výsledků je rozděluje mezi uzavřenou a otevřenou část pozice.
        """
        zmena = False
        for position_id, zaznamy in self.ib.commissions().items():
            position = self.positions.get(position_id)
            if position is None:
                continue
            for exec_id, (druh, castka) in zaznamy.items():
                prodej = druh.startswith("sell")
                cil = position.sell_commissions if prodej else position.buy_commissions
                opacny = (
                    position.buy_commissions if prodej else position.sell_commissions
                )
                # Starší uložený stav vedl provize bez rozlišení druhu a všechny
                # skončily mezi nákupními. Jakmile TWS exekuci pošle znovu i
                # s druhem, musí z opačného slovníku zmizet - jinak by se táž
                # provize započítala dvakrát.
                if opacny.pop(exec_id, None) is not None:
                    zmena = True
                if cil.get(exec_id) == castka:
                    continue
                cil[exec_id] = castka
                zmena = True
        return zmena

    # ------------------------------------------------------------------
    # Obnova po restartu a po výpadku spojení
    # ------------------------------------------------------------------

    async def restore(self) -> None:
        """
        Obnoví pozice z uloženého stavu a srovná je se skutečností v TWS.

        Uložený soubor říká, jaké pozice aplikace vedla; závazné jsou ale
        příkazy a držené kontrakty v TWS.
        """
        async with self._restore_lock:
            await self._restore_locked()

    async def _restore_locked(self) -> None:
        """Vlastní obnova; běží pod zámkem, aby se nekřížila se smyčkou."""
        if self._synced:
            return

        prikazy = await self.ib.app_trades()
        drzene = await self.ib.positions()

        # Ze souboru se čte jen při prvním spuštění; při dalším připojení
        # je stav v paměti aktuálnější než ten uložený
        ulozene: list[Position] = []
        if not self._restored:
            self._restored = True
            if self.cfg.state.enabled:
                ulozene = store.load(self.cfg.state.file)
                if ulozene:
                    self.log_event(
                        f"Obnovuji {len(ulozene)} uložených pozic a ověřuji je v TWS."
                    )
        elif self.positions:
            self.log_event("Spojení navázáno, ověřuji stav pozic v TWS.")

        znovu = ulozene + [
            p for p in self.positions.values() if p.state.is_active and p not in ulozene
        ]

        for position in znovu:
            # Ukončené pozice se jen vrátí do přehledu, nic se u nich neověřuje
            if not position.state.is_active:
                self.positions[position.id] = position
                continue
            try:
                await self._restore_position(position, prikazy, drzene)
            except Exception as exc:
                log.exception("Pozici %s se nepodařilo obnovit.", position.id)
                position.set_state(PositionState.ERROR, f"Obnova pozice selhala: {exc}")
                self.log_event(f"{position.id}: obnova selhala - {exc}")
            self.positions[position.id] = position

        # Odběry tržních dat zanikly s minulým spojením; náhled o tom neví
        self._restore_preview()

        # Držené množství podle TWS je závazné; srovnává se až teď, kdy jsou
        # obnovené všechny pozice - na jednom kontraktu jich může běžet víc
        self._reconcile_positions(drzene)

        # Opční pozice, ke kterým se nepodařilo přiřadit záznam, aplikace neřídí
        self._warn_unmanaged(drzene)

        # Číslování dalších pozic musí navázat za obnovené záznamy
        nejvyssi = 0
        for position in self.positions.values():
            cast = position.id.rsplit("-", 1)[-1]
            if cast.isdigit():
                nejvyssi = max(nejvyssi, int(cast))
        self._ids = itertools.count(nejvyssi + 1)

        if self.positions:
            self._persist()

        # Za srovnané se sezení označí až tady. Kdyby se příznak nastavil na
        # začátku, jediná výjimka v průběhu obnovy by jej nechala natrvalo
        # zapnutý a držené množství by se s TWS už nikdy neporovnalo.
        self._synced = True

    def _restore_preview(self) -> None:
        """
        Obnoví odběr tržních dat připraveného náhledu.

        Se ztrátou spojení odběry zanikly, náhled se ale dál tváří, že je drží.
        Bez obnovy by zůstal bez kotací: v přehledu i na tlačítkách by svítily
        pomlčky a nákup by skončil hláškou o chybějící kotaci, dokud by
        obchodník ticker ve formuláři nepřepsal.

        Kontrakty, které odběr ještě mají, se přeskakují - druhý odběr by jen
        zvýšil počítadlo odběratelů a při uvolnění náhledu by se nezrušil.
        """
        preview = self._preview
        if preview is None or not preview.owns_subscription:
            return
        for kontrakt in (preview.underlying, preview.option):
            if kontrakt is not None and not self.ib.is_subscribed(kontrakt):
                self.ib.subscribe(kontrakt)

    async def _restore_position(
        self, position: Position, prikazy: dict, drzene: dict
    ) -> None:
        """Obnoví jednu pozici - kontrakty, odběry dat a skutečný stav příkazů."""
        # Kontrakty je nutné znovu ověřit, runtime objekty se neukládají
        position.underlying_contract = await self.ib.qualify_stock(position.symbol)
        option, detaily = await self.ib.qualify_option(
            position.symbol, position.expiration, position.strike, position.right
        )
        position.option_contract = option
        position.option_conid = option.conId
        position.underlying_conid = position.underlying_contract.conId
        position.min_tick = detaily.minTick or position.min_tick
        position.subscribed = False
        self._subscribe(position)

        position.buy_trade = prikazy.get(order_ref(position.id, position.buy_ref_kind))
        position.sell_trade = (
            prikazy.get(order_ref(position.id, position.sell_ref_kind))
            if position.sell_seq
            else None
        )

        info = drzene.get(option.conId)
        drzeno = int(info.quantity) if info else 0

        # Nákup bez příkazu v TWS: buď se stihl vyplnit, nebo zmizel.
        # Kolik z drženého množství patří právě této pozici, se rozhodne až
        # při srovnání po kontraktech; tady se bere nejvýš to, co účet drží.
        # Kdyby se vzalo celé zadané množství, srovnání by u nedoplněného
        # příkazu zaúčtovalo rozdíl jako prodej, který nikdy neproběhl.
        if position.state == PositionState.BUYING and position.buy_trade is None:
            if drzeno >= 1:
                position.filled_quantity = position.filled_quantity or min(
                    position.quantity, drzeno
                )
                position.fill_price = position.fill_price or position.buy_limit
                position.set_state(
                    PositionState.OPEN,
                    f"Nákupní příkaz už v TWS není, na účtu kontrakt je - pozice "
                    f"se považuje za nakoupenou. Ověřte množství i cenu v TWS.",
                )
            else:
                position.set_state(
                    PositionState.CANCELLED,
                    "Nákupní příkaz v TWS nenalezen a na účtu není žádná pozice.",
                )
                self._release(position)
            self.log_event(f"{position.id}: {position.message}")
            return

        # Prodej bez příkazu v TWS: co ubylo z účtu, se považuje za prodané
        if position.state == PositionState.SELLING and position.sell_trade is None:
            position.set_state(
                PositionState.OPEN,
                "Prodejní příkaz už v TWS není, stav pozice se přebírá z účtu.",
            )
            self.log_event(f"{position.id}: {position.message}")

    def _reconcile_positions(self, drzene: dict[int, PositionInfo]) -> None:
        """
        Srovná držené množství podle TWS s tím, co evidují pozice.

        Na jednom opčním kontraktu může běžet víc pozic - třeba když se
        dokupovalo po částech - jenže TWS hlásí jediný součet za celý
        kontrakt. Množství se proto porovnává po kontraktech: dokud součet
        sedí, nemění se nic. Bez toho by si každá pozice nárokovala celé
        držené množství a aplikace by evidovala násobek skutečnosti.
        """
        podle_kontraktu: dict[int, list[Position]] = {}
        for position in self.positions.values():
            if position.state.is_active and position.option_conid:
                podle_kontraktu.setdefault(position.option_conid, []).append(position)

        for conid, pozice in podle_kontraktu.items():
            # Pořadí vzniku rozhoduje, ze které pozice se případný rozdíl bere
            pozice.sort(key=lambda p: p.created_at)
            info = drzene.get(conid)
            drzeno = int(info.quantity) if info else 0
            ocekavano = sum(p.open_quantity for p in pozice)
            if drzeno != ocekavano:
                self._apply_quantity_difference(pozice, drzeno - ocekavano)

    def _apply_quantity_difference(self, pozice: list[Position], rozdil: int) -> None:
        """
        Srovná rozdíl mezi TWS a evidencí u pozic jednoho kontraktu.

        TWS má vždy pravdu. Chybějící kusy se odepíšou od nejnovější pozice
        (u té je nejpravděpodobnější, že se s ní hýbalo naposledy) a považují
        se za prodané za odhadnutou cenu; přebývající se k nejnovější pozici
        přidají. Obojí se hlásí, protože skutečné ceny aplikace nezná.

        Pozice s nevyplněným nákupem se neupravují - jejich množství si řídí
        příkaz v TWS a monitorovací smyčka by zásah stejně přepsala. Běží-li
        na kontraktu takový příkaz, nepřipisuje se ani přebytek: patří nejspíš
        právě jemu a jinde by se tytéž kontrakty započítaly podruhé. Co se
        nepodaří přiřadit, se hlásí do přehledu událostí.
        """
        popis = ", ".join(p.id for p in pozice)
        # Pozice s příkazem v trhu si množství doplní sama z hlášení TWS
        nakupujici = [p for p in pozice if p.state == PositionState.BUYING]
        upravitelne = [
            p for p in pozice
            if p.state in (PositionState.OPEN, PositionState.SELLING)
        ]
        if not upravitelne:
            self.log_event(
                f"POZOR: držené množství v TWS neodpovídá evidenci ({popis}), "
                f"rozdíl {rozdil:+d} ks. Pozice mají v trhu nákupní příkaz, "
                f"proto se nic neupravuje - zkontrolujte je v TWS."
            )
            return

        if rozdil > 0:
            # Běží-li na kontraktu nákupní příkaz, přebytek nejspíš patří jemu:
            # vyplnil se dřív, než o něm aplikace stihla vědět. Monitorovací
            # smyčka si množství převezme z TWS sama, a kdyby se přebytek zatím
            # připsal jiné pozici, byly by tytéž kontrakty započítané dvakrát.
            if nakupujici:
                self.log_event(
                    f"POZOR: na účtu je o {rozdil} ks více, než pozice evidují "
                    f"({popis}). Na kontraktu běží nákupní příkaz, přebytek se "
                    f"proto přiřadí až podle jeho vyplnění."
                )
                return

            # Na účtu je víc kontraktů - dokupovalo se mimo aplikaci
            cil = upravitelne[-1]
            cil.filled_quantity += rozdil
            cil.touch(
                f"POZOR: na účtu je o {rozdil} ks více, než pozice evidovala - "
                f"množství se převzalo z TWS. Zkontrolujte cenu nákupu."
            )
            self.log_event(f"{cil.id}: {cil.message}")
            return

        # Na účtu je méně kontraktů - prodávalo se mimo aplikaci
        chybi = -rozdil
        for position in reversed(upravitelne):
            if chybi <= 0:
                break
            ubrat = min(chybi, position.open_quantity)
            if ubrat <= 0:
                continue

            odhad = position.sell_limit or position.mid or position.fill_price or 0.0
            position.sold_quantity += ubrat
            position.sold_value += ubrat * odhad
            chybi -= ubrat
            position.touch(
                f"POZOR: na účtu je o {ubrat} ks méně, než pozice evidovala - "
                f"kusy se považují za prodané za odhadovaných {cislo_text(odhad)}. "
                f"Zkontrolujte skutečné ceny v TWS."
            )
            self.log_event(f"{position.id}: {position.message}")
            if position.open_quantity <= 0:
                position.set_state(PositionState.CLOSED)
                self._release(position)

        # Zbytek schodku nemá kam jít - kusy si nárokuje pozice s nevyplněným
        # nákupem, do které se nezasahuje. Evidence tak zůstává nad skutečností
        # a obchodník se to musí dozvědět.
        if chybi > 0:
            self.log_event(
                f"POZOR: na účtu chybí ještě {chybi} ks oproti evidenci "
                f"({popis}) a není je odkud odepsat - zkontrolujte pozice v TWS."
            )

    def _unmanaged_positions(
        self, drzene: dict[int, PositionInfo]
    ) -> dict[int, PositionInfo]:
        """
        Vybere z držených pozic ty, které aplikace neřídí - tedy ty, na jejichž
        kontraktu nesedí žádná její aktivní pozice.

        Jediné místo, kde se "neřízená pozice" definuje: čte to jak upozornění
        v rozhraní, tak vyhýbání se obsazeným kontraktům při výběru strike.
        """
        rizene = {
            p.option_conid
            for p in self.positions.values()
            if p.state.is_active and p.option_conid
        }
        return {conid: info for conid, info in drzene.items() if conid not in rizene}

    def _warn_unmanaged(self, drzene: dict[int, PositionInfo]) -> None:
        """
        Zaznamená opční pozice na účtu, které aplikace neřídí.

        Nastává, když se ztratí uložený stav nebo když se obchoduje ručně
        přímo v TWS. Aplikace k nim sama nic nezadává, jen na ně upozorní.
        """
        self.unmanaged = self._unmanaged_positions(drzene)

    async def _occupied_conids(self, refresh: bool = False) -> dict[int, str]:
        """
        Opční kontrakty, na které si nárokuje místo někdo jiný.

        Skládá se ze tří zdrojů: živé příkazy v TWS bez značky této aplikace
        (druhá aplikace u stejné TWS, nebo příkaz zadaný ručně v okně TWS),
        držené pozice, které aplikace neřídí, a obchody zamluvené v uloženém
        stavu jiné aplikace. Vlastní příkazy ani vlastní pozice se do výsledku
        nedostanou - do svého kontraktu musí jít dokupovat dál.

        Klíčem je conId, hodnotou popis pro hlášení obchodníkovi - včetně
        druhu zdroje, takže z hlášky je poznat, jestli kontrakt drží cizí
        příkaz, neřízená pozice, nebo obchod jiné aplikace. Parametr refresh
        si vynutí čerstvý dotaz do TWS místo krátké paměti, kterou si služba
        drží kvůli opakované přípravě zadání; stavu jiné aplikace se netýká,
        ten se stejně čte ze souboru při každém volání.
        """
        # Vypnuté vyhýbání se řeší tady, aby o konfiguraci nemusel vědět
        # každý volající zvlášť; prázdný výsledek výběr striku neomezí
        if not self.cfg.strike.avoid_occupied:
            return {}

        # Každý popis říká i to, čím je kontrakt obsazený - hláška pro
        # obchodníka pak nemusí vyjmenovávat všechny možnosti a rovnou
        # ukáže, kde kolizi hledat
        obsazene = {
            conid: f"cizí příkaz {popis}".strip()
            for conid, popis in (await self.ib.foreign_order_conids(refresh)).items()
        }

        # Neřízené pozice na účtu - tytéž, na které upozorňuje _warn_unmanaged.
        # Čte se paměť spojení, kterou naplnila obnova po připojení; příkaz
        # v TWS má přednost, protože nese čerstvější popis kontraktu.
        for conid, info in self._unmanaged_positions(self.ib.known_positions()).items():
            obsazene.setdefault(conid, f"neřízená pozice {info.label}".strip())

        # Obchody, které si jiná aplikace drží ve svém stavu, aniž by je
        # zadala do TWS - typicky obchod čekající na zúžení spreadu. V TWS
        # takový kontrakt nic neobsazuje, přesto na něj druhá aplikace míří,
        # a nakoupené kusy by se v TWS sečetly do jediné pozice.
        for conid, popis in self.reserved.conids().items():
            obsazene.setdefault(conid, popis)
        return obsazene
