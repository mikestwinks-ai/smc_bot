"""Pre-execution validation of a Phase 3 plan against the LIVE market.

The plan being in the store is never enough. Everything that can have gone stale
since the plan was made is re-checked here, and the whole check runs again right
before the order is sent. Missing information fails CLOSED.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from smc.config import BotConfig
from smc.execution.order_builder import round_to_tick, stop_level_problems
from smc.models.plan import EntryModel, Plan, PlanDirection, PlanStatus
from smc.planning.context import StructureView
from smc.planning.entry_models import pd_rejection, position_at
from smc.planning.plan_store import PlanStore
from smc.risk.models import OrderKind, SymbolSpec, TickState
from smc.structure.models import ZoneStatus
from smc.utils.logger import get_logger

log = get_logger(__name__)

EXECUTABLE = (PlanStatus.ACTIVE, PlanStatus.READY_TO_EXECUTE)


@dataclass
class ValidatedOrder:
    kind: OrderKind
    entry: float  # price the order is expected to fill at
    sl: float
    tp: float
    rr: float


@dataclass
class ValidationResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    order: Optional[ValidatedOrder] = None


def required_rr(plan: Plan, cfg: BotConfig) -> float:
    if plan.entry_model in (EntryModel.FVG, EntryModel.OB_FVG):
        return cfg.structure.min_fvg_rr  # the 2R rule for FVG-based entries
    if plan.entry_model is EntryModel.SWEEP_MSS:
        return cfg.planning.min_sweep_rr
    return cfg.risk.min_rr_other


class PlanValidator:
    def __init__(self, config: BotConfig, plans: PlanStore, view: Optional[StructureView]) -> None:
        self._cfg, self._plans, self._view = config, plans, view

    # -------------------------------------------------------------- entry point
    def validate(self, plan: Plan, spec: SymbolSpec, tick: TickState, now: pd.Timestamp) -> ValidationResult:
        problems: list[str] = []
        problems += self._lifecycle(plan, now)
        if problems:
            return ValidationResult(False, problems)
        problems += self._structure(plan, now)
        problems += self._market_data(tick, spec, plan, now)
        if problems:
            return ValidationResult(False, problems)

        kind, entry, why = self.choose_order(plan, tick, spec)
        if kind is None:
            return ValidationResult(False, [why])
        order, geo = self._geometry(plan, kind, entry, spec, tick)
        if geo:
            return ValidationResult(False, geo)
        return ValidationResult(True, [], order)

    # ----------------------------------------------------------------- lifecycle
    def _lifecycle(self, plan: Plan, now: pd.Timestamp) -> list[str]:
        try:
            self._plans.get(plan.plan_id)
        except KeyError:
            return [f"{plan.plan_id} is not in the plan store"]
        if plan.status in (PlanStatus.CANCELED, PlanStatus.FLIPPED, PlanStatus.EXECUTED):
            what = plan.cancel_reason or plan.flip_reason or "already executed"
            return [f"plan is {plan.status.value}: {what}"]
        if plan.status not in EXECUTABLE:
            return [f"plan is {plan.status.value}; executable states are active / ready_to_execute"]
        limit = self._cfg.planning.plan_expiry_bars
        age = (now - plan.created_at) / plan.setup_timeframe.delta
        if plan.status is not PlanStatus.READY_TO_EXECUTE and age > limit:
            return [f"plan expired ({age:.0f} bars > {limit})"]
        if plan.entry_zone.top <= plan.entry_zone.bottom:
            return ["entry zone is empty/invalid"]
        if not plan.targets or self._cfg.execution.take_profit_target_index >= len(plan.targets):
            return ["plan has no usable target"]
        return []

    # ----------------------------------------------------------------- structure
    def _structure(self, plan: Plan, now: pd.Timestamp) -> list[str]:
        if self._view is None:
            return ["structure engine unavailable - failing closed"]
        tf = plan.setup_timeframe
        tracker = self._view.tracker(plan.symbol, tf)
        if tracker is None or not len(tracker.buffer) or tracker.last_close is None:
            return [f"no {tf} structure data for {plan.symbol} - failing closed"]
        last_open = tracker.buffer.time(tracker.buffer.last_index)
        late = (now - (last_open + tf.delta)) / tf.delta
        if late > self._cfg.execution.max_structure_age_bars:
            return [f"latest closed {tf} candle is stale ({late:.1f} bars old)"]
        out = []
        if plan.is_invalidated_by_close(tracker.last_close):
            out.append(f"invalidated: last {tf} close {tracker.last_close:.5f} is beyond {plan.invalidation.price:.5f}")
        for zid in plan.zones.zone_ids:
            z = self._view.hub.zones.get(zid)
            if z is None:
                out.append(f"zone {zid} no longer exists in the structure engine")
            elif z.status in (ZoneStatus.FLIPPED, ZoneStatus.INVALIDATED):
                out.append(f"zone {zid} is {z.status.value}")
        if out:
            return out
        # Phase 3 premium/discount hard block is re-applied, never bypassed
        respect = self._cfg.planning.flip_plans_respect_pd or plan.parent_plan_id is None
        if respect:
            ctx = self._view.build(plan.symbol, tf, now)
            if ctx is None:
                return ["cannot rebuild the premium/discount context - failing closed"]
            pos = position_at(ctx.pd_reading, plan.entry_price)
            why = pd_rejection(plan.direction, pos, self._cfg.structure.pd_equilibrium_band, False,
                               ctx.layer, tolerance=1e9)
            if why:
                return [f"premium/discount re-check: {why}"]
        return []

    # --------------------------------------------------------------- market data
    def _market_data(self, tick: TickState, spec: SymbolSpec, plan: Plan, now: pd.Timestamp) -> list[str]:
        out = spec.problems()
        age = (now - tick.time).total_seconds()
        if age > self._cfg.risk.max_tick_age_seconds:
            out.append(f"stale tick ({age:.0f}s old) - market closed or feed stalled")
        if tick.bid <= 0 or tick.ask <= 0 or tick.ask < tick.bid:
            out.append(f"invalid quote bid={tick.bid} ask={tick.ask}")
        mode = spec.trade_mode
        if mode in (0, 3):
            out.append("symbol is not open for new trades (trade mode disabled / close-only)")
        elif mode == 1 and plan.direction is PlanDirection.SHORT:
            out.append("symbol is long-only")
        elif mode == 2 and plan.direction is PlanDirection.LONG:
            out.append("symbol is short-only")
        return out

    # --------------------------------------------------------- order kind choice
    def choose_order(self, plan: Plan, tick: TickState, spec: SymbolSpec
                     ) -> tuple[Optional[OrderKind], float, str]:
        """MARKET inside the zone, LIMIT while price is approaching, STOP only if enabled."""
        ex, z = self._cfg.execution, plan.entry_zone
        long = plan.direction is PlanDirection.LONG
        px = tick.ask if long else tick.bid
        ready = plan.status is PlanStatus.READY_TO_EXECUTE
        entry = plan.entry_price
        lvl = spec.stops_level

        if z.bottom <= px <= z.top:
            if ex.allow_market_entries:
                return OrderKind.MARKET, px, ""
            return None, 0.0, "price is inside the entry zone and market entries are disabled"
        approaching = px > z.top if long else px < z.bottom  # still on the near side
        if approaching:
            if ready:
                return None, 0.0, "stale: price touched the entry zone and moved away without an entry"
            if abs(px - entry) < lvl:
                return None, 0.0, "entry is closer to market than the broker minimum distance - wait"
            return OrderKind.LIMIT, entry, ""
        # price is already through the zone (beyond its far edge)
        if ex.allow_stop_entries and not ready and abs(entry - px) >= lvl:
            return OrderKind.STOP, entry, ""
        return None, 0.0, "price is already through the entry zone - setup no longer valid"

    # ------------------------------------------------------------------ geometry
    def _geometry(self, plan: Plan, kind: OrderKind, entry: float, spec: SymbolSpec, tick: TickState
                  ) -> tuple[Optional[ValidatedOrder], list[str]]:
        long = plan.direction is PlanDirection.LONG
        sl = round_to_tick(plan.invalidation.price, spec, "up" if long else "down")  # toward entry: never more risk
        tp = round_to_tick(plan.targets[self._cfg.execution.take_profit_target_index], spec)
        entry = round_to_tick(entry, spec)
        out: list[str] = []
        if (long and not sl < entry) or (not long and not sl > entry):
            out.append(f"stop-loss {sl} is not on the loss side of entry {entry}")
        if (long and not tp > entry) or (not long and not tp < entry):
            out.append(f"take-profit {tp} is not on the profit side of entry {entry}")
        exit_px = tick.bid if long else tick.ask
        if (long and exit_px <= sl) or (not long and exit_px >= sl):
            out.append(f"price {exit_px} is already beyond the stop-loss {sl}")
        if (long and tick.bid >= tp) or (not long and tick.ask <= tp):
            out.append(f"price already reached the target {tp}: setup is stale")
        if out:
            return None, out
        risk, reward = abs(entry - sl), abs(tp - entry)
        rr = reward / risk
        need = required_rr(plan, self._cfg)
        if rr < need:
            return None, [f"RR {rr:.2f} < required {need:.1f} for {plan.entry_model.value} "
                          f"(entry {entry}, stop {sl}, target {tp})"]
        if tick.spread > self._cfg.risk.max_spread_to_stop_ratio * risk:
            return None, [f"spread {tick.spread:.5f} is more than {self._cfg.risk.max_spread_to_stop_ratio:.0%} "
                          f"of the stop distance {risk:.5f}"]
        out += stop_level_problems(kind, long, entry, sl, tp, tick, spec)
        if out:
            return None, out
        return ValidatedOrder(kind, entry, sl, tp, rr), []
