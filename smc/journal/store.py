"""Versioned SQLite event journal and reconciled broker records."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from smc.utils.logger import get_logger

log = get_logger(__name__)
_EVENT_NAMESPACE = uuid.UUID("95143966-ae75-4dc4-a6a3-97298492b6d5")


def utc_iso(value: Any = None) -> str:
    if value is None:
        stamp = pd.Timestamp.now(tz="UTC")
    else:
        stamp = pd.Timestamp(value)
        stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    return stamp.isoformat()


def _json_default(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp, datetime)):
        return utc_iso(value)
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "__dataclass_fields__"):
        return {name: getattr(value, name) for name in value.__dataclass_fields__}
    raise TypeError(f"Cannot serialize {type(value).__name__} into journal JSON")


class JournalStore:
    """Durable event and broker-history store; migrations never discard user data."""

    SCHEMA_VERSION = 4

    def __init__(self, path: Path | str) -> None:
        raw_path = str(path)
        self.path: Path | str = raw_path if raw_path == ":memory:" else Path(raw_path)
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), timeout=15.0, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._migrate()

    def _migrate(self) -> None:
        version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
        if version > self.SCHEMA_VERSION:
            raise RuntimeError(f"Journal schema {version} is newer than supported {self.SCHEMA_VERSION}")
        with self._db:
            if version < 1:
                self._db.execute(
                    """CREATE TABLE journal_events (
                        event_id TEXT PRIMARY KEY,
                        occurred_at TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        correlation_id TEXT,
                        idempotency_key TEXT UNIQUE,
                        plan_id TEXT,
                        order_ticket INTEGER,
                        deal_ticket INTEGER,
                        position_id INTEGER,
                        symbol TEXT,
                        direction TEXT,
                        payload_json TEXT NOT NULL
                    )"""
                )
                self._db.execute("CREATE INDEX journal_events_time ON journal_events(occurred_at)")
                self._db.execute("CREATE INDEX journal_events_type ON journal_events(event_type, occurred_at)")
                self._db.execute("PRAGMA user_version=1")
                version = 1
            if version < 2:
                self._db.execute(
                    """CREATE TABLE broker_orders (
                        account_id INTEGER NOT NULL, magic INTEGER NOT NULL, order_ticket INTEGER NOT NULL,
                        created_at TEXT, state TEXT, symbol TEXT, direction TEXT, volume REAL,
                        payload_json TEXT NOT NULL, PRIMARY KEY(account_id, magic, order_ticket)
                    )"""
                )
                self._db.execute(
                    """CREATE TABLE broker_deals (
                        account_id INTEGER NOT NULL, magic INTEGER NOT NULL, deal_ticket INTEGER NOT NULL,
                        order_ticket INTEGER, position_id INTEGER NOT NULL, occurred_at TEXT NOT NULL,
                        symbol TEXT NOT NULL, direction TEXT, is_entry INTEGER NOT NULL, is_exit INTEGER NOT NULL,
                        volume REAL NOT NULL, price REAL NOT NULL, commission REAL NOT NULL, swap REAL NOT NULL,
                        fee REAL NOT NULL, profit REAL NOT NULL, net REAL NOT NULL, payload_json TEXT NOT NULL,
                        PRIMARY KEY(account_id, magic, deal_ticket)
                    )"""
                )
                self._db.execute(
                    """CREATE TABLE bot_trades (
                        account_id INTEGER NOT NULL, magic INTEGER NOT NULL, position_id INTEGER NOT NULL,
                        symbol TEXT NOT NULL, direction TEXT, status TEXT NOT NULL, opened_at TEXT,
                        closed_at TEXT, volume_open REAL NOT NULL, volume_closed REAL NOT NULL,
                        net REAL NOT NULL, costs REAL NOT NULL, updated_at TEXT NOT NULL,
                        PRIMARY KEY(account_id, magic, position_id)
                    )"""
                )
                self._db.execute(
                    """CREATE TABLE current_positions (
                        account_id INTEGER NOT NULL, magic INTEGER NOT NULL, ticket INTEGER NOT NULL,
                        snapshot_json TEXT NOT NULL, updated_at TEXT NOT NULL,
                        PRIMARY KEY(account_id, magic, ticket)
                    )"""
                )
                self._db.execute(
                    """CREATE TABLE current_orders (
                        account_id INTEGER NOT NULL, magic INTEGER NOT NULL, ticket INTEGER NOT NULL,
                        snapshot_json TEXT NOT NULL, updated_at TEXT NOT NULL,
                        PRIMARY KEY(account_id, magic, ticket)
                    )"""
                )
                self._db.execute(
                    """CREATE TABLE reconciliation_runs (
                        run_id TEXT PRIMARY KEY, account_id INTEGER NOT NULL, magic INTEGER NOT NULL,
                        started_at TEXT NOT NULL, completed_at TEXT NOT NULL, history_start TEXT NOT NULL,
                        history_end TEXT NOT NULL, deals_seen INTEGER NOT NULL, orders_seen INTEGER NOT NULL,
                        positions_seen INTEGER NOT NULL, status TEXT NOT NULL
                    )"""
                )
                self._db.execute("CREATE INDEX broker_deals_position ON broker_deals(account_id, magic, position_id)")
                self._db.execute("PRAGMA user_version=2")
                version = 2
            if version < 3:
                self._db.execute("ALTER TABLE broker_deals ADD COLUMN is_reversal INTEGER NOT NULL DEFAULT 0")
                self._db.execute("PRAGMA user_version=3")
                version = 3
            if version < 4:
                self._db.execute(
                    """CREATE TABLE IF NOT EXISTS plan_id_counters (
                           symbol TEXT PRIMARY KEY,
                           last_sequence INTEGER NOT NULL
                       )"""
                )
                self._db.execute(
                    "CREATE INDEX IF NOT EXISTS journal_events_plan_id ON journal_events(plan_id)"
                )
                self._db.execute("PRAGMA user_version=4")

    def _insert_event(self, event_type: str, occurred_at: Any, correlation_id: Optional[str],
                      idempotency_key: Optional[str], plan_id: Optional[str], order_ticket: Optional[int],
                      deal_ticket: Optional[int], position_id: Optional[int], symbol: Optional[str],
                      direction: Optional[str], payload: dict[str, Any]) -> str:
        event_id = str(uuid.uuid5(_EVENT_NAMESPACE, idempotency_key)) if idempotency_key else str(uuid.uuid4())
        self._db.execute(
            """INSERT OR IGNORE INTO journal_events
               (event_id, occurred_at, event_type, correlation_id, idempotency_key, plan_id, order_ticket,
                deal_ticket, position_id, symbol, direction, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, utc_iso(occurred_at), event_type, correlation_id, idempotency_key, plan_id, order_ticket,
             deal_ticket, position_id, symbol, direction, json.dumps(payload, default=_json_default, sort_keys=True)),
        )
        return event_id

    def record_event(self, event_type: str, *, occurred_at: Any = None, correlation_id: Optional[str] = None,
                     idempotency_key: Optional[str] = None, plan_id: Optional[str] = None,
                     order_ticket: Optional[int] = None, deal_ticket: Optional[int] = None,
                     position_id: Optional[int] = None, symbol: Optional[str] = None,
                     direction: Optional[str] = None, **payload: Any) -> str:
        with self._lock, self._db:
            return self._insert_event(event_type, occurred_at, correlation_id, idempotency_key, plan_id,
                                      order_ticket, deal_ticket, position_id, symbol, direction, payload)

    def record_event_best_effort(self, event_type: str, **fields: Any) -> Optional[str]:
        """Keep journal outages observable without letting them bypass trade safety logic."""
        try:
            return self.record_event(event_type, **fields)
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            log.error("Journal write failed for %s (%s: %s); trading safeguards remain active",
                      event_type, type(exc).__name__, exc)
            return None

    def record_structure_events(self, events: list[dict[str, Any]]) -> None:
        """Persist bounded, structured events emitted by the structure/plan update pipeline."""
        for event in events:
            details = event.get("details")
            if not isinstance(details, dict):
                details = {}
            event_key = str(event["event_key"])
            self.record_event_best_effort(
                str(event["event_type"]),
                occurred_at=event["occurred_at_utc"],
                idempotency_key=f"structure:{event_key}",
                correlation_id=str(event["entity_id"]),
                symbol=str(event["symbol"]),
                timeframe=str(event["timeframe"]),
                status=str(event["status"]),
                note=str(event["note"]),
                event_key=event_key,
                **details,
            )

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM journal_events ORDER BY occurred_at DESC, rowid DESC LIMIT ?", (max(0, limit),)
            ).fetchall()
        return [self._event_dict(row) for row in rows]

    def recent_events_by_prefix(self, prefixes: tuple[str, ...], limit: int = 25) -> list[dict[str, Any]]:
        if not prefixes:
            return []
        predicates = " OR ".join("event_type LIKE ?" for _ in prefixes)
        params = tuple(f"{prefix}%" for prefix in prefixes) + (max(0, limit),)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM journal_events WHERE {predicates} ORDER BY occurred_at DESC, rowid DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._event_dict(row) for row in rows]

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        raw_payload = result.pop("payload_json")
        try:
            payload = json.loads(raw_payload)
        except (TypeError, ValueError):
            payload = raw_payload
        result["payload"] = payload if isinstance(payload, dict) else {"note": payload}
        return result

    def events(self, event_types: Optional[tuple[str, ...]] = None) -> list[dict[str, Any]]:
        with self._lock:
            if event_types:
                marks = ",".join("?" for _ in event_types)
                rows = self._db.execute(
                    f"SELECT * FROM journal_events WHERE event_type IN ({marks}) ORDER BY occurred_at, rowid",
                    event_types,
                ).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM journal_events ORDER BY occurred_at, rowid").fetchall()
        return [self._event_dict(row) for row in rows]

    def trades(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM bot_trades ORDER BY opened_at, position_id").fetchall()
        return [dict(row) for row in rows]

    def broker_orders(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM broker_orders ORDER BY created_at, order_ticket").fetchall()
        return [dict(row) for row in rows]

    def broker_deals(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM broker_deals ORDER BY occurred_at, deal_ticket").fetchall()
        return [dict(row) for row in rows]

    def reconciliation_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM reconciliation_runs ORDER BY completed_at DESC LIMIT ?", (max(0, limit),)
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_reconciliation(self) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM reconciliation_runs ORDER BY completed_at DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row is not None else None

    def status_summary(self, account_id: int, magic: int) -> dict[str, Any]:
        with self._lock:
            counts = {}
            for table in ("journal_events", "broker_orders", "broker_deals", "bot_trades"):
                row = self._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                counts[table] = int(row[0])
            return {
                "database_path": str(self.path),
                "schema_version": int(self._db.execute("PRAGMA user_version").fetchone()[0]),
                "counts": counts,
                "current_positions": len(self.current_positions(account_id, magic)),
                "current_pending_orders": len(self.current_orders(account_id, magic)),
                "last_reconciliation": self.latest_reconciliation(),
                "recent_executions": self.recent_events_by_prefix(
                    ("order_", "execution_", "deal_", "position_opened"), 25),
                "recent_errors_and_recovery": self.recent_events_by_prefix(
                    ("system_error", "mt5_disconnect", "mt5_reconnected"), 25),
            }

    def current_positions(self, account_id: int, magic: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT snapshot_json FROM current_positions WHERE account_id=? AND magic=? ORDER BY ticket",
                (account_id, magic),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def current_orders(self, account_id: int, magic: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT snapshot_json FROM current_orders WHERE account_id=? AND magic=? ORDER BY ticket",
                (account_id, magic),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def all_current_orders(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT account_id, magic, ticket, snapshot_json, updated_at FROM current_orders ORDER BY updated_at, ticket"
            ).fetchall()
        return [{**dict(row), "snapshot": json.loads(row["snapshot_json"])} for row in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()
