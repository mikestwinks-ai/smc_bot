# Phase 7 Completion Report

## 1. Baseline and final test results

- **Phase 7 starting baseline:** `python -m pytest -q` — **304 passed** in 25.39 seconds.
- **Final full suite:** `$env:PYTHON_DOTENV_DISABLED='1'; python -m pytest -q` — **315 passed** in 25.33 seconds.
- **Focused Phase 6/7 integration run:** **18 passed**.
- **Compilation:** `python -m compileall -q smc main.py tests` — passed.
- **CLI help:** `python main.py --help` and `python -m smc.journal --help` — both passed.
- **Safe dry-run/demo:** `python demo_phase4.py` — completed using the in-memory fake broker; it reported
  zero real orders. It exercised validation-only dry-run, fake pending order/fill, partial close, breakeven,
  cutoff flatten, and kill-switch block.
- Calendar and broker tests used fakes/mocks; no live MT5 account or external provider request was used.
- The sanitized release archive was integrity-checked and contains 93 source/config-example/documentation/test
  files. Validation confirmed there is no `.env`, database, workbook, log, cache, virtual environment, or
  generated `output`/`data` folder in the archive.

## 2. End-to-end and failure scenarios tested

The end-to-end fake-broker test exercises plan qualification/execution, a confirmed fill, position
management with a partial exit, SQLite reconciliation, and monitoring of the current open position.
Other focused recovery scenarios cover:

- MT5 disconnect/reconnect during analysis, position management, and history reads.
- Restart with a persisted uncertain order intent; while the broker hold period is active, no duplicate
  order is sent.
- Restart with a pending order but no restored plan; the manager cancels it because strategy conditions
  cannot be verified.
- Reconciliation failures preserve the prior snapshot; delayed/out-of-order deal history is recovered via
  the one-day overlap and ticket-level idempotency.
- An entry with neither a known closing deal nor a live position is classified `unknown`, not asserted open
  or closed.
- SQLite write and Excel output failures do not mutate broker state or bypass dry-run/risk gates.
- Monitoring account/position read errors surface instead of returning a success-shaped empty snapshot.
- Kill-switch activation blocks execution, and release returns to the existing dry-run gate.
- News provider timeout with a known event retains the event lock; provider recovery updates the snapshot.

The retained Phase 1–6 suite also covers all four entry models, structure and zone logic, P/D filters,
risk sizing/limits, stop-management constraints, session cutoffs, MT5 request handling, calendar frequency/
cache/freshness and lock boundaries, immediate pre-submit news recheck, mocked HTTP provider responses,
Telegram failure/redaction, export overwrite protection, and journal migrations/aggregation.

## 3. Files added and modified

**Added**

- `tests/test_phase7_integration.py` — integration, reconnect, recovery, status, safety, and failure tests.
- `PHASE7_COMPLETION.md` — this report.

**Modified**

- `README.md` — Phase 7 operation, monitoring/dashboard gaps, safe validation, and demo checklist.
- `smc/config.py` — configurable 60-second periodic reconciliation interval and validation.
- `smc/execution/system.py` — scheduled reconciliation with overlapping history and explicit diagnostics.
- `smc/journal/reconciliation.py` — preserve absent broker timestamps and classify unverified orphan entries
  as unknown.
- `smc/journal/store.py` — current journal-health/status queries and synchronized read access.
- `smc/journal/monitoring.py` — clearer account update timestamp and journal/reconciliation health fields.
- `main.py` — catch and record startup reconciliation filesystem failures.

No Phase 1–6 strategy threshold, entry model, risk default, news-lock default, timezone, or execution gate was
changed. The directory has no Git metadata; no branch or commit was created or claimed. `.env` was not manually
inspected, printed, or modified. The existing `smc.config` module calls `load_dotenv` on import; the initial
baseline command did not disable that behavior, although it did not connect to MT5 or print environment
values. The final suite and CLI-help checks were isolated with `PYTHON_DOTENV_DISABLED=1`.

## 4. Bugs found and corrected

1. **Reconciliation was only invoked at startup or manually.** The running system now reconciles at the
   configurable `journal.reconciliation_interval_seconds` (default 60 seconds), beginning one day before
   the last successful interval to recover delayed history. Broker deal tickets remain idempotent.
2. **A historical entry without a current position or sufficient exit history could be labeled open.**
   Such records are now `unknown` until evidence establishes open/closed status.
3. **Monitoring could omit recent executions when unrelated events crowded its general recent-event list.**
   Execution and error/recovery events are now queried by event type and exposed alongside journal counts,
   current bot exposure counts, schema version, and last reconciliation.
4. **Missing historical order setup time could be serialized as the current time.** Missing timestamps now
   remain NULL in broker order records; the event timestamp has an explicit reconciliation-time fallback.

The existing gateway still reconnects before MT5 operations. Risk and news validation remain in the
independent execution pipeline, and uncertain outcomes still wait for reconciliation rather than automatic
resubmission.

## 5. Remaining limitations and unresolved decisions

- MT5 does not provide a universal completeness marker for a successful but truncated history response.
  Explicit API errors fail the reconciliation; terminal history visibility still must be verified locally.
- Plans remain in the existing in-memory `PlanStore` and are not reconstructed after restart. A pending order
  whose plan cannot be verified is canceled fail-safe. A future dashboard should display plan restoration
  status rather than assume the plan list is restart-persistent.
- The monitoring API exposes the last successful **account** snapshot time, not an end-to-end market-data
  freshness heartbeat. Market-data update age/lag should be explicitly wired before dashboard use.
- No standalone weekend flatten/exposure policy exists in the project settings. Existing weekday session
  entry windows and configured daily strategy-timezone cutoff remain unchanged; Phase 7 did not invent a
  weekend exit rule.
- Broker magic-number attribution and successful-but-truncated history remain broker/terminal limitations.
  Ambiguous reversal records stay `unknown`.
- Calendar provider and timezone/DST settings remain configurable. The ForexFactory community feed is
  unofficial and not guaranteed; this phase made no real provider requests.
- No web dashboard or live/demo broker validation was performed.

## 6. Safe local validation commands

```powershell
$env:PYTHON_DOTENV_DISABLED='1'
python -m pytest -q
python -m compileall -q smc main.py tests
python main.py --help
python -m smc.journal --help
python demo_phase4.py
python -m smc.journal status
python -m smc.journal export --output "output\exports\journal-phase7.xlsx"
```

`demo_phase4.py` uses a fake in-memory broker. `python -m smc.journal status` and export use the local
journal only. Reconciliation requires a locally configured MT5 terminal and is a read-only broker operation:

```powershell
python -m smc.journal reconcile
```

## 7. Demo-account validation checklist

1. Review credentials locally only; do not share or commit `.env`. Keep `SMC_LIVE_EXECUTION` false/unset.
2. Verify MT5 is connected to a **demo** account, along with symbol suffix, broker UTC offset, and
   configured strategy timezone.
3. Run `python main.py --once`; confirm DRY-RUN output and unchanged broker order history.
4. Compare `python -m smc.journal status` and `ExecutionSystem.monitoring.snapshot()` with the demo
   terminal's account, magic-number positions/orders, news status, and last reconciliation.
5. With an approved mock/local news source, simulate provider timeout and recovery; do not hit the
   community feed for this test.
6. Export to a new `.xlsx` path, check all six sheets, and confirm a second export does not overwrite.
7. Confirm the kill switch blocks entries and that no monitoring/export action creates, modifies, or closes
   trades.
8. No real-order test is part of this checklist. If separate demo execution is ever approved, follow the
   project's existing explicit opt-in procedure and broker supervision.

## 8. Live execution

**Live execution remains disabled by default.** Existing double opt-in, real-account refusal, risk/news
checks, kill switch, position safeguards, and dry-run behavior remain intact. The demo used for CLI
validation was an in-memory fake broker; real orders sent: **0**.
