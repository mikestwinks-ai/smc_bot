"""Timeframe definitions and the External / Internal structure layers.

``Timeframe`` is independent of the MetaTrader5 package so that the rest of
the code base (and the unit tests) can run on machines without MT5 installed.
The MT5 constant is resolved lazily through :meth:`Timeframe.to_mt5`.
"""

from __future__ import annotations

from datetime import timedelta
from enum import Enum
from typing import Any


class Timeframe(Enum):
    """Supported chart timeframes (value = ``(label, minutes)``)."""

    M1 = ("M1", 1)
    M5 = ("M5", 5)
    M15 = ("M15", 15)
    M30 = ("M30", 30)
    H1 = ("H1", 60)
    H4 = ("H4", 240)
    D1 = ("D1", 1440)

    def __init__(self, label: str, minutes: int) -> None:
        self.label = label
        self.minutes = minutes

    # ------------------------------------------------------------------ props
    @property
    def seconds(self) -> int:
        return self.minutes * 60

    @property
    def delta(self) -> timedelta:
        return timedelta(minutes=self.minutes)

    @property
    def mt5_attr(self) -> str:
        """Name of the matching constant in the MetaTrader5 module."""
        return f"TIMEFRAME_{self.label}"

    # ---------------------------------------------------------------- helpers
    def to_mt5(self, mt5_module: Any) -> int:
        """Return the MetaTrader5 constant for this timeframe."""
        try:
            return getattr(mt5_module, self.mt5_attr)
        except AttributeError as exc:  # pragma: no cover - defensive
            raise ValueError(f"MT5 module has no constant {self.mt5_attr}") from exc

    @classmethod
    def from_str(cls, label: str) -> "Timeframe":
        """Parse ``"H4"`` / ``"h4"`` into ``Timeframe.H4``."""
        key = label.strip().upper()
        for tf in cls:
            if tf.label == key:
                return tf
        raise ValueError(f"Unknown timeframe '{label}'")

    def __str__(self) -> str:  # nicer log output
        return self.label


class StructureLayer(Enum):
    """The two independent structure layers tracked by the system."""

    EXTERNAL = "external"  # H4 + M30
    INTERNAL = "internal"  # M15 + M5
