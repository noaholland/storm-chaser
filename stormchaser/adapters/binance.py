"""Live Binance public market data (no API key): <symbol>@bookTicker best bid/ask.

Robustness: exponential backoff with jitter on any disconnect, alternate endpoint rotation,
and a receive watchdog so a silently dead socket is torn down and reconnected.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import AsyncIterator, Callable

from ..models import Tick

log = logging.getLogger("stormchaser.binance")

ENDPOINTS = ["wss://stream.binance.com:9443/ws", "wss://data-stream.binance.vision/ws"]


def backoff_delay(attempt: int, base: float = 0.5, cap: float = 30.0, jitter: float = 0.25, rnd=random.random) -> float:
    """attempt 1 -> ~base, doubling each time, capped; +/- jitter fraction."""
    d = min(cap, base * (2 ** max(0, attempt - 1)))
    return max(0.0, d * (1 + jitter * (2 * rnd() - 1)))


def parse_book_ticker(msg: str | bytes, symbol: str, now: float) -> Tick | None:
    """Returns None for anything that is not a usable quote (acks, pings, garbage)."""
    try:
        d = json.loads(msg)
        if not isinstance(d, dict) or "b" not in d or "a" not in d:
            return None
        bid, ask = float(d["b"]), float(d["a"])
        ts = float(d["E"]) / 1000.0 if "E" in d else now
        return Tick(ts=ts, price=0.5 * (bid + ask), bid=bid, ask=ask, symbol=symbol)
    except (ValueError, TypeError, KeyError):
        return None


class BinanceSource:
    kind = "live"

    def __init__(self, symbol: str = "BTCUSDT", connect: Callable | None = None, recv_timeout: float = 10.0,
                 base_delay: float = 0.5, max_delay: float = 30.0, max_attempts: int | None = None,
                 sleep=asyncio.sleep, endpoints: list[str] | None = None, min_interval: float = 0.05):
        self.symbol = symbol.upper()
        self._connect = connect
        self.recv_timeout = recv_timeout
        self.base_delay, self.max_delay = base_delay, max_delay
        self.max_attempts = max_attempts          # None = retry forever
        self._sleep = sleep
        self.endpoints = endpoints or ENDPOINTS
        self.min_interval = min_interval          # throttle: at most one tick per interval
        self.reconnects = 0
        self.connected = False
        self.last_error: str | None = None
        self.delays: list[float] = []

    async def _open(self, url: str):
        if self._connect is not None:
            return await self._connect(url)
        import websockets

        return await websockets.connect(url, open_timeout=10, ping_interval=20, ping_timeout=20, max_queue=1024)

    def __aiter__(self) -> AsyncIterator[Tick]:
        return self._gen()

    async def _gen(self):
        attempt, last_emit = 0, 0.0
        while True:
            url = f"{self.endpoints[self.reconnects % len(self.endpoints)]}/{self.symbol.lower()}@bookTicker"
            ws = None
            try:
                ws = await self._open(url)
                self.connected = True
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=self.recv_timeout)
                    now = time.time()
                    tick = parse_book_ticker(msg, self.symbol, now)
                    if tick is None:
                        continue
                    attempt = 0                       # healthy again: reset backoff
                    if now - last_emit < self.min_interval:
                        continue
                    last_emit = now
                    yield tick
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - closed, timeout, DNS, handshake ...
                self.last_error = f"{type(e).__name__}: {e}"[:200]
            finally:
                self.connected = False
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:  # noqa: BLE001
                        pass
            attempt += 1
            self.reconnects += 1
            if self.max_attempts is not None and attempt > self.max_attempts:
                raise ConnectionError(f"binance feed gave up after {self.max_attempts} attempts: {self.last_error}")
            delay = backoff_delay(attempt, self.base_delay, self.max_delay)
            self.delays.append(delay)
            log.warning("binance %s disconnected (%s); reconnect #%d in %.1fs", self.symbol, self.last_error,
                        self.reconnects, delay)
            await self._sleep(delay)
