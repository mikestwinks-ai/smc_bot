"""Phase 7 end-to-end safety and recovery checks; all broker activity is simulated."""

from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from smc.core.exceptions import MT5DataError
from smc.execution import ExecutionSystem
from smc.execution.execution_engine import Outcome
from smc.execution.fills import exec_key
from smc.execution.state_store import UNCERTAIN
from smc.journal.export import export_excel
from smc.journal.store import JournalStore
from smc.models.plan import PlanStatus
from tests.harness import add_plan, make_env
from tests.news_helpers import ev, news_env, ts


def test_end_to_end_fake_broker_trade_management_reconciliation_and_status(tmp_path):
    env = make_env(tmp_path, live=True)
    env.broker.set_price(115.0, 115.05)
    plan = add_plan(env, status=PlanStatus.READY_TO_EXECUTE)
    result = env.system.engine.process_plan(plan)
    assert result.outcome is Outcome.FILLED
    position = env.broker.positions[0]
    assert position.magic == env.cfg.execution.magic_number

    # Simulate a protective management event and a later partial close in the fake terminal.
    env.broker.set_price(120.0, 120.05)
    actions = env.system.manager.run()
    assert any(action.kind == "partial" and action.ok for action in actions)
    assert position.volume < result.volume

    reconcile = env.system.reconcile_history()
    trade = next(row for row in env.system.journal.trades() if row["position_id"] == position.ticket)
    assert reconcile.positions_seen == 1
    assert trade["status"] == "open" and trade["volume_closed"] > 0
    status = env.system.monitoring.snapshot(env.now)
    assert status["execution"]["live_enabled"] is True
    assert status["journal"]["last_reconciliation"]["status"] == "complete"
    assert status["journal"]["recent_executions"]
    assert status["positions"][0]["ticket"] == position.ticket
    env.system.close()


def test_disconnect_recovery_and_unknown_send_remain_idempotent_after_restart(tmp_path):
    env = make_env(tmp_path, live=True)
    env.broker.set_price(118.0, 118.05)
    plan = add_plan(env)
    env.broker.behaviors.append("timeout_not_executed")
    result = env.system.engine.process_plan(plan)
    assert result.outcome is Outcome.UNCERTAIN
    key = exec_key(plan.plan_id, plan.created_at)
    assert env.system.state.get_intent(key)["status"] == UNCERTAIN
    sends = len(env.broker.sent)

    # Fresh process state over persistent JSON/SQLite paths, preserving the same plan.
    db_path = tmp_path / "restart-journal.sqlite3"
    env.cfg.journal.database_path = db_path
    env.system.close()
    reborn = ExecutionSystem(env.cfg, env.conn, env.mgr, now_fn=lambda: env.broker.now,
                             state_path=env.cfg.execution.state_path)
    env.broker.shutdown()
    again = reborn.engine.process_plan(plan)
    assert again.outcome is Outcome.AWAITING
    assert len(env.broker.sent) == sends
    assert env.conn.is_alive()
    assert any(event["event_type"] == "mt5_reconnected" for event in reborn.journal.events())
    reborn.close()


def test_mt5_disconnect_during_analysis_and_management_recovers_without_order_duplication(tmp_path):
    env = make_env(tmp_path, live=True)
    plan = add_plan(env, status=PlanStatus.READY_TO_EXECUTE)
    env.broker.set_price(115.0, 115.05)
    env.broker.shutdown()
    result = env.system.engine.process_plan(plan)
    assert result.outcome is Outcome.FILLED
    sent_count = len(env.broker.sent)
    env.broker.set_price(120.0, 120.05)
    env.broker.shutdown()
    actions = env.system.manager.run()
    assert env.conn.is_alive()
    assert any(action.kind == "partial" and action.ok for action in actions)
    assert len(env.broker.sent) == sent_count + 2  # one confirmed partial and one protected stop update
    events = env.system.journal.events(("mt5_disconnect_detected", "mt5_reconnected"))
    assert any(event["event_type"] == "mt5_disconnect_detected" for event in events)
    assert any(event["event_type"] == "mt5_reconnected" for event in events)
    env.system.close()


def test_pending_order_without_recovered_plan_is_cancelled_fail_safe(tmp_path):
    env = make_env(tmp_path, live=True)
    plan = add_plan(env)
    placed = env.system.engine.process_plan(plan)
    assert placed.outcome is Outcome.PENDING_PLACED
    ticket = env.broker.orders[0].ticket

    # A fresh PlanManager with no restored plan cannot vouch for the old order.
    env.system.close()
    fresh_manager = type(env.mgr)(env.cfg, env.hub)
    reborn = ExecutionSystem(env.cfg, env.conn, fresh_manager, now_fn=lambda: env.broker.now,
                             state_path=env.cfg.execution.state_path)
    actions = reborn.manager.run()
    assert any(action.kind == "cancel" and action.target == str(ticket) for action in actions)
    assert all(order.ticket != ticket for order in env.broker.orders)
    reborn.close()


def test_periodic_reconciliation_overlaps_history_and_recovers_after_read_failure(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    env.cfg.journal.reconciliation_interval_seconds = 1
    first = env.system.reconcile_history()
    first_start = env.system.journal.latest_reconciliation()["history_start"]
    expected_prior_end = pd.Timestamp(env.system.journal.latest_reconciliation()["history_end"])
    env.broker.advance(2)
    assert env.system._reconciliation_due() is False  # explicit success updates scheduler
    monkeypatch.setattr(env.system, "_last_reconcile_monotonic", 0.0)
    original = env.broker.history_orders_get
    env.broker.history_orders_get = lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionError("offline"))

    plan = add_plan(env)
    report = env.system.run_cycle()
    assert report.results and report.results[0].outcome is Outcome.DRY_RUN
    assert "periodic_reconciliation" in [event["payload"].get("component")
                                        for event in env.system.journal.events(("system_error",))]
    assert env.system._last_reconcile_error is not None
    env.broker.history_orders_get = original
    env.system.reconcile_history()
    assert pd.Timestamp(env.system.journal.latest_reconciliation()["history_start"]) <= \
        expected_prior_end - pd.Timedelta(days=1)
    assert first_start
    env.system.close()


def test_delayed_out_of_order_deal_is_recovered_by_overlapping_reconciliation(tmp_path):
    env = make_env(tmp_path)
    position_id = env.broker.add_closed_trade(net=9.0, minutes_ago=15,
                                              magic=env.cfg.execution.magic_number)
    delayed = env.broker.deals.pop()
    first = env.system.reconcile_history()
    row = next(trade for trade in env.system.journal.trades() if trade["position_id"] == position_id)
    assert row["status"] == "unknown"

    env.broker.advance(60)
    delayed.time -= 30  # a delayed, out-of-order broker record inside the overlap window
    env.broker.deals.append(delayed)
    second = env.system.reconcile_history()
    row = next(trade for trade in env.system.journal.trades() if trade["position_id"] == position_id)
    assert second.deals_seen == first.deals_seen + 1
    assert row["status"] == "closed"
    assert row["net"] == pytest.approx(9.0)
    env.system.close()


def test_monitoring_reports_journal_state_and_external_read_failures(tmp_path):
    env = make_env(tmp_path)
    add_plan(env)
    snapshot = env.system.monitoring.snapshot(env.now)
    assert "journal" in snapshot
    assert snapshot["journal"]["schema_version"] == JournalStore.SCHEMA_VERSION
    assert "recent_errors_and_recovery" in snapshot["journal"]

    env.broker.fail_positions = True
    env.broker.last_error = lambda: (-2, "terminal error")
    with pytest.raises(MT5DataError):
        env.system.monitoring.snapshot(env.now)
    env.system.close()


def test_history_without_closure_or_live_position_is_not_reported_as_open(tmp_path):
    env = make_env(tmp_path)
    position = env.broker.add_open_position(
        "XAUUSDm", True, 0.1, 118.0, 112.0, 150.0, env.cfg.execution.magic_number,
    )
    env.broker.positions.clear()  # history only proves an entry; current exposure fetch says none
    env.system.reconcile_history()
    trade = next(row for row in env.system.journal.trades() if row["position_id"] == position.ticket)
    assert trade["status"] == "unknown"
    env.system.close()


def test_kill_switch_blocks_entries_then_releases_without_live_trading_by_default(tmp_path):
    env = make_env(tmp_path, live=False)
    plan = add_plan(env)
    env.cfg.execution.kill_switch_path.write_text("stop", encoding="utf-8")
    assert env.system.engine.process_plan(plan).outcome is Outcome.BLOCKED
    assert env.broker.sent == []
    env.cfg.execution.kill_switch_path.unlink()
    assert env.system.engine.process_plan(plan).outcome is Outcome.DRY_RUN
    assert env.broker.sent == []
    env.system.close()


def test_journal_and_export_failures_do_not_mutate_broker_state(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    plan = add_plan(env)
    monkeypatch.setattr(env.system.journal, "record_event",
                        lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")))
    before = (list(env.broker.sent), list(env.broker.orders), list(env.broker.positions))
    result = env.system.engine.process_plan(plan)
    assert result.outcome is Outcome.DRY_RUN
    assert before == (env.broker.sent, env.broker.orders, env.broker.positions)

    file_parent = tmp_path / "not-a-directory"
    file_parent.write_text("file", encoding="utf-8")
    with pytest.raises(OSError):
        export_excel(env.system.journal, file_parent / "journal.xlsx")
    assert before == (env.broker.sent, env.broker.orders, env.broker.positions)
    env.system.close()


def test_news_provider_outage_uses_stale_cache_and_recovery_remains_enforced(tmp_path):
    env, guard, service, provider = news_env(tmp_path, [ev("2026-01-07 10:05")], live=True)
    env.system.engine.process_plan(add_plan(env))
    assert env.broker.sent == []
    provider.fail = "provider timeout"
    env.broker.now = ts("2026-01-07 10:06")
    assert not service.refresh(force=True)
    assert guard.check("XAUUSD", env.broker.now).locked  # known event remains locked
    provider.fail = None
    provider.events = [ev("2026-01-07 13:30")]
    env.broker.now = ts("2026-01-07 10:12")
    assert service.refresh(force=True)
    assert guard.check("XAUUSD", env.broker.now).locked is False
    result = env.system.engine.process_plan(add_plan(env))
    assert result.outcome in (Outcome.PENDING_PLACED, Outcome.DRY_RUN)
    env.system.close()
