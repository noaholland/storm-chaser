"""Generic historical loader + paced replay source.

`load_ticks(path)` accepts CSV / parquet with unknown column names and returns clean, sorted
ticks plus a report of what was detected and what was dropped.
"""
from __future__ import annotations

import asyncio
import math
from pathlib import Path
from typing import AsyncIterator

import numpy as np
import pandas as pd

from ..models import Tick

TS_NAMES = ["timestamp", "time", "datetime", "date", "ts", "open_time", "opentime", "gmt time", "gmttime",
            "local time", "localtime", "timestamp_ms", "epoch", "t"]
PRICE_NAMES = ["price", "close", "close_mid", "last", "mid", "adj close", "adjclose", "c", "px", "last_price", "lastprice",
               "trade_price", "value", "rate"]
BID_NAMES = ["bid", "close_bid", "bid_price", "bidprice", "best_bid", "bestbid", "b", "bid1", "bid_close", "bidclose"]
ASK_NAMES = ["ask", "close_ask", "ask_price", "askprice", "best_ask", "bestask", "a", "ask1", "offer", "ask_close", "askclose"]
SYMBOL_NAMES = ["symbol", "ticker", "pair", "instrument", "asset", "ccy_pair", "currency_pair", "name"]

MIN_SPEED, MAX_SPEED = 0.1, 1_000_000.0


class DataError(ValueError):
    """Raised when a file cannot be turned into ticks. The message is safe to show to users."""


def _key(c) -> str:
    return str(c).strip().lower().replace("-", "_")


def _find(cols: dict[str, str], names: list[str]) -> str | None:
    for n in names:                                  # exact match first, in priority order
        if n in cols:
            return cols[n]
    for n in names:                                  # then substring ("gbpusd_close", "Bid Price")
        if len(n) < 3:
            continue
        for k, orig in cols.items():
            if n in k.replace(" ", "_") or n in k:
                return orig
    return None


def _to_epoch(s: pd.Series) -> pd.Series:
    """Epoch seconds as float; NaN where unparseable. Numeric epochs may be s/ms/us/ns."""
    if pd.api.types.is_datetime64_any_dtype(s):
        dt = s
    else:
        num = pd.to_numeric(s, errors="coerce")
        if num.notna().mean() > 0.9:
            med = float(num.abs().median())
            div = 1.0 if med < 1e11 else 1e3 if med < 1e14 else 1e6 if med < 1e17 else 1e9
            return (num / div).astype(float)
        dt = pd.to_datetime(s, errors="coerce", utc=True, format="mixed")
    dt = pd.to_datetime(dt, errors="coerce", utc=True)
    out = (dt - pd.Timestamp("1970-01-01", tz="UTC")) / pd.Timedelta(seconds=1)
    return out.astype(float)


def read_table(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise DataError(f"file not found: {p.name}")
    try:
        if p.suffix.lower() in (".parquet", ".pq"):
            return pd.read_parquet(p)
        if p.suffix.lower() in (".xlsx", ".xls"):
            return pd.read_excel(p)
        return pd.read_csv(p, sep=None, engine="python")
    except DataError:
        raise
    except Exception as e:  # noqa: BLE001 - corrupt / empty / wrong format
        raise DataError(f"could not read {p.name}: {type(e).__name__}: {e}") from e


def frame_to_ticks(df: pd.DataFrame, symbol: str = "", ts_col: str | None = None, price_col: str | None = None,
                   bid_col: str | None = None, ask_col: str | None = None, select: str | None = None,
                   start: str | None = None, end: str | None = None) -> tuple[list[Tick], dict]:
    """Auto-detect columns, clean, sort. `select` filters a long-format file on its symbol column."""
    if df is None or len(df) == 0:
        raise DataError("no rows in data")
    if not isinstance(df.index, pd.RangeIndex):       # yfinance-style datetime index
        df = df.reset_index()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [" ".join(str(x) for x in c if str(x) != "").strip() for c in df.columns]
    cols = {_key(c): c for c in df.columns}
    sym_col = _find(cols, SYMBOL_NAMES)
    if select and sym_col:
        df = df[df[sym_col].astype(str).str.upper() == select.upper()]
        if len(df) == 0:
            raise DataError(f"no rows for symbol {select!r}")
    ts_col = ts_col or _find(cols, TS_NAMES)
    if ts_col is None:                                # fall back to the first column that parses as time
        for c in df.columns:
            if df[c].dtype == object and _to_epoch(df[c].head(50)).notna().mean() > 0.9:
                ts_col = c
                break
    if ts_col is None or ts_col not in df.columns:
        raise DataError(f"no timestamp column found in {list(df.columns)[:12]}")
    bid_col = bid_col or _find(cols, BID_NAMES)
    ask_col = ask_col or _find(cols, ASK_NAMES)
    if not (bid_col and ask_col) or bid_col == ask_col:
        bid_col = ask_col = None
    price_col = price_col or _find(cols, PRICE_NAMES)
    if price_col in (bid_col, ask_col) and bid_col:
        price_col = None
    if price_col is None and bid_col is None:
        numeric = [c for c in df.columns if c != ts_col and pd.api.types.is_numeric_dtype(df[c])]
        if not numeric:
            raise DataError(f"no price column found in {list(df.columns)[:12]}")
        price_col = numeric[0]

    n_in = len(df)
    ts = _to_epoch(df[ts_col]).to_numpy(dtype=float)
    bid = pd.to_numeric(df[bid_col], errors="coerce").to_numpy(dtype=float) if bid_col else np.full(n_in, np.nan)
    ask = pd.to_numeric(df[ask_col], errors="coerce").to_numpy(dtype=float) if ask_col else np.full(n_in, np.nan)
    if price_col:
        price = pd.to_numeric(df[price_col], errors="coerce").to_numpy(dtype=float)
    else:
        price = 0.5 * (bid + ask)
    ok_q = np.isfinite(bid) & np.isfinite(ask) & (bid > 0) & (ask > 0) & (bid <= ask)
    bad_ts = ~np.isfinite(ts)
    outside = np.zeros(n_in, dtype=bool)               # optional [start, end) window, ISO strings in UTC
    if start:
        outside |= ts < pd.Timestamp(start, tz="UTC").timestamp()
    if end:
        outside |= ts >= pd.Timestamp(end, tz="UTC").timestamp()
    bad_px = ~(np.isfinite(price) & (price > 0))
    keep = ~(bad_ts | bad_px | outside)
    ts, price, bid, ask, ok_q = ts[keep], price[keep], bid[keep], ask[keep], ok_q[keep]
    order = np.argsort(ts, kind="stable")
    unsorted = bool(np.any(np.diff(ts) < 0))
    ts, price, bid, ask, ok_q = ts[order], price[order], bid[order], ask[order], ok_q[order]
    first = np.ones(len(ts), dtype=bool)
    first[1:] = np.diff(ts) > 0                        # keep the first row of each timestamp
    n_dup = int((~first).sum())
    ts, price, bid, ask, ok_q = ts[first], price[first], bid[first], ask[first], ok_q[first]
    if len(ts) == 0 and outside.any():
        raise DataError("no rows inside the requested start/end window")
    if len(ts) == 0:
        raise DataError("no valid rows after cleaning (all timestamps or prices invalid)")
    ticks = [Tick(float(t), float(p), float(b) if q else None, float(a) if q else None, symbol)
             for t, p, b, a, q in zip(ts, price, bid, ask, ok_q)]
    carry_cols = [c for c in df.columns if str(c).lower().startswith("carry_")]
    if carry_cols:                                     # forward curve columns -> Tick.extra["carry"]
        tenors = [str(c)[6:] for c in carry_cols]
        mat = df[carry_cols].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)[keep][order][first]
        for tick, row in zip(ticks, mat):
            cv = {t: float(v) for t, v in zip(tenors, row) if v == v}
            if cv:
                tick.extra = {"carry": cv}
    step = float(np.median(np.diff(ts))) if len(ts) > 1 else 60.0
    report = {
        "rows_in": int(n_in), "rows_out": len(ticks), "dropped_bad_timestamp": int(bad_ts.sum()),
        "dropped_bad_price": int((bad_px & ~bad_ts).sum()), "dropped_duplicates": n_dup, "was_unsorted": unsorted,
        "columns": {"timestamp": str(ts_col), "price": str(price_col) if price_col else "mid(bid,ask)",
                    "bid": str(bid_col) if bid_col else None, "ask": str(ask_col) if ask_col else None},
        "start": float(ts[0]), "end": float(ts[-1]), "median_step_s": step if step > 0 else 60.0,
        "has_quotes": bool(ok_q.any()), "curve_tenors": [str(c)[6:] for c in carry_cols],
    }
    return ticks, report


def load_ticks(path: str | Path, symbol: str = "", **kw) -> tuple[list[Tick], dict]:
    return frame_to_ticks(read_table(path), symbol=symbol, **kw)


def clamp_speed(speed) -> tuple[float, bool]:
    """Speed = data seconds per wall second. Returns (speed, was_clamped). Garbage -> MAX."""
    try:
        s = float(speed)
    except (TypeError, ValueError):
        return MAX_SPEED, True
    if not math.isfinite(s) or s <= 0:
        return MAX_SPEED, True
    c = min(max(s, MIN_SPEED), MAX_SPEED)
    return c, c != s


class ReplaySource:
    """Replays ticks at `speed` x real time. Idle gaps (weekends) are capped at `max_gap_wall_s`."""

    kind = "replay"

    def __init__(self, ticks: list[Tick], speed: float = 3600.0, max_gap_wall_s: float = 1.0, sleep=asyncio.sleep):
        self.ticks = ticks
        self.speed, self.speed_clamped = clamp_speed(speed)
        self.max_gap_wall_s = max_gap_wall_s
        self._sleep = sleep
        self.position = 0

    def __aiter__(self) -> AsyncIterator[Tick]:
        return self._gen()

    async def _gen(self):
        debt, prev = 0.0, None
        for i, t in enumerate(self.ticks):
            if prev is not None and self.speed < MAX_SPEED:     # at MAX_SPEED ("max") never sleep
                debt += min(max(t.ts - prev, 0.0) / self.speed, self.max_gap_wall_s)
            prev = t.ts
            if debt >= 0.01:
                await self._sleep(debt)
                debt = 0.0
            elif i % 200 == 0:
                await self._sleep(0)                   # stay cooperative at very high speed
            self.position = i + 1
            yield t
