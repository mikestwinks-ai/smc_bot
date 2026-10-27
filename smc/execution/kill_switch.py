"""Emergency kill switch.

Engaged  -> no NEW orders are ever sent. Position management (partial, break-even,
trailing, end-of-day closes) keeps working so open risk can still be reduced.
Engage it either programmatically or by creating the KILL_SWITCH file next to the
project (``touch KILL_SWITCH``); delete the file to release it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from smc.utils.logger import get_logger

log = get_logger(__name__)


class KillSwitch:
    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path else None
        self._flag = False
        self._reason = ""

    def engage(self, reason: str = "manual") -> None:
        self._flag, self._reason = True, reason
        log.warning("KILL SWITCH ENGAGED: %s", reason)

    def release(self) -> None:
        self._flag, self._reason = False, ""
        log.warning("Kill switch released (in-memory flag only; delete the file if one exists).")

    @property
    def engaged(self) -> bool:
        return self._flag or bool(self._path and self._path.exists())

    @property
    def reason(self) -> str:
        if self._flag:
            return self._reason
        return f"kill-switch file {self._path} exists" if self.engaged else ""
