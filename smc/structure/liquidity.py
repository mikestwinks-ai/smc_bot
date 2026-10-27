"""Step 7 - Liquidity levels.

Tracks, per symbol + timeframe:
  * Equal Highs / Equal Lows - two or more swing points within
    ``equal_level_atr_mult`` x ATR of each other with no higher/lower extreme
    printed between them.
  * Previous-day high/low and previous-session high/low (sessions from
    ``SessionConfig``) - only on the timeframe ``session_levels_timeframe``.

Every ACTIVE level is checked on each candle:
  * wick beyond the level but body closes back inside -> SWEPT  (+ LiquiditySweep)
  * body closes beyond the level                       -> TAKEN
The sweep record is what Phase 3's "Liquidity Sweep + MSS" model will consume.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from smc.config import SessionConfig, StructureConfig
from smc.core.timeframes import Timeframe
from smc.structure.models import (
    LiquidityKind, LiquidityLevel, LiquiditySide, LiquiditySweep, LiquidityStatus,
    SwingPoint, SwingType,
)
from smc.structure.tracker import StructureTracker


@dataclass
class LiquidityUpdate:
    new_levels: list[LiquidityLevel] = field(default_factory=list)
    sweeps: list[LiquiditySweep] = field(default_factory=list)


@dataclass
class _SessionState:
    active: bool = False
    high: float = float("-inf")
    low: float = float("inf")


_SWEEP_PRIORITY = {
    LiquidityKind.EQUAL_HIGHS: 0, LiquidityKind.EQUAL_LOWS: 0,
    LiquidityKind.PREV_DAY_HIGH: 1, LiquidityKind.PREV_DAY_LOW: 1,
    LiquidityKind.PREV_SESSION_HIGH: 2, LiquidityKind.PREV_SESSION_LOW: 2,
    LiquidityKind.SWING_HIGH: 3, LiquidityKind.SWING_LOW: 3,
}


def _in_window(local_t: dtime, start: dtime, end: dtime) -> bool:
    if start <= end:
        return start <= local_t < end
    return local_t >= start or local_t < end  # window wraps midnight


class LiquidityTracker:
    def __init__(self, symbol: str, timeframe: Timeframe, cfg: StructureConfig,
                 sessions: SessionConfig, track_sessions: bool) -> None:
        self.symbol, self.timeframe = symbol, timeframe
        self._cfg, self._sessions, self._track_sessions = cfg, sessions, track_sessions
        self.levels: list[LiquidityLevel] = []
        self._day_tz = ZoneInfo(cfg.day_rollover_timezone)
        self._day_key: Optional[object] = None
        self._day_hi, self._day_lo = float("-inf"), float("inf")
        self._sess: dict[str, _SessionState] = {w.name: _SessionState() for w in sessions.windows}

    # ---------------------------------------------------------------- queries
    def active_levels(self, kind: Optional[LiquidityKind] = None) -> list[LiquidityLevel]:
        return [lv for lv in self.levels
                if lv.status is LiquidityStatus.ACTIVE and (kind is None or lv.kind is kind)]

    # ----------------------------------------------------------------- update
    def on_candle(self, tracker: StructureTracker, new_swings: list[SwingPoint],
                  time: pd.Timestamp, high: float, low: float, close: float,
                  is_historical: bool = False) -> LiquidityUpdate:
        out = LiquidityUpdate()
        # 1) judge existing levels against this candle (before adding new ones)
        self._check_sweeps(time, high, low, close, out, is_historical)
        # 2) new equal highs / lows from freshly confirmed swings
        for s in new_swings:
            lvl = self._equal_level_for(tracker, s)
            if lvl is not None:
                out.new_levels.append(lvl)
            if self._cfg.track_swing_liquidity:
                out.new_levels.append(self._swing_level(s))
        # 3) previous day / session levels
        if self._track_sessions:
            self._update_sessions(time, high, low, out)
        self._prune()
        return out

    # ------------------------------------------------------------ sweep logic
    def _check_sweeps(self, time: pd.Timestamp, high: float, low: float, close: float,
                      out: LiquidityUpdate, hist: bool) -> None:
        swept: dict[LiquiditySide, list[LiquidityLevel]] = {}
        for lv in self.levels:
            if lv.status is not LiquidityStatus.ACTIVE:
                continue
            buy = lv.side is LiquiditySide.BUY_SIDE
            beyond_wick = high > lv.price if buy else low < lv.price
            beyond_close = close > lv.price if buy else close < lv.price
            if beyond_close:
                lv.status, lv.resolved_time = LiquidityStatus.TAKEN, time
            elif beyond_wick:
                lv.status, lv.resolved_time = LiquidityStatus.SWEPT, time
                swept.setdefault(lv.side, []).append(lv)

        # One record per candle and side: the most significant level is primary.
        for side, hit in swept.items():
            buy = side is LiquiditySide.BUY_SIDE
            hit.sort(key=lambda v: (_SWEEP_PRIORITY[v.kind], -v.price if buy else v.price))
            primary = hit[0]
            out.sweeps.append(LiquiditySweep(
                level_id=primary.id, kind=primary.kind, side=side, symbol=self.symbol,
                timeframe=self.timeframe, time=time, level_price=primary.price,
                extreme=high if buy else low, close=close, is_historical=hist,
                swept_level_ids=tuple(v.id for v in hit)))

    def _swing_level(self, s: SwingPoint) -> LiquidityLevel:
        """A confirmed swing point is resting liquidity until swept or taken."""
        high = s.type is SwingType.HIGH
        kind = LiquidityKind.SWING_HIGH if high else LiquidityKind.SWING_LOW
        lv = LiquidityLevel(
            id=f"LIQ:{kind.value}:{self.symbol}:{self.timeframe}:{s.time:%Y%m%dT%H%M}", kind=kind,
            side=LiquiditySide.BUY_SIDE if high else LiquiditySide.SELL_SIDE, price=s.price,
            symbol=self.symbol, timeframe=self.timeframe, created_time=s.confirmed_time)
        self.levels.append(lv)
        return lv

    # ------------------------------------------------------- equal highs/lows
    def _equal_level_for(self, tracker: StructureTracker, s: SwingPoint) -> Optional[LiquidityLevel]:
        atr = tracker.atr or 0.0
        tol = self._cfg.equal_level_atr_mult * atr
        if tol <= 0:
            return None
        is_high = s.type is SwingType.HIGH
        kind = LiquidityKind.EQUAL_HIGHS if is_high else LiquidityKind.EQUAL_LOWS
        side = LiquiditySide.BUY_SIDE if is_high else LiquiditySide.SELL_SIDE

        # extend an existing equal level if this swing lines up with it
        for lv in self.active_levels(kind):
            if abs(lv.price - s.price) <= tol:
                lv.touches += 1
                lv.price = max(lv.price, s.price) if is_high else min(lv.price, s.price)
                return None

        buf = tracker.buffer
        recent = [p for p in tracker.swings[-self._cfg.equal_level_lookback_swings * 2:]
                  if p.type is s.type and p.index < s.index][-self._cfg.equal_level_lookback_swings:]
        for p in reversed(recent):
            if abs(p.price - s.price) > tol or not buf.has(p.index):
                continue
            between = (buf.highs_between(p.index + 1, s.index - 1) if is_high
                       else buf.lows_between(p.index + 1, s.index - 1))
            if between and ((max(between) > max(p.price, s.price) + tol) if is_high
                            else (min(between) < min(p.price, s.price) - tol)):
                continue  # a more extreme print in between: not "equal"
            price = max(p.price, s.price) if is_high else min(p.price, s.price)
            lv = LiquidityLevel(
                id=f"LIQ:{kind.value}:{self.symbol}:{self.timeframe}:{s.time:%Y%m%dT%H%M}",
                kind=kind, side=side, price=price, symbol=self.symbol, timeframe=self.timeframe,
                created_time=s.confirmed_time, touches=2)
            self.levels.append(lv)
            return lv
        return None

    # ------------------------------------------------------ day / session H/L
    def _update_sessions(self, time: pd.Timestamp, high: float, low: float, out: LiquidityUpdate) -> None:
        day = time.tz_convert(self._day_tz).date()
        if self._day_key is None:
            self._day_key = day
        elif day != self._day_key:
            self._publish(LiquidityKind.PREV_DAY_HIGH, LiquidityKind.PREV_DAY_LOW, "day",
                          self._day_hi, self._day_lo, time, out)
            self._day_key, self._day_hi, self._day_lo = day, float("-inf"), float("inf")
        self._day_hi, self._day_lo = max(self._day_hi, high), min(self._day_lo, low)

        for w in self._sessions.windows:
            st = self._sess[w.name]
            local_t = time.tz_convert(ZoneInfo(w.timezone)).time()
            if _in_window(local_t, w.start, w.end):
                if not st.active:
                    st.active, st.high, st.low = True, float("-inf"), float("inf")
                st.high, st.low = max(st.high, high), min(st.low, low)
            elif st.active:
                self._publish(LiquidityKind.PREV_SESSION_HIGH, LiquidityKind.PREV_SESSION_LOW,
                              w.name, st.high, st.low, time, out)
                st.active = False

    def _publish(self, hi_kind: LiquidityKind, lo_kind: LiquidityKind, label: str,
                 hi: float, lo: float, time: pd.Timestamp, out: LiquidityUpdate) -> None:
        if hi == float("-inf") or lo == float("inf"):
            return
        for kind, price, side in ((hi_kind, hi, LiquiditySide.BUY_SIDE), (lo_kind, lo, LiquiditySide.SELL_SIDE)):
            for old in self.levels:  # only the latest level per kind+label stays active
                if old.kind is kind and old.label == label and old.status is LiquidityStatus.ACTIVE:
                    old.status, old.resolved_time = LiquidityStatus.EXPIRED, time
            lv = LiquidityLevel(
                id=f"LIQ:{kind.value}:{label}:{self.symbol}:{time:%Y%m%dT%H%M}", kind=kind, side=side,
                price=price, symbol=self.symbol, timeframe=self.timeframe, created_time=time, label=label)
            self.levels.append(lv)
            out.new_levels.append(lv)

    def _prune(self) -> None:
        if len(self.levels) > 300:
            keep = [lv for lv in self.levels if lv.status is LiquidityStatus.ACTIVE]
            rest = [lv for lv in self.levels if lv.status is not LiquidityStatus.ACTIVE][-100:]
            self.levels = sorted(keep + rest, key=lambda lv: lv.created_time)
