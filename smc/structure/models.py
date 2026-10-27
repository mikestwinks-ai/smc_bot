"""Data models shared by every structure component.

Time convention: every ``time`` field is the candle's **open** time (tz-aware
UTC), matching the index produced by ``MarketDataManager``. ``index`` fields are
absolute bar numbers inside a ``CandleBuffer`` (they never change on trimming).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import pandas as pd

from smc.core.timeframes import StructureLayer, Timeframe


# ------------------------------------------------------------------ basics
class Bias(Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"

    @property
    def opposite(self) -> "Bias":
        if self is Bias.BULLISH:
            return Bias.BEARISH
        if self is Bias.BEARISH:
            return Bias.BULLISH
        return Bias.NEUTRAL

    @property
    def tag(self) -> str:
        return {"bullish": "B", "bearish": "S", "neutral": "N"}[self.value]


class SwingType(Enum):
    HIGH = "high"
    LOW = "low"


@dataclass(frozen=True)
class SwingPoint:
    type: SwingType
    price: float
    time: pd.Timestamp  # open time of the swing candle
    index: int  # absolute bar number of the swing candle
    confirmed_time: pd.Timestamp  # open time of the candle that confirmed it (right bars later)
    timeframe: Timeframe


# --------------------------------------------------------------- structure
class StructureEventType(Enum):
    BOS = "BOS"  # break in the direction of the current bias (or the first break)
    CHOCH = "CHOCH"  # break against the current bias
    MSS = "MSS"  # CHoCH made with displacement (internal layer only)


@dataclass(frozen=True)
class StructureEvent:
    id: str
    type: StructureEventType
    direction: Bias  # direction of the break (BULLISH = price broke upward)
    symbol: str
    timeframe: Timeframe
    layer: StructureLayer
    time: pd.Timestamp  # open time of the candle whose close broke the level
    index: int
    level: float  # price of the broken swing
    broken_swing: SwingPoint
    close: float
    bias_before: Bias
    bias_after: Bias
    origin_index: int  # start (extreme) of the impulse leg that made the break
    displacement: bool
    displacement_ratio: float  # largest directional body / ATR (0 if ATR unknown)


@dataclass
class TrackerUpdate:
    """What one candle changed inside a StructureTracker."""

    new_swings: list[SwingPoint] = field(default_factory=list)
    events: list[StructureEvent] = field(default_factory=list)


# ------------------------------------------------------------------- zones
class ZoneKind(Enum):
    ORDER_BLOCK = "OB"
    FVG = "FVG"


class ZoneStatus(Enum):
    ACTIVE = "active"  # untouched since creation
    MITIGATED = "mitigated"  # price has traded into it
    FLIPPED = "flipped"  # broken by a body close; replaced by an opposite zone
    INVALIDATED = "invalidated"  # broken by a body close, no replacement created


@dataclass(kw_only=True)
class Zone:
    """Generic supply/demand zone. BULLISH bias = demand, BEARISH = supply."""

    id: str
    kind: ZoneKind
    symbol: str
    timeframe: Timeframe
    bias: Bias
    top: float
    bottom: float
    created_time: pd.Timestamp  # when the zone became valid (candle close)
    created_index: int
    status: ZoneStatus = ZoneStatus.ACTIVE
    mitigated_time: Optional[pd.Timestamp] = None
    fully_filled_time: Optional[pd.Timestamp] = None
    max_penetration: float = 0.0  # 0..1 depth price has reached into the zone
    touches: int = 0
    reaction_count: int = 0
    last_reaction_time: Optional[pd.Timestamp] = None
    last_reaction_close: Optional[float] = None
    touch_episode_active: bool = False
    touch_episode_reacted: bool = False
    flip_count: int = 0
    flipped_from: Optional[str] = None  # id of the zone this one was flipped from
    flipped_to: Optional[str] = None  # id of the zone that replaced this one
    flipped_time: Optional[pd.Timestamp] = None

    @property
    def is_demand(self) -> bool:
        return self.bias is Bias.BULLISH

    @property
    def height(self) -> float:
        return self.top - self.bottom

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2.0

    @property
    def is_flipped_zone(self) -> bool:
        return self.flipped_from is not None

    @property
    def is_live(self) -> bool:
        return self.status in (ZoneStatus.ACTIVE, ZoneStatus.MITIGATED)

    def touched_by(self, high: float, low: float) -> bool:
        """True if the candle's range trades into the zone."""
        return low <= self.top if self.is_demand else high >= self.bottom

    def penetration(self, high: float, low: float) -> float:
        """Depth (0..1) a candle reaches into the zone, measured from the near edge."""
        if self.height <= 0:
            return 0.0
        raw = (self.top - low) if self.is_demand else (high - self.bottom)
        return min(max(raw / self.height, 0.0), 1.0)

    def broken_by_close(self, close: float) -> bool:
        """Body close through the far side of the zone."""
        return close < self.bottom if self.is_demand else close > self.top


@dataclass(kw_only=True)
class OrderBlock(Zone):
    candle_time: pd.Timestamp  # open time of the order-block candle itself
    candle_index: int
    leg_start_index: Optional[int] = None
    origin_event_id: str  # BOS / CHoCH / MSS that validated it
    origin_event_type: StructureEventType = StructureEventType.BOS
    has_fvg: bool  # the impulse leg also left a same-direction FVG
    displacement_ratio: float


@dataclass(kw_only=True)
class FairValueGap(Zone):
    gap_size: float
    stop_reference: float  # extreme of the 3-candle formation (default SL anchor)

    def calculate_rr(
        self,
        target: float,
        entry: Optional[float] = None,
        entry_mode: str = "edge",
        stop: Optional[float] = None,
        stop_buffer: float = 0.0,
    ) -> Optional[float]:
        """Reward:risk if this FVG were used as the entry.

        ``entry`` defaults to the near edge (``entry_mode="edge"``) or the
        midpoint (``"midpoint"``). ``stop`` defaults to the 3-candle extreme
        beyond the gap (+/- ``stop_buffer``). Returns ``None`` when the geometry
        is invalid (target on the wrong side, zero or negative risk).
        """
        if entry is None:
            if entry_mode == "midpoint":
                entry = self.midpoint
            else:
                entry = self.top if self.is_demand else self.bottom
        if stop is None:
            stop = self.stop_reference - stop_buffer if self.is_demand else self.stop_reference + stop_buffer
        risk = (entry - stop) if self.is_demand else (stop - entry)
        reward = (target - entry) if self.is_demand else (entry - target)
        if risk <= 0 or reward <= 0:
            return None
        return reward / risk

    def current_rr(self, price: float, target: float, **kwargs: float) -> Optional[float]:
        """RR if entered at the live ``price`` instead of the zone edge."""
        return self.calculate_rr(target, entry=price, **kwargs)

    def meets_min_rr(self, target: float, min_rr: float, **kwargs: object) -> bool:
        rr = self.calculate_rr(target, **kwargs)  # type: ignore[arg-type]
        return rr is not None and rr >= min_rr


@dataclass(frozen=True)
class FlipSignal:
    """Emitted when a zone is broken by a body close.

    Consumers (Phase 3 Plan Manager) must cancel any plan built on
    ``old_zone_id`` and, if ``new_zone_id`` is set, create an opposite plan for
    the retest of the flipped zone.
    """

    old_zone_id: str
    new_zone_id: Optional[str]
    kind: ZoneKind
    symbol: str
    timeframe: Timeframe
    old_bias: Bias
    new_bias: Optional[Bias]
    time: pd.Timestamp
    close: float
    top: float
    bottom: float
    is_historical: bool = False  # True while replaying warm-up history


# --------------------------------------------------------- premium/discount
class PDState(Enum):
    PREMIUM = "premium"
    EQUILIBRIUM = "equilibrium"
    DISCOUNT = "discount"


@dataclass(frozen=True)
class PDReading:
    layer: StructureLayer
    timeframe: Timeframe
    range_high: float
    range_low: float
    price: float
    position: float  # (price - low) / (high - low); can fall outside 0..1
    state: PDState


@dataclass(frozen=True)
class DualPDReading:
    external: Optional[PDReading]
    internal: Optional[PDReading]


# ---------------------------------------------------------------- liquidity
class LiquidityKind(Enum):
    EQUAL_HIGHS = "EQH"
    EQUAL_LOWS = "EQL"
    PREV_DAY_HIGH = "PDH"
    PREV_DAY_LOW = "PDL"
    PREV_SESSION_HIGH = "PSH"
    PREV_SESSION_LOW = "PSL"
    SWING_HIGH = "SWH"
    SWING_LOW = "SWL"


class LiquiditySide(Enum):
    BUY_SIDE = "buy_side"  # resting above price (highs)
    SELL_SIDE = "sell_side"  # resting below price (lows)


class LiquidityStatus(Enum):
    ACTIVE = "active"
    SWEPT = "swept"  # wick beyond, body closed back inside
    TAKEN = "taken"  # body closed beyond
    EXPIRED = "expired"  # replaced by a newer session/day level


@dataclass(kw_only=True)
class LiquidityLevel:
    id: str
    kind: LiquidityKind
    side: LiquiditySide
    price: float
    symbol: str
    timeframe: Timeframe
    created_time: pd.Timestamp
    label: str = ""  # e.g. session name
    touches: int = 1  # number of swings forming an equal level
    status: LiquidityStatus = LiquidityStatus.ACTIVE
    resolved_time: Optional[pd.Timestamp] = None


@dataclass(frozen=True)
class LiquiditySweep:
    level_id: str
    kind: LiquidityKind
    side: LiquiditySide
    symbol: str
    timeframe: Timeframe
    time: pd.Timestamp
    level_price: float
    extreme: float  # wick extreme beyond the level
    close: float
    is_historical: bool = False
    swept_level_ids: tuple[str, ...] = ()  # every level swept by this candle on this side (incl. level_id)

    @property
    def implied_bias(self) -> Bias:
        """Buy-side sweep (highs) hints at bearish reversal, and vice versa."""
        return Bias.BEARISH if self.side is LiquiditySide.BUY_SIDE else Bias.BULLISH


# ------------------------------------------------------------ hub outputs
@dataclass
class StructureUpdate:
    """Everything that changed while processing new candles for one symbol/timeframe."""

    symbol: str
    timeframe: Timeframe
    layer: StructureLayer
    candles_processed: int = 0
    is_warmup: bool = False
    swings: list[SwingPoint] = field(default_factory=list)
    events: list[StructureEvent] = field(default_factory=list)
    new_order_blocks: list[OrderBlock] = field(default_factory=list)
    new_fvgs: list[FairValueGap] = field(default_factory=list)
    mitigated_zones: list[Zone] = field(default_factory=list)
    flip_signals: list[FlipSignal] = field(default_factory=list)
    new_flipped_zones: list[Zone] = field(default_factory=list)
    new_liquidity_levels: list[LiquidityLevel] = field(default_factory=list)
    liquidity_sweeps: list[LiquiditySweep] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return any((self.events, self.new_order_blocks, self.new_fvgs, self.mitigated_zones,
                    self.flip_signals, self.new_liquidity_levels, self.liquidity_sweeps))
