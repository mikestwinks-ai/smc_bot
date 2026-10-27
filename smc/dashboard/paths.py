"""Filesystem locations shared by the bot publisher and local dashboard."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SNAPSHOT_PATH = PROJECT_ROOT / "output" / "dashboard" / "runtime_snapshot.json"

