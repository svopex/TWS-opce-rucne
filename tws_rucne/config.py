"""Načítání, validace a ukládání konfiguračního souboru aplikace."""

from __future__ import annotations

import logging
import shutil
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

log = logging.getLogger(__name__)

# Povolené režimy výběru expirace
EXPIRATION_MODES = ("nearest", "fixed")
# Povolené režimy výběru strike ceny
STRIKE_MODES = ("otm_offset", "atm")
# Povolené typy opce, které lze ve formuláři zvolit
RIGHTS = ("C", "P")

# Výchozí přirážky nad poptávanou cenou v procentech - z každé vzniká jedno
# prodejní tlačítko. Platí, dokud je config nepřepíše (trading.ask_markups_pct).
DEFAULT_ASK_MARKUPS_PCT = (1.0, 2.0, 3.0, 4.0, 5.0, 7.0, 9.0)


@dataclass
class ConnectionConfig:
    """Parametry spojení na TWS / IB Gateway."""

    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 21
    readonly: bool = False
    # Prázdný řetězec = použije se první účet nalezený na spojení
    account: str = ""
    # 1 = live, 2 = frozen, 3 = delayed, 4 = delayed frozen
    market_data_type: int = 1
    connect_timeout: float = 8.0
    auto_reconnect: bool = True
    reconnect_delay_sec: float = 5.0


@dataclass
class TradingConfig:
    """Parametry obchodní logiky - příkazy, kontrakty, runner."""

    exchange: str = "SMART"
    currency: str = "USD"
    # Platnost příkazu: DAY = do konce obchodního dne, GTC = do zrušení
    tif: str = "DAY"
    outside_rth: bool = False
    # Výchozí množství předvyplněné ve formuláři
    default_quantity: int = 1
    # Výchozí typ opce ve formuláři: C = CALL, P = PUT
    default_right: str = "C"
    min_quantity: int = 1
    max_quantity: int = 100
    # Kolik kontraktů zůstane jako runner, když se prodává základní pozice.
    # Základní pozice je tedy množství snížené o tuto hodnotu.
    runner_quantity: int = 1
    # Tolerance nad ASK v procentech pro nákup "za ASK". Nula znamená limit
    # přesně na ASK; malá tolerance pomáhá projít při pohyblivé kotaci.
    ask_tolerance_pct: float = 0.0
    # Tolerance pod BID v procentech pro prodej "za BID"
    bid_tolerance_pct: float = 0.0
    # Přirážky nad poptávanou cenou v procentech (i desetinná místa).
    # Každá hodnota přidá do řádku jedno prodejní tlačítko, které prodává
    # nad ASK; pořadí v seznamu je pořadím tlačítek zleva doprava.
    ask_markups_pct: list[float] = field(
        default_factory=lambda: list(DEFAULT_ASK_MARKUPS_PCT)
    )
    # Spread, nad kterým rozhraní upozorní, že se obchod nevyplácí.
    # Nákup se ale nezakazuje - o zadání rozhoduje obchodník.
    max_spread_pct: float = 7.0
    # Časová zóna burzy - odpočet se časuje v ní, takže posuny letního
    # a zimního času vůči místnímu času počítače nehrají roli
    exchange_timezone: str = "America/New_York"
    # Čas otevření burzy ve tvaru HH:MM (v časové zóně burzy).
    # Slouží jen k odpočtu v hlavičce, obchodování neovlivňuje.
    exchange_open_time: str = "09:30"
    # Čas zavření burzy ve tvaru HH:MM (v časové zóně burzy).
    # Zkrácené obchodní dny (např. před svátky) aplikace nezná.
    exchange_close_time: str = "16:00"


@dataclass
class ExpirationConfig:
    """Výběr expirace opčního kontraktu."""

    # nearest = nejbližší expirace splňující min_dte, fixed = konkrétní datum
    mode: str = "nearest"
    min_dte: int = 0
    # Datum ve formátu YYYYMMDD, použije se pouze při mode = fixed
    fixed_date: str = ""


@dataclass
class StrikeConfig:
    """Výběr strike ceny podle aktuální ceny podkladu."""

    # otm_offset = strike odsazený od aktuální ceny mimo peníze (výchozí),
    # atm        = nejbližší strike aktuální ceně podkladu
    mode: str = "otm_offset"
    # Kolikátý strike za aktuální cenou se vybere při mode = otm_offset.
    # Počítá se v krocích rastru řetězce, takže platí pro každý ticker.
    otm_steps: int = 1
    # Vyhnout se kontraktu, na kterém už visí cizí příkaz v TWS nebo drží
    # neřízená pozice na účtu, a vybrat místo něj strike dál mimo peníze.
    # Brání tomu, aby se pozice této aplikace v TWS sečetla s pozicí jiné
    # aplikace na témže kontraktu - TWS je vede jako jedinou pozici.
    avoid_occupied: bool = True
    # Stavové soubory jiných aplikací (jejich state.json), ze kterých se čte,
    # které kontrakty si už drží jejich obchody. Obchod čekající na zúžení
    # spreadu ještě není v TWS zadaný, takže se o něm jinak nedá vědět.
    # Relativní cesta se vztahuje k adresáři, odkud je aplikace spuštěna;
    # čte se jen při zapnutém avoid_occupied.
    reserved_state_files: list[str] = field(default_factory=list)


@dataclass
class EngineConfig:
    """Časování monitorovací smyčky a čekání na tržní data."""

    poll_interval_sec: float = 1.0
    # Jak dlouho čekat na první ceny z TWS při přípravě zadání
    market_data_timeout_sec: float = 6.0
    # Dorazí-li u opce nejdřív jen poslední/závěrečná cena, kolik sekund
    # se ještě počká na úplnou kotaci BID/ASK
    quotes_grace_sec: float = 1.5
    # Jak často se z TWS přebírá velikost účtu (NetLiquidation) pro přepočet
    # výsledku na procenta účtu. Je to dotaz do TWS, proto vlastní, řidší
    # tempo; 0 přebírání vypne a přehled výsledků procenta neukáže
    account_refresh_sec: float = 60.0


@dataclass
class StateConfig:
    """Ukládání otevřených pozic na disk."""

    # false = stav se neukládá a po restartu aplikace o pozicích neví
    enabled: bool = True
    file: str = "state.json"


@dataclass
class UiConfig:
    """Parametry webového rozhraní."""

    host: str = "127.0.0.1"
    port: int = 8081
    refresh_interval_sec: float = 1.0
    dark: bool = False
    log_lines: int = 200
    # Jak často se měří odezva TWS pro ukazatel v hlavičce. Je to dotaz do
    # TWS, ne čtení z paměti jako zbytek obnovy, proto vlastní, řidší tempo;
    # 0 měření vypne a v hlavičce zůstane jen stáří tržních dat
    latency_interval_sec: float = 5.0


@dataclass
class AppConfig:
    """Kořenová konfigurace aplikace."""

    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    expiration: ExpirationConfig = field(default_factory=ExpirationConfig)
    strike: StrikeConfig = field(default_factory=StrikeConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    state: StateConfig = field(default_factory=StateConfig)
    ui: UiConfig = field(default_factory=UiConfig)


def _build(cls: type, data: Any, path: str = "") -> Any:
    """
    Sestaví dataclass ze slovníku načteného z YAML.
    Neznámý klíč pouze zaloguje varování, chybějící klíč ponechá výchozí hodnotu.
    """
    if not isinstance(data, dict):
        raise ValueError(
            f"Sekce '{path or 'root'}' musí být slovník, nalezeno: {type(data).__name__}"
        )

    kwargs: dict[str, Any] = {}
    znama = {f.name for f in fields(cls)}

    for key, value in data.items():
        if key not in znama:
            log.warning(
                "Neznámý konfigurační klíč '%s%s' - ignoruji.", f"{path}." if path else "", key
            )
            continue
        if value is not None:
            kwargs[key] = value

    return cls(**kwargs)


# Komentovaná šablona konfigurace dodávaná s aplikací
TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "config.example.yaml"


def load_config(path: str | Path) -> AppConfig:
    """
    Načte konfiguraci z YAML souboru. Pokud soubor neexistuje, založí jej
    ze šablony a vrátí výchozí konfiguraci.
    """
    cesta = Path(path)

    if not cesta.exists():
        log.info("Konfigurační soubor %s neexistuje - zakládám výchozí.", cesta)
        _create_default(cesta)

    with cesta.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    # Sekce se skládají po jedné, aby šlo hlásit neznámé klíče i s cestou
    cfg = AppConfig(
        connection=_build(ConnectionConfig, raw.get("connection", {}), "connection"),
        trading=_build(TradingConfig, raw.get("trading", {}), "trading"),
        expiration=_build(ExpirationConfig, raw.get("expiration", {}), "expiration"),
        strike=_build(StrikeConfig, raw.get("strike", {}), "strike"),
        engine=_build(EngineConfig, raw.get("engine", {}), "engine"),
        state=_build(StateConfig, raw.get("state", {}), "state"),
        ui=_build(UiConfig, raw.get("ui", {}), "ui"),
    )
    validate_config(cfg)
    return cfg


def _create_default(cesta: Path) -> None:
    """
    Založí výchozí konfigurační soubor.
    Přednostně se kopíruje komentovaná šablona config.example.yaml, aby si
    uživatel v souboru našel popis voleb; bez ní se soubor vygeneruje
    z výchozích hodnot.
    """
    cesta.parent.mkdir(parents=True, exist_ok=True)
    if TEMPLATE_PATH.exists():
        shutil.copyfile(TEMPLATE_PATH, cesta)
    else:
        save_config(AppConfig(), cesta)


def _cas_burzy(hodnota: str) -> tuple[int, int] | None:
    """
    Rozloží čas burzy ve tvaru HH:MM na hodinu a minutu.
    Vrací None, pokud zápis tomuto tvaru neodpovídá.
    """
    casti = str(hodnota).split(":")
    if len(casti) != 2:
        return None
    try:
        hodina, minuta = int(casti[0]), int(casti[1])
    except ValueError:
        return None
    if not (0 <= hodina <= 23 and 0 <= minuta <= 59):
        return None
    return hodina, minuta


def validate_config(cfg: AppConfig) -> None:
    """Zkontroluje hodnoty konfigurace a vyhodí ValueError s popisem chyby."""
    problemy: list[str] = []

    if cfg.expiration.mode not in EXPIRATION_MODES:
        problemy.append(
            f"expiration.mode musí být jedna z {EXPIRATION_MODES}, "
            f"nalezeno '{cfg.expiration.mode}'"
        )
    if cfg.expiration.mode == "fixed" and not cfg.expiration.fixed_date:
        problemy.append("expiration.fixed_date musí být vyplněné při expiration.mode = fixed")

    if cfg.strike.mode not in STRIKE_MODES:
        problemy.append(
            f"strike.mode musí být jedna z {STRIKE_MODES}, nalezeno '{cfg.strike.mode}'"
        )
    if cfg.strike.otm_steps < 0:
        problemy.append("strike.otm_steps nesmí být záporné")
    # Překlep v zápisu (jediná cesta místo seznamu) by se jinak projevil až
    # čtením neexistujících souborů podle jednotlivých písmen
    if not isinstance(cfg.strike.reserved_state_files, list) or any(
        not isinstance(cesta, str) for cesta in cfg.strike.reserved_state_files
    ):
        problemy.append("strike.reserved_state_files musí být seznam cest k souborům")

    if cfg.trading.default_right not in RIGHTS:
        problemy.append(
            f"trading.default_right musí být jedna z {RIGHTS}, "
            f"nalezeno '{cfg.trading.default_right}'"
        )
    if cfg.trading.runner_quantity < 1:
        problemy.append("trading.runner_quantity musí být alespoň 1")
    if cfg.trading.min_quantity < 1:
        problemy.append("trading.min_quantity musí být alespoň 1")
    if cfg.trading.max_quantity < cfg.trading.min_quantity:
        problemy.append("trading.max_quantity nesmí být menší než trading.min_quantity")
    if cfg.trading.ask_tolerance_pct < 0 or cfg.trading.bid_tolerance_pct < 0:
        problemy.append("tolerance nad ASK ani pod BID nesmí být záporná")

    # Přirážky nad poptávkou: jediné číslo místo seznamu nebo záporná hodnota
    # by se jinak projevily až rozsypaným řádkem tlačítek, případně prodejním
    # příkazem pod poptávanou cenou
    if not isinstance(cfg.trading.ask_markups_pct, list):
        problemy.append("trading.ask_markups_pct musí být seznam procent")
    else:
        for hodnota in cfg.trading.ask_markups_pct:
            # bool je v Pythonu podtyp int - 'true' v YAML by jinak prošlo jako 1 %
            if isinstance(hodnota, bool) or not isinstance(hodnota, (int, float)):
                problemy.append(
                    f"trading.ask_markups_pct smí obsahovat jen čísla, nalezeno '{hodnota}'"
                )
            elif hodnota <= 0:
                problemy.append(
                    f"trading.ask_markups_pct smí obsahovat jen kladná procenta, "
                    f"nalezeno {hodnota}"
                )
    if cfg.trading.tif not in ("DAY", "GTC"):
        problemy.append(f"trading.tif musí být DAY nebo GTC, nalezeno '{cfg.trading.tif}'")

    # Hodiny burzy se čtou až v periodické obnově rozhraní. Neověřený překlep
    # by tam vyhodil výjimku každého půl sekundy a shodil by celé překreslení -
    # tedy přehled pozic, ceny na tlačítkách i výpis průběhu
    try:
        ZoneInfo(cfg.trading.exchange_timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        problemy.append(
            f"trading.exchange_timezone není známá časová zóna, "
            f"nalezeno '{cfg.trading.exchange_timezone}'"
        )

    otevreni = _cas_burzy(cfg.trading.exchange_open_time)
    zavreni = _cas_burzy(cfg.trading.exchange_close_time)
    if otevreni is None:
        problemy.append(
            f"trading.exchange_open_time musí být ve tvaru HH:MM, "
            f"nalezeno '{cfg.trading.exchange_open_time}'"
        )
    if zavreni is None:
        problemy.append(
            f"trading.exchange_close_time musí být ve tvaru HH:MM, "
            f"nalezeno '{cfg.trading.exchange_close_time}'"
        )
    # Zavření před otevřením by odpočet v hlavičce nikdy nevypnulo
    if otevreni is not None and zavreni is not None and zavreni <= otevreni:
        problemy.append(
            "trading.exchange_close_time musí být později než trading.exchange_open_time"
        )

    if cfg.engine.poll_interval_sec <= 0:
        problemy.append("engine.poll_interval_sec musí být kladné")
    # Nula je povolená hodnota - vypíná přebírání velikosti účtu z TWS
    if cfg.engine.account_refresh_sec < 0:
        problemy.append("engine.account_refresh_sec nesmí být záporné")
    if cfg.ui.refresh_interval_sec <= 0:
        problemy.append("ui.refresh_interval_sec musí být kladné")

    if problemy:
        raise ValueError("Chybná konfigurace:\n- " + "\n- ".join(problemy))


def save_config(cfg: AppConfig, path: str | Path) -> None:
    """Uloží konfiguraci do YAML souboru (bez komentářů)."""
    cesta = Path(path)
    cesta.parent.mkdir(parents=True, exist_ok=True)
    with cesta.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(asdict(cfg), fh, allow_unicode=True, sort_keys=False)
