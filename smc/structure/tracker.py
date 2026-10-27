"""Per-timeframe market-structure tracker (the heart of Steps 2 and 3).

One :class:`StructureTracker` follows ONE symbol on ONE timeframe, candle by
candle, so live trading and history replay behave identically.

Rules (all on CLOSED candles)
-----------------------------
* Swings: fractal, confirmed ``right`` bars late (see ``swings.py``).
* A break needs a candle **close** beyond the swing price. A wick beyond the
  level with the close back inside is NOT a break (it is a liquidity sweep -
  see ``liquidity.py``).
* BOS   : break in the direction of the current bias (the very first break from
          NEUTRAL is also labelled BOS and sets the bias).
* CHoCH : break against the current bias; flips the bias.
* MSS   : (internal layer only) a CHoCH whose impulse leg shows displacement
          (largest directional body >= ``displacement_atr_mult`` x ATR).

Which swing is "the level"?
* Bullish bias: BOS level = highest unbroken swing high since the last break
  (a lower pullback high is ignored); CHoCH level = most recent swing low.
* Bearish bias: mirror image.  Neutral: most recent swing on each side.
A broken level is consumed; the next BOS needs a freshly confirmed swing.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from smc.config import StructureConfig
from smc.core.timeframes import StructureLayer, Timeframe
from smc.structure.models import (
    Bias,
    StructureEvent,
    StructureEventType,
    SwingPoint,
    SwingType,
    TrackerUpdate,
)
from smc.structure.swings import is_swing_high_at, is_swing_low_at
from smc.utils.logger import get_logger

log = get_logger(__name__)


class CandleBuffer:
    """Append-only candle store addressed by *absolute* bar number."""

    def __init__(self, max_bars: int) -> None:
        self.max_bars = max_bars
        self.base = 0  # absolute number of the first stored bar
        self.times: list[pd.Timestamp] = []
        self.opens: list[float] = []
        self.highs: list[float] = []
        self.lows: list[float] = []
        self.closes: list[float] = []

    def __len__(self) -> int:
        return len(self.times)

    @property
    def last_index(self) -> int:
        return self.base + len(self.times) - 1

    def has(self, i: int) -> bool:
        return self.base <= i <= self.last_index

    def append(self, t: pd.Timestamp, o: float, h: float, l: float, c: float) -> int:
        self.times.append(t)
        self.opens.append(o)
        self.highs.append(h)
        self.lows.append(l)
        self.closes.append(c)
        index = self.last_index
        if len(self.times) > 2 * self.max_bars:  # trim lazily, keep newest max_bars
            k = len(self.times) - self.max_bars
            for arr in (self.times, self.opens, self.highs, self.lows, self.closes):
                del arr[:k]
            self.base += k
        return index

    # absolute-index accessors
    def time(self, i: int) -> pd.Timestamp: return self.times[i - self.base]
    def open(self, i: int) -> float: return self.opens[i - self.base]
    def high(self, i: int) -> float: return self.highs[i - self.base]
    def low(self, i: int) -> float: return self.lows[i - self.base]
    def close(self, i: int) -> float: return self.closes[i - self.base]

    def highs_between(self, a: int, b: int) -> list[float]:
        a = max(a, self.base)
        return self.highs[a - self.base: b - self.base + 1]

    def lows_between(self, a: int, b: int) -> list[float]:
        a = max(a, self.base)
        return self.lows[a - self.base: b - self.base + 1]


class StructureTracker:
    """Swings, BOS / CHoCH / MSS and bias for one symbol + timeframe."""

    def __init__(
        self,
        symbol: str,
        timeframe: Timeframe,
        layer: StructureLayer,
        cfg: StructureConfig,
        emit_mss: bool,
        max_bars: int = 5000,
    ) -> None:
        self.symbol, self.timeframe, self.layer = symbol, timeframe, layer
        self._cfg, self._emit_mss = cfg, emit_mss
        self.buffer = CandleBuffer(max_bars)

        self.bias: Bias = Bias.NEUTRAL
        self.swing_high: Optional[SwingPoint] = None  # active (unbroken) level to break upward
        self.swing_low: Optional[SwingPoint] = None  # active (unbroken) level to break downward
        self.swings: list[SwingPoint] = []  # recent confirmed swings, oldest first
        self.events: list[StructureEvent] = []  # recent structure events, oldest first

        self.atr: Optional[float] = None
        self._tr_sum, self._tr_n = 0.0, 0

    # ------------------------------------------------------------------ public
    @property
    def last_close(self) -> Optional[float]:
        return self.buffer.close(self.buffer.last_index) if len(self.buffer) else None

    def process_candle(self, time: pd.Timestamp, o: float, h: float, l: float, c: float) -> TrackerUpdate:
        """Feed ONE closed candle; returns the swings/events it produced."""
        buf = self.buffer
        prev_close = buf.close(buf.last_index) if len(buf) else None
        i = buf.append(time, o, h, l, c)
        self._update_atr(h, l, prev_close)

        update = TrackerUpdate()
        for swing in self._confirm_swings(i, time):
            update.new_swings.append(swing)
            self._register_swing(swing)

        event = self._check_break(i, time, c)
        if event is not None:
            update.events.append(event)
            self.events.append(event)
            if len(self.events) > 200:
                del self.events[:50]
        return update

    def dealing_range(self) -> Optional[tuple[float, float]]:
        """Current (high, low) dealing range for Premium/Discount.

        Latest confirmed swing high / low, extended by any extreme printed since
        (so an unconfirmed fresh impulse is still inside the range).
        """
        sh = next((s for s in reversed(self.swings) if s.type is SwingType.HIGH), None)
        sl = next((s for s in reversed(self.swings) if s.type is SwingType.LOW), None)
        if sh is None or sl is None:
            return None
        buf = self.buffer
        high = max([sh.price, *buf.highs_between(sh.index, buf.last_index)])
        low = min([sl.price, *buf.lows_between(sl.index, buf.last_index)])
        return (high, low) if high > low else None

    def active_swings(self) -> dict[str, Optional[SwingPoint]]:
        return {"high": self.swing_high, "low": self.swing_low}

    # ----------------------------------------------------------------- internals
    def _update_atr(self, h: float, l: float, prev_close: Optional[float]) -> None:
        tr = h - l if prev_close is None else max(h - l, abs(h - prev_close), abs(l - prev_close))
        p = self._cfg.atr_period
        self._tr_n += 1
        if self._tr_n <= p:  # simple average while warming up, then Wilder smoothing
            self._tr_sum += tr
            self.atr = self._tr_sum / self._tr_n
        else:
            assert self.atr is not None
            self.atr = (self.atr * (p - 1) + tr) / p

    def _confirm_swings(self, i: int, time: pd.Timestamp) -> list[SwingPoint]:
        left, right, buf = self._cfg.fractal_left, self._cfg.fractal_right, self.buffer
        j = i - right
        if j - left < buf.base:
            return []
        window_start = j - left
        highs = buf.highs_between(window_start, i)
        lows = buf.lows_between(window_start, i)
        found: list[SwingPoint] = []
        if is_swing_high_at(highs, left, left, right):
            found.append(SwingPoint(SwingType.HIGH, highs[left], buf.time(j), j, time, self.timeframe))
        if is_swing_low_at(lows, left, left, right):
            found.append(SwingPoint(SwingType.LOW, lows[left], buf.time(j), j, time, self.timeframe))
        return found

    def _register_swing(self, s: SwingPoint) -> None:
        self.swings.append(s)
        if len(self.swings) > 400:
            del self.swings[:100]
        if s.type is SwingType.HIGH:
            # Bullish: keep the structural (highest) unbroken high, ignore lower pullback highs.
            keep = self.bias is Bias.BULLISH and self.swing_high is not None and s.price < self.swing_high.price
            if not keep:
                self.swing_high = s
        else:
            keep = self.bias is Bias.BEARISH and self.swing_low is not None and s.price > self.swing_low.price
            if not keep:
                self.swing_low = s

    def _check_break(self, i: int, time: pd.Timestamp, close: float) -> Optional[StructureEvent]:
        if self.swing_high is not None and close > self.swing_high.price:
            return self._make_event(Bias.BULLISH, self.swing_high, i, time, close)
        if self.swing_low is not None and close < self.swing_low.price:
            return self._make_event(Bias.BEARISH, self.swing_low, i, time, close)
        return None

    def _make_event(self, direction: Bias, swing: SwingPoint, i: int, time: pd.Timestamp, close: float) -> StructureEvent:
        bias_before = self.bias
        etype = StructureEventType.BOS if bias_before in (Bias.NEUTRAL, direction) else StructureEventType.CHOCH

        origin = self._leg_origin(swing.index, i, direction)
        displaced, ratio = self._displacement(origin, i, direction)
        if etype is StructureEventType.CHOCH and self._emit_mss and displaced:
            etype = StructureEventType.MSS

        self.bias = direction
        if direction is Bias.BULLISH:
            self.swing_high = None  # consumed
        else:
            self.swing_low = None

        event = StructureEvent(
            id=f"{etype.value}:{self.symbol}:{self.timeframe}:{time:%Y%m%dT%H%M}:{direction.tag}",
            type=etype, direction=direction, symbol=self.symbol, timeframe=self.timeframe,
            layer=self.layer, time=time, index=i, level=swing.price, broken_swing=swing, close=close,
            bias_before=bias_before, bias_after=direction, origin_index=origin,
            displacement=displaced, displacement_ratio=ratio,
        )
        log.debug("%s %s %s %s level=%.5f close=%.5f bias %s->%s disp=%.2f",
                  self.symbol, self.timeframe, etype.value, direction.value, swing.price, close,
                  bias_before.value, direction.value, ratio)
        return event

    def _leg_origin(self, swing_index: int, i: int, direction: Bias) -> int:
        """Extreme candle that started the impulse (lowest low for an up-break)."""
        buf = self.buffer
        start = max(swing_index + 1, buf.base)
        best = start
        for k in range(start, i + 1):
            if direction is Bias.BULLISH:
                if buf.low(k) <= buf.low(best):
                    best = k
            elif buf.high(k) >= buf.high(best):
                best = k
        return best

    def _displacement(self, origin: int, i: int, direction: Bias) -> tuple[bool, float]:
        """Largest directional body in the leg, measured in ATRs."""
        if not self.atr or self.atr <= 0:
            return False, 0.0
        buf = self.buffer
        biggest = 0.0
        for k in range(origin, i + 1):
            body = buf.close(k) - buf.open(k)
            if direction is Bias.BEARISH:
                body = -body
            biggest = max(biggest, body)
        ratio = biggest / self.atr
        return ratio >= self._cfg.displacement_atr_mult, ratio
