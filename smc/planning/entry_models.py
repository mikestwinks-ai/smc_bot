"""The four entry-model evaluators (pure functions of an :class:`EvalContext`).

Each evaluator returns either a :class:`Candidate` (passed EVERY filter of its
model) or a :class:`Discard` carrying a human-readable reason.

Premium / Discount is judged at the ENTRY price (near edge of the entry zone)
inside the dealing range of the layer the zone belongs to (internal zone ->
internal range, external zone -> external range):
  * OB / FVG / OB+FVG : long needs position < 0.48, short needs > 0.52
                        (0.48-0.52 = equilibrium -> discarded, "needs a cleaner" side)
  * Sweep + MSS       : only the hard block applies (long in Premium / short in
                        Discount is refused); equilibrium is allowed.
External bias is NEVER used to reject an internal setup - it is only recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Union

import pandas as pd

from smc.config import PlanningConfig, StructureConfig
from smc.core.timeframes import StructureLayer, Timeframe
from smc.models.plan import (
    EntryModel, EntryRangeBasis, Invalidation, PlanDirection, PlanQuality, PlanZones, PriceRange,
)
from smc.structure.models import (
    Bias, FairValueGap, LiquiditySweep, OrderBlock, PDReading, StructureEvent,
    StructureEventType, ZoneStatus,
)


# ------------------------------------------------------------------ results
@dataclass
class Candidate:
    model: EntryModel
    direction: PlanDirection
    timeframe: Timeframe
    layer: StructureLayer
    entry_zone: PriceRange
    invalidation: Invalidation
    targets: list[float]
    rr: float
    zones: PlanZones
    reason: str
    quality: PlanQuality
    external_range_position: Optional[float]
    internal_range_position: Optional[float]
    entry_range_basis: EntryRangeBasis

    @property
    def priority(self) -> int:
        return self.model.priority


@dataclass(frozen=True)
class Discard:
    model: EntryModel
    reason: str
    zone_id: Optional[str] = None
    silent: bool = False  # structural non-match (e.g. unrelated zones): not worth recording


Evaluation = Union[Candidate, Discard]


# ------------------------------------------------------------------ context
def _no_overlap(_: OrderBlock) -> float:
    return 0.0


@dataclass
class EvalContext:
    """Everything an evaluator may look at. Tests can build one by hand."""

    symbol: str
    timeframe: Timeframe  # timeframe of the zone / setup being evaluated
    layer: StructureLayer
    atr: float
    now: pd.Timestamp
    external_bias: Bias
    internal_bias: Bias
    external_pd: Optional[PDReading]
    internal_pd: Optional[PDReading]
    structure_cfg: StructureConfig
    planning_cfg: PlanningConfig
    find_targets: Callable[[PlanDirection, float], list[float]]
    leg_overlap: Callable[[OrderBlock], float] = _no_overlap
    fvg_stop_anchor: Callable[[FairValueGap], float] = lambda f: f.stop_reference

    @property
    def pd_reading(self) -> Optional[PDReading]:
        return self.external_pd if self.layer is StructureLayer.EXTERNAL else self.internal_pd


# ------------------------------------------------------------------ helpers
def position_at(reading: Optional[PDReading], price: float) -> Optional[float]:
    """position = (price - range_low) / (range_high - range_low), unclamped."""
    if reading is None or reading.range_high <= reading.range_low:
        return None
    return (price - reading.range_low) / (reading.range_high - reading.range_low)


def clamp01(v: Optional[float]) -> Optional[float]:
    return None if v is None else min(max(v, 0.0), 1.0)


def pd_label(pos: float, band: float) -> str:
    if pos > 0.5 + band:
        return "Premium"
    if pos < 0.5 - band:
        return "Discount"
    return "Equilibrium"


def pd_rejection(direction: PlanDirection, pos: Optional[float], band: float, strict: bool,
                 layer: StructureLayer, tolerance: float = 0.10) -> Optional[str]:
    """Return a discard reason if the P/D rule refuses this entry, else None."""
    if pos is None:
        return f"no {layer.value} dealing range available to judge Premium/Discount"
    if pos < -tolerance or pos > 1 + tolerance:
        return (f"entry is outside the current {layer.value} dealing range (position {pos:.2f}); "
                f"the zone is stale relative to present structure")
    label = pd_label(pos, band)
    if direction is PlanDirection.LONG and label == "Premium":
        return f"long entry sits in Premium of the {layer.value} range ({pos:.2f} > {0.5 + band:.2f}) - hard block"
    if direction is PlanDirection.SHORT and label == "Discount":
        return f"short entry sits in Discount of the {layer.value} range ({pos:.2f} < {0.5 - band:.2f}) - hard block"
    if strict and label == "Equilibrium":
        return (f"entry at {pos:.2f} is in {layer.value} Equilibrium ({0.5 - band:.2f}-{0.5 + band:.2f}); "
                f"needs a cleaner {'Discount' if direction is PlanDirection.LONG else 'Premium'}")
    return None


def stop_beyond(direction: PlanDirection, level: float, atr: float, mult: float) -> float:
    buf = mult * atr
    return level - buf if direction is PlanDirection.LONG else level + buf


def reward_risk(direction: PlanDirection, entry: float, stop: float, target: float) -> Optional[float]:
    risk = (entry - stop) if direction is PlanDirection.LONG else (stop - entry)
    reward = (target - entry) if direction is PlanDirection.LONG else (entry - target)
    if risk <= 0 or reward <= 0:
        return None
    return reward / risk


def _direction(bias: Bias) -> PlanDirection:
    return PlanDirection.from_bias(bias)


def _counter_trend(ctx: EvalContext, direction: PlanDirection) -> bool:
    return (ctx.layer is StructureLayer.INTERNAL and ctx.external_bias is not Bias.NEUTRAL
            and ctx.external_bias is not direction.bias)


def _positions(ctx: EvalContext, entry: float) -> tuple[Optional[float], Optional[float]]:
    return (clamp01(position_at(ctx.external_pd, entry)), clamp01(position_at(ctx.internal_pd, entry)))


def _ct_note(ctx: EvalContext, direction: PlanDirection) -> str:
    if _counter_trend(ctx, direction):
        return f"; counter-trend internal {direction.value} allowed vs {ctx.external_bias.value} external bias"
    return ""


# --------------------------------------------------------------- evaluators
class EntryModelEvaluators:
    """Stateless; one instance can serve the whole bot."""

    # ------------------------------------------------------------ 1. Order Block
    def evaluate_ob(self, ctx: EvalContext, ob: OrderBlock) -> Evaluation:
        m, pc, sc = EntryModel.OB, ctx.planning_cfg, ctx.structure_cfg
        if ob.symbol != ctx.symbol or ob.timeframe is not ctx.timeframe:
            return Discard(m, f"OB {ob.id} does not match evaluation symbol/timeframe", ob.id, silent=True)
        if ob.status is not ZoneStatus.ACTIVE:
            return Discard(m, f"OB {ob.id} is {ob.status.value} (must be unmitigated)", ob.id)

        allowed = {StructureEventType.BOS, StructureEventType.MSS}
        if ctx.layer is StructureLayer.EXTERNAL:  # external layer has no MSS: its CHoCH is the reversal
            allowed.add(StructureEventType.CHOCH)
        if ob.origin_event_type not in allowed:
            return Discard(m, f"OB {ob.id} came from a {ob.origin_event_type.value}, not a BOS/MSS", ob.id)

        if ob.displacement_ratio < sc.displacement_atr_mult:
            return Discard(m, f"OB {ob.id} impulse {ob.displacement_ratio:.2f} ATR < {sc.displacement_atr_mult:.2f} "
                              f"- not displacement", ob.id)
        overlap = ctx.leg_overlap(ob)
        if overlap > pc.ob_max_leg_overlap:
            return Discard(m, f"OB {ob.id} impulse is a slow overlapping crawl "
                              f"(mean overlap {overlap:.2f} > {pc.ob_max_leg_overlap:.2f})", ob.id)

        direction = _direction(ob.bias)
        zone = PriceRange(ob.top, ob.bottom)
        entry = zone.near_edge(direction)
        pos = position_at(ctx.pd_reading, entry)
        why = pd_rejection(direction, pos, sc.pd_equilibrium_band, True, ctx.layer, sc.pd_range_tolerance)
        if why:
            return Discard(m, f"OB {ob.id}: {why}", ob.id)

        stop = stop_beyond(direction, zone.far_edge(direction), ctx.atr, pc.stop_buffer_atr_mult)
        targets = ctx.find_targets(direction, entry)
        if not targets:
            return Discard(m, f"OB {ob.id}: no opposite structure target beyond entry {entry:.2f}", ob.id)
        rr = reward_risk(direction, entry, stop, targets[0])
        if rr is None:
            return Discard(m, f"OB {ob.id}: invalid stop/target geometry", ob.id)

        ext_pos, int_pos = _positions(ctx, entry)
        assert pos is not None
        reason = (f"{ctx.timeframe} {direction.value} OB {ob.bottom:.2f}-{ob.top:.2f} from {ob.origin_event_type.value} "
                  f"(impulse {ob.displacement_ratio:.1f} ATR{', left an FVG' if ob.has_fvg else ''}); "
                  f"entry {entry:.2f} in {pd_label(pos, sc.pd_equilibrium_band)} {pos:.2f} of {ctx.layer.value} range"
                  f"{_ct_note(ctx, direction)}")
        return Candidate(
            m, direction, ctx.timeframe, ctx.layer, zone,
            Invalidation(stop, f"body close {'below' if direction is PlanDirection.LONG else 'above'} OB far edge "
                               f"{zone.far_edge(direction):.2f} +/- {pc.stop_buffer_atr_mult:g} ATR"),
            targets, rr, PlanZones(order_block_id=ob.id), reason,
            PlanQuality(has_fvg=ob.has_fvg, impulse_ratio=ob.displacement_ratio,
                        counter_trend_internal=_counter_trend(ctx, direction)),
            ext_pos, int_pos, EntryRangeBasis.ORDER_BLOCK)

    # ------------------------------------------------------------------- 2. FVG
    def evaluate_fvg(self, ctx: EvalContext, fvg: FairValueGap) -> Evaluation:
        m, pc, sc = EntryModel.FVG, ctx.planning_cfg, ctx.structure_cfg
        if fvg.symbol != ctx.symbol or fvg.timeframe is not ctx.timeframe:
            return Discard(m, f"FVG {fvg.id} does not match evaluation symbol/timeframe", fvg.id, silent=True)
        if not fvg.is_live:
            return Discard(m, f"FVG {fvg.id} is {fvg.status.value}", fvg.id)
        if fvg.max_penetration >= 1.0:
            return Discard(m, f"FVG {fvg.id} is fully filled", fvg.id)

        if fvg.height < pc.fvg_min_gap_atr_mult * ctx.atr:
            return Discard(m, f"FVG {fvg.id} is only {fvg.height:.2f} tall (< {pc.fvg_min_gap_atr_mult:g} ATR): "
                              f"not a clean imbalance", fvg.id)
        direction = _direction(fvg.bias)
        zone = PriceRange(fvg.top, fvg.bottom)
        entry = zone.near_edge(direction)  # measured at the near edge of the gap
        pos = position_at(ctx.pd_reading, entry)
        why = pd_rejection(direction, pos, sc.pd_equilibrium_band, True, ctx.layer, sc.pd_range_tolerance)
        if why:
            return Discard(m, f"FVG {fvg.id}: {why}", fvg.id)

        anchor = ctx.fvg_stop_anchor(fvg)  # swing origin / OB that created the move
        stop = stop_beyond(direction, anchor, ctx.atr, pc.stop_buffer_atr_mult)
        targets = ctx.find_targets(direction, entry)
        if not targets:
            return Discard(m, f"FVG {fvg.id}: no opposite structure target beyond entry {entry:.2f}", fvg.id)
        rr = reward_risk(direction, entry, stop, targets[0])
        if rr is None:
            return Discard(m, f"FVG {fvg.id}: invalid stop/target geometry", fvg.id)
        if rr < sc.min_fvg_rr:
            return Discard(m, f"FVG {fvg.id}: RR {rr:.2f} < {sc.min_fvg_rr:.1f} hard minimum "
                              f"(entry {entry:.2f}, stop {stop:.2f}, target {targets[0]:.2f})", fvg.id)

        ext_pos, int_pos = _positions(ctx, entry)
        assert pos is not None
        reason = (f"{ctx.timeframe} {direction.value} FVG {fvg.bottom:.2f}-{fvg.top:.2f}; entry at near edge "
                  f"{entry:.2f} in {pd_label(pos, sc.pd_equilibrium_band)} {pos:.2f} of {ctx.layer.value} range; "
                  f"stop beyond move origin {anchor:.2f}; RR {rr:.1f}{_ct_note(ctx, direction)}")
        return Candidate(
            m, direction, ctx.timeframe, ctx.layer, zone,
            Invalidation(stop, f"body close {'below' if direction is PlanDirection.LONG else 'above'} move origin "
                               f"{anchor:.2f} +/- {pc.stop_buffer_atr_mult:g} ATR"),
            targets, rr, PlanZones(fvg_id=fvg.id), reason,
            PlanQuality(partially_filled=fvg.max_penetration > 0, counter_trend_internal=_counter_trend(ctx, direction)),
            ext_pos, int_pos, EntryRangeBasis.FAIR_VALUE_GAP)

    # ------------------------------------------------------- 3. Sweep + MSS
    def evaluate_sweep_mss(
        self, ctx: EvalContext, sweep: LiquiditySweep, mss: StructureEvent,
        entry_zone: PriceRange, zones: PlanZones, extreme_held: bool, zone_desc: str,
    ) -> Evaluation:
        m, pc, sc = EntryModel.SWEEP_MSS, ctx.planning_cfg, ctx.structure_cfg
        lid = sweep.level_id
        buy_side = sweep.implied_bias is Bias.BEARISH
        # wick beyond the level but body closed back inside; a body close beyond is a break, not a sweep
        if (sweep.close > sweep.level_price) if buy_side else (sweep.close < sweep.level_price):
            return Discard(m, f"candle at {sweep.time} closed beyond {sweep.level_price:.2f}: that is a break, not a sweep", lid)
        if mss.type not in (StructureEventType.MSS, StructureEventType.CHOCH):
            return Discard(m, f"{mss.type.value} at {mss.time} is not an MSS/CHoCH against the swept side", lid)
        if mss.direction is not sweep.implied_bias:
            return Discard(m, f"structure break {mss.direction.value} does not oppose the swept liquidity", lid)
        if mss.time <= sweep.time:
            return Discard(m, "MSS printed before the sweep", lid)
        if not extreme_held:
            return Discard(m, f"price closed/traded beyond the sweep extreme {sweep.extreme:.2f} after the sweep", lid)

        direction = _direction(sweep.implied_bias)
        entry = entry_zone.near_edge(direction)
        pos = position_at(ctx.pd_reading, entry)
        why = pd_rejection(direction, pos, sc.pd_equilibrium_band, False, ctx.layer, sc.pd_range_tolerance)
        if why:
            return Discard(m, f"sweep {lid}: {why}", lid)

        stop = stop_beyond(direction, sweep.extreme, ctx.atr, pc.stop_buffer_atr_mult)
        targets = ctx.find_targets(direction, entry)
        if not targets:
            return Discard(m, f"sweep {lid}: no next structure target beyond entry {entry:.2f}", lid)
        rr = reward_risk(direction, entry, stop, targets[0])
        if rr is None or rr < pc.min_sweep_rr:
            got = "n/a" if rr is None else f"{rr:.2f}"
            return Discard(m, f"sweep {lid}: MSS too extended - stop beyond sweep extreme {stop:.2f} gives RR {got} "
                              f"< {pc.min_sweep_rr:.1f} to next structure {targets[0]:.2f} (chasing)", lid)

        ext_pos, int_pos = _positions(ctx, entry)
        pos_txt = "n/a" if pos is None else f"{pos:.2f}"
        reason = (f"{ctx.timeframe} {direction.value}: {sweep.kind.value} {sweep.level_price:.2f} swept (wick to "
                  f"{sweep.extreme:.2f}, closed back inside) then {mss.type.value} at {mss.time:%m-%d %H:%M}; "
                  f"entry {zone_desc} {entry:.2f}, P/D {pos_txt}; RR {rr:.1f}{_ct_note(ctx, direction)}")
        return Candidate(
            m, direction, ctx.timeframe, ctx.layer, entry_zone,
            Invalidation(stop, f"body close {'below' if direction is PlanDirection.LONG else 'above'} sweep extreme "
                               f"{sweep.extreme:.2f} +/- {pc.stop_buffer_atr_mult:g} ATR"),
            targets, rr, zones, reason,
            PlanQuality(has_fvg=zones.fvg_id is not None, impulse_ratio=mss.displacement_ratio,
                        counter_trend_internal=_counter_trend(ctx, direction)),
            ext_pos, int_pos,
            EntryRangeBasis.SWEEP_MSS_ORDER_BLOCK if zones.order_block_id else
            EntryRangeBasis.SWEEP_MSS_FVG if zones.fvg_id else
            EntryRangeBasis.SWEEP_MSS_RETRACEMENT)

    # ------------------------------------------------------- 4. OB + FVG
    def evaluate_ob_fvg(self, ctx: EvalContext, ob: OrderBlock, fvg: FairValueGap) -> Evaluation:
        m, pc, sc = EntryModel.OB_FVG, ctx.planning_cfg, ctx.structure_cfg
        if (ob.symbol != fvg.symbol or ob.timeframe is not fvg.timeframe
                or ob.symbol != ctx.symbol or ob.timeframe is not ctx.timeframe):
            return Discard(m, f"OB {ob.id} and FVG {fvg.id} do not share symbol/timeframe context",
                           ob.id, silent=True)
        if ob.bias is not fvg.bias:
            return Discard(m, f"OB {ob.id} and FVG {fvg.id} point opposite ways", ob.id, silent=True)
        if ob.leg_start_index is None or not ob.leg_start_index <= fvg.created_index <= ob.created_index:
            return Discard(m, f"FVG {fvg.id} is outside OB {ob.id}'s displacement leg", ob.id, silent=True)
        gap = max(fvg.bottom - ob.top, ob.bottom - fvg.top, 0.0)  # 0 when they overlap
        if gap > pc.confluence_adjacent_atr_mult * ctx.atr:
            return Discard(m, f"FVG {fvg.id} is {gap:.2f} away from OB {ob.id}: not inside/adjacent "
                              f"(limit {pc.confluence_adjacent_atr_mult:g} ATR)", ob.id, silent=True)

        ob_res, fvg_res = self.evaluate_ob(ctx, ob), self.evaluate_fvg(ctx, fvg)  # each must pass alone
        if isinstance(ob_res, Discard):
            return Discard(m, f"OB leg of confluence fails: {ob_res.reason}", ob.id)
        if isinstance(fvg_res, Discard):
            return Discard(m, f"FVG leg of confluence fails: {fvg_res.reason}", ob.id)

        direction = ob_res.direction
        zone = PriceRange(max(ob.top, fvg.top), min(ob.bottom, fvg.bottom))
        entry = zone.near_edge(direction)
        pos = position_at(ctx.pd_reading, entry)
        why = pd_rejection(direction, pos, sc.pd_equilibrium_band, True, ctx.layer, sc.pd_range_tolerance)
        if why:
            return Discard(m, f"combined zone: {why}", ob.id)
        # widest (safest) of the two stops, measured from the combined entry
        stops = [ob_res.invalidation.price, fvg_res.invalidation.price]
        stop = min(stops) if direction is PlanDirection.LONG else max(stops)
        targets = ctx.find_targets(direction, entry)
        if not targets:
            return Discard(m, f"combined zone: no opposite structure target beyond entry {entry:.2f}", ob.id)
        rr = reward_risk(direction, entry, stop, targets[0])
        if rr is None or rr < sc.min_fvg_rr:
            got = "n/a" if rr is None else f"{rr:.2f}"
            return Discard(m, f"combined OB+FVG RR {got} < {sc.min_fvg_rr:.1f} hard minimum", ob.id)

        ext_pos, int_pos = _positions(ctx, entry)
        assert pos is not None
        reason = (f"{ctx.timeframe} {direction.value} OB+FVG confluence {zone.bottom:.2f}-{zone.top:.2f}: "
                  f"OB ({ob.origin_event_type.value}, {ob.displacement_ratio:.1f} ATR) with FVG "
                  f"{fvg.bottom:.2f}-{fvg.top:.2f}; entry {entry:.2f} in {pd_label(pos, sc.pd_equilibrium_band)} "
                  f"{pos:.2f}; RR {rr:.1f}{_ct_note(ctx, direction)}")
        return Candidate(
            m, direction, ctx.timeframe, ctx.layer, zone,
            Invalidation(stop, f"body close {'below' if direction is PlanDirection.LONG else 'above'} the wider of "
                               f"OB / FVG stops ({stop:.2f})"),
            targets, rr, PlanZones(order_block_id=ob.id, fvg_id=fvg.id), reason,
            PlanQuality(has_fvg=True, confluence=True, impulse_ratio=ob.displacement_ratio,
                        partially_filled=fvg.max_penetration > 0,
                        counter_trend_internal=_counter_trend(ctx, direction)),
            ext_pos, int_pos, EntryRangeBasis.OB_FVG_CONFLUENCE)
