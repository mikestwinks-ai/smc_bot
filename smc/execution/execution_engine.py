"""Execution engine: plan -> validated, risk-sized, idempotent MT5 order.

Pipeline for every plan (any failure stops it with a human-readable reason):
  kill switch -> idempotency/reconciliation -> live/real-account gate -> session
  -> plan validation -> risk limits + sizing -> request -> RE-VALIDATION with fresh
  data -> order_check -> (dry-run: stop here) -> write-ahead intent -> order_send
  -> classify result -> reconcile with MT5 -> update plan lifecycle.

Idempotency: the key ``<plan_id>|<ddHHMM of creation>`` is written to the MT5 order
comment and to the state file BEFORE sending. An uncertain answer (timeout, None)
is never retried blindly: the broker's orders, positions and deals are searched
first, and a retry only happens after ``uncertain_hold_seconds`` with nothing found.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

import pandas as pd

from smc.config import BotConfig
from smc.core.exceptions import SMCBotError
from smc.execution import order_builder as ob
from smc.execution import state_store as st
from smc.execution.fills import FillRegistrar, exec_key
from smc.execution.kill_switch import KillSwitch
from smc.execution.mt5_gateway import MT5Gateway, SendStatus
from smc.execution.state_store import StateStore
from smc.execution.validation import EXECUTABLE, PlanValidator, ValidatedOrder
from smc.journal.store import JournalStore
from smc.models.plan import Plan, PlanDirection, PlanStatus
from smc.news.guard import NewsGuard
from smc.planning.context import StructureView
from smc.planning.plan_store import PlanStore
from smc.risk.models import AccountState, OrderKind, PositionSizing, SymbolSpec, TickState
from smc.risk.risk_manager import RiskManager
from smc.risk.sessions import SessionClock
from smc.utils.logger import get_logger

log = get_logger(__name__)


class Outcome(Enum):
    BLOCKED = "blocked"  # kill switch / live gate
    REJECTED = "rejected"  # failed validation, session or risk
    DRY_RUN = "dry_run"  # would have been sent; nothing was
    PENDING_PLACED = "pending_placed"
    FILLED = "filled"
    ALREADY = "already_executed"
    AWAITING = "awaiting_reconciliation"
    UNCERTAIN = "uncertain"
    FAILED = "failed"


@dataclass
class ExecResult:
    plan_id: str
    outcome: Outcome
    reasons: list[str] = field(default_factory=list)
    order_ticket: int = 0
    position_ticket: int = 0
    volume: float = 0.0
    request: Optional[dict[str, Any]] = None
    sizing: Optional[PositionSizing] = None

    @property
    def sent(self) -> bool:
        return self.outcome in (Outcome.PENDING_PLACED, Outcome.FILLED)


@dataclass
class _Prepared:
    request: dict[str, Any]
    validated: ValidatedOrder
    sizing: PositionSizing
    broker_symbol: str


class ExecutionEngine:
    def __init__(self, config: BotConfig, gateway: MT5Gateway, plans: PlanStore, risk: RiskManager,
                 state: StateStore, clock: SessionClock, kill: KillSwitch, view: Optional[StructureView],
                 registrar: FillRegistrar, news_guard: Optional[NewsGuard] = None,
                 journal: Optional[JournalStore] = None) -> None:
        self._news = news_guard  # Phase 5: None = no news protection attached (tests / explicit opt-out)
        self._cfg, self._gw, self._plans, self._risk = config, gateway, plans, risk
        self._state, self._clock, self._kill, self._reg = state, clock, kill, registrar
        self._journal = journal
        self._validator = PlanValidator(config, plans, view)

    # ------------------------------------------------------------------ batch
    def process_all(self) -> list[ExecResult]:
        out = []
        for plan in self._plans.list_active():
            if plan.status in EXECUTABLE:
                out.append(self.process_plan(plan))
        return out

    # ------------------------------------------------------------ single plan
    def process_plan(self, plan: Plan) -> ExecResult:
        result = self._process_plan(plan)
        if self._journal is not None:
            kind = "risk_check_rejected" if any(reason.startswith("risk:") for reason in result.reasons) else \
                "kill_switch_block" if result.outcome is Outcome.BLOCKED and self._kill.engaged else \
                "plan_rejected" if result.outcome is Outcome.REJECTED else "execution_result"
            self._journal.record_event_best_effort(
                kind, occurred_at=self._gw.now(),
                idempotency_key=f"execution-result:{exec_key(plan.plan_id, plan.created_at)}:{result.outcome.value}",
                correlation_id=exec_key(plan.plan_id, plan.created_at), plan_id=plan.plan_id,
                symbol=plan.symbol, direction=plan.direction.value, outcome=result.outcome.value,
                reasons=result.reasons, order_ticket=result.order_ticket or None,
                position_id=result.position_ticket or None, volume=result.volume,
            )
        return result

    def _process_plan(self, plan: Plan) -> ExecResult:
        now = self._gw.now()
        key = exec_key(plan.plan_id, plan.created_at)
        pid = plan.plan_id
        if self._kill.engaged:
            return ExecResult(pid, Outcome.BLOCKED, [f"kill switch engaged ({self._kill.reason})"])
        try:
            prior = self._prior_outcome(plan, key, now)
            if prior is not None:
                return prior
            gate = self._live_gate()
            if gate:
                return ExecResult(pid, Outcome.BLOCKED, gate)
            prep, reasons = self._prepare(plan, now)
            if prep is None:
                return ExecResult(pid, Outcome.REJECTED, reasons)

            # ---- re-validate IMMEDIATELY before submission, with fresh data
            now2 = self._gw.now()
            if self._kill.engaged:
                return ExecResult(pid, Outcome.BLOCKED, ["kill switch engaged"])
            prep, reasons = self._prepare(plan, now2, final=True)
            if prep is None:
                return ExecResult(pid, Outcome.REJECTED, ["final re-validation failed: " + "; ".join(reasons)])

            chk = self._gw.check(prep.request)
            if not chk.ok:
                return ExecResult(pid, Outcome.REJECTED,
                                  [f"MT5 order_check refused (retcode {chk.retcode}): {chk.comment}"])
            if not self._gw.live:
                log.info("DRY-RUN %s: would send %s %s %.2f lots %s @ %s SL %s TP %s (order_check ok; nothing sent)",
                         pid, prep.validated.kind.value, plan.direction.value, prep.sizing.volume,
                         prep.broker_symbol, prep.validated.entry, prep.validated.sl, prep.validated.tp)
                return ExecResult(pid, Outcome.DRY_RUN, ["live execution disabled: validated and order_check passed, "
                                                         "nothing was sent"], request=prep.request, sizing=prep.sizing)
            return self._send(plan, key, prep, now2)
        except SMCBotError as exc:  # missing data of any kind => fail closed
            log.warning("EXEC %s failed closed: %s", pid, exc)
            return ExecResult(pid, Outcome.REJECTED, [f"failing closed: {exc}"])

    # ---------------------------------------------------------------- gates
    def _live_gate(self) -> list[str]:
        if not self._gw.live:
            return []
        acct = self._gw.account()
        if acct.is_real and not self._cfg.execution.allow_real_account:
            return ["live execution on a REAL account is not allowed (SMC_ALLOW_REAL_ACCOUNT is not true)"]
        return []

    def _prepare(self, plan: Plan, now: pd.Timestamp, final: bool = False) -> tuple[Optional[_Prepared], list[str]]:
        sym = self._gw.symbol_config(plan.symbol)
        ok, why = self._clock.entries_allowed(now, sym)
        if not ok:
            return None, [f"session rule: {why}"]
        if self._news is not None:  # Phase 5: runs on BOTH passes, i.e. again right before submission
            lock = self._news.check(plan.symbol, now)
            if self._journal is not None:
                self._journal.record_event_best_effort(
                    "news_lock_decision", occurred_at=now,
                    idempotency_key=(f"news-decision:{exec_key(plan.plan_id, plan.created_at)}:"
                                     f"{now.isoformat()}:{int(final)}"),
                    correlation_id=exec_key(plan.plan_id, plan.created_at), plan_id=plan.plan_id,
                    symbol=plan.symbol, locked=lock.locked, code=lock.code,
                    reasons=lock.reasons, event_id=lock.event.id if lock.event else None,
                )
            if lock.locked:
                return None, [f"news lock: {lock.message()}"]
        broker = self._gw.broker_symbol(plan.symbol)
        account: AccountState = self._gw.account()
        spec: SymbolSpec = self._gw.symbol_spec(broker)
        tick: TickState = self._gw.tick(broker)

        v = self._validator.validate(plan, spec, tick, now)
        if not v.ok or v.order is None:
            return None, v.reasons
        o = v.order
        is_buy = plan.direction is PlanDirection.LONG
        decision = self._risk.evaluate(account=account, spec=spec, broker_symbol=broker, is_buy=is_buy,
                                       entry=o.entry, sl=o.sl, now=now)
        if self._journal is not None:
            snap = decision.snapshot
            self._journal.record_event_best_effort(
                "risk_check", occurred_at=now,
                idempotency_key=(f"risk-check:{exec_key(plan.plan_id, plan.created_at)}:"
                                 f"{now.isoformat()}:{int(final)}"),
                correlation_id=exec_key(plan.plan_id, plan.created_at), plan_id=plan.plan_id,
                symbol=plan.symbol, direction=plan.direction.value, approved=decision.approved,
                reasons=decision.reasons,
                trading_day=snap.trading_day if snap else None,
                trades_opened_today=snap.trades_opened_today if snap else None,
                consecutive_losses=snap.consecutive_losses if snap else None,
                realised_loss_pct=snap.realised_loss_pct if snap else None,
                total_drawdown_pct=snap.total_drawdown_pct if snap else None,
                baseline_balance=snap.baseline_balance if snap else None,
            )
        if not decision.approved or decision.sizing is None:
            return None, ["risk: " + r for r in decision.reasons]

        expiry = None
        if o.kind is not OrderKind.MARKET and self._cfg.execution.pending_expire_at_cutoff:
            exp_utc = self._clock.pending_expiry(now, sym)
            nxt = self._news.next_lock_start(plan.symbol, now) if self._news is not None else None
            if nxt is not None and (exp_utc is None or nxt < exp_utc):
                exp_utc = nxt  # broker-side backstop: the order cannot rest into the next news lock
            expiry = self._gw.to_server_datetime(exp_utc) if exp_utc is not None else None
        key = exec_key(plan.plan_id, plan.created_at)
        req = ob.build_entry(o.kind, is_buy, broker, decision.sizing.volume, o.entry, o.sl, o.tp, spec,
                             self._gw.magic, self._cfg.execution.max_slippage_points, key, expiry)
        return _Prepared(req, o, decision.sizing, broker), []

    # ------------------------------------------------------------- reconcile
    def _find_in_mt5(self, key: str, now: pd.Timestamp):
        for p in self._gw.positions():
            if p.comment.startswith(key):
                return "position", p
        for o in self._gw.pending_orders():
            if o.comment.startswith(key):
                return "order", o
        day_start = self._clock.trading_day_start_utc(now)
        for d in self._gw.deals(day_start - pd.Timedelta(days=1), now + pd.Timedelta(hours=1)):
            if d.magic == self._gw.magic and d.is_entry and d.comment.startswith(key):
                return "closed", d
        return None

    def _prior_outcome(self, plan: Plan, key: str, now: pd.Timestamp) -> Optional[ExecResult]:
        """Broker truth first, then our own record. Returns a result if nothing may be sent."""
        pid = plan.plan_id
        found = self._find_in_mt5(key, now)
        if found:
            kind, obj = found
            if kind == "position":
                self._reg.register(obj, key, now)
                return ExecResult(pid, Outcome.ALREADY, [f"already executed: position {obj.ticket} exists"],
                                  position_ticket=obj.ticket, volume=obj.volume)
            if kind == "order":
                self._state.set_intent(key, plan_id=pid, status=st.PLACED, order=obj.ticket)
                return ExecResult(pid, Outcome.ALREADY, [f"pending order {obj.ticket} already placed"],
                                  order_ticket=obj.ticket)
            self._state.set_intent(key, plan_id=pid, status=st.FILLED)
            self._mark_executed(plan, now, "executed earlier today (position already closed)")
            return ExecResult(pid, Outcome.ALREADY, ["already executed (position opened and closed)"])

        intent = self._state.get_intent(key)
        if not intent:
            return None
        status, attempts = intent.get("status"), int(intent.get("attempts", 0))
        if status == st.FILLED:  # recorded as filled but invisible now: still never re-enter
            return ExecResult(pid, Outcome.ALREADY, ["already executed (state record)"])
        if status in (st.SENDING, st.UNCERTAIN):
            sent_at = pd.Timestamp(intent.get("sent_at", now.isoformat()))
            age = (now - sent_at).total_seconds()
            if age < self._cfg.execution.uncertain_hold_seconds:
                return ExecResult(pid, Outcome.AWAITING, [
                    f"previous send outcome unknown ({age:.0f}s ago); waiting for MT5 reconciliation, not retrying"])
            self._state.set_intent(key, status=st.FAILED, note="not visible at broker after hold period")
            log.warning("EXEC %s: uncertain send not found at broker after %.0fs - treated as not executed", pid, age)
        elif status == st.REJECTED:
            return ExecResult(pid, Outcome.REJECTED, [f"broker rejected earlier: {intent.get('note', '')}"])
        elif status == st.PLACED:  # order vanished without a fill (expired / cancelled)
            self._state.set_intent(key, status=st.CANCELED, note="pending order gone without a fill")
        if attempts >= self._cfg.execution.max_send_attempts:
            return ExecResult(pid, Outcome.REJECTED, [f"max send attempts ({attempts}) reached for this plan"])
        return None

    # ------------------------------------------------------------------ send
    def _send(self, plan: Plan, key: str, prep: _Prepared, now: pd.Timestamp) -> ExecResult:
        pid, req = plan.plan_id, prep.request
        prev = self._state.get_intent(key) or {}
        self._state.set_intent(  # write-ahead: a crash after this line is reconciled, never re-sent blindly
            key, plan_id=pid, status=st.SENDING, attempts=int(prev.get("attempts", 0)) + 1,
            sent_at=now.isoformat(), kind=prep.validated.kind.value, symbol=plan.symbol, is_buy=req["type"] in (0, 2, 4),
            volume=prep.sizing.volume, entry=prep.validated.entry, sl=prep.validated.sl, tp=prep.validated.tp,
            setup_tf=plan.setup_timeframe.label)
        if self._journal is not None:
            self._journal.record_event_best_effort(
                "order_submitted", occurred_at=now, correlation_id=key,
                idempotency_key=f"order-submit:{key}:{int(prev.get('attempts', 0)) + 1}",
                plan_id=pid, symbol=plan.symbol, direction=plan.direction.value,
                volume=prep.sizing.volume, order_kind=prep.validated.kind.value,
            )
        res = self._gw.send(req)
        base = dict(request=req, sizing=prep.sizing, volume=prep.sizing.volume)

        if res.status in (SendStatus.DONE, SendStatus.PARTIAL, SendStatus.PLACED):
            if self._journal is not None:
                self._journal.record_event_best_effort(
                    "order_accepted", occurred_at=now, correlation_id=key,
                    idempotency_key=f"order-accepted:{key}:{res.order}:{res.deal}",
                    plan_id=pid, order_ticket=res.order or None, deal_ticket=res.deal or None,
                    symbol=plan.symbol, direction=plan.direction.value, volume=res.volume, price=res.price,
                    broker_status=res.status.value,
                )
            if prep.validated.kind is OrderKind.MARKET:
                return self._confirm_market(plan, key, res, now, base)
            self._state.set_intent(key, status=st.PLACED, order=res.order, note=res.comment)
            log.info("ORDER PLACED %s: %s %s %.2f @ %s SL %s TP %s (order %s)", pid, prep.validated.kind.value,
                     plan.direction.value, prep.sizing.volume, prep.validated.entry, prep.validated.sl,
                     prep.validated.tp, res.order)
            return ExecResult(pid, Outcome.PENDING_PLACED, [], order_ticket=res.order, **base)
        if res.status is SendStatus.UNCERTAIN:
            if self._journal is not None:
                self._journal.record_event_best_effort(
                    "order_uncertain", occurred_at=now, correlation_id=key,
                    idempotency_key=f"order-uncertain:{key}:{int(prev.get('attempts', 0)) + 1}",
                    plan_id=pid, order_ticket=res.order or None, deal_ticket=res.deal or None,
                    symbol=plan.symbol, broker_comment=res.comment,
                )
            self._state.set_intent(key, status=st.UNCERTAIN, note=res.comment, order=res.order)
            found = self._find_in_mt5(key, now)  # reconcile BEFORE anything else can happen
            if found:
                return self._prior_outcome(plan, key, now) or ExecResult(pid, Outcome.UNCERTAIN, ["reconciled"], **base)
            return ExecResult(pid, Outcome.UNCERTAIN, [f"outcome unknown ({res.comment}); will reconcile, not retry"], **base)
        if res.status is SendStatus.RETRYABLE:
            if self._journal is not None:
                self._journal.record_event_best_effort(
                    "order_rejected", occurred_at=now, correlation_id=key,
                    idempotency_key=f"order-rejected:{key}:{int(prev.get('attempts', 0)) + 1}",
                    plan_id=pid, symbol=plan.symbol, retcode=res.retcode, broker_comment=res.comment,
                    retryable=True,
                )
            self._state.set_intent(key, status=st.FAILED, note=f"{res.retcode} {res.comment}")
            return ExecResult(pid, Outcome.FAILED, [f"retryable broker answer {res.retcode} {res.comment}"], **base)
        self._state.set_intent(key, status=st.REJECTED, note=f"{res.retcode} {res.comment}")
        if self._journal is not None:
            self._journal.record_event_best_effort(
                "order_rejected", occurred_at=now, correlation_id=key,
                idempotency_key=f"order-rejected:{key}:{int(prev.get('attempts', 0)) + 1}",
                plan_id=pid, symbol=plan.symbol, retcode=res.retcode, broker_comment=res.comment,
                retryable=False,
            )
        return ExecResult(pid, Outcome.FAILED, [f"broker rejected: {res.retcode} {res.comment}"], **base)

    def _confirm_market(self, plan: Plan, key: str, res, now: pd.Timestamp, base: dict) -> ExecResult:
        """A DONE answer is not enough: find the real position (partial fills included)."""
        pid = plan.plan_id
        self._state.set_intent(key, status=st.UNCERTAIN, order=res.order, note="sent; awaiting position confirmation")
        for p in self._gw.positions():
            if p.comment.startswith(key):
                self._reg.register(p, key, now)
                if p.volume < base["volume"] - 1e-9:
                    log.warning("EXEC %s partially filled: %.2f of %.2f lots - remainder is NOT re-sent", pid,
                                p.volume, base["volume"])
                log.info("ORDER FILLED %s: position %s %.2f lots @ %s", pid, p.ticket, p.volume, p.price_open)
                return ExecResult(pid, Outcome.FILLED, [], order_ticket=res.order, position_ticket=p.ticket,
                                  request=base["request"], sizing=base["sizing"], volume=p.volume)
        return ExecResult(pid, Outcome.UNCERTAIN, ["broker said done but no position is visible yet; will reconcile"],
                          order_ticket=res.order, **base)

    def _mark_executed(self, plan: Plan, now: pd.Timestamp, note: str) -> None:
        if plan.is_live:
            self._plans.set_status(plan.plan_id, PlanStatus.EXECUTED, now, note)
