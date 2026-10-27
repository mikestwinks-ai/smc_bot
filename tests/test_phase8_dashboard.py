from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pandas as pd
import pytest

import smc.dashboard.data_access as data_access_module
import smc.dashboard.snapshot as snapshot_module
from smc.core.timeframes import StructureLayer
from smc.core.data_manager import CandleStore, MarketDataManager
from smc.core.exceptions import MT5DataError
from smc.dashboard.charts import build_candlestick_chart
from smc.dashboard.data_access import (
    SNAPSHOT_STALE_AFTER_SECONDS,
    ReadOnlyJournal,
    load_snapshot,
)
from smc.dashboard.pages import (
    _display_label,
    _closed_trades,
    _observed_exposure_rows,
    _plan_rows,
    _recent_event_rows,
    _recover_legacy_plan_provenance,
    _risk_limit_rows,
    _session_rows,
    _structure_bias_rows,
    _snapshot_freshness,
    dashboard_refresh_seconds,
)
from smc.dashboard.snapshot import (
    AsyncSnapshotPublisher,
    SnapshotPublisherAlreadyRunningError,
    _plan,
    _structure,
    _snapshot_event,
    build_runtime_snapshot,
)
from smc.journal.store import JournalStore
from smc.structure.models import (
    Bias,
    FairValueGap,
    FlipSignal,
    LiquidityKind,
    LiquiditySide,
    LiquiditySweep,
    StructureUpdate,
    ZoneKind,
)
from smc.models.plan import EntryModel, PlanDirection, PlanZones
from tests.harness import SYM, add_plan, make_env
from smc.core.timeframes import Timeframe


def test_dashboard_snapshot_uses_cached_bot_state_without_broker_requests(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    add_plan(env)
    env.system.reconcile_history()
    data = MarketDataManager(env.conn, env.cfg)

    def forbidden(*args, **kwargs):
        raise AssertionError("dashboard snapshot attempted an MT5 operation")

    for name in ("account", "positions", "pending_orders", "deals", "history_orders", "tick", "send"):
        monkeypatch.setattr(env.system.gateway, name, forbidden)

    snapshot = build_runtime_snapshot(env.cfg, data, env.mgr, env.system, now=env.now)
    assert snapshot["execution"]["live_enabled"] is False
    assert snapshot["account"]["environment"] == "demo"
    assert snapshot["account"]["login_masked"].startswith("****")
    assert '"login": 1' not in json.dumps(snapshot)
    assert snapshot["plans"][0]["plan_id"]
    assert snapshot["positions"]["available"] is True
    assert snapshot["market"] == {}
    assert snapshot["risk"]["available"] is False
    assert "broker history reads" in snapshot["risk"]["unavailable_reason"]
    assert snapshot["risk"]["limits"]["max_trades_per_day"] == env.cfg.risk.max_trades_per_day
    for measurement in (
        "daily_trade_count", "consecutive_losses", "realised_pl_today", "realised_loss_pct",
        "total_drawdown_pct", "baseline_balance", "open_count", "limits_ok",
    ):
        assert snapshot["risk"][measurement] is None
    assert snapshot["risk"]["sessions"]["cutoff_timezone"] == env.cfg.sessions.cutoff_timezone
    assert snapshot["risk"]["sessions"]["windows"][0] == {
        "name": "London",
        "timezone": "Europe/London",
        "start": "08:00",
        "end": "17:00",
    }
    assert snapshot["risk"]["sessions"]["trading_day_start"] == "00:00"
    assert snapshot["risk"]["sessions"]["no_new_trades_after"] == "22:30"
    assert snapshot["risk"]["sessions"]["close_all_risk_by"] == "23:00"
    assert snapshot["risk"]["sessions"]["crypto_exempt_from_cutoffs"] is True
    env.system.close()


def test_snapshot_publishes_scenario_evaluation_when_no_zone_qualifies(tmp_path):
    env = make_env(tmp_path)
    tracker = env.hub.get_tracker(SYM, Timeframe.M15)
    assert tracker is not None
    current_price = tracker.last_close
    assert current_price is not None

    env.hub.zones._zones.clear()
    env.hub.zones._by_id.clear()
    env.hub.zones.add(FairValueGap(
        id="FVG:XAUUSD:M15:far-supply",
        kind=ZoneKind.FVG,
        symbol=SYM,
        timeframe=Timeframe.M15,
        bias=Bias.BEARISH,
        top=current_price + 101.0,
        bottom=current_price + 100.0,
        created_time=tracker.buffer.time(tracker.buffer.last_index),
        created_index=tracker.buffer.last_index,
        gap_size=1.0,
        stop_reference=current_price + 102.0,
    ))
    data = MarketDataManager(env.conn, env.cfg)
    data._symbols[SYM] = env.cfg.enabled_symbols[0]
    data._broker_names[SYM] = SYM
    for timeframe in env.cfg.timeframes.all:
        data._stores[(SYM, timeframe)] = CandleStore(
            SYM, timeframe, env.cfg.data.max_bars_in_memory
        )

    snapshot = build_runtime_snapshot(env.cfg, data, env.mgr, env.system, now=env.now)
    structure = snapshot["structure"][SYM]

    assert structure["conditional_scenarios"] == []
    assert structure["conditional_scenario_evaluation"] == {
        "status": "no_qualifying_nearby_zones",
        "live_zone_count": 1,
        "age_eligible_zone_count": 1,
        "context_eligible_zone_count": 1,
        "outside_distance_zone_count": 1,
        "unknown_age_zone_count": 0,
        "missing_price": False,
        "missing_atr_zone_count": 0,
        "approach_atr_mult": env.cfg.planning.approach_atr_mult,
        "reason": "No live zones are within the existing age and approach-distance rules.",
    }
    env.system.close()


def test_snapshot_event_parser_handles_missing_and_malformed_fields():
    assert _snapshot_event(None) is None
    malformed = _snapshot_event({
        "event_type": "system_error",
        "occurred_at": "not a timestamp",
        "payload": "{broken",
    })
    assert malformed is not None
    assert malformed["occurred_at_utc"] is None
    assert malformed["payload"] == {"note": "{broken"}

    incomplete = _snapshot_event({"event_type": None, "payload": None})
    assert incomplete is not None
    assert incomplete["event_type"] is None
    assert incomplete["payload"] == {}


def test_recent_event_rows_format_status_notes_context_and_malformed_payloads():
    rows = _recent_event_rows([
        {
            "occurred_at_utc": "2026-01-02T03:04:00+00:00",
            "event_type": "order_rejected",
            "event_id": "evt-1",
            "correlation_id": "corr-1",
            "plan_id": "plan-1",
            "symbol": "XAUUSD",
            "payload": {"status": "rejected", "note": "rejected", "reason": "Risk limit reached",
                        "component": "execution", "retcode": 10030},
        },
        {"event_type": "news_calendar_status", "payload": '{"status":"stale","note":"calendar unavailable"}'},
        {"event_type": "system_error", "payload": "{malformed json"},
        {"event_type": "system_start", "payload": None},
        {"event_type": None, "payload": ["unexpected", {"detail": "kept readable"}]},
        None,
    ])

    assert len(rows) == 5
    assert rows[0] == {
        "timestamp_utc": "2026-01-02 03:04 UTC",
        "category": "Order",
        "event": "Order Rejected",
        "status": "Rejected",
        "message": "Risk limit reached",
        "context": "Component: execution · Symbol: XAUUSD · Plan: plan-1 · Correlation: corr-1 · "
                   "Event ID: evt-1 · Return code: 10030",
    }
    assert rows[1]["status"] == "Stale"
    assert rows[1]["message"] == "calendar unavailable"
    assert rows[2]["message"] == "{malformed json"
    assert rows[3]["message"] == "Not recorded"
    assert rows[4]["event"] == "Not recorded"
    assert rows[4]["message"] == "unexpected; Detail: kept readable"


def test_dry_run_execution_event_is_not_presented_or_counted_as_a_trade():
    event_rows = _recent_event_rows([{
        "event_type": "execution_result",
        "plan_id": "PLAN-XAUUSD-0001",
        "payload": {
            "outcome": "dry_run",
            "reasons": ["live execution disabled: validated, nothing was sent"],
        },
    }])
    assert event_rows[0]["event"] == "Dry-run execution evaluation"
    assert event_rows[0]["status"] == "No order sent"
    assert "not an order, fill, or trade" in event_rows[0]["message"]
    assert _closed_trades({
        "available": True,
        "trades": [{"status": "open", "net": 0}, {"status": "partial", "net": 0}],
    }) == []


def test_journal_event_reader_preserves_malformed_payload_as_note(tmp_path):
    journal = JournalStore(tmp_path / "journal.sqlite3")
    with journal._db:
        journal._db.execute(
            """INSERT INTO journal_events
               (event_id, occurred_at, event_type, payload_json)
               VALUES (?, ?, ?, ?)""",
            ("evt-malformed", "2026-01-01T00:00:00+00:00", "system_error", "{bad json"),
        )
    event = journal.recent_events(1)[0]
    assert event["event_id"] == "evt-malformed"
    assert event["payload"] == {"note": "{bad json"}
    journal.close()


def test_dashboard_imports_do_not_load_bot_configuration():
    code = (
        "import sys; "
        "import smc.dashboard.data_access, smc.dashboard.pages, smc.dashboard.snapshot; "
        "assert 'smc.config' not in sys.modules"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_missing_and_stale_snapshots_are_explicit(tmp_path):
    missing = load_snapshot(tmp_path / "missing.json")
    assert missing["available"] is False
    assert missing["status"] == "unavailable"

    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({"generated_at_utc": "2020-01-01T00:00:00+00:00"}), encoding="utf-8")
    stale = load_snapshot(path)
    assert stale["available"] is True
    assert stale["snapshot_status"] == "stale"
    assert stale["snapshot_age_seconds"] > 15


def test_snapshot_freshness_boundary_and_visible_publication_time(tmp_path):
    now = pd.Timestamp("2026-01-01T00:00:00Z").to_pydatetime()
    path = tmp_path / "snapshot.json"
    path.write_text(
        json.dumps({"generated_at_utc": (pd.Timestamp(now) - pd.Timedelta(seconds=15)).isoformat()}),
        encoding="utf-8",
    )
    at_boundary = load_snapshot(path, now=now)
    assert at_boundary["snapshot_status"] == "fresh"
    assert at_boundary["snapshot_age_seconds"] == SNAPSHOT_STALE_AFTER_SECONDS
    assert _snapshot_freshness(at_boundary) == (
        "Snapshot published (UTC): 2025-12-31 23:59 UTC · Age: 15.0s · Fresh"
    )

    path.write_text(
        json.dumps({"generated_at_utc": (pd.Timestamp(now) - pd.Timedelta(seconds=16)).isoformat()}),
        encoding="utf-8",
    )
    stale = load_snapshot(path, now=now)
    assert stale["snapshot_status"] == "stale"
    assert "Age: 16.0s · Stale" in _snapshot_freshness(stale)


def test_all_dashboard_pages_share_configured_refresh_interval(monkeypatch):
    from types import SimpleNamespace
    import smc.dashboard.pages as pages

    observed = {}
    rendered = []

    def fragment(*, run_every):
        observed["run_every"] = run_every

        def decorate(callback):
            return callback

        return decorate

    monkeypatch.setattr(
        pages,
        "st",
        SimpleNamespace(session_state={"dashboard_refresh_seconds": 2}, fragment=fragment),
    )
    monkeypatch.setattr(pages, "load_snapshot", lambda: {"available": True})
    pages._with_refresh(rendered.append)
    assert observed["run_every"] == 2.0
    assert rendered == [{"available": True}]
    assert dashboard_refresh_seconds({}) == 5.0
    assert dashboard_refresh_seconds({"dashboard_refresh_seconds": 10}) == 10.0
    assert dashboard_refresh_seconds({"dashboard_refresh_seconds": 7}) == 5.0


def test_stale_snapshot_does_not_present_cached_plans_as_currently_active():
    from streamlit.testing.v1 import AppTest

    snapshot = {
        "available": True,
        "snapshot_status": "stale",
        "snapshot_age_seconds": 25.0,
        "generated_at_utc": "2026-01-01T00:00:00+00:00",
        "mt5": {"connection_status": "stale_or_unknown"},
        "account": {"environment": "demo", "currency": "USD"},
        "execution": {"live_enabled": False, "kill_switch_engaged": False},
        "risk": {"available": False},
        "positions": {"available": True, "items": [], "age_seconds": 0},
        "pending_orders": {"available": True, "items": [], "age_seconds": 0},
        "plans": [{
            "plan_id": "PLAN-XAUUSD-0001", "symbol": "XAUUSD", "direction": "long",
            "entry_model": "fvg", "status": "ready_to_execute",
            "updated_at_utc": "2026-01-01T00:00:00+00:00",
            "entry_zone": {"bottom": 100.0, "top": 101.0},
            "invalidation": {"price": 99.0}, "targets": [103.0], "rr_estimate": 2.0,
        }, {
            "plan_id": "PLAN-XAUUSD-0002", "symbol": "XAUUSD", "direction": "short",
            "entry_model": "ob", "status": "canceled",
        }],
    }

    def app(page_snapshot):
        from smc.dashboard.pages import render_overview

        render_overview(page_snapshot)

    app_test = AppTest.from_function(app, args=(snapshot,), default_timeout=15).run(timeout=15)
    assert not app_test.exception
    rendered = "\n".join(
        [item.value for item in app_test.subheader]
        + [item.value for item in app_test.caption]
        + [item.value for item in app_test.markdown]
    )
    assert "Last-known plans (stale snapshot; activity is not confirmed)" in rendered
    assert "Snapshot published (UTC): 2026-01-01 00:00 UTC · Age: 25.0s · Stale" in rendered
    assert "Last known · Ready To Execute" in rendered
    assert "PLAN-XAUUSD-0002" not in rendered


def test_overview_renders_legacy_plan_basis_and_exact_source_zone():
    from streamlit.testing.v1 import AppTest

    source_zone = {
        "id": "FVG:XAUUSD:M5:20261026T1355:S>F1",
        "kind": "FVG",
        "symbol": "XAUUSD",
        "timeframe": "M5",
        "bias": "bullish",
        "top": 3996.887,
        "bottom": 3994.655,
        "created_utc": "2026-10-26T14:00:00+00:00",
        "status": "mitigated",
    }
    snapshot = {
        "available": True,
        "snapshot_status": "fresh",
        "generated_at_utc": "2026-10-26T15:15:00+00:00",
        "account": {"environment": "demo", "currency": "USD"},
        "execution": {"live_enabled": False, "kill_switch_engaged": False},
        "risk": {"available": False},
        "positions": {"available": True, "items": []},
        "pending_orders": {"available": True, "items": []},
        "plans": [{
            "plan_id": "PLAN-XAUUSD-0001",
            "symbol": "XAUUSD",
            "direction": "long",
            "entry_model": "FVG",
            "status": "planned",
            "created_at_utc": "2026-10-26T15:15:00+00:00",
            "updated_at_utc": "2026-10-26T15:15:00+00:00",
            "source_timeframes": {"external": ["H4"], "internal": ["M15"], "setup": "M5"},
            "entry_zone": {"bottom": 3994.655, "top": 3996.887},
            "invalidation": {"price": 3988.06, "basis": "beyond source FVG"},
            "targets": [4034.42, 4048.32, 4065.08],
            "rr_estimate": 4.25,
            "reason": "FVG setup",
        }],
        "structure": {"XAUUSD": {"zones": [source_zone]}},
    }

    def app(page_snapshot):
        from smc.dashboard.pages import render_overview

        render_overview(page_snapshot)

    app_test = AppTest.from_function(app, args=(snapshot,), default_timeout=15).run(timeout=15)
    assert not app_test.exception
    rendered = "\n".join(
        [item.value for item in app_test.subheader]
        + [item.value for item in app_test.caption]
        + [item.value for item in app_test.markdown]
        + [
            str(cell)
            for table in app_test.table
            for cell in table.value.to_numpy().ravel()
        ]
    )
    assert "Fair value gap (M5)" in rendered
    assert source_zone["id"] in rendered
    assert "Mitigated" in rendered
    assert "recovered from legacy snapshot" in rendered
    assert "Entry range basis" in rendered


def test_overview_renders_all_entry_bases_for_fixture_symbols():
    from streamlit.testing.v1 import AppTest

    symbols = ("EURUSD", "GBPUSD", "BTCUSD", "XAUUSD")
    models = (
        ("OB", "order_block", "Order block", "order_block"),
        ("FVG", "fair_value_gap", "Fair value gap", "fvg"),
        ("OB_FVG", "ob_fvg_confluence", "OB + FVG confluence", "confluence"),
        ("SWEEP_MSS", "sweep_mss_retracement", "Sweep + MSS leg retracement", "sweep"),
    )
    plans = []
    for symbol in symbols:
        for model, basis, label, suffix in models:
            zones = []
            source_ids = {"order_block": None, "fvg": None, "flipped": None}
            entry_zone = {"bottom": 100.0, "top": 102.0}
            relationship = None
            if model == "OB":
                source_ids["order_block"] = f"OB:{symbol}:{suffix}"
                zones = [{
                    "id": source_ids["order_block"], "kind": "OB", "symbol": symbol,
                    "timeframe": "M5", "bias": "bullish", "bottom": 100.0, "top": 102.0,
                    "status": "active", "record_freshness": "current",
                }]
            elif model == "FVG":
                source_ids["fvg"] = f"FVG:{symbol}:{suffix}"
                zones = [{
                    "id": source_ids["fvg"], "kind": "FVG", "symbol": symbol,
                    "timeframe": "M5", "bias": "bullish", "bottom": 100.0, "top": 102.0,
                    "status": "active", "record_freshness": "current",
                }]
            elif model == "OB_FVG":
                ob_id, fvg_id = f"OB:{symbol}:confluence", f"FVG:{symbol}:confluence"
                source_ids["order_block"], source_ids["fvg"] = ob_id, fvg_id
                zones = [{
                    "id": ob_id, "kind": "OB", "symbol": symbol, "timeframe": "M5",
                    "bias": "bullish", "bottom": 99.0, "top": 101.0, "status": "active",
                    "created_index": 20, "leg_start_index": 10, "origin_event_id": f"EV:{symbol}",
                    "record_freshness": "current",
                }, {
                    "id": fvg_id, "kind": "FVG", "symbol": symbol, "timeframe": "M5",
                    "bias": "bullish", "bottom": 100.0, "top": 102.0, "status": "active",
                    "created_index": 15, "record_freshness": "current",
                }]
                entry_zone = {"bottom": 99.0, "top": 102.0}
                relationship = {
                    "kind": "ob_fvg_displacement_leg",
                    "status": "verified",
                    "order_block_id": ob_id,
                    "fvg_id": fvg_id,
                    "origin_event_id": f"EV:{symbol}",
                    "description": "FVG formed within the OB's recorded displacement interval",
                }
            plans.append({
                "plan_id": f"PLAN-{symbol}-{suffix}",
                "symbol": symbol,
                "direction": "long",
                "entry_model": model,
                "status": "planned",
                "created_at_utc": "2026-10-27T05:00:00+00:00",
                "updated_at_utc": "2026-10-27T05:00:00+00:00",
                "source_timeframes": {"external": ["H4"], "internal": ["M15"], "setup": "M5"},
                "entry_zone": entry_zone,
                "source_zone_ids": source_ids,
                "source_zones": zones,
                "entry_range_basis": basis,
                "entry_range_basis_label": f"{label} (M5)",
                "source_relationship": relationship,
                "invalidation": {"price": 98.0, "basis": "fixture structural stop"},
                "targets": [110.0],
                "rr_estimate": 4.0,
                "quality": {"confluence": model == "OB_FVG", "counter_trend_internal": False},
                "reason": "Synthetic UI fixture",
            })
    snapshot = {
        "available": True,
        "snapshot_status": "fresh",
        "generated_at_utc": "2026-10-27T05:00:00+00:00",
        "mt5": {"connection_status": "recent_success"},
        "account": {"environment": "demo", "currency": "USD"},
        "execution": {"live_enabled": False, "kill_switch_engaged": False},
        "risk": {"available": False},
        "positions": {"available": True, "items": []},
        "pending_orders": {"available": True, "items": []},
        "plans": plans,
    }

    def app(page_snapshot):
        from smc.dashboard.pages import render_overview

        render_overview(page_snapshot)

    app_test = AppTest.from_function(app, args=(snapshot,), default_timeout=20).run(timeout=20)
    assert not app_test.exception
    rendered = "\n".join(
        [item.value for item in app_test.subheader]
        + [item.value for item in app_test.caption]
        + [item.value for item in app_test.markdown]
        + [
            str(cell)
            for table in app_test.table
            for cell in table.value.to_numpy().ravel()
        ]
    )
    for symbol in symbols:
        assert symbol in rendered
    for _, _, label, _ in models:
        assert f"{label} (M5)" in rendered
    assert "FVG formed within the OB's recorded displacement interval" in rendered
    assert "Source zone ID not recorded for this plan" in rendered


def test_structure_events_are_bounded_persisted_deduplicated_and_published(tmp_path):
    env = make_env(tmp_path)
    at = env.now
    update = StructureUpdate(
        symbol=SYM,
        timeframe=Timeframe.M15,
        layer=StructureLayer.INTERNAL,
        candles_processed=1,
    )
    update.new_fvgs.append(FairValueGap(
        id="FVG-created", kind=ZoneKind.FVG, symbol=SYM, timeframe=Timeframe.M15,
        bias=Bias.BULLISH, top=120.0, bottom=119.0, created_time=at, created_index=10,
        gap_size=1.0, stop_reference=118.0,
    ))
    update.flip_signals.extend([
        FlipSignal("FVG-flipped", "FVG-flipped>F1", ZoneKind.FVG, SYM, Timeframe.M15,
                   Bias.BULLISH, Bias.BEARISH, at, 118.0, 120.0, 119.0),
        FlipSignal("OB-invalidated", None, ZoneKind.ORDER_BLOCK, SYM, Timeframe.M15,
                   Bias.BEARISH, None, at, 121.0, 120.0, 119.0),
    ])
    update.liquidity_sweeps.append(LiquiditySweep(
        level_id="EQH-1", kind=LiquidityKind.EQUAL_HIGHS, side=LiquiditySide.BUY_SIDE,
        symbol=SYM, timeframe=Timeframe.M15, time=at, level_price=121.0, extreme=122.0,
        close=120.5, swept_level_ids=("EQH-1", "EQH-2"),
    ))

    env.mgr._capture_structure_events(update)
    env.mgr._capture_structure_events(update)
    events = env.mgr.drain_structure_events()
    assert [event["event_type"] for event in events] == [
        "structure_zone_created", "structure_zone_flipped", "structure_zone_invalidated",
        "liquidity_sweep",
    ]
    assert len(env.mgr.recent_structure_events()) == 4
    env.system.journal.record_structure_events(events)
    env.system.journal.record_structure_events(events)
    stored = env.system.journal.recent_events_by_prefix(
        ("structure_zone_", "liquidity_sweep"), limit=100
    )
    assert len(stored) == 4
    assert stored[0]["payload"]["event_key"]

    data = MarketDataManager(env.conn, env.cfg)
    published = build_runtime_snapshot(env.cfg, data, env.mgr, env.system, now=at)
    exposed = published["journal"]["recent_structure_events"]
    assert len(exposed) == 4
    assert {event["event_type"] for event in exposed} == {
        "structure_zone_created", "structure_zone_flipped", "structure_zone_invalidated",
        "liquidity_sweep",
    }
    assert any(event["payload"].get("new_zone_id") == "FVG-flipped>F1" for event in exposed)
    env.system.close()


def test_structure_event_history_query_is_bounded_to_100_rows(tmp_path):
    journal = JournalStore(tmp_path / "journal.sqlite3")
    for index in range(125):
        journal.record_event(
            "structure_zone_created",
            occurred_at=pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(seconds=index),
            idempotency_key=f"structure-test-{index}",
            symbol=SYM,
            status="created",
            note=f"zone {index}",
            zone_id=f"FVG-{index}",
        )
    rows = journal.recent_events_by_prefix(("structure_zone_",), limit=100)
    assert len(rows) == 100
    assert rows[0]["payload"]["zone_id"] == "FVG-124"
    journal.close()


def test_failed_snapshot_replace_preserves_old_snapshot_and_it_becomes_stale(tmp_path, monkeypatch):
    path = tmp_path / "runtime_snapshot.json"
    old = {"generated_at_utc": "2020-01-01T00:00:00+00:00", "sequence": 4}
    path.write_text(json.dumps(old), encoding="utf-8")
    original = path.read_bytes()
    publisher = AsyncSnapshotPublisher(path)

    def failed_replace(source, destination):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(snapshot_module.os, "replace", failed_replace)
    with pytest.raises(OSError, match="simulated replacement failure") as failure:
        publisher._write({"generated_at_utc": "2026-01-01T00:00:00+00:00", "sequence": 5})
    publisher.close()
    assert failure.value.snapshot_operation == "atomic_replace"
    assert failure.value.snapshot_destination == str(path)
    assert Path(failure.value.snapshot_source).parent == path.parent
    assert path.read_bytes() == original
    stale = load_snapshot(path)
    assert stale["snapshot_status"] == "stale"
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))


def test_journal_dashboard_reader_is_read_only_bounded_and_account_magic_scoped(tmp_path):
    path = tmp_path / "journal.sqlite3"
    journal = JournalStore(path)
    with journal._db:
        journal._db.executemany(
            """INSERT INTO reconciliation_runs
               (run_id, account_id, magic, started_at, completed_at, history_start, history_end,
                deals_seen, orders_seen, positions_seen, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                ("run-old", 111, 240101, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00",
                 "2025-12-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", 1, 1, 0, "complete"),
                ("run-current", 222, 240101, "2026-01-02T00:00:00+00:00", "2026-01-02T00:00:01+00:00",
                 "2025-12-02T00:00:00+00:00", "2026-01-02T00:00:00+00:00", 1, 1, 0, "complete"),
                ("run-other-magic", 222, 999999, "2026-01-03T00:00:00+00:00", "2026-01-03T00:00:01+00:00",
                 "2025-12-03T00:00:00+00:00", "2026-01-03T00:00:00+00:00", 1, 1, 0, "complete"),
            ],
        )
        journal._db.executemany(
            """INSERT INTO bot_trades
               (account_id, magic, position_id, symbol, direction, status, opened_at, closed_at,
                volume_open, volume_closed, net, costs, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (111, 240101, 1, "EURUSDm", "buy", "closed", "2026-01-01", "2026-01-01", 0.1, 0.1, 5, 0, "2026-01-01"),
                (222, 240101, 2, "XAUUSDm", "sell", "closed", "2026-01-02", "2026-01-02", 0.1, 0.1, -3, 0, "2026-01-02"),
                (222, 999999, 3, "GBPUSDm", "buy", "closed", "2026-01-03", "2026-01-03", 0.1, 0.1, 8, 0, "2026-01-03"),
            ],
        )
    journal.close()

    result = ReadOnlyJournal(path).read(240101, "2026-01-02T00:00:02+00:00", limit=9999)
    assert result["available"] is True
    assert result["row_limit"] == 250
    assert result["scope"]["login_masked"] == "****22"
    assert [row["position_id"] for row in result["trades"]] == [2]
    assert all(row["magic"] == 240101 for row in result["reconciliations"])

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM bot_trades").fetchone()[0] == 3
        assert connection.execute("PRAGMA user_version").fetchone()[0] == JournalStore.SCHEMA_VERSION
    finally:
        connection.close()


def test_chart_builder_handles_empty_invalid_and_valid_rates():
    assert build_candlestick_chart([], 300) is None
    assert build_candlestick_chart([{"time_utc": "bad", "open": 1}], 300) is None
    chart = build_candlestick_chart(
        [{"time_utc": "2026-01-01T00:00:00+00:00", "open": 1.0, "high": 2.0,
          "low": 0.5, "close": 1.5}], 300, view_revision="XAUUSD_M15_0")
    assert chart is not None
    assert chart.data[0].type == "candlestick"
    assert list(chart.data[0].open) == [1.0]
    assert list(chart.data[0].close) == [1.5]
    assert chart.layout.uirevision == "XAUUSD_M15_0"
    assert chart.layout.xaxis.rangeslider.visible is False
    assert chart.layout.meta["bar_interval_seconds"] == 300


def test_risk_limits_are_formatted_as_human_readable_configured_values():
    rows = _risk_limit_rows({
        "balance_threshold": 10000,
        "risk_pct_below_threshold": 0.5,
        "risk_pct_at_or_above_threshold": 1.0,
        "hard_max_risk_pct": 1.0,
        "max_daily_drawdown_pct": 2.0,
        "max_consecutive_losses": 2,
        "max_trades_per_day": 3,
        "max_open_trades_total": 2,
        "max_open_trades_per_direction_per_symbol": 1,
    }, "USD")
    by_label = {row["Configured control"]: row["Limit"] for row in rows}
    assert by_label["Risk-tier balance threshold"] == "10,000.00 USD"
    assert by_label["Target risk below threshold"] == "0.50%"
    assert by_label["Hard per-trade risk cap"] == "1.00%"
    assert by_label["Daily drawdown limit"] == "2.00%"
    assert by_label["Maximum consecutive losses"] == "2"
    assert by_label["Maximum trades per day"] == "3"


def test_risk_and_plan_formatters_keep_missing_and_nested_values_explicit():
    missing_limits = _risk_limit_rows({"max_daily_drawdown_pct": None})
    missing = "Not available in current snapshot"
    assert next(row for row in missing_limits if row["Configured control"] == "Daily drawdown limit")["Limit"] == missing
    assert next(row for row in missing_limits if row["Configured control"] == "Risk-tier balance threshold")["Limit"] == missing
    sessions = _session_rows({
        "windows": [{"name": "London", "timezone": "Europe/London", "start": "08:00", "end": "17:00"}],
        "cutoff_timezone": "UTC",
        "no_new_trades_after": "22:30",
        "close_all_risk_by": "23:00",
        "crypto_exempt_from_cutoffs": True,
    })
    session_by_name = {row["Schedule"]: row["Configured value"] for row in sessions}
    assert session_by_name["London session"] == "Europe/London: 08:00–17:00 local time"
    assert session_by_name["No new entries after"] == "22:30 (UTC)"

    summary, details = _plan_rows([{
        "plan_id": "PLAN-EURUSD-TEST",
        "symbol": "EURUSD",
        "direction": "long",
        "entry_model": "fvg_retest",
        "status": "ready_to_execute",
        "created_at_utc": "2026-01-01T00:00:00+00:00",
        "updated_at_utc": "2026-01-01T00:05:00+00:00",
        "source_timeframes": {"external": ["H4"], "internal": ["M15"], "setup": "M5"},
        "entry_zone": {"bottom": 1.1, "top": 1.2},
        "invalidation": {"price": 1.0},
        "targets": [1.3, 1.4],
        "rr_estimate": 2.0,
    }])
    assert summary[0]["Status"] == "Ready To Execute"
    assert details[0]["Entry zone"] == "1.1 – 1.2"
    assert details[0]["Source timeframes"] == "External: H4; internal: M15; setup: M5"
    assert summary[0]["Updated (UTC)"] == "2026-01-01 00:05 UTC"


def test_plan_snapshot_and_details_preserve_referenced_source_zone_identity(tmp_path):
    env = make_env(tmp_path)
    fvg = FairValueGap(
        id="FVG-source-1", kind=ZoneKind.FVG, symbol=SYM, timeframe=Timeframe.M15,
        bias=Bias.BULLISH, top=116.0, bottom=114.0, created_time=env.now,
        created_index=10, gap_size=2.0, stop_reference=113.0,
    )
    env.hub.zones.add(fvg)
    plan = add_plan(
        env,
        direction=PlanDirection.LONG,
        model=EntryModel.FVG,
        zone=(116.0, 114.0),
        zones=PlanZones(fvg_id=fvg.id),
    )

    payload = _plan(plan, env.hub.zones.get)
    summary, details = _plan_rows([payload])

    assert payload["source_zone_ids"] == {
        "order_block": None, "fvg": fvg.id, "flipped": None,
    }
    assert payload["source_zones"][0]["id"] == fvg.id
    assert payload["source_zones"][0]["timeframe"] == "M15"
    assert payload["entry_range_basis"] == "fair_value_gap"
    assert payload["entry_range_basis_label"] == "Fair value gap (M15)"
    assert fvg.id in details[0]["Source zones"]
    assert details[0]["Entry range basis"] == "Fair value gap (M15)"
    assert details[0]["Source zones"] != "Unavailable"


def test_plan_snapshot_uses_retained_source_zone_if_zone_book_no_longer_resolves_it(tmp_path):
    env = make_env(tmp_path)
    fvg = FairValueGap(
        id="FVG-retained-1", kind=ZoneKind.FVG, symbol=SYM, timeframe=Timeframe.M15,
        bias=Bias.BULLISH, top=116.0, bottom=114.0, created_time=env.now,
        created_index=10, gap_size=2.0, stop_reference=113.0,
    )
    plan = add_plan(
        env, direction=PlanDirection.LONG, model=EntryModel.FVG,
        zones=PlanZones(fvg_id=fvg.id),
    )
    plan.source_zones = (fvg,)

    payload = _plan(plan, lambda _zone_id: None)
    _, details = _plan_rows([payload])

    assert payload["source_zones"][0]["id"] == fvg.id
    assert payload["source_zones"][0]["kind"] == "FVG"
    assert payload["source_zones"][0]["record_freshness"] == "last_known"
    assert "(last known)" in details[0]["Source zones"]


def test_legacy_snapshot_recovers_only_a_unique_exact_standalone_zone_match():
    plan = {
        "plan_id": "PLAN-XAUUSD-LEGACY",
        "symbol": "XAUUSD",
        "entry_model": "FVG",
        "direction": "long",
        "status": "planned",
        "created_at_utc": "2026-10-26T15:15:00+00:00",
        "source_timeframes": {"setup": "M5"},
        "entry_zone": {"top": 3996.887, "bottom": 3994.655},
        "reason": "Not used for provenance",
    }
    source_zone = {
        "id": "FVG:XAUUSD:M5:20261026T1355:S>F1",
        "kind": "FVG",
        "symbol": "XAUUSD",
        "timeframe": "M5",
        "bias": "bullish",
        "top": 3996.887,
        "bottom": 3994.655,
        "created_utc": "2026-10-26T14:00:00+00:00",
        "status": "mitigated",
    }
    near_but_not_exact = dict(source_zone, id="near-zone", top=3996.886)

    recovered = _recover_legacy_plan_provenance(
        plan,
        {"XAUUSD": {"zones": [near_but_not_exact, source_zone]}},
    )
    summary, details = _plan_rows([recovered])

    assert recovered["source_zone_ids"]["fvg"] == source_zone["id"]
    assert recovered["entry_range_basis"] == "fair_value_gap"
    assert recovered["source_zone_resolution"] == "unique_exact_legacy_match"
    assert recovered["source_zones"][0]["record_freshness"] == "recovered"
    assert details[0]["Entry range basis"] == "Fair value gap (M5)"
    assert source_zone["id"] in details[0]["Source zones"]
    assert "recovered from legacy snapshot" in details[0]["Source zones"]
    assert "Unavailable" not in details[0]["Entry range basis"]
    assert summary[0]["Entry model"] == "Fvg"


def test_legacy_snapshot_with_empty_provenance_fields_can_recover_exact_zone():
    plan = {
        "plan_id": "PLAN-XAUUSD-LEGACY",
        "symbol": "XAUUSD",
        "entry_model": "FVG",
        "direction": "long",
        "created_at_utc": "2026-10-26T15:15:00+00:00",
        "source_timeframes": {"setup": "M5"},
        "entry_zone": {"top": 101.0, "bottom": 100.0},
        "source_zone_ids": {"order_block": None, "fvg": None, "flipped": None},
        "source_zones": [],
    }
    source_zone = {
        "id": "FVG-exact",
        "kind": "FVG",
        "symbol": "XAUUSD",
        "timeframe": "M5",
        "bias": "bullish",
        "top": 101.0,
        "bottom": 100.0,
        "created_utc": "2026-10-26T14:00:00+00:00",
        "status": "active",
    }

    recovered = _recover_legacy_plan_provenance(plan, {"XAUUSD": {"zones": [source_zone]}})

    assert recovered["source_zone_ids"]["fvg"] == "FVG-exact"
    assert recovered["source_zones"][0]["record_freshness"] == "recovered"


def test_legacy_snapshot_reports_ambiguous_source_matches_without_guessing():
    plan = {
        "plan_id": "PLAN-XAUUSD-LEGACY",
        "symbol": "XAUUSD",
        "entry_model": "FVG",
        "direction": "long",
        "created_at_utc": "2026-10-26T15:15:00+00:00",
        "source_timeframes": {"setup": "M5"},
        "entry_zone": {"top": 101.0, "bottom": 100.0},
    }
    zones = [{
        "id": zone_id, "kind": "FVG", "symbol": "XAUUSD", "timeframe": "M5",
        "bias": "bullish", "top": 101.0, "bottom": 100.0,
        "created_utc": "2026-10-26T14:00:00+00:00",
    } for zone_id in ("FVG-a", "FVG-b")]

    recovered = _recover_legacy_plan_provenance(plan, {"XAUUSD": {"zones": zones}})
    _, details = _plan_rows([recovered])

    assert recovered["source_zones"] == []
    assert recovered["source_zone_ids"]["fvg"] is None
    assert "multiple zones" in recovered["source_zone_unavailable_reason"]
    assert "Source zone not resolved" in details[0]["Source zones"]

    near_zone = dict(zones[0], id="FVG-near", top=101.001)
    near_only = _recover_legacy_plan_provenance(plan, {"XAUUSD": {"zones": [near_zone]}})
    assert near_only["source_zones"] == []
    assert "no prior zone has the exact" in near_only["source_zone_unavailable_reason"]


def test_plan_details_identify_missing_referenced_source_zone(tmp_path):
    env = make_env(tmp_path)
    missing_zone_id = "FVG-no-longer-retained"
    plan = add_plan(
        env,
        direction=PlanDirection.LONG,
        model=EntryModel.FVG,
        zones=PlanZones(fvg_id=missing_zone_id),
    )

    payload = _plan(plan, env.hub.zones.get)
    _, details = _plan_rows([payload])

    assert payload["source_zone_ids"]["fvg"] == missing_zone_id
    assert payload["source_zones"] == []
    assert missing_zone_id in details[0]["Source zones"]
    assert "Missing source record" in details[0]["Source zones"]


def test_missing_session_snapshot_values_are_explicit_without_nested_unavailable_text():
    rows = _session_rows({})
    values = {row["Schedule"]: row["Configured value"] for row in rows}
    assert values["Allowed sessions"] == "Not available in current snapshot"
    assert values["Trading day starts"] == "Not available in current snapshot"
    assert values["No new entries after"] == "Not available in current snapshot"
    assert values["Configured close-risk cutoff"] == "Not available in current snapshot"
    assert values["Crypto exempt from cutoffs"] == "Not available in current snapshot"
    assert all("Unavailable (Unavailable)" not in value for value in values.values())


def test_dashboard_table_labels_are_human_readable():
    assert _display_label("last_success_utc") == "Last successful fetch (UTC)"
    assert _display_label("position_id") == "Position ID"
    assert _display_label("risk_limit_status") == "Risk Limit Status"


def test_observed_exposure_formatting_separates_positions_and_pending_orders():
    rows = _observed_exposure_rows(
        [{"symbol": "EURUSD", "direction": "buy"}],
        [{"symbol": "EURUSD", "direction": "buy"}, {"symbol": "BTCUSD", "direction": "sell"}],
        1,
    )
    eur_buy = next(row for row in rows if row["Symbol"] == "EURUSD")
    assert eur_buy == {
        "Symbol": "EURUSD",
        "Direction": "Buy",
        "Positions": 1,
        "Pending orders": 1,
        "Observed total": 2,
        "Configured maximum": 1,
        "Remaining": 0,
    }


def test_risk_page_renders_unavailable_snapshot_without_inventing_zero_or_json():
    from streamlit.testing.v1 import AppTest

    snapshot = {
        "available": True,
        "generated_at_utc": "2026-01-01T00:00:00+00:00",
        "account": {"currency": "USD"},
        "mt5": {"connection_status": "recent_success"},
        "risk": {
            "available": False,
            "unavailable_reason": "No cached risk calculation.",
            "limits": {
                "balance_threshold": 10000,
                "risk_pct_below_threshold": 0.5,
                "max_daily_drawdown_pct": 2,
                "max_consecutive_losses": 2,
                "max_trades_per_day": 3,
                "max_open_trades_total": 2,
            },
            "sessions": {
                "windows": [{"name": "London", "timezone": "Europe/London", "start": "08:00", "end": "17:00"}],
                "cutoff_timezone": "UTC",
                "no_new_trades_after": "22:30",
                "close_all_risk_by": "23:00",
            },
        },
        "execution": {"kill_switch_engaged": False},
        "positions": {"available": True, "items": [], "age_seconds": 0},
        "pending_orders": {"available": True, "items": [], "age_seconds": 0},
    }

    def app(page_snapshot):
        from smc.dashboard.pages import render_risk

        render_risk(page_snapshot)

    app_test = AppTest.from_function(app, args=(snapshot,), default_timeout=15).run(timeout=15)
    assert not app_test.exception
    assert not app_test.json
    assert len(app_test.table) >= 2


def test_market_page_keeps_chart_selectors_read_only():
    from streamlit.testing.v1 import AppTest

    snapshot = {
        "available": True,
        "snapshot_status": "fresh",
        "snapshot_age_seconds": 1.0,
        "mt5": {"connection_status": "recent_success"},
        "market": {
            SYM: {
                "M15": {
                    "timeframe_seconds": 900,
                    "candles": [{
                        "time_utc": "2026-01-01T00:00:00+00:00",
                        "open": 1.0, "high": 1.2, "low": 0.9, "close": 1.1, "tick_volume": 10,
                    }],
                    "last_closed_candle_utc": "2026-01-01T00:00:00+00:00",
                    "stale": False,
                },
            },
        },
    }

    def app(page_snapshot):
        from smc.dashboard.pages import render_market

        render_market(page_snapshot)

    app_test = AppTest.from_function(app, args=(snapshot,), default_timeout=15).run(timeout=15)
    assert not app_test.exception
    assert [widget.label for widget in app_test.selectbox] == ["Symbol", "Timeframe"]
    assert len(app_test.button) == 0


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("EURUSD", {"H4": "bullish", "M30": "bearish", "M15": "bearish", "M5": "bullish"}),
        ("GBPUSD", {"H4": "bearish", "M30": "bullish", "M15": "bullish", "M5": "bearish"}),
    ],
)
def test_structure_snapshot_preserves_distinct_layer_and_timeframe_biases(tmp_path, symbol, expected):
    env = make_env(tmp_path)
    candle_time = pd.Timestamp("2026-10-26T11:15:00Z")
    engines = {
        **{timeframe: env.hub.external for timeframe in env.cfg.timeframes.external},
        **{timeframe: env.hub.internal for timeframe in env.cfg.timeframes.internal},
    }
    for timeframe in env.cfg.timeframes.all:
        tracker = engines[timeframe].tracker(symbol, timeframe)
        tracker.process_candle(candle_time, 1.0, 1.1, 0.9, 1.0)
        tracker.bias = Bias(expected[timeframe.label])

    structure = _structure(env.hub, symbol)
    assert structure["external_anchor_timeframe"] == "H4"
    assert structure["internal_anchor_timeframe"] == "M15"
    assert structure["external_bias"] == expected["H4"]
    assert structure["internal_bias"] == expected["M15"]
    assert structure["external_bias_by_timeframe"] == {
        timeframe.label: expected[timeframe.label] for timeframe in env.cfg.timeframes.external
    }
    assert structure["internal_bias_by_timeframe"] == {
        timeframe.label: expected[timeframe.label] for timeframe in env.cfg.timeframes.internal
    }
    assert structure["external_bias_time_utc_by_timeframe"] == {
        timeframe.label: candle_time.isoformat() for timeframe in env.cfg.timeframes.external
    }
    assert structure["internal_bias_time_utc_by_timeframe"] == {
        timeframe.label: candle_time.isoformat() for timeframe in env.cfg.timeframes.internal
    }
    env.system.close()


def test_structure_snapshot_does_not_present_uninitialized_bias_as_neutral(tmp_path):
    env = make_env(tmp_path)
    structure = _structure(env.hub, "EURUSD")
    assert structure["external_bias"] is None
    assert structure["internal_bias"] is None
    assert set(structure["external_bias_by_timeframe"].values()) == {None}
    assert set(structure["internal_bias_by_timeframe"].values()) == {None}
    assert set(structure["external_bias_time_utc_by_timeframe"].values()) == {None}
    assert set(structure["internal_bias_time_utc_by_timeframe"].values()) == {None}
    env.system.close()


@pytest.mark.parametrize(
    ("symbol", "external", "internal", "expected_external", "expected_internal"),
    [
        ("EURUSD", {"H4": "bullish", "M30": "bearish"}, {"M15": "bearish", "M5": "bullish"},
         "Bullish", "Bearish"),
        ("GBPUSD", {"H4": "bearish", "M30": "bullish"}, {"M15": "bullish", "M5": "bearish"},
         "Bearish", "Bullish"),
    ],
)
def test_market_bias_display_names_anchor_timeframes_and_lists_each_frame(
    symbol, external, internal, expected_external, expected_internal
):
    stamp = "2026-10-26T11:15:00+00:00"
    structure = {
        "external_anchor_timeframe": "H4",
        "internal_anchor_timeframe": "M15",
        "external_bias": external["H4"],
        "internal_bias": internal["M15"],
        "external_bias_by_timeframe": external,
        "internal_bias_by_timeframe": internal,
        "external_bias_time_utc_by_timeframe": {key: stamp for key in external},
        "internal_bias_time_utc_by_timeframe": {key: stamp for key in internal},
        "zones": [],
        "liquidity": [],
    }
    assert f"External bias ({structure['external_anchor_timeframe']} anchor)" == "External bias (H4 anchor)"
    assert f"Internal bias ({structure['internal_anchor_timeframe']} anchor)" == "Internal bias (M15 anchor)"
    assert structure["external_bias"] == external["H4"]
    assert structure["internal_bias"] == internal["M15"]
    assert _structure_bias_rows(structure) == [
        {"layer": "External", "timeframe": timeframe, "bias": bias.title(), "as_of_utc": stamp}
        for timeframe, bias in external.items()
    ] + [
        {"layer": "Internal", "timeframe": timeframe, "bias": bias.title(), "as_of_utc": stamp}
        for timeframe, bias in internal.items()
    ]
    assert expected_external == "Bullish" if symbol == "EURUSD" else "Bearish"
    assert expected_internal == "Bearish" if symbol == "EURUSD" else "Bullish"


def test_snapshot_publisher_writes_atomically_and_closes(tmp_path, monkeypatch):
    path = tmp_path / "dashboard" / "snapshot.json"
    monkeypatch.setattr(snapshot_module.os, "fsync", lambda _fd: None)
    publisher = AsyncSnapshotPublisher(path)
    publisher.publish({"generated_at_utc": "2026-01-01T00:00:00+00:00", "sequence": 0})
    publisher.close()
    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["sequence"] == 1
    assert not path.with_suffix(".json.tmp").exists()
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))
    assert not publisher._thread.is_alive()


def test_snapshot_publishers_use_unique_temporary_files_and_keep_json_valid_during_concurrent_writes(
    tmp_path, monkeypatch
):
    path = tmp_path / "dashboard" / "snapshot.json"
    monkeypatch.setattr(snapshot_module.os, "fsync", lambda _fd: None)
    path.parent.mkdir()
    path.write_text(json.dumps({"sequence": -1}), encoding="utf-8")
    publisher = AsyncSnapshotPublisher(path)
    original_replace = snapshot_module.os.replace
    sources = []

    def record_replace(source, destination):
        sources.append(Path(source))
        return original_replace(source, destination)

    monkeypatch.setattr(snapshot_module.os, "replace", record_replace)
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [
            executor.submit(publisher._write, {"sequence": sequence, "payload": "x" * 1000})
            for sequence in range(36)
        ]
        for future in futures:
            future.result()
    publisher.close()

    assert len(sources) == 36
    assert len({source.name for source in sources}) == 36
    assert all(source.parent == path.parent for source in sources)
    assert json.loads(path.read_text(encoding="utf-8"))["sequence"] in range(36)
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))


def test_second_snapshot_publisher_is_rejected_and_ownership_releases_on_close(tmp_path):
    path = tmp_path / "snapshot.json"
    first = AsyncSnapshotPublisher(path)
    with pytest.raises(SnapshotPublisherAlreadyRunningError) as error:
        AsyncSnapshotPublisher(path)
    assert str(path) in str(error.value)

    first.close()
    next_owner = AsyncSnapshotPublisher(path)
    next_owner.close()


def test_snapshot_reader_retries_transient_decode_failure(tmp_path, monkeypatch):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({"generated_at_utc": "2026-01-01T00:00:00+00:00"}), encoding="utf-8")
    original_read_text = Path.read_text
    calls = 0

    def transient_read_failure(self, *args, **kwargs):
        nonlocal calls
        if self == path and calls == 0:
            calls += 1
            raise json.JSONDecodeError("incomplete concurrent read", "", 0)
        calls += 1
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", transient_read_failure)
    monkeypatch.setattr(data_access_module.time, "sleep", lambda _: None)
    result = load_snapshot(path)

    assert result["available"] is True
    assert calls == 2


def test_snapshot_reader_retries_transient_permission_failure(tmp_path, monkeypatch):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({"generated_at_utc": "2026-01-01T00:00:00+00:00"}), encoding="utf-8")
    original_read_text = Path.read_text
    calls = 0

    def transient_read_failure(self, *args, **kwargs):
        nonlocal calls
        if self == path and calls == 0:
            calls += 1
            raise PermissionError(13, "temporary sharing violation")
        calls += 1
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", transient_read_failure)
    monkeypatch.setattr(data_access_module.time, "sleep", lambda _: None)
    result = load_snapshot(path)

    assert result["available"] is True
    assert calls == 2


def test_snapshot_replace_retries_transient_permission_failure(tmp_path, monkeypatch):
    path = tmp_path / "snapshot.json"
    monkeypatch.setattr(snapshot_module.os, "fsync", lambda _fd: None)
    publisher = AsyncSnapshotPublisher(path)
    original_replace = snapshot_module.os.replace
    attempts = 0

    def transient_replace_failure(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError(13, "temporary sharing violation")
        return original_replace(source, destination)

    monkeypatch.setattr(snapshot_module.os, "replace", transient_replace_failure)
    monkeypatch.setattr(snapshot_module.time, "sleep", lambda _: None)
    publisher._write({"generated_at_utc": "2026-01-01T00:00:00+00:00", "sequence": 1})
    publisher.close()

    assert attempts == 2
    assert json.loads(path.read_text(encoding="utf-8"))["sequence"] == 1


def test_snapshot_reader_reports_persistent_malformed_json(tmp_path, monkeypatch):
    path = tmp_path / "snapshot.json"
    path.write_text("{", encoding="utf-8")
    monkeypatch.setattr(data_access_module.time, "sleep", lambda _: None)

    result = load_snapshot(path)

    assert result["available"] is False
    assert result["status"] == "error"
    assert "after 3 attempts" in result["reason"]
    assert "JSONDecodeError" in result["reason"]


def test_snapshot_writer_failure_is_logged_and_does_not_raise(tmp_path, caplog, monkeypatch):
    path = tmp_path / "snapshot.json"
    publisher = AsyncSnapshotPublisher(path)

    def fail_temp_file(**_kwargs):
        raise PermissionError(13, "simulated access denied")

    monkeypatch.setattr(snapshot_module.tempfile, "NamedTemporaryFile", fail_temp_file)
    publisher.publish({"generated_at_utc": "2026-01-01T00:00:00+00:00"})
    publisher.close()
    assert "Dashboard snapshot write failed (PermissionError)" in caplog.text
    assert "operation=create_unique_temporary_file" in caplog.text
    assert f"destination={path}" in caplog.text
    assert "errno=13" in caplog.text
    assert "Traceback" in caplog.text


def test_market_heartbeat_is_read_from_local_state_without_mt5(tmp_path):
    env = make_env(tmp_path)
    manager = MarketDataManager(env.conn, env.cfg)
    statuses = manager.series_status(pd.Timestamp("2026-01-01T00:00:00Z"))
    assert len(statuses) == len(env.cfg.timeframes.all)
    assert all(row["last_success_utc"] is None for row in statuses)
    assert all(row["last_closed_candle_utc"] is None for row in statuses)
    env.system.close()


def test_market_data_disconnect_and_recovery_updates_dashboard_heartbeat(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    manager = MarketDataManager(env.conn, env.cfg)
    tf = Timeframe.M15
    manager._stores[(SYM, tf)] = CandleStore(SYM, tf, 10)
    manager._broker_names[SYM] = "XAUUSDm"
    manager._symbols[SYM] = env.cfg.enabled_symbols[0]
    monkeypatch.setattr(manager, "_fetch_rates",
                        lambda *args, **kwargs: (_ for _ in ()).throw(MT5DataError("terminal disconnected")))

    with pytest.raises(MT5DataError):
        manager.update(SYM, tf)
    failed = next(row for row in manager.series_status() if row["symbol"] == SYM and row["timeframe"] == "M15")
    assert failed["last_success_utc"] is None
    assert failed["last_error"] == "terminal disconnected"

    candles = pd.DataFrame(
        [{"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
          "tick_volume": 10, "spread": 1, "real_volume": 0}],
        index=pd.DatetimeIndex(["2026-01-07T09:45:00Z"], name="time"),
    )
    monkeypatch.setattr(manager, "_fetch_rates", lambda *args, **kwargs: candles)
    manager.update(SYM, tf)
    recovered = next(row for row in manager.series_status() if row["symbol"] == SYM and row["timeframe"] == "M15")
    assert recovered["last_success_utc"] is not None
    assert recovered["last_closed_candle_utc"] == "2026-01-07T09:45:00+00:00"
    assert recovered["last_error"] is None
    env.system.close()
