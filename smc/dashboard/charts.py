"""Pure chart builders; empty or malformed market data is treated as unavailable."""

from __future__ import annotations

from typing import Any, Optional

import pandas as pd


def build_candlestick_chart(
    candles: list[dict[str, Any]], timeframe_seconds: int, view_revision: str = "market"
) -> Optional[Any]:
    """Build an interactive OHLC chart, returning None for empty/invalid candle data."""
    if not candles:
        return None
    try:
        import plotly.graph_objects as go
    except ImportError:
        return None
    frame = pd.DataFrame(candles)
    required = {"time_utc", "open", "high", "low", "close"}
    if frame.empty or not required.issubset(frame.columns):
        return None
    try:
        frame["time"] = pd.to_datetime(frame["time_utc"], utc=True, errors="coerce")
        for name in ("open", "high", "low", "close"):
            frame[name] = pd.to_numeric(frame[name], errors="coerce")
        frame = frame.dropna(subset=["time", "open", "high", "low", "close"]).sort_values("time")
        if frame.empty:
            return None
    except (TypeError, ValueError, OverflowError):
        return None

    figure = go.Figure(
        data=[
            go.Candlestick(
                x=frame["time"],
                open=frame["open"],
                high=frame["high"],
                low=frame["low"],
                close=frame["close"],
                increasing_line_color="#28c58b",
                decreasing_line_color="#ef5b68",
                increasing_fillcolor="#28c58b",
                decreasing_fillcolor="#ef5b68",
                whiskerwidth=0.6,
                hovertemplate=(
                    "UTC: %{x|%Y-%m-%d %H:%M}<br>"
                    "Open: %{open:.5f}<br>High: %{high:.5f}<br>"
                    "Low: %{low:.5f}<br>Close: %{close:.5f}<extra></extra>"
                ),
                name="OHLC",
            )
        ]
    )
    figure.update_layout(
        template="plotly_dark",
        height=460,
        margin={"l": 12, "r": 12, "t": 18, "b": 48},
        showlegend=False,
        dragmode="pan",
        uirevision=view_revision,
        meta={"bar_interval_seconds": max(1, int(timeframe_seconds))},
        xaxis={
            "title": "Time (UTC)",
            "tickformat": "%H:%M<br>%Y-%m-%d",
            "rangeslider": {"visible": False},
            "showgrid": True,
            "gridcolor": "#28323d",
            "autorange": True,
            "uirevision": view_revision,
        },
        yaxis={
            "title": "Price",
            "showgrid": True,
            "gridcolor": "#28323d",
            "autorange": True,
            "uirevision": view_revision,
        },
        hovermode="x",
    )
    return figure
