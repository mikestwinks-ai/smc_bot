"""Phase 2 - core structure engine.

Entry point: :class:`smc.structure.engine.StructureHub`.
Components (in build order): swings -> tracker/layers -> premium_discount ->
order_blocks -> fvg -> liquidity -> flipped_zones (zone storage in zones.py).
"""

from smc.structure.engine import StructureHub, StructureSnapshot
from smc.structure.models import (
    Bias, FairValueGap, FlipSignal, LiquidityLevel, LiquiditySweep, OrderBlock, PDState,
    StructureEvent, StructureEventType, StructureUpdate, SwingPoint, SwingType, Zone, ZoneStatus,
)
from smc.structure.swings import detect_swings

__all__ = [
    "StructureHub", "StructureSnapshot", "detect_swings", "Bias", "FairValueGap", "FlipSignal",
    "LiquidityLevel", "LiquiditySweep", "OrderBlock", "PDState", "StructureEvent",
    "StructureEventType", "StructureUpdate", "SwingPoint", "SwingType", "Zone", "ZoneStatus",
]
