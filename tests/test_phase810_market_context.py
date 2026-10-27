from __future__ import annotations

import pandas as pd
import pytest

from smc.config import BotConfig
from smc.core.timeframes import Timeframe
from smc.dashboard.pages import (
    _conditional_scenario_empty_state,
    _conditional_scenario_summary,
    _plan_rows,
)
from smc.dashboard.snapshot import _plan
from smc.models.plan import (
    EntryModel, Invalidation, Plan, PlanDirection, PlanZones, PriceRange, SourceTimeframes,
)
from smc.planning.entry_models import Candidate, EntryModelEvaluators
from smc.planning.market_context import (
    assess_conditional_zone_scenarios, conditional_zone_scenarios, plan_market_context, zone_state,
)
from smc.structure.models import (
    Bias, FairValueGap, ZoneKind, ZoneStatus,
)
from smc.structure.zones import apply_mitigation
from smc.structure.flipped_zones import FlippedZoneEngine
from tests.test_phase3 import SYM, make_ctx

NOW = pd.Timestamp("2026-10-27T00:00:00Z")
TF = Timeframe.M15


def make_fvg(bias: Bias = Bias.BULLISH, top: float = 102.0, bottom: float = 101.0) -> FairValueGap:
    return FairValueGap(
        id=f"FVG-{bias.value}-{bottom}",
        kind=ZoneKind.FVG,
        symbol=SYM,
        timeframe=TF,
        bias=bias,
        top=top,
        bottom=bottom,
        created_time=NOW,
        created_index=10,
        gap_size=top - bottom,
        stop_reference=bottom - 2 if bias is Bias.BULLISH else top + 2,
    )


def scenario_inputs(zone: FairValueGap, price: float = 101.5):
    return conditional_zone_scenarios(
        [zone],
        price,
        {TF: 1.0},
        approach_atr_mult=1.0,
        max_age_bars=200,
        current_index_by_timeframe={TF: 20},
    )


@pytest.mark.parametrize(
    ("bias", "rejection_case", "rejection_direction", "breakout_direction"),
    [
        (Bias.BEARISH, "supply_rejection", "short", "long"),
        (Bias.BULLISH, "demand_rejection", "long", "short"),
    ],
)
def test_nearby_zone_scenarios_are_context_not_entry_signals(
    bias, rejection_case, rejection_direction, breakout_direction
):
    zone = make_fvg(bias)
    scenarios = scenario_inputs(zone)

    assert len(scenarios) == 1
    assert scenarios[0]["entry_confirmation"] == "Not confirmed by this context assessment."
    assert scenarios[0]["possibilities"][0]["case"] == rejection_case
    assert scenarios[0]["possibilities"][0]["direction"] == rejection_direction
    assert scenarios[0]["possibilities"][1]["direction"] == breakout_direction
    assert zone.status is ZoneStatus.ACTIVE
    assert zone.reaction_count == 0


def test_zone_scenarios_are_stable_and_stale_or_unavailable_context_is_omitted():
    zone = make_fvg()
    assert scenario_inputs(zone) == scenario_inputs(zone)
    assert conditional_zone_scenarios(
        [zone], None, {TF: 1.0}, 1.0, 200, {TF: 20}
    ) == []
    assert conditional_zone_scenarios(
        [zone], 101.5, {TF: 1.0}, 1.0, 200, {TF: 500}
    ) == []


def test_scenario_evaluation_distinguishes_empty_stale_distant_and_missing_context():
    active_zone = make_fvg()
    dead_zone = make_fvg(Bias.BEARISH)
    dead_zone.status = ZoneStatus.INVALIDATED

    scenarios, evaluation = assess_conditional_zone_scenarios(
        [dead_zone], 101.5, {TF: 1.0}, 1.0, 200, {TF: 20}
    )
    assert scenarios == []
    assert evaluation["status"] == "no_live_zones"
    assert _conditional_scenario_empty_state(evaluation).startswith("No live supply or demand zones")

    _, distant = assess_conditional_zone_scenarios(
        [active_zone], 110.0, {TF: 1.0}, 1.0, 200, {TF: 20}
    )
    assert distant["status"] == "no_qualifying_nearby_zones"
    assert distant["outside_distance_zone_count"] == 1
    assert "1 live zone(s) checked within 1 ATR" in _conditional_scenario_empty_state(distant)

    _, stale = assess_conditional_zone_scenarios(
        [active_zone], 101.5, {TF: 1.0}, 1.0, 5, {TF: 20}
    )
    assert stale["status"] == "no_qualifying_nearby_zones"
    assert stale["age_eligible_zone_count"] == 0

    _, missing = assess_conditional_zone_scenarios(
        [active_zone], 101.5, {}, 1.0, 200, {TF: 20}
    )
    assert missing["status"] == "insufficient_context"
    assert missing["missing_atr_zone_count"] == 1
    assert "ATR for 1 zone(s)" in _conditional_scenario_empty_state(missing)

    _, unavailable_price = assess_conditional_zone_scenarios(
        [active_zone], None, {TF: 1.0}, 1.0, 200, {TF: 20}
    )
    assert unavailable_price["status"] == "insufficient_context"
    assert "current price" in _conditional_scenario_empty_state(unavailable_price)
    assert _conditional_scenario_empty_state(None) == (
        "Scenario evaluation details are unavailable in this snapshot."
    )
    assert "218 live zone(s)" in _conditional_scenario_empty_state(
        None,
        {
            "price": 100.0,
            "zones": [
                {"status": "active", "bias": "bullish"}
                for _ in range(218)
            ],
        },
    )
    assert _conditional_scenario_empty_state(
        None, {"price": None, "zones": [{"status": "active", "bias": "bearish"}]}
    ) == (
        "Insufficient market context: 1 live zone(s) are present, but current price is unavailable."
    )


def test_scenario_summary_shows_live_snapshot_diagnostics():
    summary = _conditional_scenario_summary({
        "status": "scenarios_available",
        "live_zone_count": 218,
        "age_eligible_zone_count": 59,
        "outside_distance_zone_count": 54,
        "missing_atr_zone_count": 0,
        "approach_atr_mult": 1.0,
        "reason": "Nearby zones have sufficient context and meet the existing monitoring rules.",
    }, 5)

    assert summary == (
        "5 scenario(s) qualify from 218 live zone(s); 59 passed the age check, "
        "54 outside 1 ATR, 0 missing timeframe ATR. Nearby zones have sufficient "
        "context and meet the existing monitoring rules."
    )


def test_distinct_rejection_episodes_are_counted_but_touch_candles_are_not():
    zone = make_fvg()
    apply_mitigation(zone, NOW, 102.4, 101.4, 101.8)
    apply_mitigation(zone, NOW + pd.Timedelta(minutes=15), 102.5, 101.5, 102.2)
    apply_mitigation(zone, NOW + pd.Timedelta(minutes=30), 102.6, 101.6, 102.3)
    assert zone.touches == 3
    assert zone.reaction_count == 1
    assert zone_state(zone)[0] == "respected_once"

    apply_mitigation(zone, NOW + pd.Timedelta(minutes=45), 103.0, 102.5, 102.8)
    apply_mitigation(zone, NOW + pd.Timedelta(minutes=60), 102.5, 101.5, 102.2)
    assert zone.reaction_count == 2
    assert zone_state(zone)[0] == "repeatedly_respected"


def test_repeated_touches_without_close_back_are_not_repeated_reactions():
    zone = make_fvg()
    for offset in range(4):
        apply_mitigation(
            zone, NOW + pd.Timedelta(minutes=15 * offset), 102.3, 101.4, 101.8
        )

    assert zone.touches == 4
    assert zone.reaction_count == 0
    assert zone_state(zone)[0] == "touched_no_confirmed_reaction"


def test_full_mitigation_is_not_hidden_by_a_close_back_reaction():
    zone = make_fvg()
    apply_mitigation(zone, NOW, 102.4, 100.5, 102.2)

    assert zone.max_penetration == 1.0
    assert zone.reaction_count == 1
    assert zone_state(zone)[0] == "fully_mitigated_with_reaction"


def test_flip_replaces_zone_and_retest_context_uses_existing_flip_rules():
    demand = make_fvg()
    demand.status = ZoneStatus.MITIGATED
    demand.fully_filled_time = NOW
    demand.max_penetration = 1.0
    flip_engine = FlippedZoneEngine(BotConfig().structure)

    signal = flip_engine.check(demand, NOW, 11, close=100.5)
    assert signal is not None
    flip_signal, supply = signal
    assert flip_signal.new_zone_id == supply.id
    assert demand.status is ZoneStatus.FLIPPED
    assert supply.fully_filled_time is None
    assert supply.max_penetration == 0.0
    assert zone_state(demand)[0] == "broken_flipped"
    assert zone_state(supply)[0] == "flipped_retest_pending"

    apply_mitigation(supply, NOW + pd.Timedelta(minutes=15), 102.0, 101.5, 100.8)
    assert zone_state(supply)[0] == "flipped_retest_respected"
    assert scenario_inputs(supply)[0]["possibilities"][0]["case"] == "supply_rejection"


def make_plan(source: FairValueGap) -> Plan:
    return Plan(
        plan_id="PLAN-TEST-0001",
        symbol=SYM,
        direction=PlanDirection.LONG,
        entry_model=EntryModel.FVG,
        source_timeframes=SourceTimeframes((Timeframe.H4,), (TF,), TF),
        external_bias=Bias.NEUTRAL,
        internal_bias=Bias.BULLISH,
        zones=PlanZones(fvg_id=source.id),
        entry_zone=PriceRange(source.top, source.bottom),
        invalidation=Invalidation(98.0, "test source FVG"),
        targets=[110.0],
        rr_estimate=2.5,
        reason="test plan",
        created_at=NOW,
        updated_at=NOW,
        source_zones=(source,),
    )


def test_opposing_zone_is_explained_without_rejecting_standalone_fvg():
    source = make_fvg()
    supply = make_fvg(Bias.BEARISH, top=106.0, bottom=105.0)
    plan = make_plan(source)
    context = plan_market_context(plan, [source, supply])

    assert context["opposing_zones"][0]["zone_id"] == supply.id
    assert context["opposing_zones"][0]["target_one_relation"] == "target_beyond_zone"
    assert "do not automatically reject" in context["assessment"]

    result = EntryModelEvaluators().evaluate_fvg(
        make_ctx(low=95.0, high=120.0, targets=[110.0]),
        source,
    )
    assert isinstance(result, Candidate)
    assert result.model is EntryModel.FVG


def test_plan_context_is_serialized_and_rendered_without_changing_plan_values():
    source = make_fvg()
    supply = make_fvg(Bias.BEARISH, top=106.0, bottom=105.0)
    plan = make_plan(source)
    plan.market_context = plan_market_context(plan, [source, supply])

    payload = _plan(plan)
    _, details = _plan_rows([payload])
    assert payload["targets"] == [110.0]
    assert payload["invalidation"]["price"] == 98.0
    assert "Conditional plan" in details[0]["Market context"]
    assert supply.id in details[0]["Market context"]
    assert "context only" in details[0]["Market context"]


def test_legacy_plan_without_market_context_renders_honestly():
    _, details = _plan_rows([{
        "plan_id": "PLAN-LEGACY",
        "entry_model": "FVG",
        "direction": "long",
        "status": "planned",
        "entry_zone": {"bottom": 101.0, "top": 102.0},
    }])

    assert details[0]["Market context"] == "Not recorded in this snapshot"
