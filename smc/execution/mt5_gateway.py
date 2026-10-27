"""Typed gateway over the existing :class:`MT5Connection` (no second connection).

* Converts MT5 structures into the immutable models in ``smc.risk.models``.
* Every timestamp is converted from broker server time to UTC using the same
  ``server_utc_offset_hours`` Phase 1 applies to candles.
* ``send`` is the ONLY function that can trade, and it refuses unless live
  execution was explicitly enabled. Dry-run uses ``check`` (MT5 ``order_check``),
  which never places anything.
* Returns are classified so callers can tell "definitely failed" from "outcome
  unknown" - the latter must be reconciled, never blindly retried.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

import pandas as pd

from smc.config import BotConfig, SymbolConfig
from smc.core.exceptions import MT5DataError, SMCBotError, SymbolNotFoundError
from smc.core.mt5_connection import MT5Connection
from smc.risk.models import AccountState, DealInfo, OrderInfo, PositionInfo, SymbolSpec, TickState
from smc.utils.logger import get_logger

log = get_logger(__name__)

# MetaTrader5 constants (stable ABI values from the MT5 documentation)
ACTION_DEAL, ACTION_PENDING, ACTION_SLTP, ACTION_MODIFY, ACTION_REMOVE = 1, 5, 6, 7, 8
TYPE_BUY, TYPE_SELL, TYPE_BUY_LIMIT, TYPE_SELL_LIMIT, TYPE_BUY_STOP, TYPE_SELL_STOP = 0, 1, 2, 3, 4, 5
FILL_FOK, FILL_IOC, FILL_RETURN = 0, 1, 2
TIME_GTC, TIME_SPECIFIED = 0, 2
DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY = 0, 1, 2, 3
DEAL_TYPE_BUY, DEAL_TYPE_SELL = 0, 1
ACCOUNT_TRADE_MODE_REAL = 2
RET_REQUOTE, RET_REJECT, RET_PRICE_CHANGED = 10004, 10006, 10020
RET_PLACED, RET_DONE, RET_DONE_PARTIAL = 10008, 10009, 10010
RET_ERROR, RET_TIMEOUT, RET_CONNECTION = 10011, 10012, 10031
RET_NO_QUOTES, RET_OFF_QUOTES = 10021, 10021


class LiveTradingDisabledError(SMCBotError):
    """A trade action was attempted while live execution is disabled."""


class SendStatus(Enum):
    DONE = "done"
    PARTIAL = "partial"
    PLACED = "placed"
    REJECTED = "rejected"  # definite refusal: nothing happened
    RETRYABLE = "retryable"  # definite: requote / price changed, nothing happened
    UNCERTAIN = "uncertain"  # timeout / no answer: the order MAY exist


@dataclass(frozen=True)
class SendResult:
    status: SendStatus
    retcode: int
    comment: str = ""
    order: int = 0
    deal: int = 0
    volume: float = 0.0
    price: float = 0.0

    @property
    def success(self) -> bool:
        return self.status in (SendStatus.DONE, SendStatus.PARTIAL, SendStatus.PLACED)


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    retcode: int
    comment: str
    margin_free: Optional[float] = None


def classify_retcode(retcode: int) -> SendStatus:
    if retcode == RET_DONE:
        return SendStatus.DONE
    if retcode == RET_DONE_PARTIAL:
        return SendStatus.PARTIAL
    if retcode == RET_PLACED:
        return SendStatus.PLACED
    if retcode in (RET_REQUOTE, RET_PRICE_CHANGED, RET_NO_QUOTES):
        return SendStatus.RETRYABLE
    if retcode in (RET_TIMEOUT, RET_CONNECTION, RET_ERROR):
        return SendStatus.UNCERTAIN
    return SendStatus.REJECTED


class MT5Gateway:
    def __init__(self, connection: MT5Connection, config: BotConfig,
                 now_fn: Optional[Callable[[], pd.Timestamp]] = None) -> None:
        self._conn, self._cfg = connection, config
        self._now = now_fn or (lambda: pd.Timestamp.now(tz="UTC"))
        self._offset = pd.Timedelta(hours=config.broker.server_utc_offset_hours)
        self.journal = None
        self._last_account = None
        self._last_positions: Optional[list[PositionInfo]] = None
        self._last_positions_at: Optional[pd.Timestamp] = None
        self._last_pending_orders: Optional[list[OrderInfo]] = None
        self._last_pending_orders_at: Optional[pd.Timestamp] = None

    def observed_state(self) -> dict[str, Any]:
        """Return cached successful observations; this method never contacts MT5."""
        return {
            "account": self._last_account,
            "positions": list(self._last_positions) if self._last_positions is not None else None,
            "positions_at": self._last_positions_at,
            "pending_orders": list(self._last_pending_orders) if self._last_pending_orders is not None else None,
            "pending_orders_at": self._last_pending_orders_at,
        }

    # --------------------------------------------------------------- basics
    @property
    def live(self) -> bool:
        return self._cfg.execution.live_enabled

    @property
    def magic(self) -> int:
        return self._cfg.execution.magic_number

    @property
    def connected(self) -> bool:
        return self._conn.is_alive()

    def now(self) -> pd.Timestamp:
        return self._now()

    @property
    def _mt5(self) -> Any:
        was_alive = self._conn.is_alive()
        if not was_alive and self.journal is not None:
            self.journal.record_event_best_effort(
                "mt5_disconnect_detected", occurred_at=self.now(),
                idempotency_key=f"mt5-disconnect:{self.now().isoformat()}",
            )
        self._conn.ensure_connected()
        if not was_alive and self.journal is not None:
            self.journal.record_event_best_effort(
                "mt5_reconnected", occurred_at=self.now(),
                idempotency_key=f"mt5-reconnected:{self.now().isoformat()}",
            )
        return self._conn.mt5

    def _utc(self, epoch: float) -> pd.Timestamp:
        return pd.Timestamp(epoch, unit="s", tz="UTC") - self._offset

    def to_server_datetime(self, utc: pd.Timestamp) -> dt.datetime:
        """MT5 wants datetimes in 'server time pretending to be UTC'."""
        return (utc + self._offset).to_pydatetime()

    def symbol_config(self, canonical: str) -> SymbolConfig:
        for s in self._cfg.symbols:
            if s.name == canonical:
                return s
        raise SymbolNotFoundError(f"{canonical} is not a configured symbol")

    def broker_symbol(self, canonical: str) -> str:
        return self._conn.resolve_symbol(self.symbol_config(canonical))

    # -------------------------------------------------------------- snapshots
    def account(self) -> AccountState:
        info = self._mt5.account_info()
        if info is None:
            raise MT5DataError(f"account_info() failed: {self._mt5.last_error()}")
        result = AccountState(
            login=int(info.login), currency=str(info.currency), balance=float(info.balance),
            equity=float(info.equity), margin_free=float(info.margin_free), leverage=int(info.leverage),
            trade_allowed=bool(getattr(info, "trade_allowed", True)),
            is_real=int(getattr(info, "trade_mode", 0)) == ACCOUNT_TRADE_MODE_REAL, fetched_at=self.now())
        self._last_account = result
        return result

    def symbol_spec(self, broker_symbol: str) -> SymbolSpec:
        info = self._mt5.symbol_info(broker_symbol)
        if info is None:
            raise SymbolNotFoundError(f"symbol_info({broker_symbol}) returned None")
        g = lambda name, default=0: getattr(info, name, default) or default  # noqa: E731
        tv = g("trade_tick_value")
        return SymbolSpec(
            name=broker_symbol, digits=int(g("digits")), point=float(g("point")),
            tick_size=float(g("trade_tick_size")),
            tick_value_profit=float(g("trade_tick_value_profit", tv) or tv),
            tick_value_loss=float(g("trade_tick_value_loss", tv) or tv),
            contract_size=float(g("trade_contract_size")), volume_min=float(g("volume_min")),
            volume_max=float(g("volume_max")), volume_step=float(g("volume_step")),
            stops_level_points=int(g("trade_stops_level")), freeze_level_points=int(g("trade_freeze_level")),
            trade_mode=int(getattr(info, "trade_mode", 4)), filling_flags=int(g("filling_mode")),
            expiration_flags=int(g("expiration_mode", 1)), fetched_at=self.now())

    def tick(self, broker_symbol: str) -> TickState:
        t = self._mt5.symbol_info_tick(broker_symbol)
        if t is None:
            raise MT5DataError(f"symbol_info_tick({broker_symbol}) failed: {self._mt5.last_error()}")
        return TickState(bid=float(t.bid), ask=float(t.ask), time=self._utc(t.time), fetched_at=self.now())

    # ------------------------------------------------------ positions / orders
    def positions(self, only_bot: bool = True) -> list[PositionInfo]:
        raw = self._mt5.positions_get()
        rows = self._rows(raw, "positions_get")
        out = [PositionInfo(
            ticket=int(p.ticket), symbol=str(p.symbol), is_buy=int(p.type) == TYPE_BUY, volume=float(p.volume),
            price_open=float(p.price_open), sl=float(p.sl), tp=float(p.tp), price_current=float(p.price_current),
            swap=float(getattr(p, "swap", 0.0)), profit=float(getattr(p, "profit", 0.0)),
            time=self._utc(p.time), magic=int(p.magic), comment=str(getattr(p, "comment", "")),
            identifier=int(getattr(p, "identifier", p.ticket)))
            for p in rows]
        selected = [p for p in out if p.magic == self.magic] if only_bot else out
        self._last_positions = [p for p in selected if p.magic == self.magic]
        self._last_positions_at = self.now()
        return selected

    def pending_orders(self, only_bot: bool = True) -> list[OrderInfo]:
        rows = self._rows(self._mt5.orders_get(), "orders_get")
        out = []
        for o in rows:
            t = int(o.type)
            if t not in (TYPE_BUY_LIMIT, TYPE_SELL_LIMIT, TYPE_BUY_STOP, TYPE_SELL_STOP):
                continue
            exp = getattr(o, "time_expiration", 0)
            out.append(OrderInfo(
                ticket=int(o.ticket), symbol=str(o.symbol), is_buy=t in (TYPE_BUY_LIMIT, TYPE_BUY_STOP),
                is_limit=t in (TYPE_BUY_LIMIT, TYPE_SELL_LIMIT), volume=float(o.volume_current),
                price_open=float(o.price_open), sl=float(o.sl), tp=float(o.tp),
                time_setup=self._utc(o.time_setup), magic=int(o.magic), comment=str(getattr(o, "comment", "")),
                expiration=self._utc(exp) if exp else None))
        selected = [o for o in out if o.magic == self.magic] if only_bot else out
        self._last_pending_orders = [o for o in selected if o.magic == self.magic]
        self._last_pending_orders_at = self.now()
        return selected

    def deals(self, start: pd.Timestamp, end: pd.Timestamp) -> list[DealInfo]:
        try:
            raw = self._mt5.history_deals_get(self.to_server_datetime(start), self.to_server_datetime(end))
        except Exception as exc:
            raise MT5DataError(f"history_deals_get raised {type(exc).__name__}") from exc
        out = []
        for d in self._rows(raw, "history_deals_get"):
            entry, typ = int(getattr(d, "entry", 0)), int(d.type)
            out.append(DealInfo(
                ticket=int(d.ticket), order=int(getattr(d, "order", 0)), position_id=int(getattr(d, "position_id", 0)),
                time=self._utc(d.time), is_entry=entry == DEAL_ENTRY_IN,
                is_exit=entry in (DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY, DEAL_ENTRY_INOUT),
                is_trade=typ in (DEAL_TYPE_BUY, DEAL_TYPE_SELL), volume=float(d.volume), price=float(d.price),
                commission=float(getattr(d, "commission", 0.0)), swap=float(getattr(d, "swap", 0.0)),
                profit=float(getattr(d, "profit", 0.0)), fee=float(getattr(d, "fee", 0.0)),
                magic=int(getattr(d, "magic", 0)), symbol=str(getattr(d, "symbol", "")),
                comment=str(getattr(d, "comment", "")), is_buy=typ == DEAL_TYPE_BUY,
                is_reversal=entry == DEAL_ENTRY_INOUT))
        return out

    def history_orders(self, start: pd.Timestamp, end: pd.Timestamp) -> list[dict[str, Any]]:
        """Read historical orders without issuing any trade action."""
        try:
            raw = self._mt5.history_orders_get(self.to_server_datetime(start), self.to_server_datetime(end))
        except Exception as exc:
            raise MT5DataError(f"history_orders_get raised {type(exc).__name__}") from exc
        out = []
        for order in self._rows(raw, "history_orders_get"):
            order_type = int(getattr(order, "type", -1))
            setup = getattr(order, "time_setup", 0)
            done = getattr(order, "time_done", 0)
            out.append({
                "ticket": int(order.ticket),
                "magic": int(getattr(order, "magic", 0)),
                "symbol": str(getattr(order, "symbol", "")),
                "direction": "buy" if order_type in (TYPE_BUY, TYPE_BUY_LIMIT, TYPE_BUY_STOP) else "sell",
                "type": order_type,
                "state": int(getattr(order, "state", -1)),
                "time_setup": self._utc(setup) if setup else None,
                "time_done": self._utc(done) if done else None,
                "volume_initial": float(getattr(order, "volume_initial", 0.0)),
                "volume_current": float(getattr(order, "volume_current", 0.0)),
                "price_open": float(getattr(order, "price_open", 0.0)),
                "sl": float(getattr(order, "sl", 0.0)),
                "tp": float(getattr(order, "tp", 0.0)),
                "comment": str(getattr(order, "comment", "")),
            })
        return out

    def _rows(self, raw: Any, what: str) -> list[Any]:
        """MT5 returns None for 'nothing' AND for errors - tell them apart (fail closed)."""
        if raw is None:
            err = self._mt5.last_error()
            if err and int(err[0]) not in (1,):  # 1 = RES_S_OK
                raise MT5DataError(f"{what} failed: {err}")
            return []
        return list(raw)

    # ------------------------------------------------------------- trade calls
    def check(self, request: dict[str, Any]) -> CheckResult:
        """MT5 ``order_check``: validates margin/stops/volume. Never places anything."""
        try:
            res = self._mt5.order_check(request)
        except Exception as exc:
            return CheckResult(False, -1, f"order_check raised {exc!r}")
        if res is None:
            return CheckResult(False, -2, f"order_check returned None: {self._mt5.last_error()}")
        ok = int(res.retcode) in (0, RET_DONE)
        return CheckResult(ok, int(res.retcode), str(getattr(res, "comment", "")),
                           float(getattr(res, "margin_free", 0.0)) if ok else None)

    def send(self, request: dict[str, Any]) -> SendResult:
        """The ONLY place an order can leave the bot. Raises unless live is enabled."""
        if not self.live:
            raise LiveTradingDisabledError("order_send blocked: live execution is disabled (dry-run)")
        try:
            res = self._mt5.order_send(request)
        except Exception as exc:
            return SendResult(SendStatus.UNCERTAIN, -1, f"order_send raised {exc!r}")
        if res is None:
            return SendResult(SendStatus.UNCERTAIN, -2, f"order_send returned None: {self._mt5.last_error()}")
        rc = int(res.retcode)
        return SendResult(classify_retcode(rc), rc, str(getattr(res, "comment", "")), int(getattr(res, "order", 0)),
                          int(getattr(res, "deal", 0)), float(getattr(res, "volume", 0.0)),
                          float(getattr(res, "price", 0.0)))
