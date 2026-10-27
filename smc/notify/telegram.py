"""Telegram delivery. Alerts are INFORMATION ONLY - nothing in the bot depends on their delivery.

* Token / chat id come from the environment (``TelegramConfig``); the token is never logged -
  the URL is built only inside ``_post`` and every error text is redacted.
* Non-blocking: ``notify`` only queues (bounded). A background thread does the HTTP with a short
  timeout and a few retries (backoff, honours 429 ``retry_after``). When the queue is full the
  alert is dropped and counted - the trading loop never waits for Telegram.
* De-duplication: an alert with a ``key`` is sent once; delivered keys are remembered in the
  state file so a restart cannot repeat them. A failed key is held back for a cool-down.
* A failed send changes nothing else: no order is ever retried, confirmed or cancelled because
  of Telegram.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

import pandas as pd

from smc.config import TelegramConfig
from smc.utils.logger import _TOKEN_RE, get_logger

log = get_logger(__name__)

Transport = Callable[[str, bytes, float], "tuple[int, dict[str, Any]]"]
MAX_LEN = 4000  # Telegram's limit is 4096


def urllib_transport(url: str, body: bytes, timeout: float) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https host
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


class Notifier:
    """Interface used everywhere else. The null implementation makes 'Telegram off' trivial."""

    enabled = False

    def notify(self, text: str, *, key: Optional[str] = None, persist: bool = True) -> bool:
        return False

    def close(self, timeout: float = 2.0) -> None:
        return None


class NullNotifier(Notifier):
    pass


class TelegramNotifier(Notifier):
    enabled = True

    def __init__(self, cfg: TelegramConfig, state: Any = None, transport: Optional[Transport] = None,
                 sleep_fn: Callable[[float], None] = time.sleep,
                 now_fn: Optional[Callable[[], pd.Timestamp]] = None) -> None:
        self._cfg, self._state = cfg, state
        self._transport = transport or urllib_transport
        self._sleep = sleep_fn
        self._now = now_fn or (lambda: pd.Timestamp.now(tz="UTC"))
        self._inflight: set[str] = set()
        self._cooldown: dict[str, pd.Timestamp] = {}
        self._lock = threading.Lock()
        self.sent = self.failed = self.dropped = 0
        self.last_error = ""
        self._q: Optional[queue.Queue] = None
        self._thread: Optional[threading.Thread] = None
        if cfg.async_send:
            self._q = queue.Queue(maxsize=cfg.queue_size)
            self._thread = threading.Thread(target=self._worker, name="telegram-sender", daemon=True)
            self._thread.start()

    # ------------------------------------------------------------------ public
    def redact(self, text: str) -> str:
        out = text.replace(self._cfg.bot_token, "[REDACTED-TOKEN]") if self._cfg.bot_token else text
        return _TOKEN_RE.sub("[REDACTED-TOKEN]", out)

    def notify(self, text: str, *, key: Optional[str] = None, persist: bool = True) -> bool:
        """Queue (or, in sync mode, send) one alert. Returns True if it was accepted for delivery."""
        if key is not None:
            with self._lock:
                if key in self._inflight:
                    return False
                if persist and self._state is not None and self._state.is_notified(key):
                    return False
                until = self._cooldown.get(key)
                if until is not None and self._now() < until:
                    return False
                self._inflight.add(key)
        msg = self.redact(text)[:MAX_LEN]
        if self._q is None:
            self._deliver(key, msg, persist)
            return True
        try:
            self._q.put_nowait((key, msg, persist))
        except queue.Full:
            self.dropped += 1
            self._release(key)
            log.warning("Telegram queue full: alert dropped (%d dropped so far)", self.dropped)
            return False
        return True

    def close(self, timeout: float = 2.0) -> None:
        if self._q is not None and self._thread is not None:
            try:
                self._q.put_nowait(None)
            except queue.Full:
                pass
            self._thread.join(timeout)

    # ---------------------------------------------------------------- internals
    def _worker(self) -> None:
        assert self._q is not None
        while True:
            item = self._q.get()
            if item is None:
                return
            try:
                self._deliver(*item)
            except Exception as exc:  # never let the worker die
                log.error("Telegram worker error: %s", self.redact(type(exc).__name__))

    def _release(self, key: Optional[str]) -> None:
        if key is not None:
            with self._lock:
                self._inflight.discard(key)

    def _deliver(self, key: Optional[str], text: str, persist: bool) -> None:
        ok = self._send_with_retries(text)
        if ok:
            self.sent += 1
            if key is not None and persist and self._state is not None:
                self._state.mark_notified(key, self._now().isoformat())
        else:
            self.failed += 1
            if key is not None:
                with self._lock:
                    self._cooldown[key] = self._now() + pd.Timedelta(seconds=self._cfg.retry_cooldown_seconds)
        self._release(key)

    def _send_with_retries(self, text: str) -> bool:
        attempts = self._cfg.max_retries + 1
        for n in range(1, attempts + 1):
            status, body, err = self._post(text)
            if status == 200 and body.get("ok", True):
                return True
            self.last_error = self.redact(err or f"HTTP {status} {body.get('description', '')}".strip())
            permanent = status in (400, 401, 403, 404)  # bad token / chat / request: retrying cannot help
            if permanent or n == attempts:
                log.error("Telegram delivery failed (%s): %s", "permanent" if permanent else f"after {n} attempts",
                          self.last_error)
                return False
            delay = float(body.get("parameters", {}).get("retry_after", 0)) if status == 429 else 2 ** (n - 1)
            self._sleep(min(max(delay, 0.0), 10.0))
        return False

    def _post(self, text: str) -> tuple[int, dict[str, Any], str]:
        url = f"https://api.telegram.org/bot{self._cfg.bot_token}/sendMessage"
        payload = json.dumps({"chat_id": self._cfg.chat_id, "text": text,
                              "disable_web_page_preview": True}).encode("utf-8")
        try:
            status, body = self._transport(url, payload, self._cfg.timeout_seconds)
            return status, body, ""
        except Exception as exc:  # network / timeout: type name only, never the URL
            return 0, {}, f"transport error: {type(exc).__name__}"


def build_notifier(cfg: TelegramConfig, state: Any = None, **kw: Any) -> Notifier:
    if not cfg.enabled or not cfg.bot_token or not cfg.chat_id:
        log.info("Telegram notifications are disabled (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not both set).")
        return NullNotifier()
    return TelegramNotifier(cfg, state, **kw)
