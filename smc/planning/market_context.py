"""Read-only market context derived from existing structure zones."""

from __future__ import annotations

from typing import Iterable, Optional

import pandas as pd

from smc.models.plan import Plan, PlanDirection
from smc.core.timeframes import Timeframe
from smc.structure.models import Bias, Zone, ZoneStatus


def zone_state(zone: Zone) -> tuple[str, str]:
    """Return a cautious lifecycle label and the evidence supporting it."""
    if zone.status is ZoneStatus.FLIPPED:
        return "broken_flipped", "Body close broke the zone; the existing flip lifecycle replaced it."
    if zone.status is ZoneStatus.INVALIDATED:
        return "broken_invalidated", "Body close broke the zone and no replacement remains active."
    if zone.is_flipped_zone and zone.reaction_count:
        evidence = (
            f"Flipped zone has {zone.reaction_count} distinct close-back reaction(s) "
            "after its creation."
        )
        if zone.fully_filled_time is not None:
            evidence += " Price also traversed the full zone."
        return "flipped_retest_respected", evidence
    if zone.is_flipped_zone and zone.fully_filled_time is not None:
        return "flipped_retest_fully_mitigated", (
            "Price traversed the flipped zone; no close-back retest reaction is recorded."
        )
    if zone.is_flipped_zone:
        return "flipped_retest_pending", "Flipped zone is active; no close-back retest reaction is recorded."
    if zone.fully_filled_time is not None and zone.reaction_count:
        return "fully_mitigated_with_reaction", (
            f"Price traversed the full zone and {zone.reaction_count} distinct close-back reaction(s) "
            "are recorded."
        )
    if zone.reaction_count > 1:
        return "repeatedly_respected", (
            f"{zone.reaction_count} distinct touch episode(s) closed back beyond the zone's near edge."
        )
    if zone.reaction_count == 1:
        return "respected_once", "One touch episode closed back beyond the zone's near edge."
    if zone.status is ZoneStatus.ACTIVE:
        return "untested", "No candle-range touch is recorded."
    if zone.fully_filled_time is not None:
        return "fully_mitigated", "Price traversed the full zone; this is not by itself a body-close break."
    return "touched_no_confirmed_reaction", "The zone was touched, but no close-back reaction is confirmed."


def zone_context(zone: Zone) -> dict[str, object]:
    state, evidence = zone_state(zone)
    stamp = zone.last_reaction_time
    if stamp is not None:
        stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    return {
        "zone_id": zone.id,
        "kind": zone.kind.value,
        "symbol": zone.symbol,
        "timeframe": zone.timeframe.label,
        "bias": zone.bias.value,
        "status": zone.status.value,
        "state": state,
        "flipped_from": zone.flipped_from,
        "top": zone.top,
        "bottom": zone.bottom,
        "reaction_count": zone.reaction_count,
        "last_reaction_utc": stamp.isoformat() if stamp is not None else None,
        "last_reaction_close": zone.last_reaction_close,
        "evidence": evidence,
    }


def conditional_zone_scenarios(
    zones: Iterable[Zone],
    price: Optional[float],
    atr_by_timeframe: dict[Timeframe, Optional[float]],
    approach_atr_mult: float,
    max_age_bars: int,
    current_index_by_timeframe: dict[Timeframe, int],
) -> list[dict[str, object]]:
    """Describe nearby alternatives without creating a plan or entry signal."""
    scenarios, _ = assess_conditional_zone_scenarios(
        zones, price, atr_by_timeframe, approach_atr_mult, max_age_bars,
        current_index_by_timeframe,
    )
    return scenarios


def assess_conditional_zone_scenarios(
    zones: Iterable[Zone],
    price: Optional[float],
    atr_by_timeframe: dict[Timeframe, Optional[float]],
    approach_atr_mult: float,
    max_age_bars: int,
    current_index_by_timeframe: dict[Timeframe, int],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Return read-only scenarios plus counts explaining why others were omitted."""
    live_zones = [
        zone for zone in zones
        if zone.is_live and zone.bias is not Bias.NEUTRAL
    ]
    if price is None:
        has_live_zones = bool(live_zones)
        return [], {
            "status": "insufficient_context" if has_live_zones else "no_live_zones",
            "live_zone_count": len(live_zones),
            "age_eligible_zone_count": 0,
            "context_eligible_zone_count": 0,
            "outside_distance_zone_count": 0,
            "unknown_age_zone_count": 0,
            "missing_price": has_live_zones,
            "missing_atr_zone_count": 0,
            "approach_atr_mult": approach_atr_mult,
            "reason": "Current market price is unavailable." if has_live_zones else
                      "No live directional zones are available.",
        }
    scenarios: list[dict[str, object]] = []
    age_eligible = 0
    missing_atr = 0
    outside_distance = 0
    unknown_age = 0
    for zone in live_zones:
        current_index = current_index_by_timeframe.get(zone.timeframe)
        if current_index is None or current_index < zone.created_index:
            unknown_age += 1
            continue
        if current_index - zone.created_index > max_age_bars:
            continue
        age_eligible += 1
        atr = atr_by_timeframe.get(zone.timeframe)
        if atr is None or atr <= 0:
            missing_atr += 1
            continue
        distance = max(zone.bottom - price, price - zone.top, 0.0)
        distance_atr = distance / atr
        if distance_atr > approach_atr_mult:
            outside_distance += 1
            continue

        zone_label = "supply" if zone.bias is Bias.BEARISH else "demand"
        rejection_direction = "short" if zone.bias is Bias.BEARISH else "long"
        continuation_direction = "long" if zone.bias is Bias.BEARISH else "short"
        context = zone_context(zone)
        scenarios.append({
            **context,
            "distance_atr": distance_atr,
            "scenario_status": "monitoring",
            "possibilities": [
                {
                    "case": f"{zone_label}_rejection",
                    "direction": rejection_direction,
                    "monitor": (
                        f"Watch for a close-back reaction; {rejection_direction} entry still requires "
                        "its existing entry-model confirmation."
                    ),
                },
                {
                    "case": f"{zone_label}_breakout",
                    "direction": continuation_direction,
                    "monitor": (
                        "Watch for a body close through the zone; reassess only under the existing "
                        "break, flip, and retest rules."
                    ),
                },
            ],
            "entry_confirmation": "Not confirmed by this context assessment.",
        })
    scenarios.sort(key=lambda row: float(row["distance_atr"]))
    if scenarios:
        status = "scenarios_available"
        reason = "Nearby zones have sufficient context and meet the existing monitoring rules."
    elif not live_zones:
        status = "no_live_zones"
        reason = "No live directional zones are available."
    elif unknown_age or missing_atr:
        status = "insufficient_context"
        reason = "Required timeframe age or ATR context is unavailable for some live zones."
    else:
        status = "no_qualifying_nearby_zones"
        reason = "No live zones are within the existing age and approach-distance rules."
    return scenarios, {
        "status": status,
        "live_zone_count": len(live_zones),
        "age_eligible_zone_count": age_eligible,
        "context_eligible_zone_count": age_eligible - missing_atr,
        "outside_distance_zone_count": outside_distance,
        "unknown_age_zone_count": unknown_age,
        "missing_price": False,
        "missing_atr_zone_count": missing_atr,
        "approach_atr_mult": approach_atr_mult,
        "reason": reason,
    }


def plan_market_context(
    plan: Plan,
    zones: Iterable[Zone],
    targets: Optional[list[float]] = None,
) -> dict[str, object]:
    """Summarize source-zone evidence and live opposing zones before target one."""
    all_zones = list(zones)
    source_by_id = {zone.id: zone for zone in (*plan.source_zones, *all_zones)}
    source_ids = plan.zones.zone_ids
    source_context = [
        zone_context(source_by_id[zone_id])
        for zone_id in source_ids
        if zone_id in source_by_id
    ]
    target_list = plan.targets if targets is None else targets
    opposing: list[dict[str, object]] = []
    entry = plan.entry_price
    target = target_list[0] if target_list else None
    for zone in all_zones:
        if not zone.is_live or zone.bias is not plan.direction.opposite.bias:
            continue
        if target is None:
            continue
        if plan.direction is PlanDirection.LONG:
            in_path = target > entry and zone.top >= entry and zone.bottom < target
            beyond_zone = zone.top < target
        else:
            in_path = target < entry and zone.bottom <= entry and zone.top > target
            beyond_zone = zone.bottom > target
        if not in_path:
            continue
        assessment = zone_context(zone)
        assessment["target_one_relation"] = (
            "target_inside_zone" if zone.bottom <= target <= zone.top
            else "target_beyond_zone" if beyond_zone
            else "zone_before_target"
        )
        assessment["implication"] = (
            "The existing first target lies within this opposing zone."
            if assessment["target_one_relation"] == "target_inside_zone"
            else "Price may react at this opposing zone before reaching the existing first target."
        )
        opposing.append(assessment)
    opposing.sort(
        key=lambda item: (
            abs(float(item["bottom"]) - entry)
            if plan.direction is PlanDirection.LONG
            else abs(entry - float(item["top"]))
        )
    )
    if target is None:
        assessment_text = "Opposing-zone context is unavailable because no target is recorded."
    elif opposing:
        assessment_text = (
            "Live opposing zones are noted as context only; they do not automatically reject the plan "
            "or alter its existing targets."
        )
    else:
        assessment_text = "No live opposing zone is recorded between entry and the existing first target."
    if plan.status.value == "ready_to_execute":
        conditional = "Entry zone reached; existing execution and risk validation are still required."
    else:
        conditional = (
            "Conditional plan; entry-zone interaction and existing execution validation are still required."
        )
    return {
        "conditional_scenario": conditional,
        "entry_confirmation": "The existing entry model qualified this plan; this context is not a new signal.",
        "source_zones": source_context,
        "opposing_zones": opposing,
        "assessment": assessment_text,
    }
