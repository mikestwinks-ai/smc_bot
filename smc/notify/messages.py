"""Plain-text alert formatting (no Markdown, so nothing can break on special characters)."""

from __future__ import annotations

from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from smc.models.plan import Plan
from smc.news.models import EconomicEvent
from smc.news.scenarios import PreScenario, ReleaseAnalysis


def when(ts: pd.Timestamp, tz: str = "UTC") -> str:
    s = f"{ts:%a %d %b %H:%M} UTC"
    return s if tz == "UTC" else f"{s} ({ts.tz_convert(ZoneInfo(tz)):%H:%M} {tz})"


def event_line(e: EconomicEvent, tz: str) -> str:
    vals = ", ".join(f"{n} {v}" for n, v in (("forecast", e.forecast_raw), ("previous", e.previous_raw),
                                              ("actual", e.actual_raw)) if v)
    return f"{e.currency} {e.impact.value.upper()} - {e.title} @ {when(e.time, tz)}" + (f" [{vals}]" if vals else "")


def upcoming(events: list[EconomicEvent], minutes: float, tz: str, lock_start: pd.Timestamp,
             lock_end: pd.Timestamp) -> str:
    lines = "\n".join(event_line(e, tz) for e in events)
    return (f"UPCOMING NEWS in {minutes:.0f} min\n{lines}\n"
            f"New entries blocked {lock_start:%H:%M}-{lock_end:%H:%M} UTC.")


def pre_scenarios(items: list[PreScenario]) -> str:
    """One message per release: each component's thresholds, then a single caution."""
    rows = []
    for s in items:
        if not s.supported:
            rows.append(f"- {s.title}: {s.hotter}")
        else:
            rows.append(f"- {s.title}: consensus {s.forecast} (previous {s.previous or 'n/a'}). "
                        f"HOTTER if {s.hot_rule}; COOLER if {s.cool_rule}; MIXED if {s.inline_rule}.")
    return ("PRE-NEWS SCENARIOS\n" + "\n".join(rows) +
            "\nA clear Hotter/Cooler needs the components to agree; conflicting or in-line parts = MIXED."
            "\nHotter is typically USD-supportive / gold-negative, Cooler the reverse - context, not a signal."
            f"\n{items[0].caveat}")


def lock_on(events: list[EconomicEvent], end: pd.Timestamp, tz: str, code: str) -> str:
    lines = "\n".join(event_line(e, tz) for e in events)
    return (f"NEWS LOCK ON\nCause:\n{lines}\nNo new entries; resting orders on affected symbols are cancelled.\n"
            f"Expected release of the lock: {when(end, tz)}" + ("" if code == "event" else f" (extended: {code})"))


def lock_off(events: list[EconomicEvent], tz: str) -> str:
    lines = "\n".join(event_line(e, tz) for e in events)
    return f"NEWS LOCK RELEASED\nAfter:\n{lines}\nNormal entry rules apply again."


def outcome(a: ReleaseAnalysis, when_txt: str) -> str:
    return (f"NEWS RESULT: {a.outcome.value.upper()} ({a.currency}, {when_txt})\n{a.explanation}\n"
            f"{a.market_note}\nContext only - no trade is created from this.")


def plan_line(p: Plan) -> str:
    rr = "n/a" if p.rr_estimate is None else f"{p.rr_estimate:.1f}"
    return (f"{p.symbol} {p.setup_timeframe} {p.direction.value.upper()} {p.entry_model.value}\n"
            f"Zone {p.entry_zone.bottom:g}-{p.entry_zone.top:g} | SL {p.invalidation.price:g} | "
            f"TP {', '.join(f'{t:g}' for t in p.targets) or 'n/a'} | RR {rr}")


def plan_event(kind: str, p: Plan, extra: str = "") -> str:
    head = {"created": "NEW PLAN", "upgraded": "PLAN UPGRADED", "canceled": "PLAN CANCELED", "flipped": "ZONE FLIPPED",
            "ready": "PLAN READY"}[kind]
    return f"{head} {p.plan_id}\n{plan_line(p)}\n{extra or p.reason}"[:900]
