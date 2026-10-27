"""Server-side, read-only dashboard access to the bot snapshot and SQLite journal."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from smc.dashboard.paths import DEFAULT_SNAPSHOT_PATH
from smc.utils.logger import get_logger

log = get_logger(__name__)
MAX_ROWS = 250
DEFAULT_ROWS = 100
SNAPSHOT_STALE_AFTER_SECONDS = 15
SNAPSHOT_READ_ATTEMPTS = 3
SNAPSHOT_READ_RETRY_SECONDS = 0.05


def load_snapshot(path: Path = DEFAULT_SNAPSHOT_PATH, now: Optional[datetime] = None) -> dict[str, Any]:
    """Load an atomic bot snapshot and explicitly classify missing/stale data."""
    snapshot_path = Path(path)
    last_error: Optional[OSError | UnicodeError | json.JSONDecodeError] = None
    for attempt in range(SNAPSHOT_READ_ATTEMPTS):
        try:
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            break
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt + 1 < SNAPSHOT_READ_ATTEMPTS:
                time.sleep(SNAPSHOT_READ_RETRY_SECONDS)
    else:
        if isinstance(last_error, FileNotFoundError):
            return {"available": False, "status": "unavailable",
                    "reason": "Bot has not published a snapshot yet."}
        log.warning(
            "Dashboard snapshot read failed after %d attempts (%s): %s",
            SNAPSHOT_READ_ATTEMPTS,
            type(last_error).__name__ if last_error else "UnknownError",
            last_error,
        )
        return {
            "available": False,
            "status": "error",
            "reason": f"Snapshot could not be read after {SNAPSHOT_READ_ATTEMPTS} attempts "
                      f"({type(last_error).__name__ if last_error else 'UnknownError'}).",
        }
    if not isinstance(snapshot, dict):
        return {"available": False, "status": "error", "reason": "Snapshot root must be a JSON object."}
    try:
        stamp = datetime.fromisoformat(snapshot["generated_at_utc"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        age = max(0.0, (current - stamp).total_seconds())
    except (KeyError, TypeError, ValueError):
        return {"available": False, "status": "error", "reason": "Snapshot timestamp is missing or invalid."}
    snapshot["available"] = True
    snapshot["snapshot_age_seconds"] = age
    snapshot["snapshot_status"] = (
        "stale" if age > SNAPSHOT_STALE_AFTER_SECONDS else "fresh"
    )
    return snapshot


class ReadOnlyJournal:
    """Bounded account/magic-scoped reads; the database is opened in SQLite read-only mode."""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)

    def read(self, magic: int, as_of_utc: str, limit: int = DEFAULT_ROWS) -> dict[str, Any]:
        limit = min(max(int(limit), 1), MAX_ROWS)
        if not self.database_path.is_file():
            return {"available": False, "reason": "Journal database is not available.", "scope": None}
        connection: Optional[sqlite3.Connection] = None
        try:
            uri = self.database_path.resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=0.5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            run = connection.execute(
                """SELECT account_id, magic, completed_at FROM reconciliation_runs
                   WHERE magic=? AND completed_at<=?
                   ORDER BY completed_at DESC LIMIT 1""",
                (int(magic), str(as_of_utc)),
            ).fetchone()
            if run is None:
                return {"available": False, "reason": "No completed reconciliation scope exists yet.", "scope": None}
            account_id = int(run["account_id"])
            scoped_magic = int(run["magic"])
            trades = connection.execute(
                """SELECT position_id, symbol, direction, status, opened_at, closed_at,
                          volume_open, volume_closed, net, costs, updated_at
                   FROM bot_trades WHERE account_id=? AND magic=?
                   ORDER BY COALESCE(closed_at, opened_at, updated_at) DESC LIMIT ?""",
                (account_id, scoped_magic, limit),
            ).fetchall()
            orders = connection.execute(
                """SELECT order_ticket, created_at, state, symbol, direction, volume
                   FROM broker_orders WHERE account_id=? AND magic=?
                   ORDER BY created_at DESC, order_ticket DESC LIMIT ?""",
                (account_id, scoped_magic, limit),
            ).fetchall()
            deals = connection.execute(
                """SELECT deal_ticket, order_ticket, position_id, occurred_at, symbol, direction,
                          is_entry, is_exit, volume, price, commission, swap, fee, profit, net
                   FROM broker_deals WHERE account_id=? AND magic=?
                   ORDER BY occurred_at DESC, deal_ticket DESC LIMIT ?""",
                (account_id, scoped_magic, limit),
            ).fetchall()
            reconciliations = connection.execute(
                """SELECT magic, completed_at, history_start, history_end, deals_seen, orders_seen,
                          positions_seen, status
                   FROM reconciliation_runs WHERE account_id=? AND magic=?
                   ORDER BY completed_at DESC LIMIT ?""",
                (account_id, scoped_magic, min(limit, 50)),
            ).fetchall()
            events = connection.execute(
                """SELECT event_id, occurred_at, event_type, correlation_id, plan_id, order_ticket,
                          deal_ticket, position_id, symbol, direction
                   FROM journal_events
                   WHERE order_ticket IN (
                       SELECT order_ticket FROM broker_orders WHERE account_id=? AND magic=?
                   ) OR deal_ticket IN (
                       SELECT deal_ticket FROM broker_deals WHERE account_id=? AND magic=?
                   ) OR position_id IN (
                       SELECT position_id FROM bot_trades WHERE account_id=? AND magic=?
                   )
                   ORDER BY occurred_at DESC LIMIT ?""",
                (account_id, scoped_magic, account_id, scoped_magic, account_id, scoped_magic, limit),
            ).fetchall()
            return {
                "available": True,
                "scope": {"login_masked": "****" + str(account_id)[-2:], "magic": scoped_magic,
                          "reconciliation_as_of_utc": run["completed_at"]},
                "trades": [dict(row) for row in trades],
                "orders": [dict(row) for row in orders],
                "deals": [dict(row) for row in deals],
                "reconciliations": [dict(row) for row in reconciliations],
                "related_events": [dict(row) for row in events],
                "row_limit": limit,
            }
        except (sqlite3.Error, OSError, ValueError) as exc:
            log.warning("Read-only dashboard journal query failed (%s)", type(exc).__name__)
            return {"available": False, "reason": f"Journal read failed ({type(exc).__name__}).", "scope": None}
        finally:
            if connection is not None:
                connection.close()
