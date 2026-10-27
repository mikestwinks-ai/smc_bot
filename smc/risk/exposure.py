"""Rebuilds the bot's exposure and daily statistics from MT5 - restart-safe.

Counting rules (documented because they decide what blocks a trade)
-------------------------------------------------------------------
* "Bot" = deals/positions/orders carrying ``execution.magic_number``.
* Open trades = open positions + pending orders (a pending order is committed
  exposure). Same rule for the per-direction-per-symbol limit.
* Trades opened today = distinct position ids with an ENTRY deal inside today's
  trading day + pending orders still waiting. A pending order that fills is counted
  once (as a position), a cancelled one frees its slot.
* A trade's outcome folds EVERY deal of one position (entry, partial closes, final
  close; profit + commission + swap + fee) into one net number, so partials,
  commissions and swaps are never counted as separate wins/losses.
* Consecutive losses = trailing losing trades among trades CLOSED in the current
  trading day (the streak resets at the day boundary, otherwise a blocked bot could
  never win again). Net == 0 / break-even is not a loss and breaks the streak.
* Daily baseline = start-of-day BALANCE: stored once per trading day; if missing
  (first run / lost state) it is reconstructed as
  ``balance - sum(net of every deal today, any magic)``.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

import pandas as pd

from smc.risk.models import AccountState, DealInfo, ExposureSnapshot, TradeOutcome
from smc.risk.sessions import SessionClock
from smc.utils.logger import get_logger

if TYPE_CHECKING:  # runtime import would be circular (the gateway imports smc.risk.models)
    from smc.execution.mt5_gateway import MT5Gateway
    from smc.execution.state_store import StateStore

log = get_logger(__name__)


def fold_outcomes(deals: list[DealInfo]) -> list[TradeOutcome]:
    """One TradeOutcome per FULLY closed position (partials folded in)."""
    by_pos: dict[int, list[DealInfo]] = defaultdict(list)
    for d in deals:
        if d.is_trade and d.position_id:
            by_pos[d.position_id].append(d)
    out = []
    for pid, ds in by_pos.items():
        if any(d.is_reversal for d in ds):
            continue  # broker reversal entries cannot be folded into one unambiguous trade
        vol_in = sum(d.volume for d in ds if d.is_entry)
        vol_out = sum(d.volume for d in ds if d.is_exit)
        if vol_in <= 0 or vol_out < vol_in - 1e-9:
            continue  # still open (or partially closed)
        out.append(TradeOutcome(
            position_id=pid, symbol=ds[0].symbol, opened_at=min(d.time for d in ds if d.is_entry),
            closed_at=max(d.time for d in ds if d.is_exit), net=sum(d.net for d in ds)))
    return sorted(out, key=lambda t: t.closed_at)


class ExposureTracker:
    def __init__(self, gateway: "MT5Gateway", state: "StateStore", clock: SessionClock) -> None:
        self._gw, self._state, self._clock = gateway, state, clock
        self._last_snapshot: ExposureSnapshot | None = None

    @property
    def last_snapshot(self) -> ExposureSnapshot | None:
        """Most recent successful risk snapshot, without refreshing broker data."""
        return self._last_snapshot

    def snapshot(self, account: AccountState, now: pd.Timestamp, persist_baseline: bool = True) -> ExposureSnapshot:
        day_key = self._clock.trading_day_key(now)
        day_start = self._clock.trading_day_start_utc(now)
        positions, pending = self._gw.positions(), self._gw.pending_orders()
        deals = self._gw.deals(day_start - pd.Timedelta(days=1), now + pd.Timedelta(hours=1))

        today_all = [d for d in deals if d.time >= day_start]
        baseline = self._state.get_baseline(day_key)
        if baseline is None:
            baseline = account.balance - sum(d.net for d in today_all)
            if baseline > 0 and persist_baseline:
                self._state.set_baseline(day_key, baseline)
                log.info("Daily baseline for %s set to %.2f (reconstructed from balance and today's deals)",
                         day_key, baseline)

        magic = self._gw.magic
        bot_deals = [d for d in deals if d.magic == magic and d.is_trade]
        bot_today = [d for d in bot_deals if d.time >= day_start]
        opened_ids = {d.position_id for d in bot_today if d.is_entry}
        closed_today = [t for t in fold_outcomes(bot_deals) if t.closed_at >= day_start]

        streak = 0
        for t in reversed(closed_today):
            if not t.is_loss:
                break
            streak += 1

        realised = sum(d.net for d in bot_today)
        if baseline > 0:
            realised_loss_pct = max(0.0, -realised) / baseline * 100.0
            total_dd_pct = max(0.0, baseline - account.equity) / baseline * 100.0
        else:  # unusable baseline -> fail closed
            realised_loss_pct = total_dd_pct = 100.0
        result = ExposureSnapshot(
            now=now, trading_day=day_key, positions=positions, pending=pending,
            trades_opened_today=len(opened_ids) + len(pending), closed_today=closed_today,
            consecutive_losses=streak, realised_pl_today=realised, baseline_balance=baseline,
            equity=account.equity, realised_loss_pct=realised_loss_pct, total_drawdown_pct=total_dd_pct)
        self._last_snapshot = result
        return result
