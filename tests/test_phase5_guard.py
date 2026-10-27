"""Phase 5 - news lock windows, filters, fail-safe policy, restart recovery."""

from __future__ import annotations

import pandas as pd
import pytest

from smc.news import CalendarService, NewsGuard, symbol_currencies
from smc.news.models import Impact
from tests.news_helpers import FakeProvider, ev, news_env, ts

EVENT = "2026-01-07 13:30"  # 13:20 -> 13:40 lock


def guard_for(tmp_path, events, now="2026-01-07 09:00", supports_actuals=False, **cfg):
    env, guard, svc, prov = news_env(tmp_path, events, now=now, supports_actuals=supports_actuals, **cfg)
    return guard, svc, prov


# ------------------------------------------------------------- exact window
@pytest.mark.parametrize("now,locked", [
    ("2026-01-07 13:19:59", False), ("2026-01-07 13:20:00", True), ("2026-01-07 13:30:00", True),
    ("2026-01-07 13:39:59", True), ("2026-01-07 13:40:00", False)])
def test_lock_is_10_minutes_before_and_10_minutes_after_release(tmp_path, now, locked):
    guard, *_ = guard_for(tmp_path, [ev(EVENT)])
    assert guard.check("XAUUSD", ts(now)).locked is locked


def test_lock_reason_names_event_time_impact_currency_and_unlock_time(tmp_path):
    guard, *_ = guard_for(tmp_path, [ev(EVENT, title="Core CPI m/m")])
    d = guard.check("XAUUSD", ts("2026-01-07 13:25"))
    text = d.message()
    assert d.code == "event" and d.event.title == "Core CPI m/m" and d.lock_end == ts("2026-01-07 13:40")
    for needle in ("high-impact", "USD", "Core CPI m/m", "2026-01-07 13:30", "13:20-13:40"):
        assert needle in text


# --------------------------------------------------------------- filtering
def test_only_configured_impact_levels_lock(tmp_path):
    events = [ev(EVENT, title="Medium thing", impact=Impact.MEDIUM), ev(EVENT, title="Low", impact=Impact.LOW),
              ev(EVENT, title="Holiday", impact=Impact.HOLIDAY)]
    guard, *_ = guard_for(tmp_path, events)
    assert not guard.check("XAUUSD", ts("2026-01-07 13:30")).locked  # default: high only
    medium, *_ = guard_for(tmp_path / "m", events, impact_levels=("high", "medium"))
    d = medium.check("XAUUSD", ts("2026-01-07 13:30"))
    assert d.locked and d.event.title == "Medium thing"
    assert not medium.check("XAUUSD", ts("2026-01-07 14:00")).locked


def test_currency_filter_is_symbol_aware(tmp_path):
    eur = ev(EVENT, title="ECB Rate", currency="EUR")
    guard, *_ = guard_for(tmp_path, [eur])
    assert not guard.check("EURUSD", ts("2026-01-07 13:30")).locked  # EUR not monitored by default
    both, *_ = guard_for(tmp_path / "b", [eur], currencies=("USD", "EUR"))
    assert both.check("EURUSD", ts("2026-01-07 13:30")).locked
    assert not both.check("XAUUSD", ts("2026-01-07 13:30")).locked  # gold is XAU/USD: no EUR exposure
    usd, *_ = guard_for(tmp_path / "u", [ev(EVENT)])
    for sym in ("XAUUSD", "EURUSD", "GBPUSD", "BTCUSD", "US30"):  # a USD event touches every symbol quoting USD
        assert usd.check(sym, ts("2026-01-07 13:30")).locked, sym


def test_symbol_currency_extraction():
    assert symbol_currencies("XAUUSD") == {"XAU", "USD"} and symbol_currencies("eurusd") == {"EUR", "USD"}
    assert symbol_currencies("US30") is None  # unknown shape: guard treats it as exposed (conservative)


def test_overlapping_events_report_the_longest_lock(tmp_path):
    guard, *_ = guard_for(tmp_path, [ev("2026-01-07 13:30", title="A"), ev("2026-01-07 13:35", title="B")])
    d = guard.check("XAUUSD", ts("2026-01-07 13:32"))
    assert d.event.title == "B" and d.lock_end == ts("2026-01-07 13:45")


def test_next_lock_start(tmp_path):
    guard, *_ = guard_for(tmp_path, [ev("2026-01-07 13:30"), ev("2026-01-07 15:00", title="Later")])
    assert guard.next_lock_start("XAUUSD", ts("2026-01-07 09:00")) == ts("2026-01-07 13:20")
    assert guard.next_lock_start("XAUUSD", ts("2026-01-07 13:25")) == ts("2026-01-07 14:50")
    assert guard.next_lock_start("XAUUSD", ts("2026-01-07 16:00")) is None


# ------------------------------------------------------ fail-safe policy
def test_unverified_calendar_blocks_entries_by_default_and_explains_why(tmp_path):
    guard, svc, prov = guard_for(tmp_path, [ev(EVENT)])
    far_future = ts("2026-01-09 12:00")  # 51 hours after the last successful update -> stale
    d = guard.check("XAUUSD", far_future)
    assert d.locked and d.code == "calendar_unverified"
    assert "last successful update" in d.message() and "fail-safe" in d.message()


def test_never_loaded_calendar_blocks(tmp_path):
    env, guard, svc, prov = news_env(tmp_path)
    prov.fail = "feed unreachable"
    fresh = CalendarService(prov, env.cfg.news, None, lambda: ts("2026-01-07 09:00"))
    fresh.refresh(force=True)
    g = NewsGuard(fresh, env.cfg.news, now_fn=lambda: ts("2026-01-07 09:00"))
    d = g.check("XAUUSD", ts("2026-01-07 09:00"))
    assert d.locked and d.code == "calendar_unverified" and "never loaded" in d.message()


def test_allow_policy_trades_on_but_event_locks_still_apply_from_old_data(tmp_path):
    guard, *_ = guard_for(tmp_path, [ev("2026-01-09 13:30")], on_calendar_unavailable="allow")
    assert not guard.check("XAUUSD", ts("2026-01-09 12:00")).locked  # stale data, policy allow, no event near
    assert guard.check("XAUUSD", ts("2026-01-09 13:30")).locked  # a known event is still honoured


def test_recovery_unblocks_when_the_provider_returns(tmp_path):
    env, guard, svc, prov = news_env(tmp_path, [ev("2026-01-08 13:30")])
    later = ts("2026-01-08 12:00")  # 26h later: stale
    assert guard.check("XAUUSD", later).code == "calendar_unverified"
    env.broker.now = later
    prov.events = [ev("2026-01-08 15:00")]
    assert svc.refresh(force=True)
    assert not guard.check("XAUUSD", later).locked  # verified again, no event near


def test_restart_inside_a_restricted_window_gives_the_same_answer_even_if_the_provider_is_down(tmp_path):
    env, guard, svc, prov = news_env(tmp_path, [ev(EVENT)], now="2026-01-07 13:00")
    t = ts("2026-01-07 13:25")
    assert guard.check("XAUUSD", t).locked
    down = FakeProvider()
    down.fail = "feed down"
    reborn_svc = CalendarService(down, env.cfg.news, tmp_path / "calendar_cache.json", lambda: t)  # new process, cache only
    assert not reborn_svc.refresh(force=True)
    reborn = NewsGuard(reborn_svc, env.cfg.news, now_fn=lambda: t)
    d = reborn.check("XAUUSD", t)
    assert d.locked and d.code == "event" and d.event.title == "CPI m/m"


# ---------------------------------------------------- delayed / extended locks
def test_missing_actual_extends_the_lock_only_for_actual_capable_providers(tmp_path):
    with_actuals, *_ = guard_for(tmp_path, [ev(EVENT)], supports_actuals=True)
    d = with_actuals.check("XAUUSD", ts("2026-01-07 13:45"))
    assert d.locked and d.code == "awaiting_actual" and d.lock_end == ts("2026-01-07 13:55")
    assert not with_actuals.check("XAUUSD", ts("2026-01-07 13:56")).locked
    released, *_ = guard_for(tmp_path / "a", [ev(EVENT, actual="0.4%")], supports_actuals=True)
    assert not released.check("XAUUSD", ts("2026-01-07 13:45")).locked  # actual arrived: normal end
    no_actuals, *_ = guard_for(tmp_path / "n", [ev(EVENT)], supports_actuals=False)
    assert not no_actuals.check("XAUUSD", ts("2026-01-07 13:45")).locked  # feed can't deliver actuals: no endless lock


def test_optional_extra_lock_after_a_mixed_outcome(tmp_path):
    in_line = ev(EVENT, actual="0.3%")  # equals forecast -> MIXED
    off, *_ = guard_for(tmp_path, [in_line], supports_actuals=True)
    assert not off.check("XAUUSD", ts("2026-01-07 13:45")).locked  # default: no restriction
    on, *_ = guard_for(tmp_path / "on", [in_line], supports_actuals=True, mixed_extra_lock_minutes=10)
    d = on.check("XAUUSD", ts("2026-01-07 13:45"))
    assert d.locked and d.code == "mixed_extension" and d.lock_end == ts("2026-01-07 13:50")
    decisive, *_ = guard_for(tmp_path / "d", [ev(EVENT, actual="0.5%")], supports_actuals=True, mixed_extra_lock_minutes=10)
    assert not decisive.check("XAUUSD", ts("2026-01-07 13:45")).locked
