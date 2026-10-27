"""Phase 4 - sessions & cutoffs, kill switch, live-execution gating, secrets hygiene."""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pytest

from smc.config import (
    AssetClass, BotConfig, ConfigError, LIVE_CONFIRM_PHRASE, SymbolConfig, load_config,
)
from smc.execution import KillSwitch, LiveTradingDisabledError, Outcome
from smc.models.plan import PlanStatus
from smc.risk import SessionClock
from tests.harness import BSYM, add_plan, make_env
from tests.test_phase4_management import closes, open_long, place_pending

UTC = lambda s: pd.Timestamp(s, tz="UTC")  # noqa: E731
GOLD, BTC = SymbolConfig("XAUUSD", AssetClass.METAL, "m"), SymbolConfig("BTCUSD", AssetClass.CRYPTO, "m")
ROOT = Path(__file__).resolve().parent.parent


# ============================================== 19: sessions and the 22:30 cutoff
def test_entries_are_blocked_after_2230_in_the_configured_timezone(tmp_path):
    # Tokyo is UTC+9: 13:25 UTC = 22:25 JST (allowed), 13:45 UTC = 22:45 JST (blocked) - both inside the NY session
    ok_env = make_env(tmp_path, now="2026-01-07 13:25", cutoff_tz="Asia/Tokyo")
    assert ok_env.system.engine.process_plan(add_plan(ok_env)).outcome is Outcome.DRY_RUN
    late = make_env(tmp_path / "late", now="2026-01-07 13:45", cutoff_tz="Asia/Tokyo")
    res = late.system.engine.process_plan(add_plan(late))
    assert res.outcome is Outcome.REJECTED and "no-new-trades cutoff" in " ".join(res.reasons)
    assert "Asia/Tokyo" in " ".join(res.reasons)


def test_cutoff_boundary_is_exact_and_timezone_explicit():
    clock = SessionClock(BotConfig().sessions)  # UTC by default
    assert clock.timezone_name == "UTC"
    assert not clock.after_no_new_trades(UTC("2026-01-07 22:29:59"), GOLD)
    assert clock.after_no_new_trades(UTC("2026-01-07 22:30:00"), GOLD)
    cfg = BotConfig()
    cfg.sessions.cutoff_timezone = "Africa/Kampala"  # EAT, UTC+3, no DST
    kamp = SessionClock(cfg.sessions)
    assert kamp.after_no_new_trades(UTC("2026-01-07 19:30"), GOLD)  # 22:30 EAT
    assert not kamp.after_no_new_trades(UTC("2026-01-07 19:29"), GOLD)


@pytest.mark.parametrize("when,allowed", [
    ("2026-01-07 07:59", False), ("2026-01-07 08:00", True),  # London winter = UTC
    ("2026-01-07 13:00", True),  # overlap (London + NY)
    ("2026-01-07 21:59", True), ("2026-01-07 22:00", False),  # NY winter closes 22:00 UTC
    ("2026-07-08 07:30", True), ("2026-07-08 06:59", False),  # London summer time: 08:00 BST = 07:00 UTC
    ("2026-07-08 20:59", True), ("2026-07-08 21:00", False),  # NY summer time closes 21:00 UTC
    ("2026-01-10 10:00", False), ("2026-01-11 14:00", False),  # weekend
])
def test_session_windows_are_dst_aware(when, allowed):
    assert SessionClock(BotConfig().sessions).in_session(UTC(when))[0] is allowed


def test_trading_day_boundary_and_flatten_deadline():
    clock = SessionClock(BotConfig().sessions)
    assert clock.trading_day_key(UTC("2026-01-07 23:59")) == "2026-01-07"
    assert clock.trading_day_key(UTC("2026-01-08 00:00")) == "2026-01-08"
    assert clock.close_deadline(UTC("2026-01-07 10:00"), GOLD) == UTC("2026-01-07 23:00")
    assert clock.close_deadline(UTC("2026-01-07 23:30"), GOLD) == UTC("2026-01-07 23:30")  # already overdue: immediate
    assert clock.close_deadline(UTC("2026-01-07 10:00"), BTC) is None  # existing crypto exemption
    assert clock.pending_expiry(UTC("2026-01-07 10:00"), GOLD) == UTC("2026-01-07 22:30")
    assert clock.pending_expiry(UTC("2026-01-07 23:00"), GOLD) is None


def test_unknown_timezone_is_a_config_error():
    cfg = BotConfig()
    cfg.sessions.cutoff_timezone = "Mars/Olympus"
    with pytest.raises(ConfigError):
        cfg.validate()


def test_a_delayed_execution_request_cannot_bypass_the_cutoff(tmp_path):
    env = make_env(tmp_path, live=True, now="2026-01-07 13:25", cutoff_tz="Asia/Tokyo")
    plan = add_plan(env)
    clocks = iter([env.now, env.now + pd.Timedelta(minutes=30)])  # first look: allowed; the re-check: 22:55 JST
    real_now = env.system.gateway.now
    env.system.gateway._now = lambda: next(clocks, env.now + pd.Timedelta(minutes=30))
    res = env.system.engine.process_plan(plan)
    assert res.outcome is Outcome.REJECTED and env.broker.sent == []
    assert "final re-validation failed" in " ".join(res.reasons) or "cutoff" in " ".join(res.reasons)


# ================================================================ 21: kill switch
def test_kill_switch_prevents_new_orders_in_every_mode(tmp_path):
    for live in (False, True):
        env = make_env(tmp_path / str(live), live=live)
        env.system.kill.engage("operator pressed stop")
        res = env.system.engine.process_plan(add_plan(env))
        assert res.outcome is Outcome.BLOCKED and "kill switch" in " ".join(res.reasons)
        assert env.broker.sent == [] and env.broker.checked == []
        assert env.system.run_cycle().results == []
        env.system.kill.release()
        assert env.system.engine.process_plan(add_plan(env)).outcome is not Outcome.BLOCKED


def test_kill_switch_file_works_and_is_released_by_deleting_it(tmp_path):
    env = make_env(tmp_path, live=True)
    plan = add_plan(env)
    (tmp_path / "KILL_SWITCH").write_text("stop")
    assert env.system.engine.process_plan(plan).outcome is Outcome.BLOCKED
    (tmp_path / "KILL_SWITCH").unlink()
    assert env.system.engine.process_plan(plan).outcome is Outcome.PENDING_PLACED
    ks = KillSwitch(tmp_path / "other")
    assert not ks.engaged
    ks.engage("x")
    assert ks.engaged and "x" in ks.reason


def test_kill_switch_still_allows_position_management_and_cancels_pendings(tmp_path):
    env = make_env(tmp_path, live=True)
    plan, pos = open_long(env)
    env.system.kill.engage("manual")
    env.broker.set_price(120.0, 120.05)
    acts = env.system.run_cycle().actions
    assert [a.kind for a in acts] == ["partial", "breakeven"]  # risk reduction continues
    assert env.broker.positions[0].sl == 115.05 and len(closes(env)) == 1


# ============================================ 22: live orders are off by default
def test_default_configuration_cannot_send_live_orders(tmp_path, monkeypatch):
    for var in ("SMC_LIVE_EXECUTION", "SMC_LIVE_CONFIRM", "SMC_ALLOW_REAL_ACCOUNT"):
        monkeypatch.delenv(var, raising=False)
    assert BotConfig().execution.live_enabled is False
    assert load_config().execution.live_enabled is False
    env = make_env(tmp_path)  # default = dry-run
    assert env.system.gateway.live is False
    with pytest.raises(LiveTradingDisabledError):
        env.system.gateway.send({"action": 1})
    res = env.system.engine.process_plan(add_plan(env))
    assert res.outcome is Outcome.DRY_RUN and env.broker.sent == [] and len(env.broker.checked) >= 1
    for _ in range(3):
        env.system.run_cycle()
    assert env.broker.sent == [] and env.broker.orders == [] and env.broker.positions == []


def test_live_switch_needs_both_env_vars_and_the_exact_phrase(monkeypatch):
    for var in ("SMC_LIVE_EXECUTION", "SMC_LIVE_CONFIRM"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SMC_LIVE_EXECUTION", "true")
    assert load_config().execution.live_enabled is False  # confirmation phrase missing
    monkeypatch.setenv("SMC_LIVE_CONFIRM", "yes")
    assert load_config().execution.live_enabled is False  # wrong phrase
    monkeypatch.setenv("SMC_LIVE_CONFIRM", LIVE_CONFIRM_PHRASE)
    assert load_config().execution.live_enabled is True
    monkeypatch.setenv("SMC_LIVE_EXECUTION", "false")
    assert load_config().execution.live_enabled is False


def test_live_mode_still_refuses_real_accounts_unless_explicitly_allowed(tmp_path):
    env = make_env(tmp_path, live=True, real=True)
    res = env.system.engine.process_plan(add_plan(env))
    assert res.outcome is Outcome.BLOCKED and "REAL account" in " ".join(res.reasons) and env.broker.sent == []
    env.cfg.execution.allow_real_account = True
    assert env.system.engine.process_plan(add_plan(env)).outcome is Outcome.PENDING_PLACED


def test_dry_run_management_logs_but_never_trades(tmp_path):
    env = make_env(tmp_path)  # dry-run
    env.broker.add_open_position(BSYM, True, 0.08, 115.05, 112.0, 150.2, env.cfg.execution.magic_number,
                                 comment="PLAN-DEMO|100000")
    env.broker.set_price(120.0, 120.05)
    acts = env.system.manager.run()
    assert any(a.kind == "partial" and "dry-run" in a.note for a in acts)
    assert env.broker.sent == [] and env.broker.positions[0].volume == 0.08
    mp = env.system.state.get_managed(env.broker.positions[0].ticket)
    assert not mp.partial_done and not mp.be_done  # nothing recorded as done


# ===================================================== secrets and repo hygiene
def test_env_file_is_ignored_and_no_secret_is_hard_coded():
    ignore = (ROOT / ".gitignore").read_text()
    assert ".env" in ignore.splitlines() and "KILL_SWITCH" in ignore.splitlines()
    pattern = re.compile(r"(?i)(password|passwd|token|secret|api[_-]?key)\s*=\s*[\"'][^\"']{6,}[\"']")
    offenders = []
    for path in list((ROOT / "smc").rglob("*.py")) + [ROOT / "main.py", ROOT / ".env.example"]:
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.name}:{n}")
    assert offenders == []
    example = (ROOT / ".env.example").read_text()
    assert re.search(r"^SMC_LIVE_EXECUTION=\s*$", example, re.M) and re.search(r"^SMC_LIVE_CONFIRM=\s*$", example, re.M)


def test_state_file_is_atomic_and_survives_corruption(tmp_path):
    from smc.execution import StateStore
    path = tmp_path / "s.json"
    s = StateStore(path)
    s.set_intent("k", status="placed", attempts=1)
    assert StateStore(path).get_intent("k")["status"] == "placed"
    path.write_text("{ not json")
    fresh = StateStore(path)  # corrupt file is set aside, bot starts safe and re-reconciles with MT5
    assert fresh.get_intent("k") is None and path.with_suffix(".corrupt").exists()
