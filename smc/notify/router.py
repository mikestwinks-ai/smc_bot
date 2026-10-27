"""NotificationRouter: turns plan changes, execution results and management actions into alerts.

Read-only with respect to trading: it looks at the results the pipeline already produced and
sends text. Every alert has a de-duplication key (persisted), so repeated cycles, restarts or
re-validations never spam, and a Telegram failure can never influence an order.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Optional

import pandas as pd

from smc.config import BotConfig
from smc.execution.execution_engine import Outcome
from smc.models.plan import Plan, PlanStatus
from smc.notify import messages as msg
from smc.notify.telegram import Notifier
from smc.planning.plan_manager import ChangeKind, PlanChange
from smc.utils.logger import get_logger

if TYPE_CHECKING:
    from smc.execution.system import CycleReport, ExecutionSystem

log = get_logger(__name__)

LIMIT_PATTERNS = (("daily drawdown limit", "daily_drawdown"), ("consecutive losses", "consecutive_losses"),
                  ("trades already opened", "max_trades_day"), ("open positions/pending orders", "max_open"),
                  ("already exposed", "direction_exposure"))


def _h(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:8]


class NotificationRouter:
    def __init__(self, notifier: Notifier, config: BotConfig, system: "ExecutionSystem") -> None:
        self._n, self._cfg, self._sys = notifier, config, system
        self._kill_engaged = False
        self._kill_count = 0

    # -------------------------------------------------------------------- plans
    def _plan(self, plan_id: str) -> Optional[Plan]:
        try:
            return self._sys.plans.get(plan_id)
        except KeyError:
            return None

    def _pkey(self, p: Plan, what: str) -> str:
        return f"plan:{p.plan_id}:{p.created_at:%Y%m%dT%H%M}:{what}"

    def plan_changes(self, changes: list[PlanChange]) -> None:
        for ch in changes:
            p = self._plan(ch.plan_id)
            if p is None:
                continue
            if ch.kind is ChangeKind.CREATED:
                self._n.notify(msg.plan_event("created", p), key=self._pkey(p, "created"))
            elif ch.kind is ChangeKind.UPGRADED:
                self._n.notify(msg.plan_event("upgraded", p), key=self._pkey(p, f"upgraded:{p.entry_model.value}"))
            elif ch.kind is ChangeKind.CANCELED:
                self._n.notify(msg.plan_event("canceled", p, p.cancel_reason or ""), key=self._pkey(p, "canceled"))
            elif ch.kind is ChangeKind.FLIPPED:
                child = f"\nOpposite retest plan: {', '.join(p.child_plan_ids)}" if p.child_plan_ids else ""
                self._n.notify(msg.plan_event("flipped", p, (p.flip_reason or "") + child), key=self._pkey(p, "flipped"))
            elif ch.kind is ChangeKind.STATUS and p.status is PlanStatus.READY_TO_EXECUTE:
                self._n.notify(msg.plan_event("ready", p, ch.note), key=self._pkey(p, "ready"))

    # -------------------------------------------------------------------- cycle
    def cycle(self, report: "CycleReport", now: Optional[pd.Timestamp] = None) -> None:
        now = now or self._sys.gateway.now()
        self._actions(report)
        self._results(report, now)
        self._kill(now)
        self._cutoffs(now)
        for err in report.errors:
            self._n.notify(f"SYSTEM ERROR\n{err}"[:600], key=f"err:{_h(err)}:{now:%Y%m%d%H}", persist=False)

    def _actions(self, report: "CycleReport") -> None:
        for a in report.actions:
            key = f"act:{a.kind}:{a.target}:{_h(a.note)}"
            if a.kind == "register" and a.ok:
                self._fill_alert(self._sys.state.get_managed(int(a.target)))
            elif a.kind == "partial" and a.ok:
                self._n.notify(f"PARTIAL CLOSE CONFIRMED\nticket {a.target}: {a.note}", key=key)
            elif a.kind == "partial" and "impossible" in a.note:
                self._n.notify(f"PARTIAL CLOSE SKIPPED\nticket {a.target}: {a.note}", key=key)
            elif a.kind == "breakeven" and a.ok:
                self._n.notify(f"BREAK-EVEN CONFIRMED\nticket {a.target}: {a.note}", key=key)
            elif a.kind == "trail" and a.ok:
                self._n.notify(f"STRUCTURAL STOP UPDATE\nticket {a.target}: {a.note}", key=key)
            elif a.kind == "flatten":
                self._n.notify(f"{'POSITION CLOSED' if a.ok else 'CLOSE FAILED'} (cutoff / news policy)\n"
                               f"ticket {a.target}: {a.note}", key=key)
            elif a.kind == "cancel" and a.ok:
                self._n.notify(f"PENDING ORDER CANCELED\norder {a.target}: {a.note}", key=key)

    def _fill_alert(self, mp) -> None:
        """One 'confirmed by MT5' alert per position, whichever path discovered the fill."""
        if mp is None:
            return
        p = self._plan(mp.plan_id)
        self._n.notify(f"TRADE CONFIRMED BY MT5\n{mp.symbol} {mp.setup_tf} {'BUY' if mp.is_buy else 'SELL'} "
                       f"{mp.initial_volume:g} lots @ {mp.entry:g} | SL {mp.initial_sl:g} | TP {mp.tp:g}\n"
                       f"{mp.plan_id} {p.entry_model.value if p else ''} ticket {mp.ticket}"
                       f"{' (adopted after restart)' if mp.adopted else ''}", key=f"fill:{mp.key}:{mp.ticket}")

    def _results(self, report: "CycleReport", now: pd.Timestamp) -> None:
        day = self._sys.clock.trading_day_key(now)
        for r in report.results:
            p = self._plan(r.plan_id)
            tag = f"{r.plan_id}:{p.created_at:%Y%m%dT%H%M}" if p else r.plan_id
            text = "; ".join(r.reasons)
            if r.outcome is Outcome.FILLED and p:
                self._fill_alert(self._sys.state.get_managed(r.position_ticket))
            elif r.outcome is Outcome.PENDING_PLACED and p:
                self._n.notify(f"PENDING ORDER PLACED\n{msg.plan_line(p)}\n{r.volume:g} lots, order {r.order_ticket}",
                               key=f"placed:{tag}")
            elif r.outcome in (Outcome.UNCERTAIN, Outcome.AWAITING):
                self._n.notify(f"ORDER OUTCOME UNCERTAIN\n{r.plan_id}: {text}\nReconciling with MT5 - no blind retry.",
                               key=f"uncertain:{tag}")
            elif r.outcome is Outcome.FAILED:
                self._n.notify(f"ORDER FAILED\n{r.plan_id}: {text}", key=f"failed:{tag}:{_h(text)}")
            elif r.outcome is Outcome.BLOCKED and "kill switch" not in text:
                self._n.notify(f"EXECUTION BLOCKED\n{r.plan_id}: {text}", key=f"blocked:{_h(text)}:{day}")
            elif r.outcome is Outcome.REJECTED:
                for needle, code in LIMIT_PATTERNS:
                    if needle in text:
                        self._n.notify(f"RISK LIMIT ACTIVE: {code.replace('_', ' ')}\n{text}"[:600], key=f"limit:{code}:{day}")
                if "failing closed" in text:
                    self._n.notify(f"EXECUTION REJECTED (data unavailable)\n{r.plan_id}: {text}"[:600],
                                   key=f"fc:{_h(text)}:{now:%Y%m%d%H}", persist=False)

    def _kill(self, now: pd.Timestamp) -> None:
        engaged = self._sys.kill.engaged
        if engaged and not self._kill_engaged:
            self._kill_count += 1
            self._n.notify(f"EMERGENCY KILL SWITCH ENGAGED\n{self._sys.kill.reason}\nNo new orders. Open positions are still managed; "
                           f"pending orders are cancelled.", key=f"kill:{now:%Y%m%d%H%M}:{self._kill_count}")
        elif not engaged and self._kill_engaged:
            self._n.notify("Kill switch released.", key=f"kill_off:{now:%Y%m%d%H%M}:{self._kill_count}", persist=False)
        self._kill_engaged = engaged

    def _cutoffs(self, now: pd.Timestamp) -> None:
        s = self._cfg.sessions
        local = self._sys.clock.local(now).time()
        day = self._sys.clock.trading_day_key(now)
        tz = self._sys.clock.timezone_name
        if local >= s.close_all_risk_by:
            self._n.notify(f"DAILY CUTOFF {s.close_all_risk_by:%H:%M} {tz}: bot-managed positions are closed and pending "
                           f"orders cancelled (crypto exempt only if configured).", key=f"cutoff_flat:{day}")
        elif local >= s.no_new_trades_after:
            self._n.notify(f"DAILY CUTOFF {s.no_new_trades_after:%H:%M} {tz}: no new trades until the next session.",
                           key=f"cutoff_entries:{day}")
