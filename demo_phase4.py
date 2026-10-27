"""Phase 4 demo on a FAKE in-memory broker. No MetaTrader, no network, no real orders - ever.

    python demo_phase4.py

Shows: dry-run (validate + size + order_check, nothing sent), then the same plan on the
fake broker with live mode simulated: limit order, fill, +1.5R partial, break-even,
the 23:00 flatten, and the kill switch.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from smc.models.plan import PlanStatus
from tests.harness import add_plan, make_env


def line(title: str) -> None:
    print(f"\n--- {title}")


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    print("FAKE BROKER ONLY - nothing here can reach a real account.")

    env = make_env(tmp / "dry")  # default config = dry-run
    plan = add_plan(env)
    line("1. DRY-RUN (the default): validate, size, order_check - nothing sent")
    res = env.system.engine.process_plan(plan)
    print(f"   outcome={res.outcome.value}  volume={res.sizing.volume}  risk=${res.sizing.actual_risk:.2f} "
          f"({res.sizing.risk_pct}% budget ${res.sizing.risk_amount:.2f})")
    print(f"   would send: {res.request['symbol']} type={res.request['type']} price={res.request['price']} "
          f"sl={res.request['sl']} tp={res.request['tp']}  | orders actually sent: {len(env.broker.sent)}")

    env = make_env(tmp / "sim", live=True)  # live flag on the FAKE broker
    plan = add_plan(env)
    line("2. Simulated live: resting limit order, then the plan is re-checked every cycle")
    res = env.system.engine.process_plan(plan)
    print(f"   {res.outcome.value}: order {res.order_ticket} comment={env.broker.orders[0].comment!r}")
    print(f"   duplicate attempt -> {env.system.engine.process_plan(plan).outcome.value} (idempotent)")

    line("3. Price returns to the zone: order fills, position is registered and managed")
    pos = env.broker.fill_pending(env.broker.orders[0].ticket)
    plan.status = PlanStatus.READY_TO_EXECUTE
    for a in env.system.manager.run():
        print(f"   {a.kind}: {a.note}")
    print(f"   plan status: {plan.status.value}; position {pos.volume} lots, SL {pos.sl}")

    line("4. Price reaches +1.5R: partial close, THEN break-even (only after it is confirmed)")
    env.broker.set_price(122.5, 122.55)  # entry 116, stop 112 -> 1.5R is 122.0
    for a in env.system.manager.run():
        print(f"   {a.kind} ok={a.ok}: {a.note}")
    p = env.broker.positions[0]
    print(f"   position now {p.volume} lots, SL {p.sl}")

    line("5. 23:00 strategy-timezone deadline: bot-managed exposure is closed")
    env.broker.advance(13 * 3600 + 5 * 60)
    for a in env.system.manager.run():
        print(f"   {a.kind} ok={a.ok}: {a.note}")
    print(f"   open positions left: {len(env.broker.positions)}")

    line("6. Kill switch: no new orders, ever, until released")
    env.system.kill.engage("demo")
    print(f"   new plan -> {env.system.engine.process_plan(add_plan(env)).outcome.value}")
    print(f"\nTotal orders sent to the fake broker: {len(env.broker.sent)}. Real orders sent: 0.")


if __name__ == "__main__":
    main()
