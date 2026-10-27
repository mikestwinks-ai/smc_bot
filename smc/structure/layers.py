"""Steps 2 & 3 - External (H4 + M30) and Internal (M15 + M5) structure engines.

Both engines run the same tracker logic on their own timeframes and never read
each other's state, so internal structure is free to disagree with external
bias. The only behavioural difference: the internal engine also labels
displacement CHoCHs as MSS.

Layer bias = bias of the layer's *anchor* (highest) timeframe; the per-timeframe
biases are exposed too so callers can see disagreement inside a layer.
"""

from __future__ import annotations

from typing import Optional, Sequence

import pandas as pd

from smc.config import StructureConfig
from smc.core.timeframes import StructureLayer, Timeframe
from smc.structure.models import Bias, SwingPoint, TrackerUpdate
from smc.structure.tracker import StructureTracker


class StructureLayerEngine:
    layer: StructureLayer
    emit_mss: bool = False

    def __init__(self, timeframes: Sequence[Timeframe], cfg: StructureConfig, max_bars: int = 5000) -> None:
        self.timeframes: tuple[Timeframe, ...] = tuple(sorted(timeframes, key=lambda t: -t.minutes))
        self._cfg, self._max_bars = cfg, max_bars
        self._trackers: dict[tuple[str, Timeframe], StructureTracker] = {}

    @property
    def anchor_timeframe(self) -> Timeframe:
        return self.timeframes[0]

    def handles(self, tf: Timeframe) -> bool:
        return tf in self.timeframes

    def tracker(self, symbol: str, tf: Timeframe) -> StructureTracker:
        if not self.handles(tf):
            raise ValueError(f"{type(self).__name__} does not handle {tf}")
        key = (symbol, tf)
        if key not in self._trackers:
            self._trackers[key] = StructureTracker(
                symbol, tf, self.layer, self._cfg, self.emit_mss, self._max_bars
            )
        return self._trackers[key]

    def get_tracker(self, symbol: str, tf: Timeframe) -> Optional[StructureTracker]:
        """Existing tracker or None (never creates one)."""
        return self._trackers.get((symbol, tf))

    def process_candle(self, symbol: str, tf: Timeframe, time: pd.Timestamp,
                       o: float, h: float, l: float, c: float) -> TrackerUpdate:
        return self.tracker(symbol, tf).process_candle(time, o, h, l, c)

    # ------------------------------------------------------------------ queries
    def bias(self, symbol: str) -> Bias:
        """Layer bias = bias of the anchor timeframe."""
        t = self._trackers.get((symbol, self.anchor_timeframe))
        return t.bias if t else Bias.NEUTRAL

    def bias_by_timeframe(self, symbol: str) -> dict[Timeframe, Bias]:
        return {tf: (self._trackers[(symbol, tf)].bias if (symbol, tf) in self._trackers else Bias.NEUTRAL)
                for tf in self.timeframes}

    def active_swings(self, symbol: str, tf: Optional[Timeframe] = None) -> dict[str, Optional[SwingPoint]]:
        """Active (unbroken) swing high / low used as BOS / CHoCH levels."""
        t = self._trackers.get((symbol, tf or self.anchor_timeframe))
        return t.active_swings() if t else {"high": None, "low": None}


class ExternalStructureEngine(StructureLayerEngine):
    layer = StructureLayer.EXTERNAL
    emit_mss = False  # external reports BOS and CHoCH only


class InternalStructureEngine(StructureLayerEngine):
    layer = StructureLayer.INTERNAL
    emit_mss = True  # internal also reports MSS (displacement CHoCH)
