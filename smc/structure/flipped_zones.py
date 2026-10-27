"""Step 8 - Flipped zone logic.

A zone (Order Block or FVG) that is broken by a candle BODY CLOSE through its far
side is flipped: the old zone is marked FLIPPED and an opposite zone is created
over the same price area (demand -> supply, supply -> demand) to be retested.
A :class:`FlipSignal` tells the Phase 3 Plan Manager to cancel plans built on the
old zone and create the opposite plan.

A zone may flip ``max_zone_flips`` times; after that it is just INVALIDATED (the
signal still fires so plans on it get cancelled, but ``new_zone_id`` is None).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

import pandas as pd

from smc.config import StructureConfig
from smc.structure.models import FlipSignal, Zone, ZoneStatus
from smc.utils.logger import get_logger

log = get_logger(__name__)


class FlippedZoneEngine:
    def __init__(self, cfg: StructureConfig) -> None:
        self._cfg = cfg

    def check(self, zone: Zone, time: pd.Timestamp, index: int, close: float,
              is_historical: bool = False) -> Optional[tuple[FlipSignal, Optional[Zone]]]:
        """Return ``(signal, new_zone_or_None)`` if this candle's close breaks ``zone``."""
        if not zone.is_live or not zone.broken_by_close(close):
            return None

        zone.flipped_time = time
        can_flip = self._cfg.flip_zones and zone.flip_count < self._cfg.max_zone_flips
        new_zone: Optional[Zone] = None

        if can_flip:
            new_bias = zone.bias.opposite
            new_zone = replace(
                zone,
                id=f"{zone.id}>F{zone.flip_count + 1}",
                bias=new_bias,
                status=ZoneStatus.ACTIVE,
                created_time=time,
                created_index=index,
                mitigated_time=None,
                fully_filled_time=None,
                max_penetration=0.0,
                touches=0,
                reaction_count=0,
                last_reaction_time=None,
                last_reaction_close=None,
                touch_episode_active=False,
                touch_episode_reacted=False,
                flip_count=zone.flip_count + 1,
                flipped_from=zone.id,
                flipped_to=None,
                flipped_time=None,
            )
            zone.status = ZoneStatus.FLIPPED
            zone.flipped_to = new_zone.id
        else:
            zone.status = ZoneStatus.INVALIDATED

        signal = FlipSignal(
            old_zone_id=zone.id,
            new_zone_id=new_zone.id if new_zone else None,
            kind=zone.kind, symbol=zone.symbol, timeframe=zone.timeframe,
            old_bias=zone.bias, new_bias=new_zone.bias if new_zone else None,
            time=time, close=close, top=zone.top, bottom=zone.bottom,
            is_historical=is_historical,
        )
        log.log(10 if is_historical else 20,
                "%s %s %s %s broken by close %.5f -> %s", zone.symbol, zone.timeframe,
                zone.kind.value, zone.id, close, "flipped" if new_zone else "invalidated")
        return signal, new_zone
