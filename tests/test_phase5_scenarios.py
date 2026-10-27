"""Phase 5 - Hotter / Cooler / Mixed: event-aware, unit-aware, never trade-creating."""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from pathlib import Path

import pytest

from smc.news.models import Impact
from smc.news.scenarios import Outcome, ScenarioAnalyzer, match_rule
from tests.news_helpers import ev

AN = ScenarioAnalyzer()
T = "2026-01-09 13:30"


def cls(title, forecast, actual, previous="", **kw):
    return AN.classify(ev(T, title=title, forecast=forecast, actual=actual, previous=previous, **kw))


# ------------------------------------------------------- direct metrics
@pytest.mark.parametrize("title,forecast,actual,expected", [
    ("CPI m/m", "0.3%", "0.4%", Outcome.HOTTER), ("CPI m/m", "0.3%", "0.2%", Outcome.COOLER),
    ("Core PCE Price Index m/m", "0.2%", "0.3%", Outcome.HOTTER),
    ("Non-Farm Employment Change", "150K", "190K", Outcome.HOTTER), ("Non-Farm Employment Change", "150K", "100K", Outcome.COOLER),
    ("Average Hourly Earnings m/m", "0.3%", "0.4%", Outcome.HOTTER),
    ("ISM Manufacturing PMI", "49.0", "50.5", Outcome.HOTTER), ("ISM Manufacturing PMI", "49.0", "47.9", Outcome.COOLER),
    ("Retail Sales m/m", "0.4%", "0.1%", Outcome.COOLER), ("Advance GDP q/q", "2.0%", "2.5%", Outcome.HOTTER),
    ("Prelim UoM Consumer Sentiment", "70.0", "72.0", Outcome.HOTTER)])
def test_higher_means_hotter_for_inflation_activity_and_jobs(title, forecast, actual, expected):
    assert cls(title, forecast, actual).outcome is expected


# ---------------------------------------- inverse metrics: higher is NOT hotter
@pytest.mark.parametrize("title,forecast,actual,expected", [
    ("Unemployment Rate", "4.0%", "4.3%", Outcome.COOLER), ("Unemployment Rate", "4.0%", "3.8%", Outcome.HOTTER),
    ("Unemployment Claims", "220K", "245K", Outcome.COOLER), ("Unemployment Claims", "220K", "205K", Outcome.HOTTER)])
def test_inverse_metrics_flip_the_direction(title, forecast, actual, expected):
    c = cls(title, forecast, actual)
    assert c.outcome is expected
    assert "inverse" in c.explanation or "weaker" in c.explanation or "stronger" in c.explanation


# --------------------------------------------------------- in-line / tolerance
def test_in_line_results_are_mixed_with_the_tolerance_per_event_type():
    assert cls("CPI m/m", "0.3%", "0.3%").outcome is Outcome.MIXED
    nfp_close = cls("Non-Farm Employment Change", "150K", "162K")  # inside the +/-15K band
    assert nfp_close.outcome is Outcome.MIXED and "in-line band" in nfp_close.explanation
    assert cls("Non-Farm Employment Change", "150K", "166K").outcome is Outcome.HOTTER
    assert cls("ISM Manufacturing PMI", "49.0", "49.1").outcome is Outcome.MIXED


# ------------------------------------------------------------ unit awareness
def test_units_are_compared_like_with_like():
    assert cls("Non-Farm Employment Change", "0.15M", "190K").outcome is Outcome.HOTTER  # M vs K normalised
    mismatch = cls("CPI m/m", "0.3%", "215K")
    assert mismatch.outcome is Outcome.MIXED and "unit mismatch" in mismatch.explanation
    wrong_kind = cls("CPI m/m", "52.0", "53.0")  # an inflation rule expects percentages
    assert wrong_kind.outcome is Outcome.MIXED and "unit mismatch" in wrong_kind.explanation


# ------------------------------------------------- missing / unsupported -> MIXED
def test_missing_or_unsupported_data_is_mixed_never_invented():
    assert "no actual" in cls("CPI m/m", "0.3%", "").explanation and cls("CPI m/m", "0.3%", "").outcome is Outcome.MIXED
    no_consensus = cls("CPI m/m", "", "0.4%", previous="0.2%")
    assert no_consensus.outcome is Outcome.MIXED and "no consensus" in no_consensus.explanation  # previous is not an expectation
    for title in ("FOMC Statement", "Federal Funds Rate", "Fed Chair Powell Speaks", "Trade Balance", "10-y Bond Auction"):
        c = cls(title, "4.5%", "4.75%")
        assert c.outcome is Outcome.MIXED and c.rule is None and "not in the supported list" in c.explanation
    assert cls("CPI m/m", "0.3%", "abc").outcome is Outcome.MIXED


# --------------------------------------------------- releases with components
def release(*rows):
    return AN.classify_release([ev(T, title=t, forecast=f, actual=a) for t, f, a in rows])


def test_components_of_one_release_must_agree_for_a_clear_outcome():
    both_hot = release(("Non-Farm Employment Change", "150K", "200K"), ("Unemployment Rate", "4.0%", "3.8%"),
                       ("Average Hourly Earnings m/m", "0.3%", "0.4%"))
    assert both_hot.outcome is Outcome.HOTTER and len(both_hot.components) == 3
    conflict = release(("Non-Farm Employment Change", "150K", "200K"), ("Unemployment Rate", "4.0%", "4.3%"))
    assert conflict.outcome is Outcome.MIXED and "conflict" in conflict.explanation
    one_in_line = release(("CPI m/m", "0.3%", "0.4%"), ("Core CPI m/m", "0.3%", "0.3%"))
    assert one_in_line.outcome is Outcome.MIXED
    cool = release(("CPI m/m", "0.3%", "0.1%"), ("Core CPI m/m", "0.3%", "0.2%"), ("CPI y/y", "2.9%", "2.7%"))
    assert cool.outcome is Outcome.COOLER
    unsupported = release(("FOMC Statement", "", ""))
    assert unsupported.outcome is Outcome.MIXED


def test_release_group_collects_same_time_same_currency_events_only():
    a, b = ev(T, title="Non-Farm Employment Change"), ev(T, title="Unemployment Rate")
    other_time, other_cur = ev("2026-01-09 15:00", title="X"), ev(T, title="Y", currency="EUR")
    low = ev(T, title="Z", impact=Impact.LOW)
    group = AN.release_group(a, (a, b, other_time, other_cur, low))
    assert {e.title for e in group} == {"Non-Farm Employment Change", "Unemployment Rate"}


def test_explanation_shows_the_values_used():
    c = cls("CPI m/m", "0.3%", "0.4%", previous="0.2%")
    for needle in ("actual 0.4%", "forecast 0.3%", "previous 0.2%", "+0.10pp", "HOTTER"):
        assert needle in c.explanation


# ----------------------------------------------------------- pre-news scenarios
def test_pre_release_scenarios_are_conditional_and_numeric():
    s = AN.pre_release(ev(T, title="CPI m/m", forecast="0.3%", previous="0.2%"))
    assert s.supported and "if the actual prints above 0.30%" in s.hotter and "below 0.30%" in s.cooler
    assert "MIXED if" in s.mixed and "already expects a rise" in s.expectation
    assert "actual result is unknown" in s.caveat and "no trade is created" in s.caveat
    nfp = AN.pre_release(ev(T, title="Non-Farm Employment Change", forecast="180K", previous="200K"))
    assert "above 195K" in nfp.hotter and "below 165K" in nfp.cooler and "a fall" in nfp.expectation
    rate = AN.pre_release(ev(T, title="Unemployment Rate", forecast="4.0%", previous="4.0%"))
    assert "below 4.00%" in rate.hotter and "above 4.00%" in rate.cooler  # inverse metric flips the thresholds
    assert "steady" in rate.expectation


def test_pre_release_falls_back_to_uncertain_without_usable_data():
    for event in (ev(T, title="FOMC Statement", forecast=""), ev(T, title="CPI m/m", forecast=""),
                  ev(T, title="CPI m/m", forecast="215K")):
        s = AN.pre_release(event)
        assert not s.supported and "MIXED / uncertain" in s.hotter


def test_market_notes_are_cautious_context_not_signals():
    r = release(("CPI m/m", "0.3%", "0.5%"))
    assert "not a signal" in r.market_note and "reverse" in r.market_note
    assert match_rule("CPI m/m").name == "inflation" and match_rule("Something Else") is None


# ----------------------------------------------- separation from trading code
def test_scenario_module_cannot_trade_by_construction():
    code = ("import sys, smc.news.scenarios; "
            "bad=[m for m in sys.modules if m.startswith(('smc.execution','smc.risk','smc.planning'))]; "
            "assert not bad, bad")
    res = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parent.parent,
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    out = AN.classify_release([ev(T, title="CPI m/m", forecast="0.3%", actual="0.6%")])
    names = {f.name for c in out.components for f in dataclasses.fields(c)} | {f.name for f in dataclasses.fields(out)}
    assert not names & {"order", "volume", "lot", "entry", "stop_loss", "take_profit", "direction", "plan"}
