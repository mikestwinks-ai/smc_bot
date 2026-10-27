"""Pre-news scenarios and post-release classification: Hotter / Cooler / Mixed.

PURE INTERPRETATION. This module imports nothing from planning, risk or execution and returns
plain data - it cannot create, size or send a trade. Callers may use it for context, alerts
or a configured risk restriction; it never overrides the SMC structure engine.

Definitions (event- and unit-aware)
-----------------------------------
HOTTER = the release signals a STRONGER economy / HIGHER inflation than the consensus
         (typically read as hawkish -> supportive for USD, a headwind for gold).
COOLER = WEAKER economy / LOWER inflation than consensus (typically dovish).
MIXED  = in line with consensus, components disagree, data missing, units inconsistent, or the
         event type's directionality is not established here.
"Higher" is NOT always hotter: unemployment rate and jobless claims are INVERSE (higher = cooler).
Only the supported event families below are interpreted; everything else is MIXED by design.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from smc.news.models import EconomicEvent, Impact, ParsedValue, normalize_title


class Outcome(Enum):
    HOTTER = "hotter"
    COOLER = "cooler"
    MIXED = "mixed"


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern
    direct: bool  # True: higher actual => HOTTER. False (inverse): higher actual => COOLER
    kind: str  # expected ParsedValue.kind
    tolerance: float  # |actual - forecast| <= tolerance counts as "in line" (same units as the number)
    note: str


def _r(pattern: str) -> re.Pattern:
    return re.compile(pattern)


# Order matters: first match wins.
RULES: tuple[Rule, ...] = (
    Rule("unemployment rate", _r(r"\bunemployment rate\b"), False, "pct", 0.0,
         "a higher unemployment rate signals a weaker labour market"),
    Rule("jobless claims", _r(r"\b(unemployment|jobless|initial|continuing) claims\b"), False, "abs", 5_000.0,
         "more claims signal a weaker labour market"),
    Rule("payrolls", _r(r"non-?farm|\bnfp\b|\badp\b"), True, "abs", 15_000.0,
         "more jobs than expected signal a stronger labour market"),
    Rule("hourly earnings", _r(r"average hourly earnings|hourly earnings"), True, "pct", 0.0,
         "faster wage growth signals stronger labour-driven inflation"),
    Rule("job openings", _r(r"\bjolts\b|job openings"), True, "abs", 100_000.0,
         "more openings signal a tighter labour market"),
    Rule("inflation", _r(r"\b(cpi|ppi|pce)\b|price index|import prices|inflation"), True, "pct", 0.0,
         "higher inflation than expected is hawkish"),
    Rule("growth/activity", _r(r"\bgdp\b|retail sales|industrial production|durable goods|factory orders"), True,
         "pct", 0.0, "stronger activity than expected is hawkish"),
    Rule("business surveys", _r(r"\bism\b|\bpmi\b|empire state|philly fed|philadelphia fed"), True, "num", 0.2,
         "a stronger survey reading signals stronger activity"),
    Rule("consumer surveys", _r(r"consumer (sentiment|confidence)|\buom\b"), True, "num", 0.5,
         "stronger confidence signals stronger spending"),
)


def match_rule(title: str) -> Optional[Rule]:
    t = normalize_title(title)
    return next((r for r in RULES if r.pattern.search(t)), None)


def _fmt_diff(d: float, kind: str) -> str:
    if kind == "pct":
        return f"{d:+.2f}pp"
    if kind == "abs":
        return f"{d / 1000:+,.0f}K" if abs(d) < 1e6 else f"{d / 1e6:+.2f}M"
    return f"{d:+.2f}"


def _fmt(v: Optional[ParsedValue]) -> str:
    return v.fmt() if v else "n/a"


@dataclass(frozen=True)
class Classification:
    event_id: str
    title: str
    outcome: Outcome
    explanation: str
    rule: Optional[str] = None  # None = unsupported event type
    actual: str = ""
    forecast: str = ""
    previous: str = ""


@dataclass(frozen=True)
class ReleaseAnalysis:
    currency: str
    outcome: Outcome
    explanation: str
    components: tuple[Classification, ...]
    market_note: str


@dataclass(frozen=True)
class PreScenario:
    event_id: str
    title: str
    supported: bool
    forecast: str
    previous: str
    hotter: str
    cooler: str
    mixed: str
    expectation: str  # what the consensus itself implies versus the previous reading
    hot_rule: str = ""  # short form, e.g. "above 0.30%"
    cool_rule: str = ""
    inline_rule: str = ""
    caveat: str = ("Conditional scenarios only - the actual result is unknown. Informational: no trade is created "
                   "from them and SMC structure still decides.")


MARKET_NOTES = {
    Outcome.HOTTER: ("Typically read as hawkish: supportive for USD, a headwind for gold via firmer rate "
                     "expectations. Initial spikes often reverse; this is context, not a signal."),
    Outcome.COOLER: ("Typically read as dovish: softer for USD, supportive for gold via lower rate "
                     "expectations. Initial spikes often reverse; this is context, not a signal."),
    Outcome.MIXED: "No clear read: expect two-way, volatile price action. No directional view is taken.",
}


class ScenarioAnalyzer:
    """Stateless; safe to share."""

    # ------------------------------------------------------------ post-release
    def classify(self, event: EconomicEvent) -> Classification:
        rule = match_rule(event.title)
        base = dict(event_id=event.id, title=event.title, actual=_fmt(event.actual),
                    forecast=_fmt(event.forecast), previous=_fmt(event.previous))
        if rule is None:
            return Classification(outcome=Outcome.MIXED, rule=None, explanation=(
                f"{event.title}: event type not in the supported list - directionality not established, so MIXED."), **base)
        a, f, p = event.actual, event.forecast, event.previous
        if a is None:
            return Classification(outcome=Outcome.MIXED, rule=rule.name,
                                  explanation=f"{event.title}: no actual value available yet.", **base)
        if f is None:
            return Classification(outcome=Outcome.MIXED, rule=rule.name, explanation=(
                f"{event.title}: no consensus available, so the result cannot be judged against expectations."), **base)
        if not (a.kind == f.kind == rule.kind):
            return Classification(outcome=Outcome.MIXED, rule=rule.name, explanation=(
                f"{event.title}: unit mismatch (actual {a.kind}, forecast {f.kind}, expected {rule.kind}) - not compared."),
                **base)
        diff = a.number - f.number
        detail = (f"{event.title}: actual {a.fmt()} vs forecast {f.fmt()}"
                  f"{f' (previous {p.fmt()})' if p else ''} -> {_fmt_diff(diff, rule.kind)} vs consensus")
        if abs(diff) <= rule.tolerance + 1e-9:
            return Classification(outcome=Outcome.MIXED, rule=rule.name,
                                  explanation=f"{detail}; within the in-line band, so MIXED.", **base)
        hotter = (diff > 0) == rule.direct
        out = Outcome.HOTTER if hotter else Outcome.COOLER
        how = "" if rule.direct else " (inverse metric: higher = weaker)"
        return Classification(outcome=out, rule=rule.name,
                              explanation=f"{detail}; {rule.note}{how} => {out.value.upper()}.", **base)

    def classify_release(self, events: list[EconomicEvent]) -> ReleaseAnalysis:
        """Combine every component released together (e.g. NFP + unemployment + wages)."""
        if not events:
            return ReleaseAnalysis("", Outcome.MIXED, "No events supplied.", (), MARKET_NOTES[Outcome.MIXED])
        comps = tuple(self.classify(e) for e in events)
        supported = [c for c in comps if c.rule is not None]
        if not supported:
            out, why = Outcome.MIXED, "none of the released components is a supported event type"
        else:
            outs = {c.outcome for c in supported}
            if len(outs) == 1 and Outcome.MIXED not in outs:
                out = next(iter(outs))
                why = f"all {len(supported)} supported component(s) point {out.value}"
            else:
                out = Outcome.MIXED
                why = "components conflict, are in line, or lack data: " + ", ".join(
                    f"{c.title}={c.outcome.value}" for c in supported)
        text = f"{out.value.upper()} - {why}. " + " | ".join(c.explanation for c in comps)
        return ReleaseAnalysis(events[0].currency, out, text, comps, MARKET_NOTES[out])

    @staticmethod
    def release_group(event: EconomicEvent, all_events: tuple[EconomicEvent, ...]) -> list[EconomicEvent]:
        """Events of the same currency released at the same instant (medium/high impact only)."""
        return [e for e in all_events if e.currency == event.currency and e.time == event.time
                and e.impact in (Impact.HIGH, Impact.MEDIUM)] or [event]

    # ------------------------------------------------------------- pre-release
    def pre_release(self, event: EconomicEvent) -> PreScenario:
        rule, f, p = match_rule(event.title), event.forecast, event.previous
        base = dict(event_id=event.id, title=event.title, forecast=_fmt(f), previous=_fmt(p))
        if rule is None or f is None or f.kind != rule.kind:
            why = ("event type not in the supported list" if rule is None
                   else "no usable consensus value" if f is None else "unit of the consensus is unexpected")
            msg = f"Cannot build conditional scenarios: {why}. Treat the release as MIXED / uncertain."
            return PreScenario(supported=False, hotter=msg, cooler=msg, mixed=msg, expectation=msg, **base)
        tol, u = rule.tolerance, rule.kind
        hi = f"above {_fmt_num(f.number + tol, u)}"
        lo = f"below {_fmt_num(f.number - tol, u)}"
        hot, cool = (hi, lo) if rule.direct else (lo, hi)
        expectation = "no previous value to compare"
        if p is not None and p.kind == f.kind:
            d = f.number - p.number
            expectation = (f"consensus {f.fmt()} vs previous {p.fmt()} ({_fmt_diff(d, u)}): "
                           + ("a steady reading is expected" if d == 0 else
                              f"consensus already expects {'a rise' if d > 0 else 'a fall'}"))
        inline = (f"equals {_fmt_num(f.number, u)} " if tol == 0
                  else f"is within {_fmt_num(f.number - tol, u)}-{_fmt_num(f.number + tol, u)} ")
        return PreScenario(
            supported=True,
            hotter=f"HOTTER if the actual prints {hot} ({rule.note}). {MARKET_NOTES[Outcome.HOTTER]}",
            cooler=f"COOLER if the actual prints {cool}. {MARKET_NOTES[Outcome.COOLER]}",
            mixed=(f"MIXED if the actual {inline}(in line), or sibling components of the same release conflict. "
                   f"{MARKET_NOTES[Outcome.MIXED]}"),
            expectation=expectation, hot_rule=hot, cool_rule=cool, inline_rule=inline.strip(), **base)


def _fmt_num(n: float, kind: str) -> str:
    if kind == "pct":
        return f"{n:.2f}%"
    if kind == "abs":
        return f"{n / 1000:,.0f}K" if abs(n) < 1e6 else f"{n / 1e6:.2f}M"
    return f"{n:.2f}"
