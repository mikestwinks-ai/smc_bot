"""Operational commands: ``python -m smc.journal {export,status,reconcile}``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from smc.config import load_config
from smc.core.mt5_connection import MT5Connection
from smc.execution.mt5_gateway import MT5Gateway
from smc.journal.export import export_excel
from smc.journal.reconciliation import MT5Reconciler
from smc.journal.store import JournalStore


def main() -> int:
    parser = argparse.ArgumentParser(description="SMC durable journal operations")
    parser.add_argument("command", choices=("export", "status", "reconcile"))
    parser.add_argument("--database", type=Path, default=None, help="SQLite journal path (defaults to SMC_JOURNAL_DB)")
    parser.add_argument("--output", type=Path, help="new .xlsx output path; existing files are never overwritten")
    args = parser.parse_args()
    cfg = load_config()
    database = args.database or cfg.journal.database_path
    journal = JournalStore(database)
    try:
        if args.command == "export":
            if args.output is None:
                parser.error("export requires --output with a new .xlsx path")
            print(export_excel(journal, args.output))
            return 0
        if args.command == "status":
            print(json.dumps({
                "database": str(database),
                "schema_version": JournalStore.SCHEMA_VERSION,
                "trades": journal.trades(),
                "recent_events": journal.recent_events(50),
                "last_reconciliations": journal.reconciliation_runs(5),
            }, indent=2, default=str))
            return 0
        with MT5Connection(cfg.broker) as connection:
            gateway = MT5Gateway(connection, cfg)
            symbols = {gateway.broker_symbol(symbol.name) for symbol in cfg.enabled_symbols}
            result = MT5Reconciler(journal, gateway, symbols).reconcile()
        print(json.dumps(result.__dict__, indent=2))
        return 0
    finally:
        journal.close()


if __name__ == "__main__":
    sys.exit(main())
