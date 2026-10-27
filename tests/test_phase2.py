"""Phase 2 tests - synthetic candles, no MT5 needed:  pytest -q"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from smc.config import BotConfig, StructureConfig
from smc.core.timeframes import StructureLayer, Timeframe
from smc.structure import (
    Bias, PDState, StructureEventType, StructureHub, SwingType, ZoneStatus, detect_swings,
)
from smc.structure.fvg import FVGDetector
from smc.structure.flipped_zones import FlippedZoneEngine
from smc.structure.models import FairValueGap, LiquidityKind, LiquidityStatus, ZoneKind
from smc.structure.order_blocks import OrderBlockDetector
from smc.structure.premium_discount import PremiumDiscountCalculator
from smc.structure.swings import is_swing_high_at, is_swing_low_at
from smc.structure.tracker import StructureTracker
from smc.structure.zones import apply_mitigation
from tests.synth import T0, frame, zigzag

SYM, M15 = "XAUUSD", Timeframe.M15


def run(df: pd.DataFrame, tf: Timeframe = M15, cfg: BotConfig | None = None):
    hub = StructureHub(cfg or BotConfig())
    return hub, hub.update(SYM, tf, df)


# ------------------------------------------------------------ 1. swings
def test_swings_found_on_zigzag():
    sw = detect_swings(zigzag([100, 120, 105, 125, 110]))
    assert [(s.type, round(s.price)) for s in sw] == [
        (SwingType.HIGH, 120), (SwingType.LOW, 105), (SwingType.HIGH, 125)]
    assert all(s.confirmed_time > s.time for s in sw)


def test_batch_and_incremental_swing_rules_agree():
    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(0, 1, 400))
    df = pd.DataFrame({"high": close + rng.random(400), "low": close - rng.random(400)})
    df.index = pd.date_range(T0, periods=400, freq="15min", tz="UTC")
    batch = {(s.type, s.index) for s in detect_swings(df, 4, 3)}
    h, l = df["high"].tolist(), df["low"].tolist()
    inc = {(SwingType.HIGH, j) for j in range(400) if is_swing_high_at(h, j, 4, 3)}
    inc |= {(SwingType.LOW, j) for j in range(400) if is_swing_low_at(l, j, 4, 3)}
    assert batch == inc


def test_equal_adjacent_highs_give_one_swing():
    rows = [(10, 11, 9, 10)] * 6 + [(10, 15, 9, 10), (10, 15, 9, 10)] + [(10, 11, 9, 10)] * 6
    highs = [s for s in detect_swings(frame(rows)) if s.type is SwingType.HIGH]
    assert len(highs) == 1


def test_fractal_width_is_configurable():
    df = zigzag([100, 120, 105], steps=4)
    assert len(detect_swings(df, 5, 5)) < len(detect_swings(df, 2, 2)) + 1
    assert detect_swings(df, 2, 2)


# ------------------------------------------------- 2/3. BOS / CHoCH / MSS
def test_first_break_is_bos_then_choch_flips_bias():
    _, upd = run(zigzag([100, 120, 105, 125, 112, 130, 100]))
    kinds = [(e.type, e.direction) for e in upd.events]
    assert kinds[0] == (StructureEventType.BOS, Bias.BULLISH)
    assert kinds[-1][1] is Bias.BEARISH
    assert kinds[-1][0] in (StructureEventType.CHOCH, StructureEventType.MSS)


def test_external_never_emits_mss_but_internal_does():
    pts = [100, 120, 105, 125, 112, 130, 100]
    _, ext = run(zigzag(pts, Timeframe.H4), Timeframe.H4)
    _, internal = run(zigzag(pts, Timeframe.M15), Timeframe.M15)
    assert StructureEventType.MSS not in {e.type for e in ext.events}
    assert ext.events[-1].type is StructureEventType.CHOCH
    assert internal.events[-1].type is StructureEventType.MSS
    assert ext.layer is StructureLayer.EXTERNAL and internal.layer is StructureLayer.INTERNAL


def test_wick_only_break_is_not_a_bos():
    base = zigzag_rows_to_swing_high()
    # candle wicks above the 120.2 swing high but closes back below it
    base.append((118.0, 121.5, 117.0, 119.0))
    _, upd = run(frame(base))
    assert upd.events == []


def zigzag_rows_to_swing_high():
    from tests.synth import zigzag_rows
    return zigzag_rows([100, 120, 105], steps=8) + [(105.0, 106.0, 104.8, 105.5)] * 0


def test_internal_and_external_biases_are_independent():
    hub = StructureHub(BotConfig())
    hub.update(SYM, Timeframe.H4, zigzag([100, 120, 105, 125], Timeframe.H4))  # bullish
    hub.update(SYM, M15, zigzag([130, 110, 125, 100], M15))  # bearish
    snap = hub.snapshot(SYM)
    assert snap.external_bias is Bias.BULLISH
    assert snap.internal_bias is Bias.BEARISH


def test_incremental_updates_equal_one_shot_replay():
    df = zigzag([100, 120, 105, 125, 112, 130, 100])
    one_shot, _ = run(df)
    hub = StructureHub(BotConfig())
    for k in range(40, len(df) + 1, 7):  # overlapping growing windows
        hub.update(SYM, M15, df.iloc[:k])
    hub.update(SYM, M15, df)
    a, b = one_shot.internal.tracker(SYM, M15), hub.internal.tracker(SYM, M15)
    assert [e.id for e in a.events] == [e.id for e in b.events] and a.bias is b.bias


# --------------------------------------------------------- 4. premium/discount
@pytest.mark.parametrize("price,state,pos", [
    (120, PDState.DISCOUNT, 0.2), (150, PDState.EQUILIBRIUM, 0.5), (180, PDState.PREMIUM, 0.8)])
def test_pd_formula_and_states(price, state, pos):
    calc = PremiumDiscountCalculator(StructureConfig())
    r = calc.calculate(200, 100, price, StructureLayer.EXTERNAL, Timeframe.H4)
    assert r.position == pytest.approx(pos) and r.state is state


def test_dual_pd_from_hub():
    hub = StructureHub(BotConfig())
    hub.update(SYM, Timeframe.H4, zigzag([100, 120, 105, 125], Timeframe.H4))
    hub.update(SYM, M15, zigzag([100, 120, 105, 125], M15))
    pdr = hub.snapshot(SYM, price=110).premium_discount
    assert pdr.external and pdr.internal
    assert pdr.external.range_high >= 120 and pdr.internal.range_low <= 105


# ---------------------------------------------------------- 5. order blocks
def ob_scenario() -> pd.DataFrame:
    """Range with a swing high at 101, a dip, a bearish candle, then a big bullish break."""
    rows = [(100, 100.5, 99.5, 100)] * 6
    rows += [(100, 101, 99.8, 100.5)]  # swing high 101 (index 6)
    rows += [(100.5, 100.8, 99.6, 100.0)] * 5
    rows += [(100.0, 100.4, 99.4, 99.6)]  # bearish candle  <- expected OB
    rows += [(99.6, 99.9, 99.5, 99.8)]
    rows += [(99.8, 104.0, 99.7, 103.5)]  # displacement closing above 101
    rows += [(103.5, 104.0, 103.0, 103.6)] * 3
    return frame(rows)


def test_bullish_order_block_created_from_displacement_break():
    hub, upd = run(ob_scenario())
    assert upd.events and upd.events[0].direction is Bias.BULLISH and upd.events[0].displacement
    assert len(upd.new_order_blocks) == 1
    ob = upd.new_order_blocks[0]
    assert ob.bias is Bias.BULLISH and ob.kind is ZoneKind.ORDER_BLOCK
    assert (ob.bottom, ob.top) == (99.4, 100.4)
    assert ob.status is ZoneStatus.ACTIVE and ob.created_time == upd.events[0].time
    assert ob.leg_start_index == upd.events[0].origin_index


def test_order_block_fvg_flag_only_uses_fvg_from_its_displacement_leg():
    hub, upd = run(ob_scenario())
    event = upd.events[0]
    tracker = hub.get_tracker(SYM, M15)

    def fvg(created_index, symbol=SYM, timeframe=M15):
        return FairValueGap(
            id=f"FVG-{created_index}-{symbol}-{timeframe}", kind=ZoneKind.FVG,
            symbol=symbol, timeframe=timeframe, bias=Bias.BULLISH,
            top=101.2, bottom=100.8, created_time=event.time,
            created_index=created_index, gap_size=0.4, stop_reference=99.0,
        )

    detector = OrderBlockDetector(StructureConfig())
    related = detector.detect(event, tracker, [fvg(event.origin_index)])
    assert related is not None and related.has_fvg
    assert related.leg_start_index == event.origin_index

    unrelated = OrderBlockDetector(StructureConfig()).detect(
        event, tracker, [
            fvg(event.origin_index - 1),
            fvg(event.origin_index, symbol="EURUSD"),
            fvg(event.origin_index, timeframe=Timeframe.M5),
        ],
    )
    assert unrelated is not None and not unrelated.has_fvg


def test_order_block_requires_displacement():
    rows = ob_scenario()
    for col, val in (("open", 100.9), ("high", 101.6), ("low", 100.8), ("close", 101.5)):
        rows.iloc[-4, rows.columns.get_loc(col)] = val  # small-body break: no displacement
    _, upd = run(rows)
    assert upd.events and not upd.new_order_blocks


def test_order_block_mitigation_then_flip():
    df = ob_scenario()
    hub, upd = run(df)
    ob = upd.new_order_blocks[0]
    # price returns into the OB (wick only) -> mitigated
    hub.update(SYM, M15, pd.concat([df, frame([(103.6, 103.7, 100.2, 101.0)], start=df.index[-1] + pd.Timedelta("15min"))]))
    assert ob.status is ZoneStatus.MITIGATED and 0 < ob.max_penetration <= 1
    # body close below the OB -> flipped into supply, signal fired
    nxt = df.index[-1] + pd.Timedelta("30min")
    u2 = hub.update(SYM, M15, pd.concat([df, frame([(103.6, 103.7, 100.2, 101.0), (101.0, 101.1, 98.8, 99.0)], start=df.index[-1] + pd.Timedelta("15min"))]))
    sig = [s for s in u2.flip_signals if s.old_zone_id == ob.id]
    assert ob.status is ZoneStatus.FLIPPED and len(sig) == 1 and not sig[0].is_historical
    new = hub.zones.get(sig[0].new_zone_id)
    assert new.bias is Bias.BEARISH and (new.bottom, new.top) == (ob.bottom, ob.top) and new.is_flipped_zone


# ------------------------------------------------------------------- 6. FVG
def fvg_frame() -> pd.DataFrame:
    return frame([(100, 101, 99, 100.5), (100.5, 104, 100.4, 103.5), (103.5, 105, 102.0, 104.5)])


def test_bullish_and_bearish_fvg_detection():
    t = StructureTracker(SYM, M15, StructureLayer.INTERNAL, StructureConfig(), True)
    out = []
    for ts, r in fvg_frame().iterrows():
        t.process_candle(ts, r.open, r.high, r.low, r.close)
        out.append(FVGDetector(StructureConfig()).detect(t))
    f = out[-1]
    assert f.bias is Bias.BULLISH and (f.bottom, f.top) == (101, 102.0) and f.stop_reference == 99

    t2 = StructureTracker(SYM, M15, StructureLayer.INTERNAL, StructureConfig(), True)
    for ts, r in frame([(105, 106, 104, 104.5), (104.5, 104.6, 101, 101.5), (101.5, 103.0, 100, 100.5)]).iterrows():
        t2.process_candle(ts, r.open, r.high, r.low, r.close)
    g = FVGDetector(StructureConfig()).detect(t2)
    assert g.bias is Bias.BEARISH and (g.bottom, g.top) == (103.0, 104)


def make_fvg(bias=Bias.BULLISH) -> FairValueGap:
    return FairValueGap(id="F1", kind=ZoneKind.FVG, symbol=SYM, timeframe=M15, bias=bias,
                        top=102.0, bottom=101.0, created_time=T0, created_index=2, gap_size=1.0,
                        stop_reference=99.0)


def test_fvg_rr_calculation_and_min_rr():
    f = make_fvg()
    assert f.calculate_rr(target=108.0) == pytest.approx((108 - 102) / (102 - 99))  # 2.0 at the edge
    assert f.calculate_rr(target=108.0, entry_mode="midpoint") == pytest.approx(6.5 / 2.5)
    assert f.current_rr(price=104.0, target=108.0) == pytest.approx(4 / 5)
    assert f.meets_min_rr(108.0, 2.0) and not f.meets_min_rr(106.0, 2.0)
    assert f.calculate_rr(target=100.0) is None  # target on the wrong side
    assert make_fvg(Bias.BEARISH).calculate_rr(target=95.0, stop=105.0) == pytest.approx((101 - 95) / (105 - 101))


def test_fvg_mitigation_and_inversion():
    f = make_fvg()
    ts = T0 + pd.Timedelta("1h")
    assert apply_mitigation(f, ts, high=103, low=102.5) is False  # did not reach the gap
    assert apply_mitigation(f, ts, high=103, low=101.5) is True and f.status is ZoneStatus.MITIGATED
    assert f.max_penetration == pytest.approx(0.5)
    sig, new = FlippedZoneEngine(StructureConfig()).check(f, ts, 9, close=100.5)
    assert f.status is ZoneStatus.FLIPPED and new.bias is Bias.BEARISH and sig.kind is ZoneKind.FVG


# ------------------------------------------------------- 7. liquidity levels
def test_equal_highs_detected_then_swept():
    pts = [100, 120, 105, 120.05, 100]
    df = zigzag(pts)
    hub, upd = run(df)
    eqh = [lv for lv in upd.new_liquidity_levels if lv.kind is LiquidityKind.EQUAL_HIGHS]
    assert len(eqh) == 1 and eqh[0].touches == 2 and eqh[0].price == pytest.approx(120.25)
    # wick above the level, close back inside -> SWEPT + sweep record
    sweep_bar = frame([(100.0, 121.5, 99.8, 100.5)], start=df.index[-1] + pd.Timedelta("15min"))
    u2 = hub.update(SYM, M15, pd.concat([df, sweep_bar]))
    assert len(u2.liquidity_sweeps) == 1 and u2.liquidity_sweeps[0].implied_bias is Bias.BEARISH
    assert eqh[0].status is LiquidityStatus.SWEPT


def test_equal_lows_not_formed_when_deeper_low_between():
    _, upd = run(zigzag([120, 100, 110, 90, 112, 100.05, 120]))
    assert not [lv for lv in upd.new_liquidity_levels if lv.kind is LiquidityKind.EQUAL_LOWS]


def test_previous_day_levels_created_on_day_rollover():
    day1 = zigzag([100, 110, 105], M15)  # 16 bars < 1 day
    n = 96  # pad to cross midnight UTC (15m bars)
    rows = list(zip(day1.open, day1.high, day1.low, day1.close))
    rows += [(105, 105.5, 104.5, 105)] * (n - len(rows) + 4)
    _, upd = run(frame(rows))
    kinds = {lv.kind for lv in upd.new_liquidity_levels}
    assert LiquidityKind.PREV_DAY_HIGH in kinds and LiquidityKind.PREV_DAY_LOW in kinds


# ------------------------------------------------ 8. flips through the hub
def test_flip_signal_marked_historical_during_warmup():
    _, upd = run(zigzag([100, 120, 105, 125, 112, 130, 100]))
    assert upd.is_warmup and upd.flip_signals and all(s.is_historical for s in upd.flip_signals)


def test_zone_invalidated_after_max_flips():
    cfg = StructureConfig(max_zone_flips=0)
    f = make_fvg()
    sig, new = FlippedZoneEngine(cfg).check(f, T0, 5, close=98.0)
    assert new is None and sig.new_zone_id is None and f.status is ZoneStatus.INVALIDATED
