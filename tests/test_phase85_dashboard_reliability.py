from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from smc.core.timeframes import Timeframe
from smc.dashboard.charts import build_candlestick_chart
from smc.dashboard.pages import _layer_anchor_bias, _structure_bias_rows
from smc.dashboard.snapshot import _structure


SYMBOL_BIASES = {
    "XAUUSD": {"H4": "bullish", "M30": "bearish", "M15": "bearish", "M5": "bullish"},
    "EURUSD": {"H4": "bullish", "M30": "bearish", "M15": "bearish", "M5": "bullish"},
    "GBPUSD": {"H4": "bearish", "M30": "bullish", "M15": "bullish", "M5": "bearish"},
    "BTCUSD": {"H4": "bearish", "M30": "bullish", "M15": "bearish", "M5": "bullish"},
}


class _FakeEngine:
    def __init__(self, timeframes: tuple[Timeframe, ...], symbol_biases: dict[str, dict[str, str]]) -> None:
        self.timeframes = timeframes
        self.anchor_timeframe = timeframes[0]
        self._symbol_biases = symbol_biases

    def get_tracker(self, symbol: str, timeframe: Timeframe) -> SimpleNamespace:
        bias = self._symbol_biases[symbol][timeframe.label]
        return SimpleNamespace(
            bias=SimpleNamespace(value=bias),
            buffer=SimpleNamespace(times=[pd.Timestamp("2026-10-26T12:00:00Z")]),
        )


class _FakeHub:
    def __init__(self, symbol_biases: dict[str, dict[str, str]]) -> None:
        self.external = _FakeEngine((Timeframe.H4, Timeframe.M30), symbol_biases)
        self.internal = _FakeEngine((Timeframe.M15, Timeframe.M5), symbol_biases)

    def snapshot(self, symbol: str) -> SimpleNamespace:
        return SimpleNamespace(
            price=None,
            external_swings={},
            internal_swings={},
            premium_discount=SimpleNamespace(external=None, internal=None),
            order_blocks=[],
            fvgs=[],
            liquidity=[],
        )


@pytest.mark.parametrize("symbol", tuple(SYMBOL_BIASES))
def test_structure_snapshot_and_market_bias_mapping_for_supported_symbols(symbol: str) -> None:
    structure = _structure(_FakeHub(SYMBOL_BIASES), symbol)
    expected = SYMBOL_BIASES[symbol]

    assert structure["external_anchor_timeframe"] == "H4"
    assert structure["internal_anchor_timeframe"] == "M15"
    assert structure["external_bias"] == expected["H4"]
    assert structure["internal_bias"] == expected["M15"]
    assert structure["external_bias_by_timeframe"] == {
        "H4": expected["H4"],
        "M30": expected["M30"],
    }
    assert structure["internal_bias_by_timeframe"] == {
        "M15": expected["M15"],
        "M5": expected["M5"],
    }
    assert structure["external_bias_time_utc_by_timeframe"] == {
        "H4": "2026-10-26T12:00:00+00:00",
        "M30": "2026-10-26T12:00:00+00:00",
    }
    assert structure["internal_bias_time_utc_by_timeframe"] == {
        "M15": "2026-10-26T12:00:00+00:00",
        "M5": "2026-10-26T12:00:00+00:00",
    }
    assert _layer_anchor_bias(structure, "external") == ("H4", expected["H4"].title())
    assert _layer_anchor_bias(structure, "internal") == ("M15", expected["M15"].title())

    rows = _structure_bias_rows(structure)
    external_anchor_row = next(
        row for row in rows if row["layer"] == "External" and row["timeframe"] == "H4"
    )
    internal_anchor_row = next(
        row for row in rows if row["layer"] == "Internal" and row["timeframe"] == "M15"
    )
    assert external_anchor_row["bias"] == expected["H4"].title()
    assert internal_anchor_row["bias"] == expected["M15"].title()
    assert external_anchor_row["as_of_utc"] == "2026-10-26T12:00:00+00:00"
    assert internal_anchor_row["as_of_utc"] == "2026-10-26T12:00:00+00:00"


def test_bias_display_prefers_anchor_row_and_keeps_missing_values_unavailable() -> None:
    structure = {
        "external_anchor_timeframe": "H4",
        "external_bias": "bearish",
        "external_bias_by_timeframe": {"H4": "bullish", "M30": "bearish"},
        "internal_anchor_timeframe": "M15",
        "internal_bias": "bullish",
        "internal_bias_by_timeframe": {"M15": None, "M5": "bearish"},
        "zones": [{"bias": "bullish", "timeframe": "M5"}],
    }

    assert _layer_anchor_bias(structure, "external") == ("H4", "Bullish")
    assert _layer_anchor_bias(structure, "internal") == ("M15", "Not available")
    assert _layer_anchor_bias({"external_bias": "bearish"}, "external") == (None, "Bearish")


def _render_market_app(page_snapshot: dict[str, object]) -> None:
    from smc.dashboard.pages import render_market

    render_market(page_snapshot)


def _market_snapshot() -> dict[str, object]:
    candle = {
        "time_utc": "2026-10-26T12:00:00+00:00",
        "open": 1.0,
        "high": 1.2,
        "low": 0.9,
        "close": 1.1,
        "tick_volume": 10,
    }
    structures = {}
    for symbol, biases in SYMBOL_BIASES.items():
        structures[symbol] = {
            "external_anchor_timeframe": "H4",
            "internal_anchor_timeframe": "M15",
            "external_bias": "bearish" if biases["H4"] == "bullish" else "bullish",
            "internal_bias": "bullish" if biases["M15"] == "bearish" else "bearish",
            "external_bias_by_timeframe": {"H4": biases["H4"], "M30": biases["M30"]},
            "internal_bias_by_timeframe": {"M15": biases["M15"], "M5": biases["M5"]},
            "external_bias_time_utc_by_timeframe": {
                "H4": "2026-10-26T08:00:00+00:00",
                "M30": "2026-10-26T11:30:00+00:00",
            },
            "internal_bias_time_utc_by_timeframe": {
                "M15": "2026-10-26T11:45:00+00:00",
                "M5": "2026-10-26T11:55:00+00:00",
            },
            "zones": [{"id": f"{symbol}-zone", "bias": "bullish", "timeframe": "M5"}],
            "liquidity": [],
        }
    return {
        "available": True,
        "snapshot_status": "stale",
        "snapshot_age_seconds": 25.0,
        "generated_at_utc": "2026-10-26T12:00:00+00:00",
        "mt5": {"connection_status": "recent_success"},
        "market": {
            symbol: {
                "M15": {
                    "timeframe_seconds": 900,
                    "candles": [candle],
                    "last_closed_candle_utc": candle["time_utc"],
                    "stale": False,
                }
            }
            for symbol in SYMBOL_BIASES
        },
        "structure": structures,
    }


def test_market_render_keeps_bias_controls_and_expander_state_on_rerun() -> None:
    app_test = AppTest.from_function(
        _render_market_app,
        args=(_market_snapshot(),),
        default_timeout=20,
    ).run(timeout=20)

    assert not app_test.exception
    metrics = {item.label: item.value for item in app_test.metric}
    assert metrics["External bias (H4 anchor)"] == "Bullish"
    assert metrics["Internal bias (M15 anchor)"] == "Bearish"
    assert any("Values below are last-known state" in item.value for item in app_test.warning)
    assert app_test.selectbox[0].value == "XAUUSD"
    assert len(app_test.expander) == 1
    assert app_test.expander[0].proto.expanded is False

    app_test.selectbox[0].select("GBPUSD").run(timeout=20)
    assert app_test.selectbox[0].value == "GBPUSD"
    metrics = {item.label: item.value for item in app_test.metric}
    assert metrics["External bias (H4 anchor)"] == "Bearish"
    assert metrics["Internal bias (M15 anchor)"] == "Bullish"

    app_test.session_state["market_structure_levels"] = True
    app_test.run(timeout=20)

    assert not app_test.exception
    assert app_test.expander[0].proto.expanded is True
    assert app_test.selectbox[0].value == "GBPUSD"
    assert app_test.session_state["market_structure_levels"] is True


def test_chart_view_revision_is_stable_for_new_candle_data() -> None:
    first = build_candlestick_chart(
        [{"time_utc": "2026-10-26T12:00:00Z", "open": 1, "high": 2, "low": 0.5, "close": 1.5}],
        900,
        view_revision="EURUSD_M15",
    )
    refreshed = build_candlestick_chart(
        [
            {"time_utc": "2026-10-26T12:00:00Z", "open": 1, "high": 2, "low": 0.5, "close": 1.5},
            {"time_utc": "2026-10-26T12:15:00Z", "open": 1.5, "high": 2.1, "low": 1.2, "close": 1.9},
        ],
        900,
        view_revision="EURUSD_M15",
    )

    assert first is not None and refreshed is not None
    assert first.layout.uirevision == refreshed.layout.uirevision == "EURUSD_M15"
    assert first.layout.xaxis.uirevision == refreshed.layout.xaxis.uirevision == "EURUSD_M15"
    assert first.layout.yaxis.uirevision == refreshed.layout.yaxis.uirevision == "EURUSD_M15"
