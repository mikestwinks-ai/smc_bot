"""Normalized economic-calendar models. Providers translate THEIR format into these."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import pandas as pd


class Impact(Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    HOLIDAY = "holiday"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, raw: object) -> "Impact":
        key = str(raw or "").strip().lower()
        return {"high": cls.HIGH, "medium": cls.MEDIUM, "med": cls.MEDIUM, "low": cls.LOW,
                "holiday": cls.HOLIDAY, "non-economic": cls.HOLIDAY}.get(key, cls.UNKNOWN)


@dataclass(frozen=True)
class ParsedValue:
    """A calendar number with its KIND, so values are only compared like with like.

    kind "pct"  : percentages, number in percentage points   ("3.2%" -> 3.2)
    kind "abs"  : counts / money with a K/M/B/T suffix        ("215K" -> 215000.0)
    kind "num"  : plain index-like numbers                    ("52.3" -> 52.3)
    """

    number: float
    kind: str
    raw: str

    def fmt(self) -> str:
        return self.raw if self.raw else f"{self.number:g}"


_VALUE_RE = re.compile(r"^\s*([<>~]?)\s*([-+]?)\s*[$€£¥]?\s*([\d.,]+)\s*([%kmbtKMBT]?)\s*$")
_MULT = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}


def parse_value(raw: object) -> Optional[ParsedValue]:
    """Parse '0.3%', '-215K', '1.25M', '52.3'. Returns None for blanks, ranges and anything ambiguous."""
    text = str(raw).strip() if raw is not None else ""
    if not text or text.lower() in {"n/a", "na", "-", "none", "null", "nan"}:
        return None
    m = _VALUE_RE.match(text)
    if not m or m.group(1):  # '<0.1%' / '>2' are bounds, not values
        return None
    sign, digits, suffix = m.group(2), m.group(3).replace(",", ""), m.group(4)
    try:
        num = float(digits)
    except ValueError:
        return None
    num = -num if sign == "-" else num
    if suffix == "%":
        return ParsedValue(num, "pct", text)
    if suffix:
        return ParsedValue(num * _MULT[suffix.lower()], "abs", text)
    return ParsedValue(num, "num", text)


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9%/ .-]", "", title.lower())).strip()


@dataclass(frozen=True)
class EconomicEvent:
    time: pd.Timestamp  # scheduled release, tz-aware UTC
    currency: str  # 'USD'
    title: str
    impact: Impact
    forecast_raw: str = ""
    previous_raw: str = ""
    actual_raw: str = ""
    source: str = ""

    @property
    def id(self) -> str:
        return f"{self.currency}|{normalize_title(self.title)}|{self.time:%Y%m%dT%H%M}"

    @property
    def key(self) -> tuple[str, str]:
        """Identity ignoring the time, used to spot reschedules between refreshes."""
        return self.currency, normalize_title(self.title)

    @property
    def forecast(self) -> Optional[ParsedValue]:
        return parse_value(self.forecast_raw)

    @property
    def previous(self) -> Optional[ParsedValue]:
        return parse_value(self.previous_raw)

    @property
    def actual(self) -> Optional[ParsedValue]:
        return parse_value(self.actual_raw)

    @property
    def has_actual(self) -> bool:
        return bool(str(self.actual_raw).strip())


@dataclass(frozen=True)
class CalendarSnapshot:
    events: tuple[EconomicEvent, ...]
    fetched_at: pd.Timestamp  # when the data was last successfully obtained
    source: str
    supports_actuals: bool


@dataclass(frozen=True)
class CalendarChange:
    kind: str  # rescheduled | actual | new | removed
    event: EconomicEvent
    old_time: Optional[pd.Timestamp] = None


class Health(Enum):
    OK = "ok"
    STALE = "stale"  # data exists but is older than max_calendar_age_hours
    UNAVAILABLE = "unavailable"  # never loaded


@dataclass(frozen=True)
class CalendarHealth:
    status: Health
    last_success: Optional[pd.Timestamp]
    age_hours: Optional[float]
    last_error: str = ""
    consecutive_failures: int = 0

    @property
    def verified(self) -> bool:
        return self.status is Health.OK

    def describe(self) -> str:
        if self.last_success is None:
            return f"calendar never loaded{(' (' + self.last_error + ')') if self.last_error else ''}"
        return (f"calendar {self.status.value}: last successful update {self.last_success:%Y-%m-%d %H:%M} UTC "
                f"({self.age_hours:.1f}h ago){(', last error: ' + self.last_error) if self.last_error else ''}")


@dataclass(frozen=True)
class LockDecision:
    locked: bool
    code: str = "none"  # none | event | awaiting_actual | mixed_extension | calendar_unverified
    reasons: tuple[str, ...] = ()
    event: Optional[EconomicEvent] = None
    lock_start: Optional[pd.Timestamp] = None
    lock_end: Optional[pd.Timestamp] = None

    def message(self) -> str:
        return "; ".join(self.reasons)
