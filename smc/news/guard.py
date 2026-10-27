"""News guard: when are NEW entries (and resting orders) forbidden?

Stateless - every answer is computed from the calendar snapshot and the clock - so a restart
inside a restricted window gives exactly the same answer as before the restart.

Window for a qualifying event at time T:  [T - lock_before , T + lock_after)  (start inclusive,
end exclusive). Optional extensions: a late/missing actual (actual-capable providers only) and
an extra lock after a MIXED outcome (``mixed_extra_lock_minutes``, off by default).

Fail-safe: if the calendar is not verified (never loaded, or older than ``max_calendar_age_hours``)
new entries are BLOCKED (default) or allowed (``on_calendar_unavailable="allow"``), always logged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

import pandas as pd

from smc.config import NewsConfig
from smc.news.calendar import CalendarService
from smc.news.models import EconomicEvent, Impact, LockDecision
from smc.news.scenarios import Outcome, ScenarioAnalyzer
from smc.utils.logger import get_logger

log = get_logger(__name__)

_PAIR = re.compile(r"^([A-Z]{3})([A-Z]{3})")


@dataclass(frozen=True)
class Window:
    event: EconomicEvent
    start: pd.Timestamp
    end: pd.Timestamp  # exclusive


def symbol_currencies(symbol: str) -> Optional[set[str]]:
    """XAUUSD -> {XAU, USD}; EURUSD -> {EUR, USD}; unknown shapes (indices) -> None (= treat as exposed)."""
    m = _PAIR.match(symbol.upper())
    return {m.group(1), m.group(2)} if m else None


class NewsGuard:
    def __init__(self, service: CalendarService, cfg: NewsConfig, analyzer: Optional[ScenarioAnalyzer] = None,
                 now_fn: Optional[Callable[[], pd.Timestamp]] = None) -> None:
        self._svc, self._cfg = service, cfg
        self._an = analyzer or ScenarioAnalyzer()
        self._now = now_fn or (lambda: pd.Timestamp.now(tz="UTC"))
        self._last_unverified_log: Optional[pd.Timestamp] = None

    @property
    def service(self) -> CalendarService:
        return self._svc

    # --------------------------------------------------------------- selection
    def qualifies(self, e: EconomicEvent) -> bool:
        return e.impact.value in self._cfg.impact_levels and e.currency in self._cfg.currencies

    def affects(self, symbol: str, e: EconomicEvent) -> bool:
        cur = symbol_currencies(symbol)
        return True if cur is None else e.currency in cur

    def base_window(self, e: EconomicEvent) -> Window:
        return Window(e, e.time - pd.Timedelta(minutes=self._cfg.lock_minutes_before),
                      e.time + pd.Timedelta(minutes=self._cfg.lock_minutes_after))

    def effective_end(self, e: EconomicEvent, now: pd.Timestamp) -> tuple[pd.Timestamp, str]:
        """End of the lock including late-actual / mixed-outcome extensions that apply right now."""
        base_end = self.base_window(e).end
        if now < base_end:
            return base_end, "event"
        if self._svc.supports_actuals and not e.has_actual:
            end = base_end + pd.Timedelta(minutes=self._cfg.delay_extension_minutes)
            if now < end:
                return end, "awaiting_actual"
        elif e.has_actual and self._cfg.mixed_extra_lock_minutes > 0:
            group = self._an.release_group(e, self._svc.events())
            if self._an.classify_release(group).outcome is Outcome.MIXED:
                end = base_end + pd.Timedelta(minutes=self._cfg.mixed_extra_lock_minutes)
                if now < end:
                    return end, "mixed_extension"
        return base_end, "event"

    def qualifying_events(self, symbol: Optional[str] = None) -> list[EconomicEvent]:
        return [e for e in self._svc.events() if self.qualifies(e) and (symbol is None or self.affects(symbol, e))]

    # ------------------------------------------------------------------- check
    def check(self, symbol: str, now: Optional[pd.Timestamp] = None) -> LockDecision:
        now = now or self._now()
        health = self._svc.health(now)
        if not health.verified:
            msg = health.describe()
            if self._cfg.on_calendar_unavailable == "block":
                self._log_unverified(now, f"{msg} - fail-safe: new entries BLOCKED")
                return LockDecision(True, "calendar_unverified",
                                    (f"economic calendar not verified ({msg}); fail-safe blocks new entries",))
            self._log_unverified(now, f"{msg} - policy 'allow': continuing WITHOUT verified news data")

        best: Optional[LockDecision] = None
        for e in self.qualifying_events(symbol):
            w = self.base_window(e)
            if now < w.start:
                continue
            end, code = self.effective_end(e, now)
            if now >= end:
                continue
            reason = (f"{e.impact.value}-impact {e.currency} '{e.title}' scheduled {e.time:%Y-%m-%d %H:%M} UTC; "
                      f"lock {w.start:%H:%M}-{end:%H:%M} UTC"
                      + ("" if code == "event" else f" (extended: {code.replace('_', ' ')})"))
            dec = LockDecision(True, code, (reason,), e, w.start, end)
            if best is None or (best.lock_end is not None and end > best.lock_end):
                best = dec
        return best or LockDecision(False)

    def next_lock_start(self, symbol: str, now: Optional[pd.Timestamp] = None) -> Optional[pd.Timestamp]:
        """Start of the next lock window for this symbol (used as a broker-side pending-order expiry)."""
        now = now or self._now()
        starts = [self.base_window(e).start for e in self.qualifying_events(symbol) if self.base_window(e).start > now]
        return min(starts) if starts else None

    def _log_unverified(self, now: pd.Timestamp, msg: str) -> None:
        if self._last_unverified_log is None or (now - self._last_unverified_log) >= pd.Timedelta(minutes=10):
            self._last_unverified_log = now
            log.warning("NEWS: %s", msg)
