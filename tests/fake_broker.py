"""In-memory MT5 stand-in with positions, orders, deals and scriptable order_send results.

Nothing here can reach a real broker. ``behaviors`` scripts the next order_send answers:
  done | partial | reject | requote | timeout_executed | timeout_not_executed | code_executed
"""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace
from typing import Optional

import pandas as pd

TYPE_BUY, TYPE_SELL, TYPE_BUY_LIMIT, TYPE_SELL_LIMIT, TYPE_BUY_STOP, TYPE_SELL_STOP = 0, 1, 2, 3, 4, 5
A_DEAL, A_PENDING, A_SLTP, A_REMOVE = 1, 5, 6, 8


def epoch(ts: pd.Timestamp) -> int:
    return int(ts.timestamp())


class FakeBroker:
    def __init__(self, now: str = "2026-01-07 10:00", balance: float = 5000.0, symbol: str = "XAUUSDm",
                 currency: str = "USD", commission_per_lot: float = 0.0, real: bool = False) -> None:
        self.now = pd.Timestamp(now, tz="UTC")
        self.balance, self.currency, self.real = balance, currency, real
        self.commission_per_lot = commission_per_lot
        self.equity_override: Optional[float] = None
        self.initialized = False
        self.symbols: dict[str, SimpleNamespace] = {symbol: self.make_spec(symbol)}
        self.quotes: dict[str, tuple[float, float]] = {symbol: (118.00, 118.05)}
        self.positions: list[SimpleNamespace] = []
        self.orders: list[SimpleNamespace] = []
        self.deals: list[SimpleNamespace] = []
        self.sent: list[dict] = []
        self.checked: list[dict] = []
        self.behaviors: deque[str] = deque()
        self.check_retcode = 0
        self.fail_positions = False
        self.tick_age = 0  # seconds the quoted tick is behind 'now'
        self._ticket = 1000

    # ------------------------------------------------------------------ setup
    @staticmethod
    def make_spec(name: str, **over) -> SimpleNamespace:
        d = dict(name=name, visible=True, digits=2, point=0.01, trade_tick_size=0.01, trade_tick_value=1.0,
                 trade_tick_value_profit=1.0, trade_tick_value_loss=1.0, trade_contract_size=100.0,
                 volume_min=0.01, volume_max=100.0, volume_step=0.01, trade_stops_level=10, trade_freeze_level=0,
                 trade_mode=4, filling_mode=3, expiration_mode=15)
        d.update(over)
        return SimpleNamespace(**d)

    def add_symbol(self, name: str, bid: float, ask: float, **over) -> None:
        self.symbols[name] = self.make_spec(name, **over)
        self.quotes[name] = (bid, ask)

    def set_price(self, bid: float, ask: Optional[float] = None, symbol: Optional[str] = None) -> None:
        sym = symbol or next(iter(self.symbols))
        self.quotes[sym] = (bid, ask if ask is not None else round(bid + 0.05, 2))
        for p in self.positions:
            if p.symbol == sym:
                p.price_current = bid if p.type == TYPE_BUY else self.quotes[sym][1]
                p.profit = self._pnl(p, p.price_current, p.volume)

    def advance(self, seconds: float) -> None:
        self.now += pd.Timedelta(seconds=seconds)

    def _tick_value_per_price(self, symbol: str) -> float:
        s = self.symbols[symbol]
        return s.trade_tick_value_loss / s.trade_tick_size

    def _pnl(self, p, price: float, volume: float) -> float:
        diff = (price - p.price_open) if p.type == TYPE_BUY else (p.price_open - price)
        return diff * volume * self._tick_value_per_price(p.symbol)

    def _next(self) -> int:
        self._ticket += 1
        return self._ticket

    # ---------------------------------------------------- connection surface
    def initialize(self, **kw): self.initialized = True; return True
    def shutdown(self): self.initialized = False
    def last_error(self): return (1, "Success")
    def terminal_info(self): return SimpleNamespace(connected=self.initialized)
    def symbol_select(self, name, enable): return name in self.symbols
    def symbol_info(self, name): return self.symbols.get(name)

    def account_info(self):
        if not self.initialized:
            return None
        floating = sum(p.profit for p in self.positions)
        equity = self.equity_override if self.equity_override is not None else self.balance + floating
        return SimpleNamespace(login=1, server="Fake", currency=self.currency, balance=self.balance, equity=equity,
                               margin_free=equity, leverage=500, trade_allowed=True, trade_mode=2 if self.real else 0)

    def symbol_info_tick(self, name):
        if name not in self.quotes:
            return None
        bid, ask = self.quotes[name]
        return SimpleNamespace(bid=bid, ask=ask, time=epoch(self.now) - self.tick_age)

    # ------------------------------------------------------------- read APIs
    def positions_get(self, **kw):
        return None if self.fail_positions else tuple(self.positions)

    def orders_get(self, **kw):
        return tuple(self.orders)

    def history_deals_get(self, start, end, **kw):
        def ep(x):
            t = pd.Timestamp(x)
            return epoch(t.tz_localize("UTC") if t.tzinfo is None else t)
        s, e = ep(start), ep(end)
        return tuple(d for d in self.deals if s <= d.time <= e)

    def history_orders_get(self, start, end, **kw):
        return ()

    # ------------------------------------------------------------- trade APIs
    def order_check(self, request):
        self.checked.append(dict(request))
        return SimpleNamespace(retcode=self.check_retcode, comment="fake check", margin_free=1e9)

    def order_send(self, request):
        self.sent.append(dict(request))
        mode = self.behaviors.popleft() if self.behaviors else "done"
        if mode == "reject":
            return self._res(10006, comment="rejected")
        if mode == "requote":
            return self._res(10004, comment="requote")
        if mode == "timeout_not_executed":
            return None
        res = self._execute(request, partial=(mode == "partial"))
        if mode == "timeout_executed":
            return None
        if mode == "code_executed":
            return self._res(10012, comment="timeout")
        return res

    def _res(self, code, order=0, deal=0, volume=0.0, price=0.0, comment=""):
        return SimpleNamespace(retcode=code, order=order, deal=deal, volume=volume, price=price, comment=comment)

    # --------------------------------------------------------------- execution
    def _execute(self, r: dict, partial: bool = False):
        action = r["action"]
        if action == A_REMOVE:
            self.orders = [o for o in self.orders if o.ticket != r["order"]]
            return self._res(10009)
        if action == A_SLTP:
            for p in self.positions:
                if p.ticket == r["position"]:
                    p.sl, p.tp = r["sl"], r["tp"]
            return self._res(10009)
        if action == A_PENDING:
            t = self._next()
            self.orders.append(SimpleNamespace(
                ticket=t, time_setup=epoch(self.now), type=r["type"], magic=r["magic"], volume_current=r["volume"],
                price_open=r["price"], sl=r["sl"], tp=r["tp"], symbol=r["symbol"], comment=r["comment"],
                time_expiration=(epoch(pd.Timestamp(r["expiration"]).tz_localize("UTC") if pd.Timestamp(r["expiration"]).tzinfo is None
                                       else pd.Timestamp(r["expiration"])) if r.get("expiration") else 0)))
            return self._res(10008, order=t)
        if "position" in r:  # closing deal
            return self._close(r, partial)
        vol = r["volume"] / 2 if partial else r["volume"]
        pos = self._open(r["symbol"], r["type"] == TYPE_BUY, vol, r["price"], r["sl"], r["tp"], r["magic"], r["comment"])
        return self._res(10010 if partial else 10009, order=pos.ticket, deal=self._ticket, volume=vol, price=r["price"])

    def _open(self, symbol, is_buy, volume, price, sl, tp, magic, comment, when: Optional[pd.Timestamp] = None):
        t = self._next()
        when = when or self.now
        pos = SimpleNamespace(ticket=t, time=epoch(when), type=TYPE_BUY if is_buy else TYPE_SELL, magic=magic,
                              volume=volume, price_open=price, sl=sl, tp=tp, price_current=price, swap=0.0, profit=0.0,
                              symbol=symbol, comment=comment, identifier=t)
        self.positions.append(pos)
        self._deal(pos, "in", volume, price, 0.0, when)
        return pos

    def _deal(self, pos, kind, volume, price, profit, when):
        comm = -self.commission_per_lot * volume
        self.deals.append(SimpleNamespace(
            ticket=self._next(), order=self._ticket, time=epoch(when), type=pos.type if kind == "in" else 1 - pos.type,
            entry=0 if kind == "in" else 1, magic=pos.magic, position_id=pos.ticket, volume=volume, price=price,
            commission=comm, swap=0.0, profit=profit, fee=0.0, symbol=pos.symbol, comment=pos.comment))
        self.balance += profit + comm

    def _close(self, r, partial):
        pos = next((p for p in self.positions if p.ticket == r["position"]), None)
        if pos is None:
            return self._res(10013, comment="no such position")
        vol = min(r["volume"], pos.volume)
        profit = self._pnl(pos, r["price"], vol)
        self._deal(pos, "out", vol, r["price"], profit, self.now)
        pos.volume = round(pos.volume - vol, 8)
        if pos.volume <= 1e-9:
            self.positions.remove(pos)
        return self._res(10009, order=self._next(), deal=self._ticket, volume=vol, price=r["price"])

    # ------------------------------------------------------------ test helpers
    def fill_pending(self, order_ticket: int) -> SimpleNamespace:
        o = next(x for x in self.orders if x.ticket == order_ticket)
        self.orders.remove(o)
        return self._open(o.symbol, o.type in (TYPE_BUY_LIMIT, TYPE_BUY_STOP), o.volume_current, o.price_open,
                          o.sl, o.tp, o.magic, o.comment)

    def add_closed_trade(self, net: float, minutes_ago: float, magic: int, symbol: Optional[str] = None,
                         volume: float = 0.10, partial_split: bool = False) -> int:
        """A fully closed bot trade with total ``net`` P&L, closed ``minutes_ago`` before now."""
        sym = symbol or next(iter(self.symbols))
        t_out = self.now - pd.Timedelta(minutes=minutes_ago)
        pos = SimpleNamespace(ticket=self._next(), type=TYPE_BUY, magic=magic, symbol=sym, comment="PLAN-X|000000")
        t_in = t_out - pd.Timedelta(minutes=30)
        bal = self.balance
        self._deal(pos, "in", volume, 100.0, 0.0, t_in)
        if partial_split:
            self._deal(pos, "out", volume / 2, 105.0, net * 2, t_out - pd.Timedelta(minutes=5))
            self._deal(pos, "out", volume / 2, 99.0, net - net * 2, t_out)
        else:
            self._deal(pos, "out", volume, 100.0, net, t_out)
        self.balance = bal  # history helpers must not move today's balance
        return pos.ticket

    def add_open_position(self, symbol: str, is_buy: bool, volume: float, price: float, sl: float, tp: float,
                          magic: int, comment: str = "PLAN-OLD|010000", opened_ago_min: float = 5) -> SimpleNamespace:
        return self._open(symbol, is_buy, volume, price, sl, tp, magic, comment,
                          self.now - pd.Timedelta(minutes=opened_ago_min))
