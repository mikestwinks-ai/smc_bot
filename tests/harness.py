"""Shared builders for Phase 4 tests: fake broker + real structure/plan/execution stack."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

from smc.config import AssetClass, BotConfig, SymbolConfig
from smc.core.mt5_connection import MT5Connection
from smc.core.timeframes import Timeframe
from smc.execution import ExecutionSystem
from smc.models.plan import (
    EntryModel, Invalidation, Plan, PlanDirection, PlanStatus, PlanZones, PriceRange, SourceTimeframes,
)
from smc.planning import PlanManager
from smc.structure import Bias, StructureHub
from tests.fake_broker import FakeBroker
from tests.synth import frame, zigzag_rows

SYM, BSYM, M15 = "XAUUSD", "XAUUSDm", Timeframe.M15


@dataclass
class Env:
    cfg: BotConfig
    broker: FakeBroker
    conn: MT5Connection
    hub: StructureHub
    mgr: PlanManager
    system: ExecutionSystem
    tmp: Path

    @property
    def now(self) -> pd.Timestamp:
        return self.broker.now

    def restart(self) -> ExecutionSystem:
        """A fresh process: new objects, same broker + state file (plans stay in memory)."""
        self.system = ExecutionSystem(self.cfg, self.conn, self.mgr, now_fn=lambda: self.broker.now,
                                      state_path=self.cfg.execution.state_path)
        return self.system


def make_env(tmp_path: Path, live: bool = False, now: str = "2026-01-07 10:00", balance: float = 5000.0,
             real: bool = False, cutoff_tz: str = "UTC", commission_per_lot: float = 0.0) -> Env:
    cfg = BotConfig(symbols=[SymbolConfig(SYM, AssetClass.METAL, "m")])
    cfg.execution.live_enabled = live
    cfg.execution.state_path = tmp_path / "state.json"
    cfg.execution.kill_switch_path = tmp_path / "KILL_SWITCH"
    cfg.journal.database_path = Path(":memory:")
    cfg.sessions.cutoff_timezone = cutoff_tz
    cfg.validate()
    broker = FakeBroker(now=now, balance=balance, real=real, commission_per_lot=commission_per_lot)
    conn = MT5Connection(cfg.broker, broker)
    conn.connect()
    hub = StructureHub(cfg)
    mgr = PlanManager(cfg, hub)
    rows = zigzag_rows([100, 150, 105, 118], steps=8)  # last close 118, swing high 150.2 above
    start = broker.now - pd.Timedelta(minutes=15 * len(rows))
    hub.update(SYM, M15, frame(rows, M15, start=start))  # warm-up; last closed candle opened now-15min
    system = ExecutionSystem(cfg, conn, mgr, now_fn=lambda: broker.now, state_path=cfg.execution.state_path)
    return Env(cfg, broker, conn, hub, mgr, system, tmp_path)


def add_plan(env: Env, direction: PlanDirection = PlanDirection.LONG, model: EntryModel = EntryModel.OB,
             zone: Optional[tuple[float, float]] = None, sl: Optional[float] = None,
             targets: Optional[list[float]] = None, status: PlanStatus = PlanStatus.ACTIVE,
             zones: Optional[PlanZones] = None, age_min: float = 30) -> Plan:
    long = direction is PlanDirection.LONG
    top, bottom = zone or ((116.0, 114.0) if long else (142.0, 140.0))
    sl = sl if sl is not None else (112.0 if long else 144.0)
    targets = targets if targets is not None else ([150.2] if long else [104.8])
    plan = Plan(
        plan_id=env.mgr.store.next_id(SYM), symbol=SYM, direction=direction, entry_model=model,
        source_timeframes=SourceTimeframes(env.cfg.timeframes.external, env.cfg.timeframes.internal, M15),
        external_bias=Bias.NEUTRAL, internal_bias=Bias.NEUTRAL, zones=zones or PlanZones(),
        entry_zone=PriceRange(top, bottom), invalidation=Invalidation(sl, "test"), targets=targets,
        rr_estimate=None, reason="test plan", created_at=env.now - pd.Timedelta(minutes=age_min),
        updated_at=env.now, status=status)
    return env.mgr.store.add(plan)
