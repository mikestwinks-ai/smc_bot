"""Turns a confirmed MT5 position into a managed position and closes the plan lifecycle.

Shared by the execution engine (market fills) and the trade manager (pending
orders that fill later, or positions found after a restart).
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from smc.config import BotConfig
from smc.execution import state_store as st
from smc.execution.mt5_gateway import MT5Gateway
from smc.execution.state_store import ManagedPosition, StateStore
from smc.journal.store import JournalStore
from smc.models.plan import PlanStatus
from smc.planning.plan_store import PlanStore
from smc.risk.models import PositionInfo
from smc.risk.sessions import SessionClock
from smc.utils.logger import get_logger

log = get_logger(__name__)


def exec_key(plan_id: str, created_at: pd.Timestamp) -> str:
    """Stable idempotency key, also written into the MT5 order comment (<= 31 chars).

    Plan ids restart from 0001 after a restart, so the creation time is part of the key.
    """
    return f"{plan_id}|{created_at:%d%H%M}"


def key_from_comment(comment: str) -> Optional[str]:
    parts = comment.split("|")
    return "|".join(parts[:2]) if len(parts) >= 2 and parts[0].startswith("PLAN-") else None


class FillRegistrar:
    def __init__(self, config: BotConfig, gateway: MT5Gateway, state: StateStore, plans: PlanStore,
                 clock: SessionClock, journal: Optional[JournalStore] = None) -> None:
        self._cfg, self._gw, self._state, self._plans, self._clock = config, gateway, state, plans, clock
        self._journal = journal

    def register(self, pos: PositionInfo, key: str, now: pd.Timestamp) -> ManagedPosition:
        """Idempotent: returns the existing record if the position is already managed."""
        existing = self._state.get_managed(pos.ticket)
        if existing is not None:
            return existing
        intent = self._state.get_intent(key) or {}
        symbol = intent.get("symbol") or self._canonical(pos.symbol)
        sym_cfg = self._gw.symbol_config(symbol) if symbol else None
        adopted = not intent
        mp = ManagedPosition(
            ticket=pos.ticket, key=key, plan_id=key.split("|")[0], symbol=symbol or pos.symbol,
            broker_symbol=pos.symbol, is_buy=pos.is_buy, entry=pos.price_open,
            initial_sl=float(intent.get("sl", pos.sl)), initial_volume=pos.volume,
            tp=float(intent.get("tp", pos.tp)), setup_tf=intent.get("setup_tf", "M15"),
            opened_at=pos.time.isoformat(),
            deadline=(d.isoformat() if (d := self._clock.close_deadline(pos.time, sym_cfg)) is not None else None),
            adopted=adopted)
        self._state.save_managed(mp)
        self._state.set_intent(key, status=st.FILLED, position=pos.ticket)
        if self._journal is not None:
            self._journal.record_event_best_effort(
                "position_opened", occurred_at=pos.time, correlation_id=key,
                idempotency_key=f"position-open:{pos.ticket}", plan_id=mp.plan_id,
                position_id=pos.ticket, symbol=mp.symbol, direction="buy" if pos.is_buy else "sell",
                volume=pos.volume, price=pos.price_open, stop_loss=mp.initial_sl, take_profit=mp.tp,
                adopted=adopted,
            )
        plan_id = mp.plan_id
        try:
            plan = self._plans.get(plan_id)
            if plan.is_live and key == exec_key(plan.plan_id, plan.created_at):
                self._plans.set_status(plan_id, PlanStatus.EXECUTED, now,
                                       f"executed: MT5 position {pos.ticket} {pos.volume} lots @ {pos.price_open}")
        except KeyError:
            pass
        log.info("POSITION REGISTERED %s ticket=%s %s %.2f lots @ %s SL %s%s", plan_id, pos.ticket,
                 "BUY" if pos.is_buy else "SELL", pos.volume, pos.price_open, mp.initial_sl,
                 " (adopted: risk basis = current SL)" if adopted else "")
        return mp

    def _canonical(self, broker_symbol: str) -> Optional[str]:
        for s in self._cfg.symbols:
            if broker_symbol in (s.broker_symbol, s.name):
                return s.name
        return None
