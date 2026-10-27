"""Step 1 - Fractal swing point detection.

A swing HIGH at bar ``j`` needs ``high[j]`` strictly greater than the ``left``
bars before it and greater-or-equal to the ``right`` bars after it. The tie rule
(strict left / non-strict right) guarantees that two adjacent equal highs yield
exactly ONE swing (the first), while equal highs separated by more than the
window still produce two swings (needed for equal-high liquidity).
Swing LOWs mirror this. A swing is *confirmed* ``right`` bars after it forms.

Two entry points share the same rule:
  * :func:`detect_swings`      - vectorised, whole DataFrame (batch / tests)
  * :func:`is_swing_high_at`   - single bar on plain lists (used incrementally)
"""

from __future__ import annotations

from typing import Optional, Sequence

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from smc.core.timeframes import Timeframe
from smc.structure.models import SwingPoint, SwingType


def is_swing_high_at(highs: Sequence[float], j: int, left: int, right: int) -> bool:
    """Fractal test for position ``j`` of ``highs`` (needs left/right neighbours)."""
    if j - left < 0 or j + right >= len(highs):
        return False
    c = highs[j]
    return all(c > highs[k] for k in range(j - left, j)) and all(
        c >= highs[k] for k in range(j + 1, j + right + 1)
    )


def is_swing_low_at(lows: Sequence[float], j: int, left: int, right: int) -> bool:
    if j - left < 0 or j + right >= len(lows):
        return False
    c = lows[j]
    return all(c < lows[k] for k in range(j - left, j)) and all(
        c <= lows[k] for k in range(j + 1, j + right + 1)
    )


def detect_swings(
    df: pd.DataFrame,
    left: int = 5,
    right: int = 5,
    timeframe: Optional[Timeframe] = None,
    index_offset: int = 0,
) -> list[SwingPoint]:
    """Detect all confirmed swing highs/lows in a candle DataFrame.

    ``df`` needs ``high`` / ``low`` columns and a DatetimeIndex (any timeframe).
    Returned swings are sorted by index; the last ``right`` bars can never
    contain a confirmed swing yet.
    """
    if left < 1 or right < 1:
        raise ValueError("left and right must be >= 1")
    n, width = len(df), left + right + 1
    if n < width:
        return []

    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    times = df.index
    tf = timeframe if timeframe is not None else Timeframe.M5

    hw, lw = sliding_window_view(highs, width), sliding_window_view(lows, width)
    h_c, l_c = hw[:, left], lw[:, left]
    is_high = (h_c[:, None] > hw[:, :left]).all(axis=1) & (h_c[:, None] >= hw[:, left + 1:]).all(axis=1)
    is_low = (l_c[:, None] < lw[:, :left]).all(axis=1) & (l_c[:, None] <= lw[:, left + 1:]).all(axis=1)

    swings: list[SwingPoint] = []
    for row in np.flatnonzero(is_high | is_low):
        j = int(row) + left
        confirm = times[j + right]
        if is_high[row]:
            swings.append(SwingPoint(SwingType.HIGH, float(highs[j]), times[j], j + index_offset, confirm, tf))
        if is_low[row]:
            swings.append(SwingPoint(SwingType.LOW, float(lows[j]), times[j], j + index_offset, confirm, tf))
    return swings
