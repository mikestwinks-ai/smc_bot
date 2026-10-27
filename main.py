"""Entry point: data -> structure -> plans -> (dry-run) execution and trade management.

Usage:
    python main.py            # run until Ctrl+C
    python main.py --once     # connect, load data, plan, run ONE cycle, exit

SAFETY: live order submission is OFF unless BOTH SMC_LIVE_EXECUTION=true and
SMC_LIVE_CONFIRM=<phrase in smc/config.py> are set in your local .env. Without them
the bot validates, sizes and runs MT5's order_check, but sends nothing. Even with
live on, REAL accounts are refused unless SMC_ALLOW_REAL_ACCOUNT=true.
Emergency stop: create a file named KILL_SWITCH next to this script.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time

from smc import __version__
from smc.config import BotConfig, load_config
from smc.core.data_manager import MarketDataManager
from smc.core.exceptions import SMCBotError
from smc.core.mt5_connection import MT5Connection
from smc.dashboard.snapshot import AsyncSnapshotPublisher, build_runtime_snapshot
from smc.execution import ExecutionSystem
from smc.execution.fills import exec_key
from smc.journal.store import utc_iso
from smc.news import NewsMonitor, build_news
from smc.notify import build_notifier
from smc.notify.router import NotificationRouter
from smc.planning import ChangeKind, PlanManager, PlanStore
from smc.structure import StructureHub
from smc.utils.logger import get_logger, setup_logging

BOT_SNAPSHOT_PUBLISH_INTERVAL_SECONDS = 5.0


def print_summary(manager: MarketDataManager, config: BotConfig) -> None:
    log = get_logger("main")
    for symbol in manager.symbols:
        for tf in config.timeframes.all:
            last = manager.get_latest_closed(symbol, tf)
            log.info("%-8s %-3s %-8s last closed %s  O=%.5f H=%.5f L=%.5f C=%.5f", symbol, tf,
                     config.timeframes.layer_of(tf).value, last.name, last["open"], last["high"],
                     last["low"], last["close"])


def log_cycle(log, report) -> None:
    for a in report.actions:
        log.info("MANAGE %-9s %s %s | %s", a.kind, a.target, "ok" if a.ok else "no", a.note)
    for r in report.results:
        if r.outcome.value not in ("rejected", "already_executed"):
            log.info("EXEC %s -> %s %s", r.plan_id, r.outcome.value, "; ".join(r.reasons))
        else:
            log.debug("EXEC %s -> %s %s", r.plan_id, r.outcome.value, "; ".join(r.reasons))
    for e in report.errors:
        log.error("CYCLE %s", e)


def journal_plan_changes(system: ExecutionSystem, changes, manager: PlanManager, seen_discards: set[str]) -> None:
    for change in changes:
        try:
            plan = manager.store.get(change.plan_id)
        except KeyError:
            plan = None
        system.journal.record_event_best_effort(
            f"plan_{change.kind.value}", occurred_at=change.time,
            idempotency_key=f"plan:{change.plan_id}:{change.kind.value}:{utc_iso(change.time)}",
            correlation_id=exec_key(plan.plan_id, plan.created_at) if plan else change.plan_id,
            plan_id=change.plan_id,
            symbol=plan.symbol if plan else None,
            direction=plan.direction.value if plan else None,
            status=plan.status.value if plan else None, note=change.note,
            entry_model=plan.entry_model.value if plan else None,
            zones=plan.zones.all_ids if plan else (),
            plan_revision_at_utc=utc_iso(plan.updated_at) if plan else None,
            entry_zone=({"bottom": plan.entry_zone.bottom, "top": plan.entry_zone.top} if plan else None),
            stop_loss=plan.invalidation.price if plan else None,
            targets=list(plan.targets) if plan else None,
            rr_estimate=plan.rr_estimate if plan else None,
            parent_plan_id=plan.parent_plan_id if plan else None,
            child_plan_ids=list(plan.child_plan_ids) if plan else None,
        )
    for discard in manager.discards:
        key = (f"setup-rejected:{discard.symbol}:{discard.timeframe.label}:{discard.model.value}:"
               f"{discard.ref_id}:{discard.time.isoformat()}:{discard.reason}")
        if key in seen_discards:
            continue
        seen_discards.add(key)
        system.journal.record_event_best_effort(
            "setup_rejected", occurred_at=discard.time, idempotency_key=key,
            symbol=discard.symbol, direction=None, timeframe=discard.timeframe.label,
            entry_model=discard.model.value, zone_id=discard.ref_id, reason=discard.reason,
        )


def journal_market_observations(system: ExecutionSystem, events, data) -> None:
    for event in events:
        candles = data.get_candles(event.symbol, event.timeframe, event.new_candles)
        for timestamp, row in candles.iterrows():
            time = utc_iso(timestamp)
            system.journal.record_event_best_effort(
                "market_observation", occurred_at=timestamp,
                idempotency_key=f"candle:{event.symbol}:{event.timeframe.label}:{time}",
                symbol=event.symbol, timeframe=event.timeframe.label,
                open=float(row["open"]), high=float(row["high"]), low=float(row["low"]),
                close=float(row["close"]), tick_volume=int(row["tick_volume"]),
            )


def run(once: bool) -> int:
    system = None
    dashboard_publisher = None
    config = load_config()
    setup_logging(log_dir=config.logging.log_dir, file_name=config.logging.file_name, level=config.logging.level,
                  max_bytes=config.logging.max_bytes, backup_count=config.logging.backup_count,
                  console=config.logging.console)
    log = get_logger("main")
    mode = "LIVE ORDERS ENABLED" if config.execution.live_enabled else "DRY-RUN (no orders are sent)"
    log.info("SMC bot %s starting - %s - strategy timezone %s", __version__, mode, config.sessions.cutoff_timezone)

    try:
        dashboard_publisher = AsyncSnapshotPublisher()
        with MT5Connection(config.broker) as connection:
            data = MarketDataManager(connection, config)
            data.initialize()
            print_summary(data, config)

            plans = PlanManager(
                config,
                StructureHub(config),
                store=PlanStore(config.journal.database_path),
            )
            plans.initialize(data)  # replay history: no plans are created from the past
            bootstrap_changes = plans.bootstrap(data.symbols)  # one scan of the current state
            guard = monitor = None
            if config.news.enabled:  # a calendar that cannot be built stops startup: never trade unprotected by accident
                _, guard = build_news(config)
                guard.service.refresh(force=True)
            else:
                log.error("NEWS PROTECTION IS DISABLED (news.enabled=False). New entries are NOT protected from events.")
            system = ExecutionSystem(config, connection, plans, news_guard=guard)
            seen_discards: set[str] = set()
            journal_plan_changes(system, bootstrap_changes, plans, seen_discards)
            try:
                system.reconcile_history()
            except (SMCBotError, sqlite3.Error, OSError) as exc:
                log.error("Startup journal reconciliation failed; broker data was not treated as empty: %s", exc)
                system.journal.record_event_best_effort("system_error", occurred_at=system.gateway.now(),
                                                         component="startup_reconciliation", error=str(exc))
            notifier = build_notifier(config.telegram, system.state)
            router = NotificationRouter(notifier, config, system)
            if guard is not None:
                monitor = NewsMonitor(guard, config.news, notifier, system.clock.timezone_name,
                                      journal=system.journal)
            notifier.notify(f"SMC bot started - {mode}. Strategy timezone {system.clock.timezone_name}. "
                            f"News protection: {'ON (' + guard.service.provider_name + ')' if guard else 'OFF'}.", persist=False)

            for symbol in data.symbols:
                snap = plans.hub.snapshot(symbol)
                log.info("%s structure: external=%s internal=%s | live plans=%d", symbol, snap.external_bias.value,
                         snap.internal_bias.value, len(plans.store.list_active(symbol)))
            if monitor:
                st = monitor.tick()
                log.info("NEWS: calendar %s, active locks=%d, upcoming alerts=%d", st.health, len(st.active_locks), len(st.upcoming))
            report = system.run_cycle()
            log_cycle(log, report)
            router.cycle(report)
            try:
                dashboard_publisher.publish(
                    build_runtime_snapshot(config, data, plans, system, guard, system.gateway.now())
                )
            except Exception:
                log.exception("Dashboard snapshot assembly failed; trading safeguards remain active")
            if once:
                notifier.close()
                system.close()
                return 0

            log.info("Running. Ctrl+C to stop. Kill switch file: %s", config.execution.kill_switch_path)
            last_cycle = time.monotonic()
            last_dashboard_publish = last_cycle
            while True:
                events = data.update_all()
                changes = plans.on_candle_events(events, data)
                system.journal.record_structure_events(plans.drain_structure_events())
                journal_market_observations(system, events, data)
                journal_plan_changes(system, changes, plans, seen_discards)
                for ch in changes:
                    if ch.kind in (ChangeKind.CREATED, ChangeKind.CANCELED, ChangeKind.FLIPPED, ChangeKind.UPGRADED):
                        log.info("PLAN %s %s | %s", ch.kind.value, ch.plan_id, ch.note)
                router.plan_changes(changes)
                if monitor:
                    monitor.tick()
                if events or time.monotonic() - last_cycle >= config.execution.cycle_interval_seconds:
                    report = system.run_cycle()
                    log_cycle(log, report)
                    router.cycle(report)
                    last_cycle = time.monotonic()
                if time.monotonic() - last_dashboard_publish >= BOT_SNAPSHOT_PUBLISH_INTERVAL_SECONDS:
                    try:
                        dashboard_publisher.publish(
                            build_runtime_snapshot(config, data, plans, system, guard, system.gateway.now())
                        )
                    except Exception:
                        log.exception("Dashboard snapshot assembly failed; trading safeguards remain active")
                    last_dashboard_publish = time.monotonic()
                time.sleep(config.data.poll_interval_seconds)
    except KeyboardInterrupt:
        if system is not None:
            system.close()
        log.info("Stopped by user. Open positions/orders are left as they are; the kill switch and 23:00 flatten still apply on restart.")
        return 0
    except SMCBotError as exc:
        if system is not None:
            system.journal.record_event_best_effort("system_error", occurred_at=system.gateway.now(),
                                                    component="startup", error=str(exc))
            system.close()
        log.error("Fatal: %s", exc)
        return 1
    finally:
        if dashboard_publisher is not None:
            dashboard_publisher.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SMC bot (dry-run by default)")
    parser.add_argument("--once", action="store_true", help="load data, plan, run one cycle, exit")
    sys.exit(run(parser.parse_args().once))
