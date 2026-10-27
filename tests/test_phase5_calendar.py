"""Phase 5 - calendar parsing, providers, caching, freshness, fail-safe inputs."""

from __future__ import annotations

import json
import urllib.error

import pandas as pd
import pytest

from smc.config import NewsConfig
from smc.news import (
    CalendarService, CalendarUnavailable, FaireconomyProvider, JsonFileProvider, MergedProvider,
)
from smc.news.calendar import dedupe, diff_snapshots
from smc.news.models import Health, Impact, parse_value
from tests.news_helpers import FakeProvider, ev, ts


# ------------------------------------------------------------------- parsing
@pytest.mark.parametrize("raw,number,kind", [
    ("0.3%", 0.3, "pct"), ("-0.1%", -0.1, "pct"), ("215K", 215_000.0, "abs"), ("-1.25M", -1_250_000.0, "abs"),
    ("$5.2B", 5.2e9, "abs"), ("52.3", 52.3, "num"), ("1,234", 1234.0, "num"), ("4.5 %", 4.5, "pct")])
def test_values_are_parsed_with_their_unit_kind(raw, number, kind):
    v = parse_value(raw)
    assert v is not None and v.number == pytest.approx(number) and v.kind == kind


@pytest.mark.parametrize("raw", ["", None, "n/a", "<0.1%", "4.25%-4.50%", "abc", "-", "1.2.3"])
def test_ambiguous_or_missing_values_are_none_not_guesses(raw):
    assert parse_value(raw) is None


# ---------------------------------------------------------------- faireconomy
FEED = json.dumps([
    {"title": "CPI m/m", "country": "USD", "date": "2026-01-14T08:30:00-05:00", "impact": "High",
     "forecast": "0.3%", "previous": "0.2%"},
    {"title": "Bank Holiday", "country": "USD", "date": "2026-01-19T00:00:00-05:00", "impact": "Holiday",
     "forecast": "", "previous": ""},
    {"title": "Retail Sales m/m", "country": "EUR", "date": "2026-01-14T05:00:00+00:00", "impact": "Medium",
     "forecast": "0.1%", "previous": "-0.2%"}]).encode()


def provider(body=FEED, exc=None):
    def get(url, timeout):
        if exc:
            raise exc
        return body
    return FaireconomyProvider("https://example.invalid/feed.json", http_get=get, now_fn=lambda: ts("2026-01-12 00:00"))


def test_feed_is_normalised_to_utc_with_impacts():
    events, data_time = provider().fetch()
    assert data_time == ts("2026-01-12 00:00")
    cpi = events[0]
    assert cpi.time == ts("2026-01-14 13:30") and cpi.currency == "USD" and cpi.impact is Impact.HIGH
    assert cpi.forecast.number == 0.3 and cpi.previous.number == 0.2 and not cpi.has_actual
    assert events[1].impact is Impact.HOLIDAY and events[2].impact is Impact.MEDIUM
    assert cpi.id == "USD|cpi m/m|20260114T1330"


@pytest.mark.parametrize("kwargs,needle", [
    (dict(exc=urllib.error.HTTPError("https://x", 429, "Too Many", {}, None)), "HTTP 429"),
    (dict(exc=TimeoutError("secret-url timed out")), "unreachable"),
    (dict(body=b"<html>blocked</html>"), "non-JSON"),
    (dict(body=b"[]"), "no events"),
    (dict(body=b'[{"nope": 1}, {"nope": 2}]'), "not recognised"),
])
def test_feed_problems_raise_instead_of_returning_an_empty_calendar(kwargs, needle):
    with pytest.raises(CalendarUnavailable) as err:
        provider(**kwargs).fetch()
    assert needle in str(err.value) and "secret-url" not in str(err.value)


# ------------------------------------------------------------------ file provider
def write_cal(tmp_path, rows, **extra):
    p = tmp_path / "cal.json"
    p.write_text(json.dumps({"events": rows, **extra}))
    return p


def row(at, title="CPI m/m", **kw):
    return {"time": at, "currency": "USD", "title": title, "impact": "high", "forecast": "0.3%", "previous": "0.2%", **kw}


def test_file_provider_reads_actuals_and_treats_naive_times_as_utc(tmp_path):
    p = JsonFileProvider(write_cal(tmp_path, [row("2026-01-14 13:30", actual="0.4%")], updated_at="2026-01-14T14:00:00Z"))
    events, updated = p.fetch()
    assert events[0].time == ts("2026-01-14 13:30") and events[0].actual.number == 0.4 and updated == ts("2026-01-14 14:00")
    assert p.supports_actuals


def test_file_provider_failures_are_explicit(tmp_path):
    with pytest.raises(CalendarUnavailable):
        JsonFileProvider(tmp_path / "missing.json").fetch()
    with pytest.raises(CalendarUnavailable):
        JsonFileProvider(write_cal(tmp_path, [])).fetch()
    bad = tmp_path / "bad.json"
    bad.write_text("{ nope")
    with pytest.raises(CalendarUnavailable):
        JsonFileProvider(bad).fetch()


def test_merged_provider_overlays_actuals_and_survives_actuals_outage(tmp_path):
    primary = FakeProvider([ev("2026-01-14 13:30")], lambda: ts("2026-01-14 12:00"))
    actuals = JsonFileProvider(write_cal(tmp_path, [row("2026-01-14 13:30", actual="0.5%")]))
    merged = MergedProvider(primary, actuals)
    events, _ = merged.fetch()
    assert events[0].actual.number == 0.5 and events[0].forecast.number == 0.3 and merged.supports_actuals
    (tmp_path / "cal.json").unlink()
    events, _ = merged.fetch()
    assert events[0].actual is None  # schedule still delivered, actual simply unavailable


# --------------------------------------------------------------------- service
def service_for(tmp_path, prov, now, **cfg):
    clock = {"t": ts(now)}
    n = NewsConfig(**cfg)
    prov._now = lambda: clock["t"]  # the provider stamps data with the TEST clock
    return CalendarService(prov, n, tmp_path / "cache.json", lambda: clock["t"]), clock


def test_refresh_is_rate_limited_and_never_hammers_the_provider(tmp_path):
    prov = FakeProvider([ev("2026-01-07 13:30")])
    svc, clock = service_for(tmp_path, prov, "2026-01-07 09:00", refresh_minutes=60)
    assert svc.refresh() and not svc.refresh() and not svc.refresh() and prov.fetches == 1
    clock["t"] += pd.Timedelta(minutes=61)
    assert svc.refresh() and prov.fetches == 2


def test_failed_refresh_keeps_old_data_and_backs_off(tmp_path):
    prov = FakeProvider([ev("2026-01-07 13:30")])
    svc, clock = service_for(tmp_path, prov, "2026-01-07 09:00", refresh_minutes=60, retry_minutes=5)
    svc.refresh()
    prov.fail = "HTTP 429 from calendar feed"
    clock["t"] += pd.Timedelta(minutes=61)
    assert not svc.refresh() and len(svc.events()) == 1  # NOT replaced by an empty list
    h = svc.health()
    assert h.verified and h.consecutive_failures == 1 and "429" in h.last_error
    clock["t"] += pd.Timedelta(minutes=2)
    svc.refresh()
    assert prov.fetches == 2  # still inside the 5 minute back-off
    clock["t"] += pd.Timedelta(minutes=4)
    prov.fail = None
    assert svc.refresh() and svc.health().consecutive_failures == 0  # recovered


def test_freshness_states(tmp_path):
    prov = FakeProvider([ev("2026-01-07 13:30")])
    svc, clock = service_for(tmp_path, prov, "2026-01-07 09:00", max_calendar_age_hours=24)
    assert svc.health().status is Health.UNAVAILABLE and not svc.health().verified
    svc.refresh()
    assert svc.health().status is Health.OK
    clock["t"] += pd.Timedelta(hours=25)
    h = svc.health()
    assert h.status is Health.STALE and not h.verified and "last successful update" in h.describe()


def test_never_loaded_provider_failure_is_unavailable_not_empty(tmp_path):
    prov = FakeProvider()
    prov.fail = "calendar feed unreachable: URLError"
    svc, _ = service_for(tmp_path, prov, "2026-01-07 09:00")
    assert not svc.refresh() and svc.events() == () and svc.health().status is Health.UNAVAILABLE


def test_cache_survives_a_restart_and_keeps_the_original_data_time(tmp_path):
    prov = FakeProvider([ev("2026-01-07 13:30", actual="0.4%")])
    svc, clock = service_for(tmp_path, prov, "2026-01-07 09:00")
    svc.refresh()
    prov2 = FakeProvider()
    prov2.fail = "down"
    again = CalendarService(prov2, NewsConfig(), tmp_path / "cache.json", lambda: ts("2026-01-07 20:00"))
    assert len(again.events()) == 1 and again.events()[0].actual.number == 0.4
    assert again.health().age_hours == pytest.approx(11.0)  # age measured from the ORIGINAL fetch
    (tmp_path / "cache.json").write_text("garbage")
    assert CalendarService(prov2, NewsConfig(), tmp_path / "cache.json").events() == ()


def test_duplicates_revisions_and_actuals_are_handled():
    a, b = ev("2026-01-14 13:30"), ev("2026-01-14 13:30", actual="0.4%")
    assert dedupe([a, a, b])[0].actual_raw == "0.4%" and len(dedupe([a, a, b])) == 1
    moved = ev("2026-01-14 14:00")
    changes = diff_snapshots((a,), [moved])
    assert [(c.kind, c.old_time) for c in changes] == [("rescheduled", a.time)]
    assert [c.kind for c in diff_snapshots((a,), [b])] == ["actual"]
    assert diff_snapshots((a,), [a]) == []


def test_service_reports_reschedules_and_new_actuals_on_refresh(tmp_path):
    prov = FakeProvider([ev("2026-01-14 13:30")])
    svc, clock = service_for(tmp_path, prov, "2026-01-14 09:00", refresh_minutes=5)
    svc.refresh()
    prov.events = [ev("2026-01-14 14:00")]
    clock["t"] += pd.Timedelta(minutes=6)
    svc.refresh()
    assert [c.kind for c in svc.changes] == ["rescheduled"]
