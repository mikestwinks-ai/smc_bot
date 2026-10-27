"""Position sizing from the broker's own symbol specification.

    loss_per_lot = (stop_distance / tick_size) * tick_value_loss     [account currency]
    volume       = risk_money / loss_per_lot, rounded DOWN onto the volume grid

The grid is ``volume_min + k * volume_step``. Volume is never rounded up: if even
``volume_min`` would exceed the risk budget the trade is rejected, not enlarged.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal
from typing import Optional

from smc.risk.models import PositionSizing, SymbolSpec


def floor_to_grid(value: float, volume_min: float, step: float) -> float:
    """Largest ``volume_min + k*step`` that is <= ``value`` (``value`` must be >= volume_min)."""
    d_val, d_min, d_step = Decimal(str(value)), Decimal(str(volume_min)), Decimal(str(step))
    k = ((d_val - d_min) / d_step).to_integral_value(rounding=ROUND_FLOOR)
    return float(d_min + k * d_step)


def loss_per_lot(sl_distance: float, spec: SymbolSpec) -> float:
    return (sl_distance / spec.tick_size) * spec.tick_value_loss


def compute_volume(equity: float, risk_pct: float, sl_distance: float,
                   spec: SymbolSpec) -> tuple[Optional[PositionSizing], Optional[str]]:
    """Return ``(sizing, None)`` or ``(None, rejection_reason)``."""
    problems = spec.problems()
    if problems:
        return None, "; ".join(problems)
    if equity <= 0:
        return None, f"equity {equity} is not positive"
    if sl_distance <= 0:
        return None, f"stop distance {sl_distance} is not positive"

    risk_money = equity * risk_pct / 100.0
    per_lot = loss_per_lot(sl_distance, spec)
    raw = risk_money / per_lot
    if raw < spec.volume_min:
        min_risk_pct = spec.volume_min * per_lot / equity * 100.0
        return None, (f"minimum lot {spec.volume_min} would risk {min_risk_pct:.2f}% of equity "
                      f"(limit {risk_pct:.2f}%) - trade rejected, not enlarged")
    volume = floor_to_grid(min(raw, spec.volume_max), spec.volume_min, spec.volume_step)
    actual = volume * per_lot
    if actual > risk_money * (1 + 1e-9):  # defensive: can only happen through a bug
        return None, f"internal sizing error: risk {actual:.2f} > budget {risk_money:.2f}"
    return PositionSizing(volume, risk_pct, risk_money, actual, sl_distance, per_lot), None
