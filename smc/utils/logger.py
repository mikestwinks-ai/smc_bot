"""Logging setup: console + size-rotated file, all under the ``smc`` namespace."""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_TOKEN_RE = re.compile(r"\b(bot)?\d{6,}:[A-Za-z0-9_-]{20,}\b")


class RedactingFilter(logging.Filter):
    """Masks anything that looks like a Telegram bot token, in messages AND formatted arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if _TOKEN_RE.search(msg):
            record.msg, record.args = _TOKEN_RE.sub("[REDACTED-TOKEN]", msg), ()
        return True


_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_ROOT_NAME = "smc"


def setup_logging(
    log_dir: Path,
    file_name: str = "smc_bot.log",
    level: str = "INFO",
    max_bytes: int = 5_000_000,
    backup_count: int = 5,
    console: bool = True,
) -> logging.Logger:
    """Configure the project logger. Safe to call more than once."""
    root = logging.getLogger(_ROOT_NAME)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False

    # Remove handlers from a previous call so we never log twice.
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(_FORMAT, _DATE_FORMAT)

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / file_name,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(RedactingFilter())
    root.addHandler(file_handler)

    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler.addFilter(RedactingFilter())
        root.addHandler(console_handler)

    return root


def get_logger(name: str) -> logging.Logger:
    """Return a logger inside the ``smc`` namespace (pass ``__name__``)."""
    if name == _ROOT_NAME or name.startswith(f"{_ROOT_NAME}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_ROOT_NAME}.{name}")
