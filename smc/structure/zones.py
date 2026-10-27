"""Zone storage + mitigation (shared by Order Blocks and FVGs).

Mitigation: the first time a candle's range trades into a zone it becomes
MITIGATED (``mitigated_time`` set). Deeper trades keep raising ``max_penetration``.
A body close through the far side is NOT mitigation - that is a break and is
handled by ``flipped_zones.py``.
"""

from __future__ import annotations

from typing import Iterable, Optional

import pandas as pd

from smc.core.timeframes import Timeframe
from smc.structure.models import (
    Bias, FairValueGap, OrderBlock, Zone, ZoneKind, ZoneStatus,
)


def apply_mitigation(
    zone: Zone,
    time: pd.Timestamp,
    high: float,
    low: float,
    close: Optional[float] = None,
) -> bool:
    """Update ``zone`` with a candle. Returns True only on the FIRST mitigation."""
    if not zone.is_live:
        return False
    if not zone.touched_by(high, low):
        zone.touch_episode_active = False
        zone.touch_episode_reacted = False
        return False

    if not zone.touch_episode_active:
        zone.touch_episode_active = True
        zone.touch_episode_reacted = False
    zone.touches += 1
    zone.max_penetration = max(zone.max_penetration, zone.penetration(high, low))
    if zone.max_penetration >= 1.0 and zone.fully_filled_time is None:
        zone.fully_filled_time = time
    rejected = (
        close is not None
        and (close > zone.top if zone.is_demand else close < zone.bottom)
    )
    if rejected and not zone.touch_episode_reacted:
        zone.reaction_count += 1
        zone.last_reaction_time = time
        zone.last_reaction_close = close
        zone.touch_episode_reacted = True
    if zone.status is ZoneStatus.ACTIVE:
        zone.status = ZoneStatus.MITIGATED
        zone.mitigated_time = time
        return True
    return False


class ZoneBook:
    """All zones, grouped by (symbol, timeframe)."""

    def __init__(self, max_per_timeframe: int = 150) -> None:
        self._max = max_per_timeframe
        self._zones: dict[tuple[str, Timeframe], list[Zone]] = {}
        self._by_id: dict[str, Zone] = {}

    def add(self, zone: Zone) -> None:
        bucket = self._zones.setdefault((zone.symbol, zone.timeframe), [])
        bucket.append(zone)
        self._by_id[zone.id] = zone
        if len(bucket) > self._max:  # drop oldest dead zones first, then oldest overall
            dead = [z for z in bucket if not z.is_live]
            victim = dead[0] if dead else bucket[0]
            bucket.remove(victim)
            self._by_id.pop(victim.id, None)

    def get(self, zone_id: str) -> Optional[Zone]:
        return self._by_id.get(zone_id)

    def zones(self, symbol: str, tf: Timeframe) -> list[Zone]:
        return list(self._zones.get((symbol, tf), []))

    def live(self, symbol: str, tf: Optional[Timeframe] = None, kind: Optional[ZoneKind] = None,
             bias: Optional[Bias] = None, untouched_only: bool = False) -> list[Zone]:
        """Zones still in play (ACTIVE, or MITIGATED unless ``untouched_only``)."""
        out: list[Zone] = []
        for (sym, ztf), bucket in self._zones.items():
            if sym != symbol or (tf is not None and ztf != tf):
                continue
            for z in bucket:
                if not z.is_live or (kind and z.kind is not kind) or (bias and z.bias is not bias):
                    continue
                if untouched_only and z.status is not ZoneStatus.ACTIVE:
                    continue
                out.append(z)
        return out

    def order_blocks(self, symbol: str, tf: Timeframe) -> list[OrderBlock]:
        return [z for z in self.zones(symbol, tf) if isinstance(z, OrderBlock)]

    def fvgs(self, symbol: str, tf: Timeframe) -> list[FairValueGap]:
        return [z for z in self.zones(symbol, tf) if isinstance(z, FairValueGap)]

    def all_for(self, symbol: str) -> Iterable[Zone]:
        for (sym, _), bucket in self._zones.items():
            if sym == symbol:
                yield from bucket
