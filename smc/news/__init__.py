"""Phase 5 - economic calendar, news guard, pre/post-news scenarios."""

from smc.news.calendar import CalendarService
from smc.news.factory import build_news, build_provider
from smc.news.guard import NewsGuard, symbol_currencies
from smc.news.models import EconomicEvent, Impact, LockDecision
from smc.news.monitor import NewsMonitor
from smc.news.providers import (
    CalendarProvider, CalendarUnavailable, FaireconomyProvider, JsonFileProvider, MergedProvider,
)
from smc.news.scenarios import Outcome, ScenarioAnalyzer

__all__ = ["CalendarProvider", "CalendarService", "CalendarUnavailable", "EconomicEvent", "FaireconomyProvider",
           "Impact", "JsonFileProvider", "LockDecision", "MergedProvider", "NewsGuard", "NewsMonitor", "Outcome",
           "ScenarioAnalyzer", "build_news", "build_provider", "symbol_currencies"]
