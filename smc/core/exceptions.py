"""Project-wide exception hierarchy.

Catching ``SMCBotError`` catches everything raised deliberately by this
project; more specific subclasses let callers react to particular failures.
"""

from __future__ import annotations


class SMCBotError(Exception):
    """Base class for all project errors."""


class ConfigError(SMCBotError):
    """Invalid or inconsistent configuration."""


class MT5ConnectionError(SMCBotError):
    """Could not initialise, log in to, or keep a MetaTrader 5 connection."""


class SymbolNotFoundError(SMCBotError):
    """A configured symbol does not exist (or cannot be selected) on the broker."""


class MT5DataError(SMCBotError):
    """Market data could not be retrieved or was malformed."""
