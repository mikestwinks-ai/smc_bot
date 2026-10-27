"""In-memory plan store (Phase 3). Persistence comes with the journal in Phase 6."""

from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from smc.models.plan import LIVE_STATUSES, Plan, PlanDirection, PlanStatus
from smc.utils.logger import get_logger

log = get_logger(__name__)


class PlanStore:
    def __init__(self, id_database_path: Optional[Path | str] = None) -> None:
        self._plans: dict[str, Plan] = {}
        self._counters: dict[str, int] = {}
        self._id_database_path = id_database_path
        self._counter_lock = threading.Lock()

    # --------------------------------------------------------------- ids
    def next_id(self, symbol: str) -> str:
        if self._id_database_path is not None:
            return self._next_persistent_id(symbol)
        n = self._counters.get(symbol, 0) + 1
        self._counters[symbol] = n
        return f"PLAN-{symbol}-{n:04d}"

    def _next_persistent_id(self, symbol: str) -> str:
        path = str(self._id_database_path)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._counter_lock:
            connection = sqlite3.connect(path, timeout=15.0)
            try:
                connection.execute("PRAGMA busy_timeout=15000")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS plan_id_counters (
                           symbol TEXT PRIMARY KEY,
                           last_sequence INTEGER NOT NULL
                       )"""
                )
                row = connection.execute(
                    "SELECT last_sequence FROM plan_id_counters WHERE symbol=?", (symbol,)
                ).fetchone()
                last_sequence = int(row[0]) if row is not None else 0

                if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='journal_events'"
                ).fetchone():
                    prefix = f"PLAN-{symbol}-"
                    escaped_prefix = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    persisted_ids = connection.execute(
                        "SELECT plan_id FROM journal_events WHERE plan_id LIKE ? ESCAPE '\\'",
                        (escaped_prefix + "%",),
                    )
                    matcher = re.compile(rf"^{re.escape(prefix)}(\d+)$")
                    for (plan_id,) in persisted_ids:
                        match = matcher.fullmatch(plan_id or "")
                        if match:
                            last_sequence = max(last_sequence, int(match.group(1)))

                next_sequence = last_sequence + 1
                connection.execute(
                    """INSERT INTO plan_id_counters(symbol, last_sequence) VALUES (?, ?)
                       ON CONFLICT(symbol) DO UPDATE SET last_sequence=excluded.last_sequence""",
                    (symbol, next_sequence),
                )
                connection.commit()
                return f"PLAN-{symbol}-{next_sequence:04d}"
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    # ------------------------------------------------------------- basics
    def add(self, plan: Plan) -> Plan:
        if plan.plan_id in self._plans:
            raise ValueError(f"Duplicate plan id {plan.plan_id}")
        self._plans[plan.plan_id] = plan
        return plan

    def get(self, plan_id: str) -> Plan:
        try:
            return self._plans[plan_id]
        except KeyError as exc:
            raise KeyError(f"Unknown plan {plan_id}") from exc

    def update(self, plan_id: str, now: pd.Timestamp, note: str = "", **fields: Any) -> Plan:
        """Set attributes on a live plan and stamp ``updated_at``."""
        plan = self.get(plan_id)
        if not plan.is_live:
            raise ValueError(f"{plan_id} is {plan.status.value}; only live plans can be updated")
        for name, value in fields.items():
            if not hasattr(plan, name):
                raise AttributeError(f"Plan has no field '{name}'")
            setattr(plan, name, value)
        plan.log(now, "updated", note or "updated " + ", ".join(fields))
        return plan

    def set_status(self, plan_id: str, status: PlanStatus, now: pd.Timestamp, note: str) -> Plan:
        plan = self.get(plan_id)
        plan.status = status
        plan.log(now, "status", note)
        return plan

    def cancel(self, plan_id: str, reason: str, now: pd.Timestamp) -> Plan:
        plan = self.get(plan_id)
        if not plan.is_live:
            raise ValueError(f"{plan_id} is already {plan.status.value}")
        plan.status = PlanStatus.CANCELED
        plan.cancel_reason = reason
        plan.log(now, "canceled", reason)
        return plan

    def flip(self, plan_id: str, reason: str, now: pd.Timestamp, new_plan: Optional[Plan] = None) -> Plan:
        """Mark a plan FLIPPED; optionally register the opposite child plan."""
        plan = self.get(plan_id)
        if not plan.is_live:
            raise ValueError(f"{plan_id} is already {plan.status.value}")
        plan.status = PlanStatus.FLIPPED
        plan.flip_reason = reason
        if new_plan is not None:
            new_plan.parent_plan_id = plan.plan_id
            self.add(new_plan)
            plan.child_plan_ids.append(new_plan.plan_id)
        plan.log(now, "flipped", reason)
        return plan

    # ------------------------------------------------------------ queries
    def all(self) -> list[Plan]:
        return list(self._plans.values())

    def list_active(self, symbol: Optional[str] = None) -> list[Plan]:
        """Plans that are PLANNED, ACTIVE or READY_TO_EXECUTE."""
        return [p for p in self._plans.values()
                if p.status in LIVE_STATUSES and (symbol is None or p.symbol == symbol)]

    def by_status(self, status: PlanStatus) -> list[Plan]:
        return [p for p in self._plans.values() if p.status is status]

    def find_by_zones(self, symbol: str, direction: PlanDirection, zone_ids: tuple[str, ...]) -> Optional[Plan]:
        """A live plan in the same direction that shares any zone (duplicate guard)."""
        wanted = set(zone_ids)
        for p in self.list_active(symbol):
            if p.direction is direction and wanted & set(p.zones.zone_ids):
                return p
        return None

    def find_overlapping(self, symbol: str, direction: PlanDirection, top: float, bottom: float,
                         tolerance: float = 0.0) -> Optional[Plan]:
        """A live same-direction plan whose entry zone overlaps [bottom, top] (same area = same idea)."""
        for p in self.list_active(symbol):
            z = p.entry_zone
            if p.direction is direction and bottom <= z.top + tolerance and top >= z.bottom - tolerance:
                return p
        return None

    def depending_on_zone(self, symbol: str, zone_id: str) -> list[Plan]:
        return [p for p in self.list_active(symbol) if zone_id in p.zones.zone_ids]
