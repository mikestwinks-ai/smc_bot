"""Manages what MT5 has CONFIRMED: positions and pending orders owned by the bot.

One call to :meth:`TradeManager.run` does, in this order:
  1. sync      register fills / adopt positions; forget closed ones
  2. flatten   close positions past their 23:00 deadline (stored per position)
  3. pending   cancel orders whose plan is invalid / flipped / expired / changed,
               orders outside the entry window, and (kill switch) all pendings
  4. manage    +1.5R partial close -> break-even (only after the partial is
               CONFIRMED) -> structural trailing (never widens the stop)

Idempotency: partial closes are tracked with a write-ahead intent AND by comparing
the live position volume with the volume it started with, so a restart or a repeated
cycle can never close the partial twice. Break-even and trailing compare against the
live stop-loss and only ever tighten it.

In dry-run mode nothing is sent and nothing is recorded as done: actions are only logged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from smc.config import BotConfig
from smc.core.exceptions import SMCBotError
from smc.core.timeframes import Timeframe
from smc.execution import order_builder as ob
from smc.execution import state_store as st
from smc.execution.fills import FillRegistrar, exec_key, key_from_comment
from smc.execution.kill_switch import KillSwitch
from smc.execution.mt5_gateway import MT5Gateway, SendResult, SendStatus
from smc.execution.state_store import ManagedPosition, StateStore
from smc.models.plan import PlanStatus
from smc.news.guard import NewsGuard
from smc.planning.context import StructureView
from smc.planning.plan_store import PlanStore
from smc.risk.models import OrderInfo, PositionInfo, SymbolSpec, TickState
from smc.risk.sessions import SessionClock
from smc.risk.sizing import floor_to_grid
from smc.structure.models import Bias, StructureEventType, SwingType
from smc.utils.logger import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Action:
    kind: str  # register | flatten | cancel | partial | breakeven | trail | skip
    target: str
    ok: bool
    note: str


class TradeManager:
    def __init__(self, config: BotConfig, gateway: MT5Gateway, plans: PlanStore, state: StateStore,
                 clock: SessionClock, kill: KillSwitch, view: Optional[StructureView],
                 registrar: FillRegistrar, news_guard: Optional[NewsGuard] = None) -> None:
        self._news = news_guard  # Phase 5
        self._cfg, self._gw, self._plans, self._state = config, gateway, plans, state
        self._clock, self._kill, self._view, self._reg = clock, kill, view, registrar

    # ------------------------------------------------------------------ entry
    def run(self) -> list[Action]:
        now = self._gw.now()
        acts: list[Action] = []
        positions, pending = self._gw.positions(), self._gw.pending_orders()
        acts += self._sync(positions, now)
        positions = [p for p in positions if self._state.get_managed(p.ticket)]
        acts += self._flatten(positions, now)
        positions = [p for p in positions if not any(a.kind == "flatten" and a.ok and a.target == str(p.ticket) for a in acts)]
        acts += self._monitor_pending(pending, now)
        acts += self._news_flatten(positions, now)
        positions = [p for p in positions if not any(a.kind == "flatten" and a.ok and a.target == str(p.ticket) for a in acts)]
        for pos in positions:
            try:
                acts += self._manage(pos, now)
            except SMCBotError as exc:
                log.warning("Management of %s skipped (failing safe, stop untouched): %s", pos.ticket, exc)
                acts.append(Action("skip", str(pos.ticket), False, f"data unavailable: {exc}"))
        return acts

    # ------------------------------------------------------------------- sync
    def _sync(self, positions: list[PositionInfo], now: pd.Timestamp) -> list[Action]:
        acts, open_tickets = [], {p.ticket for p in positions}
        for p in positions:
            if self._state.get_managed(p.ticket) is None:
                key = key_from_comment(p.comment)
                if key:
                    mp = self._reg.register(p, key, now)
                    acts.append(Action("register", str(p.ticket), True,
                                       f"{mp.plan_id} {'adopted' if mp.adopted else 'filled'}"))
        for t in self._state.managed_tickets():
            if t not in open_tickets:
                self._state.drop_managed(t)
        return acts

    # --------------------------------------------------------------- flatten
    def _flatten(self, positions: list[PositionInfo], now: pd.Timestamp) -> list[Action]:
        acts = []
        for p in positions:
            mp = self._state.get_managed(p.ticket)
            if mp is None or mp.deadline is None or now < pd.Timestamp(mp.deadline):
                continue
            ok, note = self._close(p, p.volume, f"{mp.key}|EOD")
            acts.append(Action("flatten", str(p.ticket), ok, f"end-of-day close past {mp.deadline}: {note}"))
            log.warning("FLATTEN %s ticket %s: %s", mp.plan_id, p.ticket, note)
        return acts

    def _news_flatten(self, positions: list[PositionInfo], now: pd.Timestamp) -> list[Action]:
        """Opt-in policy ("flatten"). The default policy "keep" never closes anything because of news.
        A calendar OUTAGE alone never flattens: only a real, identified event lock does."""
        if self._news is None or self._cfg.news.open_position_policy != "flatten":
            return []
        acts = []
        for p in positions:
            mp = self._state.get_managed(p.ticket)
            if mp is None:
                continue
            lock = self._news.check(mp.symbol, now)
            if not lock.locked or lock.code == "calendar_unverified":
                continue
            ok, note = self._close(p, p.volume, f"{mp.key}|NWS")
            acts.append(Action("flatten", str(p.ticket), ok, f"news policy 'flatten': {lock.message()}: {note}"))
        return acts

    def _close(self, pos: PositionInfo, volume: float, comment: str) -> tuple[bool, str]:
        spec, tick = self._gw.symbol_spec(pos.symbol), self._gw.tick(pos.symbol)
        req = ob.build_close(pos, volume, tick, spec, self._cfg.execution.max_slippage_points, comment)
        res = self._dispatch(req, f"close {volume} of {pos.ticket}")
        if res is None:
            return False, "dry-run: not sent"
        if not res.success:
            return False, f"{res.status.value} {res.retcode} {res.comment}"
        left = next((p for p in self._gw.positions() if p.ticket == pos.ticket), None)
        confirmed = left is None or left.volume <= pos.volume - volume + 1e-9
        return confirmed, "confirmed by MT5" if confirmed else "sent, awaiting confirmation"

    def _dispatch(self, request: dict, what: str) -> Optional[SendResult]:
        """Send through the gateway - or just log in dry-run (nothing is recorded as done)."""
        if not self._gw.live:
            log.info("DRY-RUN would: %s", what)
            return None
        return self._gw.send(request)

    # --------------------------------------------------------- pending orders
    def _monitor_pending(self, pending: list[OrderInfo], now: pd.Timestamp) -> list[Action]:
        acts = []
        for o in pending:
            why = self._pending_problem(o, now)
            if not why:
                continue
            ok, note = self._cancel(o)
            acts.append(Action("cancel", str(o.ticket), ok, f"{why}: {note}"))
            log.info("PENDING %s %s -> %s (%s)", o.ticket, o.comment, "canceled" if ok else "NOT canceled", why)
        return acts

    def _pending_problem(self, o: OrderInfo, now: pd.Timestamp) -> Optional[str]:
        if self._kill.engaged and self._cfg.execution.kill_switch_cancels_pending:
            return f"kill switch engaged ({self._kill.reason})"
        sym = next((s for s in self._cfg.symbols if o.symbol in (s.broker_symbol, s.name)), None)
        if self._news is not None:  # Phase 5: a resting order must not be able to trigger inside a news lock
            lock = self._news.check(sym.name if sym else o.symbol, now)
            if lock.locked:
                return f"news lock: {lock.message()}"
        ok, why = self._clock.entries_allowed(now, sym)
        if not ok:
            return f"entry window closed: {why}"
        key = key_from_comment(o.comment)
        plan_id = key.split("|")[0] if key else None
        if not plan_id:
            return "pending order has no plan reference"
        try:
            plan = self._plans.get(plan_id)
        except KeyError:
            return f"plan {plan_id} no longer exists (restart?) - strategy rules cannot be verified"
        if key != exec_key(plan.plan_id, plan.created_at):
            return f"order belongs to an older incarnation of {plan_id}"
        if plan.status in (PlanStatus.CANCELED, PlanStatus.FLIPPED, PlanStatus.EXECUTED):
            return f"plan is {plan.status.value}: {plan.cancel_reason or plan.flip_reason or ''}".strip()
        if plan.status not in (PlanStatus.ACTIVE, PlanStatus.READY_TO_EXECUTE, PlanStatus.PLANNED):
            return f"plan status {plan.status.value} no longer supports a resting order"
        spec = self._gw.symbol_spec(o.symbol)
        tol = spec.tick_size * 1.5
        tp = plan.targets[min(self._cfg.execution.take_profit_target_index, len(plan.targets) - 1)] if plan.targets else 0
        if abs(o.price_open - plan.entry_price) > tol or abs(o.sl - plan.invalidation.price) > tol * 2 \
                or (tp and abs(o.tp - tp) > tol * 2):
            return "plan entry / stop / target changed since the order was placed"
        return None

    def _cancel(self, o: OrderInfo) -> tuple[bool, str]:
        spec, tick = self._gw.symbol_spec(o.symbol), self._gw.tick(o.symbol)
        px = tick.ask if o.is_buy else tick.bid
        if spec.freeze_level > 0 and abs(px - o.price_open) <= spec.freeze_level:
            return False, "inside the broker freeze level - cannot cancel yet, will retry"
        res = self._dispatch(ob.build_remove(o.ticket), f"cancel order {o.ticket}")
        if res is None:
            return False, "dry-run: not sent"
        still = any(x.ticket == o.ticket for x in self._gw.pending_orders())
        if res.success and not still:
            key = key_from_comment(o.comment)
            if key:
                self._state.set_intent(key, status=st.CANCELED, note="canceled by trade manager")
            return True, "removed"
        if res.status is SendStatus.UNCERTAIN and not still:  # answer lost, but the order is gone
            filled = any(p.comment.startswith(o.comment[:20]) for p in self._gw.positions())
            return True, "gone from MT5 (filled)" if filled else "gone from MT5 (verified)"
        return False, f"{res.status.value} {res.retcode} {res.comment}"

    # -------------------------------------------------------- open positions
    def _manage(self, pos: PositionInfo, now: pd.Timestamp) -> list[Action]:
        mp = self._state.get_managed(pos.ticket)
        if mp is None:
            return []
        if pos.sl == 0 or mp.initial_sl == 0:
            return [Action("skip", str(pos.ticket), False, "position has no stop-loss; R cannot be measured")]
        spec, tick = self._gw.symbol_spec(pos.symbol), self._gw.tick(pos.symbol)
        acts: list[Action] = []
        tcfg = self._cfg.trade_management

        if not mp.partial_done and pos.volume < mp.initial_volume - spec.volume_step / 2:
            mp.partial_done = True  # volume already reduced (restart / earlier cycle): never close twice
            self._state.save_managed(mp)
            acts.append(Action("partial", str(pos.ticket), True, "volume already reduced - partial recognised"))

        r = self._r_multiple(pos, mp, tick)
        if not mp.partial_done and not mp.partial_skipped and r >= tcfg.partial_close_at_r:
            acts.append(self._partial(pos, mp, spec, tick, now, r))
            mp = self._state.get_managed(pos.ticket) or mp
            pos = next((p for p in self._gw.positions() if p.ticket == pos.ticket), pos)
        be_ready = mp.partial_done or (mp.partial_skipped and self._cfg.execution.breakeven_if_partial_impossible)
        if tcfg.move_sl_to_breakeven_after_partial and be_ready and not mp.be_done:
            acts.append(self._breakeven(pos, mp, spec, tick))
            pos = next((p for p in self._gw.positions() if p.ticket == pos.ticket), pos)
        if tcfg.structural_trailing_enabled:
            act = self._trail(pos, mp, spec, tick, now)
            if act:
                acts.append(act)
        return acts

    @staticmethod
    def _r_multiple(pos: PositionInfo, mp: ManagedPosition, tick: TickState) -> float:
        risk = abs(mp.entry - mp.initial_sl)
        if risk <= 0:
            return 0.0
        px = tick.bid if pos.is_buy else tick.ask  # the price a long/short would be closed at
        return ((px - mp.entry) if pos.is_buy else (mp.entry - px)) / risk

    # ---- partial close -----------------------------------------------------
    def _partial(self, pos: PositionInfo, mp: ManagedPosition, spec: SymbolSpec, tick: TickState,
                 now: pd.Timestamp, r: float) -> Action:
        tag, key = str(pos.ticket), f"{mp.key}|P1"
        frac = self._cfg.trade_management.partial_close_fraction
        intent = self._state.get_intent(key)
        if intent:  # an earlier attempt exists: judge it by the broker's volume, not by our memory
            if pos.volume <= float(intent["volume_before"]) - float(intent["close"]) + 1e-9:
                mp.partial_done, mp.partial_volume = True, float(intent["close"])
                self._state.save_managed(mp)
                self._state.set_intent(key, status=st.FILLED)
                return Action("partial", tag, True, "earlier partial close confirmed by volume")
            if intent.get("status") in (st.SENDING, st.UNCERTAIN):
                age = (now - pd.Timestamp(intent["sent_at"])).total_seconds()
                if age < self._cfg.execution.uncertain_hold_seconds:
                    return Action("partial", tag, False, "previous partial-close outcome unknown; waiting to reconcile")
            if int(intent.get("attempts", 0)) >= self._cfg.execution.max_send_attempts:
                return Action("partial", tag, False, "partial close abandoned after repeated failures; stop unchanged")

        close_vol = self._partial_volume(pos.volume, frac, spec)
        if close_vol is None:
            mp.partial_skipped = True
            self._state.save_managed(mp)
            msg = (f"partial close impossible: {pos.volume} lots cannot be split {frac:.0%} within min {spec.volume_min} "
                   f"/ step {spec.volume_step}; " + ("will move SL to break-even without it"
                   if self._cfg.execution.breakeven_if_partial_impossible else "position left running"))
            log.warning("%s %s", mp.plan_id, msg)
            return Action("partial", tag, False, msg)
        if not self._gw.live:
            log.info("DRY-RUN would: partial close %s of %s at %.2fR", close_vol, pos.ticket, r)
            return Action("partial", tag, False, "dry-run: not sent")

        attempts = int((intent or {}).get("attempts", 0)) + 1
        self._state.set_intent(key, plan_id=mp.plan_id, status=st.SENDING, attempts=attempts, close=close_vol,
                               volume_before=pos.volume, sent_at=now.isoformat())
        req = ob.build_close(pos, close_vol, tick, spec, self._cfg.execution.max_slippage_points, key)
        res = self._gw.send(req)
        after = next((p for p in self._gw.positions() if p.ticket == pos.ticket), None)
        if res.status in (SendStatus.DONE, SendStatus.PARTIAL) and after is not None \
                and after.volume <= pos.volume - close_vol + 1e-9:
            mp.partial_done, mp.partial_volume = True, close_vol
            self._state.save_managed(mp)
            self._state.set_intent(key, status=st.FILLED)
            log.info("PARTIAL CLOSE %s: %.2f of %.2f lots at %.2fR - confirmed", mp.plan_id, close_vol, pos.volume, r)
            return Action("partial", tag, True, f"closed {close_vol} lots at {r:.2f}R (confirmed)")
        status = st.UNCERTAIN if res.status in (SendStatus.UNCERTAIN, SendStatus.DONE, SendStatus.PARTIAL) else st.FAILED
        self._state.set_intent(key, status=status, note=f"{res.retcode} {res.comment}")
        return Action("partial", tag, False, f"not confirmed: {res.status.value} {res.retcode} {res.comment}")

    @staticmethod
    def _partial_volume(volume: float, fraction: float, spec: SymbolSpec) -> Optional[float]:
        """Closable volume on the broker's grid leaving a legal remainder, or None."""
        want = volume * fraction
        if want < spec.volume_min - 1e-12:
            return None
        close = floor_to_grid(want, spec.volume_min, spec.volume_step)
        if volume - close < spec.volume_min - 1e-9:  # remainder would be illegal: close one step less
            close = round(close - spec.volume_step, 8)
        if close < spec.volume_min - 1e-9 or volume - close < spec.volume_min - 1e-9:
            return None
        return close

    # ---- break-even ----------------------------------------------------------
    def _breakeven(self, pos: PositionInfo, mp: ManagedPosition, spec: SymbolSpec, tick: TickState) -> Action:
        tag = str(pos.ticket)
        target = self._breakeven_price(pos, mp, spec)
        buy = pos.is_buy
        if (buy and pos.sl >= target - spec.tick_size / 2) or (not buy and pos.sl <= target + spec.tick_size / 2):
            mp.be_done = True
            self._state.save_managed(mp)
            return Action("breakeven", tag, True, "stop already at/inside break-even")
        market = tick.bid if buy else tick.ask
        if (buy and target >= market) or (not buy and target <= market):
            return Action("breakeven", tag, False, "price has not enough room above/below break-even yet")
        problems = ob.modify_problems(pos, target, tick, spec)
        if problems:
            return Action("breakeven", tag, False, "; ".join(problems))
        if not self._gw.live:
            log.info("DRY-RUN would: move SL of %s to break-even %s", pos.ticket, target)
            return Action("breakeven", tag, False, "dry-run: not sent")
        res = self._gw.send(ob.build_sltp(pos, target, pos.tp))
        after = next((p for p in self._gw.positions() if p.ticket == pos.ticket), None)
        if after is not None and abs(after.sl - target) <= spec.tick_size / 2:
            mp.be_done = True
            self._state.save_managed(mp)
            log.info("BREAK-EVEN %s: SL -> %s (confirmed)", mp.plan_id, target)
            return Action("breakeven", tag, True, f"SL moved to {target} (confirmed)")
        return Action("breakeven", tag, False, f"not confirmed: {res.status.value} {res.retcode} {res.comment}")

    def _breakeven_price(self, pos: PositionInfo, mp: ManagedPosition, spec: SymbolSpec) -> float:
        """Entry +/- the money cost still to be recouped (commission, swap) + extra ticks."""
        cost_money = 0.0
        try:
            opened = pd.Timestamp(mp.opened_at)
            deals = [d for d in self._gw.deals(opened - pd.Timedelta(hours=1), self._gw.now() + pd.Timedelta(hours=1))
                     if d.position_id == pos.ticket and d.is_entry]
            comm_in = -sum(d.commission for d in deals)  # commissions are negative numbers
            per_lot = comm_in / mp.initial_volume if mp.initial_volume else 0.0
            cost_money = per_lot * pos.volume * 2 + max(0.0, -pos.swap)  # entry share + expected exit + swap
        except SMCBotError:
            pass  # cost data unavailable: fall back to pure break-even
        value_per_price = spec.tick_value_loss / spec.tick_size
        dist = cost_money / (pos.volume * value_per_price) if pos.volume > 0 and value_per_price > 0 else 0.0
        extra = self._cfg.execution.breakeven_extra_ticks * spec.tick_size
        if pos.is_buy:
            return ob.round_to_tick(mp.entry + dist + extra, spec, "up")
        return ob.round_to_tick(mp.entry - dist - extra, spec, "down")

    # ---- structural trailing -------------------------------------------------
    def _trail(self, pos: PositionInfo, mp: ManagedPosition, spec: SymbolSpec, tick: TickState,
               now: pd.Timestamp) -> Optional[Action]:
        tag = str(pos.ticket)
        if self._view is None:
            return Action("trail", tag, False, "structure unavailable - stop unchanged")
        tf = Timeframe.from_str(mp.setup_tf)
        tracker = self._view.tracker(mp.symbol, tf)
        if tracker is None or not len(tracker.buffer) or not tracker.atr:
            return Action("trail", tag, False, "no structure data - stop unchanged")
        last_open = tracker.buffer.time(tracker.buffer.last_index)
        if (now - (last_open + tf.delta)) / tf.delta > self._cfg.execution.max_structure_age_bars:
            return Action("trail", tag, False, "structure data is stale - stop unchanged")

        buy = pos.is_buy
        opened = pd.Timestamp(mp.opened_at)
        want = SwingType.LOW if buy else SwingType.HIGH
        direction = Bias.BULLISH if buy else Bias.BEARISH
        proofs = [e.time for e in tracker.events if e.direction is direction and e.type is StructureEventType.BOS]
        buf_mult = self._cfg.planning.stop_buffer_atr_mult * tracker.atr
        best: Optional[float] = None
        best_swing = None
        for s in tracker.swings:
            # protected swing: formed and confirmed AFTER entry, then proven by a new BOS in our direction
            if s.type is not want or s.confirmed_time <= opened or not any(t > s.time for t in proofs):
                continue
            lvl = ob.round_to_tick(s.price - buf_mult if buy else s.price + buf_mult, spec, "down" if buy else "up")
            if (buy and lvl > pos.sl and (best is None or lvl > best)) or (not buy and lvl < pos.sl and (best is None or lvl < best)):
                best, best_swing = lvl, s
        if best is None or best_swing is None:
            return None  # no new protected swing: keep the existing stop (nothing to log every cycle)
        if (buy and best <= pos.sl) or (not buy and best >= pos.sl):  # never widen, never equal
            return None
        market = tick.bid if buy else tick.ask
        if (buy and best >= market) or (not buy and best <= market):
            return Action("trail", tag, False, "structural level is beyond current price - stop unchanged")
        problems = ob.modify_problems(pos, best, tick, spec)
        if problems:
            return Action("trail", tag, False, "; ".join(problems) + " - stop unchanged")
        if not self._gw.live:
            log.info("DRY-RUN would: trail SL of %s to %s (swing %s)", pos.ticket, best, best_swing.time)
            return Action("trail", tag, False, "dry-run: not sent")
        res = self._gw.send(ob.build_sltp(pos, best, pos.tp))
        after = next((p for p in self._gw.positions() if p.ticket == pos.ticket), None)
        if after is not None and abs(after.sl - best) <= spec.tick_size / 2:
            mp.last_trail_swing = best_swing.time.isoformat()
            self._state.save_managed(mp)
            log.info("TRAIL %s: SL %s -> %s behind swing %s", mp.plan_id, pos.sl, best, best_swing.time)
            return Action("trail", tag, True, f"SL {pos.sl} -> {best} behind protected swing {best_swing.time:%m-%d %H:%M}")
        return Action("trail", tag, False, f"not confirmed: {res.status.value} {res.retcode} {res.comment}")
