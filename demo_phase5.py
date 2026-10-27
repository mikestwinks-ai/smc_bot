"""Phase 5 demo - fake broker, fake calendar, console 'Telegram'. No network, no real orders.

    python demo_phase5.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd

from smc.news import NewsMonitor
from smc.notify.router import NotificationRouter
from smc.planning import ChangeKind, PlanChange
from tests.harness import add_plan
from tests.news_helpers import RecordingNotifier, ev, news_env, ts


class ConsoleNotifier(RecordingNotifier):
    def notify(self, text, *, key=None, persist=True):
        ok = super().notify(text, key=key, persist=persist)
        if ok:
            print("   [telegram] " + text.replace("\n", "\n              "))
        return ok


def main() -> None:
    print("FAKE broker + FAKE calendar + console output. Nothing leaves this machine.")
    events = [ev("2026-01-07 13:30", title="Core CPI m/m", forecast="0.3%", previous="0.2%"),
              ev("2026-01-07 13:30", title="CPI m/m", forecast="0.3%", previous="0.2%")]
    env, guard, svc, prov = news_env(Path(tempfile.mkdtemp()), events, live=True, supports_actuals=True)
    note = ConsoleNotifier(env.system.state)
    router = NotificationRouter(note, env.cfg, env.system)
    mon = NewsMonitor(guard, env.cfg.news, note, "UTC", now_fn=lambda: env.broker.now)

    def at(when: str) -> None:
        env.broker.now = ts(when)
        print(f"\n=== {when} UTC")
        mon.tick(ts(when))

    at("2026-01-07 10:00")
    plan = add_plan(env)
    router.plan_changes([PlanChange(ChangeKind.CREATED, env.now, plan.plan_id, plan.reason)])
    res = env.system.engine.process_plan(plan)
    print(f"   execution: {res.outcome.value} (limit order expires at the next news lock: "
          f"{pd.Timestamp(env.broker.orders[0].time_expiration, unit='s', tz='UTC'):%H:%M} UTC)")

    at("2026-01-07 12:35")
    at("2026-01-07 13:21")
    print("   guard says:", guard.check("XAUUSD", env.now).message())
    acts = env.system.manager.run()
    router.cycle(type("R", (), {"actions": acts, "results": [], "errors": []})(), env.now)
    print(f"   resting orders left: {len(env.broker.orders)}; new entry now -> "
          f"{env.system.engine.process_plan(add_plan(env)).reasons[0][:70]}...")

    prov.events = [ev("2026-01-07 13:30", title="Core CPI m/m", forecast="0.3%", previous="0.2%", actual="0.5%"),
                   ev("2026-01-07 13:30", title="CPI m/m", forecast="0.3%", previous="0.2%", actual="0.4%")]
    env.broker.now = ts("2026-01-07 13:36")
    svc.refresh(force=True)
    at("2026-01-07 13:36")
    at("2026-01-07 13:41")
    print(f"\nOrders sent to the broker by news logic: 0 new; plans created from news: 0.")


if __name__ == "__main__":
    main()
