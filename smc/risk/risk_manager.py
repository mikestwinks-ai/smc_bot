"""Central risk manager - every proposed trade passes through ``evaluate``.

Daily drawdown baseline (documented decision)
---------------------------------------------
``baseline`` = start-of-trading-day BALANCE (see ``exposure.py``). Two measures are
tracked and shown separately:
    realised_loss_pct  = bot's realised losses today / baseline
    total_drawdown_pct = (baseline - equity) / baseline   (realised + floating)
New entries are blocked when EITHER reaches ``max_daily_drawdown_pct`` (2%). Using
the larger of the two means floating profit can never hide a realised loss, and a
deep floating loss is stopped before it is realised.

Risk per trade: 0.5% below the balance threshold, 1% at/above it, clamped to the
hard ceiling. Sizing rounds volume DOWN and rejects when the minimum lot is too big.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from smc.config import ABSOLUTE_MAX_RISK_PCT, BotConfig
from smc.risk.exposure import ExposureTracker
from smc.risk.models import AccountState, ExposureSnapshot, RiskDecision, SymbolSpec
from smc.risk.sizing import compute_volume
from smc.utils.logger import get_logger

log = get_logger(__name__)


class RiskManager:
    def __init__(self, config: BotConfig, exposure: ExposureTracker) -> None:
        self._cfg, self._exposure = config, exposure

    # --------------------------------------------------------------- risk %
    def risk_pct(self, equity: float) -> float:
        r = self._cfg.risk
        pct = r.risk_pct_below_threshold if equity < r.balance_threshold else r.risk_pct_at_or_above_threshold
        return min(pct, r.hard_max_risk_pct, ABSOLUTE_MAX_RISK_PCT)

    # --------------------------------------------------------------- checks
    def account_problems(self, account: AccountState, now: pd.Timestamp) -> list[str]:
        out = []
        if not account.currency:
            out.append("account currency unknown")
        if account.equity <= 0 or account.balance <= 0:
            out.append(f"account equity/balance not positive ({account.equity}/{account.balance})")
        if not account.trade_allowed:
            out.append("trading is not allowed on this account/terminal")
        age = (now - account.fetched_at).total_seconds()
        if age > self._cfg.risk.max_account_age_seconds:
            out.append(f"account data is stale ({age:.0f}s old)")
        return out

    def limit_problems(self, snap: ExposureSnapshot, broker_symbol: str, is_buy: bool) -> list[str]:
        r, out = self._cfg.risk, []
        if snap.baseline_balance <= 0:
            out.append("daily baseline unavailable - failing closed")
        loss = max(snap.realised_loss_pct, snap.total_drawdown_pct)
        if loss >= r.max_daily_drawdown_pct:
            out.append(f"daily drawdown limit reached: {loss:.2f}% >= {r.max_daily_drawdown_pct:.1f}% "
                       f"(realised {snap.realised_loss_pct:.2f}%, floating {snap.floating_drawdown_pct:.2f}%, "
                       f"baseline {snap.baseline_balance:.2f})")
        if snap.consecutive_losses >= r.max_consecutive_losses:
            out.append(f"{snap.consecutive_losses} consecutive losses (limit {r.max_consecutive_losses})")
        if snap.trades_opened_today >= r.max_trades_per_day:
            out.append(f"{snap.trades_opened_today} trades already opened this trading day (limit {r.max_trades_per_day})")
        if snap.open_count >= r.max_open_trades_total:
            out.append(f"{snap.open_count} open positions/pending orders (limit {r.max_open_trades_total})")
        if snap.has_exposure(broker_symbol, is_buy):
            out.append(f"already exposed {'long' if is_buy else 'short'} on {broker_symbol} "
                       f"(limit {r.max_open_trades_per_direction_per_symbol} per direction per symbol)")
        return out

    # ------------------------------------------------------------- decision
    def evaluate(self, *, account: AccountState, spec: SymbolSpec, broker_symbol: str, is_buy: bool,
                 entry: float, sl: float, now: pd.Timestamp,
                 snapshot: Optional[ExposureSnapshot] = None) -> RiskDecision:
        reasons = self.account_problems(account, now)
        if reasons:
            return RiskDecision(False, reasons)
        snap = snapshot or self._exposure.snapshot(account, now)
        reasons = self.limit_problems(snap, broker_symbol, is_buy)
        if reasons:
            return RiskDecision(False, reasons, snapshot=snap)

        dist = (entry - sl) if is_buy else (sl - entry)
        if dist <= 0:
            return RiskDecision(False, [f"stop {sl} is not on the loss side of entry {entry}"], snapshot=snap)
        pct = self.risk_pct(account.equity)
        sizing, why = compute_volume(account.equity, pct, dist, spec)
        if sizing is None:
            return RiskDecision(False, [why or "sizing failed"], snapshot=snap)
        if sizing.actual_risk / account.equity * 100.0 > ABSOLUTE_MAX_RISK_PCT + 1e-9:  # belt and braces
            return RiskDecision(False, ["computed risk exceeds the 1% hard ceiling"], snapshot=snap)
        return RiskDecision(True, [], sizing, snap)
