"""
Obálka nad ib_async - spojení s TWS, výběr kontraktů, tržní data a příkazy.
Všechny metody jsou asynchronní a počítají s během ve smyčce NiceGUI.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ib_async import (
    IB,
    Contract,
    ContractDetails,
    Fill,
    LimitOrder,
    Option,
    Order,
    OrderStatus,
    Stock,
    Ticker,
    Trade,
)

from .config import AppConfig

log = logging.getLogger(__name__)

# Předpona značky, kterou aplikace označuje své příkazy v poli orderRef.
# Podle ní pozná své příkazy v TWS i po restartu bez uloženého stavu.
ORDER_REF_PREFIX = "TWSRUCNE"

# Jak dlouho se nejvýš čeká na odpověď TWS při měření odezvy
RTT_TIMEOUT_SEC = 3.0

# Jak dlouho se nejvýš čeká na souhrn účtu. Dotaz visí v monitorovací
# smyčce, takže bez lhůty by mlčící TWS zastavila sledování pozic.
ACCOUNT_TIMEOUT_SEC = 5.0

# Jak dlouho platí zjištěný seznam cizích příkazů, než se vyžádá z TWS znovu.
# Příprava zadání běží po každé změně formuláře, takže bez této lhůty by
# do TWS šel dotaz při každém stisku klávesy.
FOREIGN_ORDERS_CACHE_SEC = 5.0


def order_ref(position_id: str, druh: str) -> str:
    """Sestaví značku příkazu, například 'TWSRUCNE:AAPL-1:buy'."""
    return f"{ORDER_REF_PREFIX}:{position_id}:{druh}"


def parse_order_ref(ref: str) -> tuple[str, str] | None:
    """
    Rozloží značku příkazu na identifikátor pozice a druh příkazu.
    Vrací None, pokud značka nepochází z této aplikace.
    """
    if not ref or not ref.startswith(f"{ORDER_REF_PREFIX}:"):
        return None
    casti = ref.split(":")
    if len(casti) != 3:
        return None
    return casti[1], casti[2]


@dataclass
class PositionInfo:
    """Držená opční pozice na účtu podle TWS."""

    conid: int
    quantity: float
    # Popis kontraktu, například 'META  260819P00545000'
    label: str
    symbol: str


def valid_price(value: float | None) -> float | None:
    """
    Ověří, že cena z TWS je použitelná.
    TWS posílá u chybějících kotací NaN nebo -1, takové hodnoty se zahazují.
    """
    if value is None or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return float(value)


class IBService:
    """Spravuje jedno spojení na TWS a odběry tržních dat."""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.ib = IB()
        self.account: str = cfg.connection.account
        # Odebírané tickery podle conId, aby se stejný kontrakt neodebíral vícekrát
        self._tickers: dict[int, Ticker] = {}
        # Počet odběratelů kontraktu - odběr se ruší až s tím posledním
        self._subscribers: dict[int, int] = {}
        # Kontrakty, u kterých už odklad na úplné kotace proběhl. Příprava
        # zadání se volá po každé změně formuláře a bez této paměti by se
        # u opce bez kotací čekalo znovu při každém stisku klávesy.
        self._quotes_grace_done: set[int] = set()
        self._connect_lock = asyncio.Lock()
        self._chain_cache: dict[str, Any] = {}
        # Naposledy zjištěné cizí příkazy a čas jejich zjištění -
        # viz foreign_order_conids a FOREIGN_ORDERS_CACHE_SEC
        self._foreign_conids: dict[int, str] = {}
        self._foreign_checked: float = 0.0
        # Poslední naměřená odezva TWS v milisekundách; None znamená, že se
        # zatím neměřilo, nebo že poslední pokus neuspěl. Drží se tady, aby
        # ji synchronní obnova hlavičky mohla jen přečíst
        self.rtt_ms: float | None = None

        self.ib.disconnectedEvent += self._on_disconnected
        self.ib.errorEvent += self._on_error

    # ------------------------------------------------------------------
    # Spojení
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """Stav spojení na TWS."""
        return self.ib.isConnected()

    async def connect(self) -> None:
        """
        Naváže spojení na TWS a nastaví typ tržních dat.
        Opakované volání při již navázaném spojení nic neprovede.
        """
        async with self._connect_lock:
            if self.ib.isConnected():
                return

            conn = self.cfg.connection
            log.info(
                "Připojuji se k TWS %s:%s (clientId=%s)", conn.host, conn.port, conn.client_id
            )
            await self.ib.connectAsync(
                host=conn.host,
                port=conn.port,
                clientId=conn.client_id,
                timeout=conn.connect_timeout,
                readonly=conn.readonly,
                account=conn.account,
            )
            # Typ tržních dat - live / frozen / delayed podle konfigurace
            self.ib.reqMarketDataType(conn.market_data_type)

            # Účet z konfigurace má přednost, jinak se použije první dostupný
            if not self.account:
                ucty = self.ib.managedAccounts()
                self.account = ucty[0] if ucty else ""

            log.info("Spojení navázáno, účet: %s", self.account or "(neurčen)")

    async def disconnect(self) -> None:
        """Ukončí spojení a zruší všechny odběry tržních dat."""
        for conid in list(self._tickers):
            self._cancel_ticker(conid)
        self._tickers.clear()
        self._subscribers.clear()
        if self.ib.isConnected():
            self.ib.disconnect()

    def _on_disconnected(self) -> None:
        """Reakce na ztrátu spojení - odběry z minulého spojení už neplatí."""
        log.warning("Spojení s TWS bylo přerušeno.")
        self._tickers.clear()
        self._subscribers.clear()
        self._quotes_grace_done.clear()
        # Cizí příkazy se po novém spojení musí zjistit znovu
        self._foreign_conids.clear()
        self._foreign_checked = 0.0
        # Naměřená odezva patřila ke ztracenému spojení
        self.rtt_ms = None

    def _on_error(self, reqId: int, errorCode: int, errorString: str, contract: Any) -> None:
        """
        Logování chyb z TWS. Kódy 2100-2199 jsou pouze informativní hlášení
        (například stav datového spojení), proto se logují jen jako info.
        """
        if 2100 <= errorCode < 2200:
            log.info("TWS info %s: %s", errorCode, errorString)
        else:
            popis = getattr(contract, "localSymbol", "") if contract is not None else ""
            popis = f" [{popis}]" if popis else ""
            log.error("TWS chyba %s (reqId=%s)%s: %s", errorCode, reqId, popis, errorString)

    # ------------------------------------------------------------------
    # Kvalita spojení
    # ------------------------------------------------------------------

    async def measure_rtt(self) -> float | None:
        """
        Změří odezvu TWS v milisekundách a zapamatuje ji do self.rtt_ms.

        Dotaz na aktuální čas je nejlevnější zpráva API - jde tam a zpět bez
        tržních dat, takže měří, jak rychle TWS odpovídá. Neměří síť k IB:
        běží-li TWS na tomtéž stroji, je to odezva samotné aplikace, tedy
        ukazatel, že není zatuhlá.

        Nedostupná odpověď (odpojení, vypršení lhůty) vrací None a nic
        neloguje - je to údaj o spojení, ne chyba, a měření se opakuje.
        """
        if not self.ib.isConnected():
            self.rtt_ms = None
            return None

        start = time.perf_counter()
        try:
            await asyncio.wait_for(self.ib.reqCurrentTimeAsync(), RTT_TIMEOUT_SEC)
        except Exception:
            self.rtt_ms = None
            return None

        self.rtt_ms = (time.perf_counter() - start) * 1000
        return self.rtt_ms

    def quotes_age(self) -> float | None:
        """
        Stáří tržních dat v sekundách - kolik uplynulo od nejčerstvější
        kotace napříč všemi odebíranými kontrakty.

        Odpovídá na otázku „teče proud dat?". Bere se nejnovější čas, ne
        nejstarší: jednotlivá nelikvidní opce se aktualizuje zřídka i při
        zcela zdravém spojení, kdežto stojící maximum znamená, že nepřichází
        nic. Mimo obchodní hodiny proto hodnota přirozeně roste.

        None znamená, že se nic neodebírá nebo že žádný ticker zatím čas
        nemá - tedy že se není z čeho ptát.
        """
        casy = [t.time for t in self._tickers.values() if t.time is not None]
        if not casy:
            return None

        nejnovejsi = max(casy)
        # ib_async dodává časy v UTC s časovou zónou; testovací náhrady je
        # mívají bez ní, proto se „teď" bere ve stejné podobě jako kotace
        ted = datetime.now(nejnovejsi.tzinfo)
        # Hodiny TWS mohou být napřed - záporné stáří by mátlo víc než nula
        return max(0.0, (ted - nejnovejsi).total_seconds())

    # ------------------------------------------------------------------
    # Účet
    # ------------------------------------------------------------------

    async def net_liquidation(self) -> float | None:
        """
        Aktuální likvidační hodnota účtu (NetLiquidation) z TWS v USD.

        Je to skutečná velikost účtu včetně otevřených pozic - přehled
        výsledků z ní počítá procenta z účtu. None znamená, že hodnota
        není k dispozici (bez spojení, nebo ji TWS neposlala).
        """
        if not self.connected:
            return None
        try:
            hodnoty = await asyncio.wait_for(
                self.ib.accountSummaryAsync(self.account), ACCOUNT_TIMEOUT_SEC
            )
        except asyncio.TimeoutError:
            # Mlčící TWS není chyba aplikace - hodnota se zkusí příště znovu
            log.warning(
                "TWS neodpověděla na dotaz na souhrn účtu do %s s.", ACCOUNT_TIMEOUT_SEC
            )
            return None
        except Exception:
            log.exception("Souhrn účtu se nepodařilo z TWS načíst.")
            return None

        for hodnota in hodnoty:
            if hodnota.tag == "NetLiquidation":
                try:
                    return float(hodnota.value)
                except (TypeError, ValueError):
                    return None
        return None

    # ------------------------------------------------------------------
    # Kontrakty
    # ------------------------------------------------------------------

    async def qualify_stock(self, symbol: str) -> Contract:
        """Doplní identifikátory akciového kontraktu podle tickeru."""
        akcie = Stock(symbol.upper().strip(), self.cfg.trading.exchange, self.cfg.trading.currency)
        overene = await self.ib.qualifyContractsAsync(akcie)
        if not overene or overene[0] is None:
            raise ValueError(f"Ticker '{symbol}' se nepodařilo najít v TWS.")
        return overene[0]

    async def option_chain(self, underlying: Contract) -> Any:
        """
        Načte parametry opčního řetězce pro podklad (expirace a strike ceny).
        Výsledek se kešuje, protože se v průběhu dne nemění.
        """
        klic = f"{underlying.symbol}:{underlying.conId}"
        if klic in self._chain_cache:
            return self._chain_cache[klic]

        retezce = await self.ib.reqSecDefOptParamsAsync(
            underlying.symbol, "", underlying.secType, underlying.conId
        )
        if not retezce:
            raise ValueError(f"Pro ticker '{underlying.symbol}' nejsou dostupné opce.")

        # Preferuje se řetězec na SMART s obchodní třídou shodnou s tickerem
        # (standardní opce), teprve pak cokoliv dalšího
        vybrane = [
            c for c in retezce if c.exchange == "SMART" and c.tradingClass == underlying.symbol
        ]
        if not vybrane:
            vybrane = [c for c in retezce if c.exchange == "SMART"]
        if not vybrane:
            vybrane = retezce

        retezec = max(vybrane, key=lambda c: len(c.expirations))
        self._chain_cache[klic] = retezec
        return retezec

    async def qualify_option(
        self, symbol: str, expiration: str, strike: float, right: str, trading_class: str = ""
    ) -> tuple[Contract, ContractDetails]:
        """
        Sestaví a ověří opční kontrakt, vrátí kontrakt včetně detailů
        (kvůli minimálnímu tiku pro zaokrouhlování limitních cen).
        """
        opce = Option(
            symbol=symbol,
            lastTradeDateOrContractMonth=expiration,
            strike=strike,
            right=right,
            exchange=self.cfg.trading.exchange,
            currency=self.cfg.trading.currency,
        )
        if trading_class:
            opce.tradingClass = trading_class

        detaily = await self.ib.reqContractDetailsAsync(opce)
        if not detaily:
            raise ValueError(
                f"Opční kontrakt {symbol} {expiration} {right} {strike:g} není v TWS dostupný."
            )
        # Při více variantách se bere ta s nejmenším multiplikátorem (standardní kontrakt)
        detail = min(detaily, key=lambda d: float(d.contract.multiplier or 100))
        return detail.contract, detail

    # ------------------------------------------------------------------
    # Tržní data
    # ------------------------------------------------------------------

    def subscribe(self, contract: Contract) -> Ticker | None:
        """
        Zahájí (nebo znovu použije) odběr tržních dat kontraktu.
        Počítadlo odběratelů zajišťuje, že se data zruší až po posledním zájemci.
        """
        conid = contract.conId
        if conid in self._tickers:
            self._subscribers[conid] = self._subscribers.get(conid, 0) + 1
            return self._tickers[conid]

        ticker = self.ib.reqMktData(contract, "", False, False)
        self._tickers[conid] = ticker
        self._subscribers[conid] = 1
        return ticker

    def unsubscribe(self, contract: Contract | None) -> None:
        """Sníží počet odběratelů kontraktu a při nule zruší odběr dat."""
        if contract is None:
            return
        conid = contract.conId
        if conid not in self._subscribers:
            return

        self._subscribers[conid] -= 1
        if self._subscribers[conid] <= 0:
            self._cancel_ticker(conid)

    def _cancel_ticker(self, conid: int) -> None:
        """Zruší odběr tržních dat daného kontraktu."""
        ticker = self._tickers.pop(conid, None)
        self._subscribers.pop(conid, None)
        if ticker is None or not self.ib.isConnected():
            return
        try:
            self.ib.cancelMktData(ticker.contract)
        except Exception:
            log.exception("Nepodařilo se zrušit odběr tržních dat conId=%s.", conid)

    def is_subscribed(self, contract: Contract | None) -> bool:
        """
        Odebírá se právě tržní data tohoto kontraktu?

        Po ztrátě spojení se počítadlo odběratelů vyprázdní, takže odpověď
        rozliší odběr, který ještě žije, od toho, co po výpadku zbylo jen
        v paměti volajícího.
        """
        return contract is not None and contract.conId in self._subscribers

    def ticker(self, contract: Contract | None) -> Ticker | None:
        """Vrátí odebíranou strukturu s cenami daného kontraktu."""
        if contract is None:
            return None
        return self._tickers.get(contract.conId)

    def underlying_price(self, contract: Contract | None) -> float | None:
        """
        Aktuální cena podkladu.
        Přednost má poslední obchod, následuje střed trhu a nakonec závěrečná cena.
        """
        ticker = self.ticker(contract)
        if ticker is None:
            return None

        # midpoint() a marketPrice() jsou metody, markPrice je datové pole
        for kandidat in (ticker.last, ticker.midpoint(), ticker.markPrice, ticker.close):
            cena = valid_price(kandidat)
            if cena is not None:
                return cena
        return None

    def option_quotes(
        self, contract: Contract | None
    ) -> tuple[float | None, float | None, float | None]:
        """Vrátí trojici (bid, ask, delta) opčního kontraktu z odebíraných dat."""
        ticker = self.ticker(contract)
        if ticker is None:
            return None, None, None

        bid = valid_price(ticker.bid)
        ask = valid_price(ticker.ask)

        # Delta z modelu TWS, při nedostupnosti z posledního obchodu nebo kotací
        delta = None
        for greeks in (ticker.modelGreeks, ticker.lastGreeks, ticker.bidGreeks, ticker.askGreeks):
            if greeks is not None and greeks.delta is not None and math.isfinite(greeks.delta):
                delta = float(greeks.delta)
                break

        return bid, ask, delta

    async def wait_for_quotes(
        self, contract: Contract, timeout: float, quotes_grace: float = 0.0
    ) -> None:
        """
        Počká, než TWS pošle první použitelná data kontraktu.

        Vrací ihned, jakmile jsou k dispozici obě strany kotace (BID i ASK) -
        bez nich nelze spočítat střed trhu. Dorazí-li nejdřív jen jiná cena,
        čeká se ještě quotes_grace sekund, aby úplné kotace dostaly šanci.
        Po vypršení časového limitu se pokračuje i bez dat; odklad se u téhož
        kontraktu čeká jen jednou, jinak by se opakoval při každé změně formuláře.
        """
        conid = getattr(contract, "conId", 0)
        if conid in self._quotes_grace_done:
            quotes_grace = 0.0

        smycka = asyncio.get_running_loop()
        konec = smycka.time() + timeout
        prvni_cena: float | None = None
        while smycka.time() < konec:
            ticker = self.ticker(contract)
            if ticker is not None:
                if valid_price(ticker.bid) is not None and valid_price(ticker.ask) is not None:
                    return
                # Jakákoliv jiná použitelná cena - čeká se ještě odklad na kotace
                if any(
                    valid_price(v) is not None
                    for v in (ticker.bid, ticker.ask, ticker.last, ticker.close)
                ):
                    if prvni_cena is None:
                        prvni_cena = smycka.time()
                    if smycka.time() - prvni_cena >= quotes_grace:
                        if conid:
                            self._quotes_grace_done.add(conid)
                        return
            await asyncio.sleep(0.2)

    # ------------------------------------------------------------------
    # Příkazy
    # ------------------------------------------------------------------

    def _finish_order(self, order: Order, ref: str) -> Order:
        """Doplní příkazu společné atributy: platnost, obchodní hodiny, účet a značku."""
        order.tif = self.cfg.trading.tif
        order.outsideRth = self.cfg.trading.outside_rth
        if ref:
            order.orderRef = ref
        if self.account:
            order.account = self.account
        return order

    def build_buy_order(self, quantity: int, limit_price: float, ref: str = "") -> Order:
        """Limitní nákupní příkaz na opci - cena podle zvoleného tlačítka (ASK / MID)."""
        return self._finish_order(LimitOrder("BUY", quantity, limit_price), ref)

    def build_sell_order(self, quantity: int, limit_price: float, ref: str = "") -> Order:
        """Limitní prodejní příkaz na opci - cena podle zvoleného tlačítka (BID / MID)."""
        return self._finish_order(LimitOrder("SELL", quantity, limit_price), ref)

    def place(self, contract: Contract, order: Order) -> Trade:
        """Odešle příkaz do TWS a vrátí objekt sledující jeho stav."""
        return self.ib.placeOrder(contract, order)

    def cancel(self, trade: Trade | None) -> None:
        """Zruší dříve zadaný příkaz, pokud je v TWS ještě aktivní."""
        if trade is None or not self.ib.isConnected():
            return
        # Vyplněný, zrušený i rušený příkaz TWS odmítá hlášením,
        # které by uživateli vyskočilo na obrazovku
        if trade.orderStatus.status not in OrderStatus.ActiveStates:
            return
        try:
            self.ib.cancelOrder(trade.order)
        except Exception:
            log.exception("Příkaz orderId=%s se nepodařilo zrušit.", trade.order.orderId)

    async def _reload_open_orders(self, kde: str) -> bool:
        """
        Vyžádá si z TWS otevřené příkazy včetně těch od ostatních klientů.

        Parametr kde pojmenuje volajícího, aby se v logu poznalo, který dotaz
        selhal. Vrací True při úspěchu; jak se naloží s neúspěchem, rozhoduje
        volající - obnova pokračuje s tím, co spojení ví, hledání cizích
        příkazů se drží posledního známého stavu.
        """
        try:
            await self.ib.reqAllOpenOrdersAsync()
            return True
        except Exception:
            log.exception("Otevřené příkazy se nepodařilo z TWS načíst (%s).", kde)
            return False

    async def app_trades(self) -> dict[str, Trade]:
        """
        Vrátí příkazy založené touto aplikací, klíčované značkou z orderRef.
        Používá se po restartu k dohledání příkazů, které v TWS zůstaly.

        Načítají se i dokončené příkazy - podle vyplněného nákupu aplikace
        pozná, že jí patří otevřená pozice.
        """
        await self._reload_open_orders("obnova příkazů aplikace")
        try:
            # apiOnly=False vrací i příkazy zadané ručně v TWS, filtruje se dál podle značky
            await self.ib.reqCompletedOrdersAsync(False)
        except Exception:
            log.exception("Dokončené příkazy se nepodařilo z TWS načíst.")

        nalezene: dict[str, Trade] = {}
        for trade in self.ib.trades():
            ref = trade.order.orderRef or ""
            if parse_order_ref(ref) is None:
                continue
            # Pod jednou značkou může být více záznamů; přednost má ten vyplněnější
            drivejsi = nalezene.get(ref)
            if drivejsi is not None and drivejsi.orderStatus.filled >= trade.orderStatus.filled:
                continue
            nalezene[ref] = trade
        return nalezene

    async def foreign_order_conids(self, refresh: bool = False) -> dict[int, str]:
        """
        Opční kontrakty, na kterých v TWS visí živý příkaz cizího původu.

        Vrací conId kontraktu a popis pro hlášení. Za cizí se považuje každý
        příkaz bez značky této aplikace v orderRef - tedy i příkaz druhé
        aplikace připojené ke stejné TWS, i příkaz zadaný ručně v okně TWS.
        Vlastní příkazy se vynechávají schválně: do svého kontraktu musí jít
        dokupovat dál.

        Dotaz reqAllOpenOrders vrací i příkazy ostatních klientů TWS. Měnit
        ani rušit je nelze, ke zjištění obsazeného kontraktu ale stačí.
        Výsledek platí FOREIGN_ORDERS_CACHE_SEC, protože příprava zadání
        se volá po každé změně formuláře. Před zadáním příkazu do trhu se
        volá s refresh=True, kde na čerstvosti záleží víc než na počtu dotazů.
        """
        loop = asyncio.get_running_loop()
        if (
            not refresh
            and self._foreign_checked
            and loop.time() - self._foreign_checked < FOREIGN_ORDERS_CACHE_SEC
        ):
            return dict(self._foreign_conids)

        # Bez odpovědi se raději vrátí poslední známý stav než prázdno - jinak
        # by výpadek dotazu tiše vypnul vyhýbání se cizím kontraktům
        if not await self._reload_open_orders("hledání cizích příkazů"):
            return dict(self._foreign_conids)

        nalezene: dict[int, str] = {}
        for trade in self.ib.trades():
            contract = trade.contract
            if contract is None or contract.secType != "OPT" or not contract.conId:
                continue
            # Vlastní příkazy pozná aplikace podle značky v orderRef
            if parse_order_ref(trade.order.orderRef or "") is not None:
                continue
            # Vyplněný, zrušený ani zamítnutý příkaz už kontrakt neblokuje
            if trade.orderStatus.status not in OrderStatus.ActiveStates:
                continue
            nalezene[contract.conId] = contract.localSymbol or contract.symbol

        self._foreign_conids = nalezene
        self._foreign_checked = loop.time()
        return dict(nalezene)

    def _raw_fills(self) -> list[Fill]:
        """
        Vyplnění příkazů, o kterých spojení ví.

        Po navázání spojení si ib_async vyžádá exekuce celého obchodního dne,
        takže seznam obsahuje i vyplnění z doby před startem aplikace.
        Vyčleněno do vlastní metody kvůli testům, které TWS nahrazují.
        """
        return list(self.ib.fills())

    def commissions(self) -> dict[str, dict[str, tuple[str, float]]]:
        """
        Provize skutečně účtované TWS, roztříděné podle pozic aplikace.

        Vrací identifikátor pozice -> {execId: (druh příkazu, provize v USD)}.
        Druh je poslední část značky orderRef ('buy', 'sell1', ...), podle níž
        se odliší provize za nákup od provizí za prodeje - přehled výsledků je
        rozděluje mezi uzavřenou a otevřenou část pozice. Klíčem je
        identifikátor exekuce, takže opakované načtení téhož vyplnění hodnotu
        jen přepíše. Cizí příkazy se přeskakují.

        Zpráva o provizi dorazí z TWS až krátce po vyplnění; do té doby
        se záznam do výsledku nedostane.
        """
        nalezene: dict[str, dict[str, tuple[str, float]]] = {}
        for fill in self._raw_fills():
            rozklad = parse_order_ref(fill.execution.orderRef or "")
            if rozklad is None:
                continue
            castka = fill.commissionReport.commission
            # TWS u neznámé provize posílá "nenastavenou" hodnotu 1.8e308,
            # před doručením zprávy je hodnota nulová - obojí se zahazuje
            if not isinstance(castka, (int, float)) or not math.isfinite(castka) or not castka:
                continue
            position_id, druh = rozklad
            nalezene.setdefault(position_id, {})[fill.execution.execId] = (
                druh,
                float(castka),
            )
        return nalezene

    async def positions(self) -> dict[int, PositionInfo]:
        """
        Vyžádá si z TWS aktuální stav účtu a vrátí držené opční pozice.
        Používá se při obnově, kde na čerstvosti údajů závisí srovnání evidence.
        """
        try:
            await self.ib.reqPositionsAsync()
        except Exception:
            log.exception("Pozice se nepodařilo z TWS načíst.")
            return {}
        return self.known_positions()

    def known_positions(self) -> dict[int, PositionInfo]:
        """
        Držené opční pozice podle conId, jak je spojení eviduje právě teď.

        Čte jen paměť ib_async, která se po prvním dotazu udržuje hlášeními
        z TWS - proto ji smí volat i monitorovací smyčka při každém průchodu,
        aniž by tím do TWS posílala dotaz.
        """
        return {
            p.contract.conId: PositionInfo(
                conid=p.contract.conId,
                quantity=p.position,
                label=p.contract.localSymbol or p.contract.symbol,
                symbol=p.contract.symbol,
            )
            for p in self.ib.positions()
            if p.contract.secType == "OPT" and p.position
        }
