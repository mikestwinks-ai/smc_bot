"""Phase 4 - partial close, break-even, structural trailing, pending monitor, end-of-day flatten."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from smc.config import AssetClass, SymbolConfig
from smc.core.timeframes import Timeframe
from smc.execution import TradeManager
from smc.execution.execution_engine import Outcome
from smc.execution.fills import FillRegistrar
from smc.execution.trade_manager import TradeManager as TM
from smc.models.plan import PlanStatus, PriceRange
from smc.risk.models import SymbolSpec
from smc.structure.models import Bias, StructureEventType, SwingPoint, SwingType
from tests.fake_broker import A_DEAL, A_REMOVE, A_SLTP
from tests.harness import BSYM, SYM, add_plan, make_env
from tests.test_phase4_risk import spec

M15 = Timeframe.M15


def open_long(env, set_price: bool = True):
    """Live market entry at 115.05, stop 112 (R = 3.05), 0.08 lots."""
    env.broker.set_price(115.0, 115.05)
    plan = add_plan(env, status=PlanStatus.READY_TO_EXECUTE)
    res = env.system.engine.process_plan(plan)
    assert res.outcome is Outcome.FILLED and res.volume == pytest.approx(0.08)
    return plan, env.broker.positions[0]


def closes(env):  # closing deals sent so far
    return [r for r in env.broker.sent if r["action"] == A_DEAL and "position" in r]


def sltps(env):
    return [r for r in env.broker.sent if r["action"] == A_SLTP]


# ===================================================== 15: partial close volume
@pytest.mark.parametrize("volume,close", [
    (0.08, 0.04), (0.03, 0.01), (0.05, 0.02), (0.02, 0.01), (0.01, None), (0.0, None)])
def test_partial_volume_respects_min_and_step(volume, close):
    assert TM._partial_volume(volume, 0.5, spec()) == close


def test_partial_volume_on_coarse_grids():
    s = spec(volume_min=0.1, volume_step=0.1)
    assert TM._partial_volume(0.3, 0.5, s) == 0.1 and TM._partial_volume(0.2, 0.5, s) == 0.1
    assert TM._partial_volume(0.1, 0.5, s) is None
    assert TM._partial_volume(0.7, 0.5, s) == 0.3  # 0.35 -> 0.3, remainder 0.4 is legal
    assert TM._partial_volume(0.3, 0.9, s) == 0.2  # 0.27 -> 0.2 ; remainder 0.1 >= min


def test_partial_close_happens_at_one_and_a_half_r_and_only_once(tmp_path):
    env = make_env(tmp_path, live=True)
    plan, pos = open_long(env)
    env.broker.set_price(119.0, 119.05)  # 1.29R: nothing yet
    assert env.system.manager.run() == [] and closes(env) == []
    env.broker.set_price(120.0, 120.05)  # 1.62R
    acts = env.system.manager.run()
    assert [a.kind for a in acts] == ["partial", "breakeven"] and all(a.ok for a in acts)
    assert len(closes(env)) == 1 and closes(env)[0]["volume"] == pytest.approx(0.04)
    assert env.broker.positions[0].volume == pytest.approx(0.04)
    for _ in range(3):
        env.system.manager.run()
    assert len(closes(env)) == 1  # idempotent across cycles
    env.restart()
    env.system.manager.run()
    assert len(closes(env)) == 1  # ... and across restarts


def test_partial_already_done_is_recognised_from_volume_after_a_crash(tmp_path):
    env = make_env(tmp_path, live=True)
    plan, pos = open_long(env)
    env.broker.set_price(120.0, 120.05)
    env.system.manager.run()  # partial + BE done
    mp = env.system.state.get_managed(pos.ticket)
    mp.partial_done = mp.be_done = False  # simulate a crash that lost our bookkeeping
    env.system.state.save_managed(mp)
    env.system.state.set_intent(mp.key + "|P1", status="sending")  # ... mid-send
    n = len(env.broker.sent)
    env.restart()
    env.system.manager.run()
    assert len(closes(env)) == 1 and env.system.state.get_managed(pos.ticket).partial_done
    assert len(env.broker.sent) == n or len(sltps(env)) <= 2  # no second partial under any circumstance


def test_impossible_partial_is_skipped_safely_and_logged(tmp_path):
    env = make_env(tmp_path, live=True, balance=2_000.0)
    env.broker.set_price(115.0, 115.05)
    plan = add_plan(env, status=PlanStatus.READY_TO_EXECUTE, sl=110.0, targets=[150.2])  # risk $10, raw 0.0198 lot -> floored to 0.01
    res = env.system.engine.process_plan(plan)
    assert res.outcome is Outcome.FILLED and res.volume == 0.01
    env.broker.set_price(125.0, 125.05)  # 1.9R
    acts = env.system.manager.run()
    part = next(a for a in acts if a.kind == "partial")
    assert not part.ok and "partial close impossible" in part.note and closes(env) == []
    assert env.broker.positions[0].volume == 0.01
    assert env.broker.positions[0].sl == 115.05  # configured fallback: break-even without the partial
    env2 = make_env(tmp_path / "b", live=True, balance=2_000.0)
    env2.cfg.execution.breakeven_if_partial_impossible = False
    env2.broker.set_price(115.0, 115.05)
    env2.system.engine.process_plan(add_plan(env2, status=PlanStatus.READY_TO_EXECUTE, sl=110.0))
    env2.broker.set_price(125.0, 125.05)
    env2.system.manager.run()
    assert env2.broker.positions[0].sl == 110.0 and sltps(env2) == []


# =================================================== 16: BE only after the partial
def test_break_even_waits_for_a_confirmed_partial(tmp_path):
    env = make_env(tmp_path, live=True)
    plan, pos = open_long(env)
    env.broker.set_price(120.0, 120.05)
    env.broker.behaviors.append("timeout_not_executed")  # the partial close gets no answer and does NOT happen
    acts = env.system.manager.run()
    assert [a.kind for a in acts] == ["partial"] and not acts[0].ok
    assert env.broker.positions[0].sl == 112.0 and sltps(env) == []  # BE must not move yet
    assert env.system.state.get_managed(pos.ticket).partial_done is False
    assert env.system.manager.run() == [] or sltps(env) == []  # still waiting, nothing resent
    assert len(closes(env)) == 1
    env.broker.advance(31)  # hold expired, MT5 shows the volume unchanged -> safe to retry
    acts = env.system.manager.run()
    assert [a.kind for a in acts] == ["partial", "breakeven"] and all(a.ok for a in acts)
    assert env.broker.positions[0].sl == 115.05
    order = [("close" if "position" in r and r["action"] == A_DEAL else "sltp") for r in env.broker.sent[1:]]
    assert order == ["close", "close", "sltp"]  # the SL change came strictly AFTER the confirmed partial


def test_rejected_partial_leaves_the_stop_alone(tmp_path):
    env = make_env(tmp_path, live=True)
    open_long(env)
    env.broker.set_price(120.0, 120.05)
    env.broker.behaviors.append("reject")
    acts = env.system.manager.run()
    assert not acts[0].ok and env.broker.positions[0].sl == 112.0 and sltps(env) == []


def test_break_even_includes_commission_and_is_never_looser_than_entry(tmp_path):
    env = make_env(tmp_path, live=True, commission_per_lot=7.0)  # $7 per lot per side
    open_long(env)
    env.broker.set_price(120.0, 120.05)
    env.system.manager.run()
    # remaining 0.04 lots: entry share 0.04*7*2 = $0.56 -> /(0.04*100 per price unit) = 0.14 above entry
    assert env.broker.positions[0].sl == pytest.approx(115.19)
    assert env.broker.positions[0].sl > 115.05


def test_break_even_is_postponed_when_price_has_no_room(tmp_path):
    env = make_env(tmp_path, live=True)
    plan, pos = open_long(env)
    env.broker.set_price(120.0, 120.05)
    mgr, spec_ = env.system.manager, env.system.gateway.symbol_spec(BSYM)
    env.broker.symbols[BSYM].trade_stops_level = 600  # 6.00 minimum distance: BE 115.05 is 4.95 from price
    env.system.manager.run()
    assert env.broker.positions[0].sl == 112.0  # partial done, but BE refused by the broker rule
    env.broker.symbols[BSYM].trade_stops_level = 10
    env.system.manager.run()
    assert env.broker.positions[0].sl == 115.05


# =============================================== 17: structural trailing, stubs
class StubBuffer:
    def __init__(self, last_open): self.last_index, self._t = 100, last_open
    def __len__(self): return 101
    def time(self, i): return self._t


class StubTracker:
    def __init__(self, last_open, swings, events, atr=1.0):
        self.buffer, self.swings, self.events, self.atr = StubBuffer(last_open), swings, events, atr


class StubView:
    def __init__(self, tracker): self._t = tracker
    def tracker(self, symbol, tf): return self._t


def swing_low(price, at, confirmed):
    return SwingPoint(SwingType.LOW, price, at, 90, confirmed, M15)


def bos_bullish(at):
    return SimpleNamespace(direction=Bias.BULLISH, type=StructureEventType.BOS, time=at)


def trail_env(tmp_path, sl_now=115.05, bid=130.0):
    env = make_env(tmp_path, live=True)
    plan, pos = open_long(env)
    mp = env.system.state.get_managed(pos.ticket)
    mp.partial_done = mp.be_done = True  # trailing is independent of the partial; keep the test focused
    env.system.state.save_managed(mp)
    env.broker.positions[0].sl = sl_now
    env.broker.set_price(bid, bid + 0.05)
    return env, pos


def trailing_manager(env, tracker):
    s = env.system
    return TradeManager(env.cfg, s.gateway, s.plans, s.state, s.clock, s.kill, StubView(tracker), s.registrar)


def after(env, minutes):
    return env.now + pd.Timedelta(minutes=minutes)


def test_structural_trail_moves_behind_a_new_protected_swing(tmp_path):
    env, pos = trail_env(tmp_path)
    tr = StubTracker(env.now - pd.Timedelta(minutes=15), [swing_low(120.0, after(env, 1), after(env, 6))],
                     [bos_bullish(after(env, 8))])
    acts = trailing_manager(env, tr).run()
    t = next(a for a in acts if a.kind == "trail")
    assert t.ok and env.broker.positions[0].sl == pytest.approx(119.9)  # swing - 0.1 ATR buffer


@pytest.mark.parametrize("swing_price,sl_now,why", [
    (110.0, 115.05, "swing below current stop"),
    (120.0, 125.0, "would widen the stop"),
    (120.0, 119.9, "same level"),
])
def test_structural_trail_never_widens_the_stop(tmp_path, swing_price, sl_now, why):
    env, _ = trail_env(tmp_path, sl_now=sl_now)
    tr = StubTracker(env.now - pd.Timedelta(minutes=15), [swing_low(swing_price, after(env, 1), after(env, 6))],
                     [bos_bullish(after(env, 8))])
    trailing_manager(env, tr).run()
    assert env.broker.positions[0].sl == sl_now and sltps(env) == [], why


def test_trail_needs_a_swing_formed_after_entry_and_proven_by_a_bos(tmp_path):
    env, _ = trail_env(tmp_path)
    old = swing_low(120.0, after(env, -120), after(env, -100))
    unproven = swing_low(121.0, after(env, 1), after(env, 6))
    tr = StubTracker(env.now - pd.Timedelta(minutes=15), [old, unproven], [])
    trailing_manager(env, tr).run()
    assert env.broker.positions[0].sl == 115.05  # swing before entry / swing without a BOS: no trail
    tr2 = StubTracker(env.now - pd.Timedelta(minutes=15), [old], [bos_bullish(after(env, 8))])
    trailing_manager(env, tr2).run()
    assert env.broker.positions[0].sl == 115.05


def test_trail_is_skipped_when_structure_is_stale_or_missing(tmp_path):
    env, _ = trail_env(tmp_path)
    tr = StubTracker(env.now - pd.Timedelta(hours=5), [swing_low(120.0, after(env, 1), after(env, 6))],
                     [bos_bullish(after(env, 8))])
    acts = trailing_manager(env, tr).run()
    assert env.broker.positions[0].sl == 115.05
    assert any("stale" in a.note for a in acts if a.kind == "trail")
    s = env.system
    none_view = TradeManager(env.cfg, s.gateway, s.plans, s.state, s.clock, s.kill, None, s.registrar)
    assert any("unavailable" in a.note for a in none_view.run() if a.kind == "trail")
    assert env.broker.positions[0].sl == 115.05


def test_trail_respects_broker_stop_distance(tmp_path):
    env, _ = trail_env(tmp_path, bid=120.3)  # swing 120 -> SL 119.9 is only 0.4 from price
    env.broker.symbols[BSYM].trade_stops_level = 100  # 1.00
    tr = StubTracker(env.now - pd.Timedelta(minutes=15), [swing_low(120.0, after(env, 1), after(env, 6))],
                     [bos_bullish(after(env, 8))])
    trailing_manager(env, tr).run()
    assert env.broker.positions[0].sl == 115.05


def test_there_is_no_fixed_pip_trailing_setting():
    from smc.config import TradeManagementConfig
    assert not [f for f in vars(TradeManagementConfig()) if "pip" in f.lower()]


# ============================================ 18: pending orders follow plans
def place_pending(tmp_path, **kw):
    env = make_env(tmp_path, live=True, **kw)
    plan = add_plan(env)
    assert env.system.engine.process_plan(plan).outcome is Outcome.PENDING_PLACED
    return env, plan


def removed(env):
    return [r for r in env.broker.sent if r["action"] == A_REMOVE]


@pytest.mark.parametrize("mutate", [
    lambda e, p: e.mgr.store.cancel(p.plan_id, "invalidated: body close below stop", e.now),
    lambda e, p: e.mgr.store.cancel(p.plan_id, "expired: 97 bars without the zone being reached", e.now),
    lambda e, p: e.mgr.store.flip(p.plan_id, "zone broken by body close", e.now),
    lambda e, p: e.mgr.store._plans.pop(p.plan_id),
    lambda e, p: setattr(p, "entry_zone", PriceRange(118.0, 116.5)),
    lambda e, p: e.system.kill.engage("test"),
    lambda e, p: e.broker.advance(13 * 3600),
], ids=["canceled", "expired", "flipped", "orphan", "changed", "kill-switch", "session-over"])
def test_pending_orders_are_canceled_when_their_plan_stops_being_valid(tmp_path, mutate):
    env, plan = place_pending(tmp_path)
    mutate(env, plan)
    acts = env.system.manager.run()
    assert env.broker.orders == [] and len(removed(env)) == 1
    assert [a.kind for a in acts] == ["cancel"] and acts[0].ok


def test_valid_pending_orders_are_left_alone_and_dry_run_never_removes(tmp_path):
    env, plan = place_pending(tmp_path)
    assert env.system.manager.run() == [] and len(env.broker.orders) == 1
    dry = make_env(tmp_path / "dry")
    dry.broker._execute({"action": 5, "type": 2, "magic": dry.cfg.execution.magic_number, "volume": 0.05,
                         "price": 116.0, "sl": 112.0, "tp": 150.0, "symbol": BSYM, "comment": "PLAN-GONE|010000"})
    acts = dry.system.manager.run()
    assert acts and not acts[0].ok and "dry-run" in acts[0].note
    assert dry.broker.sent == [] and len(dry.broker.orders) == 1  # reported, but nothing sent


def test_cancel_inside_the_freeze_level_is_deferred(tmp_path):
    env, plan = place_pending(tmp_path)
    env.broker.symbols[BSYM].trade_freeze_level = 500
    env.mgr.store.cancel(plan.plan_id, "invalidated", env.now)
    acts = env.system.manager.run()
    assert not acts[0].ok and "freeze" in acts[0].note and len(env.broker.orders) == 1
    env.broker.symbols[BSYM].trade_freeze_level = 0
    assert env.system.manager.run()[0].ok and env.broker.orders == []


def test_cancel_with_lost_answer_is_verified_against_mt5(tmp_path):
    env, plan = place_pending(tmp_path)
    env.mgr.store.cancel(plan.plan_id, "invalidated", env.now)
    env.broker.behaviors.append("timeout_executed")  # order is removed but the reply is lost
    acts = env.system.manager.run()
    assert acts[0].ok and "verified" in acts[0].note and env.broker.orders == []


# ================================================ 20: 23:00 flatten closes exposure
def test_positions_are_closed_at_the_2300_deadline_even_after_a_restart(tmp_path):
    env = make_env(tmp_path, live=True)
    plan, pos = open_long(env)
    mp = env.system.state.get_managed(pos.ticket)
    assert mp.deadline == "2026-01-07T23:00:00+00:00"
    env.broker.advance(12 * 3600 + 59 * 60)  # 22:59
    env.broker.set_price(118.0, 118.05)
    env.system.manager.run()
    assert len(env.broker.positions) == 1
    env.broker.advance(120)  # 23:01 - and the bot was "down" until now: a fresh process picks it up
    env.restart()
    acts = env.system.manager.run()
    assert env.broker.positions == [] and any(a.kind == "flatten" and a.ok for a in acts)
    assert closes(env)[-1]["volume"] == pytest.approx(0.08) and closes(env)[-1]["comment"].endswith("|EOD")


def test_unknown_positions_with_a_plan_comment_are_adopted_and_flattened(tmp_path):
    env = make_env(tmp_path, live=True, now="2026-01-07 21:00")
    env.broker.add_open_position(BSYM, True, 0.05, 116.0, 112.0, 150.0, env.cfg.execution.magic_number,
                                 comment="PLAN-OLD|010000", opened_ago_min=30)
    env.system.manager.run()
    mp = env.system.state.get_managed(env.broker.positions[0].ticket)
    assert mp.adopted and mp.deadline == "2026-01-07T23:00:00+00:00" and mp.initial_sl == 112.0
    env.broker.advance(2 * 3600 + 60)
    env.system.manager.run()
    assert env.broker.positions == []


def test_crypto_exemption_comes_from_the_existing_config_only(tmp_path):
    env = make_env(tmp_path)
    clock, t = env.system.clock, pd.Timestamp("2026-01-07 10:00", tz="UTC")
    btc, gold = SymbolConfig("BTCUSD", AssetClass.CRYPTO), SymbolConfig("XAUUSD", AssetClass.METAL)
    assert clock.close_deadline(t, btc) is None and clock.close_deadline(t, gold) is not None
    assert not clock.after_no_new_trades(pd.Timestamp("2026-01-07 23:10", tz="UTC"), btc)
    env.cfg.sessions.crypto_exempt_from_cutoffs = False
    assert clock.close_deadline(t, btc) is not None


def test_pending_orders_are_canceled_after_the_2230_cutoff(tmp_path):
    env, plan = place_pending(tmp_path)
    env.broker.advance(12 * 3600 + 31 * 60)  # 22:31 UTC
    acts = env.system.manager.run()
    assert acts[0].ok and "entry window closed" in acts[0].note and env.broker.orders == []
