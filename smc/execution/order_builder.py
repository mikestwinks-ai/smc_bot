"""Builds MT5 request dictionaries and checks broker stop/freeze constraints."""

from __future__ import annotations

import datetime as dt
import math
from typing import Any, Optional

from smc.execution import mt5_gateway as m
from smc.risk.models import OrderKind, PositionInfo, SymbolSpec, TickState


def round_to_tick(price: float, spec: SymbolSpec, mode: str = "nearest") -> float:
    """Snap a price onto the symbol's tick grid. mode: nearest | up | down."""
    q = price / spec.tick_size
    q = {"up": math.ceil(q - 1e-9), "down": math.floor(q + 1e-9)}.get(mode, round(q))
    return round(q * spec.tick_size, spec.digits)


def market_filling(spec: SymbolSpec) -> int:
    """Per-symbol filling policy: IOC if supported, else FOK, else RETURN."""
    if spec.filling_flags & 2:
        return m.FILL_IOC
    if spec.filling_flags & 1:
        return m.FILL_FOK
    return m.FILL_RETURN


def order_type(kind: OrderKind, is_buy: bool) -> int:
    if kind is OrderKind.MARKET:
        return m.TYPE_BUY if is_buy else m.TYPE_SELL
    if kind is OrderKind.LIMIT:
        return m.TYPE_BUY_LIMIT if is_buy else m.TYPE_SELL_LIMIT
    return m.TYPE_BUY_STOP if is_buy else m.TYPE_SELL_STOP


def build_entry(kind: OrderKind, is_buy: bool, symbol: str, volume: float, price: float, sl: float, tp: float,
                spec: SymbolSpec, magic: int, deviation: int, comment: str,
                expiration: Optional[dt.datetime] = None) -> dict[str, Any]:
    req: dict[str, Any] = {
        "symbol": symbol, "volume": volume, "type": order_type(kind, is_buy), "price": price,
        "sl": sl, "tp": tp, "magic": magic, "comment": comment[:31]}
    if kind is OrderKind.MARKET:
        req.update(action=m.ACTION_DEAL, deviation=deviation, type_time=m.TIME_GTC, type_filling=market_filling(spec))
    else:
        req.update(action=m.ACTION_PENDING, type_filling=m.FILL_RETURN)
        if expiration is not None and spec.expiration_flags & 4:
            req.update(type_time=m.TIME_SPECIFIED, expiration=expiration)
        else:
            req.update(type_time=m.TIME_GTC)
    return req


def build_close(pos: PositionInfo, volume: float, tick: TickState, spec: SymbolSpec, deviation: int,
                comment: str) -> dict[str, Any]:
    """Close (all or part of) a position with an opposite market deal."""
    return {
        "action": m.ACTION_DEAL, "symbol": pos.symbol, "volume": volume, "position": pos.ticket,
        "type": m.TYPE_SELL if pos.is_buy else m.TYPE_BUY, "price": tick.bid if pos.is_buy else tick.ask,
        "deviation": deviation, "magic": pos.magic, "comment": comment[:31], "type_time": m.TIME_GTC,
        "type_filling": market_filling(spec)}


def build_sltp(pos: PositionInfo, sl: float, tp: float) -> dict[str, Any]:
    return {"action": m.ACTION_SLTP, "position": pos.ticket, "symbol": pos.symbol, "sl": sl, "tp": tp,
            "magic": pos.magic}


def build_remove(ticket: int) -> dict[str, Any]:
    return {"action": m.ACTION_REMOVE, "order": ticket}


# ----------------------------------------------------------- broker constraints
def stop_level_problems(kind: OrderKind, is_buy: bool, entry: float, sl: float, tp: float,
                        tick: TickState, spec: SymbolSpec) -> list[str]:
    """Minimum-distance rules (SYMBOL_TRADE_STOPS_LEVEL)."""
    lvl, out = spec.stops_level, []
    ref = tick.ask if is_buy else tick.bid
    if kind is OrderKind.LIMIT and abs(ref - entry) < lvl:
        out.append(f"limit price {entry} is {abs(ref - entry):.5f} from market, broker minimum {lvl:.5f}")
    if kind is OrderKind.STOP and abs(entry - ref) < lvl:
        out.append(f"stop-entry price {entry} is {abs(entry - ref):.5f} from market, broker minimum {lvl:.5f}")
    basis = tick.bid if (kind is OrderKind.MARKET and is_buy) else tick.ask if kind is OrderKind.MARKET else entry
    if abs(basis - sl) < lvl:
        out.append(f"stop-loss {sl} is closer than the broker minimum {lvl:.5f}")
    if tp and abs(tp - basis) < lvl:
        out.append(f"take-profit {tp} is closer than the broker minimum {lvl:.5f}")
    return out


def modify_problems(pos: PositionInfo, new_sl: float, tick: TickState, spec: SymbolSpec) -> list[str]:
    """Rules for moving an existing stop: stops level and freeze level."""
    out = []
    market = tick.bid if pos.is_buy else tick.ask  # the price a stop-loss is compared with
    if abs(market - new_sl) < spec.stops_level:
        out.append(f"new SL {new_sl} is within the broker stops level {spec.stops_level:.5f} of price {market}")
    if spec.freeze_level > 0:
        if pos.sl and abs(market - pos.sl) <= spec.freeze_level:
            out.append("current SL is inside the freeze level - cannot be modified now")
        if pos.tp and abs(market - pos.tp) <= spec.freeze_level:
            out.append("take-profit is inside the freeze level - cannot be modified now")
    return out
