"""Central configuration for the SMC trading system.

Every tunable value lives here. Secrets (MT5 password, Telegram token) are
read from environment variables / a ``.env`` file and are never hard-coded.

Later phases extend these dataclasses rather than scattering constants through
the code base. Build the config with :func:`load_config`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time as dtime
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from smc.core.exceptions import ConfigError
from smc.core.timeframes import StructureLayer, Timeframe

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Phrase that must be present in SMC_LIVE_CONFIRM before live orders can be enabled.
LIVE_CONFIRM_PHRASE = "I_ACCEPT_LIVE_ORDER_RISK"
# Hard ceiling on per-trade risk, independent of configuration.
ABSOLUTE_MAX_RISK_PCT = 1.0

try:  # optional dependency: python-dotenv
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:  # pragma: no cover
    pass


# ----------------------------------------------------------------- env helpers
def _env_str(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int = 0) -> int:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"Environment variable {key} must be an integer, got '{raw}'") from exc


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


# ------------------------------------------------------------------ sections
@dataclass
class BrokerConfig:
    """MetaTrader 5 terminal / account settings."""

    login: int = 0  # 0 -> use whatever account the terminal is already logged into
    password: str = field(default="", repr=False)
    server: str = ""
    terminal_path: str = ""  # e.g. C:\Program Files\MetaTrader 5\terminal64.exe ("" = auto)
    timeout_ms: int = 60_000
    max_connect_retries: int = 5
    retry_delay_seconds: float = 3.0
    # MT5 timestamps are in *broker server time*. Offset (hours) from UTC, e.g. 0
    # for servers on UTC, 2/3 for typical EET/EEST servers. Verify on your broker.
    server_utc_offset_hours: int = 0

    @classmethod
    def from_env(cls) -> "BrokerConfig":
        return cls(
            login=_env_int("MT5_LOGIN", 0),
            password=_env_str("MT5_PASSWORD"),
            server=_env_str("MT5_SERVER"),
            terminal_path=_env_str("MT5_PATH"),
            server_utc_offset_hours=_env_int("MT5_SERVER_UTC_OFFSET", 0),
        )


class AssetClass(Enum):
    FOREX = "forex"
    METAL = "metal"
    CRYPTO = "crypto"


@dataclass
class SymbolConfig:
    """One tradable instrument."""

    name: str  # canonical name, e.g. "XAUUSD"
    asset_class: AssetClass
    suffix: str = ""  # broker suffix, e.g. "m" -> "XAUUSDm"
    enabled: bool = True

    @property
    def broker_symbol(self) -> str:
        return f"{self.name}{self.suffix}"

    @property
    def is_crypto(self) -> bool:
        return self.asset_class is AssetClass.CRYPTO


@dataclass
class TimeframeConfig:
    """Which timeframes belong to which structure layer."""

    external: tuple[Timeframe, ...] = (Timeframe.H4, Timeframe.M30)
    internal: tuple[Timeframe, ...] = (Timeframe.M15, Timeframe.M5)

    @property
    def all(self) -> tuple[Timeframe, ...]:
        """All timeframes, highest first (so higher TFs are processed first)."""
        return tuple(sorted({*self.external, *self.internal}, key=lambda t: -t.minutes))

    def layer_of(self, tf: Timeframe) -> StructureLayer:
        if tf in self.external:
            return StructureLayer.EXTERNAL
        if tf in self.internal:
            return StructureLayer.INTERNAL
        raise ConfigError(f"Timeframe {tf} is not assigned to a structure layer")


@dataclass
class DataConfig:
    """Candle history and polling behaviour."""

    bars_per_timeframe: dict[Timeframe, int] = field(
        default_factory=lambda: {
            Timeframe.H4: 600,
            Timeframe.M30: 1500,
            Timeframe.M15: 2000,
            Timeframe.M5: 3000,
        }
    )
    max_bars_in_memory: int = 5000
    poll_interval_seconds: float = 1.0
    update_overlap_bars: int = 3  # closed bars re-fetched on every update
    fetch_retries: int = 3
    fetch_retry_delay_seconds: float = 1.0


@dataclass
class StructureConfig:
    """Structure-engine parameters (used from Phase 2)."""

    fractal_left: int = 5
    fractal_right: int = 5
    bos_requires_body_close: bool = True  # wick-only break = liquidity sweep
    min_fvg_rr: float = 2.0

    # --- Phase 2 additions (defaults are starting points - tune per symbol) ---
    atr_period: int = 14
    # A structure break is a "displacement" when the largest directional candle
    # body in the impulse leg is >= this many ATRs. CHoCH + displacement = MSS.
    displacement_atr_mult: float = 1.5
    # Order Blocks
    ob_zone_mode: str = "wick"  # "wick" = full candle range, "body" = open/close only
    ob_max_lookback_bars: int = 10  # how far before the leg origin to look for the opposing candle
    ob_require_displacement: bool = True
    ob_require_fvg: bool = False  # False = FVG is preferred (flagged), not mandatory
    # Fair Value Gaps
    fvg_min_size_atr_mult: float = 0.0  # 0 = classic (any gap); raise to filter noise
    # Zones / flips
    flip_zones: bool = True
    max_zone_flips: int = 1  # a zone may flip this many times, then it is invalidated
    max_zones_per_timeframe: int = 150
    # Liquidity
    equal_level_atr_mult: float = 0.1  # tolerance for equal highs/lows
    equal_level_lookback_swings: int = 12
    track_swing_liquidity: bool = True  # prior swing highs/lows count as sweepable liquidity
    session_levels_timeframe: Timeframe = Timeframe.M15  # data used for prev day/session H/L
    day_rollover_timezone: str = "UTC"
    # Premium / Discount
    pd_equilibrium_band: float = 0.02  # 0.48-0.52 counts as Equilibrium
    pd_range_tolerance: float = 0.10  # entries further than this outside the dealing range are stale
    external_range_timeframe: Timeframe = Timeframe.H4
    internal_range_timeframe: Timeframe = Timeframe.M15


@dataclass
class PlanningConfig:
    """Planning layer (Phase 3)."""

    # Timeframes scanned for new setups. External zones are used for context
    # (bias, P&D); add Timeframe.M30 here to also trade external-layer zones.
    scan_timeframes: tuple[Timeframe, ...] = (Timeframe.M15, Timeframe.M5)
    stop_buffer_atr_mult: float = 0.1  # stop sits this many ATRs beyond the invalidating level
    target_min_distance_atr: float = 1.0  # next structure must be at least this far from entry
    max_targets: int = 3
    min_sweep_rr: float = 1.5  # below this the MSS entry is "chasing"
    ob_max_leg_overlap: float = 0.6  # mean candle overlap in the impulse leg; above = slow crawl
    confluence_adjacent_atr_mult: float = 0.25  # FVG may sit this far from an OB and still count
    sweep_max_age_bars: int = 20  # bars (of the sweep timeframe) a sweep stays usable
    mss_max_bars_after_sweep: int = 15
    approach_atr_mult: float = 1.0  # price within this of the entry zone -> plan ACTIVE
    plan_expiry_bars: int = 96  # setup-timeframe bars before an untouched plan is canceled
    zone_scan_max_age_bars: int = 200  # ignore zones older than this when scanning
    flip_plans_respect_pd: bool = False  # True = hard P/D block also applies to flip-retest plans
    fvg_min_gap_atr_mult: float = 0.25  # a 'clean' FVG is at least this many ATRs tall
    max_discard_records: int = 500


@dataclass
class RiskConfig:
    """Risk rules (used from Phase 4)."""

    balance_threshold: float = 10_000.0
    risk_pct_below_threshold: float = 0.5
    risk_pct_at_or_above_threshold: float = 1.0
    max_daily_drawdown_pct: float = 2.0
    max_consecutive_losses: int = 2
    max_trades_per_day: int = 3
    max_open_trades_total: int = 2  # positions + pending orders owned by the bot (its magic number)
    max_open_trades_per_direction_per_symbol: int = 1

    # --- Phase 4 additions -------------------------------------------------
    # Absolute per-trade ceiling. Sizing is clamped to this even if the two
    # percentages above are mis-configured (validate() also rejects values above it).
    hard_max_risk_pct: float = 1.0
    max_account_age_seconds: float = 30.0  # account data older than this is "stale" -> reject
    max_tick_age_seconds: float = 60.0  # tick older than this (market closed / feed stalled) -> reject
    max_spread_to_stop_ratio: float = 0.25  # reject when spread > 25% of the stop distance
    min_rr_other: float = 1.0  # sanity floor for OB / flip plans (FVG 2.0 and Sweep 1.5 live elsewhere)


@dataclass
class SessionWindow:
    """A trading session expressed in its own local time zone (DST-safe)."""

    name: str
    timezone: str
    start: dtime
    end: dtime


@dataclass
class SessionConfig:
    """Allowed trading sessions and daily cut-offs."""

    # London + London/NY overlap + New York. The overlap is the intersection of
    # the two windows below, so it is automatically allowed.
    windows: tuple[SessionWindow, ...] = (
        SessionWindow("London", "Europe/London", dtime(8, 0), dtime(17, 0)),
        SessionWindow("New York", "America/New_York", dtime(8, 0), dtime(17, 0)),
    )
    # The two cut-offs below are evaluated in ``cutoff_timezone``.
    # UNRESOLVED DECISION: the brief does not say which clock 22:30 / 23:00 use.
    # Default UTC; set STRATEGY_TIMEZONE (e.g. "Africa/Kampala", "America/New_York") to change.
    cutoff_timezone: str = field(default_factory=lambda: _env_str("STRATEGY_TIMEZONE", "UTC"))
    trading_day_start: dtime = dtime(0, 0)  # daily limits reset at this local time (cutoff_timezone)
    no_new_trades_after: dtime = dtime(22, 30)
    close_all_risk_by: dtime = dtime(23, 0)
    crypto_exempt_from_cutoffs: bool = True


@dataclass
class TradeManagementConfig:
    """Trade management (used from Phase 4). Fixed-pip trailing is not supported."""

    partial_close_at_r: float = 1.5
    partial_close_fraction: float = 0.5
    move_sl_to_breakeven_after_partial: bool = True
    structural_trailing_enabled: bool = True


@dataclass
class ExecutionConfig:
    """Order execution defaults (used from Phase 4)."""

    magic_number: int = 240101
    max_slippage_points: int = 20
    comment_prefix: str = "SMC"

    # --- Phase 4 additions -------------------------------------------------
    # LIVE ORDER SUBMISSION IS OFF BY DEFAULT. Turning it on needs BOTH env vars
    # (see load_config): SMC_LIVE_EXECUTION=true and SMC_LIVE_CONFIRM=<confirmation phrase>.
    live_enabled: bool = False
    allow_real_account: bool = False  # even when live, REAL accounts need SMC_ALLOW_REAL_ACCOUNT=true
    allow_market_entries: bool = True  # price already inside the zone -> market order
    allow_stop_entries: bool = False  # buy/sell STOP entries (breakout style); off by default
    kill_switch_path: Path = PROJECT_ROOT / "KILL_SWITCH"  # file exists => no new orders
    kill_switch_cancels_pending: bool = True
    state_path: Path = PROJECT_ROOT / "output" / "state" / "execution_state.json"
    max_send_attempts: int = 2  # per plan; uncertain results are reconciled before any retry
    uncertain_hold_seconds: float = 30.0  # wait this long before treating an unseen order as failed
    take_profit_target_index: int = 0  # which Phase 3 target becomes the take-profit
    breakeven_extra_ticks: int = 0  # extra ticks added to the break-even stop
    breakeven_if_partial_impossible: bool = True  # lot too small to split -> still move SL to BE at 1.5R
    max_structure_age_bars: int = 3  # structure data older than this many bars is "stale"
    pending_expire_at_cutoff: bool = True  # pending orders carry a broker-side expiry at the 22:30 cutoff
    cycle_interval_seconds: float = 5.0  # how often main.py runs a management + execution cycle


@dataclass
class NewsConfig:
    """News handling (used from Phase 5)."""

    lock_minutes_before: int = 10
    lock_minutes_after: int = 10
    impact_levels: tuple[str, ...] = ("high",)  # add "medium" for medium-impact protection

    # --- Phase 5 additions -------------------------------------------------
    enabled: bool = True  # False = NO news protection (logged loudly); never recommended
    currencies: tuple[str, ...] = ("USD",)  # event currencies that lock symbols quoting/based in them
    # Calendar source. "faireconomy" = community ForexFactory JSON feed (unofficial, no actual values);
    # "file" = a local JSON file you maintain/export (can carry actuals).
    provider: str = field(default_factory=lambda: _env_str("CALENDAR_PROVIDER", "faireconomy"))
    calendar_url: str = field(default_factory=lambda: _env_str(
        "CALENDAR_URL", "https://nfs.faireconomy.media/ff_calendar_thisweek.json"))
    calendar_file: str = field(default_factory=lambda: _env_str("CALENDAR_FILE"))
    actuals_file: str = field(default_factory=lambda: _env_str("CALENDAR_ACTUALS_FILE"))  # optional overlay
    cache_path: Path = PROJECT_ROOT / "output" / "cache" / "calendar_cache.json"
    refresh_minutes: float = 60.0  # the free feed rate-limits; do not poll it faster
    retry_minutes: float = 5.0  # wait after a failed refresh
    request_timeout_seconds: float = 10.0
    max_calendar_age_hours: float = 24.0  # older than this = NOT verified
    on_calendar_unavailable: str = "block"  # "block" new entries (fail-safe) or "allow"
    open_position_policy: str = "keep"  # "keep" open positions during a lock, or "flatten" (opt-in)
    alert_ahead_minutes: int = 60  # Telegram heads-up for qualifying events this far ahead
    lookahead_hours: float = 48.0
    delay_extension_minutes: int = 15  # lock stays on this long if a late actual is missing (actual-capable providers)
    mixed_extra_lock_minutes: int = 0  # optional extra lock after a MIXED outcome (0 = off)


@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token: str = field(default="", repr=False)
    chat_id: str = ""
    timeout_seconds: float = 5.0  # per HTTP request; the trading loop never waits on Telegram
    max_retries: int = 2
    queue_size: int = 100  # bounded: when full, new alerts are dropped (and counted), never blocking
    retry_cooldown_seconds: float = 300.0  # after a failed delivery the same alert is not retried sooner
    async_send: bool = True

    @classmethod
    def from_env(cls) -> "TelegramConfig":
        token, chat = _env_str("TELEGRAM_BOT_TOKEN"), _env_str("TELEGRAM_CHAT_ID")
        return cls(enabled=bool(token and chat), bot_token=token, chat_id=chat)


@dataclass
class JournalConfig:
    """SQLite lifecycle journal and separate Excel exports."""

    database_path: Path = field(default_factory=lambda: Path(
        _env_str("SMC_JOURNAL_DB", str(PROJECT_ROOT / "output" / "state" / "trade_journal.sqlite3"))
    ))
    reconciliation_interval_seconds: float = 60.0


@dataclass
class LoggingConfig:
    level: str = "INFO"
    log_dir: Path = PROJECT_ROOT / "logs"
    file_name: str = "smc_bot.log"
    max_bytes: int = 5_000_000
    backup_count: int = 5
    console: bool = True


# ---------------------------------------------------------------- root config
def _default_symbols() -> list[SymbolConfig]:
    suffix = _env_str("BROKER_SYMBOL_SUFFIX", "m")
    return [
        SymbolConfig("XAUUSD", AssetClass.METAL, suffix),
        SymbolConfig("EURUSD", AssetClass.FOREX, suffix),
        SymbolConfig("GBPUSD", AssetClass.FOREX, suffix),
        SymbolConfig("BTCUSD", AssetClass.CRYPTO, suffix),
    ]


@dataclass
class BotConfig:
    """Top-level configuration object passed around the application."""

    broker: BrokerConfig = field(default_factory=BrokerConfig)
    symbols: list[SymbolConfig] = field(default_factory=_default_symbols)
    timeframes: TimeframeConfig = field(default_factory=TimeframeConfig)
    data: DataConfig = field(default_factory=DataConfig)
    structure: StructureConfig = field(default_factory=StructureConfig)
    planning: PlanningConfig = field(default_factory=PlanningConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    sessions: SessionConfig = field(default_factory=SessionConfig)
    trade_management: TradeManagementConfig = field(default_factory=TradeManagementConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    journal: JournalConfig = field(default_factory=JournalConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    @property
    def enabled_symbols(self) -> list[SymbolConfig]:
        return [s for s in self.symbols if s.enabled]

    # ------------------------------------------------------------ validation
    def validate(self) -> None:
        """Raise :class:`ConfigError` describing the first problem found."""
        errors: list[str] = []

        if not self.enabled_symbols:
            errors.append("At least one symbol must be enabled.")
        names = [s.name for s in self.symbols]
        if len(names) != len(set(names)):
            errors.append("Duplicate symbol names in config.")

        ext, internal = set(self.timeframes.external), set(self.timeframes.internal)
        if not ext or not internal:
            errors.append("Both external and internal timeframe lists must be non-empty.")
        if ext & internal:
            errors.append("A timeframe cannot be in both external and internal layers.")

        for tf in self.timeframes.all:
            bars = self.data.bars_per_timeframe.get(tf)
            if bars is None:
                errors.append(f"data.bars_per_timeframe is missing {tf}.")
            elif bars < 100:
                errors.append(f"data.bars_per_timeframe[{tf}]={bars} is too small (min 100).")
            elif bars > self.data.max_bars_in_memory:
                errors.append(f"data.bars_per_timeframe[{tf}]={bars} exceeds max_bars_in_memory.")
        if self.data.update_overlap_bars < 1:
            errors.append("data.update_overlap_bars must be >= 1.")

        if self.structure.fractal_left < 1 or self.structure.fractal_right < 1:
            errors.append("Fractal left/right bars must be >= 1.")
        if self.structure.min_fvg_rr < 1:
            errors.append("structure.min_fvg_rr must be >= 1.")
        st = self.structure
        if st.atr_period < 1:
            errors.append("structure.atr_period must be >= 1.")
        if st.displacement_atr_mult <= 0:
            errors.append("structure.displacement_atr_mult must be > 0.")
        if st.ob_zone_mode not in {"wick", "body"}:
            errors.append("structure.ob_zone_mode must be 'wick' or 'body'.")
        if not 0 <= st.pd_equilibrium_band < 0.5:
            errors.append("structure.pd_equilibrium_band must be in [0, 0.5).")
        if st.max_zone_flips < 0 or st.ob_max_lookback_bars < 1:
            errors.append("structure.max_zone_flips >= 0 and ob_max_lookback_bars >= 1 required.")
        if st.external_range_timeframe not in self.timeframes.external:
            errors.append("structure.external_range_timeframe must be an external timeframe.")
        if st.internal_range_timeframe not in self.timeframes.internal:
            errors.append("structure.internal_range_timeframe must be an internal timeframe.")
        if st.session_levels_timeframe not in self.timeframes.all:
            errors.append("structure.session_levels_timeframe must be a configured timeframe.")

        pl = self.planning
        if not pl.scan_timeframes or any(t not in self.timeframes.all for t in pl.scan_timeframes):
            errors.append("planning.scan_timeframes must be non-empty configured timeframes.")
        for label, val in (("stop_buffer_atr_mult", pl.stop_buffer_atr_mult),
                           ("target_min_distance_atr", pl.target_min_distance_atr)):
            if val < 0:
                errors.append(f"planning.{label} must be >= 0.")
        if pl.min_sweep_rr <= 0 or not 0 < pl.ob_max_leg_overlap <= 1 or pl.max_targets < 1:
            errors.append("planning: min_sweep_rr > 0, 0 < ob_max_leg_overlap <= 1, max_targets >= 1 required.")

        r = self.risk
        for label, pct in (
            ("risk_pct_below_threshold", r.risk_pct_below_threshold),
            ("risk_pct_at_or_above_threshold", r.risk_pct_at_or_above_threshold),
            ("max_daily_drawdown_pct", r.max_daily_drawdown_pct),
        ):
            if not 0 < pct <= 100:
                errors.append(f"risk.{label} must be in (0, 100], got {pct}.")
        for label, val in (
            ("max_consecutive_losses", r.max_consecutive_losses),
            ("max_trades_per_day", r.max_trades_per_day),
            ("max_open_trades_total", r.max_open_trades_total),
        ):
            if val < 1:
                errors.append(f"risk.{label} must be >= 1.")

        if not 0 < r.hard_max_risk_pct <= ABSOLUTE_MAX_RISK_PCT:
            errors.append(f"risk.hard_max_risk_pct must be in (0, {ABSOLUTE_MAX_RISK_PCT}].")
        for label, pct in (("risk_pct_below_threshold", r.risk_pct_below_threshold),
                           ("risk_pct_at_or_above_threshold", r.risk_pct_at_or_above_threshold)):
            if pct > r.hard_max_risk_pct:
                errors.append(f"risk.{label}={pct} exceeds the hard per-trade ceiling {r.hard_max_risk_pct}%.")
        if r.max_account_age_seconds <= 0 or r.max_tick_age_seconds <= 0 or not 0 < r.max_spread_to_stop_ratio <= 1:
            errors.append("risk: max_*_age_seconds > 0 and 0 < max_spread_to_stop_ratio <= 1 required.")
        nw = self.news
        if nw.provider not in {"faireconomy", "file"}:
            errors.append("news.provider must be 'faireconomy' or 'file'.")
        if nw.provider == "file" and not nw.calendar_file:
            errors.append("news.provider='file' needs CALENDAR_FILE (path to the calendar JSON).")
        if nw.on_calendar_unavailable not in {"block", "allow"}:
            errors.append("news.on_calendar_unavailable must be 'block' or 'allow'.")
        if nw.open_position_policy not in {"keep", "flatten"}:
            errors.append("news.open_position_policy must be 'keep' or 'flatten'.")
        if any(i not in {"high", "medium", "low"} for i in nw.impact_levels) or not nw.impact_levels:
            errors.append("news.impact_levels must be a non-empty subset of high/medium/low.")
        if not nw.currencies or any(len(c) != 3 or not c.isupper() for c in nw.currencies):
            errors.append("news.currencies must be 3-letter upper-case codes, e.g. ('USD',).")
        if nw.lock_minutes_before < 0 or nw.lock_minutes_after < 0 or nw.refresh_minutes <= 0 \
                or nw.retry_minutes <= 0 or nw.max_calendar_age_hours <= 0:
            errors.append("news: lock minutes >= 0 and refresh/retry/max-age > 0 required.")
        ex = self.execution
        if ex.max_send_attempts < 1 or ex.take_profit_target_index < 0 or ex.max_structure_age_bars < 1:
            errors.append("execution: max_send_attempts >= 1, take_profit_target_index >= 0, max_structure_age_bars >= 1.")
        if self.journal.reconciliation_interval_seconds <= 0:
            errors.append("journal.reconciliation_interval_seconds must be > 0.")

        tm = self.trade_management
        if tm.partial_close_at_r <= 0:
            errors.append("trade_management.partial_close_at_r must be > 0.")
        if not 0 < tm.partial_close_fraction < 1:
            errors.append("trade_management.partial_close_fraction must be in (0, 1).")

        tz_names = [w.timezone for w in self.sessions.windows] + [
            self.sessions.cutoff_timezone,
            self.structure.day_rollover_timezone,
        ]
        for tz in tz_names:
            try:
                ZoneInfo(tz)
            except (ZoneInfoNotFoundError, ValueError):
                errors.append(f"Unknown time zone '{tz}' (on Windows: pip install tzdata).")
        if self.sessions.no_new_trades_after >= self.sessions.close_all_risk_by:
            errors.append("sessions.no_new_trades_after must be earlier than close_all_risk_by.")

        if self.broker.login and not (self.broker.password and self.broker.server):
            errors.append("MT5_LOGIN is set but MT5_PASSWORD / MT5_SERVER is missing.")

        if errors:
            raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(errors))


def load_config() -> BotConfig:
    """Build the configuration from defaults + environment, then validate it."""
    cfg = BotConfig(
        broker=BrokerConfig.from_env(),
        telegram=TelegramConfig.from_env(),
    )
    # Live order submission needs an explicit, double opt-in. Anything else = dry-run.
    cfg.execution.live_enabled = (_env_bool("SMC_LIVE_EXECUTION", False)
                                  and _env_str("SMC_LIVE_CONFIRM") == LIVE_CONFIRM_PHRASE)
    cfg.execution.allow_real_account = _env_bool("SMC_ALLOW_REAL_ACCOUNT", False)
    cfg.journal.database_path = Path(_env_str("SMC_JOURNAL_DB", str(cfg.journal.database_path)))
    cfg.logging.level = _env_str("LOG_LEVEL", "INFO").upper()
    cfg.logging.console = _env_bool("LOG_CONSOLE", True)
    cfg.validate()
    return cfg
