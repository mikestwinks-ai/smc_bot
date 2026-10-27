"""Builds the calendar provider / service / guard from NewsConfig."""

from __future__ import annotations

from typing import Callable, Optional

import pandas as pd

from smc.config import BotConfig
from smc.core.exceptions import ConfigError
from smc.news.calendar import CalendarService
from smc.news.guard import NewsGuard
from smc.news.providers import CalendarProvider, FaireconomyProvider, JsonFileProvider, MergedProvider


def build_provider(config: BotConfig, now_fn: Optional[Callable[[], pd.Timestamp]] = None) -> CalendarProvider:
    n = config.news
    if n.provider == "file":
        if not n.calendar_file:
            raise ConfigError("CALENDAR_PROVIDER=file needs CALENDAR_FILE")
        primary: CalendarProvider = JsonFileProvider(n.calendar_file, now_fn)
    else:
        primary = FaireconomyProvider(n.calendar_url, n.request_timeout_seconds, now_fn=now_fn)
    if n.actuals_file:
        return MergedProvider(primary, JsonFileProvider(n.actuals_file, now_fn))
    return primary


def build_news(config: BotConfig, now_fn: Optional[Callable[[], pd.Timestamp]] = None
               ) -> tuple[CalendarService, NewsGuard]:
    service = CalendarService(build_provider(config, now_fn), config.news, config.news.cache_path, now_fn)
    return service, NewsGuard(service, config.news, now_fn=now_fn)
