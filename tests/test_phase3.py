"""Phase 3 tests - hand-built candles / contexts, no MT5 needed:  pytest -q"""

from __future__ import annotations

import pandas as pd
import pytest

from smc.config import BotConfig
from smc.core.timeframes import StructureLayer, Timeframe
from smc.dashboard.snapshot import _plan
from smc.models.plan import (
    EntryModel, EntryRangeBasis, Invalidation, Plan, PlanDirection, PlanStatus, PlanZones, PriceRange,
    SourceTimeframes,
)
from smc.planning import ChangeKind, PlanManager, PlanStore
from smc.planning.entry_models import Candidate, Discard, EntryModelEvaluators, EvalContext
from smc.structure import Bias, StructureEventType, StructureHub
from smc.structure.models import (
    FairValueGap, LiquidityKind, LiquiditySide, LiquiditySweep, OrderBlock, PDReading, PDState,
    StructureEvent, ZoneKind, ZoneStatus,
)
from tests.synth import T0, frame, zigzag

SYM, M15 = "XAUUSD", Timeframe.M15
EV = EntryModelEvaluators()


# =============================================================== builders
def make_ctx(external_bias=Bias.NEUTRAL, internal_bias=Bias.NEUTRAL, low=100.0, high=110.0,
             targets=(112.0,), atr=1.0, layer=StructureLayer.INTERNAL, tf=M15, overlap=0.0) -> EvalContext:
    cfg = BotConfig()
    tgt = list(targets)
    pdr = PDReading(layer, tf, high, low, (high + low) / 2, 0.5, PDState.EQUILIBRIUM)
    return EvalContext(
        symbol=SYM, timeframe=tf, layer=layer, atr=atr, now=T0,
        external_bias=external_bias, internal_bias=internal_bias,
        external_pd=pdr if layer is StructureLayer.EXTERNAL else None,
        internal_pd=pdr if layer is StructureLayer.INTERNAL else None,
        structure_cfg=cfg.structure, planning_cfg=cfg.planning,
        find_targets=lambda d, entry: [t for t in tgt if (t > entry if d is PlanDirection.LONG else t < entry)],
        leg_overlap=lambda ob: overlap,
    )


def make_ob(bias=Bias.BULLISH, top=102.0, bottom=101.0, status=ZoneStatus.ACTIVE, ratio=2.0,
            ev_type=StructureEventType.BOS, has_fvg=False, idx=50, leg_start_idx=None,
            symbol=SYM, timeframe=M15) -> OrderBlock:
    return OrderBlock(id=f"OB-{bias.tag}-{bottom}", kind=ZoneKind.ORDER_BLOCK, symbol=symbol,
                      timeframe=timeframe,
                      bias=bias, top=top, bottom=bottom, created_time=T0, created_index=idx, status=status,
                      candle_time=T0, candle_index=idx - 3,
                      leg_start_index=idx - 5 if leg_start_idx is None else leg_start_idx,
                      origin_event_id="EV", origin_event_type=ev_type,
                      has_fvg=has_fvg, displacement_ratio=ratio)


def make_fvg(bias=Bias.BULLISH, top=102.0, bottom=101.0, stop_ref=99.0, idx=50,
             symbol=SYM, timeframe=M15) -> FairValueGap:
    return FairValueGap(id=f"FVG-{bias.tag}-{bottom}", kind=ZoneKind.FVG, symbol=symbol,
                        timeframe=timeframe, bias=bias,
                        top=top, bottom=bottom, created_time=T0, created_index=idx, gap_size=top - bottom,
                        stop_reference=stop_ref)


# =========================================== (a) FVG with RR < 2 is discarded
def test_a_fvg_with_rr_below_two_is_discarded():
    fvg = make_fvg()  # entry 102 (near edge), stop 99 - 0.1 = 98.9 -> risk 3.1
    res = EV.evaluate_fvg(make_ctx(targets=[105.0]), fvg)  # reward 3.0 -> RR 0.97
    assert isinstance(res, Discard) and "RR" in res.reason and "2.0" in res.reason and fvg.id in res.reason


def test_a_fvg_just_under_and_over_the_hard_minimum():
    fvg = make_fvg()
    under = EV.evaluate_fvg(make_ctx(targets=[102 + 2 * 3.1 - 0.2]), fvg)  # RR 1.94
    over = EV.evaluate_fvg(make_ctx(targets=[102 + 2 * 3.1 + 0.2]), fvg)   # RR 2.06
    assert isinstance(under, Discard)
    assert isinstance(over, Candidate) and over.rr >= 2.0 and over.model is EntryModel.FVG
    assert over.invalidation.price == pytest.approx(98.9)  # beyond the move origin, not the gap edge


# ======================================== (b) long OB in Premium is discarded
def test_b_long_ob_in_premium_is_discarded():
    res = EV.evaluate_ob(make_ctx(), make_ob(top=108.0, bottom=107.0))  # entry 108 -> 0.80 of 100-110
    assert isinstance(res, Discard) and "Premium" in res.reason and "hard block" in res.reason


def test_b_short_ob_in_discount_is_discarded_and_equilibrium_needs_cleaner_side():
    short_in_discount = EV.evaluate_ob(make_ctx(targets=[95.0]), make_ob(Bias.BEARISH, top=103.0, bottom=102.0))
    assert isinstance(short_in_discount, Discard) and "Discount" in short_in_discount.reason
    eq = EV.evaluate_ob(make_ctx(), make_ob(top=105.0, bottom=104.0))  # entry 105 -> exactly 0.50
    assert isinstance(eq, Discard) and "Equilibrium" in eq.reason


def test_b_ob_filters_mitigated_crawl_and_weak_impulse():
    ctx = make_ctx()
    assert "mitigated" in EV.evaluate_ob(ctx, make_ob(status=ZoneStatus.MITIGATED)).reason
    assert "crawl" in EV.evaluate_ob(make_ctx(overlap=0.8), make_ob()).reason
    assert "not displacement" in EV.evaluate_ob(ctx, make_ob(ratio=0.9)).reason
    assert "not a BOS/MSS" in EV.evaluate_ob(ctx, make_ob(ev_type=StructureEventType.CHOCH)).reason


def test_b_ob_that_also_made_an_fvg_is_flagged():
    res = EV.evaluate_ob(make_ctx(), make_ob(has_fvg=True))
    assert isinstance(res, Candidate) and res.quality.has_fvg


# ==== (c) internal long allowed while external bias is bearish (and mirror)
def test_c_internal_long_allowed_against_bearish_external_bias():
    ctx = make_ctx(external_bias=Bias.BEARISH, internal_bias=Bias.BULLISH)
    res = EV.evaluate_ob(ctx, make_ob())  # entry 102 -> 0.2: valid internal Discount
    assert isinstance(res, Candidate) and res.direction is PlanDirection.LONG
    assert res.quality.counter_trend_internal and "counter-trend internal long" in res.reason


def test_c_internal_short_allowed_against_bullish_external_bias():
    ctx = make_ctx(external_bias=Bias.BULLISH, internal_bias=Bias.BEARISH, targets=[95.0])
    res = EV.evaluate_ob(ctx, make_ob(Bias.BEARISH, top=109.0, bottom=108.0))  # entry 108 -> Premium 0.8
    assert isinstance(res, Candidate) and res.direction is PlanDirection.SHORT and res.quality.counter_trend_internal


def test_c_external_bias_never_changes_the_verdict():
    for bias in Bias:
        res = EV.evaluate_ob(make_ctx(external_bias=bias), make_ob())
        assert isinstance(res, Candidate)
    # ...but the P/D rule still blocks an internal long in Premium even if external agrees
    res = EV.evaluate_ob(make_ctx(external_bias=Bias.BULLISH), make_ob(top=108.0, bottom=107.0))
    assert isinstance(res, Discard)


# ============================================ shared hub-driven scenario
def build_world(history_points=(100, 150, 105, 118)):
    cfg = BotConfig()
    hub = StructureHub(cfg)
    mgr = PlanManager(cfg, hub)
    df = zigzag(list(history_points), M15)
    hub.update(SYM, M15, df)  # warm-up replay
    return cfg, hub, mgr, df


def next_candle(df: pd.DataFrame, o, h, l, c) -> pd.DataFrame:
    return frame([(o, h, l, c)], M15, start=df.index[-1] + pd.Timedelta("15min"))


def inject_plan(mgr: PlanManager, hub: StructureHub, direction: PlanDirection, zone: PriceRange,
                zone_id: str | None, stop: float, targets: list[float]) -> Plan:
    t = hub.get_tracker(SYM, M15)
    now = t.buffer.time(t.buffer.last_index)
    plan = Plan(
        plan_id=mgr.store.next_id(SYM), symbol=SYM, direction=direction, entry_model=EntryModel.OB,
        source_timeframes=SourceTimeframes((Timeframe.H4, Timeframe.M30), (M15, Timeframe.M5), M15),
        external_bias=Bias.NEUTRAL, internal_bias=Bias.NEUTRAL,
        zones=PlanZones(order_block_id=zone_id), entry_zone=zone,
        invalidation=Invalidation(stop, "test stop"), targets=targets, rr_estimate=3.0,
        reason="injected for test", created_at=now, updated_at=now)
    return mgr.store.add(plan)


# ============ (d) body close through a supply zone flips to a demand retest
def test_d_body_close_through_supply_flips_plan_to_demand_retest():
    cfg, hub, mgr, df = build_world()  # last close 118, swing high 150 still unbroken above
    t = hub.get_tracker(SYM, M15)
    supply = make_ob(Bias.BEARISH, top=122.0, bottom=120.0, idx=t.buffer.last_index)
    hub.zones.add(supply)
    short = inject_plan(mgr, hub, PlanDirection.SHORT, PriceRange(122.0, 120.0), supply.id,
                        stop=124.0, targets=[105.0])

    # candle whose BODY closes above the supply zone top (122)
    changes = mgr.run_candles(SYM, M15, next_candle(df, 118.0, 123.5, 117.8, 123.0))

    assert supply.status is ZoneStatus.FLIPPED
    assert short.status is PlanStatus.FLIPPED and "flipped to demand" in short.flip_reason
    assert "body close 123.00" in short.flip_reason
    assert len(short.child_plan_ids) == 1
    child = mgr.store.get(short.child_plan_ids[0])
    assert child.direction is PlanDirection.LONG and child.parent_plan_id == short.plan_id
    assert child.status is PlanStatus.PLANNED and child.is_live
    assert (child.entry_zone.bottom, child.entry_zone.top) == (120.0, 122.0)
    assert child.zones.flipped_zone_id == supply.flipped_to and "retest of flipped demand" in child.reason
    assert child.targets and child.targets[0] > child.entry_price  # next structure above
    assert child.invalidation.price < 120.0  # stop beyond the flipped range
    kinds = [c.kind for c in changes]
    assert ChangeKind.FLIPPED in kinds and ChangeKind.CREATED in kinds


@pytest.mark.parametrize(
    ("low", "expected_status"),
    [(115.0, PlanStatus.READY_TO_EXECUTE), (114.0, PlanStatus.CANCELED)],
)
def test_fully_filled_fvg_cancels_plan_but_partial_fill_does_not(low, expected_status):
    cfg, hub, mgr, df = build_world()
    tracker = hub.get_tracker(SYM, M15)
    fvg = make_fvg(top=116.0, bottom=114.0, idx=tracker.buffer.last_index)
    hub.zones.add(fvg)
    plan = inject_plan(mgr, hub, PlanDirection.LONG, PriceRange(116.0, 114.0), fvg.id,
                       stop=112.0, targets=[150.0])
    plan.zones.order_block_id = None
    plan.zones.fvg_id = fvg.id

    mgr.run_candles(SYM, M15, next_candle(df, 118.0, 118.5, low, 117.5))

    assert plan.status is expected_status
    if expected_status is PlanStatus.CANCELED:
        assert plan.cancel_reason is not None
        assert "FVG" in plan.cancel_reason and "fully filled" in plan.cancel_reason
        assert fvg.fully_filled_time is not None
    else:
        assert plan.cancel_reason is None
        assert fvg.max_penetration == pytest.approx(0.5)


def test_d_wick_through_supply_does_not_flip():
    cfg, hub, mgr, df = build_world()
    t = hub.get_tracker(SYM, M15)
    supply = make_ob(Bias.BEARISH, top=122.0, bottom=120.0, idx=t.buffer.last_index)
    hub.zones.add(supply)
    short = inject_plan(mgr, hub, PlanDirection.SHORT, PriceRange(122.0, 120.0), supply.id, 124.0, [105.0])
    mgr.run_candles(SYM, M15, next_candle(df, 118.0, 123.9, 117.8, 121.0))  # wick above, close inside
    assert supply.status is ZoneStatus.MITIGATED and short.is_live and not short.child_plan_ids


def test_d_flip_beyond_flip_limit_cancels_without_child():
    cfg, hub, mgr, df = build_world()
    t = hub.get_tracker(SYM, M15)
    supply = make_ob(Bias.BEARISH, top=122.0, bottom=120.0, idx=t.buffer.last_index)
    supply.flip_count = cfg.structure.max_zone_flips  # already flipped once
    hub.zones.add(supply)
    short = inject_plan(mgr, hub, PlanDirection.SHORT, PriceRange(122.0, 120.0), supply.id, 124.0, [105.0])
    mgr.run_candles(SYM, M15, next_candle(df, 118.0, 123.5, 117.8, 123.0))
    assert short.status is PlanStatus.CANCELED and "flip limit" in short.cancel_reason


def test_flip_retest_below_hard_rr_for_fvg_is_not_created_but_plan_still_flips():
    cfg, hub, mgr, df = build_world()
    t = hub.get_tracker(SYM, M15)
    fvg = make_fvg(Bias.BEARISH, top=147.0, bottom=120.0, stop_ref=150.0, idx=t.buffer.last_index)  # huge gap
    hub.zones.add(fvg)
    plan = inject_plan(mgr, hub, PlanDirection.SHORT, PriceRange(147.0, 120.0), None, 151.0, [105.0])
    plan.zones.fvg_id = fvg.id
    mgr.run_candles(SYM, M15, next_candle(df, 118.0, 148.5, 117.8, 148.0))
    assert plan.status is PlanStatus.FLIPPED and not plan.child_plan_ids
    assert "no retest plan created" in plan.flip_reason


# ======================================== adaptive loop: invalidation / expiry
def test_invalidation_by_body_close_cancels_plan_but_wick_does_not():
    cfg, hub, mgr, df = build_world()
    plan = inject_plan(mgr, hub, PlanDirection.LONG, PriceRange(116.0, 114.0), None, stop=112.0, targets=[150.0])
    mgr.run_candles(SYM, M15, next_candle(df, 118.0, 118.5, 111.0, 114.0))  # wick through 112, close above
    assert plan.is_live
    df2 = pd.concat([df, next_candle(df, 118.0, 118.5, 111.0, 114.0)])
    mgr.run_candles(SYM, M15, next_candle(df2, 114.0, 114.5, 110.0, 111.5))  # body close below 112
    assert plan.status is PlanStatus.CANCELED
    assert "invalidated: body close 111.50" in plan.cancel_reason and "112.00" in plan.cancel_reason


def test_plan_status_progresses_planned_active_ready():
    cfg, hub, mgr, df = build_world()
    plan = inject_plan(mgr, hub, PlanDirection.LONG, PriceRange(114.0, 112.0), None, stop=108.0, targets=[150.0])
    assert plan.status is PlanStatus.PLANNED
    mgr.run_candles(SYM, M15, next_candle(df, 118.0, 118.2, 116.0, 116.5))  # within 1 ATR band? entry 114
    df2 = pd.concat([df, next_candle(df, 118.0, 118.2, 116.0, 116.5)])
    mgr.run_candles(SYM, M15, next_candle(df2, 116.5, 116.6, 113.0, 113.5))  # trades into 112-114
    assert plan.status is PlanStatus.READY_TO_EXECUTE
    assert [e.kind for e in plan.history if e.kind == "status"]  # history recorded


def test_untouched_plan_expires():
    cfg, hub, mgr, df = build_world()
    plan = inject_plan(mgr, hub, PlanDirection.LONG, PriceRange(80.0, 78.0), None, stop=70.0, targets=[150.0])
    cur = df
    for _ in range(cfg.planning.plan_expiry_bars + 2):
        row = next_candle(cur, 118.0, 118.4, 117.6, 118.0)
        mgr.run_candles(SYM, M15, row)
        cur = pd.concat([cur, row])
        if not plan.is_live:
            break
    assert plan.status is PlanStatus.CANCELED and "expired" in plan.cancel_reason


# ==================== (e) wick through a high without a body close = a sweep
def test_e_wick_through_high_without_body_close_is_a_sweep_not_a_bos():
    hub = StructureHub(BotConfig())
    df = zigzag([100, 120, 105], M15)  # swing high 120.2 confirmed
    hub.update(SYM, M15, df)
    before = len(hub.get_tracker(SYM, M15).events)
    wick = next_candle(df, 105.0, 121.5, 104.5, 119.0)  # wick > 120.2, body closes back below
    upd = hub.update(SYM, M15, wick)
    assert len(hub.get_tracker(SYM, M15).events) == before and not upd.events  # no BOS / CHoCH
    assert len(upd.liquidity_sweeps) == 1
    sw = upd.liquidity_sweeps[0]
    assert sw.side is LiquiditySide.BUY_SIDE and sw.implied_bias is Bias.BEARISH
    assert sw.level_price == pytest.approx(120.2) and sw.extreme == 121.5 and sw.close < sw.level_price
    assert sw.kind is LiquidityKind.SWING_HIGH  # a prior swing counts as sweepable liquidity


def test_e_body_close_beyond_the_high_is_a_bos_and_not_a_sweep():
    hub = StructureHub(BotConfig())
    df = zigzag([100, 120, 105], M15)
    hub.update(SYM, M15, df)
    upd = hub.update(SYM, M15, next_candle(df, 105.0, 122.0, 104.5, 121.5))
    assert [e.type for e in upd.events] == [StructureEventType.BOS] and not upd.liquidity_sweeps


def make_sweep(close=119.0, level=120.2, extreme=121.5) -> LiquiditySweep:
    return LiquiditySweep(level_id="LIQ-SWH", kind=LiquidityKind.SWING_HIGH, side=LiquiditySide.BUY_SIDE, symbol=SYM,
                          timeframe=M15, time=T0, level_price=level, extreme=extreme, close=close)


def make_mss(direction=Bias.BEARISH, etype=StructureEventType.MSS, minutes=45) -> StructureEvent:
    ob = make_ob(Bias.BEARISH)
    return StructureEvent(id="MSS-1", type=etype, direction=direction, symbol=SYM, timeframe=M15,
                          layer=StructureLayer.INTERNAL, time=T0 + pd.Timedelta(minutes=minutes), index=60,
                          level=118.0, broken_swing=None, close=117.0, bias_before=Bias.BULLISH, bias_after=direction,
                          origin_index=55, displacement=True, displacement_ratio=2.0)


def sweep_eval(ctx, sweep=None, mss=None, held=True, zone=PriceRange(119.0, 118.0)):
    return EV.evaluate_sweep_mss(ctx, sweep or make_sweep(), mss or make_mss(), zone,
                                 PlanZones(liquidity_level_id="LIQ-SWH"), held, "at the MSS order block")


def test_e_sweep_requires_close_back_inside():
    ctx = make_ctx(low=100.0, high=130.0, targets=[100.0])
    res = sweep_eval(ctx, make_sweep(close=121.8))  # body closed beyond the level
    assert isinstance(res, Discard) and "break, not a sweep" in res.reason


def test_sweep_mss_candidate_and_chasing_discard():
    ctx = make_ctx(low=100.0, high=130.0, targets=[100.0], atr=1.0)  # entry 118 (bottom), stop 121.6
    ok = sweep_eval(ctx)
    assert isinstance(ok, Candidate) and ok.model is EntryModel.SWEEP_MSS and ok.direction is PlanDirection.SHORT
    assert ok.invalidation.price == pytest.approx(121.6) and ok.rr == pytest.approx(18 / 3.6)
    chasing = sweep_eval(make_ctx(low=100.0, high=130.0, targets=[115.0]))  # reward 3 vs risk 3.6 -> 0.83R
    assert isinstance(chasing, Discard) and "chasing" in chasing.reason and "1.5" in chasing.reason


def test_sweep_mss_other_rejections():
    ctx = make_ctx(low=100.0, high=130.0, targets=[100.0])
    assert "not an MSS/CHoCH" in sweep_eval(ctx, mss=make_mss(etype=StructureEventType.BOS)).reason
    assert "does not oppose" in sweep_eval(ctx, mss=make_mss(direction=Bias.BULLISH)).reason
    assert "sweep extreme" in sweep_eval(ctx, held=False).reason
    # short entered in Discount is hard-blocked even for sweeps; equilibrium is allowed
    low_ctx = make_ctx(low=100.0, high=150.0, targets=[90.0])  # entry 118 -> 0.36 Discount
    assert "Discount" in sweep_eval(low_ctx).reason
    eq_ctx = make_ctx(low=100.0, high=136.0, targets=[100.0])  # entry 118 -> 0.50
    assert isinstance(sweep_eval(eq_ctx), Candidate)


# ================================================ 4. OB + FVG confluence
def test_ob_fvg_confluence_and_individual_leg_failure():
    ctx = make_ctx(targets=[112.0])
    ob, fvg = make_ob(top=102.0, bottom=101.0), make_fvg(top=103.0, bottom=102.0, stop_ref=99.5)
    res = EV.evaluate_ob_fvg(ctx, ob, fvg)
    assert isinstance(res, Candidate) and res.model is EntryModel.OB_FVG and res.priority > EntryModel.OB.priority
    assert (res.entry_zone.bottom, res.entry_zone.top) == (101.0, 103.0) and res.quality.confluence
    assert res.zones.order_block_id == ob.id and res.zones.fvg_id == fvg.id

    weak_ob = make_ob(top=102.0, bottom=101.0, ratio=0.5)
    assert "OB leg of confluence fails" in EV.evaluate_ob_fvg(ctx, weak_ob, fvg).reason
    far_fvg = make_fvg(top=109.0, bottom=108.0)
    assert EV.evaluate_ob_fvg(ctx, ob, far_fvg).silent  # not adjacent: structural non-match


@pytest.mark.parametrize(
    "fvg",
    [
        make_fvg(idx=40),
        make_fvg(symbol="EURUSD"),
        make_fvg(timeframe=Timeframe.M5),
        make_fvg(bias=Bias.BEARISH),
    ],
)
def test_ob_fvg_rejects_unrelated_or_mismatched_zone_context(fvg):
    ctx = make_ctx(targets=[112.0])
    result = EV.evaluate_ob_fvg(ctx, make_ob(), fvg)

    assert isinstance(result, Discard)


def test_standalone_models_remain_eligible_when_confluence_is_unrelated():
    ctx = make_ctx(targets=[112.0])
    ob = make_ob()
    fvg = make_fvg(idx=40)

    assert isinstance(EV.evaluate_ob(ctx, ob), Candidate)
    assert isinstance(EV.evaluate_fvg(ctx, fvg), Candidate)
    assert isinstance(EV.evaluate_ob_fvg(ctx, ob, fvg), Discard)


def test_ob_fvg_requires_both_zones_to_remain_eligible():
    ctx = make_ctx(targets=[112.0])
    ob = make_ob(status=ZoneStatus.FLIPPED)
    fvg = make_fvg()

    result = EV.evaluate_ob_fvg(ctx, ob, fvg)

    assert isinstance(result, Discard)
    assert "OB leg of confluence fails" in result.reason


def test_zone_models_reject_symbol_or_timeframe_mismatches_with_context():
    ctx = make_ctx(targets=[112.0])

    assert isinstance(EV.evaluate_ob(ctx, make_ob(symbol="EURUSD")), Discard)
    assert isinstance(EV.evaluate_ob(ctx, make_ob(timeframe=Timeframe.M5)), Discard)
    assert isinstance(EV.evaluate_fvg(ctx, make_fvg(symbol="EURUSD")), Discard)
    assert isinstance(EV.evaluate_fvg(ctx, make_fvg(timeframe=Timeframe.M5)), Discard)


def test_ob_fvg_two_to_one_rule_applies_to_the_combined_entry():
    ctx = make_ctx(targets=[105.5])  # combined entry 103, widest stop 98.9 -> RR ~0.6
    res = EV.evaluate_ob_fvg(ctx, make_ob(), make_fvg(top=103.0, bottom=102.0, stop_ref=99.5))
    assert isinstance(res, Discard) and "RR" in res.reason


# ================================================================ store & dedupe
def test_plan_store_lifecycle_and_duplicate_guard():
    cfg, hub, mgr, df = build_world()
    z = PriceRange(116.0, 114.0)
    p = inject_plan(mgr, hub, PlanDirection.LONG, z, "ZONE-1", 112.0, [150.0])
    store = mgr.store
    assert store.list_active(SYM) == [p]
    assert store.find_by_zones(SYM, PlanDirection.LONG, ("ZONE-1",)) is p
    assert store.find_by_zones(SYM, PlanDirection.SHORT, ("ZONE-1",)) is None
    assert store.find_overlapping(SYM, PlanDirection.LONG, 115.0, 113.0) is p
    store.update(p.plan_id, T0, "moved target", targets=[140.0])
    assert p.targets == [140.0] and p.history[-1].note == "moved target"
    store.cancel(p.plan_id, "test cancel", T0)
    assert p.status is PlanStatus.CANCELED and not store.list_active(SYM)
    with pytest.raises(ValueError):
        store.update(p.plan_id, T0, x=1)
    with pytest.raises(ValueError):
        store.cancel(p.plan_id, "again", T0)


def test_same_zone_same_direction_never_creates_two_plans():
    cfg, hub, mgr, df = build_world()
    ctx = mgr.view.build(SYM, M15)
    ob = make_ob(top=102.0, bottom=101.0)
    ctx2 = make_ctx(low=100.0, high=110.0, targets=[112.0])
    cand = EV.evaluate_ob(ctx2, ob)
    assert isinstance(cand, Candidate)
    first = mgr._apply_candidate(cand, ctx, T0)
    again = mgr._apply_candidate(cand, ctx, T0)
    assert len(first) == 1 and first[0].kind is ChangeKind.CREATED and again == []
    assert len(mgr.store.list_active(SYM)) == 1


def test_higher_priority_model_upgrades_the_existing_plan_instead_of_stacking():
    cfg, hub, mgr, df = build_world()
    ctx = mgr.view.build(SYM, M15)
    ctx2 = make_ctx(targets=[112.0])
    ob, fvg = make_ob(top=102.0, bottom=101.0), make_fvg(top=103.0, bottom=102.0, stop_ref=99.5)
    hub.zones.add(ob)
    hub.zones.add(fvg)
    ob_c, conf = EV.evaluate_ob(ctx2, ob), EV.evaluate_ob_fvg(ctx2, ob, fvg)
    mgr._apply_candidate(ob_c, ctx, T0)
    out = mgr._apply_candidate(conf, ctx, T0)
    plans = mgr.store.list_active(SYM)
    assert len(plans) == 1 and plans[0].entry_model is EntryModel.OB_FVG
    assert out[0].kind is ChangeKind.UPGRADED and plans[0].zones.fvg_id == fvg.id
    assert plans[0].entry_range_basis is EntryRangeBasis.OB_FVG_CONFLUENCE
    assert {zone.id for zone in plans[0].source_zones} == {ob.id, fvg.id}
    assert mgr._apply_candidate(ob_c, ctx, T0) == []  # lower priority never downgrades


def test_m15_fvg_upgraded_to_m5_ob_fvg_refreshes_setup_timeframe():
    cfg, hub, mgr, _ = build_world()
    ctx = mgr.view.build(SYM, M15)
    m15_ctx = make_ctx(targets=[112.0], tf=M15)
    m15_fvg = make_fvg(top=102.0, bottom=101.0, stop_ref=99.5, timeframe=M15)
    original_candidate = EV.evaluate_fvg(m15_ctx, m15_fvg)
    assert isinstance(original_candidate, Candidate)
    mgr._apply_candidate(original_candidate, ctx, T0)
    original_plan = mgr.store.list_active(SYM)[0]
    original_plan_id = original_plan.plan_id

    m5_ctx = make_ctx(targets=[112.0], tf=Timeframe.M5)
    m5_ob = make_ob(top=102.0, bottom=101.0, timeframe=Timeframe.M5)
    m5_fvg = make_fvg(top=103.0, bottom=102.0, stop_ref=99.5, timeframe=Timeframe.M5)
    hub.zones.add(m5_ob)
    hub.zones.add(m5_fvg)
    upgrade_candidate = EV.evaluate_ob_fvg(m5_ctx, m5_ob, m5_fvg)
    assert isinstance(upgrade_candidate, Candidate)

    changes = mgr._apply_candidate(upgrade_candidate, ctx, T0 + pd.Timedelta("1min"))
    upgraded = mgr.store.get(original_plan_id)

    assert changes[0].kind is ChangeKind.UPGRADED
    assert upgraded.entry_model is EntryModel.OB_FVG
    assert upgraded.plan_id == original_plan_id
    assert upgraded.source_timeframes.setup is Timeframe.M5
    assert {zone.timeframe for zone in upgraded.source_zones} == {Timeframe.M5}


def test_distinct_fvg_plans_with_shared_stops_and_targets_remain_distinct_and_idempotent():
    cfg, hub, mgr, df = build_world()
    ctx = make_ctx(targets=[118.0])
    first = make_fvg(top=102.0, bottom=101.0, stop_ref=99.0, idx=50)
    second = make_fvg(top=104.0, bottom=103.0, stop_ref=99.0, idx=51)
    second.id = "FVG-second-independent"
    hub.zones.add(first)
    hub.zones.add(second)
    first_candidate = EV.evaluate_fvg(ctx, first)
    second_candidate = EV.evaluate_fvg(ctx, second)
    assert isinstance(first_candidate, Candidate) and isinstance(second_candidate, Candidate)
    assert first_candidate.invalidation.price == second_candidate.invalidation.price
    assert first_candidate.targets == second_candidate.targets

    created_first = mgr._apply_candidate(first_candidate, ctx, T0)
    created_second = mgr._apply_candidate(second_candidate, ctx, T0)
    repeated = mgr._apply_candidate(first_candidate, ctx, T0)

    plans = mgr.store.list_active(SYM)
    assert [change.kind for change in created_first] == [ChangeKind.CREATED]
    assert [change.kind for change in created_second] == [ChangeKind.CREATED]
    assert repeated == []
    assert len(plans) == 2
    assert {plan.zones.fvg_id for plan in plans} == {first.id, second.id}
    assert all(plan.entry_range_basis is EntryRangeBasis.FAIR_VALUE_GAP for plan in plans)
    assert all(len(plan.source_zones) == 1 for plan in plans)


def test_standalone_ob_plan_retains_zone_provenance_through_plan_updates():
    cfg, hub, mgr, df = build_world()
    ctx = make_ctx(targets=[112.0])
    ob = make_ob()
    hub.zones.add(ob)
    candidate = EV.evaluate_ob(ctx, ob)
    assert isinstance(candidate, Candidate)

    created = mgr._apply_candidate(candidate, ctx, T0)
    plan = mgr.store.get(created[0].plan_id)
    mgr.store.update(plan.plan_id, T0 + pd.Timedelta("1min"), "refresh test", reason="updated")
    payload = _plan(plan, hub.zones.get)

    assert plan.entry_range_basis is EntryRangeBasis.ORDER_BLOCK
    assert plan.zones.order_block_id == ob.id
    assert plan.source_zones == (ob,)
    assert payload["source_zone_ids"]["order_block"] == ob.id
    assert payload["entry_range_basis"] == "order_block"
    assert payload["source_zones"][0]["id"] == ob.id


def test_ob_fvg_plan_snapshot_confirms_only_the_recorded_displacement_leg():
    cfg, hub, mgr, df = build_world()
    ctx = make_ctx(targets=[112.0])
    ob = make_ob(top=102.0, bottom=101.0, idx=50, leg_start_idx=45)
    fvg = make_fvg(top=103.0, bottom=102.0, stop_ref=99.5, idx=47)
    hub.zones.add(ob)
    hub.zones.add(fvg)
    candidate = EV.evaluate_ob_fvg(ctx, ob, fvg)
    assert isinstance(candidate, Candidate)
    changes = mgr._apply_candidate(candidate, ctx, T0)
    plan = mgr.store.get(changes[0].plan_id)

    payload = _plan(plan, hub.zones.get)

    assert payload["entry_range_basis"] == "ob_fvg_confluence"
    assert payload["source_relationship"]["status"] == "verified"
    assert payload["source_relationship"]["origin_event_id"] == ob.origin_event_id

    unrelated = make_fvg(top=103.0, bottom=102.0, stop_ref=99.5, idx=40)
    unrelated.id = "FVG-outside-origin-leg"
    hub.zones.add(unrelated)
    bad_payload = _plan(plan, lambda zone_id: unrelated if zone_id == fvg.id else hub.zones.get(zone_id))
    assert bad_payload["source_relationship"]["status"] == "unavailable"


def test_retired_zone_is_not_replanned_after_cancel():
    cfg, hub, mgr, df = build_world()
    ctx = mgr.view.build(SYM, M15)
    cand = EV.evaluate_ob(make_ctx(targets=[112.0]), make_ob())
    mgr._apply_candidate(cand, ctx, T0)
    plan = mgr.store.list_active(SYM)[0]
    mgr._retire(plan)
    mgr.store.cancel(plan.plan_id, "test", T0)
    assert mgr._apply_candidate(cand, ctx, T0) == []


# ============================================ end-to-end through the real hub
def test_manager_ignores_warmup_and_only_plans_on_scan_timeframes():
    cfg = BotConfig()
    hub = StructureHub(cfg)
    mgr = PlanManager(cfg, hub)
    changes = mgr.run_candles(SYM, M15, zigzag([100, 150, 105, 140, 110]))
    assert changes == [] and not mgr.store.all()  # warm-up: no plans from replayed history
    mgr.bootstrap([SYM])  # must not raise; any plan it finds comes from the CURRENT state only
    h4 = mgr.run_candles(SYM, Timeframe.H4, zigzag([100, 150, 105], Timeframe.H4))
    assert all(c.kind is not ChangeKind.CREATED for c in h4)
