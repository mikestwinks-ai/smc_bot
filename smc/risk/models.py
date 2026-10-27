"""Typed snapshots used by the risk and execution layers.

Everything that comes from the broker is converted once (in ``MT5Gateway``) into
these immutable objects, with all times in tz-aware UTC and a ``fetched_at``
stamp so staleness can be judged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import pandas as pd


@dataclass(frozen=True)
class AccountState:
    login: int
    currency: str
    balance: float
    equity: float
    margin_free: float
    leverage: int
    trade_allowed: bool
    is_real: bool
    fetched_at: pd.Timestamp


@dataclass(frozen=True)
class SymbolSpec:
    """Broker specification of one symbol (never assumed - always read from MT5)."""

    name: str
    digits: int
    point: float
    tick_size: float
    tick_value_profit: float  # money per tick per 1 lot, in ACCOUNT currency
    tick_value_loss: float
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level_points: int
    freeze_level_points: int
    trade_mode: int  # 0 disabled, 1 long only, 2 short only, 3 close only, 4 full
    filling_flags: int  # bit 1 = FOK, bit 2 = IOC
    expiration_flags: int  # bit 1 GTC, 2 DAY, 4 SPECIFIED, 8 SPECIFIED_DAY
    fetched_at: pd.Timestamp

    @property
    def stops_level(self) -> float:
        return self.stops_level_points * self.point

    @property
    def freeze_level(self) -> float:
        return self.freeze_level_points * self.point

    def problems(self) -> list[str]:
        """Reasons this spec cannot be used for sizing (empty list = usable)."""
        bad = []
        for label, v in (("tick_size", self.tick_size), ("tick_value_loss", self.tick_value_loss),
                         ("contract_size", self.contract_size), ("volume_min", self.volume_min),
                         ("volume_step", self.volume_step), ("point", self.point)):
            if not v or v <= 0:
                bad.append(f"symbol {self.name}: {label} missing/invalid ({v})")
        if self.volume_max < self.volume_min:
            bad.append(f"symbol {self.name}: volume_max {self.volume_max} < volume_min {self.volume_min}")
        return bad


@dataclass(frozen=True)
class TickState:
    bid: float
    ask: float
    time: pd.Timestamp  # tick time, UTC
    fetched_at: pd.Timestamp

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True)
class PositionInfo:
    ticket: int
    symbol: str  # broker symbol
    is_buy: bool
    volume: float
    price_open: float
    sl: float
    tp: float
    price_current: float
    swap: float
    profit: float
    time: pd.Timestamp  # open time, UTC
    magic: int
    comment: str
    identifier: Optional[int] = None


@dataclass(frozen=True)
class OrderInfo:
    ticket: int
    symbol: str
    is_buy: bool
    is_limit: bool  # False = stop order
    volume: float
    price_open: float
    sl: float
    tp: float
    time_setup: pd.Timestamp
    magic: int
    comment: str
    expiration: Optional[pd.Timestamp] = None


@dataclass(frozen=True)
class DealInfo:
    ticket: int
    order: int
    position_id: int
    time: pd.Timestamp
    is_entry: bool  # IN
    is_exit: bool  # OUT / OUT_BY / INOUT-close
    is_trade: bool  # buy/sell deal (False for balance/credit operations)
    volume: float
    price: float
    commission: float
    swap: float
    profit: float
    fee: float
    magic: int
    symbol: str
    comment: str
    is_buy: Optional[bool] = None
    is_reversal: bool = False

    @property
    def net(self) -> float:
        """Everything this deal did to the balance - commissions and swaps included."""
        return self.profit + self.commission + self.swap + self.fee


@dataclass(frozen=True)
class TradeOutcome:
    """One FULLY CLOSED trade (all deals of a position_id folded together)."""

    position_id: int
    symbol: str
    opened_at: pd.Timestamp
    closed_at: pd.Timestamp
    net: float  # profit + commission + swap + fee over every deal (partials included once)

    @property
    def is_loss(self) -> bool:
        return self.net < 0


@dataclass
class ExposureSnapshot:
    """Everything the limit checks need, rebuilt from MT5 (restart-safe)."""

    now: pd.Timestamp
    trading_day: str
    positions: list[PositionInfo]
    pending: list[OrderInfo]
    trades_opened_today: int
    closed_today: list[TradeOutcome]
    consecutive_losses: int
    realised_pl_today: float  # bot deals: profit + commission + swap + fee
    baseline_balance: float  # daily reference (documented in RiskManager)
    equity: float
    realised_loss_pct: float  # max(0, -realised) / baseline * 100
    total_drawdown_pct: float  # max(0, baseline - equity) / baseline * 100 (realised + floating)

    @property
    def floating_drawdown_pct(self) -> float:
        """Part of the drawdown that is floating rather than realised."""
        return max(0.0, self.total_drawdown_pct - self.realised_loss_pct)

    @property
    def open_count(self) -> int:
        return len(self.positions) + len(self.pending)

    def has_exposure(self, broker_symbol: str, is_buy: bool) -> bool:
        return (any(p.symbol == broker_symbol and p.is_buy == is_buy for p in self.positions)
                or any(o.symbol == broker_symbol and o.is_buy == is_buy for o in self.pending))


@dataclass(frozen=True)
class PositionSizing:
    volume: float
    risk_pct: float  # the configured percentage that applied
    risk_amount: float  # money budget = equity * risk_pct
    actual_risk: float  # money actually at risk with the rounded-down volume
    sl_distance: float
    loss_per_lot: float

    @property
    def actual_risk_pct_of(self) -> float:  # filled by RiskManager via equity; kept for logs
        return 0.0


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reasons: list[str] = field(default_factory=list)
    sizing: Optional[PositionSizing] = None
    snapshot: Optional[ExposureSnapshot] = None


class OrderKind(Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
