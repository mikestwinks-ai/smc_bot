"""MetaTrader 5 connection management.

Responsibilities:
  * initialise / log in / shut down the terminal with retry + back-off
  * detect a dropped connection and transparently reconnect
  * resolve and select symbols (handles broker suffixes such as ``XAUUSDm``)
  * expose lightweight, typed snapshots of account / symbol data

The ``MetaTrader5`` package only exists on Windows, so it is imported lazily
and can be replaced with a fake module (``mt5_module=``) for testing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Optional

from smc.config import BrokerConfig, SymbolConfig
from smc.core.exceptions import MT5ConnectionError, SymbolNotFoundError
from smc.utils.logger import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class AccountSnapshot:
    """Point-in-time view of the trading account."""

    login: int
    server: str
    currency: str
    balance: float
    equity: float
    margin_free: float
    leverage: int
    trade_allowed: bool


class MT5Connection:
    """Thin, resilient wrapper around the MetaTrader5 module."""

    def __init__(self, config: BrokerConfig, mt5_module: Optional[Any] = None) -> None:
        self._cfg = config
        self._mt5 = mt5_module if mt5_module is not None else self._import_mt5()
        self._connected = False
        self._symbol_cache: dict[str, str] = {}  # canonical name -> verified broker symbol

    # ------------------------------------------------------------ construction
    @staticmethod
    def _import_mt5() -> Any:
        try:
            import MetaTrader5 as mt5  # type: ignore[import-not-found]
        except ImportError as exc:
            raise MT5ConnectionError(
                "The 'MetaTrader5' package is not installed (Windows only): "
                "pip install MetaTrader5"
            ) from exc
        return mt5

    @property
    def mt5(self) -> Any:
        """The underlying MetaTrader5 module (for modules that need raw access)."""
        return self._mt5

    # ------------------------------------------------------------- connection
    def connect(self) -> None:
        """Initialise the terminal, retrying with linear back-off."""
        cfg = self._cfg
        last_error: Any = None

        for attempt in range(1, cfg.max_connect_retries + 1):
            kwargs: dict[str, Any] = {"timeout": cfg.timeout_ms}
            if cfg.terminal_path:
                kwargs["path"] = cfg.terminal_path
            if cfg.login:
                kwargs.update(login=cfg.login, password=cfg.password, server=cfg.server)

            log.info("Connecting to MT5 (attempt %d/%d)...", attempt, cfg.max_connect_retries)
            try:
                if self._mt5.initialize(**kwargs) and self._mt5.account_info() is not None:
                    self._connected = True
                    acct = self.get_account_snapshot()
                    log.info(
                        "Connected: account=%s server=%s balance=%.2f %s trade_allowed=%s",
                        acct.login, acct.server, acct.balance, acct.currency, acct.trade_allowed,
                    )
                    if not acct.trade_allowed:
                        log.warning("Trading is NOT allowed on this account/terminal.")
                    return
                last_error = self._mt5.last_error()
            except Exception as exc:  # MT5 can raise on a half-dead terminal
                last_error = exc

            log.warning("MT5 connect failed: %s", last_error)
            self._safe_shutdown()
            if attempt < cfg.max_connect_retries:
                time.sleep(cfg.retry_delay_seconds * attempt)

        self._connected = False
        raise MT5ConnectionError(
            f"Could not connect to MT5 after {cfg.max_connect_retries} attempts: {last_error}"
        )

    def disconnect(self) -> None:
        if self._connected:
            log.info("Shutting down MT5 connection.")
        self._safe_shutdown()
        self._connected = False

    def is_alive(self) -> bool:
        """True if the terminal is running and connected to the broker."""
        try:
            info = self._mt5.terminal_info()
        except Exception:
            return False
        return bool(info is not None and getattr(info, "connected", False))

    def ensure_connected(self) -> None:
        """Reconnect if the link dropped. Call before any MT5 request."""
        if self._connected and self.is_alive():
            return
        log.warning("MT5 connection lost - reconnecting...")
        self._safe_shutdown()
        self._connected = False
        self._symbol_cache.clear()
        self.connect()

    def _safe_shutdown(self) -> None:
        try:
            self._mt5.shutdown()
        except Exception:  # pragma: no cover - nothing useful to do
            pass

    # -------------------------------------------------------- context manager
    def __enter__(self) -> "MT5Connection":
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.disconnect()

    # ----------------------------------------------------------------- account
    def get_account_snapshot(self) -> AccountSnapshot:
        info = self._mt5.account_info()
        if info is None:
            raise MT5ConnectionError(f"account_info() failed: {self._mt5.last_error()}")
        return AccountSnapshot(
            login=int(info.login),
            server=str(info.server),
            currency=str(info.currency),
            balance=float(info.balance),
            equity=float(info.equity),
            margin_free=float(info.margin_free),
            leverage=int(info.leverage),
            trade_allowed=bool(getattr(info, "trade_allowed", True)),
        )

    # ----------------------------------------------------------------- symbols
    def resolve_symbol(self, symbol: SymbolConfig) -> str:
        """Verify the symbol exists, add it to Market Watch, return its broker name.

        Tries ``<name><suffix>`` first, then the bare name as a fallback.
        """
        cached = self._symbol_cache.get(symbol.name)
        if cached:
            return cached

        self.ensure_connected()
        for candidate in dict.fromkeys((symbol.broker_symbol, symbol.name)):
            info = self._mt5.symbol_info(candidate)
            if info is None:
                continue
            if not getattr(info, "visible", True) and not self._mt5.symbol_select(candidate, True):
                log.warning("Found %s but could not add it to Market Watch.", candidate)
                continue
            if candidate != symbol.broker_symbol:
                log.warning("Using fallback symbol %s (expected %s).", candidate, symbol.broker_symbol)
            self._symbol_cache[symbol.name] = candidate
            return candidate

        raise SymbolNotFoundError(
            f"Symbol '{symbol.broker_symbol}' (or '{symbol.name}') not found on the broker."
        )

    def get_symbol_info(self, broker_symbol: str) -> Any:
        """Raw MT5 ``SymbolInfo`` (digits, point, contract size, stops level, ...)."""
        self.ensure_connected()
        info = self._mt5.symbol_info(broker_symbol)
        if info is None:
            raise SymbolNotFoundError(f"symbol_info('{broker_symbol}') returned None")
        return info

    def get_tick(self, broker_symbol: str) -> Any:
        """Latest tick (bid/ask/time) for a symbol."""
        self.ensure_connected()
        tick = self._mt5.symbol_info_tick(broker_symbol)
        if tick is None:
            raise MT5ConnectionError(
                f"symbol_info_tick('{broker_symbol}') failed: {self._mt5.last_error()}"
            )
        return tick
