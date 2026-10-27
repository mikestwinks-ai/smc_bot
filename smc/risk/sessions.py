"""Trading-session clock. ONE place for every time-of-day rule.

* Allowed sessions come from ``SessionConfig.windows`` (London + New York, each
  in its OWN local time, so daylight saving is handled by ``zoneinfo``). The
  London/NY overlap is inside both windows, so it is allowed automatically.
* ``no_new_trades_after`` (22:30) and ``close_all_risk_by`` (23:00) are read in
  ``cutoff_timezone`` - an explicit config value, never silently UTC/broker/EAT.
* The trading day (daily limits) starts at ``trading_day_start`` in that same zone.
* Crypto symbols are exempt from the two cutoffs only if
  ``crypto_exempt_from_cutoffs`` is True (it is in the existing config). They are
  NOT exempt from the session windows.

Every method takes ``now`` as a parameter, so tests and restarts behave identically.
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from smc.config import SessionConfig, SymbolConfig


class SessionClock:
    def __init__(self, cfg: SessionConfig) -> None:
        self._cfg = cfg
        self._tz = ZoneInfo(cfg.cutoff_timezone)

    @property
    def timezone_name(self) -> str:
        return self._cfg.cutoff_timezone

    # --------------------------------------------------------------- helpers
    def local(self, now: pd.Timestamp) -> datetime:
        return now.tz_convert(self._tz).to_pydatetime()

    def _exempt(self, symbol: Optional[SymbolConfig]) -> bool:
        return bool(symbol and symbol.is_crypto and self._cfg.crypto_exempt_from_cutoffs)

    # ---------------------------------------------------------- trading day
    def trading_day_start_utc(self, now: pd.Timestamp) -> pd.Timestamp:
        """UTC instant at which the current trading day began."""
        loc = self.local(now)
        start = datetime.combine(loc.date(), self._cfg.trading_day_start, tzinfo=self._tz)
        if start > loc:
            start -= timedelta(days=1)
        return pd.Timestamp(start).tz_convert("UTC")

    def trading_day_key(self, now: pd.Timestamp) -> str:
        loc = self.local(self.trading_day_start_utc(now))
        return loc.strftime("%Y-%m-%d")

    # --------------------------------------------------------------- entries
    def in_session(self, now: pd.Timestamp) -> tuple[bool, str]:
        for w in self._cfg.windows:
            loc = now.tz_convert(ZoneInfo(w.timezone))
            if loc.weekday() >= 5:
                continue
            t = loc.time()
            inside = (w.start <= t < w.end) if w.start <= w.end else (t >= w.start or t < w.end)
            if inside:
                return True, f"inside {w.name} session ({loc:%H:%M} {w.timezone})"
        return False, "outside London / London-NY overlap / New York sessions"

    def after_no_new_trades(self, now: pd.Timestamp, symbol: Optional[SymbolConfig] = None) -> bool:
        if self._exempt(symbol):
            return False
        return self.local(now).time() >= self._cfg.no_new_trades_after

    def entries_allowed(self, now: pd.Timestamp, symbol: Optional[SymbolConfig] = None) -> tuple[bool, str]:
        ok, why = self.in_session(now)
        if not ok:
            return False, why
        if self.after_no_new_trades(now, symbol):
            return False, (f"after the {self._cfg.no_new_trades_after:%H:%M} no-new-trades cutoff "
                           f"({self.timezone_name})")
        return True, why

    # --------------------------------------------------------------- flatten
    def close_deadline(self, opened_at: pd.Timestamp, symbol: Optional[SymbolConfig] = None) -> Optional[pd.Timestamp]:
        """When exposure opened at ``opened_at`` must be closed (None = exempt).

        The next ``close_all_risk_by`` in the cutoff zone; if the position was somehow
        opened after it, the deadline is immediate. Stored with the position, so a
        restart or a delayed cycle cannot extend it.
        """
        if self._exempt(symbol):
            return None
        loc = self.local(opened_at)
        cut = datetime.combine(loc.date(), self._cfg.close_all_risk_by, tzinfo=self._tz)
        if cut < loc:
            return opened_at
        return pd.Timestamp(cut).tz_convert("UTC")

    def pending_expiry(self, now: pd.Timestamp, symbol: Optional[SymbolConfig] = None) -> Optional[pd.Timestamp]:
        """Broker-side expiry for a pending order: the next no-new-trades cutoff."""
        if self._exempt(symbol):
            return None
        loc = self.local(now)
        cut = datetime.combine(loc.date(), self._cfg.no_new_trades_after, tzinfo=self._tz)
        if cut <= loc:
            return None
        return pd.Timestamp(cut).tz_convert("UTC")
