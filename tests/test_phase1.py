"""Phase 1 tests - run without MetaTrader 5 installed:  pytest -q"""

from __future__ import annotations

import pytest

from smc.config import AssetClass, BotConfig, BrokerConfig, SymbolConfig
from smc.core.data_manager import CandleStore, MarketDataManager
from smc.core.exceptions import ConfigError, MT5ConnectionError, MT5DataError
from smc.core.mt5_connection import MT5Connection
from smc.core.timeframes import StructureLayer, Timeframe
from tests.fake_mt5 import FakeMT5


def make_config(**broker_kw) -> BotConfig:
    cfg = BotConfig(
        broker=BrokerConfig(retry_delay_seconds=0, max_connect_retries=3, **broker_kw),
        symbols=[SymbolConfig("XAUUSD", AssetClass.METAL, "m")],
    )
    cfg.data.fetch_retry_delay_seconds = 0
    return cfg


# ---------------------------------------------------------------- config
def test_default_config_is_valid():
    BotConfig().validate()


def test_layers_and_ordering():
    cfg = BotConfig()
    assert [t.label for t in cfg.timeframes.all] == ["H4", "M30", "M15", "M5"]
    assert cfg.timeframes.layer_of(Timeframe.H4) is StructureLayer.EXTERNAL
    assert cfg.timeframes.layer_of(Timeframe.M5) is StructureLayer.INTERNAL


def test_validation_catches_bad_values():
    cfg = BotConfig()
    cfg.risk.risk_pct_below_threshold = 0
    cfg.structure.fractal_left = 0
    with pytest.raises(ConfigError) as err:
        cfg.validate()
    assert "risk_pct_below_threshold" in str(err.value) and "Fractal" in str(err.value)


# ------------------------------------------------------------ connection
def test_connect_retries_then_succeeds():
    fake = FakeMT5(fail_init_times=2)
    conn = MT5Connection(make_config().broker, fake)
    conn.connect()
    assert conn.is_alive()


def test_connect_fails_after_max_retries():
    conn = MT5Connection(make_config().broker, FakeMT5(fail_init_times=99))
    with pytest.raises(MT5ConnectionError):
        conn.connect()


def test_reconnects_when_connection_drops():
    fake = FakeMT5()
    conn = MT5Connection(make_config().broker, fake)
    conn.connect()
    fake.initialized = False  # simulate dropped link
    conn.ensure_connected()
    assert conn.is_alive()


# ------------------------------------------------------------------ data
def test_initialize_and_update_detects_new_candles():
    fake, cfg = FakeMT5(), make_config()
    manager = MarketDataManager(MT5Connection(cfg.broker, fake), cfg)
    manager.initialize()

    df = manager.get_candles("XAUUSD", Timeframe.M5)
    assert len(df) == cfg.data.bars_per_timeframe[Timeframe.M5]
    assert str(df.index.tz) == "UTC" and df.index.is_monotonic_increasing

    assert manager.update_all() == []  # nothing new yet
    fake.extra_bars = 2  # two more candles closed
    events = manager.update_all()
    assert {e.timeframe for e in events} == set(cfg.timeframes.all)
    assert all(e.new_candles == 2 for e in events)
    assert events[0].timeframe is Timeframe.H4  # highest timeframe first


def test_unknown_symbol_is_skipped_but_all_failing_raises():
    cfg = make_config()
    cfg.symbols = [SymbolConfig("NOPE", AssetClass.FOREX, "m")]
    manager = MarketDataManager(MT5Connection(cfg.broker, FakeMT5()), cfg)
    with pytest.raises(MT5DataError):
        manager.initialize()
    assert "NOPE" in manager.failed_symbols


def test_candle_store_dedupes_and_trims():
    cfg = make_config()
    manager = MarketDataManager(MT5Connection(cfg.broker, FakeMT5()), cfg)
    frame = manager._rates_to_frame(FakeMT5().copy_rates_from_pos("XAUUSDm", 5, 1, 50))
    store = CandleStore("XAUUSD", Timeframe.M5, max_bars=40)
    store.replace(frame)
    assert len(store) == 40
    assert store.merge(frame) == 0  # identical data -> nothing new


def test_rates_to_frame_keeps_latest_duplicate_and_sorts_timestamps():
    cfg = make_config()
    manager = MarketDataManager(MT5Connection(cfg.broker, FakeMT5()), cfg)
    rates = [
        {"time": 120, "open": 2.0, "high": 3.0, "low": 1.0, "close": 2.5},
        {"time": 60, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5},
        {"time": 120, "open": 2.0, "high": 4.0, "low": 1.0, "close": 3.5},
    ]

    result = manager._rates_to_frame(rates)

    assert result.index.is_monotonic_increasing
    assert len(result) == 2
    assert result.iloc[-1]["high"] == 4.0


@pytest.mark.parametrize(
    "candle",
    [
        {"time": 60, "open": 2.0, "high": 1.0, "low": 0.0, "close": 1.5},
        {"time": 60, "open": 2.0, "high": 3.0, "low": 2.5, "close": 1.5},
        {"time": 60, "open": 2.0, "high": 1.0, "low": 2.0, "close": 1.5},
        {"time": 60, "open": float("inf"), "high": 3.0, "low": 1.0, "close": 2.0},
    ],
)
def test_rates_to_frame_rejects_invalid_ohlc(candle):
    cfg = make_config()
    manager = MarketDataManager(MT5Connection(cfg.broker, FakeMT5()), cfg)

    with pytest.raises(MT5DataError):
        manager._rates_to_frame([candle])
