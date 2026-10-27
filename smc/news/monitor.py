"""NewsMonitor: refreshes the calendar and turns calendar facts into one-time alerts.

It only OBSERVES and NOTIFIES. Blocking is the NewsGuard's job (stateless, called by the execution
pipeline); this class never touches orders. Alert de-duplication keys are persisted by the
notifier, so restarting inside a restricted window neither loses nor repeats alerts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

import pandas as pd

from smc.config import NewsConfig
from smc.news.guard import NewsGuard
from smc.news.models import EconomicEvent, Health
from smc.news.scenarios import Outcome, ScenarioAnalyzer
from smc.journal.store import JournalStore
from smc.notify import messages as msg
from smc.notify.telegram import Notifier
from smc.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class NewsStatus:
    health: str
    active_locks: list[str] = field(default_factory=list)
    upcoming: list[str] = field(default_factory=list)


class NewsMonitor:
    def __init__(self, guard: NewsGuard, cfg: NewsConfig, notifier: Notifier, tz_name: str = "UTC",
                 analyzer: Optional[ScenarioAnalyzer] = None,
                 now_fn: Optional[Callable[[], pd.Timestamp]] = None,
                 journal: Optional[JournalStore] = None) -> None:
        self._g, self._cfg, self._n, self._tz = guard, cfg, notifier, tz_name
        self._journal = journal
        self._an = analyzer or ScenarioAnalyzer()
        self._now = now_fn or (lambda: pd.Timestamp.now(tz="UTC"))
        self._last_health: Optional[Health] = None
        self._active_locks: set[str] = set()

    def tick(self, now: Optional[pd.Timestamp] = None) -> NewsStatus:
        now = now or self._now()
        svc = self._g.service
        svc.refresh()
        self._alert_changes()
        health = svc.health(now)
        self._alert_health(health, now)
        if self._journal is not None:
            health_key = (f"news-health:{svc.provider_name}:{health.status.value}:"
                          f"{health.last_success.isoformat() if health.last_success is not None else 'none'}:"
                          f"{health.consecutive_failures}")
            self._journal.record_event_best_effort(
                "news_calendar_status", occurred_at=now, idempotency_key=health_key,
                provider=svc.provider_name, status=health.status.value, verified=health.verified,
                last_success_utc=health.last_success, age_hours=health.age_hours,
                consecutive_failures=health.consecutive_failures, error=health.last_error,
            )
            for event in self._g.qualifying_events():
                self._journal.record_event_best_effort(
                    "news_event", occurred_at=event.time, idempotency_key=f"news-event:{event.id}",
                    correlation_id=event.id, symbol=None, currency=event.currency, title=event.title,
                    impact=event.impact.value, forecast=event.forecast_raw, previous=event.previous_raw,
                    actual=event.actual_raw, source=event.source,
                )
        status = NewsStatus(health.status.value)
        releases: dict[tuple[str, pd.Timestamp], list[EconomicEvent]] = {}
        for e in self._g.qualifying_events():
            releases.setdefault((e.currency, e.time), []).append(e)
        for (cur, when), group in sorted(releases.items(), key=lambda kv: kv[0][1]):
            gid = f"{cur}|{when:%Y%m%dT%H%M}"
            start = min(self._g.base_window(e).start for e in group)
            ends = [self._g.effective_end(e, now) for e in group]
            end, code = max(ends, key=lambda x: x[0])
            base_end = max(self._g.base_window(e).end for e in group)
            ahead = (when - now).total_seconds() / 60.0
            if 0 <= ahead <= self._cfg.alert_ahead_minutes:
                status.upcoming.append(gid)
                self._n.notify(msg.upcoming(group, ahead, self._tz, start, base_end), key=f"upcoming:{gid}")
                self._n.notify(msg.pre_scenarios([self._an.pre_release(e) for e in group]), key=f"scenario:{gid}")
            if start <= now < end:
                status.active_locks.append(gid)
                if self._n.notify(msg.lock_on(group, end, self._tz, code), key=f"lock_on:{gid}"):
                    log.info("NEWS LOCK ON: %s | lock %s-%s UTC (%s)", "; ".join(msg.event_line(e, self._tz) for e in group),
                             f"{start:%H:%M}", f"{end:%H:%M}", code)
            elif now >= end and (now - end) <= pd.Timedelta(minutes=30):  # recent end only: no spam about old events
                if self._n.notify(msg.lock_off(group, self._tz), key=f"lock_off:{gid}"):
                    log.info("NEWS LOCK RELEASED: %s", "; ".join(msg.event_line(e, self._tz) for e in group))
            self._alert_outcome(group[0], now)
        if self._journal is not None:
            current = set(status.active_locks)
            for lock_id in current - self._active_locks:
                self._journal.record_event_best_effort(
                    "news_lock_on", occurred_at=now, idempotency_key=f"news-lock-on:{lock_id}",
                    correlation_id=lock_id, event_id=lock_id,
                )
            for lock_id in self._active_locks - current:
                self._journal.record_event_best_effort(
                    "news_lock_off", occurred_at=now,
                    idempotency_key=f"news-lock-off:{lock_id}:{now.isoformat()}",
                    correlation_id=lock_id, event_id=lock_id,
                )
            self._active_locks = current
        return status

    # ------------------------------------------------------------------ pieces
    def _alert_outcome(self, e: EconomicEvent, now: pd.Timestamp) -> None:
        if now < e.time or not e.has_actual:
            return
        group = self._an.release_group(e, self._g.service.events())
        if e is not group[0] and e.id != group[0].id:
            return  # one outcome message per release, sent when processing its first component
        analysis = self._an.classify_release(group)
        self._n.notify(msg.outcome(analysis, msg.when(e.time, self._tz)), key=f"outcome:{e.currency}|{e.time:%Y%m%dT%H%M}")
        if analysis.outcome is Outcome.MIXED:
            log.info("NEWS RESULT MIXED: %s", analysis.explanation[:300])

    def _alert_changes(self) -> None:
        for c in self._g.service.changes:
            if c.kind == "rescheduled" and self._g.qualifies(c.event):
                self._n.notify(f"NEWS RESCHEDULED\n{msg.event_line(c.event, self._tz)}\nwas {msg.when(c.old_time, self._tz)}",
                               key=f"resched:{c.event.id}")
        self._g.service.changes = []

    def _alert_health(self, health, now: pd.Timestamp) -> None:
        if health.status is self._last_health:
            return
        prev, self._last_health = self._last_health, health.status
        if health.status is Health.OK:
            if prev is not None:
                self._n.notify("NEWS CALENDAR RECOVERED\n" + health.describe(), key=f"cal_ok:{now:%Y%m%d%H%M}", persist=False)
        else:
            policy = ("new entries are BLOCKED until it recovers" if self._cfg.on_calendar_unavailable == "block"
                      else "policy 'allow': trading continues WITHOUT verified news data")
            self._n.notify(f"NEWS CALENDAR NOT VERIFIED\n{health.describe()}\n{policy}",
                           key=f"cal_bad:{now:%Y%m%d%H%M}", persist=False)
