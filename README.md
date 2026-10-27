# Adaptive SMC Trading System (MetaTrader 5)

**Status: Phase 8 - read-only local dashboard foundation.**
Live orders are OFF by default.

## Structure
```
smc_bot/
├── main.py                  # Phase 1 smoke test (connect, load, poll)
├── .env.example             # credentials template -> copy to .env
├── requirements.txt
├── smc/
│   ├── config.py            # ALL settings (dataclasses + validation)
│   ├── core/
│   │   ├── exceptions.py    # error hierarchy
│   │   ├── timeframes.py    # Timeframe enum, External/Internal layers
│   │   ├── mt5_connection.py# connect/retry/reconnect, symbols, account
│   │   └── data_manager.py  # CandleStore + MarketDataManager
│   ├── utils/logger.py      # console + rotating file logging
│   └── structure/ planning/ risk/ execution/ intelligence/ journal/
│                            # empty placeholders for Phases 2-6
├── tests/                   # run without MT5 via a fake module
├── logs/  output/  data/
```

## Setup (Windows, Python 3.10+)
```
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # then fill in MT5 details
python main.py --once       # smoke test
pytest -q                   # unit tests
```

## Assumptions to verify
- **Symbol suffix**: defaults to `m` (e.g. `XAUUSDm`). Change `BROKER_SYMBOL_SUFFIX`.
- **Server time**: set `MT5_SERVER_UTC_OFFSET` to your broker's offset from UTC.
- **Cut-offs (22:30 / 23:00)** are evaluated in UTC (`sessions.cutoff_timezone`).
- **Sessions** are London and New York 08:00-17:00 local time (DST-aware); the overlap is their intersection.
- Only **closed** candles are stored; the forming bar is never used for structure.

## Hooks for Phase 2
`MarketDataManager.update_all()` returns `CandleEvent`s (highest timeframe first),
and `get_candles(symbol, tf, count)` returns UTC-indexed OHLCV DataFrames.

## Phase 2 - structure engine (`smc/structure/`)
| Step | File | What it does |
|---|---|---|
| 1 | `swings.py` | Fractal swings (5/5 default), batch + incremental, same rule |
| 2 | `layers.py` + `tracker.py` | External (H4, M30) BOS / CHoCH, bias, active swings |
| 3 | `layers.py` + `tracker.py` | Internal (M15, M5) BOS / CHoCH / MSS, independent bias |
| 4 | `premium_discount.py` | Dual P/D from External and Internal dealing ranges |
| 5 | `order_blocks.py` | Last opposing candle before a displacement break; records the impulse-leg origin for FVG linkage; mitigation in `zones.py` |
| 6 | `fvg.py` | 3-candle FVGs, mitigation, `calculate_rr()` / `current_rr()` |
| 7 | `liquidity.py` | Equal highs/lows, prev day/session H/L, sweep detection |
| 8 | `flipped_zones.py` | Body-close break -> zone flipped + `FlipSignal` |
| - | `engine.py` | `StructureHub`: orchestrates everything per closed candle |

Usage: `hub = StructureHub(config); hub.initialize(data_manager)`, then
`hub.on_candle_events(data_manager.update_all(), data_manager)`; read state with `hub.snapshot(symbol)`.

## Phase 3 - planning layer
| File | Role |
|---|---|
| `smc/models/plan.py` | `Plan`, `PlanStatus`, `EntryModel`, zones / invalidation / targets |
| `smc/planning/plan_store.py` | In-memory `PlanStore`: add, update, cancel, flip, list_active, duplicate guards |
| `smc/planning/entry_models.py` | The four evaluators -> `Candidate` or `Discard(reason)`; OB+FVG confluence requires same-symbol/timeframe zones from the OB displacement leg, while standalone OB and FVG models remain eligible independently |
| `smc/planning/context.py` | Builds evaluation contexts (P/D ranges, targets, impulse overlap, FVG stop anchor) |
| `smc/planning/plan_manager.py` | Adaptive loop: flip, cancel, expire, refresh, progress, scan, dedupe |
| `demo_phase3.py` | Synthetic multi-timeframe run printing created / canceled / flipped plans |

Plans retain a typed entry-range basis and references to their actual source-zone objects.
Dashboard snapshots publish those IDs, zone records, and the verified OB/FVG displacement-leg
relationship. Older snapshots can recover standalone OB/FVG provenance only from one exact
symbol/timeframe/direction/bounds match created before the plan; ambiguous or missing matches
remain explicitly unresolved, and exact legacy recoveries are labeled as recovered in the
dashboard. Plan storage itself remains in-memory.

Status flow: PLANNED -> ACTIVE (price within 1 ATR of the entry zone) -> READY_TO_EXECUTE
(price traded into the zone). Phase 3 never sets EXECUTED.

Zone context reports touches separately from confirmed reactions: one reaction is counted
at most once per continuous touch episode, and only when a touched candle closes back beyond
the zone's near edge. Nearby supply/demand scenarios are read-only possibilities, not entry
signals; plans can also report live opposing zones before their existing first target without
changing entry-model eligibility, targets, invalidation, risk, or execution rules.

```
mgr = PlanManager(config, StructureHub(config))
mgr.run_candles(symbol, tf, closed_candles_df)   # feed higher timeframes first
mgr.store.list_active(symbol)
```
Run `python demo_phase3.py` and `pytest -q`.

## Phase 4 - risk, execution and trade management

**Live order submission is disabled by default.** Without the switches below the bot validates plans, sizes
positions and calls MT5 `order_check`, but never calls `order_send`. `MT5Gateway.send` itself raises when live is off.

| Area | Files |
|---|---|
| Risk | `smc/risk/risk_manager.py` (limits + sizing), `sizing.py`, `exposure.py` (restart-safe counting from MT5), `sessions.py` (the only time-of-day logic), `models.py` |
| Execution | `smc/execution/mt5_gateway.py` (typed MT5 access, the only place that can send), `validation.py`, `order_builder.py`, `execution_engine.py`, `fills.py` |
| Management | `smc/execution/trade_manager.py` (partial, break-even, structural trail, pending monitor, 23:00 flatten) |
| Safety | `kill_switch.py`, `state_store.py` (atomic JSON), `system.py` (wiring) |

### Rules as implemented
* Risk per trade: equity < $10,000 -> 0.5%, >= $10,000 -> 1.0%, clamped to a 1% hard ceiling. Volume is rounded DOWN
  onto `volume_min + k*volume_step`; if the minimum lot exceeds the budget the trade is rejected.
* Daily limit (2%): baseline = start-of-trading-day **balance**. Blocks when EITHER realised loss OR total drawdown
  (realised + floating) reaches 2%. Both are logged separately.
* Max 2 consecutive losses (counted over trades closed in the current trading day), max 3 trades/day, max 2 open
  (positions + pending orders), 1 per direction per symbol. All rebuilt from MT5 history, so restarts cannot reset them.
* Executable plan states: `active` / `ready_to_execute`. MARKET when price is inside the zone, LIMIT while approaching,
  STOP only if `allow_stop_entries`. Everything is re-validated immediately before sending.
* Idempotency: key `<plan_id>|<ddHHMM of creation>` goes into the MT5 order comment and the state file *before* sending.
  Uncertain answers are reconciled against MT5 orders/positions/deals before any retry.
* +1.5R: close `partial_close_fraction` (50%), and only after MT5 confirms the volume change is the stop moved to
  break-even (entry +/- commission/swap cost). Structural trailing needs a new swing formed after entry AND a later BOS;
  it only ever tightens the stop.
* 22:30 no new entries, 23:00 flatten - in `sessions.cutoff_timezone` (**default UTC; set `STRATEGY_TIMEZONE`**).
  Pending orders also carry a broker-side expiry at 22:30. Crypto uses the pre-existing `crypto_exempt_from_cutoffs`.

### Safe way to run it
1. `pytest -q` (no broker needed) and `python demo_phase4.py` (fake broker only).
2. Copy `.env.example` to `.env`, fill MT5 login **locally** (never paste credentials anywhere), leave `SMC_LIVE_*` empty.
3. `python main.py --once` against a **demo** account: you will see `DRY-RUN` and what *would* be sent.
4. To place real DEMO orders: set `SMC_LIVE_EXECUTION=true` and `SMC_LIVE_CONFIRM=I_ACCEPT_LIVE_ORDER_RISK`.
   REAL accounts stay refused unless `SMC_ALLOW_REAL_ACCOUNT=true`.
5. Emergency stop: create a file called `KILL_SWITCH` in the project folder (delete it to release).

## Phase 5 - economic calendar, news guard, scenarios, Telegram

| Area | Files |
|---|---|
| Calendar | `smc/news/providers.py` (replaceable sources), `calendar.py` (refresh, cache, freshness, de-dup, change detection), `models.py` |
| Protection | `smc/news/guard.py` (stateless lock decisions), wired into `ExecutionEngine._prepare` (both validation passes) and `TradeManager` (pending orders, optional flatten policy) |
| Interpretation | `smc/news/scenarios.py` (Hotter / Cooler / Mixed - pure, cannot trade), `monitor.py` (alerts) |
| Telegram | `smc/notify/telegram.py` (non-blocking, de-duplicated, token-safe), `messages.py`, `router.py` |

**Calendar sources** (`CALENDAR_PROVIDER` in `.env`)
* `faireconomy` (default): the community ForexFactory JSON feed. Unofficial, forecast + previous only, **no actual values**,
  may rate-limit. *Not verified from the build environment - confirm it on your machine.* Polled at most hourly.
* `file`: a JSON file you maintain/export (`{"updated_at": "...", "events": [{"time","currency","title","impact","forecast","previous","actual"}]}`).
* `CALENDAR_ACTUALS_FILE` overlays `actual` values onto any provider - this is what enables post-release Hotter/Cooler/Mixed.
* The MetaTrader5 Python package has no calendar API (as far as I know), so MT5 is not a source.

**Lock rule:** `[T-10min, T+10min)` for qualifying events (impact in `news.impact_levels`, currency in `news.currencies`;
default high-impact USD; add `"medium"` for medium protection). Symbols are matched by base/quote currency
(XAUUSD -> XAU+USD), unknown symbols are treated as exposed. Pending orders also carry a broker-side expiry at the next
lock start, and are cancelled when a lock begins. Open positions are **kept** (`news.open_position_policy="keep"`);
`"flatten"` is opt-in and never triggers on a calendar outage alone.

**Fail-safe:** a calendar older than `max_calendar_age_hours` (24h) or never loaded is "not verified": new entries are
**blocked** (`on_calendar_unavailable="block"`, default) or allowed with a loud warning (`"allow"`). A failed or empty fetch is
never treated as "no news". A disk cache lets a restart inside a lock window recover without the provider.

**Scenarios:** Hotter = stronger economy / higher inflation than consensus; Cooler = weaker; Mixed = in line, conflicting
components, missing data, unit mismatch or unsupported event type. Inverse metrics (unemployment rate, jobless claims) are
handled, units are compared like with like, and nothing is inferred without a consensus value. Output is context only.

**Telegram:** set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in your local `.env` (never in chat). Disabled = silent no-op.
Delivery is queued with timeouts/retries; a failure never affects orders. Run `python demo_phase5.py` to see the messages
(console only, fake data).

## Phase 6 - durable journal, reconciliation, export and monitoring

### Durable database

Lifecycle events and broker history are stored in SQLite at
`output/state/trade_journal.sqlite3` by default. Override the database location with `SMC_JOURNAL_DB`.
The SQLite file is separate from the restart-safe execution cache (`output/state/execution_state.json`),
the news cache, and Excel exports. The database uses WAL mode, full synchronous commits, UTC ISO-8601
timestamps, unique event IDs/idempotency keys, and versioned `PRAGMA user_version` migrations. Schema
versions 1-3 create the event table, broker/trade tables, then reversal classification; migrations preserve
existing rows. A failed journal append is logged and does not change execution/risk decisions.

Events cover closed-candle observations, plan lifecycle and rejected setups, risk/kill-switch decisions,
orders and uncertain outcomes, fills/positions, management actions, news calendar state/events/locks,
reconciliation, connection recovery, and system lifecycle/errors. Data is event-based; forming ticks are
not persisted. Credentials and notification tokens are not journal fields.

### MT5 reconciliation and safety

The live application attempts reconciliation at startup, and it can be run explicitly:

```powershell
python -m smc.journal reconcile
```

It reads MT5 history orders/deals plus current bot-owned positions and pending orders. Records are scoped to
the connected account, configured magic number, and configured symbols. Manual trades and other magic
numbers are excluded. Deal tickets are unique per account/magic, orders are upserted by ticket, and all
partial fills/exits and commission, swap, fee, and profit values aggregate by position ID. Open positions
remain open in the trade table. Repeated runs are idempotent. The initial history interval starts at
2000-01-01 UTC through the current time.

Reconciliation never sends, edits, or closes an order. All required broker reads complete before the journal
transaction starts. A raised/failed API call aborts the run and does not clear the last successful snapshot
or record a successful reconciliation. A broker may still provide a successful but truncated history
response; MT5 exposes no universal completeness marker. The connected terminal/account must therefore have
the relevant history available. Ambiguous reversal (`INOUT`) positions are kept as `unknown`, not reported
as completed trades. The configured `magic_number` is the ownership boundary; brokers that do not preserve
it consistently need an explicit broker-specific ownership decision before using trade statistics.

### Excel export

Install dependencies with `python -m pip install -r requirements.txt` (Excel exports use `openpyxl`).
Choose a new `.xlsx` path; existing workbooks are never overwritten:

```powershell
python -m smc.journal export --output "output\exports\journal-2026-10-25.xlsx"
```

Use `--database` to export a non-default journal file. The workbook is generated only from committed SQLite
records and contains `Trade Summary`, `Setup & Plan`, `Orders & Fills`, `Risk & Rejections`, `News Events`,
and `System Health` sheets. Empty datasets still produce all six sheets. Exports remain separate from
other workbooks and are written via a temporary file before publication.

Inspect persisted journal/reconciliation records without connecting to a broker:

```powershell
python -m smc.journal status
```

### Read-only monitoring interface

`ExecutionSystem.monitoring.snapshot()` returns a structured, read-only dictionary containing connection
and account identity (login/currency/environment, no credentials), live/kill-switch state, current plans,
bot-owned positions and pending orders, daily risk counters/limits, news health/locks, and recent journal
events. It uses the existing gateway, plan store, exposure tracker, risk manager and news guard; it does not
place, modify or close trades and does not persist a daily baseline while reading status. MT5/API failures
are raised to the caller rather than presented as a successful empty snapshot. Do not call this interface
from a browser request because it performs live broker reads.

## Phase 8 - local dashboard

The Phase 8 foundation adds a Streamlit multipage dashboard with Overview, Market, Performance, Model
Analysis, Execution, Risk Centre, News Centre, Journal, and System Health views. It reads a sanitized,
versioned runtime snapshot written by the bot and opens the journal using SQLite read-only mode. The bot
remains the only owner of MT5 and all trading state. Browser refreshes do not create MT5 connections,
execute orders, mutate the journal, toggle safety controls, reconcile history, or write exports.

The bot publishes `output\dashboard\runtime_snapshot.json` at startup and attempts another publication
whenever at least five seconds have elapsed in the main loop. Snapshot assembly reads cached bot state and
bounded journal data in that loop; file I/O runs in a one-item background queue. If the writer is busy, a
pending snapshot is replaced by the newest one rather than blocking the trading loop. Writes use a temporary
file followed by an atomic replace, so a failed write leaves the prior snapshot intact. Failed assembly or
publication is logged and retried on a later interval; the prior snapshot's original timestamp is retained
and the dashboard marks it stale after 15 seconds. Each active page runs a Streamlit fragment that reloads
the snapshot at the sidebar-selected 1, 2, 5, 10, or 30 second interval (default 5 seconds). The displayed
publication timestamp, age, and stale status help distinguish an old state from a current one.

Market data contains at most 500 closed candles per initialized symbol/timeframe; the forming candle is not
fetched for the dashboard. Per-series successful-fetch timestamps and errors are included. Missing
account/risk/history information is shown as unavailable rather than zero. The generated output folder is
ignored by Git.

The Risk Centre presents configured limits and session cutoffs as labeled values, distinct from cached risk
measurements and cached position/order observations. If no successful risk evaluation has populated the
bot's exposure snapshot, risk measurements remain unavailable; the dashboard does not initiate broker
history reads to synthesize them. Settings absent from a running snapshot are shown as unavailable, not
reconstructed from defaults or private configuration. After a bot restart, the current snapshot publisher
includes session windows, time zones, and cutoffs. A bot process started with an older publisher must be
restarted normally at a planned time before those new fields appear; the dashboard will not restart it.

The Market chart uses Plotly candlesticks. Scroll over the chart or use the chart toolbar to zoom, drag to
pan, and use **Reset axes** in the toolbar to restore the full currently published candle range. Ordinary
snapshot refreshes preserve the interactive view. The chart metadata labels data freshness, the last closed
candle in UTC, and snapshot age. Tables use readable labels and explicit widths; wide tables remain
horizontally scrollable, and plan details are available in per-plan expanders. Stateful, keyed expanders
preserve their open/closed state across normal dashboard reruns and fragment refreshes. Market bias metrics
read the selected structure layer's anchor-timeframe value from the same per-timeframe map shown in the
Structure levels and zones table; unavailable anchor values remain unavailable. A sample with no closed
trades is labeled as having no closed trades rather than a verified zero-return period.

Start the bot normally with live execution left disabled (the default), then start the dashboard in a
second PowerShell window from the project root:

```powershell
python main.py
python -m streamlit run smc\dashboard\app.py --server.address 127.0.0.1
```

For a non-looping, dry-run smoke run, `python main.py --once` publishes one snapshot and exits; it does not
keep updating the dashboard after shutdown. The dashboard binds to loopback and should not be exposed to a
LAN. Installing dependencies in a project environment uses `python -m pip install -r requirements.txt`;
Streamlit is declared there. No separate API server or second MT5 connection is used.

### Refreshing the running bot's dashboard snapshot

The current publisher writes configured risk limits and session settings with each snapshot. If these fields
are absent, the dashboard process does not infer them from defaults: the snapshot may have been written by an
older bot process. Risk measurements remain unavailable until the bot has a successful cached risk/exposure
calculation; a missing measurement is not zero and does not mean risk checks passed.

To load the current publisher safely, use the console where the bot is already running:

1. Do not launch another `python main.py` process. If you cannot identify the existing bot console or cannot
   confirm that no other bot process is running, stop and resolve that first.
2. In the existing bot console, press **Ctrl+C once** and wait for the normal shutdown message and return to
   the prompt. Do not force-kill the process; this lets the journal and snapshot writer close cleanly.
3. From the project root in that same PowerShell window, force dry-run for this process and start exactly
   one bot:

   ```powershell
   $env:SMC_LIVE_EXECUTION = "false"
   python main.py
   ```

   This process-level setting takes precedence over `.env`; do not print, share, or edit `.env`. Do not start
   `--once` or another bot instance alongside it.
4. Continue only if its startup log explicitly reports **DRY-RUN (no orders are sent)**. If the log reports
   live execution enabled or the mode is unclear, press **Ctrl+C**, wait for shutdown, and do not proceed.
5. After the next publication interval, inspect only a safe metadata summary (not the complete snapshot):

   ```powershell
   python -c "import json; s=json.load(open(r'output\dashboard\runtime_snapshot.json', encoding='utf-8')); r=s.get('risk', {}); print({'generated_at_utc': s.get('generated_at_utc'), 'schema_version': s.get('schema_version'), 'session_fields': sorted((r.get('sessions') or {}).keys()), 'configured_limit_count': len(r.get('limits') or {}), 'risk_available': r.get('available'), 'risk_measurements': {k: r.get(k) for k in ('daily_trade_count', 'consecutive_losses', 'realised_pl_today', 'realised_loss_pct', 'total_drawdown_pct', 'baseline_balance', 'open_count', 'limits_ok')}})"
   ```

   Do not print or share the complete snapshot; it contains account and operational data. A false
   `risk_available` means risk measurements are unavailable, not zero and not a passing risk check.
6. Leave the dashboard running or open it in a separate console using the command above. The dashboard does
   not need to be restarted to read the updated snapshot.

Plan snapshots use the plan manager's current in-memory store, not log parsing. Terminal plans are retained
in the snapshot for lifecycle context; Overview displays only live statuses, and stale snapshots label those
statuses as last-known rather than active. Upgrade transitions keep the existing plan ID (they revise the
same plan) and journal the state, levels, entry model, and transition timestamp; flip-generated replacement
plans receive their own child plan ID. Plan state is still process-local and is not restored after restart,
but plan IDs are allocated durably from the journal database and checked against historical journal IDs.
Only one process may publish to a given snapshot path; additional publishers fail during startup.
Snapshot writes use unique same-directory temporary files and atomic replacement, with bounded retries for
transient Windows sharing violations. Dashboard reads retry transient file/JSON failures and report
persistent corruption explicitly.

Current market-structure state alone omits dead zones. New bounded journal events now capture structure
breaks, OB/FVG creation, mitigation, flips, invalidations, and liquidity sweeps as they are emitted by the
existing structure update pipeline. These are persisted idempotently and System Health queries the latest
100 matching records; no history is fabricated from old log lines. General bot events remain bounded to
the latest 30 records. Execution evaluations with a `dry_run` outcome are labeled explicitly as evaluations
with no order sent, and are not included in trade or performance counts.

The journal screen queries only the account and magic number from the latest reconciliation at or before
the snapshot time. It is bounded to 25–250 rows and uses parameterized read-only SQLite queries. Related
events are limited to broker order/deal/position identifiers; generic system events appear separately as
bot-process health context. Login is masked. Model/session attribution, validated equity curves, expectancy,
R-multiples, and full-history completeness are not claimed. Plans and structure remain in memory and are
not restored after restart. MT5 successful-but-truncated history responses remain unverifiable.

**Live execution remains disabled by default.** Do not enable live trading to run the dashboard.

## Phase 7 - integration, recovery and release readiness

The verified Phase 7 baseline was **304 passed**; the final Phase 7 full-suite run completed with
**315 passed**. See `PHASE7_COMPLETION.md` for the final suite result,
the exact integration/failure scenarios, modified files, and release checklist. The application performs an
initial startup reconciliation and repeats it at the configured `journal.reconciliation_interval_seconds`
(default 60 seconds) while running. Each successful history interval overlaps the last by one day so delayed
and out-of-order broker records can be recovered without re-counting stored deal tickets. A failed read or
write is recorded/logged; execution still performs its own independent account, position, order, risk, and
news checks, and uncertain order submissions remain subject to the existing reconciliation hold.

`ExecutionSystem.monitoring.snapshot()` includes live account connection/environment, the last successful
account snapshot time, current in-memory plans, bot-owned positions/orders, daily risk counters, active news
locks/freshness, journal schema/counts/last reconciliation, and recent execution/error/recovery events.
The snapshot is read-only with respect to broker trade actions. The dashboard publishes a market-data
freshness heartbeat, but plans are still not restored after process restart and should remain marked
process-local/unavailable until the bot rebuilds them.

There is **no separately configured weekend flatten policy** in the existing strategy. Current session
windows exclude weekends for new entries, while position deadlines continue to use the configured daily
strategy-timezone cutoff. No new weekend exit rule was introduced in Phase 7. Confirm whether a broker-
specific weekend exposure policy is needed before adding one.

### Safe Phase 7 validation

```powershell
python -m pytest -q
python -m compileall -q smc main.py tests
python main.py --help
python -m smc.journal --help
python demo_phase4.py
```

`demo_phase4.py` uses only the fake in-memory broker. It includes simulated-live behavior against that fake
only and cannot connect to a real terminal or send a real order.

### Demo-account checklist (manual, optional; never use a real account for release tests)

1. Do not display, share, or edit `.env` for this check. In the same PowerShell window, set
   `$env:SMC_LIVE_EXECUTION = "false"` before starting the bot to force this process to DRY-RUN.
2. Confirm MT5 is logged into a demo account; verify configured symbols, suffix, server UTC offset, and
   `STRATEGY_TIMEZONE` with the broker.
3. Run `python main.py --once`; confirm the log says DRY-RUN and broker order history is unchanged.
4. Inspect `python -m smc.journal status`; verify account-scoped reconciliation and current magic-number
   positions/orders match the demo terminal.
5. Trigger a provider outage only with a controlled local/mock provider; confirm the configured fail-safe
   policy and cached known event locks.
6. Export to a new file using the documented command and verify the six sheets; repeat to a different path.
7. Confirm monitoring reads show the same account, kill switch, position, news, and reconciliation state as
   the terminal without creating/modifying orders.
8. If simulated/demo order execution is ever separately authorized, perform it only under an approved
   procedure; this release does not require enabling live execution.

**Live execution remains disabled by default** and retains its existing two-factor environment opt-in and
real-account gate. No strategy thresholds or news/timezone defaults were changed.

### Tests and remaining decisions

Run the full offline regression suite using `python -m pytest -q`. Phase 5's reported baseline was verified
before Phase 6: **297 passed**. Phase 6's final full-suite run completed with **304 passed**. Phase 6 adds
automated coverage for migrations, UTC/idempotent events,
partial/cost aggregation, broker ownership, open positions, incomplete reads, repeated reconciliation,
reversal ambiguity, Excel sheets/overwrite protection, and read-only monitoring. Test details are recorded in `PHASE6_COMPLETION.md`.

Timezone/DST and calendar provider decisions remain configurable as described above. History completeness
depends on broker/terminal availability, and exact accounting for broker-specific netting/reversal records
remains intentionally unclaimed where MT5 data is ambiguous.
