"""Phase 5 - news protection inside the real Phase 4 pipeline, monitor alerts, router alerts."""

from __future__ import annotations

import pandas as pd
import pytest

from smc.execution import CycleReport, ExecutionSystem, Outcome
from smc.execution.trade_manager import Action
from smc.models.plan import EntryModel, PlanDirection, PlanStatus
from smc.news import CalendarService, NewsGuard, NewsMonitor
from smc.notify.router import NotificationRouter
from smc.planning import ChangeKind, PlanChange
from tests.fake_broker import A_DEAL, A_REMOVE, epoch
from tests.harness import add_plan
from tests.news_helpers import FakeProvider, RecordingNotifier, ev, news_env, ts
from tests.test_phase4_management import closes, open_long, removed

LOCK_EVENT = "2026-01-07 10:05"  # lock 09:55 - 10:15 UTC


def why(res) -> str:
    return " | ".join(res.reasons)


# ============================================= news lock in the entry pipeline
@pytest.mark.parametrize("model", [EntryModel.OB, EntryModel.FVG, EntryModel.SWEEP_MSS, EntryModel.OB_FVG])
def test_lock_blocks_every_entry_model_for_limit_orders(tmp_path, model):
    env, *_ = news_env(tmp_path, [ev(LOCK_EVENT)])
    res = env.system.engine.process_plan(add_plan(env, model=model))
    assert res.outcome is Outcome.REJECTED and "news lock" in why(res) and "CPI m/m" in why(res)
    assert env.broker.sent == [] and env.broker.checked == []


def test_lock_blocks_market_orders_too(tmp_path):
    env, *_ = news_env(tmp_path, [ev(LOCK_EVENT)], live=True)
    env.broker.set_price(115.0, 115.05)  # inside the zone: would be a MARKET order
    res = env.system.engine.process_plan(add_plan(env, status=PlanStatus.READY_TO_EXECUTE))
    assert res.outcome is Outcome.REJECTED and "news lock" in why(res) and env.broker.sent == []


def test_outside_the_lock_the_same_plan_goes_through(tmp_path):
    env, *_ = news_env(tmp_path, [ev("2026-01-07 13:30")])
    assert env.system.engine.process_plan(add_plan(env)).outcome is Outcome.DRY_RUN


def test_lock_is_rechecked_immediately_before_submission(tmp_path):
    env, *_ = news_env(tmp_path, [ev(LOCK_EVENT)], live=True)
    calls = [0]

    def drifting_clock():  # the first look is at 09:50 (free), the final re-check happens at 09:56 (locked)
        calls[0] += 1
        return ts("2026-01-07 09:50") if calls[0] == 1 else ts("2026-01-07 09:56")

    env.system.gateway._now = drifting_clock
    res = env.system.engine.process_plan(add_plan(env))
    assert res.outcome is Outcome.REJECTED and "final re-validation failed" in why(res) and "news lock" in why(res)
    assert env.broker.sent == []


def test_a_stale_plan_or_delayed_request_cannot_bypass_the_lock(tmp_path):
    env, *_ = news_env(tmp_path, [ev(LOCK_EVENT)], live=True)
    old_plan = add_plan(env, age_min=300)  # created long before the event became relevant
    assert env.system.engine.process_plan(old_plan).outcome is Outcome.REJECTED
    env.broker.advance(20 * 60)  # 10:20, lock over: the same plan is evaluated afresh
    assert "news lock" not in why(env.system.engine.process_plan(old_plan))


def test_pending_orders_expire_at_the_next_lock_start_as_a_broker_side_backstop(tmp_path):
    env, *_ = news_env(tmp_path, [ev("2026-01-07 13:30")], live=True)
    env.system.engine.process_plan(add_plan(env))
    assert env.broker.orders[0].time_expiration == epoch(ts("2026-01-07 13:20"))  # before the 22:30 cutoff expiry


# ============================== resting orders during a lock; position policy
def place_with_event(tmp_path, event="2026-01-07 13:30", **kw):
    env, guard, svc, prov = news_env(tmp_path, [ev(event)], live=True, **kw)
    plan = add_plan(env)
    assert env.system.engine.process_plan(plan).outcome is Outcome.PENDING_PLACED
    return env, plan


def test_resting_orders_are_cancelled_when_the_lock_begins_and_not_before(tmp_path):
    env, plan = place_with_event(tmp_path)
    env.broker.advance(30 * 60)  # 10:30 - far from the event
    assert env.system.manager.run() == [] and len(env.broker.orders) == 1
    env.broker.now = ts("2026-01-07 13:20")  # lock starts
    acts = env.system.manager.run()
    assert [a.kind for a in acts] == ["cancel"] and acts[0].ok and "news lock" in acts[0].note
    assert env.broker.orders == [] and len(removed(env)) == 1


def test_open_positions_are_kept_by_default_during_a_lock(tmp_path):
    env, *_ = news_env(tmp_path, [ev("2026-01-07 10:30")], live=True)
    plan, pos = open_long(env)
    env.broker.now = ts("2026-01-07 10:25")  # inside the lock
    acts = env.system.manager.run()
    assert env.broker.positions and not any(a.kind == "flatten" for a in acts) and closes(env) == []


def test_flatten_policy_closes_bot_positions_only_for_a_real_event_lock(tmp_path):
    env, *_ = news_env(tmp_path, [ev("2026-01-07 10:30")], live=True, open_position_policy="flatten")
    open_long(env)
    env.broker.now = ts("2026-01-07 10:25")
    acts = env.system.manager.run()
    assert env.broker.positions == [] and any(a.kind == "flatten" and a.ok and "news policy" in a.note for a in acts)

    env2, guard2, svc2, prov2 = news_env(tmp_path / "b", [ev("2026-01-07 15:00")], live=True,
                                         open_position_policy="flatten", max_calendar_age_hours=0.5)
    open_long(env2)
    env2.broker.now = ts("2026-01-07 10:45")  # same day, calendar now older than 30 min: UNVERIFIED, not an event
    assert guard2.check("XAUUSD", env2.broker.now).code == "calendar_unverified"
    env2.system.manager.run()
    assert len(env2.broker.positions) == 1  # an outage alone never flattens


def test_restart_inside_a_lock_still_blocks_new_entries_from_cache_alone(tmp_path):
    env, guard, svc, prov = news_env(tmp_path, [ev(LOCK_EVENT)], live=True)
    clock = lambda: env.broker.now  # noqa: E731
    down = FakeProvider()
    down.fail = "feed down"
    reborn_svc = CalendarService(down, env.cfg.news, tmp_path / "calendar_cache.json", clock)  # cache only
    reborn_svc.refresh(force=True)
    reborn = ExecutionSystem(env.cfg, env.conn, env.mgr, now_fn=clock, state_path=env.cfg.execution.state_path,
                             news_guard=NewsGuard(reborn_svc, env.cfg.news, now_fn=clock))
    res = reborn.engine.process_plan(add_plan(env))
    assert res.outcome is Outcome.REJECTED and "news lock" in why(res) and env.broker.sent == []


def test_calendar_outage_fails_safe_then_recovers(tmp_path):
    env, guard, svc, prov = news_env(tmp_path, [ev("2026-01-07 13:30")], fail="HTTP 429 from calendar feed")
    res = env.system.engine.process_plan(add_plan(env))
    assert res.outcome is Outcome.REJECTED and "not verified" in why(res) and "never loaded" in why(res)
    prov.fail = None
    env.broker.advance(6 * 60)
    assert svc.refresh()
    assert env.system.engine.process_plan(add_plan(env)).outcome is Outcome.DRY_RUN


def test_outage_policy_allow_keeps_trading_but_the_status_is_logged(tmp_path, caplog):
    env, *_ = news_env(tmp_path, [], fail="down", on_calendar_unavailable="allow")
    with caplog.at_level("WARNING", logger="smc"):
        assert env.system.engine.process_plan(add_plan(env)).outcome is Outcome.DRY_RUN
    assert "WITHOUT verified news data" in caplog.text


# ================================================================= monitor alerts
def monitor_env(tmp_path, events, supports_actuals=False, now="2026-01-07 12:00", **cfg):
    env, guard, svc, prov = news_env(tmp_path, events, now=now, supports_actuals=supports_actuals, **cfg)
    note = RecordingNotifier(env.system.state)
    return env, guard, svc, prov, note, NewsMonitor(guard, env.cfg.news, note, "UTC", now_fn=lambda: env.broker.now)


def at(env, mon, when):
    env.broker.now = ts(when)
    return mon.tick(ts(when))


def test_upcoming_lock_and_release_alerts_fire_once_each(tmp_path):
    env, guard, svc, prov, note, mon = monitor_env(tmp_path, [ev("2026-01-07 13:30")])
    at(env, mon, "2026-01-07 12:20")
    assert note.sent == []  # 70 minutes ahead: too early
    st = at(env, mon, "2026-01-07 12:35")
    assert note.texts("UPCOMING NEWS in 55 min") and note.texts("PRE-NEWS SCENARIOS") and st.upcoming
    at(env, mon, "2026-01-07 12:36")
    assert len(note.texts("UPCOMING")) == 1 and len(note.texts("PRE-NEWS")) == 1
    st = at(env, mon, "2026-01-07 13:21")
    assert len(note.texts("NEWS LOCK ON")) == 1 and st.active_locks and "13:40" in note.texts("NEWS LOCK ON")[0]
    at(env, mon, "2026-01-07 13:25")
    assert len(note.texts("NEWS LOCK ON")) == 1  # no repeats while locked
    at(env, mon, "2026-01-07 13:41")
    at(env, mon, "2026-01-07 13:42")
    assert len(note.texts("NEWS LOCK RELEASED")) == 1 and "CPI m/m" in note.texts("NEWS LOCK RELEASED")[0]


def test_restart_during_a_window_neither_loses_nor_repeats_alerts(tmp_path):
    env, guard, svc, prov, note, mon = monitor_env(tmp_path, [ev("2026-01-07 13:30")])
    at(env, mon, "2026-01-07 13:22")
    assert len(note.texts("NEWS LOCK ON")) == 1
    reborn_note = RecordingNotifier(env.system.state)  # same persisted state, new process
    reborn = NewsMonitor(guard, env.cfg.news, reborn_note, "UTC", now_fn=lambda: env.broker.now)
    at(env, reborn, "2026-01-07 13:30")
    assert reborn_note.texts("NEWS LOCK ON") == [] and reborn_note.texts("UPCOMING") == []
    at(env, reborn, "2026-01-07 13:41")
    assert len(reborn_note.texts("NEWS LOCK RELEASED")) == 1


def test_old_events_do_not_spam_release_alerts_on_startup(tmp_path):
    env, guard, svc, prov, note, mon = monitor_env(tmp_path, [ev("2026-01-07 08:30")])
    at(env, mon, "2026-01-07 12:00")
    assert note.sent == []


def test_hot_cool_mixed_outcome_alert_once_when_the_actual_arrives(tmp_path):
    env, guard, svc, prov, note, mon = monitor_env(tmp_path, [ev("2026-01-07 13:30")], supports_actuals=True)
    at(env, mon, "2026-01-07 13:35")
    assert note.texts("NEWS RESULT") == []  # forecast known, actual not yet: nothing claimed
    prov.events = [ev("2026-01-07 13:30", actual="0.5%")]
    env.broker.now = ts("2026-01-07 13:40")
    svc.refresh(force=True)
    at(env, mon, "2026-01-07 13:40")
    at(env, mon, "2026-01-07 13:41")
    out = note.texts("NEWS RESULT")
    assert len(out) == 1 and "HOTTER" in out[0] and "actual 0.5%" in out[0] and "no trade is created" in out[0]


def test_conflicting_components_are_reported_as_mixed(tmp_path):
    rows = [ev("2026-01-09 13:30", title="Non-Farm Employment Change", forecast="150K", actual="210K"),
            ev("2026-01-09 13:30", title="Unemployment Rate", forecast="4.0%", actual="4.3%")]
    env, guard, svc, prov, note, mon = monitor_env(tmp_path, rows, now="2026-01-09 13:45", supports_actuals=True)
    at(env, mon, "2026-01-09 13:45")
    out = note.texts("NEWS RESULT")
    assert len(out) == 1 and "MIXED" in out[0] and "conflict" in out[0]


def test_news_scenarios_never_create_plans_or_orders(tmp_path):
    env, guard, svc, prov, note, mon = monitor_env(
        tmp_path, [ev("2026-01-07 13:30", actual="0.9%")], supports_actuals=True, now="2026-01-07 13:35", )
    before = list(env.mgr.store.all())
    for t in ("13:35", "13:40", "13:50", "14:30"):
        at(env, mon, f"2026-01-07 {t}")
    assert note.texts("HOTTER") and list(env.mgr.store.all()) == before and env.broker.sent == []


def test_calendar_health_alerts_on_transitions_only(tmp_path):
    env, guard, svc, prov, note, mon = monitor_env(tmp_path, [ev("2026-01-09 13:30")])
    at(env, mon, "2026-01-07 12:01")
    assert note.texts("CALENDAR") == []  # healthy start: silent
    prov.fail = "HTTP 429 from calendar feed"
    at(env, mon, "2026-01-08 13:00")  # >24h since the last success
    at(env, mon, "2026-01-08 13:10")
    assert len(note.texts("NOT VERIFIED")) == 1 and "BLOCKED" in note.texts("NOT VERIFIED")[0]
    prov.fail = None
    at(env, mon, "2026-01-08 13:20")
    assert len(note.texts("CALENDAR RECOVERED")) == 1


def test_rescheduled_events_are_announced_once(tmp_path):
    env, guard, svc, prov, note, mon = monitor_env(tmp_path, [ev("2026-01-07 13:30")])
    prov.events = [ev("2026-01-07 14:00")]
    at(env, mon, "2026-01-07 13:05")  # past the 60 minute refresh interval: the change is discovered
    at(env, mon, "2026-01-07 13:06")
    assert len(note.texts("NEWS RESCHEDULED")) == 1


# ================================================================== router alerts
def routed(tmp_path, **kw):
    env, guard, svc, prov = news_env(tmp_path, [], **kw)
    note = RecordingNotifier(env.system.state)
    return env, note, NotificationRouter(note, env.cfg, env.system)


def change(env, plan, kind, text="note"):
    return PlanChange(kind, env.now, plan.plan_id, text)


def test_plan_lifecycle_alerts_are_clear_and_not_repeated(tmp_path):
    env, note, router = routed(tmp_path)
    plan = add_plan(env)
    router.plan_changes([change(env, plan, ChangeKind.CREATED)])
    router.plan_changes([change(env, plan, ChangeKind.CREATED)])
    text = note.texts("NEW PLAN")[0]
    for needle in (plan.plan_id, "XAUUSD", "M15", "LONG", "OB", "114-116", "SL 112", "TP 150.2"):
        assert needle in text
    assert len(note.texts("NEW PLAN")) == 1
    router.plan_changes([change(env, plan, ChangeKind.STATUS, "active: price near")])
    assert len(note.sent) == 1  # routine status changes are not alerts
    plan.status = PlanStatus.READY_TO_EXECUTE
    router.plan_changes([change(env, plan, ChangeKind.STATUS, "ready_to_execute: traded into zone")])
    plan.cancel_reason = "invalidated: body close 111.00 below 112.00"
    router.plan_changes([change(env, plan, ChangeKind.CANCELED)])
    other = add_plan(env, direction=PlanDirection.SHORT)
    other.flip_reason = "zone broken by body close 143.00"
    other.child_plan_ids = ["PLAN-XAUUSD-0099"]
    router.plan_changes([change(env, other, ChangeKind.FLIPPED), change(env, plan, ChangeKind.UPGRADED)])
    assert note.texts("PLAN READY") and note.texts("PLAN CANCELED") and note.texts("ZONE FLIPPED") and note.texts("PLAN UPGRADED")
    assert "PLAN-XAUUSD-0099" in note.texts("ZONE FLIPPED")[0] and "invalidated" in note.texts("PLAN CANCELED")[0]


def test_execution_and_management_alerts_follow_confirmed_events(tmp_path):
    env, note, router = routed(tmp_path, live=True)
    env.broker.set_price(115.0, 115.05)
    plan = add_plan(env, status=PlanStatus.READY_TO_EXECUTE)
    for _ in range(3):  # three cycles: still exactly one fill alert
        router.cycle(env.system.run_cycle())
    fills = note.texts("TRADE CONFIRMED BY MT5")
    assert len(fills) == 1 and "0.08 lots" in fills[0] and "XAUUSD M15 BUY" in fills[0]  # one alert, whichever path saw the fill
    env.broker.set_price(122.5, 122.55)
    for _ in range(2):
        router.cycle(env.system.run_cycle())
    assert len(note.texts("PARTIAL CLOSE CONFIRMED")) == 1 and len(note.texts("BREAK-EVEN CONFIRMED")) == 1
    rep = CycleReport(actions=[Action("trail", "7", True, "SL 115.05 -> 119.9 behind protected swing 01-07 10:00")])
    router.cycle(rep)
    router.cycle(rep)
    assert len(note.texts("STRUCTURAL STOP UPDATE")) == 1


def test_pending_placed_uncertain_and_failed_orders_alert_once(tmp_path):
    env, note, router = routed(tmp_path, live=True)
    env.broker.behaviors.append("timeout_not_executed")
    add_plan(env)
    router.cycle(env.system.run_cycle())
    router.cycle(env.system.run_cycle())
    assert len(note.texts("ORDER OUTCOME UNCERTAIN")) == 1  # the AWAITING repeat is the same alert
    env2, note2, router2 = routed(tmp_path / "x", live=True)
    env2.broker.behaviors.append("reject")
    add_plan(env2)
    router2.cycle(env2.system.run_cycle())
    assert note2.texts("ORDER FAILED") and "rejected" in note2.texts("ORDER FAILED")[0]
    env3, note3, router3 = routed(tmp_path / "y", live=True)
    add_plan(env3)
    router3.cycle(env3.system.run_cycle())
    assert len(note3.texts("PENDING ORDER PLACED")) == 1


def test_dry_run_and_routine_rejections_are_silent(tmp_path):
    env, note, router = routed(tmp_path)
    add_plan(env)
    add_plan(env, sl=117.0)  # invalid stop: a routine validation rejection
    for _ in range(3):
        router.cycle(env.system.run_cycle())
    assert note.sent == [] or all("CUTOFF" in t for t, _ in note.sent)


def test_risk_limit_activation_alerts_once_per_day(tmp_path):
    env, note, router = routed(tmp_path, live=True)
    for i in range(3):
        env.broker.add_closed_trade(+10.0, 300 - i * 30, env.cfg.execution.magic_number)
    add_plan(env)
    for _ in range(3):
        router.cycle(env.system.run_cycle())
    assert len(note.texts("RISK LIMIT ACTIVE: max trades day")) == 1


def test_kill_switch_cutoff_and_error_alerts(tmp_path):
    env, note, router = routed(tmp_path)
    router.cycle(CycleReport())
    env.system.kill.engage("operator")
    router.cycle(CycleReport())
    router.cycle(CycleReport())
    assert len(note.texts("KILL SWITCH ENGAGED")) == 1 and "operator" in note.texts("KILL SWITCH ENGAGED")[0]
    env.system.kill.release()
    router.cycle(CycleReport())
    assert note.texts("Kill switch released")
    err = CycleReport(errors=["management failed: MT5 connection lost"])
    router.cycle(err)
    router.cycle(err)
    assert len(note.texts("SYSTEM ERROR")) == 1
    late, note2, router2 = routed(tmp_path / "late", now="2026-01-07 22:35")
    router2.cycle(CycleReport())
    router2.cycle(CycleReport())
    assert len(note2.texts("DAILY CUTOFF 22:30")) == 1 and not note2.texts("DAILY CUTOFF 23:00")
    late.broker.advance(31 * 60)
    router2.cycle(CycleReport())
    assert len(note2.texts("DAILY CUTOFF 23:00")) == 1


def test_news_blocked_entries_are_not_alert_spam(tmp_path):
    env, guard, svc, prov = news_env(tmp_path, [ev(LOCK_EVENT)])
    note = RecordingNotifier(env.system.state)
    router = NotificationRouter(note, env.cfg, env.system)
    add_plan(env)
    for _ in range(3):
        router.cycle(env.system.run_cycle())
    assert note.sent == []  # the monitor announces the lock itself; each rejection is not an alert


def test_components_released_together_produce_one_alert_per_stage(tmp_path):
    rows = [ev("2026-01-07 13:30", title="CPI m/m"), ev("2026-01-07 13:30", title="Core CPI m/m")]
    env, guard, svc, prov, note, mon = monitor_env(tmp_path, rows)
    for t in ("12:40", "13:22", "13:41"):
        at(env, mon, f"2026-01-07 {t}")
    for header in ("UPCOMING NEWS", "PRE-NEWS SCENARIOS", "NEWS LOCK ON", "NEWS LOCK RELEASED"):
        assert len(note.texts(header)) == 1, header
    assert "CPI m/m" in note.texts("NEWS LOCK ON")[0] and "Core CPI m/m" in note.texts("NEWS LOCK ON")[0]
