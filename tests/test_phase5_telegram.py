"""Phase 5 - Telegram: disabled mode, secrets, retries, dedupe, non-blocking, no effect on trading."""

from __future__ import annotations

import json
import logging
import threading
import time

import pandas as pd
import pytest

from smc.config import BotConfig, TelegramConfig, load_config
from smc.execution import StateStore
from smc.execution.execution_engine import Outcome
from smc.notify import NullNotifier, TelegramNotifier, build_notifier
from smc.notify.router import NotificationRouter
from smc.utils.logger import setup_logging
from tests.harness import add_plan
from tests.news_helpers import RecordingNotifier, news_env

TOKEN = "123456789:AAEhBP0av28GDoRCx3x9yY4hZ7wQ9k1LmNo"


def cfg(**kw) -> TelegramConfig:
    base = dict(enabled=True, bot_token=TOKEN, chat_id="42", async_send=False, max_retries=2, retry_cooldown_seconds=300)
    base.update(kw)
    return TelegramConfig(**base)


class Transport:
    """Scriptable transport: a list of (status, body) or exceptions, consumed in order."""

    def __init__(self, *script):
        self.script, self.calls = list(script), []

    def __call__(self, url, body, timeout):
        self.calls.append((url, json.loads(body), timeout))
        item = self.script.pop(0) if self.script else (200, {"ok": True})
        if isinstance(item, Exception):
            raise item
        return item


def notifier(transport=None, state=None, sleeps=None, **kw):
    sleeps = sleeps if sleeps is not None else []
    return TelegramNotifier(cfg(**kw), state, transport or Transport(), sleep_fn=sleeps.append,
                            now_fn=lambda: pd.Timestamp("2026-01-07 10:00", tz="UTC")), sleeps


# ------------------------------------------------------------------- disabled
def test_disabled_telegram_never_crashes(tmp_path):
    n = build_notifier(TelegramConfig(enabled=False))
    assert isinstance(n, NullNotifier) and n.notify("hello", key="k") is False
    n.close()
    assert isinstance(build_notifier(TelegramConfig(enabled=True, bot_token="", chat_id="1")), NullNotifier)


def test_config_reads_token_and_chat_from_the_environment_only(monkeypatch):
    for v in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(v, raising=False)
    assert load_config().telegram.enabled is False
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    assert load_config().telegram.enabled is False  # token alone is not enough
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    c = load_config().telegram
    assert c.enabled and c.bot_token == TOKEN and TOKEN not in repr(c)  # repr hides the token


# --------------------------------------------------------------------- sending
def test_message_goes_to_the_right_chat_with_a_timeout():
    t = Transport()
    n, _ = notifier(t)
    assert n.notify("NEWS LOCK ON\nCPI") and n.sent == 1
    url, payload, timeout = t.calls[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert payload["chat_id"] == "42" and payload["text"] == "NEWS LOCK ON\nCPI" and timeout == 5.0


def test_long_messages_are_truncated_to_telegrams_limit():
    t = Transport()
    n, _ = notifier(t)
    n.notify("x" * 9000)
    assert len(t.calls[0][1]["text"]) <= 4000


# ----------------------------------------------------------------- secrets
def test_token_is_redacted_from_text_errors_and_logs(caplog, tmp_path):
    t = Transport(RuntimeError(f"cannot reach https://api.telegram.org/bot{TOKEN}/sendMessage"))
    n, _ = notifier(t, max_retries=0)
    with caplog.at_level(logging.DEBUG, logger="smc"):
        n.notify(f"debug dump {TOKEN}")
    assert TOKEN not in t.calls[0][1]["text"] and "[REDACTED-TOKEN]" in t.calls[0][1]["text"]
    assert TOKEN not in n.last_error and TOKEN not in caplog.text and n.failed == 1
    # the file/console handlers also mask anything token-shaped, even from other modules
    setup_logging(tmp_path / "logs", "t.log", "INFO", console=False)
    logging.getLogger("smc.test").info("oops %s", f"bot{TOKEN}")
    for h in logging.getLogger("smc").handlers:
        h.flush()
    assert TOKEN not in (tmp_path / "logs" / "t.log").read_text()


# ---------------------------------------------------------- retries & timeouts
def test_server_errors_are_retried_with_backoff_then_succeed():
    t = Transport((502, {}), (500, {}), (200, {"ok": True}))
    n, sleeps = notifier(t)
    assert n.notify("hi") and n.sent == 1 and len(t.calls) == 3 and sleeps == [1, 2]


def test_rate_limit_retry_after_is_honoured_but_capped():
    t = Transport((429, {"parameters": {"retry_after": 3}}), (429, {"parameters": {"retry_after": 99}}), (200, {"ok": True}))
    n, sleeps = notifier(t)
    n.notify("hi")
    assert sleeps == [3, 10] and n.sent == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_permanent_errors_are_not_retried(status):
    t = Transport((status, {"description": "Unauthorized"}))
    n, sleeps = notifier(t)
    assert n.notify("hi") and n.failed == 1 and len(t.calls) == 1 and sleeps == []


def test_timeouts_and_network_errors_are_contained():
    t = Transport(TimeoutError("timed out"), ConnectionError("down"), TimeoutError("again"))
    n, sleeps = notifier(t)
    n.notify("hi")  # must not raise
    assert n.failed == 1 and len(t.calls) == 3 and "TimeoutError" in n.last_error


# ---------------------------------------------------------------------- dedupe
def test_same_key_is_sent_once_and_survives_a_restart(tmp_path):
    state = StateStore(tmp_path / "s.json")
    t = Transport()
    n, _ = notifier(t, state)
    assert n.notify("lock on", key="lock_on:E1") and not n.notify("lock on", key="lock_on:E1")
    assert n.notify("lock on", key="lock_on:E2")
    reborn, _ = notifier(t, StateStore(tmp_path / "s.json"))  # process restart
    assert not reborn.notify("lock on", key="lock_on:E1") and len(t.calls) == 2
    assert n.notify("always", key=None) and n.notify("always", key=None)  # keyless alerts are never suppressed


def test_failed_alert_is_held_back_for_a_cooldown_not_hammered():
    t = Transport((500, {}), (500, {}), (500, {}))
    n, _ = notifier(t)
    n.notify("x", key="k")
    calls = len(t.calls)
    assert not n.notify("x", key="k") and len(t.calls) == calls  # cool-down
    n._now = lambda: pd.Timestamp("2026-01-07 10:06", tz="UTC")
    assert n.notify("x", key="k")  # allowed again after the cool-down


# ----------------------------------------------------------------- not blocking
def test_async_notify_returns_immediately_even_when_telegram_hangs():
    gate = threading.Event()

    def slow(url, body, timeout):
        gate.wait(5)
        return 200, {"ok": True}

    n = TelegramNotifier(cfg(async_send=True, queue_size=3), None, slow)
    t0 = time.monotonic()
    accepted = [n.notify(f"m{i}") for i in range(10)]
    assert time.monotonic() - t0 < 0.5  # the trading loop never waits
    assert n.dropped >= 5 and accepted.count(False) == n.dropped  # bounded queue: overflow is dropped and counted
    gate.set()
    n.close(timeout=3)


# --------------------------------- Telegram can never influence trading
def test_telegram_failure_does_not_change_orders_or_trigger_a_retry(tmp_path):
    env, guard, *_ = news_env(tmp_path, [], live=True)
    failing = TelegramNotifier(cfg(max_retries=1), env.system.state, Transport(*([(500, {})] * 40)), sleep_fn=lambda s: None)
    router = NotificationRouter(failing, env.cfg, env.system)
    plan = add_plan(env)
    for _ in range(4):  # several full cycles with Telegram completely down
        router.cycle(env.system.run_cycle())
    assert failing.failed >= 1
    assert len(env.broker.sent) == 1 and len(env.broker.orders) == 1  # exactly one order, never re-sent
    assert env.system.engine.process_plan(plan).outcome is Outcome.ALREADY


def test_a_successful_alert_is_not_proof_of_anything_either(tmp_path):
    env, *_ = news_env(tmp_path, [], live=False)
    good = RecordingNotifier(env.system.state)
    router = NotificationRouter(good, env.cfg, env.system)
    add_plan(env)
    router.cycle(env.system.run_cycle())
    assert env.broker.sent == [] and not good.texts("ORDER")  # dry-run: no order alert, no order
