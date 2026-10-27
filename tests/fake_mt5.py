"""Minimal in-memory stand-in for the MetaTrader5 module (runs on any OS)."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

RATE_DTYPE = [
    ("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"),
    ("close", "<f8"), ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8"),
]
_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


class FakeMT5:
    TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15 = 1, 5, 15
    TIMEFRAME_M30, TIMEFRAME_H1, TIMEFRAME_H4, TIMEFRAME_D1 = 30, 60, 240, 1440

    def __init__(self, symbols=("XAUUSDm",), total_bars=5000, fail_init_times=0):
        self.symbols = set(symbols)
        self.total_bars = total_bars  # bars of history, incl. the forming one
        self.fail_init_times = fail_init_times
        self.initialized = False
        self.extra_bars = 0  # simulate time passing: new closed candles

    # --- connection
    def initialize(self, **kwargs):
        if self.fail_init_times > 0:
            self.fail_init_times -= 1
            return False
        self.initialized = True
        return True

    def shutdown(self):
        self.initialized = False

    def last_error(self):
        return (-1, "fake error")

    def terminal_info(self):
        return SimpleNamespace(connected=self.initialized)

    def account_info(self):
        if not self.initialized:
            return None
        return SimpleNamespace(login=1, server="Fake-Server", currency="USD", balance=5000.0,
                               equity=5000.0, margin_free=5000.0, leverage=500, trade_allowed=True)

    # --- symbols
    def symbol_info(self, name):
        return SimpleNamespace(visible=True, name=name) if name in self.symbols else None

    def symbol_select(self, name, enable):
        return name in self.symbols

    def symbol_info_tick(self, name):
        return SimpleNamespace(bid=2000.0, ask=2000.2, time=0)

    # --- rates: bar i has open time = i * tf seconds; position 0 = newest (forming)
    def copy_rates_from_pos(self, symbol, tf_minutes, start_pos, count):
        if symbol not in self.symbols:
            return None
        n = self.total_bars + self.extra_bars
        newest = n - 1
        first = newest - start_pos - count + 1
        idx = np.arange(max(first, 0), newest - start_pos + 1)
        out = np.zeros(len(idx), dtype=RATE_DTYPE)
        out["time"] = idx * tf_minutes * 60 + 1_700_000_000
        base = 2000.0 + idx * 0.1
        out["open"], out["high"], out["low"], out["close"] = base, base + 1, base - 1, base + 0.5
        out["tick_volume"] = 100
        return out
