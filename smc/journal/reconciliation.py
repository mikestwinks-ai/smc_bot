"""Fail-closed MT5 history reconciliation into the durable journal."""

from __future__ import annotations

import json
import hashlib
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd

from smc.journal.store import JournalStore, utc_iso
from smc.utils.logger import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class ReconciliationResult:
    account_id: int
    magic: int
    deals_seen: int
    orders_seen: int
    positions_seen: int
    trades_open: int
    trades_partial: int
    trades_closed: int
    completed_at: str


def _plain(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return utc_iso(value)
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if hasattr(value, "value"):
        return value.value
    return value


class MT5Reconciler:
    def __init__(self, journal: JournalStore, gateway: Any, configured_symbols: set[str]) -> None:
        self._journal, self._gateway = journal, gateway
        self._symbols = configured_symbols

    def reconcile(self, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None) -> ReconciliationResult:
        now = self._gateway.now()
        start = start or pd.Timestamp("2000-01-01", tz="UTC")
        end = end or (now + pd.Timedelta(seconds=1))
        started = utc_iso(now)
        account = self._gateway.account()
        magic = self._gateway.magic

        # Fetch every required broker surface before committing anything. A failed
        # response must never be turned into an empty history or a false closure.
        positions = self._gateway.positions()
        pending = self._gateway.pending_orders()
        deals = self._gateway.deals(start, end)
        orders = self._gateway.history_orders(start, end)

        owned_positions = [p for p in positions if p.magic == magic and p.symbol in self._symbols]
        owned_pending = [o for o in pending if o.magic == magic and o.symbol in self._symbols]
        owned_deals = [d for d in deals if d.is_trade and d.magic == magic and d.symbol in self._symbols]
        owned_orders = [o for o in orders if int(o.get("magic", 0)) == magic
                        and str(o.get("symbol", "")) in self._symbols]
        completed = utc_iso(self._gateway.now())
        run_id = str(uuid.uuid4())

        try:
            with self._journal._lock, self._journal._db:
                db = self._journal._db
                for order in owned_orders:
                    ticket = int(order["ticket"])
                    symbol = str(order.get("symbol", ""))
                    direction = str(order.get("direction", ""))
                    payload = json.dumps(order, default=_plain, sort_keys=True)
                    db.execute(
                        """INSERT INTO broker_orders
                           (account_id, magic, order_ticket, created_at, state, symbol, direction, volume, payload_json)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(account_id, magic, order_ticket) DO UPDATE SET
                           created_at=excluded.created_at, state=excluded.state, symbol=excluded.symbol,
                           direction=excluded.direction, volume=excluded.volume, payload_json=excluded.payload_json""",
                        (account.login, magic, ticket,
                         utc_iso(order["time_setup"]) if order.get("time_setup") is not None else None,
                         order.get("state"),
                         symbol, direction, order.get("volume_current", order.get("volume_initial")), payload),
                    )
                    state = order.get("state")
                    order_event_time = order.get("time_done") or order.get("time_setup") or completed
                    self._journal._insert_event(
                        "order_reconciled", order_event_time,
                        f"order:{account.login}:{order['ticket']}",
                        f"mt5-order:{account.login}:{magic}:{ticket}:{state}",
                        None, ticket, None, None, symbol, direction,
                        {"state": state, "volume": order.get("volume_current", order.get("volume_initial")),
                         "price": order.get("price_open"), "comment": order.get("comment", "")},
                    )
                for deal in owned_deals:
                    direction = ("buy" if deal.is_buy else "sell") if deal.is_buy is not None else None
                    plan_id = deal.comment.split("|", 1)[0] if deal.comment.startswith("PLAN-") else None
                    correlation_id = "|".join(deal.comment.split("|", 2)[:2]) if plan_id else \
                        f"position:{account.login}:{deal.position_id}"
                    payload = json.dumps(asdict(deal), default=_plain, sort_keys=True)
                    inserted = db.execute(
                        """INSERT OR IGNORE INTO broker_deals
                           (account_id, magic, deal_ticket, order_ticket, position_id, occurred_at, symbol,
                            direction, is_entry, is_exit, volume, price, commission, swap, fee, profit, net,
                            payload_json, is_reversal)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (account.login, magic, deal.ticket, deal.order, deal.position_id, utc_iso(deal.time),
                         deal.symbol, direction, int(deal.is_entry), int(deal.is_exit), deal.volume, deal.price,
                         deal.commission, deal.swap, deal.fee, deal.profit, deal.net, payload,
                         int(deal.is_reversal)),
                    ).rowcount
                    if inserted:
                        self._journal._insert_event(
                            "deal_reconciled", deal.time, correlation_id,
                            f"mt5-deal:{account.login}:{magic}:{deal.ticket}", plan_id, deal.order, deal.ticket,
                            deal.position_id, deal.symbol, direction, {"correlation_id": correlation_id,
                                                                       "volume": deal.volume, "price": deal.price,
                                                                       "commission": deal.commission, "swap": deal.swap,
                                                                       "fee": deal.fee, "profit": deal.profit,
                                                                       "net": deal.net})

                db.execute("DELETE FROM current_positions WHERE account_id=? AND magic=?", (account.login, magic))
                for position in owned_positions:
                    snapshot = json.dumps(asdict(position), default=_plain, sort_keys=True)
                    change = {"ticket": position.ticket, "identifier": position.identifier,
                              "symbol": position.symbol, "direction": "buy" if position.is_buy else "sell",
                              "volume": position.volume, "open_price": position.price_open, "sl": position.sl,
                              "tp": position.tp}
                    digest = hashlib.sha256(json.dumps(change, sort_keys=True).encode("utf-8")).hexdigest()
                    self._journal._insert_event(
                        "position_snapshot", position.time, f"position:{account.login}:{position.identifier}",
                        f"position-state:{account.login}:{magic}:{position.identifier}:{digest}",
                        None, None, None, position.identifier, position.symbol, change["direction"], change,
                    )
                    db.execute(
                        "INSERT INTO current_positions VALUES (?, ?, ?, ?, ?)",
                        (account.login, magic, position.ticket, snapshot, completed),
                    )
                db.execute("DELETE FROM current_orders WHERE account_id=? AND magic=?", (account.login, magic))
                for order in owned_pending:
                    snapshot = json.dumps(asdict(order), default=_plain, sort_keys=True)
                    db.execute(
                        "INSERT INTO current_orders VALUES (?, ?, ?, ?, ?)",
                        (account.login, magic, order.ticket, snapshot, completed),
                    )

                by_position = db.execute(
                    """SELECT position_id, symbol, MIN(CASE WHEN is_entry=1 THEN occurred_at END) AS opened_at,
                              MAX(CASE WHEN is_exit=1 THEN occurred_at END) AS closed_at,
                              SUM(CASE WHEN is_entry=1 THEN volume ELSE 0 END) AS volume_open,
                              SUM(CASE WHEN is_exit=1 THEN volume ELSE 0 END) AS volume_closed,
                              SUM(net) AS net, SUM(commission+swap+fee) AS costs,
                              SUM(CASE WHEN is_entry=1 THEN 1 ELSE 0 END) AS entries,
                              SUM(is_reversal) AS reversals
                       FROM broker_deals WHERE account_id=? AND magic=? GROUP BY position_id""",
                    (account.login, magic),
                ).fetchall()
                open_tickets = {p.identifier or p.ticket for p in owned_positions}
                counts = {"open": 0, "partial": 0, "closed": 0, "unknown": 0}
                for row in by_position:
                    pid = int(row["position_id"])
                    opened, closed = float(row["volume_open"] or 0), float(row["volume_closed"] or 0)
                    if pid in open_tickets:
                        status = "open"
                    elif row["reversals"]:
                        status = "unknown"
                    elif opened > 0 and closed >= opened - 1e-9:
                        status = "closed"
                    elif closed > 0:
                        status = "partially_closed"
                    else:
                        status = "unknown"
                    counts["partial" if status == "partially_closed" else status] += 1
                    direction_row = db.execute(
                        "SELECT direction FROM broker_deals WHERE account_id=? AND magic=? AND position_id=? "
                        "AND is_entry=1 ORDER BY occurred_at, deal_ticket LIMIT 1",
                        (account.login, magic, pid),
                    ).fetchone()
                    db.execute(
                        """INSERT INTO bot_trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(account_id, magic, position_id) DO UPDATE SET
                           symbol=excluded.symbol, direction=excluded.direction, status=excluded.status,
                           opened_at=excluded.opened_at, closed_at=excluded.closed_at,
                           volume_open=excluded.volume_open, volume_closed=excluded.volume_closed,
                           net=excluded.net, costs=excluded.costs, updated_at=excluded.updated_at""",
                        (account.login, magic, pid, row["symbol"], direction_row["direction"] if direction_row else None,
                         status, row["opened_at"], row["closed_at"] if status == "closed" else None,
                         opened, closed, float(row["net"] or 0), float(row["costs"] or 0), completed),
                    )
                db.execute(
                    """INSERT INTO reconciliation_runs
                       (run_id, account_id, magic, started_at, completed_at, history_start, history_end,
                        deals_seen, orders_seen, positions_seen, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'complete')""",
                    (run_id, account.login, magic, started, completed, utc_iso(start), utc_iso(end),
                     len(owned_deals), len(owned_orders), len(owned_positions)),
                )
                self._journal._insert_event(
                    "reconciliation_complete", completed, f"account:{account.login}:magic:{magic}",
                    f"reconciliation:{run_id}", None, None, None, None, None, None,
                    {"deals": len(owned_deals), "orders": len(owned_orders),
                     "positions": len(owned_positions), "trades": counts})
        except sqlite3.Error:
            log.exception("MT5 reconciliation database transaction failed; no successful run was recorded")
            raise

        log.info("MT5 reconciliation complete: account=%s magic=%s deals=%d orders=%d positions=%d",
                 account.login, magic, len(owned_deals), len(owned_orders), len(owned_positions))
        return ReconciliationResult(account.login, magic, len(owned_deals), len(owned_orders),
                                    len(owned_positions), counts["open"], counts["partial"], counts["closed"], completed)
