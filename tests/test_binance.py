"""Live Binance adapter: parsing, backoff, auto-reconnect, silent-socket watchdog."""
import asyncio
import json

import pytest

from stormchaser.adapters.binance import BinanceSource, backoff_delay, parse_book_ticker


def msg(bid, ask):
    return json.dumps({"u": 1, "s": "BTCUSDT", "b": str(bid), "B": "1", "a": str(ask), "A": "1"})


class FakeWS:
    """Plays a script: strings are messages, exceptions are raised, 'hang' blocks forever."""

    def __init__(self, script):
        self.script, self.closed = list(script), False

    async def recv(self):
        if not self.script:
            raise ConnectionError("closed by peer")
        item = self.script.pop(0)
        if item == "hang":
            await asyncio.sleep(3600)
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        self.closed = True


def connector(plans, urls):
    plans = list(plans)

    async def connect(url):
        urls.append(url)
        plan = plans.pop(0)
        if isinstance(plan, Exception):
            raise plan
        return plan

    return connect


async def no_sleep(_):
    await asyncio.sleep(0)


async def take(src, n):
    out = []
    async for t in src:
        out.append(t)
        if len(out) == n:
            break
    return out


def test_parse_book_ticker():
    t = parse_book_ticker(msg(100, 102), "BTCUSDT", now=5.0)
    assert (t.price, t.bid, t.ask, t.ts, t.symbol) == (101.0, 100.0, 102.0, 5.0, "BTCUSDT")
    for junk in ["", "not json", "[]", '{"result":null,"id":1}', '{"b":"x","a":"1"}', b"\xff\xfe", "null"]:
        assert parse_book_ticker(junk, "BTCUSDT", 0.0) is None


def test_backoff_grows_exponentially_and_is_capped():
    mid = [backoff_delay(a, base=0.5, cap=30, jitter=0.25, rnd=lambda: 0.5) for a in range(1, 12)]
    assert mid[:6] == [0.5, 1.0, 2.0, 4.0, 8.0, 16.0] and mid[-1] == 30.0
    lo, hi = backoff_delay(3, rnd=lambda: 0.0), backoff_delay(3, rnd=lambda: 1.0)
    assert lo == pytest.approx(1.5) and hi == pytest.approx(2.5)        # +/- 25% jitter


async def test_disconnect_triggers_reconnect_with_backoff_and_resets_after_success():
    urls = []
    ws1 = FakeWS([msg(100, 101), msg(101, 102)])                         # then the peer closes
    ws4 = FakeWS([msg(102, 103), ConnectionResetError("reset")])
    ws5 = FakeWS([msg(103, 104)])
    plans = [ws1, OSError("dns"), TimeoutError("handshake"), ws4, ws5]
    src = BinanceSource("btcusdt", connect=connector(plans, urls), sleep=no_sleep, min_interval=0)
    ticks = await asyncio.wait_for(take(src, 4), 5)
    assert [t.bid for t in ticks] == [100.0, 101.0, 102.0, 103.0]
    assert src.reconnects == 4 and len(urls) == 5 and ws1.closed and ws4.closed
    d = src.delays
    assert d[0] < d[1] < d[2]                                           # three failures in a row: 0.5, 1, 2
    assert d[3] < d[2] and d[3] <= 0.5 * 1.25                           # healthy again: backoff reset
    assert len({u.split("/ws")[0] for u in urls}) == 2                  # rotates between both endpoints
    assert all(u.endswith("/btcusdt@bookTicker") for u in urls)


async def test_silent_socket_is_torn_down_by_the_watchdog():
    urls = []
    dead = FakeWS([msg(100, 101), "hang"])                              # connected but no data: a zombie
    alive = FakeWS([msg(105, 106)])
    src = BinanceSource("BTCUSDT", connect=connector([dead, alive], urls), sleep=no_sleep, recv_timeout=0.05,
                        min_interval=0)
    ticks = await asyncio.wait_for(take(src, 2), 5)
    assert [t.bid for t in ticks] == [100.0, 105.0] and src.reconnects == 1
    assert "TimeoutError" in src.last_error and dead.closed


async def test_garbage_messages_are_skipped_not_fatal():
    ws = FakeWS(["{}", "garbage", msg(100, 101), '{"result":null}', msg(-1, 5), msg(101, 102)])
    src = BinanceSource("BTCUSDT", connect=connector([ws], []), sleep=no_sleep, min_interval=0)
    ticks = await asyncio.wait_for(take(src, 3), 5)
    assert src.reconnects == 0 and [t.ask for t in ticks] == [101.0, 5.0, 102.0]   # engine rejects the bad quote


async def test_gives_up_only_when_max_attempts_is_set():
    plans = [OSError("down")] * 4
    src = BinanceSource("BTCUSDT", connect=connector(plans, []), sleep=no_sleep, max_attempts=3)
    with pytest.raises(ConnectionError, match="gave up after 3"):
        await asyncio.wait_for(take(src, 1), 5)
    assert src.connected is False


async def test_throttle_limits_tick_rate():
    ws = FakeWS([msg(100 + i, 101 + i) for i in range(50)] + ["hang"])
    src = BinanceSource("BTCUSDT", connect=connector([ws], []), sleep=no_sleep, min_interval=60.0, recv_timeout=0.05,
                        max_attempts=0)
    out = []
    with pytest.raises(ConnectionError):
        async for t in src:
            out.append(t)
    assert len(out) == 1                                                # 50 quotes in one interval -> one tick
