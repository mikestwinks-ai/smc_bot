"""Sanitized bot-owned dashboard snapshots and non-blocking file publication."""

from __future__ import annotations

import json
import hashlib
import os
import queue
import re
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import pandas as pd

from smc.dashboard.paths import DEFAULT_SNAPSHOT_PATH
from smc.core.exceptions import SMCBotError
from smc.utils.logger import get_logger

if TYPE_CHECKING:
    from smc.config import BotConfig
    from smc.core.data_manager import MarketDataManager
    from smc.planning.plan_manager import PlanManager

log = get_logger(__name__)
MAX_CANDLES_PER_SERIES = 500
SECRET_PATTERN = re.compile(
    r"(?i)\b(password|passwd|token|secret|api[_-]?key|authorization)\b(\s*[:=]\s*)([^\s,;]+)"
)
_PUBLISHER_PATHS: set[str] = set()
_PUBLISHER_PATHS_LOCK = threading.Lock()
SNAPSHOT_REPLACE_ATTEMPTS = 3
SNAPSHOT_REPLACE_RETRY_SECONDS = 0.025


class SnapshotPublisherAlreadyRunningError(SMCBotError):
    """Another process already owns the configured snapshot destination."""


class _SnapshotOwnershipLock:
    """OS-released lock; the lock file is only a stable rendezvous point."""

    def __init__(self, snapshot_path: Path) -> None:
        canonical = os.path.normcase(str(snapshot_path.resolve()))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self.lock_path = snapshot_path.parent / f".snapshot-publisher-{digest}.lock"
        self._canonical = canonical
        self._handle = None
        with _PUBLISHER_PATHS_LOCK:
            if canonical in _PUBLISHER_PATHS:
                raise SnapshotPublisherAlreadyRunningError(
                    f"A snapshot publisher already owns {snapshot_path.resolve()}."
                )
            _PUBLISHER_PATHS.add(canonical)
        try:
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.lock_path.open("a+b")
            try:
                if os.name == "nt":
                    import msvcrt

                    self._handle.seek(0, os.SEEK_END)
                    if self._handle.tell() == 0:
                        self._handle.write(b"\0")
                        self._handle.flush()
                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise SnapshotPublisherAlreadyRunningError(
                    f"A snapshot publisher already owns {snapshot_path.resolve()}."
                ) from exc
        except Exception:
            if self._handle is not None:
                self._handle.close()
            with _PUBLISHER_PATHS_LOCK:
                _PUBLISHER_PATHS.discard(canonical)
            raise

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            log.exception("Failed to release snapshot publisher lock %s", self.lock_path)
        finally:
            self._handle.close()
            self._handle = None
            with _PUBLISHER_PATHS_LOCK:
                _PUBLISHER_PATHS.discard(self._canonical)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    else:
        stamp = stamp.tz_convert("UTC")
    return stamp.isoformat()


def _safe_text(value: Any, limit: int = 400) -> str:
    text = str(value)
    return SECRET_PATTERN.sub(r"\1\2[REDACTED]", text)[:limit]


def _safe_payload_value(value: Any) -> Any:
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, list):
        return [_safe_payload_value(item) for item in value[:30]]
    if isinstance(value, dict):
        return {str(key): _safe_payload_value(item) for key, item in list(value.items())[:30]}
    return value


_EVENT_PAYLOAD_KEYS = {
    "action", "age_hours", "bias", "bias_after", "bias_before", "broker_comment", "broker_status",
    "bottom", "child_plan_ids", "close", "component", "consecutive_failures", "direction", "entry_model",
    "entry_zone", "error", "event_key", "extreme", "invalidation", "kind", "last_success_utc", "level",
    "level_id", "level_price", "new_bias", "new_zone_id", "note", "old_bias", "order_kind", "outcome",
    "parent_plan_id", "plan_revision_at_utc", "price", "provider", "reason", "reasons",
    "retcode", "retryable", "rr_estimate", "side", "status", "stop_loss", "structure_type", "succeeded",
    "swept_level_ids", "target", "targets", "timeframe", "top", "verified", "volume", "zone_id",
    "zone_kind", "zones",
}


def _event_payload(event: Any) -> dict[str, Any]:
    if not isinstance(event, dict):
        return {}
    payload = event.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = {"note": payload}
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        payload = {"note": payload}
    return {
        str(key): _safe_payload_value(value)
        for key, value in payload.items()
        if key in _EVENT_PAYLOAD_KEYS and value is not None
    }


def _snapshot_event(event: Any) -> Optional[dict[str, Any]]:
    if not isinstance(event, dict):
        return None
    try:
        occurred_at = _iso(event.get("occurred_at"))
    except (TypeError, ValueError, OverflowError):
        occurred_at = None
    event_type = event.get("event_type")
    try:
        event_type = _safe_text(event_type, 100) if event_type is not None else None
    except Exception:
        event_type = None
    return {
        "event_id": _safe_text(event["event_id"], 100) if event.get("event_id") else None,
        "occurred_at_utc": occurred_at,
        "event_type": event_type,
        "correlation_id": _safe_text(event["correlation_id"], 120) if event.get("correlation_id") else None,
        "symbol": _safe_text(event["symbol"], 80) if event.get("symbol") else None,
        "plan_id": _safe_text(event["plan_id"], 120) if event.get("plan_id") else None,
        "order_ticket": event.get("order_ticket"),
        "payload": _event_payload(event),
    }


def _pd_reading(reading: Any) -> Optional[dict[str, Any]]:
    if reading is None:
        return None
    return {
        "layer": reading.layer.value,
        "timeframe": reading.timeframe.label,
        "range_high": reading.range_high,
        "range_low": reading.range_low,
        "price": reading.price,
        "position": reading.position,
        "state": reading.state.value,
    }


def _structure(hub: Any, symbol: str) -> dict[str, Any]:
    from smc.structure.models import FairValueGap, OrderBlock
    from smc.planning.market_context import zone_state

    state = hub.snapshot(symbol)

    def layer_bias(engine: Any) -> tuple[str, dict[str, Optional[str]], dict[str, Optional[str]]]:
        biases: dict[str, Optional[str]] = {}
        timestamps: dict[str, Optional[str]] = {}
        for timeframe in engine.timeframes:
            tracker = engine.get_tracker(symbol, timeframe)
            has_candles = tracker is not None and bool(tracker.buffer.times)
            biases[timeframe.label] = tracker.bias.value if has_candles else None
            timestamps[timeframe.label] = (
                _iso(tracker.buffer.times[-1]) if has_candles else None
            )
        anchor = engine.anchor_timeframe.label
        return anchor, biases, timestamps

    external_anchor, external_biases, external_times = layer_bias(hub.external)
    internal_anchor, internal_biases, internal_times = layer_bias(hub.internal)
    zones = []
    for zone in [*state.order_blocks, *state.fvgs]:
        record = {
            "id": zone.id,
            "kind": zone.kind.value,
            "symbol": zone.symbol,
            "timeframe": zone.timeframe.label,
            "bias": zone.bias.value,
            "top": zone.top,
            "bottom": zone.bottom,
            "created_index": zone.created_index,
            "status": zone.status.value,
            "market_state": zone_state(zone)[0],
            "created_utc": _iso(zone.created_time),
            "flipped": zone.is_flipped_zone,
            "touches": zone.touches,
            "reaction_count": zone.reaction_count,
            "last_reaction_utc": _iso(zone.last_reaction_time),
            "last_reaction_close": zone.last_reaction_close,
            "max_penetration": zone.max_penetration,
            "mitigated_utc": _iso(zone.mitigated_time),
            "fully_filled_utc": _iso(zone.fully_filled_time),
            "flip_count": zone.flip_count,
            "flipped_from": zone.flipped_from,
            "flipped_to": zone.flipped_to,
        }
        if isinstance(zone, OrderBlock):
            record["displacement_ratio"] = zone.displacement_ratio
            record["has_fvg"] = zone.has_fvg
            record["origin_event_id"] = zone.origin_event_id
            record["origin_event_type"] = zone.origin_event_type.value
            record["candle_time_utc"] = _iso(zone.candle_time)
            record["leg_start_index"] = zone.leg_start_index
        if isinstance(zone, FairValueGap):
            record["gap_size"] = zone.gap_size
        zones.append(record)
    swings = {}
    for label, mapping in (("external", state.external_swings), ("internal", state.internal_swings)):
        swings[label] = {
            name: None if swing is None else {
                "type": swing.type.value,
                "price": swing.price,
                "time_utc": _iso(swing.time),
                "confirmed_utc": _iso(swing.confirmed_time),
                "timeframe": swing.timeframe.label,
            }
            for name, swing in mapping.items()
        }
    return {
        "symbol": symbol,
        "price": state.price,
        "external_anchor_timeframe": external_anchor,
        "internal_anchor_timeframe": internal_anchor,
        "external_bias": external_biases[external_anchor],
        "internal_bias": internal_biases[internal_anchor],
        "external_bias_by_timeframe": external_biases,
        "internal_bias_by_timeframe": internal_biases,
        "external_bias_time_utc_by_timeframe": external_times,
        "internal_bias_time_utc_by_timeframe": internal_times,
        "premium_discount": {
            "external": _pd_reading(state.premium_discount.external),
            "internal": _pd_reading(state.premium_discount.internal),
        },
        "swings": swings,
        "zones": zones,
        "liquidity": [{
            "id": item.id,
            "kind": item.kind.value,
            "side": item.side.value,
            "price": item.price,
            "timeframe": item.timeframe.label,
            "created_utc": _iso(item.created_time),
            "status": item.status.value,
            "label": item.label,
        } for item in state.liquidity],
    }


def _plan_entry_basis(plan: Any):
    from smc.models.plan import EntryModel, EntryRangeBasis

    basis = getattr(plan, "entry_range_basis", None)
    if basis is not None:
        return basis
    if plan.parent_plan_id and plan.zones.flipped_zone_id:
        return EntryRangeBasis.FLIPPED_ZONE_RETEST
    if plan.entry_model is EntryModel.OB:
        return EntryRangeBasis.ORDER_BLOCK
    if plan.entry_model is EntryModel.FVG:
        return EntryRangeBasis.FAIR_VALUE_GAP
    if (plan.entry_model is EntryModel.OB_FVG and plan.quality.confluence
            and plan.zones.order_block_id and plan.zones.fvg_id):
        return EntryRangeBasis.OB_FVG_CONFLUENCE
    if plan.entry_model is EntryModel.SWEEP_MSS:
        if plan.zones.order_block_id:
            return EntryRangeBasis.SWEEP_MSS_ORDER_BLOCK
        if plan.zones.fvg_id:
            return EntryRangeBasis.SWEEP_MSS_FVG
    return None


def _plan(plan: Any, zone_lookup: Any = None) -> dict[str, Any]:
    from smc.planning.market_context import zone_state

    from smc.models.plan import EntryRangeBasis

    source_zone_ids = {
        "order_block": plan.zones.order_block_id,
        "fvg": plan.zones.fvg_id,
        "flipped": plan.zones.flipped_zone_id,
    }
    zone_ids = list(dict.fromkeys(zone_id for zone_id in source_zone_ids.values() if zone_id))
    source_zones = []
    from smc.structure.models import FairValueGap, OrderBlock

    retained_zones = {zone.id: zone for zone in getattr(plan, "source_zones", ())}
    for zone_id in zone_ids:
        zone = zone_lookup(zone_id) if zone_lookup is not None else None
        record_freshness = "current"
        if zone is None:
            zone = retained_zones.get(zone_id)
            record_freshness = "last_known"
        if zone is None:
            continue
        if zone.id != zone_id:
            continue
        record = {
            "id": zone.id,
            "kind": zone.kind.value,
            "symbol": zone.symbol,
            "timeframe": zone.timeframe.label,
            "bias": zone.bias.value,
            "top": zone.top,
            "bottom": zone.bottom,
            "created_index": zone.created_index,
            "status": zone.status.value,
            "market_state": zone_state(zone)[0],
            "created_utc": _iso(zone.created_time),
            "touches": zone.touches,
            "reaction_count": zone.reaction_count,
            "last_reaction_utc": _iso(zone.last_reaction_time),
            "last_reaction_close": zone.last_reaction_close,
            "max_penetration": zone.max_penetration,
            "mitigated_utc": _iso(zone.mitigated_time),
            "fully_filled_utc": _iso(zone.fully_filled_time),
            "record_freshness": record_freshness,
        }
        if isinstance(zone, OrderBlock):
            record.update({
                "origin_event_id": zone.origin_event_id,
                "origin_event_type": zone.origin_event_type.value,
                "candle_time_utc": _iso(zone.candle_time),
                "leg_start_index": zone.leg_start_index,
                "displacement_ratio": zone.displacement_ratio,
            })
        if isinstance(zone, FairValueGap):
            record["gap_size"] = zone.gap_size
        source_zones.append(record)

    basis = _plan_entry_basis(plan)
    basis_labels = {
        EntryRangeBasis.ORDER_BLOCK: "Order block",
        EntryRangeBasis.FAIR_VALUE_GAP: "Fair value gap",
        EntryRangeBasis.OB_FVG_CONFLUENCE: "OB + FVG confluence",
        EntryRangeBasis.SWEEP_MSS_ORDER_BLOCK: "Sweep + MSS retrace at order block",
        EntryRangeBasis.SWEEP_MSS_FVG: "Sweep + MSS retrace at FVG",
        EntryRangeBasis.SWEEP_MSS_RETRACEMENT: "Sweep + MSS leg retracement",
        EntryRangeBasis.FLIPPED_ZONE_RETEST: "Flipped-zone retest",
    }
    basis_label = basis_labels.get(basis, "Not recorded for this plan")
    if basis in (EntryRangeBasis.ORDER_BLOCK, EntryRangeBasis.FAIR_VALUE_GAP):
        basis_label = f"{basis_label} ({plan.setup_timeframe.label})"

    source_relationship = None
    if basis is EntryRangeBasis.OB_FVG_CONFLUENCE:
        records = {record["id"]: record for record in source_zones}
        ob = records.get(plan.zones.order_block_id)
        fvg = records.get(plan.zones.fvg_id)
        same_leg = bool(
            ob and fvg
            and ob["kind"] == "OB" and fvg["kind"] == "FVG"
            and ob["symbol"] == fvg["symbol"] == plan.symbol
            and ob["timeframe"] == fvg["timeframe"] == plan.setup_timeframe.label
            and ob["bias"] == fvg["bias"]
            and ob.get("leg_start_index") is not None
            and ob["leg_start_index"] <= fvg["created_index"] <= ob["created_index"]
            and plan.quality.confluence
        )
        source_relationship = {
            "kind": "ob_fvg_displacement_leg",
            "status": "verified" if same_leg else "unavailable",
            "order_block_id": plan.zones.order_block_id,
            "fvg_id": plan.zones.fvg_id,
            "origin_event_id": ob.get("origin_event_id") if ob else None,
            "description": (
                "FVG formed within the OB's recorded displacement interval"
                if same_leg else
                "The source records do not permit verification of the OB/FVG displacement-leg link"
            ),
        }

    return {
        "plan_id": plan.plan_id,
        "symbol": plan.symbol,
        "direction": plan.direction.value,
        "entry_model": plan.entry_model.value,
        "status": plan.status.value,
        "source_timeframes": {
            "external": [tf.label for tf in plan.source_timeframes.external],
            "internal": [tf.label for tf in plan.source_timeframes.internal],
            "setup": plan.source_timeframes.setup.label,
        },
        "external_bias": plan.external_bias.value,
        "internal_bias": plan.internal_bias.value,
        "entry_zone": {"top": plan.entry_zone.top, "bottom": plan.entry_zone.bottom},
        "source_zone_ids": source_zone_ids,
        "source_zones": source_zones,
        "entry_range_basis": basis.value if basis is not None else None,
        "entry_range_basis_label": basis_label,
        "source_relationship": source_relationship,
        "invalidation": {"price": plan.invalidation.price, "basis": plan.invalidation.basis},
        "targets": list(plan.targets),
        "rr_estimate": plan.rr_estimate,
        "reason": _safe_text(plan.reason),
        "created_at_utc": _iso(plan.created_at),
        "updated_at_utc": _iso(plan.updated_at),
        "external_range_position": plan.external_range_position,
        "internal_range_position": plan.internal_range_position,
        "quality": {
            "has_fvg": plan.quality.has_fvg,
            "confluence": plan.quality.confluence,
            "impulse_ratio": plan.quality.impulse_ratio,
            "partially_filled": plan.quality.partially_filled,
            "counter_trend_internal": plan.quality.counter_trend_internal,
        },
        "cancel_reason": _safe_text(plan.cancel_reason) if plan.cancel_reason else None,
        "flip_reason": _safe_text(plan.flip_reason) if plan.flip_reason else None,
        "parent_plan_id": plan.parent_plan_id,
        "child_plan_ids": list(plan.child_plan_ids),
        "history": [{"time_utc": _iso(item.time), "kind": item.kind, "note": _safe_text(item.note)}
                    for item in plan.history[-30:]],
        "market_context": _safe_payload_value(plan.market_context) if plan.market_context else None,
    }


def build_runtime_snapshot(
    config: BotConfig,
    data: MarketDataManager,
    plans: PlanManager,
    system: Any,
    news_guard: Any = None,
    now: Optional[pd.Timestamp] = None,
) -> dict[str, Any]:
    """Read only already-held bot state; does not call MT5 or mutate the journal."""
    now = now or pd.Timestamp.now(tz="UTC")
    generated = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
    stale_after = max(15.0, config.data.poll_interval_seconds * 5.0)
    series_status = data.series_status(generated)
    heartbeat_times = [pd.Timestamp(row["last_success_utc"]) for row in series_status
                       if row["last_success_utc"] is not None]
    latest_market_success = max(heartbeat_times) if heartbeat_times else None
    mt5_age = ((generated - latest_market_success).total_seconds()
               if latest_market_success is not None else None)

    markets: dict[str, dict[str, Any]] = {}
    structure: dict[str, Any] = {}
    for symbol in data.symbols:
        markets[symbol] = {}
        for tf in config.timeframes.all:
            frame = data.get_candles(symbol, tf, MAX_CANDLES_PER_SERIES)
            status = next((row for row in series_status
                           if row["symbol"] == symbol and row["timeframe"] == tf.label), None)
            success_at = pd.Timestamp(status["last_success_utc"]) if status and status["last_success_utc"] else None
            age = (generated - success_at).total_seconds() if success_at is not None else None
            markets[symbol][tf.label] = {
                "timeframe_seconds": tf.seconds,
                "candles": [{
                    "time_utc": _iso(index),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "tick_volume": int(row["tick_volume"]),
                } for index, row in frame.iterrows()],
                "last_closed_candle_utc": status["last_closed_candle_utc"] if status else None,
                "last_success_utc": status["last_success_utc"] if status else None,
                "age_seconds": age,
                "stale": age is None or age > stale_after,
                "last_error": _safe_text(status["last_error"]) if status and status["last_error"] else None,
            }
        structure[symbol] = _structure(plans.hub, symbol)
        scenarios, scenario_evaluation = plans.conditional_scenario_assessment(symbol)
        structure[symbol]["conditional_scenarios"] = scenarios
        structure[symbol]["conditional_scenario_evaluation"] = scenario_evaluation

    observed = system.gateway.observed_state()
    account = observed["account"]
    configured_symbols = {
        value
        for item in config.enabled_symbols
        for value in (item.name, item.broker_symbol)
    }
    configured_symbols.update(
        data.broker_symbol(symbol) for symbol in data.symbols
    )
    positions = ([item for item in observed["positions"] if item.symbol in configured_symbols]
                 if observed["positions"] is not None else None)
    orders = ([item for item in observed["pending_orders"] if item.symbol in configured_symbols]
              if observed["pending_orders"] is not None else None)
    exposure = system.exposure.last_snapshot
    risk_problems = (system.risk.limit_problems(exposure, "", False) if exposure is not None else None)
    risk = {
        "available": exposure is not None,
        "unavailable_reason": (
            None if exposure is not None else
            "No successful risk/exposure calculation is cached. Calculating one requires broker history reads; "
            "the dashboard publisher intentionally uses cached observations only."
        ),
        "as_of_utc": _iso(exposure.now) if exposure is not None else None,
        "daily_trade_count": exposure.trades_opened_today if exposure is not None else None,
        "consecutive_losses": exposure.consecutive_losses if exposure is not None else None,
        "realised_pl_today": exposure.realised_pl_today if exposure is not None else None,
        "realised_loss_pct": exposure.realised_loss_pct if exposure is not None else None,
        "total_drawdown_pct": exposure.total_drawdown_pct if exposure is not None else None,
        "baseline_balance": exposure.baseline_balance if exposure is not None else None,
        "open_count": exposure.open_count if exposure is not None else None,
        "limits_ok": not risk_problems if risk_problems is not None else None,
        "limit_reasons": [_safe_text(reason) for reason in risk_problems] if risk_problems is not None else [],
        "limits": {
            "balance_threshold": config.risk.balance_threshold,
            "risk_pct_below_threshold": config.risk.risk_pct_below_threshold,
            "risk_pct_at_or_above_threshold": config.risk.risk_pct_at_or_above_threshold,
            "hard_max_risk_pct": config.risk.hard_max_risk_pct,
            "max_daily_drawdown_pct": config.risk.max_daily_drawdown_pct,
            "max_consecutive_losses": config.risk.max_consecutive_losses,
            "max_trades_per_day": config.risk.max_trades_per_day,
            "max_open_trades_total": config.risk.max_open_trades_total,
            "max_open_trades_per_direction_per_symbol": config.risk.max_open_trades_per_direction_per_symbol,
        },
        "sessions": {
            "windows": [{
                "name": window.name,
                "timezone": window.timezone,
                "start": window.start.isoformat(timespec="minutes"),
                "end": window.end.isoformat(timespec="minutes"),
            } for window in config.sessions.windows],
            "cutoff_timezone": config.sessions.cutoff_timezone,
            "trading_day_start": config.sessions.trading_day_start.isoformat(timespec="minutes"),
            "no_new_trades_after": config.sessions.no_new_trades_after.isoformat(timespec="minutes"),
            "close_all_risk_by": config.sessions.close_all_risk_by.isoformat(timespec="minutes"),
            "crypto_exempt_from_cutoffs": config.sessions.crypto_exempt_from_cutoffs,
        },
    }

    broker_age = (generated - account.fetched_at).total_seconds() if account is not None else None
    broker_snapshot = {
        "status": "fresh" if broker_age is not None and broker_age <= stale_after else
                  ("stale" if broker_age is not None else "unavailable"),
        "last_account_update_utc": _iso(account.fetched_at) if account is not None else None,
        "age_seconds": broker_age,
        "connection_status": "recent_success" if mt5_age is not None and mt5_age <= stale_after else "stale_or_unknown",
    }
    account_view = None if account is None else {
        "login_masked": f"****{str(account.login)[-2:]}",
        "currency": account.currency,
        "environment": "real" if account.is_real else "demo",
        "balance": account.balance,
        "equity": account.equity,
        "trade_allowed": account.trade_allowed,
    }
    position_view = None if positions is None else [{
        "ticket": item.ticket, "symbol": item.symbol,
        "direction": "buy" if item.is_buy else "sell", "volume": item.volume,
        "open_price": item.price_open, "current_price": item.price_current,
        "stop_loss": item.sl, "take_profit": item.tp, "profit": item.profit,
        "swap": item.swap, "opened_at_utc": _iso(item.time),
    } for item in positions]
    order_view = None if orders is None else [{
        "ticket": item.ticket, "symbol": item.symbol,
        "direction": "buy" if item.is_buy else "sell",
        "kind": "limit" if item.is_limit else "stop", "volume": item.volume,
        "price": item.price_open, "stop_loss": item.sl, "take_profit": item.tp,
        "created_at_utc": _iso(item.time_setup),
    } for item in orders]
    positions_age = ((generated - observed["positions_at"]).total_seconds()
                     if observed["positions_at"] is not None else None)
    orders_age = ((generated - observed["pending_orders_at"]).total_seconds()
                  if observed["pending_orders_at"] is not None else None)

    news: dict[str, Any] = {"enabled": False, "health": "disabled", "upcoming": [], "active_locks": []}
    if news_guard is not None:
        service = news_guard.service
        health = service.health(generated)
        upcoming = []
        for event in news_guard.qualifying_events():
            if event.time >= generated:
                window = news_guard.base_window(event)
                upcoming.append({
                    "event_id": event.id,
                    "time_utc": _iso(event.time),
                    "currency": event.currency,
                    "title": _safe_text(event.title, 200),
                    "impact": event.impact.value,
                    "source": _safe_text(event.source, 100),
                    "forecast": event.forecast_raw or None,
                    "previous": event.previous_raw or None,
                    "actual": event.actual_raw or None,
                    "lock_start_utc": _iso(window.start),
                    "lock_end_utc": _iso(window.end),
                })
        locks = []
        for symbol in (item.name for item in config.enabled_symbols):
            decision = news_guard.check(symbol, generated)
            if decision.locked:
                locks.append({"symbol": symbol, "code": decision.code,
                              "reasons": [_safe_text(reason) for reason in decision.reasons],
                              "lock_start_utc": _iso(decision.lock_start),
                              "lock_end_utc": _iso(decision.lock_end)})
        news = {
            "enabled": True,
            "provider": _safe_text(service.provider_name, 100),
            "health": health.status.value,
            "verified": health.verified,
            "last_success_utc": _iso(health.last_success),
            "age_hours": health.age_hours,
            "consecutive_failures": health.consecutive_failures,
            "last_error": _safe_text(health.last_error) if health.last_error else None,
            "filters": {"currencies": list(config.news.currencies), "impacts": list(config.news.impact_levels)},
            "upcoming": sorted(upcoming, key=lambda row: row["time_utc"])[:50],
            "active_locks": locks,
        }

    journal_read_error = None
    try:
        recent_events = [
            normalized for event in system.journal.recent_events(30)
            if (normalized := _snapshot_event(event)) is not None
        ]
        recent_structure_events = [
            normalized
            for event in system.journal.recent_events_by_prefix(
                ("structure_zone_", "structure_break", "liquidity_sweep"), limit=100
            )
            if (normalized := _snapshot_event(event)) is not None
        ]
        latest_reconciliation = system.journal.latest_reconciliation()
    except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
        log.error("Dashboard snapshot could not read journal status (%s)", type(exc).__name__)
        recent_events, recent_structure_events, latest_reconciliation = [], [], None
        journal_read_error = type(exc).__name__

    return {
        "schema_version": 1,
        "generated_at_utc": _iso(generated),
        "bot": {"status": "running", "live_enabled": config.execution.live_enabled},
        "mt5": broker_snapshot,
        "account": account_view,
        "execution": {
            "live_enabled": config.execution.live_enabled,
            "kill_switch_engaged": system.kill.engaged,
            "kill_switch_reason": _safe_text(system.kill.reason) if system.kill.reason else None,
        },
        "risk": risk,
        "positions": {"available": position_view is not None, "as_of_utc": _iso(observed["positions_at"]),
                      "age_seconds": positions_age,
                      "items": position_view or []},
        "pending_orders": {"available": order_view is not None, "as_of_utc": _iso(observed["pending_orders_at"]),
                           "age_seconds": orders_age,
                           "items": order_view or []},
        "plans": [_plan(plan, plans.hub.zones.get) for plan in plans.store.all()],
        "market": markets,
        "market_status": series_status,
        "structure": structure,
        "news": news,
        "journal": {
            "database_path": str(config.journal.database_path),
            "magic": config.execution.magic_number,
            "latest_reconciliation": ({
                key: latest_reconciliation.get(key)
                for key in ("completed_at", "history_start", "history_end", "deals_seen",
                            "orders_seen", "positions_seen", "status", "magic")
            } if latest_reconciliation else None),
            "read_status": "error" if journal_read_error else "ok",
            "read_error": journal_read_error,
            "recent_events": recent_events,
            "recent_structure_events": recent_structure_events,
        },
    }


class AsyncSnapshotPublisher:
    """Single-slot background writer; bot state publication never waits for disk I/O."""

    def __init__(self, path: Path = DEFAULT_SNAPSHOT_PATH) -> None:
        self.path = Path(path)
        self._ownership_lock = _SnapshotOwnershipLock(self.path)
        self._queue: queue.Queue[Optional[dict[str, Any]]] = queue.Queue(maxsize=1)
        self._closed = False
        self._sequence = 0
        self._lock = threading.Lock()
        self._replace_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="dashboard-snapshot-writer", daemon=True)
        try:
            self._thread.start()
        except Exception:
            self._ownership_lock.release()
            raise

    def publish(self, snapshot: dict[str, Any]) -> None:
        """Queue the newest snapshot without waiting for the writer or broker."""
        with self._lock:
            if self._closed:
                return
            self._sequence += 1
            snapshot["sequence"] = self._sequence
        try:
            self._queue.put_nowait(snapshot)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(snapshot)
            except queue.Full:
                log.warning("Dashboard snapshot queue remained full; latest snapshot was skipped")

    def close(self, timeout: float = 2.0) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._thread.join(timeout=max(0.0, timeout))
        if self._thread.is_alive():
            log.warning("Dashboard snapshot writer did not stop before shutdown timeout")

    def _run(self) -> None:
        try:
            while True:
                try:
                    snapshot = self._queue.get(timeout=0.1)
                except queue.Empty:
                    with self._lock:
                        if self._closed:
                            return
                    continue
                try:
                    if snapshot is None:
                        return
                    self._write(snapshot)
                except (OSError, TypeError, ValueError) as exc:
                    log.exception(
                        "Dashboard snapshot write failed (%s) operation=%s source=%s "
                        "destination=%s errno=%r winerror=%r; trading safeguards remain active",
                        type(exc).__name__,
                        getattr(exc, "snapshot_operation", "unknown"),
                        getattr(exc, "snapshot_source", self.path),
                        getattr(exc, "snapshot_destination", self.path),
                        getattr(exc, "errno", None),
                        getattr(exc, "winerror", None),
                    )
                finally:
                    self._queue.task_done()
        finally:
            self._ownership_lock.release()

    def _write(self, snapshot: dict[str, Any]) -> None:
        temporary: Optional[Path] = None
        operation = "create_parent_directory"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            operation = "create_unique_temporary_file"
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                operation = "serialize_snapshot"
                json.dump(snapshot, handle, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
                operation = "flush_temporary_file"
                handle.flush()
                operation = "fsync_temporary_file"
                os.fsync(handle.fileno())
            operation = "atomic_replace"
            with self._replace_lock:
                for attempt in range(SNAPSHOT_REPLACE_ATTEMPTS):
                    try:
                        os.replace(temporary, self.path)
                        break
                    except PermissionError:
                        if attempt + 1 == SNAPSHOT_REPLACE_ATTEMPTS:
                            raise
                        time.sleep(SNAPSHOT_REPLACE_RETRY_SECONDS)
            temporary = None
        except (OSError, TypeError, ValueError) as exc:
            exc.snapshot_operation = operation
            if temporary is not None:
                source_path = str(temporary)
            elif operation == "create_unique_temporary_file":
                source_path = str(self.path.parent / f".{self.path.name}.*.tmp")
            elif operation == "create_parent_directory":
                source_path = str(self.path.parent)
            else:
                source_path = str(self.path)
            exc.snapshot_source = source_path
            exc.snapshot_destination = str(self.path)
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    log.exception(
                        "Snapshot temporary-file cleanup failed operation=cleanup_temporary_file "
                        "source=%s destination=%s errno=%r winerror=%r",
                        temporary,
                        self.path,
                        cleanup_error.errno,
                        getattr(cleanup_error, "winerror", None),
                    )
            raise
