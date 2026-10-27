"""Phase 4 - risk sizing, daily limits, exposure counting."""

from __future__ import annotations

import pandas as pd
import pytest

from smc.config import BotConfig, ConfigError
from smc.execution.execution_engine import Outcome
from smc.models.plan import EntryModel, PlanDirection
from smc.risk import RiskManager, compute_volume, floor_to_grid, fold_outcomes
from smc.risk.models import AccountState, DealInfo, SymbolSpec
from tests.fake_broker import TYPE_BUY
from tests.harness import BSYM, add_plan, make_env

T = pd.Timestamp("2026-01-07 10:00", tz="UTC")


def spec(**kw) -> SymbolSpec:
    d = dict(name="X", digits=2, point=0.01, tick_size=0.01, tick_value_profit=1.0, tick_value_loss=1.0,
             contract_size=100.0, volume_min=0.01, volume_max=100.0, volume_step=0.01, stops_level_points=10,
             freeze_level_points=0, trade_mode=4, filling_flags=3, expiration_flags=15, fetched_at=T)
    d.update(kw)
    return SymbolSpec(**d)


def mgr_for(tmp_path, **kw):
    env = make_env(tmp_path, **kw)
    return env, env.system.risk


# ================================================== 1 & 2: risk percentage
@pytest.mark.parametrize("equity,pct", [(500.0, 0.5), (9_999.99, 0.5), (10_000.0, 1.0), (50_000.0, 1.0)])
def test_risk_pct_by_account_equity(tmp_path, equity, pct):
    _, rm = mgr_for(tmp_path)
    assert rm.risk_pct(equity) == pct


def test_hard_ceiling_is_enforced_in_config_and_in_code(tmp_path):
    cfg = BotConfig()
    cfg.risk.risk_pct_at_or_above_threshold = 1.5
    with pytest.raises(ConfigError):
        cfg.validate()
    env, rm = mgr_for(tmp_path)
    rm._cfg.risk.risk_pct_at_or_above_threshold = 3.0  # bypass validate(): the clamp still holds
    rm._cfg.risk.hard_max_risk_pct = 3.0
    assert rm.risk_pct(20_000) == 1.0


def test_sizing_uses_equity_and_actual_stop_distance(tmp_path):
    env, rm = mgr_for(tmp_path)
    acct = env.system.gateway.account()
    dec = rm.evaluate(account=acct, spec=env.system.gateway.symbol_spec(BSYM), broker_symbol=BSYM, is_buy=True,
                      entry=116.0, sl=112.0, now=env.now)
    assert dec.approved and dec.sizing.risk_pct == 0.5 and dec.sizing.risk_amount == pytest.approx(25.0)
    assert dec.sizing.volume == 0.06 and dec.sizing.actual_risk == pytest.approx(24.0)  # rounded DOWN
    env2, rm2 = mgr_for(tmp_path / "b", balance=20_000.0)
    dec2 = rm2.evaluate(account=env2.system.gateway.account(), spec=env2.system.gateway.symbol_spec(BSYM),
                        broker_symbol=BSYM, is_buy=True, entry=116.0, sl=112.0, now=env2.now)
    assert dec2.sizing.risk_pct == 1.0 and dec2.sizing.volume == 0.5  # $200 / $400 per lot


# ================================== 3: symbol-specific tick values and steps
def test_volume_respects_symbol_tick_value_and_step():
    eurusd = spec(tick_size=0.00001, tick_value_loss=1.0, point=0.00001, digits=5, contract_size=100_000)
    s, why = compute_volume(10_000, 1.0, 0.0020, eurusd)  # 200 ticks -> $200/lot, $100 budget
    assert why is None and s.volume == 0.5 and s.loss_per_lot == pytest.approx(200.0)
    usdjpy = spec(tick_size=0.001, tick_value_loss=0.6667, point=0.001, digits=3)
    s, _ = compute_volume(5_000, 0.5, 0.30, usdjpy)  # 300 ticks * 0.6667 = $200/lot, $25 budget
    assert s.volume == 0.12 and s.actual_risk <= 25.0
    index = spec(tick_size=0.1, tick_value_loss=0.1, volume_min=0.1, volume_step=0.1, point=0.1, digits=1)
    s, _ = compute_volume(5_000, 0.5, 30.0, index)  # 300 ticks * 0.1 = $30/lot, $25 budget -> 0.83 -> 0.8
    assert s.volume == 0.8
    capped = spec(volume_max=0.3)
    s, _ = compute_volume(100_000, 1.0, 1.0, capped)
    assert s.volume == 0.3  # never above the broker maximum


def test_volume_grid_never_rounds_up():
    assert floor_to_grid(0.0999, 0.01, 0.01) == 0.09
    assert floor_to_grid(0.57, 0.1, 0.1) == 0.5
    assert floor_to_grid(0.3, 0.01, 0.01) == 0.3  # exact multiples stay exact (no float drift)


# ================================================ 4: minimum lot > budget
def test_minimum_lot_exceeding_risk_budget_is_rejected_not_enlarged():
    s, why = compute_volume(1_000, 0.5, 10.0, spec())  # $5 budget, $1000 per lot, min lot risks $10
    assert s is None and "minimum lot" in why and "rejected, not enlarged" in why


def test_missing_or_invalid_symbol_data_is_rejected():
    for bad in (spec(tick_size=0.0), spec(tick_value_loss=0.0), spec(volume_step=0.0), spec(volume_max=0.001)):
        s, why = compute_volume(10_000, 1.0, 1.0, bad)
        assert s is None and why
    s, why = compute_volume(-5.0, 1.0, 1.0, spec())
    assert s is None


def test_stale_or_invalid_account_data_is_rejected(tmp_path):
    env, rm = mgr_for(tmp_path)
    old = AccountState(1, "USD", 5000, 5000, 5000, 500, True, False, env.now - pd.Timedelta(minutes=5))
    assert any("stale" in p for p in rm.account_problems(old, env.now))
    broke = AccountState(1, "", 0, 0, 0, 500, True, False, env.now)
    probs = rm.account_problems(broke, env.now)
    assert any("currency" in p for p in probs) and any("not positive" in p for p in probs)


# ======================================== 7: daily drawdown (floating vs realised)
def test_daily_drawdown_blocks_new_trades_floating(tmp_path):
    env, _ = mgr_for(tmp_path)
    plan = add_plan(env)
    assert env.system.engine.process_plan(plan).outcome is Outcome.DRY_RUN  # baseline stored: 5000
    env.broker.equity_override = 4_890.0  # -2.2% floating
    res = env.system.engine.process_plan(plan)
    assert res.outcome is Outcome.REJECTED
    assert "daily drawdown limit" in " ".join(res.reasons) and "floating 2.20%" in " ".join(res.reasons)
    env.broker.equity_override = 4_950.0  # -1% : fine again
    assert env.system.engine.process_plan(plan).outcome is Outcome.DRY_RUN


def test_daily_drawdown_blocks_on_realised_loss_even_if_floating_profit_hides_it(tmp_path):
    env = make_env(tmp_path, balance=4_890.0)
    env.broker.add_closed_trade(-110.0, 60, env.cfg.execution.magic_number)  # baseline = 4890 + 110 = 5000
    env.broker.equity_override = 5_000.0  # floating profit masks the loss completely
    snap = env.system.exposure.snapshot(env.system.gateway.account(), env.now)
    assert snap.baseline_balance == pytest.approx(5_000.0)
    assert snap.realised_loss_pct == pytest.approx(2.2) and snap.total_drawdown_pct == 0.0
    res = env.system.engine.process_plan(add_plan(env))
    assert res.outcome is Outcome.REJECTED and "daily drawdown limit" in " ".join(res.reasons)


def test_baseline_survives_restart_and_resets_only_at_day_boundary(tmp_path):
    env = make_env(tmp_path, now="2026-01-07 23:50")
    gw, clock = env.system.gateway, env.system.clock
    env.system.exposure.snapshot(gw.account(), env.now)
    assert env.system.state.get_baseline("2026-01-07") == 5000.0
    env.broker.balance = 4_000.0
    env.restart()
    snap = env.system.exposure.snapshot(gw.account(), env.now)
    assert snap.baseline_balance == 5000.0  # restart did not re-baseline at the lower balance
    env.broker.advance(15 * 60)  # 00:05 next UTC day
    assert clock.trading_day_key(env.now) == "2026-01-08"
    assert env.system.exposure.snapshot(gw.account(), env.now).baseline_balance == pytest.approx(4_000.0)


# ===================================================== 8 & 9: streak, day count
def test_two_consecutive_losses_block_new_trades(tmp_path):
    env = make_env(tmp_path)
    magic = env.cfg.execution.magic_number
    env.broker.add_closed_trade(-10.0, 120, magic)
    plan = add_plan(env)
    assert env.system.engine.process_plan(plan).outcome is Outcome.DRY_RUN  # one loss: fine
    env.broker.add_closed_trade(-12.0, 60, magic)
    res = env.system.engine.process_plan(plan)
    assert res.outcome is Outcome.REJECTED and "2 consecutive losses" in " ".join(res.reasons)


def test_loss_streak_counting_rules(tmp_path):
    env = make_env(tmp_path)
    magic, snap = env.cfg.execution.magic_number, lambda: env.system.exposure.snapshot(env.system.gateway.account(), env.now)
    env.broker.add_closed_trade(-5.0, 300, magic)
    env.broker.add_closed_trade(+8.0, 240, magic)
    env.broker.add_closed_trade(-5.0, 120, magic)
    assert snap().consecutive_losses == 1  # a win in between resets the streak
    env.broker.add_closed_trade(0.0, 60, magic)  # break-even is not a loss
    assert snap().consecutive_losses == 0


def test_partials_and_costs_are_folded_into_one_trade_outcome():
    mk = lambda ticket, entry, vol, profit, comm, t: DealInfo(  # noqa: E731
        ticket, ticket, 77, T + pd.Timedelta(minutes=t), entry, not entry, True, vol, 100.0, comm, -0.5 if not entry else 0.0,
        profit, 0.0, 1, "XAUUSDm", "k")
    deals = [mk(1, True, 0.10, 0.0, -1.0, 0), mk(2, False, 0.05, +12.0, -0.5, 10), mk(3, False, 0.05, -14.0, -0.5, 20)]
    out = fold_outcomes(deals)
    assert len(out) == 1 and out[0].net == pytest.approx(-1.0 + 12 - 0.5 - 14 - 0.5 - 0.5 - 0.5)  # ONE trade, net negative
    assert out[0].is_loss  # +12 partial win does not make it two outcomes
    still_open = fold_outcomes(deals[:2])
    assert still_open == []  # a partially closed position is not a closed trade yet


def test_three_trades_in_one_day_block_additional_entries(tmp_path):
    env = make_env(tmp_path)
    magic = env.cfg.execution.magic_number
    for i in range(3):
        env.broker.add_closed_trade(+20.0, 300 - i * 60, magic)
    res = env.system.engine.process_plan(add_plan(env))
    assert res.outcome is Outcome.REJECTED and "3 trades already opened this trading day" in " ".join(res.reasons)


def test_yesterdays_trades_do_not_count_today(tmp_path):
    env = make_env(tmp_path, now="2026-01-07 10:00")
    for i in range(3):
        env.broker.add_closed_trade(-5.0, 14 * 60 + i * 30, env.cfg.execution.magic_number)  # ~20:00 the day before
    res = env.system.engine.process_plan(add_plan(env))
    assert res.outcome is Outcome.DRY_RUN


# ===================================================== 10 & 11: open exposure
def test_two_open_positions_across_the_bot_block_a_third(tmp_path):
    env = make_env(tmp_path)
    magic = env.cfg.execution.magic_number
    env.broker.add_open_position("EURUSDm", True, 0.1, 1.1, 1.09, 1.12, magic)
    env.broker.add_open_position("GBPUSDm", False, 0.1, 1.3, 1.31, 1.28, magic)
    res = env.system.engine.process_plan(add_plan(env))
    assert res.outcome is Outcome.REJECTED and "limit 2" in " ".join(res.reasons)


def test_pending_orders_count_as_open_exposure(tmp_path):
    env = make_env(tmp_path, live=True)
    first = add_plan(env)
    assert env.system.engine.process_plan(first).outcome is Outcome.PENDING_PLACED
    snap = env.system.exposure.snapshot(env.system.gateway.account(), env.now)
    assert snap.open_count == 1 and snap.trades_opened_today == 1


def test_same_direction_on_same_symbol_is_rejected_but_opposite_is_allowed(tmp_path):
    env = make_env(tmp_path)
    env.broker.add_open_position(BSYM, True, 0.1, 116.0, 112.0, 150.0, env.cfg.execution.magic_number)
    long_res = env.system.engine.process_plan(add_plan(env))
    assert long_res.outcome is Outcome.REJECTED and "already exposed long" in " ".join(long_res.reasons)
    short = add_plan(env, direction=PlanDirection.SHORT)
    assert env.system.engine.process_plan(short).outcome is Outcome.DRY_RUN


def test_manual_trades_with_other_magic_numbers_are_not_counted(tmp_path):
    env = make_env(tmp_path)
    env.broker.add_open_position(BSYM, True, 0.1, 116.0, 112.0, 150.0, magic=999)
    env.broker.add_closed_trade(-50.0, 90, magic=999)
    assert env.system.engine.process_plan(add_plan(env)).outcome is Outcome.DRY_RUN
