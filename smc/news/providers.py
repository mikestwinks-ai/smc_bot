"""Replaceable calendar sources. The rest of the bot only sees :class:`CalendarProvider`.

Honest scope notes
------------------
* The MetaTrader5 *Python* package does not (to my knowledge) expose the MQL5 economic calendar,
  so MT5 cannot be the source here.
* ``FaireconomyProvider`` reads the community ForexFactory JSON feed. It is UNOFFICIAL, carries
  forecast/previous but NO actual values, and may rate-limit or change. It has not been verified
  from the build environment (no network) - verify it on your machine first.
* ``JsonFileProvider`` reads a file you control (exported from any source) and CAN carry actuals.
* ``MergedProvider`` overlays actuals from a second provider onto a primary one.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

from smc.core.exceptions import SMCBotError
from smc.news.models import EconomicEvent, Impact


class CalendarUnavailable(SMCBotError):
    """The provider could not deliver a trustworthy calendar (network, format, empty, ...)."""


class CalendarProvider(ABC):
    name: str = "provider"
    supports_actuals: bool = False

    @abstractmethod
    def fetch(self) -> tuple[list[EconomicEvent], pd.Timestamp]:
        """Return ``(events, data_timestamp)``. Must RAISE CalendarUnavailable instead of returning
        an empty list when it cannot obtain data - an empty calendar is never assumed."""


def _to_utc(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


HttpGet = Callable[[str, float], bytes]


def default_http_get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (smc-bot calendar client)",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URL from config
        return resp.read()


class FaireconomyProvider(CalendarProvider):
    name = "faireconomy"
    supports_actuals = False

    def __init__(self, url: str, timeout: float = 10.0, http_get: Optional[HttpGet] = None,
                 now_fn: Optional[Callable[[], pd.Timestamp]] = None) -> None:
        self._url, self._timeout = url, timeout
        self._get = http_get or default_http_get
        self._now = now_fn or (lambda: pd.Timestamp.now(tz="UTC"))

    def fetch(self) -> tuple[list[EconomicEvent], pd.Timestamp]:
        try:
            body = self._get(self._url, self._timeout)
        except urllib.error.HTTPError as exc:  # 429 = rate limited
            raise CalendarUnavailable(f"HTTP {exc.code} from calendar feed") from None
        except Exception as exc:
            raise CalendarUnavailable(f"calendar feed unreachable: {type(exc).__name__}") from None
        try:
            rows = json.loads(body)
        except ValueError:
            raise CalendarUnavailable("calendar feed returned non-JSON content") from None
        if not isinstance(rows, list) or not rows:
            raise CalendarUnavailable("calendar feed returned no events (refusing to treat that as 'no news')")
        events, bad = [], 0
        for r in rows:
            try:
                events.append(EconomicEvent(
                    time=_to_utc(r["date"]), currency=str(r["country"]).strip().upper(), title=str(r["title"]).strip(),
                    impact=Impact.parse(r.get("impact")), forecast_raw=str(r.get("forecast") or "").strip(),
                    previous_raw=str(r.get("previous") or "").strip(), actual_raw=str(r.get("actual") or "").strip(),
                    source=self.name))
            except (KeyError, ValueError, TypeError):
                bad += 1
        if not events or bad > len(rows) // 2:
            raise CalendarUnavailable(f"calendar feed format not recognised ({bad}/{len(rows)} rows unreadable)")
        return events, self._now()


class JsonFileProvider(CalendarProvider):
    """``{"updated_at": "...", "events": [{"time","currency","title","impact","forecast","previous","actual"}]}``
    (a bare list of events is accepted too). Times without a zone are treated as UTC."""

    name = "file"
    supports_actuals = True

    def __init__(self, path: str | Path, now_fn: Optional[Callable[[], pd.Timestamp]] = None) -> None:
        self._path = Path(path)
        self._now = now_fn or (lambda: pd.Timestamp.now(tz="UTC"))

    def fetch(self) -> tuple[list[EconomicEvent], pd.Timestamp]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            mtime = pd.Timestamp(self._path.stat().st_mtime, unit="s", tz="UTC")
        except (OSError, ValueError) as exc:
            raise CalendarUnavailable(f"calendar file unreadable: {type(exc).__name__}") from None
        rows = raw.get("events") if isinstance(raw, dict) else raw
        if not isinstance(rows, list) or not rows:
            raise CalendarUnavailable("calendar file contains no events (refusing to treat that as 'no news')")
        updated = _to_utc(raw["updated_at"]) if isinstance(raw, dict) and raw.get("updated_at") else mtime
        events = []
        for r in rows:
            try:
                events.append(EconomicEvent(
                    time=_to_utc(r["time"]), currency=str(r["currency"]).strip().upper(), title=str(r["title"]).strip(),
                    impact=Impact.parse(r.get("impact")), forecast_raw=str(r.get("forecast") or "").strip(),
                    previous_raw=str(r.get("previous") or "").strip(), actual_raw=str(r.get("actual") or "").strip(),
                    source=self.name))
            except (KeyError, ValueError, TypeError) as exc:
                raise CalendarUnavailable(f"calendar file has an unreadable event: {type(exc).__name__}") from None
        return events, updated


class MergedProvider(CalendarProvider):
    """Primary events (schedule, forecast) + actual values from a second provider."""

    def __init__(self, primary: CalendarProvider, actuals: CalendarProvider) -> None:
        self._p, self._a = primary, actuals
        self.name = f"{primary.name}+{actuals.name}"
        self.supports_actuals = True

    def fetch(self) -> tuple[list[EconomicEvent], pd.Timestamp]:
        events, ts = self._p.fetch()
        try:
            extra, _ = self._a.fetch()
        except CalendarUnavailable:
            return events, ts  # schedule still valid; actuals simply unavailable this round
        lookup = {(e.currency, e.key[1], e.time.date()): e for e in extra if e.has_actual}
        merged = []
        for e in events:
            hit = lookup.get((e.currency, e.key[1], e.time.date()))
            merged.append(EconomicEvent(e.time, e.currency, e.title, e.impact, e.forecast_raw or (hit.forecast_raw if hit else ""),
                                        e.previous_raw, hit.actual_raw if hit else e.actual_raw, self.name))
        return merged, ts
