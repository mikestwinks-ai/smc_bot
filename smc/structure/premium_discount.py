"""Step 4 - Dual Premium / Discount calculator (External + Internal ranges).

position = (price - range_low) / (range_high - range_low)
  > 0.5 + band  -> PREMIUM,  < 0.5 - band -> DISCOUNT,  otherwise EQUILIBRIUM.
Position may fall outside 0..1 when price trades beyond the range.
"""

from __future__ import annotations

from typing import Optional

from smc.config import StructureConfig
from smc.core.timeframes import StructureLayer, Timeframe
from smc.structure.models import DualPDReading, PDReading, PDState
from smc.structure.tracker import StructureTracker


class PremiumDiscountCalculator:
    def __init__(self, cfg: StructureConfig) -> None:
        self._band = cfg.pd_equilibrium_band

    def calculate(self, range_high: float, range_low: float, price: float,
                  layer: StructureLayer, timeframe: Timeframe) -> Optional[PDReading]:
        if range_high <= range_low:
            return None
        position = (price - range_low) / (range_high - range_low)
        if position > 0.5 + self._band:
            state = PDState.PREMIUM
        elif position < 0.5 - self._band:
            state = PDState.DISCOUNT
        else:
            state = PDState.EQUILIBRIUM
        return PDReading(layer, timeframe, range_high, range_low, price, position, state)

    def from_tracker(self, tracker: Optional[StructureTracker], price: float) -> Optional[PDReading]:
        if tracker is None:
            return None
        rng = tracker.dealing_range()
        if rng is None:
            return None
        return self.calculate(rng[0], rng[1], price, tracker.layer, tracker.timeframe)


class DualPremiumDiscount:
    """Computes the External and Internal readings side by side."""

    def __init__(self, cfg: StructureConfig) -> None:
        self._calc = PremiumDiscountCalculator(cfg)

    def calculate(self, external_tracker: Optional[StructureTracker],
                  internal_tracker: Optional[StructureTracker], price: float) -> DualPDReading:
        return DualPDReading(
            external=self._calc.from_tracker(external_tracker, price),
            internal=self._calc.from_tracker(internal_tracker, price),
        )
