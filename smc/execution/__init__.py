"""Phase 4 - order execution and trade management (live orders OFF by default)."""

from smc.execution.execution_engine import ExecResult, ExecutionEngine, Outcome
from smc.execution.kill_switch import KillSwitch
from smc.execution.mt5_gateway import LiveTradingDisabledError, MT5Gateway, SendStatus
from smc.execution.state_store import StateStore
from smc.execution.system import CycleReport, ExecutionSystem
from smc.execution.trade_manager import Action, TradeManager

__all__ = ["Action", "CycleReport", "ExecResult", "ExecutionEngine", "ExecutionSystem", "KillSwitch",
           "LiveTradingDisabledError", "MT5Gateway", "Outcome", "SendStatus", "StateStore", "TradeManager"]
