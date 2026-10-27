from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from smc.journal.store import JournalStore
from smc.planning.plan_store import PlanStore


def test_plan_ids_survive_store_recreation_and_skip_existing_journal_ids(tmp_path):
    database = tmp_path / "journal.sqlite3"
    journal = JournalStore(database)
    journal.record_event(
        "plan_created",
        plan_id="PLAN-EURUSD-0007",
        symbol="EURUSD",
        idempotency_key="existing-eurusd-plan-7",
    )
    journal.record_event(
        "plan_created",
        plan_id="PLAN-EURUSD-0007",
        symbol="EURUSD",
        idempotency_key="duplicate-historical-plan-7",
    )
    journal.close()

    assert PlanStore(database).next_id("EURUSD") == "PLAN-EURUSD-0008"
    assert PlanStore(database).next_id("EURUSD") == "PLAN-EURUSD-0009"


def test_concurrent_plan_id_allocations_are_unique_across_stores(tmp_path):
    database = tmp_path / "journal.sqlite3"

    def allocate(_: int) -> str:
        return PlanStore(database).next_id("GBPUSD")

    with ThreadPoolExecutor(max_workers=8) as executor:
        identifiers = list(executor.map(allocate, range(32)))

    assert len(set(identifiers)) == 32
    assert set(identifiers) == {f"PLAN-GBPUSD-{n:04d}" for n in range(1, 33)}
