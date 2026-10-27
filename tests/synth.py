"""Synthetic candle builders for structure tests."""

from __future__ import annotations

import numpy as np
import pandas as pd

from smc.core.timeframes import Timeframe

T0 = pd.Timestamp("2026-01-05 00:00", tz="UTC")  # a Monday


def frame(rows: list[tuple[float, float, float, float]], tf: Timeframe = Timeframe.M15,
          start: pd.Timestamp = T0) -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(rows), freq=f"{tf.minutes}min", tz="UTC", name="time")
    arr = np.array(rows, dtype=float)
    df = pd.DataFrame(arr, columns=["open", "high", "low", "close"], index=idx)
    df["tick_volume"], df["spread"], df["real_volume"] = 100.0, 0.0, 0.0
    return df


def zigzag_rows(points: list[float], steps: int = 8, wick: float = 0.2) -> list[tuple[float, float, float, float]]:
    """Straight legs between waypoints; each candle opens at the previous close."""
    rows, prev = [], points[0]
    for target in points[1:]:
        for s in range(1, steps + 1):
            close = prev + (target - prev) * s / steps
            o = prev
            rows.append((o, max(o, close) + wick, min(o, close) - wick, close))
            prev = close
    return rows


def zigzag(points: list[float], tf: Timeframe = Timeframe.M15, steps: int = 8) -> pd.DataFrame:
    return frame(zigzag_rows(points, steps), tf)
