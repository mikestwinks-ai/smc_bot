"""Multi-timeframe candle fetching and storage.

Design notes
------------
* Only **closed** candles are stored. MT5 position 0 is the still-forming bar,
  so history is always requested from position 1. Structure logic therefore
  never sees an unfinished candle.
* Index = candle *open* time, tz-aware UTC (broker server offset removed).
* Each (symbol, timeframe) pair has its own :class:`CandleStore`.
* :meth:`MarketDataManager.update_all` returns :class:`CandleEvent` objects for
  every newly closed candle, highest timeframe first, which is the hook the
  structure engine will subscribe to in Phase 2.
"""

from __future__ import annotations

import threading
import time
import math
from dataclasses import dataclass
from typing import Any, Optional

import pandas as pd

from smc.config import BotConfig, SymbolConfig
from smc.core.exceptions import MT5DataError, SymbolNotFoundError
from smc.core.mt5_connection import MT5Connection
from smc.core.timeframes import Timeframe
from smc.utils.logger import get_logger

log = get_logger(__name__)

RATE_COLUMNS: list[str] = ["open", "high", "low", "close", "tick_volume", "spread", "real_volume"]


def _empty_frame() -> pd.DataFrame:
    index = pd.DatetimeIndex([], tz="UTC", name="time")
    return pd.DataFrame(columns=RATE_COLUMNS, index=index, dtype="float64")


@dataclass(frozen=True)
class CandleEvent:
    """Emitted when one or more new candles have closed."""

    symbol: str  # canonical symbol name
    timeframe: Timeframe
    last_candle_time: pd.Timestamp  # open time of the newest closed candle
    new_candles: int


class CandleStore:
    """Rolling window of closed candles for one symbol/timeframe."""

    def __init__(self, symbol: str, timeframe: Timeframe, max_bars: int) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.max_bars = max_bars
        self._df: pd.DataFrame = _empty_frame()

    def __len__(self) -> int:
        return len(self._df)

    @property
    def last_time(self) -> Optional[pd.Timestamp]:
        return None if self._df.empty else self._df.index[-1]

    def replace(self, df: pd.DataFrame) -> None:
        self._df = df.tail(self.max_bars).copy()

    def merge(self, df: pd.DataFrame) -> int:
        """Merge fresh candles in; return how many are *newer* than before."""
        if df.empty:
            return 0
        if self._df.empty:
            self._df = df.tail(self.max_bars).copy()
            return len(self._df)

        previous_last = self._df.index[-1]
        combined = pd.concat([self._df, df])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        self._df = combined.tail(self.max_bars)
        return int((self._df.index > previous_last).sum())

    def get(self, count: Optional[int] = None) -> pd.DataFrame:
        """Return a copy of the last ``count`` candles (all if ``None``)."""
        df = self._df if count is None else self._df.tail(count)
        return df.copy()


class MarketDataManager:
    """Fetches and maintains candles for all configured symbols/timeframes."""

    def __init__(self, connection: MT5Connection, config: BotConfig) -> None:
        self._conn = connection
        self._cfg = config
        self._lock = threading.RLock()
        self._stores: dict[tuple[str, Timeframe], CandleStore] = {}
        self._series_last_success: dict[tuple[str, Timeframe], pd.Timestamp] = {}
        self._series_last_error: dict[tuple[str, Timeframe], str] = {}
        self._broker_names: dict[str, str] = {}  # canonical -> broker symbol
        self._symbols: dict[str, SymbolConfig] = {}
        self.failed_symbols: dict[str, str] = {}

    # ------------------------------------------------------------- properties
    @property
    def symbols(self) -> list[str]:
        """Canonical names of successfully initialised symbols."""
        return list(self._symbols)

    def broker_symbol(self, symbol: str) -> str:
        return self._broker_names[symbol]

    # ---------------------------------------------------------- initialisation
    def initialize(self) -> None:
        """Resolve every symbol and download its initial history."""
        for sym in self._cfg.enabled_symbols:
            try:
                broker_name = self._conn.resolve_symbol(sym)
                for tf in self._cfg.timeframes.all:
                    self._load_history(sym.name, broker_name, tf)
            except (SymbolNotFoundError, MT5DataError) as exc:
                log.error("Skipping %s: %s", sym.name, exc)
                self.failed_symbols[sym.name] = str(exc)
                self._drop_symbol(sym.name)
                continue
            self._broker_names[sym.name] = broker_name
            self._symbols[sym.name] = sym

        if not self._symbols:
            raise MT5DataError("No symbols could be initialised - check symbol names/suffix.")
        log.info("Market data ready for: %s", ", ".join(self._symbols))

    def _drop_symbol(self, symbol: str) -> None:
        for key in [k for k in self._stores if k[0] == symbol]:
            del self._stores[key]

    def _load_history(self, symbol: str, broker_name: str, tf: Timeframe) -> None:
        bars = self._cfg.data.bars_per_timeframe[tf]
        df = self._fetch_rates(broker_name, tf, start_pos=1, count=bars)
        store = CandleStore(symbol, tf, self._cfg.data.max_bars_in_memory)
        store.replace(df)
        self._stores[(symbol, tf)] = store
        key = (symbol, tf)
        self._series_last_success[key] = pd.Timestamp.now(tz="UTC")
        self._series_last_error.pop(key, None)

        gaps = self._count_unexpected_gaps(df, tf)
        log.info(
            "Loaded %s %s: %d candles (%s -> %s)%s",
            symbol, tf, len(df), df.index[0], df.index[-1],
            f", {gaps} unexplained gap(s)" if gaps else "",
        )

    # ------------------------------------------------------------------ fetch
    def _fetch_rates(
        self, broker_symbol: str, tf: Timeframe, start_pos: int, count: int
    ) -> pd.DataFrame:
        """Fetch ``count`` candles starting ``start_pos`` bars back (with retries)."""
        data_cfg = self._cfg.data
        last_error: Any = None

        for attempt in range(1, data_cfg.fetch_retries + 1):
            self._conn.ensure_connected()
            mt5 = self._conn.mt5
            try:
                rates = mt5.copy_rates_from_pos(
                    broker_symbol, tf.to_mt5(mt5), start_pos, count
                )
            except Exception as exc:
                rates, last_error = None, exc
            else:
                if rates is not None and len(rates) > 0:
                    return self._rates_to_frame(rates)
                last_error = mt5.last_error()

            log.warning(
                "No rates for %s %s (attempt %d/%d): %s",
                broker_symbol, tf, attempt, data_cfg.fetch_retries, last_error,
            )
            time.sleep(data_cfg.fetch_retry_delay_seconds)

        raise MT5DataError(f"Failed to fetch {broker_symbol} {tf} candles: {last_error}")

    def _rates_to_frame(self, rates: Any) -> pd.DataFrame:
        """Convert MT5 structured array -> UTC-indexed, de-duplicated DataFrame."""
        df = pd.DataFrame(rates)
        missing = {"time", "open", "high", "low", "close"} - set(df.columns)
        if missing:
            raise MT5DataError(f"MT5 returned malformed rates, missing: {sorted(missing)}")

        offset = pd.Timedelta(hours=self._cfg.broker.server_utc_offset_hours)
        index = pd.DatetimeIndex(pd.to_datetime(df["time"], unit="s") - offset)
        df.index = index.tz_localize("UTC")
        df.index.name = "time"
        for col in RATE_COLUMNS:
            if col not in df.columns:
                df[col] = 0
        df = df[RATE_COLUMNS].astype("float64")
        df = df[~df.index.duplicated(keep="last")].sort_index()

        if not df.index.is_monotonic_increasing or df.isna().any().any():
            raise MT5DataError("Rates contain NaN values or are not time-ordered.")
        prices = df[["open", "high", "low", "close"]]
        if any(not math.isfinite(value) for column in prices for value in prices[column]):
            raise MT5DataError("Rates contain non-finite OHLC values.")
        invalid_ohlc = (
            (df["high"] < df[["open", "close"]].max(axis=1))
            | (df["low"] > df[["open", "close"]].min(axis=1))
            | (df["high"] < df["low"])
        )
        if invalid_ohlc.any():
            raise MT5DataError("Rates contain an invalid OHLC envelope.")
        return df

    @staticmethod
    def _count_unexpected_gaps(df: pd.DataFrame, tf: Timeframe) -> int:
        """Count holes larger than 1.5 bars that a weekend does not explain."""
        if len(df) < 2:
            return 0
        deltas = df.index.to_series().diff().dropna()
        count = 0
        for ts, delta in deltas[deltas > tf.delta * 1.5].items():
            prev = ts - delta
            weekend = prev.weekday() >= 4 and ts.weekday() in (0, 6) and delta <= pd.Timedelta(days=3)
            if not weekend:
                count += 1
        return count

    # ----------------------------------------------------------------- updates
    def update(self, symbol: str, tf: Timeframe) -> int:
        """Refresh one series; return the number of newly closed candles."""
        with self._lock:
            store = self._get_store(symbol, tf)
            try:
                df = self._fetch_rates(
                    self._broker_names[symbol], tf,
                    start_pos=1, count=self._cfg.data.update_overlap_bars,
                )
            except MT5DataError as exc:
                self._series_last_error[(symbol, tf)] = str(exc)
                raise
            self._series_last_success[(symbol, tf)] = pd.Timestamp.now(tz="UTC")
            self._series_last_error.pop((symbol, tf), None)
            return store.merge(df)

    def update_all(self) -> list[CandleEvent]:
        """Refresh every series. Failures on one series never block the others."""
        events: list[CandleEvent] = []
        for tf in self._cfg.timeframes.all:  # highest timeframe first
            for symbol in self._symbols:
                try:
                    new = self.update(symbol, tf)
                except MT5DataError as exc:
                    log.warning("Update failed for %s %s: %s", symbol, tf, exc)
                    continue
                if new > 0:
                    last = self._get_store(symbol, tf).last_time
                    assert last is not None
                    events.append(CandleEvent(symbol, tf, last, new))
        return events

    # ------------------------------------------------------------------ access
    def _get_store(self, symbol: str, tf: Timeframe) -> CandleStore:
        try:
            return self._stores[(symbol, tf)]
        except KeyError as exc:
            raise MT5DataError(f"No data loaded for {symbol} {tf}") from exc

    def get_candles(self, symbol: str, tf: Timeframe, count: Optional[int] = None) -> pd.DataFrame:
        """Closed candles (copy), oldest first. ``count`` limits to the most recent N."""
        with self._lock:
            return self._get_store(symbol, tf).get(count)

    def series_status(self, now: Optional[pd.Timestamp] = None) -> list[dict[str, Any]]:
        """Return local fetch heartbeats without making a broker request."""
        now = now or pd.Timestamp.now(tz="UTC")
        with self._lock:
            rows = []
            for symbol in (s.name for s in self._cfg.enabled_symbols):
                for tf in self._cfg.timeframes.all:
                    key = (symbol, tf)
                    store = self._stores.get(key)
                    success = self._series_last_success.get(key)
                    rows.append({
                        "symbol": symbol,
                        "timeframe": tf.label,
                        "last_success_utc": success.isoformat() if success is not None else None,
                        "last_closed_candle_utc": store.last_time.isoformat() if store and store.last_time is not None else None,
                        "last_error": self._series_last_error.get(key) or self.failed_symbols.get(symbol),
                    })
            return rows

    def get_latest_closed(self, symbol: str, tf: Timeframe) -> pd.Series:
        df = self.get_candles(symbol, tf, 1)
        if df.empty:
            raise MT5DataError(f"No candles for {symbol} {tf}")
        return df.iloc[-1]

    def get_forming_candle(self, symbol: str, tf: Timeframe) -> pd.Series:
        """Live, unfinished candle (not stored). Use for display only, never for structure."""
        df = self._fetch_rates(self._broker_names[symbol], tf, start_pos=0, count=1)
        return df.iloc[-1]

    def get_tick(self, symbol: str) -> Any:
        return self._conn.get_tick(self._broker_names[symbol])
