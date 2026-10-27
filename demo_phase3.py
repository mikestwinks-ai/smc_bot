"""Phase 3 demo - synthetic multi-timeframe feed through structure engine + plan manager.

No MetaTrader needed:  python demo_phase3.py [--bars 6000] [--seed 7] [--verbose]

An M5 random walk with trending regimes and impulse jumps is resampled to M15,
M30 and H4. The first part is replayed as history (warm-up); the rest streams
candle by candle, highest timeframe first, exactly as the live loop would feed it.
"""

from __future__ import annotations

import argparse
import logging
from collections import Counter

import numpy as np
import pandas as pd

from smc.config import AssetClass, BotConfig, SymbolConfig
from smc.core.timeframes import Timeframe
from smc.models.plan import PlanStatus
from smc.planning import ChangeKind, PlanManager
from smc.structure import StructureHub
from smc.utils.logger import setup_logging

SYMBOL = "XAUUSD"


def make_m5(bars: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = np.empty(bars)
    price, drift, left = 2000.0, 0.0, 0
    for i in range(bars):
        if left == 0:
            left, drift = int(rng.integers(40, 160)), float(rng.normal(0, 0.35))
        left -= 1
        step = drift + rng.normal(0, 0.9)
        if rng.random() < 0.02:  # displacement candle
            step += rng.choice([-1, 1]) * rng.uniform(3, 6)
        price += step
        close[i] = price
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + rng.uniform(0.05, 0.8, bars)
    low = np.minimum(open_, close) - rng.uniform(0.05, 0.8, bars)
    idx = pd.date_range("2026-03-02 00:00", periods=bars, freq="5min", tz="UTC", name="time")
    df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx)
    df["tick_volume"], df["spread"], df["real_volume"] = 100.0, 0.0, 0.0
    return df


def resample(m5: pd.DataFrame, tf: Timeframe) -> pd.DataFrame:
    if tf is Timeframe.M5:
        return m5
    agg = {"open": "first", "high": "max", "low": "min", "close": "last",
           "tick_volume": "sum", "spread": "last", "real_volume": "sum"}
    out = m5.resample(f"{tf.minutes}min", label="left", closed="left").agg(agg)
    out = out[m5["close"].resample(f"{tf.minutes}min", label="left", closed="left").count() == tf.minutes // 5]
    out.index.name = "time"
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, default=6000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--warmup", type=int, default=2500, help="M5 bars replayed as history")
    ap.add_argument("--verbose", action="store_true", help="print every create / cancel / update / status change")
    args = ap.parse_args()

    cfg = BotConfig(symbols=[SymbolConfig(SYMBOL, AssetClass.METAL)])
    cfg.validate()
    setup_logging(cfg.logging.log_dir, "demo_phase3.log", "WARNING", console=False)  # keep stdout clean

    m5 = make_m5(args.bars, args.seed)
    frames = {tf: resample(m5, tf) for tf in cfg.timeframes.all}  # highest first
    hub = StructureHub(cfg)
    mgr = PlanManager(cfg, hub)

    t_start = m5.index[args.warmup - 1] + Timeframe.M5.delta  # close time of last history bar
    cursor = {}
    for tf, df in frames.items():  # history replay (warm-up)
        done = df[df.index + tf.delta <= t_start]
        hub.update(SYMBOL, tf, done)
        cursor[tf] = len(done)
    mgr.bootstrap([SYMBOL])
    print(f"History replayed: {args.warmup} M5 bars. Plans after bootstrap: {len(mgr.store.all())}\n")

    counts: Counter[str] = Counter()
    for t in m5.index[args.warmup:]:
        close_time = t + Timeframe.M5.delta
        for tf in cfg.timeframes.all:  # H4 -> M30 -> M15 -> M5: external before internal
            df = frames[tf]
            while cursor[tf] < len(df) and df.index[cursor[tf]] + tf.delta <= close_time:
                cursor[tf] += 1
                for ch in mgr.run_candles(SYMBOL, tf, df.iloc[max(0, cursor[tf] - 6):cursor[tf]]):
                    counts[ch.kind.value] += 1
                    show = ch.kind in (ChangeKind.FLIPPED, ChangeKind.UPGRADED)
                    if show or args.verbose:
                        print(f"{ch.time:%m-%d %H:%M} {ch.kind.value.upper():8s} {ch.plan_id}: {ch.note}")

    print("\n================ SUMMARY ================")
    print("changes:", dict(counts))
    by_status = Counter(p.status.value for p in mgr.store.all())
    by_model = Counter(p.entry_model.value for p in mgr.store.all())
    print("plans by status:", dict(by_status))
    print("plans by model :", dict(by_model))
    ct = [p for p in mgr.store.all() if p.quality.counter_trend_internal]
    print(f"counter-trend internal plans (internal vs external bias): {len(ct)}")
    live = mgr.store.list_active()
    print(f"\nLive plans now: {len(live)} (showing up to 6)")
    for p in live[-6:]:
        print("  ", p.summary())
    print("\nOne example of each entry model:")
    for model in ("OB", "FVG", "SWEEP_MSS", "OB_FVG"):
        ex = next((p for p in mgr.store.all() if p.entry_model.value == model), None)
        print(f"  [{model}] " + (f"{ex.plan_id}: {ex.reason}" if ex else "none this run"))
    n_flips = len(mgr.store.by_status(PlanStatus.FLIPPED))
    n_children = sum(1 for p in mgr.store.all() if p.parent_plan_id)
    print(f"flipped plans: {n_flips}, of which produced an opposite retest plan: {n_children}")
    flips = [p for p in mgr.store.by_status(PlanStatus.FLIPPED) if p.child_plan_ids] or mgr.store.by_status(PlanStatus.FLIPPED)
    if flips:
        p = flips[-1]
        print(f"\nLast flip: {p.plan_id} -> children {p.child_plan_ids}\n   {p.flip_reason}")
    print(f"\nDiscards recorded: {len(mgr.discards)}  (latest 5)")
    for d in list(mgr.discards)[-5:]:
        print(f"   {d.time:%m-%d %H:%M} {d.timeframe} {d.model.value}: {d.reason}")


if __name__ == "__main__":
    main()
