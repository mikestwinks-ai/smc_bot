"""Wires the Phase 4 pieces to the existing project and runs one safe cycle."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from smc.config import BotConfig
from smc.core.exceptions import SMCBotError
from smc.core.mt5_connection import MT5Connection
from smc.execution.execution_engine import ExecResult, ExecutionEngine
from smc.execution.fills import FillRegistrar
from smc.execution.kill_switch import KillSwitch
from smc.execution.mt5_gateway import MT5Gateway
from smc.execution.state_store import StateStore
from smc.execution.trade_manager import Action, TradeManager
from smc.journal.monitoring import MonitoringService
from smc.journal.reconciliation import MT5Reconciler
from smc.journal.store import JournalStore
from smc.news.guard import NewsGuard
from smc.planning.context import StructureView
from smc.planning.plan_manager import PlanManager
from smc.risk.exposure import ExposureTracker
from smc.risk.risk_manager import RiskManager
from smc.risk.sessions import SessionClock
from smc.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class CycleReport:
    actions: list[Action] = field(default_factory=list)
    results: list[ExecResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class ExecutionSystem:
    def __init__(self, config: BotConfig, connection: MT5Connection, plan_manager: PlanManager,
                 now_fn: Optional[Callable[[], pd.Timestamp]] = None, state_path: Optional[Path] = None,
                 persist_state: bool = True, news_guard: Optional[NewsGuard] = None) -> None:
        self.config = config
        self.plans = plan_manager.store
        self.view = StructureView(plan_manager.hub, config)
        self.gateway = MT5Gateway(connection, config, now_fn)
        self.clock = SessionClock(config.sessions)
        self.state = StateStore((state_path or config.execution.state_path) if persist_state else None)
        self.journal = JournalStore(config.journal.database_path)
        self.gateway.journal = self.journal
        self.kill = KillSwitch(config.execution.kill_switch_path)
        self.exposure = ExposureTracker(self.gateway, self.state, self.clock)
        self.risk = RiskManager(config, self.exposure)
        self.registrar = FillRegistrar(config, self.gateway, self.state, self.plans, self.clock, self.journal)
        self.engine = ExecutionEngine(config, self.gateway, self.plans, self.risk, self.state, self.clock,
                                      self.kill, self.view, self.registrar, news_guard, self.journal)
        self.manager = TradeManager(config, self.gateway, self.plans, self.state, self.clock, self.kill,
                                    self.view, self.registrar, news_guard)
        self.news = news_guard
        self._last_reconcile_monotonic: Optional[float] = None
        self._last_reconcile_error: Optional[str] = None
        self.monitoring = MonitoringService(config, self.gateway, self.exposure, self.risk, self.kill,
                                            self.plans, self.journal, news_guard)
        self.journal.record_event_best_effort(
            "system_start", occurred_at=self.gateway.now(),
            idempotency_key=f"system-start:{self.gateway.now().isoformat()}",
            live_enabled=config.execution.live_enabled, magic_number=config.execution.magic_number,
        )
        if news_guard is None and config.news.enabled:
            log.warning("No NewsGuard attached to the execution system: news protection is NOT active.")
        log.info("Execution system ready. LIVE ORDERS %s. Strategy timezone: %s",
                 "ENABLED" if config.execution.live_enabled else "DISABLED (dry-run)", self.clock.timezone_name)

    def run_cycle(self) -> CycleReport:
        """Manage existing exposure first, then (unless killed) consider new entries."""
        rep = CycleReport()
        try:
            rep.actions = self.manager.run()
        except SMCBotError as exc:
            rep.errors.append(f"management failed: {exc}")
            log.error("Management cycle failed: %s", exc)
            self.journal.record_event_best_effort("system_error", occurred_at=self.gateway.now(),
                                                  component="trade_management", error=str(exc))
        for action in rep.actions:
            action_type = str(action.kind).lower()
            event_type = ("order_canceled" if "cancel" in action_type else
                          "position_modified" if any(word in action_type for word in ("stop", "partial", "close"))
                          else "management_action")
            self.journal.record_event_best_effort(
                event_type, occurred_at=self.gateway.now(),
                idempotency_key=f"management:{self.gateway.now().isoformat()}:{action.kind}:{action.target}:{action.note}",
                correlation_id=str(action.target), action=action.kind, target=action.target,
                succeeded=action.ok, note=action.note,
            )
        try:
            if self._reconciliation_due():
                self.reconcile_history()
                self._last_reconcile_error = None
        except (SMCBotError, sqlite3.Error, OSError) as exc:
            self._last_reconcile_error = f"{type(exc).__name__}: {exc}"
            log.error("Periodic MT5 reconciliation failed; risk checks remain mandatory: %s", self._last_reconcile_error)
            self.journal.record_event_best_effort("system_error", occurred_at=self.gateway.now(),
                                                  component="periodic_reconciliation",
                                                  error=self._last_reconcile_error)
        if self.kill.engaged:
            self.journal.record_event_best_effort(
                "kill_switch_active", occurred_at=self.gateway.now(),
                idempotency_key=f"kill-switch-active:{self.gateway.now().date()}:{self.kill.reason}",
                reason=self.kill.reason,
            )
            return rep
        if rep.errors:  # exposure could not be managed/read -> do not add new risk
            rep.errors.append("new entries skipped this cycle (failing closed)")
            return rep
        try:
            rep.results = self.engine.process_all()
        except SMCBotError as exc:
            rep.errors.append(f"execution failed: {exc}")
            log.error("Execution cycle failed: %s", exc)
            self.journal.record_event_best_effort("system_error", occurred_at=self.gateway.now(),
                                                  component="execution", error=str(exc))
        return rep

    def reconcile_history(self):
        """Read broker history/current exposure and durably reconcile bot-owned records."""
        symbols = {value for symbol in self.config.enabled_symbols for value in
                   (symbol.name, symbol.broker_symbol)}
        symbols.update(self.gateway.broker_symbol(symbol.name) for symbol in self.config.enabled_symbols)
        latest = self.journal.latest_reconciliation()
        start = None
        if latest is not None:
            # Overlap the prior successful interval to tolerate delayed/out-of-order broker history.
            start = pd.Timestamp(latest["history_end"]) - pd.Timedelta(days=1)
        result = MT5Reconciler(self.journal, self.gateway, symbols).reconcile(start=start)
        self._last_reconcile_monotonic = time.monotonic()
        return result

    def _reconciliation_due(self) -> bool:
        if self._last_reconcile_monotonic is None:
            return True
        return time.monotonic() - self._last_reconcile_monotonic >= \
            self.config.journal.reconciliation_interval_seconds

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self.journal.record_event_best_effort("system_shutdown", occurred_at=self.gateway.now())
        self.journal.close()
        self._closed = True
