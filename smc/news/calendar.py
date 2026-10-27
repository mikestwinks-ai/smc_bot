"""Calendar service: refresh scheduling, caching, de-duplication, change detection, freshness.

* Never treats a failed/empty fetch as "no events": the previous snapshot is kept and the
  failure is recorded; trust in the data is judged by AGE (see ``health``).
* A disk cache lets a restart inside a restricted window still know today's events even if the
  provider is down - as long as the cached data is younger than ``max_calendar_age_hours``.
* Error text is kept short and token-free (providers never include URLs with secrets).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from smc.config import NewsConfig
from smc.news.models import (
    CalendarChange, CalendarHealth, CalendarSnapshot, EconomicEvent, Health, Impact,
)
from smc.news.providers import CalendarProvider, CalendarUnavailable
from smc.utils.logger import get_logger

log = get_logger(__name__)


def dedupe(events: list[EconomicEvent]) -> list[EconomicEvent]:
    """One entry per (currency, title, time); when a duplicate carries an actual, that one wins."""
    best: dict[str, EconomicEvent] = {}
    for e in events:
        cur = best.get(e.id)
        if cur is None or (e.has_actual and not cur.has_actual):
            best[e.id] = e
    return sorted(best.values(), key=lambda e: (e.time, e.currency, e.title))


def diff_snapshots(old: tuple[EconomicEvent, ...], new: list[EconomicEvent]) -> list[CalendarChange]:
    """Reschedules and newly-arrived actuals between two refreshes (for alerts and logs)."""
    out: list[CalendarChange] = []
    old_by_id = {e.id: e for e in old}
    old_by_key: dict[tuple[str, str], list[EconomicEvent]] = {}
    for e in old:
        old_by_key.setdefault(e.key, []).append(e)
    new_by_key: dict[tuple[str, str], list[EconomicEvent]] = {}
    for e in new:
        new_by_key.setdefault(e.key, []).append(e)
    for e in new:
        prev = old_by_id.get(e.id)
        if prev is not None:
            if e.has_actual and not prev.has_actual:
                out.append(CalendarChange("actual", e))
        elif e.key in old_by_key and len(old_by_key[e.key]) == 1 and len(new_by_key[e.key]) == 1:
            before = old_by_key[e.key][0]
            if abs((e.time - before.time).total_seconds()) <= 24 * 3600:
                out.append(CalendarChange("rescheduled", e, old_time=before.time))
    return out


class CalendarService:
    def __init__(self, provider: CalendarProvider, cfg: NewsConfig, cache_path: Optional[Path] = None,
                 now_fn: Optional[Callable[[], pd.Timestamp]] = None) -> None:
        self._p, self._cfg = provider, cfg
        self._cache = Path(cache_path) if cache_path else None
        self._now = now_fn or (lambda: pd.Timestamp.now(tz="UTC"))
        self.snapshot: Optional[CalendarSnapshot] = None
        self.changes: list[CalendarChange] = []  # changes found by the LAST successful refresh
        self._last_attempt: Optional[pd.Timestamp] = None
        self._last_ok = False
        self._last_error = ""
        self._failures = 0
        self._load_cache()

    @property
    def supports_actuals(self) -> bool:
        return self._p.supports_actuals

    @property
    def provider_name(self) -> str:
        return self._p.name

    # ---------------------------------------------------------------- refresh
    def due(self, now: pd.Timestamp) -> bool:
        if self._last_attempt is None:
            return True
        wait = self._cfg.refresh_minutes if self._last_ok else self._cfg.retry_minutes
        return (now - self._last_attempt) >= pd.Timedelta(minutes=wait)

    def refresh(self, force: bool = False) -> bool:
        """Refresh if due. True = fresh data obtained now. Failures keep the previous snapshot."""
        now = self._now()
        if not force and not self.due(now):
            return False
        self._last_attempt = now
        try:
            events, data_ts = self._p.fetch()
        except CalendarUnavailable as exc:
            self._last_ok, self._last_error = False, str(exc)
            self._failures += 1
            log.warning("Calendar refresh failed (%d in a row): %s. %s", self._failures, exc,
                        self.health(now).describe())
            return False
        events = dedupe(events)
        self.changes = diff_snapshots(self.snapshot.events, events) if self.snapshot else []
        for c in self.changes:
            log.info("Calendar change: %s %s %s", c.kind, c.event.id, f"(was {c.old_time})" if c.old_time else "")
        if self._failures:
            log.info("Calendar provider recovered after %d failed attempt(s).", self._failures)
        self.snapshot = CalendarSnapshot(tuple(events), data_ts, self._p.name, self._p.supports_actuals)
        self._last_ok, self._last_error, self._failures = True, "", 0
        self._save_cache()
        log.info("Calendar updated from %s: %d events (data time %s UTC).", self._p.name, len(events),
                 data_ts.strftime("%Y-%m-%d %H:%M"))
        return True

    # ----------------------------------------------------------------- queries
    def events(self) -> tuple[EconomicEvent, ...]:
        return self.snapshot.events if self.snapshot else ()

    def health(self, now: Optional[pd.Timestamp] = None) -> CalendarHealth:
        now = now or self._now()
        if self.snapshot is None:
            return CalendarHealth(Health.UNAVAILABLE, None, None, self._last_error, self._failures)
        age = (now - self.snapshot.fetched_at).total_seconds() / 3600.0
        status = Health.OK if age <= self._cfg.max_calendar_age_hours else Health.STALE
        return CalendarHealth(status, self.snapshot.fetched_at, age, self._last_error, self._failures)

    # ------------------------------------------------------------------- cache
    def _save_cache(self) -> None:
        if not self._cache or not self.snapshot:
            return
        data = {"fetched_at": self.snapshot.fetched_at.isoformat(), "source": self.snapshot.source,
                "supports_actuals": self.snapshot.supports_actuals,
                "events": [{"time": e.time.isoformat(), "currency": e.currency, "title": e.title,
                            "impact": e.impact.value, "forecast": e.forecast_raw, "previous": e.previous_raw,
                            "actual": e.actual_raw} for e in self.snapshot.events]}
        try:
            self._cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self._cache)
        except OSError as exc:
            log.warning("Could not write calendar cache: %s", type(exc).__name__)

    def _load_cache(self) -> None:
        if not self._cache or not self._cache.exists():
            return
        try:
            d = json.loads(self._cache.read_text(encoding="utf-8"))
            events = [EconomicEvent(pd.Timestamp(r["time"]), r["currency"], r["title"], Impact.parse(r["impact"]),
                                    r.get("forecast", ""), r.get("previous", ""), r.get("actual", ""), "cache")
                      for r in d["events"]]
            self.snapshot = CalendarSnapshot(tuple(dedupe(events)), pd.Timestamp(d["fetched_at"]),
                                             d.get("source", "cache"), bool(d.get("supports_actuals", False)))
            log.info("Calendar cache loaded: %d events from %s (data time %s UTC).", len(events),
                     self.snapshot.source, self.snapshot.fetched_at.strftime("%Y-%m-%d %H:%M"))
        except (OSError, ValueError, KeyError, TypeError):
            log.warning("Calendar cache unreadable; ignoring it (a fresh fetch is required).")
