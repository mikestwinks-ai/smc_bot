"""The Adaptive Plan Manager - Phase 3's decision loop.

Call order per new confirmed candle (the caller feeds timeframes highest first,
so External is updated before Internal):

  1. ``StructureHub.update`` refreshes structure, zones, liquidity and P/D
     (``run_candles`` does this for you, then calls ``on_update``).
  2. Existing plans are checked:
        a. a plan's zone broken by a body close -> plan FLIPPED, opposite retest
           plan created (parent_plan_id links them)
        b. invalidation broken by a body close on the plan's setup timeframe
           -> plan CANCELED with a reason
        c. expired plans are canceled; survivors are refreshed (biases, range
           positions, targets, RR) and progress PLANNED -> ACTIVE -> READY_TO_EXECUTE
  3. New candidates are scanned on the scan timeframes with all four models.
  4. A candidate becomes a plan only if it passes every filter of its model.
  5. A candidate sharing a zone with a live same-direction plan never creates a
     duplicate: the existing plan is upgraded (higher-priority model) or left alone.
  6. Every create / update / cancel / flip is logged with a short structural note.

Tie-break: if one candle both breaks a plan's zone AND its invalidation, the FLIP
wins (it carries more information: the opposite retest plan).
"""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import pandas as pd

from smc.config import BotConfig
from smc.core.timeframes import Timeframe
from smc.models.plan import (
    EntryModel, EntryRangeBasis, Invalidation, Plan, PlanDirection, PlanQuality, PlanStatus,
    PlanZones, PriceRange, SourceTimeframes,
)
from smc.planning.context import StructureView
from smc.planning.entry_models import (
    Candidate, Discard, EntryModelEvaluators, EvalContext, clamp01, pd_label, pd_rejection,
    position_at, reward_risk, stop_beyond,
)
from smc.planning.plan_store import PlanStore
from smc.planning.market_context import assess_conditional_zone_scenarios, plan_market_context
from smc.structure.engine import StructureHub
from smc.structure.models import (
    Bias, FairValueGap, FlipSignal, LiquiditySide, LiquiditySweep, OrderBlock, StructureEvent,
    StructureEventType, StructureUpdate, Zone, ZoneKind, ZoneStatus,
)
from smc.utils.logger import get_logger

log = get_logger(__name__)


class ChangeKind(Enum):
    CREATED = "created"
    UPGRADED = "upgraded"
    UPDATED = "updated"
    STATUS = "status"
    CANCELED = "canceled"
    FLIPPED = "flipped"


@dataclass(frozen=True)
class PlanChange:
    kind: ChangeKind
    time: pd.Timestamp
    plan_id: str
    note: str


@dataclass(frozen=True)
class DiscardRecord:
    time: pd.Timestamp
    symbol: str
    timeframe: Timeframe
    model: EntryModel
    ref_id: Optional[str]
    reason: str


class PlanManager:
    def __init__(self, config: BotConfig, hub: StructureHub, store: Optional[PlanStore] = None) -> None:
        self._cfg, self.hub = config, hub
        self.store = store or PlanStore()
        self.view = StructureView(hub, config)
        self.evaluators = EntryModelEvaluators()
        self.discards: deque[DiscardRecord] = deque(maxlen=config.planning.max_discard_records)
        self._discard_seen: dict[tuple[str, str], str] = {}
        self._sweeps: dict[str, list[LiquiditySweep]] = {}
        self._pairs_done: set[tuple[str, str]] = set()
        self._retired: set[tuple[str, PlanDirection]] = set()  # (zone id, direction) already planned & ended
        self._recent_structure_events: deque[dict[str, object]] = deque(maxlen=200)
        self._structure_event_keys: set[str] = set()
        self._pending_structure_events: deque[dict[str, object]] = deque(maxlen=500)

    # ---------------------------------------------------------------- entry points
    def run_candles(self, symbol: str, tf: Timeframe, candles: pd.DataFrame) -> list[PlanChange]:
        """Step 1 + loop: update the structure engine, then run the plan loop."""
        return self.on_update(self.hub.update(symbol, tf, candles))

    def initialize(self, data) -> None:
        """Replay stored history from a MarketDataManager (warm-up; creates no plans)."""
        self.hub.initialize(data)

    def on_candle_events(self, events, data) -> list[PlanChange]:
        """Run the plan loop for every newly closed candle announced by MarketDataManager.update_all()."""
        out: list[PlanChange] = []
        for ev in events:  # update_all() yields the highest timeframe first
            out += self.run_candles(ev.symbol, ev.timeframe, data.get_candles(ev.symbol, ev.timeframe, ev.new_candles + 3))
        return out

    def bootstrap(self, symbols: list[str]) -> list[PlanChange]:
        """After history replay: scan the CURRENT state once (no plans come from replayed history)."""
        changes: list[PlanChange] = []
        for symbol in symbols:
            for tf in self._cfg.planning.scan_timeframes:
                t = self.view.tracker(symbol, tf)
                if t is not None and len(t.buffer):
                    changes += self._scan(symbol, tf, t.buffer.time(t.buffer.last_index))
        return changes

    def on_update(self, upd: StructureUpdate) -> list[PlanChange]:
        self._capture_structure_events(upd)
        self._ingest_sweeps(upd)
        if upd.is_warmup or upd.candles_processed == 0:
            return []
        tracker = self.view.tracker(upd.symbol, upd.timeframe)
        if tracker is None:
            return []
        now = tracker.buffer.time(tracker.buffer.last_index)
        changes: list[PlanChange] = []
        changes += self._handle_flips(upd, now)
        changes += self._check_invalidation(upd, tracker, now)
        changes += self._expire(upd, now)
        changes += self._refresh_and_progress(upd, tracker, now)
        if upd.timeframe in self._cfg.planning.scan_timeframes:
            changes += self._scan(upd.symbol, upd.timeframe, now)
        return changes

    def recent_structure_events(self) -> list[dict[str, object]]:
        return [dict(event) for event in self._recent_structure_events]

    def conditional_scenarios(self, symbol: str) -> list[dict[str, object]]:
        """Return nearby zone alternatives without creating or changing trade plans."""
        scenarios, _ = self.conditional_scenario_assessment(symbol)
        return scenarios

    def conditional_scenario_assessment(
        self, symbol: str,
    ) -> tuple[list[dict[str, object]], dict[str, object]]:
        """Return scenarios and a concise explanation of the current evaluation."""
        state = self.hub.snapshot(symbol)
        atr_by_timeframe: dict[Timeframe, Optional[float]] = {}
        current_index_by_timeframe: dict[Timeframe, int] = {}
        for zone in self.hub.zones.all_for(symbol):
            tracker = self.hub.get_tracker(symbol, zone.timeframe)
            if tracker is not None and len(tracker.buffer):
                atr_by_timeframe[zone.timeframe] = tracker.atr
                current_index_by_timeframe[zone.timeframe] = tracker.buffer.last_index
        return assess_conditional_zone_scenarios(
            self.hub.zones.all_for(symbol),
            state.price,
            atr_by_timeframe,
            self._cfg.planning.approach_atr_mult,
            self._cfg.planning.zone_scan_max_age_bars,
            current_index_by_timeframe,
        )

    def drain_structure_events(self) -> list[dict[str, object]]:
        events = list(self._pending_structure_events)
        self._pending_structure_events.clear()
        return events

    def _capture_structure_events(self, upd: StructureUpdate) -> None:
        if upd.is_warmup:
            return

        def add(event_type: str, time: pd.Timestamp, entity_id: str, status: str, note: str, **details) -> None:
            stamp = time.tz_localize("UTC") if time.tzinfo is None else time.tz_convert("UTC")
            key_material = f"{event_type}|{upd.symbol}|{upd.timeframe.label}|{stamp.isoformat()}|{entity_id}"
            event_key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
            if event_key in self._structure_event_keys:
                return
            event: dict[str, object] = {
                "event_key": event_key,
                "occurred_at_utc": stamp.isoformat(),
                "event_type": event_type,
                "symbol": upd.symbol,
                "timeframe": upd.timeframe.label,
                "entity_id": entity_id,
                "status": status,
                "note": note,
                "details": details,
            }
            if len(self._recent_structure_events) == self._recent_structure_events.maxlen:
                expired = self._recent_structure_events.popleft()
                self._structure_event_keys.discard(str(expired["event_key"]))
            self._recent_structure_events.append(event)
            self._structure_event_keys.add(event_key)
            self._pending_structure_events.append(event)

        for structure_event in upd.events:
            add(
                "structure_break",
                structure_event.time,
                structure_event.id,
                structure_event.type.value.lower(),
                f"{structure_event.type.value} {structure_event.direction.value} break at "
                f"{structure_event.level:g}",
                direction=structure_event.direction.value,
                structure_type=structure_event.type.value,
                level=structure_event.level,
                close=structure_event.close,
                bias_before=structure_event.bias_before.value,
                bias_after=structure_event.bias_after.value,
            )

        for zone in (*upd.new_order_blocks, *upd.new_fvgs):
            add(
                "structure_zone_created",
                zone.created_time,
                zone.id,
                "created",
                f"New {zone.kind.value} detected",
                zone_id=zone.id,
                zone_kind=zone.kind.value,
                bias=zone.bias.value,
                top=zone.top,
                bottom=zone.bottom,
            )

        for signal in upd.flip_signals:
            status = "flipped" if signal.new_zone_id else "invalidated"
            verb = "flipped" if signal.new_zone_id else "invalidated"
            add(
                f"structure_zone_{status}",
                signal.time,
                signal.old_zone_id,
                status,
                f"{signal.kind.value} {signal.old_zone_id} {verb}"
                + (f" to {signal.new_zone_id}" if signal.new_zone_id else ""),
                zone_id=signal.old_zone_id,
                new_zone_id=signal.new_zone_id,
                zone_kind=signal.kind.value,
                old_bias=signal.old_bias.value,
                new_bias=signal.new_bias.value if signal.new_bias else None,
                top=signal.top,
                bottom=signal.bottom,
                close=signal.close,
            )

        for zone in upd.mitigated_zones:
            add(
                "structure_zone_mitigated",
                zone.mitigated_time or zone.created_time,
                zone.id,
                "mitigated",
                f"{zone.kind.value} {zone.id} mitigated",
                zone_id=zone.id,
                zone_kind=zone.kind.value,
                bias=zone.bias.value,
                top=zone.top,
                bottom=zone.bottom,
            )

        for sweep in upd.liquidity_sweeps:
            add(
                "liquidity_sweep",
                sweep.time,
                sweep.level_id,
                "swept",
                f"{sweep.side.value} liquidity swept at {sweep.level_price:g}",
                level_id=sweep.level_id,
                kind=sweep.kind.value,
                side=sweep.side.value,
                level_price=sweep.level_price,
                extreme=sweep.extreme,
                close=sweep.close,
                swept_level_ids=list(sweep.swept_level_ids),
            )

    # -------------------------------------------------------------------- sweeps
    def _ingest_sweeps(self, upd: StructureUpdate) -> None:
        if not upd.liquidity_sweeps:
            return
        bucket = self._sweeps.setdefault(upd.symbol, [])
        bucket.extend(upd.liquidity_sweeps)
        del bucket[:-60]

    # ---------------------------------------------------------------------- flips
    def _handle_flips(self, upd: StructureUpdate, now: pd.Timestamp) -> list[PlanChange]:
        affected: dict[str, list[FlipSignal]] = {}
        for sig in upd.flip_signals:
            if sig.is_historical:
                continue
            for plan in self.store.depending_on_zone(sig.symbol, sig.old_zone_id):
                affected.setdefault(plan.plan_id, []).append(sig)
        changes: list[PlanChange] = []
        for pid, sigs in affected.items():
            changes += self._flip_plan(self.store.get(pid), sigs, now)
        return changes

    def _flip_plan(self, plan: Plan, sigs: list[FlipSignal], now: pd.Timestamp) -> list[PlanChange]:
        first = sigs[0]
        broken = ", ".join(f"{s.kind.value} {s.old_zone_id}" for s in sigs)
        self._retire(plan)
        new_zones = [z for z in (self.hub.zones.get(s.new_zone_id) for s in sigs if s.new_zone_id) if z]

        if not new_zones:  # flip limit reached: nothing to retest
            reason = (f"{broken} broken by body close {first.close:.2f}; zone flip limit reached, "
                      f"no opposite retest - plan canceled")
            self.store.cancel(plan.plan_id, reason, now)
            log.info("PLAN CANCELED %s | %s", plan.plan_id, reason)
            return [PlanChange(ChangeKind.CANCELED, now, plan.plan_id, reason)]

        child, why_not = self._plan_from_flip(plan, new_zones, now)
        tag = "supply" if new_zones[0].bias is Bias.BEARISH else "demand"
        flip_reason = (f"{broken} broken by body close {first.close:.2f} against the {plan.direction.value} plan -> "
                       f"zone flipped to {tag} {min(z.bottom for z in new_zones):.2f}-{max(z.top for z in new_zones):.2f}")
        flip_reason += (f"; opposite {child.direction.value} retest plan {child.plan_id} created" if child
                        else f"; no retest plan created ({why_not})")
        self.store.flip(plan.plan_id, flip_reason, now, child)
        log.info("PLAN FLIPPED %s | %s", plan.plan_id, flip_reason)
        out = [PlanChange(ChangeKind.FLIPPED, now, plan.plan_id, flip_reason)]
        if child:
            child.log(now, "created", child.reason)
            log.info("PLAN CREATED %s | %s | %s", child.plan_id, child.summary(), child.reason)
            out.append(PlanChange(ChangeKind.CREATED, now, child.plan_id, child.reason))
        return out

    def _plan_from_flip(self, old: Plan, zones: list[Zone], now: pd.Timestamp) -> tuple[Optional[Plan], str]:
        """Build the opposite retest plan. Returns (plan, '') or (None, human reason)."""
        pc, sc = self._cfg.planning, self._cfg.structure
        tf = zones[0].timeframe
        ctx = self.view.build(old.symbol, tf, now)
        if ctx is None:
            return None, "not enough data to size the retest"
        direction = PlanDirection.from_bias(zones[0].bias)
        union = PriceRange(max(z.top for z in zones), min(z.bottom for z in zones))
        has_ob = any(z.kind is ZoneKind.ORDER_BLOCK for z in zones)
        has_fvg = any(z.kind is ZoneKind.FVG for z in zones)
        model = EntryModel.OB_FVG if has_ob and has_fvg else (EntryModel.OB if has_ob else EntryModel.FVG)
        entry = union.near_edge(direction)
        pos = position_at(ctx.pd_reading, entry)
        if pc.flip_plans_respect_pd:
            why = pd_rejection(direction, pos, sc.pd_equilibrium_band, False, ctx.layer, sc.pd_range_tolerance)
            if why:
                return None, why
        stop = stop_beyond(direction, union.far_edge(direction), ctx.atr, pc.stop_buffer_atr_mult)
        targets = ctx.find_targets(direction, entry)
        if not targets:
            return None, f"no opposite structure target beyond {entry:.2f}"
        rr = reward_risk(direction, entry, stop, targets[0])
        if rr is None:
            return None, "invalid stop/target geometry"
        if has_fvg and rr < sc.min_fvg_rr:
            return None, f"RR {rr:.2f} < {sc.min_fvg_rr:.1f} hard minimum for FVG entries"

        ids = {z.kind: z.id for z in zones}
        pz = PlanZones(order_block_id=ids.get(ZoneKind.ORDER_BLOCK), fvg_id=ids.get(ZoneKind.FVG),
                       flipped_zone_id=ids.get(ZoneKind.ORDER_BLOCK) or ids.get(ZoneKind.FVG))
        if (dupe := self.store.find_by_zones(old.symbol, direction, pz.zone_ids)) is not None:
            return None, f"already covered by {dupe.plan_id}"
        label = pd_label(pos, sc.pd_equilibrium_band) if pos is not None else "n/a"
        reason = (f"retest of flipped {'demand' if direction is PlanDirection.LONG else 'supply'} "
                  f"{union.bottom:.2f}-{union.top:.2f} (was the {old.direction.value} plan's zone); "
                  f"entry {entry:.2f} in {label} of {ctx.layer.value} range; RR {rr:.1f}")
        ext_pos = clamp01(position_at(ctx.external_pd, entry))
        int_pos = clamp01(position_at(ctx.internal_pd, entry))
        plan = Plan(
            plan_id=self.store.next_id(old.symbol), symbol=old.symbol, direction=direction, entry_model=model,
            source_timeframes=SourceTimeframes(self._cfg.timeframes.external, self._cfg.timeframes.internal, tf),
            external_bias=ctx.external_bias, internal_bias=ctx.internal_bias, zones=pz, entry_zone=union,
            invalidation=Invalidation(stop, f"body close {'below' if direction is PlanDirection.LONG else 'above'} "
                                            f"flipped zone far edge {union.far_edge(direction):.2f}"),
            targets=targets, rr_estimate=rr, reason=reason, created_at=now, updated_at=now,
            external_range_position=ext_pos, internal_range_position=int_pos,
            quality=PlanQuality(has_fvg=has_fvg, confluence=has_ob and has_fvg),
            parent_plan_id=old.plan_id,
            entry_range_basis=EntryRangeBasis.FLIPPED_ZONE_RETEST,
            source_zones=tuple(zones))
        plan.market_context = plan_market_context(plan, self.hub.zones.all_for(plan.symbol))
        return plan, ""

    # --------------------------------------------------------------- invalidation
    def _check_invalidation(self, upd: StructureUpdate, tracker, now: pd.Timestamp) -> list[PlanChange]:
        buf = tracker.buffer
        first = buf.last_index - upd.candles_processed + 1
        out: list[PlanChange] = []
        for plan in self.store.list_active(upd.symbol):
            if plan.setup_timeframe is not upd.timeframe:
                continue
            if plan.zones.fvg_id:
                zone = self.hub.zones.get(plan.zones.fvg_id)
                if isinstance(zone, FairValueGap) and zone.max_penetration >= 1.0:
                    filled_at = zone.fully_filled_time or now
                    reason = f"FVG {zone.id} fully filled; entry thesis is no longer valid"
                    self._retire(plan)
                    self.store.cancel(plan.plan_id, reason, filled_at)
                    log.info("PLAN CANCELED %s | %s", plan.plan_id, reason)
                    out.append(PlanChange(ChangeKind.CANCELED, filled_at, plan.plan_id, reason))
                    continue
            for idx in range(max(first, buf.base), buf.last_index + 1):
                t, close = buf.time(idx), buf.close(idx)
                if t <= plan.created_at or not plan.is_invalidated_by_close(close):
                    continue
                reason = (f"invalidated: body close {close:.2f} {'below' if plan.direction is PlanDirection.LONG else 'above'} "
                          f"{plan.invalidation.price:.2f} ({plan.invalidation.basis})")
                self._retire(plan)
                self.store.cancel(plan.plan_id, reason, t)
                log.info("PLAN CANCELED %s | %s", plan.plan_id, reason)
                out.append(PlanChange(ChangeKind.CANCELED, t, plan.plan_id, reason))
                break
        return out

    def _expire(self, upd: StructureUpdate, now: pd.Timestamp) -> list[PlanChange]:
        limit = self._cfg.planning.plan_expiry_bars
        out: list[PlanChange] = []
        for plan in self.store.list_active(upd.symbol):
            if plan.setup_timeframe is not upd.timeframe or plan.status is PlanStatus.READY_TO_EXECUTE:
                continue
            age = (now - plan.created_at) / upd.timeframe.delta
            if age > limit:
                reason = f"expired: {age:.0f} bars on {upd.timeframe} without the entry zone being reached (limit {limit})"
                self._retire(plan)
                self.store.cancel(plan.plan_id, reason, now)
                log.info("PLAN CANCELED %s | %s", plan.plan_id, reason)
                out.append(PlanChange(ChangeKind.CANCELED, now, plan.plan_id, reason))
        return out

    # ------------------------------------------------------------ refresh/progress
    def _refresh_and_progress(self, upd: StructureUpdate, tracker, now: pd.Timestamp) -> list[PlanChange]:
        buf = tracker.buffer
        h, l, c = buf.high(buf.last_index), buf.low(buf.last_index), buf.close(buf.last_index)
        out: list[PlanChange] = []
        for plan in self.store.list_active(upd.symbol):
            if plan.setup_timeframe is upd.timeframe:
                out += self._refresh(plan, h, l, now)
            elif plan.is_live:
                context = plan_market_context(plan, self.hub.zones.all_for(plan.symbol))
                if context != plan.market_context:
                    note = f"market context updated: {context['assessment']}"
                    self.store.update(
                        plan.plan_id, now, note, market_context=context,
                    )
                    out.append(PlanChange(ChangeKind.UPDATED, now, plan.plan_id, note))
            if not plan.is_live or now <= plan.created_at:
                continue
            if plan.status is not PlanStatus.READY_TO_EXECUTE and plan.entry_zone.touched_by(plan.direction, h, l):
                out.append(self._set_status(plan, PlanStatus.READY_TO_EXECUTE, now,
                                            f"price traded into entry zone {plan.entry_zone.bottom:.2f}-{plan.entry_zone.top:.2f}"))
            elif plan.status is PlanStatus.PLANNED:
                st = self.view.tracker(plan.symbol, plan.setup_timeframe)
                atr = st.atr if st and st.atr else tracker.atr
                if atr and abs(c - plan.entry_price) <= self._cfg.planning.approach_atr_mult * atr:
                    out.append(self._set_status(plan, PlanStatus.ACTIVE, now,
                                                f"price {c:.2f} within {self._cfg.planning.approach_atr_mult:g} ATR of entry {plan.entry_price:.2f}"))
        return out

    def _set_status(self, plan: Plan, status: PlanStatus, now: pd.Timestamp, note: str) -> PlanChange:
        self.store.set_status(plan.plan_id, status, now, note)
        log.info("PLAN %s %s -> %s | %s", plan.plan_id, "status", status.value, note)
        return PlanChange(ChangeKind.STATUS, now, plan.plan_id, f"{status.value}: {note}")

    def _refresh(self, plan: Plan, high: float, low: float, now: pd.Timestamp) -> list[PlanChange]:
        """Keep biases, range positions and targets current as price develops."""
        ctx = self.view.build(plan.symbol, plan.setup_timeframe, now)
        if ctx is None:
            return []
        notes: list[str] = []
        fields: dict = {}
        if plan.external_bias is not ctx.external_bias or plan.internal_bias is not ctx.internal_bias:
            notes.append(f"bias ext {plan.external_bias.value}->{ctx.external_bias.value}, "
                         f"int {plan.internal_bias.value}->{ctx.internal_bias.value}")
            fields.update(external_bias=ctx.external_bias, internal_bias=ctx.internal_bias)
        entry = plan.entry_price
        ext_pos, int_pos = clamp01(position_at(ctx.external_pd, entry)), clamp01(position_at(ctx.internal_pd, entry))
        fields.update(external_range_position=ext_pos, internal_range_position=int_pos)  # silent refresh

        reached = (lambda t: high >= t) if plan.direction is PlanDirection.LONG else (lambda t: low <= t)
        targets = [t for t in plan.targets if not reached(t)]
        if len(targets) != len(plan.targets):
            notes.append(f"target(s) {[t for t in plan.targets if reached(t)]} reached")
        if not targets:
            targets = ctx.find_targets(plan.direction, entry)
            if targets:
                notes.append(f"targets re-pointed to {targets[0]:.2f}")
        if targets != plan.targets:
            rr = reward_risk(plan.direction, entry, plan.invalidation.price, targets[0]) if targets else None
            fields.update(targets=targets, rr_estimate=rr)
            if rr is not None:
                notes.append(f"RR now {rr:.2f}")
            hard = self._cfg.structure.min_fvg_rr
            if plan.entry_model in (EntryModel.FVG, EntryModel.OB_FVG) and (rr is None or rr < hard):
                reason = f"RR fell to {'n/a' if rr is None else f'{rr:.2f}'} < {hard:.1f} hard minimum after structure moved"
                self._retire(plan)
                self.store.cancel(plan.plan_id, reason, now)
                log.info("PLAN CANCELED %s | %s", plan.plan_id, reason)
                return [PlanChange(ChangeKind.CANCELED, now, plan.plan_id, reason)]
        market_context = plan_market_context(
            plan,
            self.hub.zones.all_for(plan.symbol),
            targets=fields.get("targets", plan.targets),
        )
        if market_context != plan.market_context:
            fields["market_context"] = market_context
            notes.append(f"market context updated: {market_context['assessment']}")
        if not notes:  # nothing noteworthy: refresh quietly, no history entry
            for k, v in fields.items():
                setattr(plan, k, v)
            return []
        self.store.update(plan.plan_id, now, "; ".join(notes), **fields)
        log.info("PLAN UPDATED %s | %s", plan.plan_id, "; ".join(notes))
        return [PlanChange(ChangeKind.UPDATED, now, plan.plan_id, "; ".join(notes))]

    # ------------------------------------------------------------------------ scan
    def _scan(self, symbol: str, tf: Timeframe, now: pd.Timestamp) -> list[PlanChange]:
        ctx = self.view.build(symbol, tf, now)
        tracker = self.view.tracker(symbol, tf)
        if ctx is None or tracker is None:
            return []
        floor = tracker.buffer.last_index - self._cfg.planning.zone_scan_max_age_bars
        obs = [z for z in self.hub.zones.order_blocks(symbol, tf)
               if z.status is ZoneStatus.ACTIVE and z.created_index >= floor]
        fvgs = [z for z in self.hub.zones.fvgs(symbol, tf)
                if z.is_live and z.max_penetration < 1.0 and z.created_index >= floor]

        candidates: list[Candidate] = []
        used: set[str] = set()
        for ob in obs:  # model 4 first (highest priority)
            for fvg in fvgs:
                if ob.bias is not fvg.bias:
                    continue
                res = self.evaluators.evaluate_ob_fvg(ctx, ob, fvg)
                if isinstance(res, Candidate):
                    candidates.append(res)
                    used |= {ob.id, fvg.id}
                else:
                    self._note_discard(now, symbol, tf, res)
        for ob in obs:  # model 1
            if ob.id in used or self._covered(symbol, ob):
                continue
            self._collect(candidates, self.evaluators.evaluate_ob(ctx, ob), now, symbol, tf)
        for fvg in fvgs:  # model 2
            if fvg.id in used or self._covered(symbol, fvg):
                continue
            self._collect(candidates, self.evaluators.evaluate_fvg(ctx, fvg), now, symbol, tf)
        candidates += self._scan_sweeps(ctx, symbol, tf, tracker, now)  # model 3

        changes: list[PlanChange] = []
        for cand in sorted(candidates, key=lambda c: -c.priority):
            changes += self._apply_candidate(cand, ctx, now)
        return changes

    def _covered(self, symbol: str, zone: Zone) -> bool:
        """A live same-direction plan already depends on this zone (nothing new to evaluate)."""
        return self.store.find_by_zones(symbol, PlanDirection.from_bias(zone.bias), (zone.id,)) is not None

    def _collect(self, out: list[Candidate], res, now, symbol, tf) -> None:
        if isinstance(res, Candidate):
            out.append(res)
        else:
            self._note_discard(now, symbol, tf, res)

    def _note_discard(self, now: pd.Timestamp, symbol: str, tf: Timeframe, d: Discard) -> None:
        if d.silent:
            return
        key = (d.zone_id or "-", d.model.value)
        if self._discard_seen.get(key) == d.reason:
            return
        self._discard_seen[key] = d.reason
        self.discards.append(DiscardRecord(now, symbol, tf, d.model, d.zone_id, d.reason))
        log.debug("DISCARD %s %s %s | %s", symbol, tf, d.model.value, d.reason)

    # ------------------------------------------------------------- sweep + MSS scan
    def _scan_sweeps(self, ctx: EvalContext, symbol: str, tf: Timeframe, tracker, now: pd.Timestamp) -> list[Candidate]:
        pc = self._cfg.planning
        buf = tracker.buffer
        out: list[Candidate] = []
        sweeps = [s for s in self._sweeps.get(symbol, []) if s.timeframe.minutes >= tf.minutes
                  and (now - s.time) <= pc.sweep_max_age_bars * s.timeframe.delta]
        if not sweeps:
            return out
        recent = [e for e in tracker.events[-12:] if e.type in (StructureEventType.MSS, StructureEventType.CHOCH)
                  and e.index >= buf.last_index - pc.sweep_max_age_bars * 4]
        for ev in recent:
            match = [s for s in sweeps if s.implied_bias is ev.direction and s.time < ev.time
                     and (ev.time - s.time) <= pc.mss_max_bars_after_sweep * s.timeframe.delta]
            if not match:
                continue
            sw = max(match, key=lambda s: s.time)  # newest qualifying sweep
            pair = (f"{sw.level_id}@{sw.time}", ev.id)
            if pair in self._pairs_done:
                continue
            self._pairs_done.add(pair)
            zone, zones, desc = self._sweep_entry_zone(tracker, sw, ev)
            held = self._extreme_held(tracker, sw)
            res = self.evaluators.evaluate_sweep_mss(ctx, sw, ev, zone, zones, held, desc)
            self._collect(out, res, now, symbol, tf)
        return out

    @staticmethod
    def _extreme_held(tracker, sw: LiquiditySweep) -> bool:
        buf = tracker.buffer
        for k in range(buf.last_index, buf.base - 1, -1):
            if buf.time(k) < sw.time:
                break
            if sw.side is LiquiditySide.BUY_SIDE and buf.high(k) > sw.extreme + 1e-9:
                return False
            if sw.side is LiquiditySide.SELL_SIDE and buf.low(k) < sw.extreme - 1e-9:
                return False
        return True

    def _sweep_entry_zone(self, tracker, sw: LiquiditySweep, ev: StructureEvent) -> tuple[PriceRange, PlanZones, str]:
        """OB from the MSS leg, else its FVG, else the 50-61.8% retrace of the leg."""
        zones = PlanZones(liquidity_level_id=sw.level_id)
        for ob in self.hub.zones.order_blocks(ev.symbol, ev.timeframe):
            if ob.origin_event_id == ev.id and ob.is_live:
                zones.order_block_id = ob.id
                return PriceRange(ob.top, ob.bottom), zones, "at the MSS order block"
        gaps = [f for f in self.hub.zones.fvgs(ev.symbol, ev.timeframe)
                if f.bias is ev.direction and ev.origin_index < f.created_index <= ev.index and f.is_live]
        if gaps:
            g = min(gaps, key=lambda f: f.created_index)
            zones.fvg_id = g.id
            return PriceRange(g.top, g.bottom), zones, "at the MSS leg FVG"
        buf = tracker.buffer
        lo_i = next((k for k in range(buf.last_index, buf.base - 1, -1) if buf.time(k) < sw.time), buf.base) + 1
        if ev.direction is Bias.BEARISH:
            low = min(buf.lows_between(lo_i, buf.last_index))
            leg = sw.extreme - low
            return PriceRange(low + 0.618 * leg, low + 0.5 * leg), zones, "at the 50-61.8% retrace of the MSS leg"
        high = max(buf.highs_between(lo_i, buf.last_index))
        leg = high - sw.extreme
        return PriceRange(high - 0.5 * leg, high - 0.618 * leg), zones, "at the 50-61.8% retrace of the MSS leg"

    # ----------------------------------------------------------- apply candidates
    def _retire(self, plan: Plan) -> None:
        for zid in plan.zones.zone_ids:
            self._retired.add((zid, plan.direction))

    def _apply_candidate(self, c: Candidate, ctx: EvalContext, now: pd.Timestamp) -> list[PlanChange]:
        symbol = ctx.symbol
        if any((z, c.direction) in self._retired for z in c.zones.zone_ids):
            return []  # that zone already had its plan (ended); never recreate it
        existing = self._find_existing(symbol, c, 0.1 * ctx.atr)
        if existing is not None:
            if c.priority <= existing.entry_model.priority or existing.status is PlanStatus.READY_TO_EXECUTE:
                return []  # same idea seen again: _refresh keeps it current, no duplicate
            note = (f"upgraded {existing.entry_model.value} -> {c.model.value}: {c.reason}")
            self.store.update(
                existing.plan_id, now, note, entry_model=c.model, zones=c.zones, entry_zone=c.entry_zone,
                invalidation=c.invalidation, targets=c.targets, rr_estimate=c.rr, reason=c.reason, quality=c.quality,
                external_range_position=c.external_range_position, internal_range_position=c.internal_range_position,
                entry_range_basis=c.entry_range_basis,
                source_timeframes=SourceTimeframes(
                    existing.source_timeframes.external,
                    existing.source_timeframes.internal,
                    c.timeframe,
                ),
                source_zones=tuple(
                    zone for zone_id in c.zones.zone_ids
                    if (zone := self.hub.zones.get(zone_id)) is not None
                ))
            existing.market_context = plan_market_context(existing, self.hub.zones.all_for(symbol))
            log.info("PLAN UPGRADED %s | %s", existing.plan_id, note)
            return [PlanChange(ChangeKind.UPGRADED, now, existing.plan_id, note)]

        plan = Plan(
            plan_id=self.store.next_id(symbol), symbol=symbol, direction=c.direction, entry_model=c.model,
            source_timeframes=SourceTimeframes(self._cfg.timeframes.external, self._cfg.timeframes.internal, c.timeframe),
            external_bias=ctx.external_bias, internal_bias=ctx.internal_bias, zones=c.zones,
            entry_zone=c.entry_zone, invalidation=c.invalidation, targets=c.targets, rr_estimate=c.rr,
            reason=c.reason, created_at=now, updated_at=now,
            external_range_position=c.external_range_position, internal_range_position=c.internal_range_position,
            quality=c.quality, entry_range_basis=c.entry_range_basis,
            source_zones=tuple(
                zone for zone_id in c.zones.zone_ids
                if (zone := self.hub.zones.get(zone_id)) is not None
            ),
            market_context=None)
        plan.market_context = plan_market_context(plan, self.hub.zones.all_for(symbol))
        self.store.add(plan)
        plan.log(now, "created", c.reason)
        log.info("PLAN CREATED %s | %s | %s", plan.plan_id, plan.summary(), c.reason)
        return [PlanChange(ChangeKind.CREATED, now, plan.plan_id, c.reason)]

    def _find_existing(self, symbol: str, c: Candidate, tolerance: float) -> Optional[Plan]:
        """Same zone, same liquidity level, or the same price area in the same direction."""
        if c.zones.zone_ids:
            hit = self.store.find_by_zones(symbol, c.direction, c.zones.zone_ids)
            if hit is not None:
                return hit
        lid = c.zones.liquidity_level_id
        hit = next((p for p in self.store.list_active(symbol)
                    if p.direction is c.direction and lid and p.zones.liquidity_level_id == lid), None)
        if hit is not None:
            return hit
        return self.store.find_overlapping(symbol, c.direction, c.entry_zone.top, c.entry_zone.bottom, tolerance)
