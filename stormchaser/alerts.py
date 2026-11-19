"""Alerts: pluggable notifiers + a debounced alert manager.

Telegram is enabled only when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set; otherwise it is a no-op.
A notifier failure never propagates into the scoring loop.
"""
from __future__ import annotations

import logging
import os
import time
from collections import deque
from typing import Callable, Protocol

from .score import LEVELS

log = logging.getLogger("stormchaser.alerts")
ICON = {"green": "\U0001F7E2", "watch": "\U0001F7E1", "warning": "\U0001F7E0", "tornado": "\U0001F32A\ufe0f",
        "stale": "\U0001F50C"}


class Notifier(Protocol):
    name: str

    def send(self, text: str) -> bool: ...


class NullNotifier:
    name = "null"

    def send(self, text: str) -> bool:
        return False


class LogNotifier:
    name = "log"

    def send(self, text: str) -> bool:
        log.warning("ALERT %s", text)
        return True


class TelegramNotifier:
    name = "telegram"

    def __init__(self, token: str | None = None, chat_id: str | None = None, post: Callable | None = None,
                 timeout: float = 5.0):
        self.token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID", "")
        self._post = post
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.token.strip() and self.chat_id.strip())

    def send(self, text: str) -> bool:
        if not self.enabled:
            return False                                   # unset env: silent no-op
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            payload = {"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": True}
            if self._post is not None:
                resp = self._post(url, payload)
            else:
                import httpx

                resp = httpx.post(url, json=payload, timeout=self.timeout)
            return getattr(resp, "status_code", 200) == 200
        except Exception as e:  # noqa: BLE001 - never leak the token, never raise into the engine
            log.error("telegram send failed: %s", type(e).__name__)
            return False


def default_notifiers() -> list:
    tg = TelegramNotifier()
    return [LogNotifier()] + ([tg] if tg.enabled else [])


class AlertManager:
    """Fires on upward level changes at or above `min_level`, and on stale feeds.

    Debounce: the same (symbol, level) is not re-sent within `debounce_s`. An escalation to a
    level higher than anything sent in the window always goes through.
    """

    def __init__(self, notifiers: list | None = None, min_level: str = "warning", debounce_s: float = 300.0,
                 clock: Callable[[], float] = time.monotonic, dispatch: Callable | None = None):
        self.notifiers = default_notifiers() if notifiers is None else notifiers
        self.min_idx = LEVELS.index(min_level) if min_level in LEVELS else 2
        self.debounce_s = debounce_s
        self.clock = clock
        self._dispatch = dispatch                           # e.g. run in a thread; default inline
        self._last: dict[tuple[str, str], float] = {}
        self.history: deque = deque(maxlen=200)
        self.suppressed = 0

    def _emit(self, symbol: str, key: str, text: str, meta: dict) -> bool:
        # level alerts debounce in data time (so a fast replay behaves like the real event did);
        # stale alerts have no data time and use the wall clock
        now = meta.get("data_ts") if meta.get("data_ts") is not None else self.clock()
        last = self._last.get((symbol, key))
        if last is not None and 0 <= now - last < self.debounce_s:
            self.suppressed += 1
            return False
        self._last[(symbol, key)] = now
        rec = {"symbol": symbol, "kind": key, "text": text, "wall_time": time.time(), **meta}
        self.history.append(rec)
        for n in self.notifiers:
            try:
                if self._dispatch:
                    self._dispatch(n.send, text)
                else:
                    n.send(text)
            except Exception as e:  # noqa: BLE001
                log.error("notifier %s failed: %s", getattr(n, "name", "?"), type(e).__name__)
        return True

    def on_transition(self, symbol: str, frm: str, to: str, score: float, price: float, ts: float | None = None,
                      note: str = "") -> bool:
        if to not in LEVELS or frm not in LEVELS:
            return False
        if LEVELS.index(to) <= LEVELS.index(frm) or LEVELS.index(to) < self.min_idx:
            return False
        when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts)) if ts else ""
        text = (f"{ICON.get(to, '')} Storm Chaser: {symbol} {frm} -> {to.upper()} | score {score:.0f}/100 | "
                f"price {price:,.6g} {when}" + (f" | {note}" if note else "")).strip()
        return self._emit(symbol, to, text, {"level": to, "score": score, "price": price, "data_ts": ts})

    def on_stale(self, symbol: str, age_s: float) -> bool:
        text = f"{ICON['stale']} Storm Chaser: {symbol} feed STALE, no ticks for {age_s:.0f}s. Score is not reliable."
        return self._emit(symbol, "stale", text, {"level": "stale", "age_s": age_s})
