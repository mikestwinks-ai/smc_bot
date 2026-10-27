"""Builds :class:`EvalContext` objects from a live :class:`StructureHub`.

Also home to the structure queries the evaluators need: next opposite structure
(targets), impulse overlap and the FVG stop anchor.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from smc.config import BotConfig
from smc.core.timeframes import StructureLayer, Timeframe
from smc.models.plan import PlanDirection
from smc.planning.entry_models import EvalContext
from smc.structure.engine import StructureHub
from smc.structure.models import (
    Bias, FairValueGap, LiquiditySide, OrderBlock, SwingType,
)
from smc.structure.tracker import StructureTracker


class StructureView:
    def __init__(self, hub: StructureHub, config: BotConfig) -> None:
        self.hub, self._cfg = hub, config

    # --------------------------------------------------------------- trackers
    def tracker(self, symbol: str, tf: Timeframe) -> Optional[StructureTracker]:
        return self.hub.get_tracker(symbol, tf)

    def symbol_trackers(self, symbol: str) -> list[StructureTracker]:
        out = []
        for tf in self._cfg.timeframes.all:
            t = self.hub.get_tracker(symbol, tf)
            if t is not None and len(t.buffer):
                out.append(t)
        return out

    # ---------------------------------------------------------------- context
    def build(self, symbol: str, tf: Timeframe, now: Optional[pd.Timestamp] = None) -> Optional[EvalContext]:
        tracker = self.tracker(symbol, tf)
        if tracker is None or not tracker.atr or tracker.last_close is None:
            return None
        layer = self._cfg.timeframes.layer_of(tf)
        dual = self.hub.premium_discount(symbol)
        highs, lows = self._structure_levels(symbol)
        pc = self._cfg.planning
        min_dist = pc.target_min_distance_atr * tracker.atr
        atr = tracker.atr

        def find_targets(direction: PlanDirection, entry: float) -> list[float]:
            if direction is PlanDirection.LONG:
                ordered = sorted(p for p in highs if p - entry >= min_dist)
            else:
                ordered = sorted((p for p in lows if entry - p >= min_dist), reverse=True)
            out: list[float] = []
            for p in ordered:  # collapse near-identical levels
                if not out or abs(p - out[-1]) > 0.25 * atr:
                    out.append(p)
            return out[: pc.max_targets]

        return EvalContext(
            symbol=symbol, timeframe=tf, layer=layer, atr=atr,
            now=now if now is not None else tracker.buffer.time(tracker.buffer.last_index),
            external_bias=self.hub.external.bias(symbol), internal_bias=self.hub.internal.bias(symbol),
            external_pd=dual.external, internal_pd=dual.internal,
            structure_cfg=self._cfg.structure, planning_cfg=pc,
            find_targets=find_targets,
            leg_overlap=lambda ob: self.leg_overlap(ob),
            fvg_stop_anchor=lambda f: self.fvg_stop_anchor(f),
        )

    # ------------------------------------------------------- structure levels
    def _structure_levels(self, symbol: str) -> tuple[list[float], list[float]]:
        """Unbroken swing highs/lows of every timeframe + active liquidity."""
        highs: list[float] = []
        lows: list[float] = []
        for t in self.symbol_trackers(symbol):
            h, l = self._unbroken_swings(t)
            highs += h
            lows += l
        for lv in self.hub.active_liquidity(symbol):
            (highs if lv.side is LiquiditySide.BUY_SIDE else lows).append(lv.price)
        return highs, lows

    @staticmethod
    def _unbroken_swings(t: StructureTracker, recent_swings: int = 80) -> tuple[list[float], list[float]]:
        """Swing prices that no later candle has traded beyond (one O(n) pass)."""
        buf = t.buffer
        recent = [s for s in t.swings[-recent_swings:] if buf.has(s.index)]
        if not recent:
            return [], []
        first = min(s.index for s in recent)
        hs, ls = buf.highs[first - buf.base:], buf.lows[first - buf.base:]
        n = len(hs)
        suf_max, suf_min = [0.0] * n, [0.0] * n
        run_hi, run_lo = float("-inf"), float("inf")
        for k in range(n - 1, -1, -1):
            suf_max[k], suf_min[k] = run_hi, run_lo  # extremes of the bars AFTER k
            run_hi, run_lo = max(run_hi, hs[k]), min(run_lo, ls[k])
        highs, lows = [], []
        for s in recent:
            k = s.index - first
            if s.type is SwingType.HIGH and suf_max[k] <= s.price:
                highs.append(s.price)
            elif s.type is SwingType.LOW and suf_min[k] >= s.price:
                lows.append(s.price)
        return highs, lows

    # ------------------------------------------------------------ OB / FVG aids
    def leg_overlap(self, ob: OrderBlock) -> float:
        """Mean overlap between consecutive impulse candles (0 = clean, 1 = crawl)."""
        t = self.tracker(ob.symbol, ob.timeframe)
        if t is None:
            return 0.0
        buf = t.buffer
        start, end = max(ob.candle_index + 1, buf.base), ob.created_index
        if end - start < 1 or not buf.has(end):
            return 0.0
        vals = []
        for k in range(start + 1, end + 1):
            h, l = buf.high(k), buf.low(k)
            ph, pl = buf.high(k - 1), buf.low(k - 1)
            rng = h - l
            if rng <= 0:
                continue
            vals.append(max(0.0, min(h, ph) - max(l, pl)) / rng)
        return sum(vals) / len(vals) if vals else 0.0

    def fvg_stop_anchor(self, fvg: FairValueGap) -> float:
        """Swing origin (or the OB) of the move that created the gap; fallback = 3-candle extreme."""
        long = fvg.bias is Bias.BULLISH
        anchor = fvg.stop_reference
        t = self.tracker(fvg.symbol, fvg.timeframe)
        if t is None:
            return anchor
        buf = t.buffer
        want = SwingType.LOW if long else SwingType.HIGH
        origin = next((s for s in reversed(t.swings)
                       if s.type is want and s.index < fvg.created_index and buf.has(s.index)), None)
        if origin is not None:
            span = (buf.lows_between if long else buf.highs_between)(origin.index, fvg.created_index)
            if span:
                anchor = min(anchor, *span) if long else max(anchor, *span)
        for ob in self.hub.zones.order_blocks(fvg.symbol, fvg.timeframe):  # OB that created the move
            if ob.bias is fvg.bias and ob.candle_index <= fvg.created_index <= ob.created_index:
                anchor = min(anchor, ob.bottom) if long else max(anchor, ob.top)
        return anchor
