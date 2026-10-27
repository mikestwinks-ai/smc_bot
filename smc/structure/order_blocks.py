"""Step 5 - Order Block detection.

An Order Block is the LAST OPPOSING candle before the displacement leg that
caused a BOS / CHoCH / MSS:
  bullish break -> last bearish candle at/before the impulse origin  (demand OB)
  bearish break -> last bullish candle at/before the impulse origin  (supply OB)
It is only valid if the leg showed displacement (``ob_require_displacement``).
``has_fvg`` flags the preferred case where the leg also left a same-direction
FVG. Mitigation is applied by ``zones.apply_mitigation`` like any other zone.
"""

from __future__ import annotations

from typing import Iterable, Optional

from smc.config import StructureConfig
from smc.structure.models import Bias, FairValueGap, OrderBlock, StructureEvent, ZoneKind
from smc.structure.tracker import StructureTracker


class OrderBlockDetector:
    def __init__(self, cfg: StructureConfig) -> None:
        self._cfg = cfg
        self._seen: set[tuple[str, str, int, Bias]] = set()  # never create the same OB twice

    def detect(self, event: StructureEvent, tracker: StructureTracker,
               fvgs: Iterable[FairValueGap]) -> Optional[OrderBlock]:
        cfg, buf = self._cfg, tracker.buffer
        if cfg.ob_require_displacement and not event.displacement:
            return None

        bullish = event.direction is Bias.BULLISH
        floor = max(buf.base, event.origin_index - cfg.ob_max_lookback_bars)
        idx: Optional[int] = None
        for k in range(event.origin_index, floor - 1, -1):  # walk back from the impulse origin
            o, c = buf.open(k), buf.close(k)
            if (bullish and c < o) or (not bullish and c > o):
                idx = k
                break
        if idx is None:
            return None

        key = (event.symbol, str(event.timeframe), idx, event.direction)
        if key in self._seen:
            return None

        has_fvg = any(
            f.bias is event.direction
            and f.symbol == event.symbol
            and f.timeframe is event.timeframe
            and event.origin_index <= f.created_index <= event.index
            for f in fvgs
        )
        if cfg.ob_require_fvg and not has_fvg:
            return None

        if cfg.ob_zone_mode == "body":
            top, bottom = max(buf.open(idx), buf.close(idx)), min(buf.open(idx), buf.close(idx))
        else:
            top, bottom = buf.high(idx), buf.low(idx)
        if top <= bottom:
            return None

        self._seen.add(key)
        if len(self._seen) > 2000:
            self._seen.clear()

        candle_time = buf.time(idx)
        return OrderBlock(
            id=f"OB:{event.symbol}:{event.timeframe}:{candle_time:%Y%m%dT%H%M}:{event.direction.tag}",
            kind=ZoneKind.ORDER_BLOCK, symbol=event.symbol, timeframe=event.timeframe, bias=event.direction,
            top=top, bottom=bottom, created_time=event.time, created_index=event.index,
            candle_time=candle_time, candle_index=idx, leg_start_index=event.origin_index,
            origin_event_id=event.id, origin_event_type=event.type,
            has_fvg=has_fvg, displacement_ratio=event.displacement_ratio,
        )
