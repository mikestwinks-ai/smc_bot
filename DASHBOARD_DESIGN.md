# Local Quant-Style Dashboard — Design and Phase 8 Foundation

**Status:** Phase 8 foundation implemented. This document keeps the verified data boundaries and records the implementation choices and remaining scope.

## 1. Executive recommendation

Build a local-only, read-only browser terminal with **Streamlit**. The trading process remains the sole owner of MT5, strategy state, risk decisions, and order actions. It publishes a bounded sanitized JSON runtime snapshot asynchronously; the Streamlit server reads that snapshot and queries SQLite in read-only mode. A separate HTTP API was not needed for this first foundation. Browser/page refreshes never call MT5 or journal write methods.

The first terminal now provides a desktop-first overview, risk, plan/execution, news, journal, health views, a closed-candle market chart, and current structure overlays. Clearly distinguish live-in-memory state, persisted journal facts, unavailable data, and stale/uncertain data.

Streamlit is now declared in [requirements.txt](./requirements.txt). The inspected environment provided Streamlit 1.66.1, pandas and Altair; no API framework was added. These installed versions are environment observations, not pinned compatibility guarantees.

## 2. Verified existing capabilities and data boundaries

### 2.1 What is already available

| Area | Verified source | Available data now | Persistence / caveats |
|---|---|---|---|
| Current monitoring | [monitoring.py](./smc/journal/monitoring.py), wired by [system.py](./smc/execution/system.py) | MT5 connectivity, last successful account-fetch time, account environment/currency/balance/equity, live-execution flag, kill switch, plans, bot-owned positions and pending orders, daily risk counters/percentages and reasons, news health/locks, journal summary, recent events | `snapshot()` calls the gateway for account, positions, and orders. It may block or fail if MT5 is unavailable, so do not call it on a browser request or label a failed read as an empty account. Login is currently returned; a future public contract should mask or omit it. |
| Account and execution | [risk/models.py](./smc/risk/models.py), [mt5_gateway.py](./smc/execution/mt5_gateway.py) | Account context and broker-derived position/order facts; current position profit and swap; stop/target and volumes | Current values are live broker reads. Account credentials are not required by a dashboard and must never be exposed. Daily risk metrics are percentages/counters, not a complete account P/L history. |
| Plans | [plan.py](./smc/models/plan.py), [plan_store.py](./smc/planning/plan_store.py), [plan_manager.py](./smc/planning/plan_manager.py) | Four models (`OB`, `FVG`, `SWEEP_MSS`, `OB_FVG`), direction, symbol, status, entry zone, invalidation, targets, RR estimate, rationale, source timeframes, quality flags and lifecycle history | `PlanStore` is explicitly in-memory. Phase 8 publishes full current plan fields and bounded lifecycle history, but plans are not restored after restart. Numerical qualification score is not a Plan field. Candidate discard reasons are not yet published. |
| Market data | [data_manager.py](./smc/core/data_manager.py) | Closed-candle DataFrames per symbol/timeframe, separately fetchable forming candle, and Phase 8 last-success/error heartbeat | Candles/heartbeats live in process memory and are published in the runtime snapshot, not journalled as a full chart history. The dashboard uses closed bars only; the forming candle is not fetched for rendering. |
| Structure and zones | [engine.py](./smc/structure/engine.py), [models.py](./smc/structure/models.py), [context.py](./smc/planning/context.py) | `StructureHub.snapshot(symbol)` can produce external/internal bias by timeframe, swings, dual premium/discount, live OBs, FVGs, and liquidity. Phase 8 publishes these current states. | Runtime state is not persisted and cannot provide a replayable chart/zone timeline after restart. |
| Risk | [risk_manager.py](./smc/risk/risk_manager.py), [exposure.py](./smc/risk/exposure.py), [monitoring.py](./smc/journal/monitoring.py) | Trade count, consecutive losses, realized-loss percentage, total drawdown percentage, baseline balance, bot exposure, limit pass/fail and reasons; configured risk/limit values exist | Monitoring reports current counters and reasons, not a time series of risk-limit changes. A percentage is not a dollar daily P/L. Show source time and configured limit alongside the measured value. |
| News | [monitor.py](./smc/news/monitor.py), [calendar.py](./smc/news/calendar.py), [guard.py](./smc/news/guard.py) | Provider health, last successful fetch, age, failures, qualifying events in the service, and configured-symbol lock decisions | Monitoring exposes health and current locks, not a structured upcoming-event list. Calendar events and provider state are primarily runtime data; selected news facts/health/lock events are journalled. Preserve the configured provider, filters, and timezone. The ForexFactory community feed is not an officially guaranteed source. |
| Journal and reconciliation | [store.py](./smc/journal/store.py), [reconciliation.py](./smc/journal/reconciliation.py) | Version 3 SQLite journal: lifecycle events; account/magic-scoped broker orders, deals, current positions/orders, aggregated trades, and reconciliation runs. Read APIs expose events, trades, broker orders/deals, current exposure and reconciliation status. | Default database is `output/state/trade_journal.sqlite3`, configurable by `SMC_JOURNAL_DB`. UTC ISO timestamps, migrations, WAL/full synchronous commits, event idempotency, and deal-ticket keys are implemented. Successful but truncated history is not always detectable; a successful run is not proof of complete broker history. |
| Execution restart state | [state_store.py](./smc/execution/state_store.py) | Durable JSON execution intents, managed-position progress, daily baselines and notification de-duplication | This is a safety cache, not a reporting API. Do not expose the full file or use it as a second source of truth for current broker exposure. |
| Export | [export.py](./smc/journal/export.py), [__main__.py](./smc/journal/__main__.py) | Deterministic Excel export from persisted journal data, six sheets, refusal to overwrite an existing workbook | Existing export is a command-line operation. Keep it separate from any unrelated workbook and do not add a dashboard-triggered file-write endpoint in the first read-only release. |
| System errors and recovery | [system.py](./smc/execution/system.py), journal events and application logs | System start/shutdown, management/execution errors, reconciliation errors, kill-switch and MT5 disconnect/reconnect events where recorded | The monitoring response includes recent journal errors/recovery events, but logs are not a structured, comprehensive event store. Sanitize exception text before exposing it. |

### 2.2 Important absences

- No HTTP API exists or is required by the current single-user local implementation. The dashboard uses a versioned JSON snapshot contract and read-only SQLite access.
- A per-symbol/timeframe market fetch heartbeat is now published with last successful fetch, last closed candle, age, and last error. The freshness badge uses a dashboard heartbeat threshold; it does not change trading decisions.
- A versioned JSON snapshot payload exists for the Phase 8 fields, but it is not a long-term persisted market/structure store.
- Current structure/zone snapshots, candles, and live plan objects are still not durable across restart. Plan lifecycle journal events do not constitute a complete restorable Plan snapshot.
- Some existing `JournalStore` query helpers return all stored trades/orders/deals, and `status_summary()` counts all events and broker records; they are not all account/magic-filtered. The dashboard's separate read-only reader applies account/magic scope to its broker record queries and does not assume generic events have account attribution.
- Monitoring currently obtains broker state during the request and does not explicitly report API freshness for market data.
- News monitoring does not return a typed list of upcoming event details in its monitoring contract.
- No reliably validated R-multiple, setup-model expectancy, slippage, fill-rate, or execution-quality series is available. Do not display these as facts until the trade attribution and denominator are defined and validated.
- Account-specific balance/equity is available only from the current broker read. Historical balance/equity curves and account-wide daily P/L are not journalled as a validated time series.
- Successful-but-truncated broker history cannot always be distinguished from complete history. Expose reconciliation status and this uncertainty, not a green “complete” assertion.
- There is no separately configured weekend flatten policy. Do not infer or invent one from weekday session restrictions.

The authoritative Phase 6/7 capabilities and limitations are recorded in [PHASE6_COMPLETION.md](./PHASE6_COMPLETION.md), [PHASE7_COMPLETION.md](./PHASE7_COMPLETION.md), and the Phase 6/7 sections of [README.md](./README.md). The reports record the Phase 7 suite at **315 passed**; this design-only task did not rerun the suite.

## 3. Proposed visual system and navigation

### Visual direction

- Desktop workstation canvas, dark charcoal/navy background, high-contrast text, restrained green/red for positive/negative and amber for warning/staleness. Do not rely on color alone; pair it with labels/icons.
- Fixed global header: bot process state, MT5 state, execution mode, kill-switch status, snapshot time/age, and a persistent warning banner for stale or incomplete data.
- Left navigation: **Overview**, **Market**, **Plans**, **Execution**, **Risk**, **News**, **Journal**, **System**.
- Main content uses aligned metric cards, dense but readable tables, explicit units, sortable columns, and expandable diagnostic context. Avoid decorative indicators that imply precision unavailable in the data.
- All displayed timestamps are UTC internally and labelled with their display timezone. If local display conversion is later added, show the timezone name and preserve UTC in exports/contracts.
- Read-only affordances only: symbol/timeframe selection, date range, filters, row expansion, and navigation. No trade controls, kill-switch toggles, order actions, or strategy configuration controls.

### Page wireframes and data sources

#### Overview

```text
┌ Bot state | MT5 connected/stale | DRY-RUN/LIVE | Kill switch | Snapshot age ┐
│ Account context     Daily risk       Open exposure      News health         │
├───────────────────────────────┬─────────────────────────────────────────────┤
│ Daily risk / account summary  │ Bot-owned positions + pending orders        │
├───────────────────────────────┴─────────────────────────────────────────────┤
│ Active plans                     Recent execution / rejection / recovery    │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Cards:** masked account context (currency, demo/real, equity and balance only when current), execution mode, kill switch, daily trade count, drawdown percentages, open bot-owned positions/orders, news age/locks, latest reconciliation. Account identifiers should be omitted or masked by default.

**Charts/tables:** daily equity/P&L chart is **not** available as a trustworthy series today; do not draw a synthetic one. Show current broker values as single readings with “as of” timestamps. Use the plan, position, and recent event tables described below.

**Sources:** published runtime snapshot, journal read model and latest reconciliation record. Any unavailable source gets an explicit unknown/stale/error state.

#### Market intelligence

```text
Symbol [selector]  Timeframe [H4 | M30 | M15 | M5]   Data age / closed candle time
┌ Candles + bias + swing levels + OB / FVG / liquidity / sweep / flip overlays ┐
├ External bias (H4/M30)     Internal bias (M15/M5)     Premium/discount        │
└ Recent structure events / zones table                                          ┘
```

Use closed OHLC candles and `StructureHub.snapshot(symbol)` outputs. Overlay order blocks, fair value gaps, active liquidity, sweeps, and flipped zones only when those objects are present in a fresh published snapshot; annotate timeframe and active/resolved status. Show external and internal bias separately. Do not promote forming candles to confirmed structure. Candle/zone history is not persisted now; chart lookback initially means the current process’s in-memory window, and restart must visibly reset the available range.

#### Setup and plan monitor

```text
Filters: symbol | entry model | lifecycle
┌ Plan ID | symbol | direction | model | status | timeframe | RR | updated       ┐
└ Selected plan: entry-zone range | invalidation | targets | biases | rationale  ┘
  quality flags | source zones | lifecycle timeline | cancel/flip reason
```

Display all four model names, status, price zone, invalidation, target levels, RR estimate, quality flags, rationale, parent/child plan IDs and source timeframes when supplied. Numerical qualification score is not defined; do not create one from quality flags. Candidate rejection reasons can be shown only if the runtime publisher adds the `PlanManager.discards` view. Reconstructed lifecycle history from journal events must be labelled as event history, not a complete current plan object.

**Sources:** in-memory plan snapshot and journal plan events. After restart, show “plans rebuilding/not restored” rather than treating an empty `PlanStore` as proof that no prior plans existed.

#### Execution and positions

```text
Tabs: Open positions | Pending orders | Orders & deals | Management events
┌ Ticket/position | symbol | side | volume | entry/current | SL/TP | P/L | time ┐
└ Selected lifecycle: plan → order → deal(s) → position / close; costs + status   ┘
```

Current bot-owned positions/orders come from the broker snapshot scoped by account, symbol and configured magic. Reconciled history supplies orders, individual deals/fills, position aggregation, commissions, swaps, fees and net results where identified. Keep the order, deal, position and completed-trade entities distinct. Show partial exits as their own deals and only aggregate closed performance from attributable closed trades; `unknown` and open trades are not wins/losses. No existing source reliably provides slippage or fill quality, so omit these metrics.

**Sources:** runtime snapshot and account/magic-scoped journal tables/events. If the broker snapshot failed, mark current exposure unknown and retain last-good values with their age; never replace them with empty lists.

#### Risk centre

```text
┌ Risk per trade (configured) | Daily drawdown / limit | Consecutive losses / limit ┐
│ Trades today / limit       | Open exposure / cap     | Kill-switch state          │
├ Current symbol/direction exposure table ────────────────────────────────────────┤
│ Latest blocked-entry reasons and recent risk decisions                           │
└──────────────────────────────────────────────────────────────────────────────────┘
```

Show actual configured caps next to values: risk percentage, daily drawdown, consecutive-loss count, trades/day, maximum open trades, and per-symbol/direction exposure. The latest monitor provides global risk-limit reasons; exact per-proposal risk decision history is available only where journalled, and must not be inferred from a current limit status. Distinguish realized-loss and total-drawdown percentages. Kill-switch is status-only.

#### News centre

```text
Provider health | Last successful fetch | Calendar age | Consecutive failures
┌ Warning when stale/unverified or policy blocks new entries ──────────────────┐
│ Upcoming qualifying events: UTC time | currency | impact | title | lock span │
│ Active locks by configured symbol | reason | window | last checked           │
└ Recent provider / lock / recovery events                                         ┘
```

The current monitor has provider health and active locks but not a structured upcoming calendar. Add an immutable event-list snapshot with configured currency/impact filters, source, event time, actual/forecast/previous only when available, and calculated lock boundaries. Display the configured timezone and provider; make no claim that the community feed is guaranteed. Missing actual values remain missing and do not become zero.

#### Performance and journal

```text
Date range | symbol | model | status (closed/open/unknown)
┌ Closed trades | net result | costs | volume | direction | open/close time       ┐
├ Persisted journal events and reconciliation runs ──────────────────────────────┤
│ Validated metrics only: closed count, net P/L and win/loss if attribution valid │
│ Export: instruction/link to existing CLI; no implicit overwrite or write API   │
└────────────────────────────────────────────────────────────────────────────────┘
```

Use persisted SQLite records, not in-memory state. Initially show counts and net/cost figures over an explicit date range, with open/unknown rows separately identified. Only calculate win/loss from attributable `closed` trades and explain exclusions. Do not show expectancy, R multiples, model performance, drawdown curves or account performance until initial risk, plan-model attribution, complete trade closure and broker history coverage are proven for the selected population. Excel export remains the explicit CLI flow and requires a new output path.

#### System health

```text
Trading process | MT5 account-update age | Market data by symbol/timeframe
SQLite schema/health | Reconciliation status & age | News provider | Notifications
┌ Recent exceptions, disconnects, retries, reconciliation runs and recoveries ─┐
│ Each row: UTC time | component | severity | correlation | sanitized detail     │
└──────────────────────────────────────────────────────────────────────────────┘
```

Add database read/write health as a distinct source-health check; do not infer healthy writes merely from opening the file. Show the latest successful reconciliation and last failure separately. Show notification failures only if they are captured as structured events. Do not expose raw secrets, environment contents, full paths containing user names, or unsanitized exception text.

## 4. Local data architecture

### Flow

```text
Trading process (single owner of MT5 and mutable strategy state)
  ├─ bounded asynchronous publisher: sanitized runtime snapshot -> atomic local file
  └─ existing SQLite journal
         │
         ▼
Streamlit UI on loopback (snapshot file + SQLite opened read-only)
```

The bot assembles snapshots from its existing in-process data and cached observations, and queues a single latest snapshot to a background atomic file writer. The running loop publishes at startup and then on a bounded five-second cadence. The writer never performs MT5 calls or journal reads; a failed write is logged and leaves the prior file stale. The publisher does not call `MonitoringService.snapshot()` because that method makes broker requests. The browser process imports no MT5 or trade-execution code. Current cached exposure is restricted to the configured broker/canonical symbols and configured magic.

The dashboard access boundary:

- Streamlit is explicitly launched bound to `127.0.0.1`; do not expose it to a LAN or cloud.
- Runtime snapshots contain masked account identity, not credentials or raw login. Dashboard journal queries select the latest reconciliation account for the configured magic number and use that exact account/magic pair.
- SQLite is opened with `mode=ro` and `PRAGMA query_only=ON`; queries are parameterized and row-limited to 250. Broker records and related events are scoped to account and magic. Generic events without broker entity links are not presented as account-scoped journal history.
- Missing/corrupt snapshots, stale snapshots, missing journal scope, and journal read failures remain explicit degraded states.
- The app exposes no trade, position, kill-switch, reconciliation, config, journal-write, or export-write controls. No order execution module is imported into the app.
- Streamlit reruns and database reads do not perform broker operations. Snapshot file publication is queued to a single background writer.

### Snapshot and journal contracts

The snapshot includes `schema_version`, `sequence`, and `generated_at_utc`, plus source-specific timestamps, ages, and unavailable states. Market candles and current structure/plans are runtime-only. The SQLite access layer uses explicit account/magic scope, bounded result counts, and safe `unavailable` results when it cannot derive a completed account scope.

If a separate API becomes necessary for a non-Streamlit client, retain this contract but implement a new authenticated loopback-only read boundary; do not have it call MT5 or add write routes.

### Refresh and stale-data behavior

- UI refresh can be selected at 1, 2, 5, 10, or 30 seconds; market candles update only when the bot publishes new closed bars.
- Journal results are bounded to 25–250 rows; the current app reads directly through the local read-only data-access module.
- Display both “snapshot generated” and the source-specific timestamp. A freshly published snapshot can still contain stale broker/news/market data.
- Carry forward last-good data only with a prominent stale badge and its source age. On first startup with no successful data, show `unknown`, not “disconnected with no positions” or “no news.”
- Show history-query completeness as unverified when broker/terminal does not expose a completeness marker.

## 5. Prerequisite fixes and unresolved limitations

1. **Plan recovery:** persist complete versioned plan snapshots and lifecycle events, or clearly limit the first release to current-process plans. Restoration must revalidate expiry, current market state, and lifecycle before any plan is labelled actionable; never resurrect stale plans as executable.
2. **History completeness:** expose `complete / incomplete / unverified` separately from successful API calls. Until a broker-specific completeness mechanism exists, performance metrics must disclose that reconciled history can be truncated.
3. **Market heartbeat:** last-success timestamps, last-closed candle time, per-symbol/timeframe fetch errors, and measured age are published. The current UI threshold (15 seconds or five polling intervals, whichever is larger) is a dashboard warning only and does not alter strategy freshness checks.
4. **Weekend exposure:** show the current configured session/cutoff policy and “weekend flatten policy unresolved.” Decide a separate policy only if a future strategy change is desired; dashboard work must not close positions or imply one.
5. **Structure and chart data:** basic closed-candle chart and current structure/zone snapshot are published. Structure events such as historical sweep timelines and all zone transitions are not yet persisted for chart replay.
6. **Plan/rejection DTO:** current plan fields and UTC lifecycle history are published; bounded candidate-discard/rejection reasons are not yet surfaced. Add scores only if a documented, tested scoring method is approved; existing `PlanQuality` booleans/ratios are not an overall score.
7. **News event DTO:** qualifying upcoming events and active symbol locks are now published from the existing in-memory provider data. The dashboard never refreshes the provider.
8. **Monitoring isolation:** browser access reads cached bot snapshots; the existing `MonitoringService.snapshot()` remains a direct broker-reading method and must not be wired to page refresh.
9. **Attribution and performance:** validate plan→order→deal→position links, unknown-status handling, costs and time boundaries before showing model statistics, R, expectancy or equity/drawdown histories.
10. **Database health and logs:** add explicit read/write health and structured notification failure events if required for the health view; sanitize error details and prevent secret-bearing paths/values from leaving the process.

## 6. Recommended technology

| Layer | Recommendation | Trade-off |
|---|---|---|
| UI | Streamlit multipage app using `st.navigation`/`st.Page`; native components first | Implemented and declared in requirements. Fast local Python UI; less flexible than a React SPA for a bespoke terminal. |
| Data boundary | Atomic JSON runtime snapshot + read-only SQLite queries in Streamlit server process | Implemented without another HTTP/API framework or MT5 lifecycle. Reconsider an API only for another client or stronger process isolation requirements. |
| Data | Atomic JSON runtime snapshot + read-only SQLite queries | Runtime JSON carries ephemeral market/structure/plans without adding a dashboard database; SQLite remains the persisted journal source. Avoid a second writable state store. |
| Charts | Altair/Vega-Lite for risk/performance time series, simple OHLC/indicator layers, distribution and event charts; test candlestick usability with real bot data | Altair was available in the inspected environment and follows Streamlit’s Vega-based chart path. If a genuinely interactive multi-timeframe candlestick terminal with large overlay counts cannot meet UX/performance criteria, evaluate a small Lightweight Charts custom component in a later stage. This adds a frontend build/runtime and a larger security/maintenance surface; do not add Plotly/Bokeh solely by assumption. |
| Windows development | Dedicated project virtual environment; run bot and UI as separate local processes | Implemented local snapshot path and loopback launch. No cloud deployment, external database, or container is needed. |

For Streamlit-specific implementation, follow the installed package’s guidance: native components before custom HTML/CSS, `st.fragment` for independent periodic sections, bounded `st.cache_data` TTLs for serializable reads, and no slow work hidden in tabs. The design does not rely on these behaviors as existing bot features.

## 7. Phased implementation and acceptance criteria

### Phase A — Data contracts and publisher (foundation implemented)

- Add versioned immutable DTOs and a bounded, atomic snapshot publisher for account summary, plans, positions/orders, risk, news, source health and current structure/candles where available.
- Add per-source timestamps, sequence, stale/unknown reasons, restart state and masked account identity.
- Ensure publisher errors cannot block trading or bypass safeguards.

**Accept when:** tests prove no MT5 trade API is called, no credential fields are serialized, publication failures do not change execution decisions, snapshot reads are concurrency-safe, and stale/unknown values remain distinguishable from zero/empty.

### Phase B — Read-only access and security (file/SQLite boundary implemented)

- Publish a bounded JSON snapshot atomically from the bot and read it from a separate local Streamlit process.
- Read journal records through read-only SQLite connections, bounded queries, and exact account/magic scope.
- Bind Streamlit to loopback; do not introduce an HTTP API or broker access in the dashboard process.

**Accept when:** tests prove snapshot publication and journal reads cannot invoke broker trade operations or mutate journal data; journal queries are bounded and parameterized; missing, corrupt, and stale sources are explicit; and the app is documented for loopback-only use.

### Phase C — Initial terminal (first working pages implemented)

- Implement Overview, Market, Performance, Model Analysis, Execution, Risk Centre, News Centre, Journal, and System Health pages.
- Show explicitly unavailable or insufficiently attributed data rather than inventing metrics; retain desktop hierarchy, source times, and stale/error states.

**Accept when:** each visible field maps to the published snapshot or persisted journal, stale state is visually unambiguous, account identity is masked, and all controls are navigation or display-only filters.

### Phase D — Market/structure terminal and plan recovery (market foundation implemented; recovery remains)

- Add closed-candle visualization and current structure/zone snapshot; durable plan recovery and candidate rejection attribution remain future work.
- Evaluate Altair performance against actual candle/zone counts before selecting a custom chart.

**Accept when:** published structure/zone data matches source snapshots; chart candles are clearly identified as closed-bar data; and restart never presents stale plans as executable. Historical sweep/zone replay and durable plan recovery are not yet implemented.

### Phase E — Validated performance and release readiness (remaining)

- Add only metrics with validated attributable closed-trade populations and disclose history completeness limitations.
- Exercise mock broker disconnects, snapshot staleness, database failures, restart, unauthorized requests and dashboard polling load.

**Accept when:** no live account is needed; dashboard/API cannot send or alter trades; API load does not block strategy execution; open/unknown trades are excluded from closed-trade statistics; export remains separate and no existing file is overwritten.

## 8. Decisions and implementation gate

Initial product choices are now:

1. **Chart ambition:** Altair-based closed candles first; specialist chart component deferred pending a demonstrated need.
2. **Local access model:** single Windows user; Streamlit binds to loopback. No LAN exposure.
3. **Account identity display:** masked by default. Never show credentials.
4. **Weekend policy:** no dashboard decision is needed. If a future trading-policy change is desired, decide it separately; this design will expose the current unresolved state without flattening exposure.

## 9. Phase 8 implementation and safety

- Phase 8 adds a Streamlit multipage app, async atomic JSON snapshot publisher, per-series market heartbeat, cached broker observations and a read-only journal query module. Snapshot market rows are bounded to 500 closed candles per configured symbol/timeframe.
- Pages cover Overview, Market, Performance, Model analysis, Execution, Risk centre, News centre, Journal, and System health. Performance and model metrics are constrained to reconciled closed trades; model/session attribution remains explicitly unavailable.
- Snapshot assembly reads cached bot observations and existing in-memory data. It does not call `MonitoringService.snapshot()`, open an MT5 connection, refresh news, or issue trade actions. The file writer is backgrounded; journal write failures and snapshot write failures do not change trading safeguards.
- No trading controls, journal mutations, reconciliation triggers, or workbook writes are exposed. The journal reader uses SQLite read-only mode, parameterized queries, and a 250-row maximum.
- Start the bot normally, then in another PowerShell window run:

  ```powershell
  python -m streamlit run smc\dashboard\app.py --server.address 127.0.0.1
  ```

- **Live execution remains disabled by default.** A safe dashboard validation does not require enabling live execution or sending an order. Use a demo account only for optional visual validation of broker-connected data.
