"""The Plan model - a living trade idea, not a one-shot signal.

Status lifecycle
----------------
PLANNED            valid structural idea, price is still away from the entry zone
ACTIVE             price is within ``approach_atr_mult`` ATRs of the entry zone (watch closely)
READY_TO_EXECUTE   price has traded into the entry zone and nothing invalidated the idea;
                   Phase 4 (risk + execution) may act. Phase 3 stops here.
CANCELED           invalidation broken, expired, or otherwise no longer valid (see cancel_reason)
FLIPPED            its zone was broken by a body close; a child plan took over (see flip_reason)
EXECUTED           reserved for Phase 4 - never set in Phase 3
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import pandas as pd

from smc.core.timeframes import Timeframe
from smc.structure.models import Bias, Zone


class PlanDirection(Enum):
    LONG = "long"
    SHORT = "short"

    @property
    def opposite(self) -> "PlanDirection":
        return PlanDirection.SHORT if self is PlanDirection.LONG else PlanDirection.LONG

    @property
    def bias(self) -> Bias:
        return Bias.BULLISH if self is PlanDirection.LONG else Bias.BEARISH

    @classmethod
    def from_bias(cls, bias: Bias) -> "PlanDirection":
        if bias is Bias.NEUTRAL:
            raise ValueError("A neutral bias has no trade direction")
        return cls.LONG if bias is Bias.BULLISH else cls.SHORT


class PlanStatus(Enum):
    PLANNED = "planned"
    ACTIVE = "active"
    READY_TO_EXECUTE = "ready_to_execute"
    CANCELED = "canceled"
    FLIPPED = "flipped"
    EXECUTED = "executed"


LIVE_STATUSES = (PlanStatus.PLANNED, PlanStatus.ACTIVE, PlanStatus.READY_TO_EXECUTE)


class EntryModel(Enum):
    OB = "OB"
    FVG = "FVG"
    SWEEP_MSS = "SWEEP_MSS"
    OB_FVG = "OB_FVG"

    @property
    def priority(self) -> int:
        """Higher wins when two models describe the same idea (confluence first)."""
        return {"OB_FVG": 4, "SWEEP_MSS": 3, "OB": 2, "FVG": 1}[self.value]


class EntryRangeBasis(Enum):
    ORDER_BLOCK = "order_block"
    FAIR_VALUE_GAP = "fair_value_gap"
    OB_FVG_CONFLUENCE = "ob_fvg_confluence"
    SWEEP_MSS_ORDER_BLOCK = "sweep_mss_order_block"
    SWEEP_MSS_FVG = "sweep_mss_fvg"
    SWEEP_MSS_RETRACEMENT = "sweep_mss_retracement"
    FLIPPED_ZONE_RETEST = "flipped_zone_retest"


@dataclass(frozen=True)
class PriceRange:
    top: float
    bottom: float

    def near_edge(self, direction: PlanDirection) -> float:
        """Edge price reaches first on a retrace: top for longs, bottom for shorts."""
        return self.top if direction is PlanDirection.LONG else self.bottom

    def far_edge(self, direction: PlanDirection) -> float:
        return self.bottom if direction is PlanDirection.LONG else self.top

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2.0

    def touched_by(self, direction: PlanDirection, high: float, low: float) -> bool:
        return low <= self.top if direction is PlanDirection.LONG else high >= self.bottom


@dataclass(frozen=True)
class Invalidation:
    """Plan dies when a candle BODY CLOSES beyond ``price`` on the setup timeframe."""

    price: float
    basis: str  # human-readable, e.g. "below OB low - 0.1 ATR"


@dataclass(frozen=True)
class SourceTimeframes:
    external: tuple[Timeframe, ...]
    internal: tuple[Timeframe, ...]
    setup: Timeframe  # the timeframe whose candles define this plan's invalidation


@dataclass
class PlanZones:
    """Ids of the structure-engine objects the plan depends on."""

    order_block_id: Optional[str] = None
    fvg_id: Optional[str] = None
    flipped_zone_id: Optional[str] = None  # supply/demand zone created by a flip
    liquidity_level_id: Optional[str] = None

    @property
    def zone_ids(self) -> tuple[str, ...]:
        """Zones whose body-close break flips the plan (liquidity is not a zone)."""
        return tuple(dict.fromkeys(z for z in (self.order_block_id, self.fvg_id, self.flipped_zone_id) if z))

    @property
    def all_ids(self) -> tuple[str, ...]:
        return self.zone_ids + ((self.liquidity_level_id,) if self.liquidity_level_id else ())


@dataclass
class PlanQuality:
    has_fvg: bool = False  # OB that also created an FVG, or an OB+FVG confluence
    confluence: bool = False
    impulse_ratio: float = 0.0  # displacement in ATRs
    partially_filled: bool = False  # FVG already partly traded into
    counter_trend_internal: bool = False  # internal direction disagrees with external bias


@dataclass(frozen=True)
class PlanEvent:
    time: pd.Timestamp
    kind: str  # created | updated | status | canceled | flipped | upgraded
    note: str


@dataclass
class Plan:
    plan_id: str
    symbol: str
    direction: PlanDirection
    entry_model: EntryModel
    source_timeframes: SourceTimeframes
    external_bias: Bias
    internal_bias: Bias  # independent of external_bias - they may disagree
    zones: PlanZones
    entry_zone: PriceRange
    invalidation: Invalidation
    targets: list[float]
    rr_estimate: Optional[float]
    reason: str
    created_at: pd.Timestamp
    updated_at: pd.Timestamp
    external_range_position: Optional[float] = None  # 0..1 at the entry price
    internal_range_position: Optional[float] = None
    status: PlanStatus = PlanStatus.PLANNED
    cancel_reason: Optional[str] = None
    flip_reason: Optional[str] = None
    parent_plan_id: Optional[str] = None
    child_plan_ids: list[str] = field(default_factory=list)
    quality: PlanQuality = field(default_factory=PlanQuality)
    history: list[PlanEvent] = field(default_factory=list)
    entry_range_basis: Optional[EntryRangeBasis] = None
    source_zones: tuple[Zone, ...] = ()
    market_context: Optional[dict[str, object]] = None

    # ------------------------------------------------------------- helpers
    @property
    def is_live(self) -> bool:
        return self.status in LIVE_STATUSES

    @property
    def entry_price(self) -> float:
        """Reference entry: the near edge of the entry zone."""
        return self.entry_zone.near_edge(self.direction)

    @property
    def setup_timeframe(self) -> Timeframe:
        return self.source_timeframes.setup

    def is_invalidated_by_close(self, close: float) -> bool:
        if self.direction is PlanDirection.LONG:
            return close < self.invalidation.price
        return close > self.invalidation.price

    def log(self, time: pd.Timestamp, kind: str, note: str) -> None:
        self.history.append(PlanEvent(time, kind, note))
        self.updated_at = time

    def summary(self) -> str:
        pos = lambda v: "n/a" if v is None else f"{v:.2f}"  # noqa: E731
        rr = "n/a" if self.rr_estimate is None else f"{self.rr_estimate:.2f}"
        return (f"{self.plan_id} {self.symbol} {self.direction.value.upper()} {self.entry_model.value} "
                f"[{self.status.value}] zone {self.entry_zone.bottom:.2f}-{self.entry_zone.top:.2f} "
                f"SL {self.invalidation.price:.2f} TP {','.join(f'{t:.2f}' for t in self.targets) or 'n/a'} "
                f"RR {rr} P/D ext {pos(self.external_range_position)} int {pos(self.internal_range_position)}")
