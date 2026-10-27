"""Read-only structured view of current trading and safety state."""

from __future__ import annotations

from typing import Any, Optional

import pandas as pd


class MonitoringService:
    def __init__(self, config: Any, gateway: Any, exposure: Any, risk: Any, kill_switch: Any,
                 plans: Any, journal: Any, news_guard: Optional[Any] = None) -> None:
        self._cfg, self._gateway, self._exposure, self._risk = config, gateway, exposure, risk
        self._kill, self._plans, self._journal, self._news = kill_switch, plans, journal, news_guard

    def snapshot(self, now: Optional[pd.Timestamp] = None) -> dict[str, Any]:
        now = now or self._gateway.now()
        account = self._gateway.account()
        positions = self._gateway.positions()
        pending = self._gateway.pending_orders()
        exposure = self._exposure.snapshot(account, now, persist_baseline=False)
        risk_problems = self._risk.limit_problems(exposure, "", False)
        news_status: dict[str, Any] = {"provider": None, "health": "disabled", "calendar_age_hours": None,
                                       "active_locks": []}
        if self._news is not None:
            health = self._news.service.health(now)
            news_status = {
                "provider": self._news.service.provider_name,
                "health": health.status.value,
                "last_success_utc": health.last_success.isoformat() if health.last_success is not None else None,
                "calendar_age_hours": health.age_hours,
                "last_error": health.last_error,
                "consecutive_failures": health.consecutive_failures,
                "active_locks": [
                    {"symbol": symbol, "code": decision.code, "reasons": list(decision.reasons)}
                    for symbol in (s.name for s in self._cfg.enabled_symbols)
                    if (decision := self._news.check(symbol, now)).locked
                ],
            }
        plans = [{
            "plan_id": plan.plan_id, "symbol": plan.symbol, "direction": plan.direction.value,
            "status": plan.status.value, "entry_model": plan.entry_model.value,
            "created_at_utc": plan.created_at.isoformat(), "updated_at_utc": plan.updated_at.isoformat(),
            "entry_zone": {"top": plan.entry_zone.top, "bottom": plan.entry_zone.bottom},
            "invalidation": plan.invalidation.price,
        } for plan in self._plans.all()]
        return {
            "as_of_utc": now.isoformat(),
            "mt5": {"connected": self._gateway.connected,
                    "last_successful_account_update_utc": account.fetched_at.isoformat()},
            "account": {"login": account.login, "currency": account.currency,
                        "environment": "real" if account.is_real else "demo", "balance": account.balance,
                        "equity": account.equity},
            "execution": {"live_enabled": self._cfg.execution.live_enabled,
                          "kill_switch_engaged": self._kill.engaged, "kill_switch_reason": self._kill.reason},
            "plans": plans,
            "positions": [self._position(p) for p in positions],
            "pending_orders": [self._order(order) for order in pending],
            "risk": {
                "daily_trade_count": exposure.trades_opened_today,
                "consecutive_losses": exposure.consecutive_losses,
                "daily_drawdown_pct": exposure.total_drawdown_pct,
                "realised_loss_pct": exposure.realised_loss_pct,
                "baseline_balance": exposure.baseline_balance,
                "limits_ok": not risk_problems,
                "limit_reasons": risk_problems,
            },
            "news": news_status,
            "journal": self._journal.status_summary(account.login, self._gateway.magic),
            "recent_events": self._journal.recent_events(50),
        }

    @staticmethod
    def _position(position: Any) -> dict[str, Any]:
        return {"ticket": position.ticket, "symbol": position.symbol,
                "direction": "buy" if position.is_buy else "sell", "volume": position.volume,
                "open_price": position.price_open, "current_price": position.price_current,
                "stop_loss": position.sl, "take_profit": position.tp, "profit": position.profit,
                "swap": position.swap, "opened_at_utc": position.time.isoformat()}

    @staticmethod
    def _order(order: Any) -> dict[str, Any]:
        return {"ticket": order.ticket, "symbol": order.symbol,
                "direction": "buy" if order.is_buy else "sell",
                "kind": "limit" if order.is_limit else "stop", "volume": order.volume,
                "price": order.price_open, "stop_loss": order.sl, "take_profit": order.tp,
                "created_at_utc": order.time_setup.isoformat()}
