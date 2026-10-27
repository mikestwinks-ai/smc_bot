"""Phase 4 - validation, order building, idempotent execution, reconciliation."""

from __future__ import annotations

import pytest

from smc.execution import order_builder as ob
from smc.execution.execution_engine import Outcome
from smc.models.plan import EntryModel, PlanDirection, PlanStatus
from smc.risk.models import OrderKind
from tests.fake_broker import A_DEAL, A_PENDING, A_SLTP, TYPE_BUY_LIMIT
from tests.harness import BSYM, SYM, add_plan, make_env


def why(res) -> str:
    return " | ".join(res.reasons)


# ==================================================== 5: invalid stop / target
@pytest.mark.parametrize("kwargs,needle", [
    (dict(sl=117.0), "not on the loss side"),  # stop above a long entry
    (dict(targets=[110.0]), "not on the profit side"),  # target below a long entry
])
def test_invalid_stop_or_target_is_rejected(tmp_path, kwargs, needle):
    env = make_env(tmp_path)
    res = env.system.engine.process_plan(add_plan(env, **kwargs))
    assert res.outcome is Outcome.REJECTED and needle in why(res)
    assert env.broker.sent == []


def test_stop_inside_the_broker_stops_level_is_rejected(tmp_path):
    env = make_env(tmp_path)
    env.broker.symbols[BSYM].trade_stops_level = 500  # 5.00 minimum distance vs a 4.00 stop
    env.broker.set_price(125.0, 125.05)  # limit entry itself is far enough from the market
    res = env.system.engine.process_plan(add_plan(env))
    assert res.outcome is Outcome.REJECTED and "closer than the broker minimum" in why(res)


def test_short_geometry_is_checked_on_the_other_side(tmp_path):
    env = make_env(tmp_path)
    bad = add_plan(env, direction=PlanDirection.SHORT, sl=139.0)  # stop BELOW a short entry
    assert "not on the loss side" in why(env.system.engine.process_plan(bad))


def test_price_already_beyond_stop_or_target_is_stale(tmp_path):
    env = make_env(tmp_path)
    env.broker.set_price(155.0, 155.05)
    res = env.system.engine.process_plan(add_plan(env))
    assert res.outcome is Outcome.REJECTED and ("through the entry zone" in why(res) or "stale" in why(res))


# ======================================================== 6: FVG needs >= 2R
def test_fvg_plan_below_two_r_is_rejected_but_other_models_use_their_own_floor(tmp_path):
    env = make_env(tmp_path)
    geometry = dict(targets=[122.0])  # entry 116, stop 112 -> reward 6 / risk 4 = 1.5R
    fvg = env.system.engine.process_plan(add_plan(env, model=EntryModel.FVG, **geometry))
    assert fvg.outcome is Outcome.REJECTED and "RR 1.50 < required 2.0 for FVG" in why(fvg)
    both = env.system.engine.process_plan(add_plan(env, model=EntryModel.OB_FVG, **geometry))
    assert "required 2.0 for OB_FVG" in why(both)
    sweep = env.system.engine.process_plan(add_plan(env, model=EntryModel.SWEEP_MSS, targets=[121.0]))  # 1.25R
    assert "required 1.5 for SWEEP_MSS" in why(sweep)
    ob_plan = env.system.engine.process_plan(add_plan(env, model=EntryModel.OB, **geometry))
    assert ob_plan.outcome is Outcome.DRY_RUN
    fvg_ok = env.system.engine.process_plan(add_plan(env, model=EntryModel.FVG, targets=[124.0]))  # exactly 2.0R
    assert fvg_ok.outcome is Outcome.DRY_RUN


# ============================================= 12: lifecycle states cannot trade
@pytest.mark.parametrize("status,needle", [
    (PlanStatus.CANCELED, "canceled"), (PlanStatus.FLIPPED, "flipped"),
    (PlanStatus.EXECUTED, "executed"), (PlanStatus.PLANNED, "executable states")])
def test_plans_in_the_wrong_state_cannot_execute(tmp_path, status, needle):
    env = make_env(tmp_path, live=True)
    plan = add_plan(env, status=status)
    plan.cancel_reason = "invalidated" if status is PlanStatus.CANCELED else None
    res = env.system.engine.process_plan(plan)
    assert res.outcome is Outcome.REJECTED and needle in why(res).lower()
    assert env.broker.sent == []


def test_expired_and_invalidated_plans_are_rejected(tmp_path):
    env = make_env(tmp_path, live=True)
    old = add_plan(env, age_min=96 * 15 + 120)
    assert "expired" in why(env.system.engine.process_plan(old))
    env.broker.advance(0)
    dead = add_plan(env, sl=119.0, zone=(120.0, 118.5))  # last closed candle (118) is already beyond the stop
    assert env.system.engine.process_plan(dead).outcome is Outcome.REJECTED
    assert env.broker.sent == []


def test_plan_not_in_store_or_with_dead_zone_is_rejected(tmp_path):
    env = make_env(tmp_path)
    plan = add_plan(env)
    env.mgr.store._plans.pop(plan.plan_id)
    assert "not in the plan store" in why(env.system.engine.process_plan(plan))
    from smc.models.plan import PlanZones
    ghost = add_plan(env, zones=PlanZones(order_block_id="OB:does-not-exist"))
    assert "no longer exists" in why(env.system.engine.process_plan(ghost))


def test_stale_market_data_and_structure_fail_closed(tmp_path):
    env = make_env(tmp_path)
    env.broker.tick_age = 600
    assert "stale tick" in why(env.system.engine.process_plan(add_plan(env)))
    env.broker.tick_age = 0
    env.broker.advance(3 * 3600)  # structure last updated 3 hours ago
    res = env.system.engine.process_plan(add_plan(env))
    assert res.outcome is Outcome.REJECTED and ("stale" in why(res) or "session" in why(res))
    env.system.view.hub._last_time.clear()
    env.system.engine._validator._view = None
    assert "structure engine unavailable" in why(env.system.engine.process_plan(add_plan(env)))


def test_premium_discount_hard_block_is_rechecked_at_execution(tmp_path):
    env = make_env(tmp_path)
    in_premium = add_plan(env, zone=(130.0, 128.0), sl=117.0, targets=[150.2])  # long entry 130 -> 0.55 of the range
    res = env.system.engine.process_plan(in_premium)
    assert res.outcome is Outcome.REJECTED and "premium/discount re-check" in why(res)


def test_trade_mode_and_spread_guards(tmp_path):
    env = make_env(tmp_path)
    env.broker.symbols[BSYM].trade_mode = 3  # close only
    assert "not open for new trades" in why(env.system.engine.process_plan(add_plan(env)))
    env.broker.symbols[BSYM].trade_mode = 4
    env.broker.set_price(118.0, 119.5)  # 1.5 spread vs 4.0 stop
    assert "spread" in why(env.system.engine.process_plan(add_plan(env)))


# ========================================== order types / filling / stop levels
def test_order_kind_selection_market_limit_stop(tmp_path):
    env = make_env(tmp_path)
    v, spec, plan = env.system.engine._validator, env.system.gateway.symbol_spec(BSYM), add_plan(env)
    tick = lambda b, a: type("T", (), {"bid": b, "ask": a, "spread": a - b})()  # noqa: E731
    assert v.choose_order(plan, tick(118.0, 118.05), spec)[0] is OrderKind.LIMIT  # approaching -> limit at 116
    k, entry, _ = v.choose_order(plan, tick(115.0, 115.05), spec)
    assert k is OrderKind.MARKET and entry == 115.05  # inside the zone -> market at the ask
    assert v.choose_order(plan, tick(110.0, 110.05), spec)[0] is None  # through the zone
    plan.status = PlanStatus.READY_TO_EXECUTE
    assert "moved away" in v.choose_order(plan, tick(118.0, 118.05), spec)[2]
    env.cfg.execution.allow_stop_entries = True
    plan.status = PlanStatus.ACTIVE
    assert v.choose_order(plan, tick(110.0, 110.05), spec)[0] is OrderKind.STOP


def test_request_fields_per_order_kind_and_filling_policy():
    from smc.execution.order_builder import build_entry, market_filling
    from tests.test_phase4_risk import spec
    s = spec(filling_flags=2)
    assert market_filling(s) == 1 and market_filling(spec(filling_flags=1)) == 0 and market_filling(spec(filling_flags=0)) == 2
    mkt = build_entry(OrderKind.MARKET, True, "X", 0.1, 100.0, 99.0, 102.0, s, 7, 20, "c")
    assert mkt["action"] == A_DEAL and mkt["type"] == 0 and mkt["type_filling"] == 1 and mkt["deviation"] == 20
    lim = build_entry(OrderKind.LIMIT, False, "X", 0.1, 100.0, 101.0, 98.0, s, 7, 20, "c")
    assert lim["action"] == A_PENDING and lim["type"] == 3 and "deviation" not in lim
    stp = build_entry(OrderKind.STOP, True, "X", 0.1, 100.0, 99.0, 102.0, s, 7, 20, "c")
    assert stp["type"] == 4 and ob.round_to_tick(100.004, s) == 100.0 and ob.round_to_tick(100.004, s, "up") == 100.01


def test_pending_orders_expire_at_the_no_new_trades_cutoff(tmp_path):
    env = make_env(tmp_path, live=True)
    env.system.engine.process_plan(add_plan(env))
    o = env.broker.orders[0]
    assert o.type == TYPE_BUY_LIMIT and o.time_expiration == int(pd_ts("2026-01-07 22:30").timestamp())


def pd_ts(s):
    import pandas as pd
    return pd.Timestamp(s, tz="UTC")


# =========================================== 13: duplicates, retries, restarts
def test_duplicate_execution_is_prevented_across_retries_and_restarts(tmp_path):
    env = make_env(tmp_path, live=True)
    plan = add_plan(env)
    first = env.system.engine.process_plan(plan)
    assert first.outcome is Outcome.PENDING_PLACED and len(env.broker.sent) == 1
    assert env.system.engine.process_plan(plan).outcome is Outcome.ALREADY  # retry
    env.restart()
    assert env.system.engine.process_plan(plan).outcome is Outcome.ALREADY  # restart, state file intact
    from smc.execution import ExecutionSystem
    amnesiac = ExecutionSystem(env.cfg, env.conn, env.mgr, now_fn=lambda: env.broker.now, persist_state=False)
    assert amnesiac.engine.process_plan(plan).outcome is Outcome.ALREADY  # state file LOST: broker comment scan
    assert len(env.broker.sent) == 1 and len(env.broker.orders) == 1


def test_filled_position_is_registered_and_plan_marked_executed(tmp_path):
    env = make_env(tmp_path, live=True)
    plan = add_plan(env)
    env.system.engine.process_plan(plan)
    pos = env.broker.fill_pending(env.broker.orders[0].ticket)
    again = env.system.engine.process_plan(plan)
    assert again.outcome is Outcome.ALREADY and again.position_ticket == pos.ticket
    mp = env.system.state.get_managed(pos.ticket)
    assert mp and mp.initial_sl == 112.0 and mp.deadline == "2026-01-07T23:00:00+00:00"
    assert plan.status is PlanStatus.EXECUTED
    assert env.system.engine.process_all() == []  # executed plans are never offered again


def test_market_entry_creates_position_with_confirmation(tmp_path):
    env = make_env(tmp_path, live=True)
    env.broker.set_price(115.0, 115.05)  # inside the 114-116 zone
    plan = add_plan(env, status=PlanStatus.READY_TO_EXECUTE)
    res = env.system.engine.process_plan(plan)
    assert res.outcome is Outcome.FILLED and res.position_ticket and env.broker.sent[0]["action"] == A_DEAL
    assert env.broker.positions[0].sl == 112.0 and plan.status is PlanStatus.EXECUTED
    assert env.broker.sent[0]["comment"].startswith(plan.plan_id)


def test_partial_fill_is_accepted_and_the_remainder_is_never_resent(tmp_path):
    env = make_env(tmp_path, live=True)
    env.broker.set_price(115.0, 115.05)
    env.broker.behaviors.append("partial")
    plan = add_plan(env, status=PlanStatus.READY_TO_EXECUTE)
    res = env.system.engine.process_plan(plan)
    assert res.outcome is Outcome.FILLED and res.volume == pytest.approx(0.04)  # half of 0.08
    env.system.engine.process_plan(plan)
    assert len(env.broker.sent) == 1  # no top-up


# =========================================== 14: failed / uncertain results
def test_timeout_after_execution_is_reconciled_not_resent(tmp_path):
    env = make_env(tmp_path, live=True)
    env.broker.behaviors.append("timeout_executed")  # broker DID create the order, answer lost
    plan = add_plan(env)
    res = env.system.engine.process_plan(plan)
    assert res.outcome is Outcome.ALREADY and len(env.broker.orders) == 1
    assert env.system.engine.process_plan(plan).outcome is Outcome.ALREADY
    assert len(env.broker.sent) == 1


def test_uncertain_code_with_execution_is_reconciled(tmp_path):
    env = make_env(tmp_path, live=True)
    env.broker.behaviors.append("code_executed")  # retcode 10012 (timeout) but order exists
    res = env.system.engine.process_plan(add_plan(env))
    assert res.outcome is Outcome.ALREADY and len(env.broker.sent) == 1


def test_timeout_without_execution_waits_then_retries_once_reconciled(tmp_path):
    env = make_env(tmp_path, live=True)
    env.broker.behaviors.append("timeout_not_executed")
    plan = add_plan(env)
    assert env.system.engine.process_plan(plan).outcome is Outcome.UNCERTAIN
    waiting = env.system.engine.process_plan(plan)
    assert waiting.outcome is Outcome.AWAITING and len(env.broker.sent) == 1  # NOT blindly resent
    env.broker.advance(31)
    retry = env.system.engine.process_plan(plan)  # still nothing at the broker after the hold -> safe to retry
    assert retry.outcome is Outcome.PENDING_PLACED and len(env.broker.sent) == 2 and len(env.broker.orders) == 1


def test_requote_is_retryable_but_rejection_is_final(tmp_path):
    env = make_env(tmp_path, live=True)
    env.broker.behaviors.extend(["requote"])
    plan = add_plan(env)
    assert env.system.engine.process_plan(plan).outcome is Outcome.FAILED
    assert env.system.engine.process_plan(plan).outcome is Outcome.PENDING_PLACED  # revalidated, second attempt
    env2 = make_env(tmp_path / "two", live=True)
    env2.broker.behaviors.append("reject")
    p2 = add_plan(env2)
    assert env2.system.engine.process_plan(p2).outcome is Outcome.FAILED
    again = env2.system.engine.process_plan(p2)
    assert again.outcome is Outcome.REJECTED and "broker rejected earlier" in why(again)
    assert len(env2.broker.sent) == 1


def test_attempts_are_capped(tmp_path):
    env = make_env(tmp_path, live=True)
    env.broker.behaviors.extend(["requote", "requote"])
    plan = add_plan(env)
    env.system.engine.process_plan(plan)
    env.system.engine.process_plan(plan)
    third = env.system.engine.process_plan(plan)
    assert third.outcome is Outcome.REJECTED and "max send attempts" in why(third)
    assert len(env.broker.sent) == 2


def test_order_check_failure_blocks_the_send(tmp_path):
    env = make_env(tmp_path, live=True)
    env.broker.check_retcode = 10019  # no money
    res = env.system.engine.process_plan(add_plan(env))
    assert res.outcome is Outcome.REJECTED and "order_check refused" in why(res) and env.broker.sent == []


def test_unreadable_positions_fail_closed(tmp_path):
    env = make_env(tmp_path, live=True)
    env.broker.fail_positions = True
    env.broker.last_error = lambda: (-2, "terminal error")
    res = env.system.engine.process_plan(add_plan(env))
    assert res.outcome is Outcome.REJECTED and "failing closed" in why(res) and env.broker.sent == []
