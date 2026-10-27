"""Durable execution state (JSON, atomic writes).

It exists so that a restart can never (a) place the same plan twice, (b) repeat a
partial close / break-even, or (c) reset the daily baseline. It is a CACHE of facts
the broker also knows: every decision that matters is re-checked against MT5 first.

Sections
  intents   exec_key -> what we tried to send and what we know about the outcome
  managed   position ticket -> ManagedPosition (R maths + management progress)
  baselines trading-day key -> start-of-day balance used for the 2% daily limit
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from smc.utils.logger import get_logger

log = get_logger(__name__)

# intent statuses
SENDING = "sending"  # written BEFORE order_send: a crash here leaves an unknown outcome
PLACED = "placed"  # pending order accepted by the broker
FILLED = "filled"  # position exists
UNCERTAIN = "uncertain"  # timeout / no answer: reconcile with MT5 before anything else
FAILED = "failed"  # definitively not executed (may be retried while attempts remain)
REJECTED = "rejected"  # broker refused; never auto-retried
CANCELED = "canceled"  # pending order removed / expired without filling


@dataclass
class ManagedPosition:
    ticket: int
    key: str
    plan_id: str
    symbol: str  # canonical name
    broker_symbol: str
    is_buy: bool
    entry: float
    initial_sl: float
    initial_volume: float
    tp: float
    setup_tf: str
    opened_at: str  # ISO UTC
    deadline: Optional[str]  # ISO UTC; None = exempt from the 23:00 flatten
    partial_done: bool = False
    partial_skipped: bool = False
    partial_volume: float = 0.0
    be_done: bool = False
    last_trail_swing: Optional[str] = None
    adopted: bool = False  # found in MT5 without an intent record (risk basis = current SL)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ManagedPosition":
        return cls(**d)


class StateStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path else None  # None = memory only (tests)
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {"version": 1, "intents": {}, "managed": {}, "baselines": {}, "notified": {}}
        if self._path and self._path.exists():
            try:
                loaded = json.loads(self._path.read_text(encoding="utf-8"))
                for k in self._data:
                    if k in loaded:
                        self._data[k] = loaded[k]
            except (OSError, ValueError) as exc:  # corrupt file: keep a copy, start safe
                backup = self._path.with_suffix(".corrupt")
                log.error("State file unreadable (%s); moved to %s. MT5 reconciliation will rebuild it.",
                          exc, backup)
                try:
                    os.replace(self._path, backup)
                except OSError:
                    pass

    # ------------------------------------------------------------------ save
    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._path)  # atomic on Windows and POSIX

    # --------------------------------------------------------------- intents
    def get_intent(self, key: str) -> Optional[dict[str, Any]]:
        with self._lock:
            v = self._data["intents"].get(key)
            return dict(v) if v else None

    def set_intent(self, key: str, **fields: Any) -> dict[str, Any]:
        """Create or update an intent (merge). Persisted immediately."""
        with self._lock:
            cur = self._data["intents"].setdefault(key, {"attempts": 0})
            cur.update(fields)
            self._save()
            return dict(cur)

    def intents(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._data["intents"].items()}

    # --------------------------------------------------------------- managed
    def get_managed(self, ticket: int) -> Optional[ManagedPosition]:
        with self._lock:
            v = self._data["managed"].get(str(ticket))
            return ManagedPosition.from_dict(v) if v else None

    def save_managed(self, mp: ManagedPosition) -> None:
        with self._lock:
            self._data["managed"][str(mp.ticket)] = mp.to_dict()
            self._save()

    def managed_tickets(self) -> list[int]:
        with self._lock:
            return [int(k) for k in self._data["managed"]]

    def drop_managed(self, ticket: int) -> None:
        with self._lock:
            self._data["managed"].pop(str(ticket), None)
            self._save()

    # ---------------------------------------------------------- notifications
    def is_notified(self, key: str) -> bool:
        with self._lock:
            return key in self._data["notified"]

    def mark_notified(self, key: str, when: str = "") -> None:
        """Remember an alert was delivered, so a restart cannot repeat it."""
        with self._lock:
            self._data["notified"][key] = when or "1"
            if len(self._data["notified"]) > 2000:  # keep memory bounded: drop the oldest 500
                for old in list(self._data["notified"])[:500]:
                    del self._data["notified"][old]
            self._save()

    # ------------------------------------------------------------- baselines
    def get_baseline(self, day: str) -> Optional[float]:
        with self._lock:
            return self._data["baselines"].get(day)

    def set_baseline(self, day: str, value: float) -> None:
        with self._lock:
            self._data["baselines"][day] = float(value)
            for old in sorted(self._data["baselines"])[:-14]:  # keep two weeks
                del self._data["baselines"][old]
            self._save()
