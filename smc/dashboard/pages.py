"""Streamlit page renderers for the local read-only trading dashboard."""

from __future__ import annotations

from collections import defaultdict
import json
from typing import Any, Callable

import pandas as pd
import streamlit as st

from smc.dashboard.charts import build_candlestick_chart
from smc.dashboard.data_access import (
    DEFAULT_ROWS,
    SNAPSHOT_STALE_AFTER_SECONDS,
    ReadOnlyJournal,
    load_snapshot,
)
from smc.dashboard.paths import DEFAULT_SNAPSHOT_PATH

REFRESH_INTERVALS_SECONDS = (1, 2, 5, 10, 30)


def _with_refresh(render: Callable[[dict[str, Any]], None]) -> None:
    interval = dashboard_refresh_seconds(st.session_state)

    @st.fragment(run_every=interval)
    def _refreshable() -> None:
        render(load_snapshot())

    _refreshable()


def dashboard_refresh_seconds(session_state: Any) -> float:
    value = session_state.get("dashboard_refresh_seconds", 5)
    try:
        interval = float(value)
    except (TypeError, ValueError):
        return 5.0
    return interval if interval in REFRESH_INTERVALS_SECONDS else 5.0


def _snapshot_freshness(snapshot: dict[str, Any]) -> str:
    generated = _utc_display(snapshot.get("generated_at_utc"))
    age = snapshot.get("snapshot_age_seconds")
    age_text = f"{float(age):.1f}s" if isinstance(age, (int, float)) else "Unavailable"
    status = str(snapshot.get("snapshot_status") or "unknown").replace("_", " ").title()
    return f"Snapshot published (UTC): {generated} · Age: {age_text} · {status}"


def _structure_bias_rows(structure: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for layer, bias_key, time_key in (
        ("External", "external_bias_by_timeframe", "external_bias_time_utc_by_timeframe"),
        ("Internal", "internal_bias_by_timeframe", "internal_bias_time_utc_by_timeframe"),
    ):
        biases = structure.get(bias_key)
        if not isinstance(biases, dict):
            continue
        timestamps = structure.get(time_key)
        if not isinstance(timestamps, dict):
            timestamps = {}
        rows.extend(
            {
                "layer": layer,
                "timeframe": timeframe,
                "bias": value.title() if isinstance(value, str) and value else "Not available",
                "as_of_utc": timestamps.get(timeframe),
            }
            for timeframe, value in biases.items()
        )
    return rows


def _layer_anchor_bias(structure: dict[str, Any], layer: str) -> tuple[Any, str]:
    anchor = structure.get(f"{layer}_anchor_timeframe")
    biases = structure.get(f"{layer}_bias_by_timeframe")
    if isinstance(biases, dict) and isinstance(anchor, str) and anchor:
        value = biases.get(anchor)
    else:
        value = structure.get(f"{layer}_bias")
    display = value.title() if isinstance(value, str) and value else "Not available"
    return anchor, display


def _notice(snapshot: dict[str, Any]) -> None:
    if not snapshot.get("available"):
        st.error(snapshot.get("reason", "Dashboard snapshot unavailable."), icon=":material/error:")
        st.caption("Start the bot in dry-run mode to publish current market and monitoring data.")
        return
    st.caption(_snapshot_freshness(snapshot))
    if snapshot.get("snapshot_status") == "stale":
        st.warning(
            f"Bot snapshot is stale ({snapshot.get('snapshot_age_seconds', 0):.0f}s old; "
            f"stale threshold {SNAPSHOT_STALE_AFTER_SECONDS}s). "
            "Values below are last-known state, not confirmed current state.",
            icon=":material/warning:",
        )
    mt5 = snapshot.get("mt5", {})
    if mt5.get("connection_status") != "recent_success":
        st.warning("MT5 connection is stale or unknown based on the bot's last successful market-data update.")


def _journal(snapshot: dict[str, Any]) -> dict[str, Any]:
    journal_info = snapshot.get("journal", {})
    if not journal_info.get("database_path"):
        return {"available": False, "reason": "Journal path is unavailable.", "scope": None}
    return ReadOnlyJournal(journal_info["database_path"]).read(
        journal_info.get("magic", 0), snapshot.get("generated_at_utc", ""), DEFAULT_ROWS
    )


def _metric_row(items: list[tuple[str, Any]]) -> None:
    for start in range(0, len(items), 2):
        row = items[start:start + 2]
        columns = st.columns(len(row))
        for column, (label, value) in zip(columns, row):
            with column:
                st.metric(label, value, border=True)


_COLUMN_LABELS = {
    "account_id": "Account ID",
    "age_seconds": "Data age (seconds)",
    "as_of_utc": "As of (UTC)",
    "balance": "Balance",
    "bias": "Bias",
    "close": "Close",
    "closed_at": "Closed (UTC)",
    "commission": "Commission",
    "completed_at": "Completed (UTC)",
    "correlation_id": "Correlation ID",
    "created_at": "Created (UTC)",
    "created_at_utc": "Created (UTC)",
    "deal_ticket": "Deal ticket",
    "deals_seen": "Deals seen",
    "direction": "Direction",
    "fee": "Fee",
    "event_type": "Event type",
    "equity": "Equity",
    "history_end": "History end (UTC)",
    "history_start": "History start (UTC)",
    "high": "High",
    "is_entry": "Entry deal",
    "is_exit": "Exit deal",
    "last_closed_candle_utc": "Last closed candle (UTC)",
    "last_error": "Latest retrieval error",
    "last_success_utc": "Last successful fetch (UTC)",
    "layer": "Layer",
    "low": "Low",
    "magic": "Magic number",
    "net": "Net result",
    "open": "Open",
    "opened_at": "Opened (UTC)",
    "order_ticket": "Order ticket",
    "occurred_at": "Occurred (UTC)",
    "price": "Price",
    "profit": "Profit",
    "plan_id": "Plan ID",
    "position_id": "Position ID",
    "positions_seen": "Positions seen",
    "reconciliation_as_of_utc": "Reconciled (UTC)",
    "real_volume": "Real volume",
    "row_limit": "Maximum rows",
    "swap": "Swap",
    "status": "Status",
    "symbol": "Symbol",
    "tick_volume": "Tick volume",
    "time_utc": "Time (UTC)",
    "timeframe": "Timeframe",
    "trade_allowed": "Trading allowed",
    "volume": "Volume",
    "volume_closed": "Volume closed",
    "volume_open": "Volume opened",
    "account_snapshot_age": "Account snapshot age",
    "balance_threshold": "Balance threshold",
    "consecutive_failures": "Consecutive failures",
    "history_start_utc": "History start (UTC)",
    "history_end_utc": "History end (UTC)",
    "closed_trades": "Closed trades",
    "net_result": "Net result",
    "wins": "Wins",
    "losses": "Losses",
    "pending_orders": "Pending orders",
    "observed_total": "Observed total",
    "configured_maximum": "Configured maximum",
    "remaining": "Remaining",
    "created_at_utc": "Created (UTC)",
    "updated_at": "Updated (UTC)",
    "updated_at_utc": "Updated (UTC)",
    "timestamp_utc": "Timestamp (UTC)",
    "category": "Category",
    "event": "Event",
    "message": "Message",
    "context": "Context",
}


def _display_label(name: str) -> str:
    if name in _COLUMN_LABELS:
        return _COLUMN_LABELS[name]
    return name.replace("_", " ").strip().title()


def _show_table(
    records: list[dict[str, Any]] | pd.DataFrame,
    *,
    alt: str,
    widths: dict[str, int | str] | None = None,
    row_height: int = 42,
) -> bool:
    frame = records.copy() if isinstance(records, pd.DataFrame) else pd.DataFrame(records)
    if frame.empty:
        return False
    original_names = list(frame.columns)
    frame = frame.rename(columns={name: _display_label(str(name)) for name in original_names})
    column_config = {}
    for original_name in original_names:
        display_name = _display_label(str(original_name))
        width = (widths or {}).get(str(original_name), 160)
        column_config[display_name] = st.column_config.Column(display_name, width=width)
        if str(original_name).endswith("_utc") or str(original_name) in {
            "closed_at", "completed_at", "created_at", "history_end", "history_start",
            "occurred_at", "opened_at", "updated_at",
        }:
            frame[display_name] = frame[display_name].map(_utc_display)
    st.dataframe(
        frame,
        width="stretch",
        hide_index=True,
        alt=alt,
        column_config=column_config,
        row_height=row_height,
    )
    return True


def _closed_trades(journal: dict[str, Any]) -> list[dict[str, Any]]:
    if not journal.get("available"):
        return []
    return [trade for trade in journal.get("trades", []) if trade.get("status") == "closed"]


def _price(value: Any) -> str:
    try:
        return f"{float(value):,.6g}" if value is not None else "Unavailable"
    except (TypeError, ValueError):
        return "Unavailable"


def _utc_display(value: Any) -> str:
    if not value:
        return "Unavailable"
    try:
        stamp = pd.Timestamp(value)
        stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
        return stamp.strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return "Unavailable"


def _event_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return "; ".join(
            f"{str(key).replace('_', ' ').title()}: {_event_text(item)}"
            for key, item in value.items()
            if item is not None
        )
    if isinstance(value, (list, tuple)):
        return "; ".join(text for item in value if (text := _event_text(item)))
    return str(value).strip()


def _event_payload_dict(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = {"note": payload}
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return payload
    return {"note": payload}


def _recent_event_rows(events: Any) -> list[dict[str, str]]:
    if not isinstance(events, (list, tuple)):
        return []
    rows = []
    context_keys = (
        ("component", "Component"), ("provider", "Provider"), ("symbol", "Symbol"),
        ("plan_id", "Plan"), ("order_ticket", "Order"), ("correlation_id", "Correlation"),
        ("event_id", "Event ID"), ("action", "Action"), ("target", "Target"),
        ("timeframe", "Timeframe"), ("zone_id", "Zone"), ("new_zone_id", "Replacement zone"),
        ("zone_kind", "Zone type"), ("entry_model", "Entry model"), ("entry_zone", "Entry zone"),
        ("stop_loss", "Stop-loss"), ("targets", "Targets"),
        ("broker_status", "Broker status"), ("retcode", "Return code"),
        ("retryable", "Retryable"), ("verified", "Verified"),
        ("consecutive_failures", "Consecutive failures"), ("age_hours", "Calendar age"),
        ("order_kind", "Order type"), ("volume", "Volume"), ("price", "Price"),
        ("last_success_utc", "Last success (UTC)"),
    )
    for event in events:
        if not isinstance(event, dict):
            continue
        payload = _event_payload_dict(event)
        status_value = next(
            (payload[key] for key in ("status", "outcome", "broker_status")
             if payload.get(key) is not None and _event_text(payload[key])),
            None,
        )
        if status_value is None and isinstance(payload.get("succeeded"), bool):
            status_value = "Succeeded" if payload["succeeded"] else "Failed"
        if status_value is None and isinstance(payload.get("retryable"), bool):
            status_value = "Retryable" if payload["retryable"] else "Not retryable"
        status = _event_text(status_value).replace("_", " ").title() if status_value is not None else "Not recorded"

        message_parts = []
        status_normalized = " ".join(status.casefold().split())
        for key in ("error", "reason", "reasons", "note", "broker_comment"):
            text = _event_text(payload.get(key))
            normalized = " ".join(text.casefold().split())
            if text and normalized != status_normalized and normalized not in {
                " ".join(item.casefold().split()) for item in message_parts
            }:
                message_parts.append(text)

        event_type = _event_text(event.get("event_type"))
        category = event_type.split("_", 1)[0].replace("-", " ").title() if event_type else "Not recorded"
        message = " · ".join(message_parts) if message_parts else "Not recorded"
        if event_type == "execution_result" and str(payload.get("outcome", "")).casefold() == "dry_run":
            event_label = "Dry-run execution evaluation"
            status = "No order sent"
            dry_run_note = "Dry-run evaluation only; not an order, fill, or trade."
            if dry_run_note.casefold() not in message.casefold():
                message = f"{message} · {dry_run_note}" if message != "Not recorded" else dry_run_note
        else:
            event_label = event_type.replace("_", " ").replace("-", " ").title() if event_type else "Not recorded"
        context_parts = []
        for key, label in context_keys:
            value = event.get(key) if key in event else payload.get(key)
            text = _event_text(value)
            if text:
                context_parts.append(f"{label}: {text}")
        rows.append({
            "timestamp_utc": _utc_display(event.get("occurred_at_utc") or event.get("occurred_at")),
            "category": category,
            "event": event_label,
            "status": status,
            "message": message,
            "context": " · ".join(context_parts) if context_parts else "Not recorded",
        })
    return rows


def _plan_rows(plans: list[dict[str, Any]]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    summary = []
    details = []
    for plan in plans:
        model = str(plan.get("entry_model") or "Unavailable").replace("_", " ").title()
        direction = str(plan.get("direction") or "Unavailable").replace("_", " ").title()
        status = str(plan.get("status") or "Unavailable").replace("_", " ").title()
        source = plan.get("source_timeframes") or {}
        source_text = (
            f"External: {' / '.join(source.get('external') or []) or 'Unavailable'}; "
            f"internal: {' / '.join(source.get('internal') or []) or 'Unavailable'}; "
            f"setup: {source.get('setup') or 'Unavailable'}"
        )
        zone = plan.get("entry_zone") or {}
        entry = (
            f"{_price(zone.get('bottom'))} – {_price(zone.get('top'))}"
            if zone.get("bottom") is not None and zone.get("top") is not None
            else "Unavailable"
        )
        invalidation = plan.get("invalidation") or {}
        targets = plan.get("targets") or []
        rr = plan.get("rr_estimate")
        source_zones = plan.get("source_zones") or []
        source_zone_text = " · ".join(
            f"{item.get('kind') or 'Zone'} {item.get('id') or 'Not recorded'} "
            f"({item.get('symbol') or plan.get('symbol') or 'Symbol not recorded'}, "
            f"{str(item.get('bias') or 'Direction not recorded').title()} "
            f"{item.get('timeframe') or 'Timeframe not recorded'}, "
            f"{_price(item.get('bottom'))}–{_price(item.get('top'))}, "
            f"{str(item.get('status') or 'Status not recorded').title()}"
            f"{' (last known)' if item.get('record_freshness') == 'last_known' else ''}"
            f"{' (recovered from legacy snapshot)' if item.get('record_freshness') == 'recovered' else ''})"
            for item in source_zones
        ) or "Unavailable"
        referenced_zone_ids = plan.get("source_zone_ids") or {}
        known_zone_ids = {item.get("id") for item in source_zones}
        missing_zone_ids = list(dict.fromkeys(
            zone_id for zone_id in referenced_zone_ids.values()
            if zone_id and zone_id not in known_zone_ids
        ))
        if missing_zone_ids:
            missing_text = "Missing source record(s): " + ", ".join(map(str, missing_zone_ids))
            source_zone_text = (
                f"{source_zone_text} · {missing_text}"
                if source_zone_text != "Unavailable"
                else missing_text
            )
        if source_zone_text == "Unavailable":
            source_zone_text = (
                "Source zone not resolved: " + str(plan.get("source_zone_unavailable_reason"))
                if plan.get("source_zone_unavailable_reason")
                else "Source zone ID not recorded for this plan"
            )
        basis = plan.get("entry_range_basis_label")
        if not basis:
            basis = {
                "order_block": "Order block",
                "fair_value_gap": "Fair value gap",
                "ob_fvg_confluence": "OB + FVG confluence",
                "sweep_mss_order_block": "Sweep + MSS retrace at order block",
                "sweep_mss_fvg": "Sweep + MSS retrace at FVG",
                "sweep_mss_retracement": "Sweep + MSS leg retracement",
                "flipped_zone_retest": "Flipped-zone retest",
            }.get(plan.get("entry_range_basis"), "Entry range basis not recorded")
            if basis in {"Order block", "Fair value gap"}:
                basis = f"{basis} ({source.get('setup') or 'setup timeframe not recorded'})"
            if plan.get("entry_range_basis") is None:
                model_basis = {"OB": "Order block", "FVG": "Fair value gap"}.get(plan.get("entry_model"))
                if model_basis:
                    basis = (
                        f"{model_basis} ({source.get('setup') or 'setup timeframe not recorded'}; "
                        "basis inferred from the structured entry model)"
                    )

        relationship = plan.get("source_relationship") or {}
        relationship_status = relationship.get("status")
        if relationship_status == "verified":
            relationship_text = str(relationship.get("description"))
            if relationship.get("origin_event_id"):
                relationship_text += f" · origin event {relationship['origin_event_id']}"
        elif plan.get("entry_range_basis") == "ob_fvg_confluence":
            relationship_text = str(
                relationship.get("description")
                or "OB/FVG relationship is not verifiable from the available source records"
            )
        else:
            relationship_text = "Not applicable or not recorded"

        market_context = plan.get("market_context")
        context_parts = []
        if isinstance(market_context, dict):
            conditional = market_context.get("conditional_scenario")
            if conditional:
                context_parts.append(str(conditional))
            for item in market_context.get("source_zones", []):
                if isinstance(item, dict):
                    context_parts.append(
                        f"Source {item.get('zone_id', 'zone')} {item.get('timeframe', 'timeframe unavailable')}: "
                        f"{item.get('state', 'state unavailable')} — {item.get('evidence', 'evidence unavailable')}"
                    )
            for item in market_context.get("opposing_zones", []):
                if isinstance(item, dict):
                    context_parts.append(
                        f"Opposing {item.get('zone_id', 'zone')} "
                        f"{item.get('timeframe', 'timeframe unavailable')}: "
                        f"{item.get('state', 'state unavailable')}; "
                        f"{item.get('implication', 'context only')}"
                    )
            if market_context.get("assessment"):
                context_parts.append(str(market_context["assessment"]))
        market_context_text = " · ".join(context_parts) or "Not recorded in this snapshot"

        quality = plan.get("quality") or {}
        if quality.get("counter_trend_internal") is True:
            countertrend_text = (
                f"Counter-trend internal {direction.lower()} vs "
                f"{str(plan.get('external_bias') or 'unknown').lower()} external bias"
            )
        elif quality.get("counter_trend_internal") is False:
            countertrend_text = "Not flagged by the plan"
        else:
            countertrend_text = "Counter-trend context not recorded"
        summary.append({
            "Plan": str(plan.get("plan_id") or "Unavailable"),
            "Symbol": str(plan.get("symbol") or "Unavailable"),
            "Direction": direction,
            "Entry model": model,
            "Status": status,
            "Updated (UTC)": _utc_display(plan.get("updated_at_utc")),
        })
        details.append({
            "Plan": str(plan.get("plan_id") or "Unavailable"),
            "Entry zone": entry,
            "Invalidation": _price(invalidation.get("price")),
            "Invalidation basis": str(invalidation.get("basis") or "Not recorded"),
            "Targets": " / ".join(_price(target) for target in targets) or "Unavailable",
            "Risk / reward": f"{float(rr):.2f}R" if isinstance(rr, (int, float)) else "Unavailable",
            "Source timeframes": source_text,
            "Entry range basis": basis,
            "Source zones": source_zone_text,
            "Source relationship": relationship_text,
            "Market context": market_context_text,
            "Counter-trend context": countertrend_text,
            "Created (UTC)": _utc_display(plan.get("created_at_utc")),
            "Reason": str(plan.get("reason") or "Unavailable"),
        })
    return summary, details


def _conditional_scenario_empty_state(
    evaluation: Any, structure: Optional[dict[str, Any]] = None,
) -> str:
    if not isinstance(evaluation, dict):
        if structure is None:
            return "Scenario evaluation details are unavailable in this snapshot."
        zones = structure.get("zones") or []
        live_count = sum(
            1 for zone in zones
            if isinstance(zone, dict)
            and zone.get("status") in {"active", "mitigated"}
            and zone.get("bias") in {"bullish", "bearish"}
        )
        if live_count == 0:
            return "No live supply or demand zones are available for scenario evaluation."
        if structure.get("price") is None:
            return (
                f"Insufficient market context: {live_count} live zone(s) are present, "
                "but current price is unavailable."
            )
        return (
            f"No scenarios were published for {live_count} live zone(s). This snapshot lacks "
            "evaluation details to distinguish age/distance exclusions from missing timeframe ATR."
        )

    status = evaluation.get("status")
    live_count = evaluation.get("live_zone_count")
    if status == "no_live_zones":
        return "No live supply or demand zones are available for scenario evaluation."
    if status == "insufficient_context":
        missing: list[str] = []
        if evaluation.get("missing_price"):
            missing.append("current price")
        unknown_age = evaluation.get("unknown_age_zone_count", 0)
        missing_atr = evaluation.get("missing_atr_zone_count", 0)
        if unknown_age:
            missing.append(f"timeframe age for {unknown_age} zone(s)")
        if missing_atr:
            missing.append(f"ATR for {missing_atr} zone(s)")
        reason = ", ".join(missing) or str(evaluation.get("reason") or "required market context")
        count = f" ({live_count} live zone(s) found)." if isinstance(live_count, int) else ""
        return f"Insufficient market context to evaluate nearby scenarios: {reason}.{count}"
    if status == "no_qualifying_nearby_zones":
        count = f"{live_count} live zone(s) checked" if isinstance(live_count, int) else "Live zones checked"
        threshold = evaluation.get("approach_atr_mult")
        distance = (
            f" within {threshold:g} ATR"
            if isinstance(threshold, (int, float))
            else ""
        )
        outside = evaluation.get("outside_distance_zone_count")
        outside_text = (
            f"; {outside} outside the approach distance"
            if isinstance(outside, int) and outside
            else ""
        )
        age_eligible = evaluation.get("age_eligible_zone_count")
        age_text = (
            f"; {age_eligible} passed the age check"
            if isinstance(age_eligible, int)
            else ""
        )
        return f"No qualifying nearby zones: {count}{distance}{age_text}{outside_text}."
    return str(evaluation.get("reason") or "No conditional scenarios are currently available.")


def _conditional_scenario_summary(evaluation: Any, scenario_count: int) -> Optional[str]:
    if not isinstance(evaluation, dict):
        return None
    if evaluation.get("status") != "scenarios_available":
        return str(evaluation.get("reason") or "Scenario evaluation details are unavailable.")

    live_count = evaluation.get("live_zone_count")
    age_eligible = evaluation.get("age_eligible_zone_count")
    outside_distance = evaluation.get("outside_distance_zone_count")
    missing_atr = evaluation.get("missing_atr_zone_count")
    threshold = evaluation.get("approach_atr_mult")
    counts = (live_count, age_eligible, outside_distance, missing_atr)
    if not all(isinstance(value, (int, float)) for value in counts):
        return str(evaluation.get("reason") or "Scenario evaluation details are incomplete.")

    distance_text = (
        f"{outside_distance} outside {threshold:g} ATR"
        if isinstance(threshold, (int, float))
        else f"{outside_distance} outside the approach distance"
    )
    reason = evaluation.get("reason")
    reason_text = f" {reason}" if reason else ""
    return (
        f"{scenario_count} scenario(s) qualify from {live_count} live zone(s); "
        f"{age_eligible} passed the age check, {distance_text}, "
        f"{missing_atr} missing timeframe ATR.{reason_text}"
    )


def _recover_legacy_plan_provenance(
    plan: dict[str, Any], structure_by_symbol: dict[str, Any]
) -> dict[str, Any]:
    """Recover standalone source IDs only from a unique exact zone match in old snapshots."""
    source_ids = plan.get("source_zone_ids")
    if (isinstance(source_ids, dict) and any(source_ids.values())) or plan.get("source_zones"):
        return plan

    model = plan.get("entry_model")
    zone_type = {"OB": "OB", "FVG": "FVG"}.get(model)
    if zone_type is None:
        return plan

    source = plan.get("source_timeframes") or {}
    setup_timeframe = source.get("setup")
    entry_zone = plan.get("entry_zone") or {}
    created_at = pd.to_datetime(plan.get("created_at_utc"), utc=True, errors="coerce")
    symbol = plan.get("symbol")
    direction = plan.get("direction")
    expected_bias = {"long": "bullish", "short": "bearish"}.get(direction)
    structure = structure_by_symbol.get(symbol, {})
    zones = structure.get("zones", []) if isinstance(structure, dict) else []
    exact_matches: dict[str, dict[str, Any]] = {}
    if (setup_timeframe and expected_bias and not pd.isna(created_at)
            and entry_zone.get("top") is not None and entry_zone.get("bottom") is not None):
        for zone in zones:
            zone_created = pd.to_datetime(zone.get("created_utc"), utc=True, errors="coerce")
            if (
                zone.get("kind") == zone_type
                and zone.get("symbol") == symbol
                and zone.get("timeframe") == setup_timeframe
                and zone.get("bias") == expected_bias
                and zone.get("top") == entry_zone.get("top")
                and zone.get("bottom") == entry_zone.get("bottom")
                and not pd.isna(zone_created)
                and zone_created <= created_at
                and zone.get("id")
            ):
                exact_matches[str(zone["id"])] = zone

    enriched = dict(plan)
    basis = "order_block" if model == "OB" else "fair_value_gap"
    enriched["entry_range_basis"] = basis
    type_label = "Order block" if model == "OB" else "Fair value gap"
    enriched["entry_range_basis_label"] = (
        f"{type_label} ({setup_timeframe})" if setup_timeframe
        else f"{type_label} (setup timeframe not recorded)"
    )
    if len(exact_matches) == 1:
        record = dict(next(iter(exact_matches.values())))
        record["record_freshness"] = "recovered"
        enriched["source_zone_ids"] = {
            "order_block": record["id"] if zone_type == "OB" else None,
            "fvg": record["id"] if zone_type == "FVG" else None,
            "flipped": None,
        }
        enriched["source_zones"] = [record]
        enriched["source_zone_resolution"] = "unique_exact_legacy_match"
    else:
        enriched["source_zone_ids"] = {
            "order_block": None, "fvg": None, "flipped": None,
        }
        enriched["source_zones"] = []
        if len(exact_matches) > 1:
            why = "multiple zones have the same exact symbol, timeframe, direction, bounds, and prior timestamp"
        else:
            why = "no prior zone has the exact symbol, timeframe, direction, and entry bounds"
        enriched["source_zone_unavailable_reason"] = (
            f"Legacy snapshot omitted the source ID; {why}."
        )
    return enriched


def _risk_limit_rows(limits: dict[str, Any], currency: str = "") -> list[dict[str, str]]:
    rows = []
    unavailable = "Not available in current snapshot"

    def add(key: str, label: str, formatter: Callable[[Any], str], meaning: str) -> None:
        value = limits.get(key)
        try:
            rendered = formatter(value) if value is not None else unavailable
        except (TypeError, ValueError):
            rendered = unavailable
        rows.append({"Configured control": label, "Limit": rendered, "Meaning": meaning})

    add("balance_threshold", "Risk-tier balance threshold",
        lambda value: f"{float(value):,.2f} {currency or 'account units'}",
        "Balance where the target-risk tier changes.")
    add("risk_pct_below_threshold", "Target risk below threshold",
        lambda value: f"{float(value):.2f}%",
        "Sizing target below the balance threshold.")
    add("risk_pct_at_or_above_threshold", "Target risk at/above threshold",
        lambda value: f"{float(value):.2f}%",
        "Sizing target at or above the balance threshold.")
    add("hard_max_risk_pct", "Hard per-trade risk cap",
        lambda value: f"{float(value):.2f}%",
        "Absolute per-trade risk ceiling.")
    add("max_daily_drawdown_pct", "Daily drawdown limit",
        lambda value: f"{float(value):.2f}%",
        "Blocks new entries at the limit.")
    for key, label, meaning in (
        ("max_consecutive_losses", "Maximum consecutive losses", "Loss streak limit."),
        ("max_trades_per_day", "Maximum trades per day", "Daily entry count."),
        ("max_open_trades_total", "Maximum simultaneous bot trades", "Positions plus pending orders."),
        ("max_open_trades_per_direction_per_symbol", "Maximum per direction and symbol",
         "Per-symbol, per-direction exposure."),
    ):
        add(key, label, lambda value: f"{int(value)}", meaning)
    return rows


def _session_rows(sessions: dict[str, Any]) -> list[dict[str, str]]:
    unavailable = "Not available in current snapshot"
    timezone_name = sessions.get("cutoff_timezone")
    windows = sessions.get("windows") or []
    rows = [
        {
            "Schedule": f"{item.get('name') or 'Trading'} session",
            "Configured value": (
                f"{item.get('timezone')}: {item.get('start')}–{item.get('end')} local time"
                if item.get("timezone") and item.get("start") and item.get("end")
                else unavailable
            ),
        }
        for item in windows
    ]
    if not rows:
        rows.append({"Schedule": "Allowed sessions", "Configured value": unavailable})
    for key, label in (
        ("trading_day_start", "Trading day starts"),
        ("no_new_trades_after", "No new entries after"),
        ("close_all_risk_by", "Configured close-risk cutoff"),
    ):
        value = sessions.get(key)
        if value is None:
            rendered = unavailable
        elif timezone_name:
            rendered = f"{value} ({timezone_name})"
        else:
            rendered = f"{value} (time zone not available in current snapshot)"
        rows.append({
            "Schedule": label,
            "Configured value": rendered,
        })
    rows.append({
        "Schedule": "Crypto exempt from cutoffs",
        "Configured value": (
            "Yes" if sessions.get("crypto_exempt_from_cutoffs") is True
            else ("No" if sessions.get("crypto_exempt_from_cutoffs") is False else unavailable)
        ),
    })
    return rows


def _observed_exposure_rows(
    positions: list[dict[str, Any]], orders: list[dict[str, Any]], per_side_limit: Any
) -> list[dict[str, Any]]:
    counts: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {"Positions": 0, "Pending orders": 0})
    for kind, records in (("Positions", positions), ("Pending orders", orders)):
        for record in records:
            symbol = str(record.get("symbol") or "Unavailable")
            direction = str(record.get("direction") or "Unavailable").title()
            counts[(symbol, direction)][kind] += 1
    rows = []
    for (symbol, direction), values in sorted(counts.items()):
        total = values["Positions"] + values["Pending orders"]
        limit = int(per_side_limit) if per_side_limit is not None else None
        rows.append({
            "Symbol": symbol,
            "Direction": direction,
            "Positions": values["Positions"],
            "Pending orders": values["Pending orders"],
            "Observed total": total,
            "Configured maximum": limit if limit is not None else "Unavailable",
            "Remaining": max(0, limit - total) if limit is not None else "Unavailable",
        })
    return rows


def render_overview(snapshot: dict[str, Any]) -> None:
    _notice(snapshot)
    if not snapshot.get("available"):
        return
    account = snapshot.get("account") or {}
    execution = snapshot.get("execution", {})
    risk = snapshot.get("risk", {})
    positions = snapshot.get("positions", {})
    orders = snapshot.get("pending_orders", {})
    journal = _journal(snapshot)
    closed = _closed_trades(journal)
    pnl = sum(float(trade.get("net") or 0) for trade in closed)
    _metric_row([
        ("Account", f"{account.get('environment', 'unknown').upper()} {account.get('currency', '')}".strip()),
        ("Equity", f"{account['equity']:,.2f} {account.get('currency', '')}" if account.get("equity") is not None else "Unavailable"),
    ])
    _metric_row([
        ("Recent closed net (≤100)",
         f"{pnl:,.2f} {account.get('currency', '')}" if closed else
         ("No closed trades" if journal.get("available") else "Unavailable")),
        ("MT5 connection", "Recent success" if snapshot.get("mt5", {}).get("connection_status") == "recent_success"
         else "Stale / unknown"),
    ])
    market_updates = [
        row.get("last_success_utc") for row in snapshot.get("market_status", [])
        if row.get("last_success_utc")
    ]
    if market_updates:
        st.caption(f"Latest market-data update (UTC): {_utc_display(max(market_updates))}")
    _metric_row([
        ("Execution", "LIVE" if execution.get("live_enabled") is True
         else ("DRY-RUN" if execution.get("live_enabled") is False else "Unknown")),
        ("Kill switch", "ENGAGED" if execution.get("kill_switch_engaged") is True
         else ("Clear" if execution.get("kill_switch_engaged") is False else "Unknown")),
    ])
    _metric_row([
        ("Open positions", len(positions.get("items", [])) if positions.get("available") else "Unknown"),
        ("Pending orders", len(orders.get("items", [])) if orders.get("available") else "Unknown"),
    ])
    if not risk.get("available"):
        st.info(
            risk.get("unavailable_reason")
            or "The bot has no successful exposure snapshot cached. The dashboard will not initiate broker history reads."
        )
    else:
        drawdown = risk.get("total_drawdown_pct")
        drawdown_limit = risk.get("limits", {}).get("max_daily_drawdown_pct")
        trade_count = risk.get("daily_trade_count")
        loss_streak = risk.get("consecutive_losses")
        st.caption(
            f"Daily drawdown {f'{drawdown:.2f}%' if drawdown is not None else 'Unavailable'} "
            f"(limit {f'{drawdown_limit:.2f}%' if drawdown_limit is not None else 'Unavailable'}) · "
            f"{trade_count if trade_count is not None else 'Unavailable'} trades today · "
            f"{loss_streak if loss_streak is not None else 'Unavailable'} consecutive losses"
        )
    st.subheader("Bot-owned positions")
    if positions.get("age_seconds") is not None and positions["age_seconds"] > 15:
        st.warning(f"Position observation is stale ({positions['age_seconds']:.0f}s old).")
    if not _show_table(
        positions.get("items", []),
        alt="Current bot-owned open positions",
        widths={"ticket": 120, "symbol": 110, "opened_at_utc": 190, "stop_loss": 120, "take_profit": 120},
    ):
        st.info("No bot-owned open-position records are available in the current snapshot.")
    stale = snapshot.get("snapshot_status") == "stale"
    st.subheader(
        "Last-known plans (stale snapshot; activity is not confirmed)"
        if stale else "Active plans"
    )
    live = [plan for plan in snapshot.get("plans", [])
            if plan.get("status") in {"planned", "active", "ready_to_execute"}]
    structure_by_symbol = snapshot.get("structure") or {}
    live = [_recover_legacy_plan_provenance(plan, structure_by_symbol) for plan in live]
    plan_summary, plan_details = _plan_rows(live)
    if stale:
        for summary in plan_summary:
            summary["Status"] = f"Last known · {summary['Status']}"
    if not plan_summary:
        st.info(
            "No active plans are present in the current in-memory plan store."
            if not stale else "The stale snapshot contains no last-known active plans."
        )
    for summary, details in zip(plan_summary, plan_details):
        with st.container(border=True):
            st.markdown(
                f"**{summary['Symbol']} · {summary['Direction']} · {summary['Entry model']}**"
            )
            st.caption(
                f"Plan {summary['Plan']} · {summary['Status']} · updated {summary['Updated (UTC)']}"
            )
            with st.expander(
                "Plan levels, source timeframes, and lifecycle details",
                key=f"plan_details_{summary['Plan']}",
                on_change="rerun",
            ):
                detail_rows = [
                    {"Detail": label, "Value": value}
                    for label, value in details.items()
                    if label != "Plan"
                ]
                st.table(pd.DataFrame(detail_rows), width="stretch", hide_index=True,
                         alt="Expanded levels, source timeframes, and lifecycle details for this plan")
    if journal.get("available") and not closed:
        st.caption("No closed trades are present in the bounded journal sample; this is not a verified zero-return period.")
    if execution.get("kill_switch_engaged") is True:
        st.error(f"Kill switch engaged: {execution.get('kill_switch_reason') or 'reason unavailable'}")
    elif execution.get("kill_switch_engaged") is None:
        st.warning("Kill-switch state unavailable.")
    if not journal.get("available"):
        st.warning(journal.get("reason", "Journal history unavailable."))


def render_market(snapshot: dict[str, Any]) -> None:
    _notice(snapshot)
    if not snapshot.get("available"):
        return
    market = snapshot.get("market", {})
    symbols = list(market)
    if not symbols:
        st.warning("No initialized market-data series are available.")
        return
    controls = st.columns(2)
    with controls[0]:
        symbol = st.selectbox("Symbol", symbols, key="market_symbol")
    series = market.get(symbol, {})
    timeframes = list(series)
    if not timeframes:
        st.warning(f"No market-data timeframes are available for {symbol}.")
        return
    with controls[1]:
        timeframe = st.selectbox(
            "Timeframe",
            timeframes,
            index=min(timeframes.index("M15"), len(timeframes) - 1) if "M15" in timeframes else 0,
            key="market_timeframe",
        )
    selected = series[timeframe]
    age = selected.get("age_seconds")
    if selected.get("stale"):
        st.warning(f"{symbol} {timeframe} data is stale or unavailable "
                   f"(last fetch {age:.0f}s ago)." if age is not None else
                   f"{symbol} {timeframe} data is stale or unavailable.")
    if selected.get("last_error"):
        st.error(f"Latest data retrieval failed: {selected['last_error']}")
    if not selected.get("candles"):
        st.info("No closed candles are available. The chart will appear after the bot publishes market data.")
        return
    chart = build_candlestick_chart(
        selected["candles"],
        selected.get("timeframe_seconds", 60),
        view_revision=f"{symbol}_{timeframe}",
    )
    if chart is None:
        st.info("Candles are empty or invalid; no chart can be rendered.")
        return
    st.plotly_chart(
        chart,
        width="stretch",
        key=f"market_chart_{symbol}_{timeframe}",
        config={
            "displaylogo": False,
            "scrollZoom": True,
            "responsive": True,
            "modeBarButtonsToAdd": ["zoomIn2d", "zoomOut2d"],
            "modeBarButtonsToRemove": ["select2d", "lasso2d"],
        },
        alt=f"{symbol} {timeframe} closed-candle market chart",
    )
    st.caption("Scroll or use chart controls to zoom · drag to pan · use **Reset axes** in the chart toolbar to restore the full range.")
    st.caption(
        f"Market data: {'Stale' if selected.get('stale') else 'Fresh'} · "
        f"Last closed candle: {_utc_display(selected.get('last_closed_candle_utc'))} · "
        f"Snapshot age: {snapshot.get('snapshot_age_seconds', 0):.1f}s"
    )
    structure = snapshot.get("structure", {}).get(symbol)
    if structure:
        external_anchor, external_bias = _layer_anchor_bias(structure, "external")
        internal_anchor, internal_bias = _layer_anchor_bias(structure, "internal")
        _metric_row([
            (f"External bias ({external_anchor} anchor)" if external_anchor else "External bias (anchor unavailable)",
             external_bias),
            (f"Internal bias ({internal_anchor} anchor)" if internal_anchor else "Internal bias (anchor unavailable)",
             internal_bias),
            ("Live zones", len(structure.get("zones", []))),
            ("Active liquidity", len(structure.get("liquidity", []))),
        ])
        with st.expander(
            "Structure levels and zones",
            key="market_structure_levels",
            on_change="rerun",
        ):
            st.subheader("Bias by structure timeframe")
            bias_rows = _structure_bias_rows(structure)
            if not _show_table(
                bias_rows,
                alt="External and internal structure bias by timeframe with source candle time",
                widths={"layer": 110, "timeframe": 110, "bias": 120, "as_of_utc": 190},
            ):
                st.info("Per-timeframe bias fields are not available in this snapshot.")
            st.subheader("Order blocks and fair value gaps")
            if not _show_table(
                structure.get("zones", []),
                alt="Current order block and fair value gap zones",
                widths={"id": 170, "created_utc": 190},
            ):
                st.info("No structure zones are present in the current snapshot.")
            st.subheader("Conditional zone scenarios")
            scenario_rows = structure.get("conditional_scenarios", [])
            scenario_summary = _conditional_scenario_summary(
                structure.get("conditional_scenario_evaluation"), len(scenario_rows)
            )
            if scenario_rows and scenario_summary:
                st.caption(scenario_summary)
            if not _show_table(
                scenario_rows,
                alt="Nearby supply and demand context with unconfirmed reaction and breakout possibilities",
                widths={"zone_id": 170, "timeframe": 110},
            ):
                st.info(_conditional_scenario_empty_state(
                    structure.get("conditional_scenario_evaluation"), structure
                ))
            st.subheader("Liquidity levels")
            if not _show_table(
                structure.get("liquidity", []),
                alt="Current active liquidity levels",
                widths={"id": 170, "created_utc": 190},
            ):
                st.info("No active liquidity levels are present in the current snapshot.")


def render_performance(snapshot: dict[str, Any]) -> None:
    _notice(snapshot)
    if not snapshot.get("available"):
        return
    journal = _journal(snapshot)
    if not journal.get("available"):
        st.warning(journal.get("reason", "Account-scoped journal is unavailable."))
        return
    trades = journal.get("trades", [])
    closed = [trade for trade in trades if trade.get("status") == "closed"]
    unknown = sum(trade.get("status") == "unknown" for trade in trades)
    net = sum(float(trade.get("net") or 0) for trade in closed)
    winners = sum(float(trade.get("net") or 0) > 0 for trade in closed)
    currency = (snapshot.get("account") or {}).get("currency", "")
    _metric_row([
        ("Closed trades", len(closed)),
        ("Recent closed net (≤100)", f"{net:,.2f} {currency}".strip() if closed else "No closed trades"),
        ("Winners / losers", f"{winners} / {len(closed) - winners}" if closed else "No closed trades"),
        ("Unknown attribution", unknown),
    ])
    st.caption("Metrics cover the bounded latest 100 reconciled bot-owned trades. Open, partial, unknown, and truncated-history records are not represented as completed outcomes.")
    if not closed:
        st.info("No reconciled closed trades are present in this sample. This is not a verified zero-return period.")
    if not _show_table(
        trades,
        alt="Account- and magic-scoped reconciled trades",
        widths={"position_id": 135, "opened_at": 190, "closed_at": 190, "updated_at": 190},
    ):
        st.info("No account- and magic-scoped trade records are available in this sample.")
    risk = snapshot.get("risk", {})
    if risk.get("available"):
        drawdown = risk.get("total_drawdown_pct")
        st.metric("Current daily drawdown", f"{drawdown:.2f}%" if drawdown is not None else "Unavailable")
    st.info("Historical equity curves, expectancy, and R-multiples are not shown because validated time-series and attribution data are not available.")


def render_model_analysis(snapshot: dict[str, Any]) -> None:
    _notice(snapshot)
    if not snapshot.get("available"):
        return
    journal = _journal(snapshot)
    if not journal.get("available"):
        st.warning(journal.get("reason", "Account-scoped journal is unavailable."))
        return
    closed = _closed_trades(journal)
    st.subheader("By symbol and direction")
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for trade in closed:
        groups[(str(trade.get("symbol") or "unknown"), str(trade.get("direction") or "unknown"))].append(
            float(trade.get("net") or 0)
        )
    rows = [{"symbol": key[0], "direction": key[1], "closed_trades": len(values),
             "net_result": f"{sum(values):,.2f} {(snapshot.get('account') or {}).get('currency', '')}".strip(),
             "wins": sum(value > 0 for value in values),
             "losses": sum(value < 0 for value in values)} for key, values in sorted(groups.items())]
    if not _show_table(rows, alt="Closed bot trade outcomes by symbol and direction"):
        st.info("No symbol/direction outcome rows are available.")
    if not closed:
        st.info("No reconciled closed trades are available in the bounded sample.")
    st.info("Entry-model and trading-session performance are unavailable: reconciled trades do not have a reliable model/session attribution field.")


def render_execution(snapshot: dict[str, Any]) -> None:
    _notice(snapshot)
    if not snapshot.get("available"):
        return
    journal = _journal(snapshot)
    positions = snapshot.get("positions", {})
    orders = snapshot.get("pending_orders", {})
    execution_events = [
        event for event in snapshot.get("journal", {}).get("recent_events", [])
        if isinstance(event, dict) and str(event.get("event_type", "")).startswith(
            ("execution_", "risk_check_rejected", "order_", "position_", "fill_")
        )
    ]
    st.subheader("Recent execution evaluations and events")
    st.caption("Dry-run evaluations are not orders, fills, completed trades, or performance results.")
    execution_rows = _recent_event_rows(execution_events)
    if execution_rows:
        _show_table(
            execution_rows,
            alt="Recent execution evaluations and broker execution events",
            widths={"timestamp_utc": 190, "category": 110, "event": 210, "status": 150,
                    "message": 360, "context": 360},
        )
    else:
        st.info("No recent execution evaluations or execution events are available.")
    st.subheader("Current exposure")
    left, right = st.columns(2)
    with left:
        st.caption("Open positions")
        if positions.get("age_seconds") is not None and positions["age_seconds"] > 15:
            st.warning(f"Position observation is stale ({positions['age_seconds']:.0f}s old).")
        if not _show_table(
            positions.get("items", []),
            alt="Bot-owned open positions from cached bot observations",
            widths={"ticket": 120, "symbol": 110, "opened_at_utc": 190},
        ):
            st.info("No bot-owned open-position records are available.")
        if not positions.get("available"):
            st.warning("No successful position observation is available.")
    with right:
        st.caption("Pending orders")
        if orders.get("age_seconds") is not None and orders["age_seconds"] > 15:
            st.warning(f"Order observation is stale ({orders['age_seconds']:.0f}s old).")
        if not _show_table(
            orders.get("items", []),
            alt="Bot-owned pending orders from cached bot observations",
            widths={"ticket": 120, "symbol": 110, "created_at_utc": 190},
        ):
            st.info("No bot-owned pending-order records are available.")
        if not orders.get("available"):
            st.warning("No successful pending-order observation is available.")
    if not journal.get("available"):
        st.warning(journal.get("reason", "Journal history unavailable."))
        return
    st.subheader("Recent reconciled orders")
    if not _show_table(
        journal.get("orders", []),
        alt="Recent account-scoped broker orders",
        widths={"order_ticket": 145, "created_at": 190},
    ):
        st.info("No account-scoped broker-order records are available.")
    st.subheader("Recent fills and deals")
    if not _show_table(
        journal.get("deals", []),
        alt="Recent account-scoped broker deals",
        widths={"deal_ticket": 145, "order_ticket": 145, "position_id": 135, "occurred_at": 190},
    ):
        st.info("No account-scoped broker-deal records are available.")


def render_risk(snapshot: dict[str, Any]) -> None:
    _notice(snapshot)
    if not snapshot.get("available"):
        return
    risk = snapshot.get("risk", {})
    execution = snapshot.get("execution", {})
    if risk.get("available"):
        as_of = risk.get("as_of_utc")
        risk_age = None
        if as_of and snapshot.get("generated_at_utc"):
            try:
                risk_age = max(
                    0.0,
                    (pd.Timestamp(snapshot["generated_at_utc"]) - pd.Timestamp(as_of)).total_seconds(),
                )
            except (TypeError, ValueError):
                risk_age = None
        _metric_row([
            ("Daily drawdown", f"{risk['total_drawdown_pct']:.2f}%" if risk.get("total_drawdown_pct") is not None else "Unavailable"),
            ("Realized loss", f"{risk['realised_loss_pct']:.2f}%" if risk.get("realised_loss_pct") is not None else "Unavailable"),
        ])
        _metric_row([
            ("Consecutive losses", risk.get("consecutive_losses") if risk.get("consecutive_losses") is not None else "Unavailable"),
            ("Trades today", risk.get("daily_trade_count") if risk.get("daily_trade_count") is not None else "Unavailable"),
        ])
        limits_label = {True: "Within limits", False: "Limit reached / blocked", None: "Unknown"}
        st.metric("Risk-limit evaluation", limits_label.get(risk.get("limits_ok"), "Unknown"))
        for reason in risk.get("limit_reasons", []):
            st.warning(reason)
        st.caption(
            f"Cached risk evaluation as of {_utc_display(as_of)}"
            + (f" · {risk_age:.1f}s old" if risk_age is not None else "")
            + "."
        )
        if risk_age is not None and risk_age > 15:
            st.warning("The last risk evaluation is stale; do not interpret it as current clearance.")
        limits = risk.get("limits", {})
        remaining = []
        for field, limit_field, label, formatter in (
            ("total_drawdown_pct", "max_daily_drawdown_pct", "Daily drawdown capacity", lambda value: f"{value:.2f}% remaining"),
            ("consecutive_losses", "max_consecutive_losses", "Loss-streak capacity", lambda value: f"{value} remaining"),
            ("daily_trade_count", "max_trades_per_day", "Daily trade capacity", lambda value: f"{value} trades remaining"),
        ):
            value, cap = risk.get(field), limits.get(limit_field)
            if value is not None and cap is not None:
                remaining.append((label, formatter(max(0, cap - value))))
        if remaining:
            _metric_row(remaining)
    else:
        st.warning(
            risk.get("unavailable_reason") or
            "No successful risk/exposure snapshot is cached; the dashboard will not initiate broker history reads "
            "or infer that limits are clear."
        )
        st.info("Current risk measurements: Not yet calculated.")

    st.subheader("Configured limits")
    configured = _risk_limit_rows(risk.get("limits", {}), (snapshot.get("account") or {}).get("currency", ""))
    if configured:
        st.table(
            pd.DataFrame(configured),
            width="stretch",
            hide_index=True,
            alt="Configured risk limits, values, and meanings",
        )
        if any(row["Limit"] == "Not available in current snapshot" for row in configured):
            st.caption("Missing risk settings are not inferred from defaults or private configuration.")
    else:
        st.info("Configured risk limits are unavailable.")

    sessions = risk.get("sessions", {})
    st.subheader("Trading sessions and cutoffs")
    if not sessions:
        st.info(
            "Session settings are not present in this process's published snapshot. "
            "A planned normal bot restart is needed for the current publisher schema; "
            "the dashboard does not read private configuration."
        )
    session_table = _session_rows(sessions)
    if session_table:
        st.table(
            pd.DataFrame(session_table),
            width="stretch",
            hide_index=True,
            alt="Configured trading-session windows, cutoffs, time zones, and exemptions",
        )
    else:
        st.info("Trading session and cutoff configuration is unavailable.")

    positions = snapshot.get("positions", {})
    orders = snapshot.get("pending_orders", {})
    total_limit = risk.get("limits", {}).get("max_open_trades_total")
    if positions.get("available") and orders.get("available") and total_limit is not None:
        open_count = len(positions.get("items", [])) + len(orders.get("items", []))
        stale_ages = [value for value in (positions.get("age_seconds"), orders.get("age_seconds"))
                      if value is not None and value > 15]
        _metric_row([
            ("Observed bot positions + pending", open_count),
            ("Available total trade slots (cached)", max(0, int(total_limit) - open_count)),
        ])
        st.caption("These counts use cached, magic-number-owned broker observations; they are not a new risk evaluation.")
        if stale_ages:
            st.warning(f"Cached exposure observation is stale (up to {max(stale_ages):.0f}s old).")
        directional = _observed_exposure_rows(
            positions.get("items", []),
            orders.get("items", []),
            risk.get("limits", {}).get("max_open_trades_per_direction_per_symbol"),
        )
        st.subheader("Observed symbol and direction exposure")
        if directional:
            _show_table(
                directional,
                alt="Cached bot-owned positions and pending orders by symbol and direction",
            )
        else:
            st.info("No bot-owned positions or pending orders are present in the cached observations.")
    st.subheader("Kill switch")
    if execution.get("kill_switch_engaged") is True:
        st.error(f"Engaged: {execution.get('kill_switch_reason') or 'reason unavailable'}")
    elif execution.get("kill_switch_engaged") is False:
        st.success("Not engaged")
    else:
        st.warning("Kill-switch state unavailable.")
    st.caption("This page is informational only. It cannot enable, disable, or change any risk control.")


def render_news(snapshot: dict[str, Any]) -> None:
    _notice(snapshot)
    if not snapshot.get("available"):
        return
    news = snapshot.get("news", {})
    if not news.get("enabled"):
        st.warning("News monitoring is disabled or unavailable in the running bot.")
        return
    _metric_row([
        ("Provider status", news.get("health", "unknown").upper()),
        ("Last successful fetch (UTC)", _utc_display(news.get("last_success_utc"))),
    ])
    _metric_row([
        ("Calendar age", f"{news['age_hours']:.1f}h" if news.get("age_hours") is not None else "Unknown"),
        ("Consecutive failures",
         news.get("consecutive_failures") if news.get("consecutive_failures") is not None else "Unknown"),
    ])
    if not news.get("verified"):
        st.error("Calendar is stale or unverified. Displayed events must not be interpreted as a complete calendar.")
    filters = news.get("filters", {})
    currencies = ", ".join(filters.get("currencies") or []) or "Unavailable"
    impacts = ", ".join(str(value).title() for value in (filters.get("impacts") or [])) or "Unavailable"
    st.caption(
        f"Provider: {news.get('provider', 'unknown')} · "
        f"Configured filters: {currencies} currency · {impacts} impact"
    )
    st.subheader("Active news locks")
    locks = news.get("active_locks", [])
    if locks:
        _show_table(locks, alt="Current news lock decisions by configured symbol")
    else:
        st.info("No active lock is recorded for the configured symbols, subject to the calendar health state above.")
    st.subheader("Upcoming qualifying events")
    upcoming = news.get("upcoming", [])
    if upcoming:
        _show_table(
            upcoming,
            alt="Upcoming configured economic calendar events",
            widths={"event_id": 160, "time_utc": 190, "title": 260, "lock_start_utc": 190, "lock_end_utc": 190},
        )
    else:
        st.info("No upcoming event records are available from the current bot snapshot.")


def render_journal(snapshot: dict[str, Any]) -> None:
    _notice(snapshot)
    if not snapshot.get("available"):
        return
    row_limit = st.select_slider("Maximum rows", options=[25, 50, 100, 250], value=100, key="journal_limit")
    journal = ReadOnlyJournal(snapshot.get("journal", {}).get("database_path", "")).read(
        snapshot.get("journal", {}).get("magic", 0), snapshot["generated_at_utc"], row_limit
    )
    if not journal.get("available"):
        st.warning(journal.get("reason", "Account-scoped journal is unavailable."))
        return
    st.caption(
        f"Scope: {journal['scope']['login_masked']} · magic {journal['scope']['magic']} · "
        f"latest reconciliation (UTC): {_utc_display(journal['scope']['reconciliation_as_of_utc'])}"
    )
    st.subheader("Reconciled trades")
    if not _show_table(
        journal.get("trades", []),
        alt="Bounded account-scoped reconciled trade journal",
        widths={"position_id": 135, "opened_at": 190, "closed_at": 190, "updated_at": 190},
    ):
        st.info("No reconciled trade rows are available for this scope.")
    st.subheader("Related lifecycle events")
    if not _show_table(
        journal.get("related_events", []),
        alt="Bounded lifecycle events associated with scoped broker records",
        widths={"event_id": 180, "correlation_id": 180, "occurred_at": 190},
    ):
        st.info("No related lifecycle events are available for this scope.")
    st.subheader("Reconciliation history")
    if not _show_table(
        journal.get("reconciliations", []),
        alt="Account- and magic-scoped reconciliation results",
        widths={"completed_at": 190, "history_start": 190, "history_end": 190},
    ):
        st.info("No account- and magic-scoped reconciliation rows are available.")
    st.caption("All journal access is SQLite read-only. Excel export remains the explicit CLI workflow; the dashboard does not write or overwrite workbooks.")


def render_system_health(snapshot: dict[str, Any]) -> None:
    _notice(snapshot)
    if not snapshot.get("available"):
        return
    mt5 = snapshot.get("mt5", {})
    _metric_row([
        ("Bot snapshot", snapshot.get("snapshot_status", "unknown").title()),
        ("Account snapshot", mt5.get("status", "unknown").title()),
    ])
    _metric_row([
        ("Last account update (UTC)", _utc_display(mt5.get("last_account_update_utc"))),
        ("Account snapshot age", f"{mt5['age_seconds']:.1f}s" if mt5.get("age_seconds") is not None else "Unknown"),
    ])
    _metric_row([
        ("MT5 market-data feed", "Recent update"
         if mt5.get("connection_status") == "recent_success" else "Stale / unknown"),
    ])
    st.subheader("Market-data freshness")
    statuses = snapshot.get("market_status", [])
    if not _show_table(
        statuses,
        alt="Per-symbol and timeframe market fetch heartbeat",
        widths={"last_success_utc": 190, "last_closed_candle_utc": 190, "last_error": 280},
    ):
        st.info("No market-data heartbeat records are available.")
    latest = snapshot.get("journal", {}).get("latest_reconciliation")
    st.subheader("Latest reconciliation")
    if latest:
        reconciliation_rows = [
            {"Field": "Status", "Value": str(latest.get("status") or "Unavailable").title()},
            {"Field": "Completed (UTC)", "Value": _utc_display(latest.get("completed_at"))},
            {"Field": "History start (UTC)", "Value": _utc_display(latest.get("history_start"))},
            {"Field": "History end (UTC)", "Value": _utc_display(latest.get("history_end"))},
            {"Field": "Deals seen", "Value": str(latest.get("deals_seen") if latest.get("deals_seen") is not None else "Unavailable")},
            {"Field": "Orders seen", "Value": str(latest.get("orders_seen") if latest.get("orders_seen") is not None else "Unavailable")},
            {"Field": "Positions seen", "Value": str(latest.get("positions_seen") if latest.get("positions_seen") is not None else "Unavailable")},
        ]
        st.table(
            pd.DataFrame(reconciliation_rows),
            width="stretch",
            hide_index=True,
            alt="Latest reconciliation status, time range, and record counts",
        )
        st.caption("A successful MT5 history response does not prove that the broker returned complete history.")
    else:
        st.info("No successful reconciliation is recorded.")
    st.subheader("Recent bot events")
    events = snapshot.get("journal", {}).get("recent_events", [])
    event_rows = _recent_event_rows(events)
    if event_rows:
        _show_table(
            event_rows,
            alt="Recent bot events with readable timestamps, types, statuses, messages, and context",
            widths={"timestamp_utc": 190, "category": 110, "event": 180, "status": 140, "message": 360, "context": 360},
        )
    else:
        st.info("No recent events are available.")
    st.subheader("Recent market-structure events")
    structure_events = snapshot.get("journal", {}).get("recent_structure_events", [])
    structure_rows = _recent_event_rows(structure_events)
    if structure_rows:
        _show_table(
            structure_rows,
            alt="Bounded recent market-structure creation, flip, invalidation, break, and sweep events",
            widths={"timestamp_utc": 190, "category": 110, "event": 210, "status": 140,
                    "message": 360, "context": 360},
        )
    else:
        st.info("No structured market-structure events have been journaled in recent history.")
    st.caption("Weekend flatten policy remains unresolved. The dashboard does not make or execute position-management decisions.")
