"""Step 6 - Fair Value Gap detection (classic 3-candle imbalance).

Bullish FVG: low[i] > high[i-2]  -> zone [high[i-2], low[i]]  (demand)
Bearish FVG: high[i] < low[i-2]  -> zone [high[i], low[i-2]]  (supply)
Mitigation / flipping is handled generically by ``zones.py`` / ``flipped_zones.py``;
RR maths lives on :class:`FairValueGap` (``calculate_rr`` / ``current_rr``).
"""

from __future__ import annotations

from typing import Optional

from smc.config import StructureConfig
from smc.structure.models import Bias, FairValueGap, ZoneKind
from smc.structure.tracker import StructureTracker


class FVGDetector:
    def __init__(self, cfg: StructureConfig) -> None:
        self._cfg = cfg

    def detect(self, tracker: StructureTracker) -> Optional[FairValueGap]:
        """Check the 3 candles ending at the tracker's newest candle."""
        buf = tracker.buffer
        i = buf.last_index
        if i - 2 < buf.base:
            return None

        h0, l0 = buf.high(i - 2), buf.low(i - 2)
        h2, l2 = buf.high(i), buf.low(i)
        if l2 > h0:
            bias, bottom, top = Bias.BULLISH, h0, l2
            stop_ref = min(buf.low(k) for k in (i - 2, i - 1, i))
        elif h2 < l0:
            bias, bottom, top = Bias.BEARISH, h2, l0
            stop_ref = max(buf.high(k) for k in (i - 2, i - 1, i))
        else:
            return None

        size = top - bottom
        min_size = self._cfg.fvg_min_size_atr_mult * (tracker.atr or 0.0)
        if size <= 0 or size < min_size:
            return None

        t = buf.time(i)
        return FairValueGap(
            id=f"FVG:{tracker.symbol}:{tracker.timeframe}:{t:%Y%m%dT%H%M}:{bias.tag}",
            kind=ZoneKind.FVG, symbol=tracker.symbol, timeframe=tracker.timeframe, bias=bias,
            top=top, bottom=bottom, created_time=t, created_index=i,
            gap_size=size, stop_reference=stop_ref,
        )
