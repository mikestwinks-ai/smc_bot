"""Builders for Phase 5 tests: fake calendar provider, events, recording notifier, news-enabled env."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd

from smc.execution import ExecutionSystem
from smc.news import CalendarService, NewsGuard
from smc.news.models import EconomicEvent, Impact
from smc.news.providers import CalendarProvider, CalendarUnavailable
from smc.notify.telegram import Notifier
from tests.harness import Env, make_env


def ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="UTC")


def ev(at: str, title: str = "CPI m/m", currency: str = "USD", impact: Impact = Impact.HIGH,
       forecast: str = "0.3%", previous: str = "0.2%", actual: str = "") -> EconomicEvent:
    return EconomicEvent(ts(at), currency, title, impact, forecast, previous, actual, "test")


class FakeProvider(CalendarProvider):
    name = "fake"

    def __init__(self, events: Optional[list[EconomicEvent]] = None, now_fn=None, supports_actuals: bool = False):
        self.events = list(events or [])
        self.supports_actuals = supports_actuals
        self.fail: Optional[str] = None
        self.fetches = 0
        self.data_time: Optional[pd.Timestamp] = None
        self._now = now_fn or (lambda: pd.Timestamp.now(tz="UTC"))

    def fetch(self):
        self.fetches += 1
        if self.fail:
            raise CalendarUnavailable(self.fail)
        return list(self.events), (self.data_time or self._now())


class RecordingNotifier(Notifier):
    """Same de-duplication contract as the real one, but records instead of sending."""

    enabled = True

    def __init__(self, state=None):
        self.sent: list[tuple[str, Optional[str]]] = []
        self._seen: set[str] = set()
        self._state = state

    def notify(self, text, *, key=None, persist=True):
        if key is not None:
            if key in self._seen or (persist and self._state is not None and self._state.is_notified(key)):
                return False
            self._seen.add(key)
            if persist and self._state is not None:
                self._state.mark_notified(key)
        self.sent.append((text, key))
        return True

    def texts(self, needle: str = "") -> list[str]:
        return [t for t, _ in self.sent if needle in t]


def news_env(tmp_path: Path, events: Optional[list[EconomicEvent]] = None, live: bool = False,
             now: str = "2026-01-07 10:00", supports_actuals: bool = False, fail: Optional[str] = None, **news_cfg):
    env: Env = make_env(tmp_path, live=live, now=now)
    for k, v in news_cfg.items():
        setattr(env.cfg.news, k, v)
    clock = lambda: env.broker.now  # noqa: E731
    provider = FakeProvider(events, clock, supports_actuals)
    provider.fail = fail
    service = CalendarService(provider, env.cfg.news, tmp_path / "calendar_cache.json", clock)
    service.refresh(force=True)
    guard = NewsGuard(service, env.cfg.news, now_fn=clock)
    env.system = ExecutionSystem(env.cfg, env.conn, env.mgr, now_fn=clock, state_path=env.cfg.execution.state_path,
                                 news_guard=guard)
    return env, guard, service, provider
