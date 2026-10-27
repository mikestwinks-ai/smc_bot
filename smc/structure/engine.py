"""StructureHub - wires every Phase 2 component together.

Per closed candle (strict order, so nothing is judged by its own candle):
  1. tracker: swings, BOS / CHoCH / MSS, bias             (layers.py / tracker.py)
  2. existing zones: body-close break -> flip, else mitigation (flipped_zones / zones)
  3. new FVG from the last 3 candles                       (fvg.py)
  4. new Order Block for any structure event               (order_blocks.py)
  5. liquidity: sweeps, equal highs/lows, day/session H/L  (liquidity.py)

Feed it with :meth:`update` (a candle DataFrame) or :meth:`on_candle_events`
(the ``CandleEvent`` list from ``MarketDataManager.update_all``). The first call
for a symbol/timeframe replays history ("warm-up"); its signals are flagged
``is_historical`` so Phase 3 can ignore them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from smc.config import BotConfig
from smc.core.data_manager import CandleEvent, MarketDataManager
from smc.core.timeframes import StructureLayer, Timeframe
from smc.structure.flipped_zones import FlippedZoneEngine
from smc.structure.fvg import FVGDetector
from smc.structure.layers import ExternalStructureEngine, InternalStructureEngine, StructureLayerEngine
from smc.structure.liquidity import LiquidityTracker
from smc.structure.models import (
    Bias, DualPDReading, FairValueGap, LiquidityLevel, OrderBlock, StructureUpdate, SwingPoint,
)
from smc.structure.order_blocks import OrderBlockDetector
from smc.structure.premium_discount import DualPremiumDiscount
from smc.structure.zones import ZoneBook, apply_mitigation
from smc.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class StructureSnapshot:
    """Read-only picture of one symbol's structure, for Phase 3 consumers."""

    symbol: str
    price: Optional[float]
    external_bias: Bias
    internal_bias: Bias
    external_bias_by_tf: dict[Timeframe, Bias]
    internal_bias_by_tf: dict[Timeframe, Bias]
    external_swings: dict[str, Optional[SwingPoint]]
    internal_swings: dict[str, Optional[SwingPoint]]
    premium_discount: DualPDReading
    order_blocks: list[OrderBlock] = field(default_factory=list)  # live (active + mitigated)
    fvgs: list[FairValueGap] = field(default_factory=list)  # live
    liquidity: list[LiquidityLevel] = field(default_factory=list)  # active


class StructureHub:
    def __init__(self, config: BotConfig) -> None:
        self._cfg = config
        st = config.structure
        max_bars = config.data.max_bars_in_memory
        self.external = ExternalStructureEngine(config.timeframes.external, st, max_bars)
        self.internal = InternalStructureEngine(config.timeframes.internal, st, max_bars)
        self.zones = ZoneBook(st.max_zones_per_timeframe)
        self._fvg, self._ob = FVGDetector(st), OrderBlockDetector(st)
        self._flips = FlippedZoneEngine(st)
        self._pd = DualPremiumDiscount(st)
        self._liquidity: dict[tuple[str, Timeframe], LiquidityTracker] = {}
        self._last_time: dict[tuple[str, Timeframe], pd.Timestamp] = {}

    # ----------------------------------------------------------------- plumbing
    def _engine_for(self, tf: Timeframe) -> StructureLayerEngine:
        if self.external.handles(tf):
            return self.external
        if self.internal.handles(tf):
            return self.internal
        raise ValueError(f"Timeframe {tf} is not part of any structure layer")

    def engine_for(self, tf: Timeframe) -> StructureLayerEngine:
        return self._engine_for(tf)

    def get_tracker(self, symbol: str, tf: Timeframe):  # -> Optional[StructureTracker]
        return self._engine_for(tf).get_tracker(symbol, tf)

    def active_liquidity(self, symbol: str) -> list[LiquidityLevel]:
        return [lv for (sym, _), t in self._liquidity.items() if sym == symbol for lv in t.active_levels()]

    def liquidity(self, symbol: str, tf: Timeframe) -> LiquidityTracker:
        key = (symbol, tf)
        if key not in self._liquidity:
            self._liquidity[key] = LiquidityTracker(
                symbol, tf, self._cfg.structure, self._cfg.sessions,
                track_sessions=(tf is self._cfg.structure.session_levels_timeframe))
        return self._liquidity[key]

    # ------------------------------------------------------------------- inputs
    def initialize(self, data: MarketDataManager) -> list[StructureUpdate]:
        """Replay all stored history, highest timeframe first."""
        updates = []
        for tf in self._cfg.timeframes.all:
            for symbol in data.symbols:
                updates.append(self.update(symbol, tf, data.get_candles(symbol, tf)))
        return updates

    def on_candle_events(self, events: list[CandleEvent], data: MarketDataManager) -> list[StructureUpdate]:
        """Process the new candles announced by ``MarketDataManager.update_all()``."""
        out = []
        for ev in events:
            upd = self.update(ev.symbol, ev.timeframe, data.get_candles(ev.symbol, ev.timeframe, ev.new_candles + 3))
            if upd.has_changes:
                out.append(upd)
        return out

    def update(self, symbol: str, tf: Timeframe, candles: pd.DataFrame) -> StructureUpdate:
        """Process every candle in ``candles`` newer than the last one seen."""
        key = (symbol, tf)
        engine = self._engine_for(tf)
        last = self._last_time.get(key)
        warmup = last is None
        new = candles if last is None else candles[candles.index > last]
        upd = StructureUpdate(symbol, tf, engine.layer, is_warmup=warmup)
        if new.empty:
            return upd

        for ts, o, h, l, c in zip(new.index, new["open"].to_numpy(), new["high"].to_numpy(),
                                  new["low"].to_numpy(), new["close"].to_numpy()):
            self._process_candle(engine, symbol, tf, ts, float(o), float(h), float(l), float(c), upd, warmup)
            self._last_time[key] = ts
            upd.candles_processed += 1

        if not warmup and upd.has_changes:
            log.info("%s %s: %d event(s), %d new OB, %d new FVG, %d flip(s), %d sweep(s)", symbol, tf,
                     len(upd.events), len(upd.new_order_blocks), len(upd.new_fvgs),
                     len(upd.flip_signals), len(upd.liquidity_sweeps))
        elif warmup:
            tr = engine.tracker(symbol, tf)
            log.info("%s %s warm-up: %d candles, bias=%s, %d live zones", symbol, tf,
                     upd.candles_processed, tr.bias.value, len(self.zones.live(symbol, tf)))
        return upd

    # ------------------------------------------------------ per-candle pipeline
    def _process_candle(self, engine: StructureLayerEngine, symbol: str, tf: Timeframe, ts: pd.Timestamp,
                        o: float, h: float, l: float, c: float, upd: StructureUpdate, hist: bool) -> None:
        tracker = engine.tracker(symbol, tf)
        # 1) structure
        tu = engine.process_candle(symbol, tf, ts, o, h, l, c)
        i = tracker.buffer.last_index
        upd.swings.extend(tu.new_swings)
        upd.events.extend(tu.events)

        # 2) existing zones: flip on body close, otherwise mitigation
        for zone in self.zones.zones(symbol, tf):
            if not zone.is_live or zone.created_index >= i:
                continue
            flipped = self._flips.check(zone, ts, i, c, is_historical=hist)
            if flipped is not None:
                signal, new_zone = flipped
                upd.flip_signals.append(signal)
                if new_zone is not None:
                    self.zones.add(new_zone)
                    upd.new_flipped_zones.append(new_zone)
                continue
            if apply_mitigation(zone, ts, h, l, c):
                upd.mitigated_zones.append(zone)

        # 3) fair value gap
        fvg = self._fvg.detect(tracker)
        if fvg is not None:
            self.zones.add(fvg)
            upd.new_fvgs.append(fvg)

        # 4) order blocks for this candle's structure events
        for ev in tu.events:
            ob = self._ob.detect(ev, tracker, self.zones.fvgs(symbol, tf))
            if ob is not None:
                self.zones.add(ob)
                upd.new_order_blocks.append(ob)

        # 5) liquidity
        lu = self.liquidity(symbol, tf).on_candle(tracker, tu.new_swings, ts, h, l, c, is_historical=hist)
        upd.new_liquidity_levels.extend(lu.new_levels)
        upd.liquidity_sweeps.extend(lu.sweeps)

    # ------------------------------------------------------------------ queries
    def last_price(self, symbol: str) -> Optional[float]:
        """Last close of the lowest timeframe that has data for ``symbol``."""
        for tf in sorted(self._cfg.timeframes.all, key=lambda t: t.minutes):
            if (symbol, tf) in self._last_time:
                return self._engine_for(tf).tracker(symbol, tf).last_close
        return None

    def premium_discount(self, symbol: str, price: Optional[float] = None) -> DualPDReading:
        price = price if price is not None else self.last_price(symbol)
        st = self._cfg.structure
        if price is None:
            return DualPDReading(None, None)
        ext = self.external.tracker(symbol, st.external_range_timeframe) \
            if (symbol, st.external_range_timeframe) in self._last_time else None
        internal = self.internal.tracker(symbol, st.internal_range_timeframe) \
            if (symbol, st.internal_range_timeframe) in self._last_time else None
        return self._pd.calculate(ext, internal, price)

    def snapshot(self, symbol: str, price: Optional[float] = None) -> StructureSnapshot:
        price = price if price is not None else self.last_price(symbol)
        live = self.zones.live(symbol)
        levels = [lv for (sym, _), t in self._liquidity.items() if sym == symbol for lv in t.active_levels()]
        return StructureSnapshot(
            symbol=symbol, price=price,
            external_bias=self.external.bias(symbol), internal_bias=self.internal.bias(symbol),
            external_bias_by_tf=self.external.bias_by_timeframe(symbol),
            internal_bias_by_tf=self.internal.bias_by_timeframe(symbol),
            external_swings=self.external.active_swings(symbol),
            internal_swings=self.internal.active_swings(symbol),
            premium_discount=self.premium_discount(symbol, price),
            order_blocks=[z for z in live if isinstance(z, OrderBlock)],
            fvgs=[z for z in live if isinstance(z, FairValueGap)],
            liquidity=levels,
        )
