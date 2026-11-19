"""Loaders: generic CSV/parquet auto-detection, replay pacing and speed limits, SGX nested zips, yfinance."""
import asyncio
import io
import math
import zipfile

import numpy as np
import pandas as pd
import pytest
from conftest import DATA

from stormchaser.adapters.replay import (MAX_SPEED, MIN_SPEED, DataError, ReplaySource, clamp_speed, frame_to_ticks,
                                         load_ticks)
from stormchaser.adapters.sgx import available, implied_carry, read_sgx
from stormchaser.adapters.yf import load_yfinance
from stormchaser.models import Tick, same_asset, split_symbol


# ------------------------------------------------------ column detection
@pytest.mark.parametrize("cols,expect", [
    ({"Date": ["2024-01-01", "2024-01-02", "2024-01-03"], "Open": [1, 2, 3], "Close": [10.0, 11.0, 12.0]}, "Close"),
    ({"time": [1704067200000, 1704067260000, 1704067320000], "price": [10.0, 11.0, 12.0], "qty": [5, 5, 5]}, "price"),
    ({"Gmt time": ["01.01.2024 00:00:00", "01.01.2024 00:01:00", "01.01.2024 00:02:00"], "Bid": [9.9, 10.9, 11.9],
      "Ask": [10.1, 11.1, 12.1]}, "mid(bid,ask)"),
    ({"ts": [1704067200, 1704067260, 1704067320], "foo": [10.0, 11.0, 12.0]}, "foo"),
    ({"open_time": [1704067200000000, 1704067260000000, 1704067320000000], "last": [10.0, 11.0, 12.0]}, "last"),
])
def test_auto_detects_timestamp_and_price_columns(tmp_path, cols, expect):
    p = tmp_path / "x.csv"
    pd.DataFrame(cols).to_csv(p, index=False)
    ticks, rep = load_ticks(p, "X")
    assert rep["columns"]["price"] == expect and len(ticks) == 3
    assert [round(t.price) for t in ticks] == [10, 11, 12]
    assert 1.70e9 < ticks[0].ts < 1.71e9 and ticks[1].ts - ticks[0].ts in (60.0, 86400.0)
    if expect.startswith("mid"):
        assert ticks[0].bid == 9.9 and ticks[0].ask == 10.1 and rep["has_quotes"]


def test_parquet_and_semicolon_csv(tmp_path):
    df = pd.DataFrame({"timestamp": pd.date_range("2024-01-01", periods=5, freq="min", tz="UTC"), "close": range(1, 6)})
    df.to_parquet(tmp_path / "x.parquet")
    df.to_csv(tmp_path / "x.csv", sep=";", index=False)
    a, _ = load_ticks(tmp_path / "x.parquet")
    b, _ = load_ticks(tmp_path / "x.csv")
    assert [t.price for t in a] == [t.price for t in b] == [1, 2, 3, 4, 5]
    assert [t.ts for t in a] == [t.ts for t in b]


def test_dirty_file_is_cleaned_sorted_and_reported(tmp_path):
    p = tmp_path / "dirty.csv"
    p.write_text("timestamp,close\n"
                 "2024-01-01 00:02:00,102\n"
                 "2024-01-01 00:00:00,100\n"      # out of order
                 "2024-01-01 00:01:00,101\n"
                 "2024-01-01 00:01:00,999\n"      # duplicate timestamp
                 "not a date,103\n"
                 "2024-01-01 00:03:00,\n"         # missing price
                 "2024-01-01 00:04:00,-5\n"
                 "2024-01-01 00:05:00,0\n"
                 "2024-01-01 00:06:00,abc\n"
                 "2024-01-01 00:07:00,107\n")
    ticks, rep = load_ticks(p)
    assert [t.price for t in ticks] == [100, 101, 102, 107]
    assert rep["was_unsorted"] and rep["dropped_duplicates"] == 1
    assert rep["dropped_bad_timestamp"] == 1 and rep["dropped_bad_price"] == 4 and rep["rows_in"] == 10


def test_crossed_or_missing_quotes_keep_the_price_drop_the_quote(tmp_path):
    p = tmp_path / "q.csv"
    p.write_text("time,price,bid,ask\n1704067200,100,99,101\n1704067260,100,101,99\n1704067320,100,,101\n")
    ticks, _ = load_ticks(p)
    assert (ticks[0].bid, ticks[0].ask) == (99, 101) and ticks[1].bid is None and ticks[2].ask is None


@pytest.mark.parametrize("content,msg", [
    ("", "could not read"),
    ("timestamp,close\n", "no rows"),
    ("a,b\nx,y\nz,w\n", "no timestamp"),
    ("timestamp,note\n2024-01-01,hello\n", "no price"),
    ("timestamp,close\nbad,1\nworse,2\n", "no valid rows"),
])
def test_unusable_files_raise_a_clear_error(tmp_path, content, msg):
    p = tmp_path / "bad.csv"
    p.write_text(content)
    with pytest.raises(DataError, match=msg):
        load_ticks(p)


def test_missing_file_and_window_filter(tmp_path):
    with pytest.raises(DataError, match="not found"):
        load_ticks(tmp_path / "nope.csv")
    ticks, _ = load_ticks(DATA / "btc_ftx_2022.csv", start="2022-11-08", end="2022-11-09")
    assert len(ticks) == 1440
    with pytest.raises(DataError, match="window"):
        load_ticks(DATA / "btc_ftx_2022.csv", start="2030-01-01")


def test_bundled_datasets_load():
    for name, n in [("btc_ftx_2022.csv", 10080), ("gbpusd_minibudget_2022.csv", 948), ("sgx_usdjpy_2026.parquet", 64355)]:
        ticks, rep = load_ticks(DATA / name)
        assert len(ticks) == n and rep["dropped_bad_price"] == 0 and all(t.price > 0 for t in ticks[:100])


def test_symbol_helpers():
    assert same_asset("BTC", "BTCUSDT") and same_asset("btc-usd", "BTCUSDT") and same_asset("GBPUSD=X", "GBPUSD")
    assert not same_asset("ETH", "BTCUSDT") and not same_asset("", "BTCUSDT")
    assert split_symbol("USDJPY") == ("USD", "JPY") and split_symbol("AAPL") == ("AAPL", "USD")


# ------------------------------------------------------- replay / speed
@pytest.mark.parametrize("speed,expect,clamped", [
    (60, 60.0, False), (MIN_SPEED / 10, MIN_SPEED, True), (1e12, MAX_SPEED, True), (0, MAX_SPEED, True),
    (-3, MAX_SPEED, True), (float("nan"), MAX_SPEED, True), (float("inf"), MAX_SPEED, True), ("fast", MAX_SPEED, True),
    (None, MAX_SPEED, True),
])
def test_replay_speed_limits(speed, expect, clamped):
    assert clamp_speed(speed) == (expect, clamped)


async def collect(src):
    return [t async for t in src]


async def test_replay_pacing_and_weekend_gap_cap():
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    ticks = [Tick(60.0 * i, 100.0 + i) for i in range(100)] + [Tick(60.0 * 99 + 2 * 86400 + 60.0 * i, 200.0 + i)
                                                                for i in range(1, 51)]
    src = ReplaySource(ticks, speed=600.0, max_gap_wall_s=1.0, sleep=fake_sleep)
    out = await collect(src)
    assert out == ticks and src.position == 150                         # everything, in order
    # 149 ordinary steps of 60s at 600x = 0.1s each; the weekend is capped at 1s instead of 288s
    assert sum(slept) == pytest.approx(148 * 0.1 + 1.0, rel=1e-6)
    assert max(slept) <= 1.0 + 1e-9


async def test_replay_at_max_speed_stays_cooperative_and_handles_empty():
    assert await collect(ReplaySource([], speed=10)) == []
    ticks = [Tick(float(i), 1.0) for i in range(1000)]
    src = ReplaySource(ticks, speed="garbage")
    assert src.speed == MAX_SPEED and src.speed_clamped
    assert len(await asyncio.wait_for(collect(src), 5)) == 1000


# ------------------------------------------------------------------- SGX
def _inner(name, days, forward=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for d in days:
            ts = pd.date_range(f"2026-07-{d:02d} 00:01", periods=3, freq="min", tz="UTC")
            df = pd.DataFrame({"timestamp": ts.strftime("%Y-%m-%d %H:%M:%S+00:00"), "settlement_date": f"2026-07-{d + 2:02d}",
                               "close_bid": 162.63, "close_ask": 162.64, "close_mid": 162.635})
            if forward:
                df["settlement_date"] = forward["settle"]
                df["close_mid_fp"] = forward["fp"]
            z.writestr(f"{name}_202607{d:02d}.csv", df.to_csv(index=False))
        z.writestr(f"{name}_empty.csv", "")
        z.writestr(f"__MACOSX/._{name}.csv", "junk")
    return buf.getvalue()


@pytest.fixture
def sgx_zip(tmp_path):
    spot = "USDJPY_USD_SPOT_SPOT_1000000_20260701-20260831"
    fwd = "USDJPY_USD_FORWARD_3M_1000000_20260701-20260831"
    p = tmp_path / "Challenge1.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr(f"{spot}.zip", _inner(spot, [1, 2]))
        z.writestr(f"{fwd}.zip", _inner(fwd, [1, 2], {"settle": "2026-10-05", "fp": -1.2328}))
        z.writestr(f"__MACOSX/._{spot}.zip", "junk")
        z.writestr("README.txt", "hello")
    return p


def test_implied_carry_hand_check():
    # USDJPY 3M: -1.2328 points on 162.635 spot over 94 days -> -0.75802% * 365/94 = -2.9434% a year
    assert implied_carry(-1.2328, 162.635, 94) == pytest.approx(-0.029434, abs=5e-6)


def test_sgx_nested_zip_is_read_directly_with_forward_curve(sgx_zip, tmp_path):
    assert available(sgx_zip) == {"USDJPY": ["3M", "SPOT"]}
    df = read_sgx(sgx_zip, "usdjpy")
    assert len(df) == 6 and list(df.columns) == ["timestamp", "bid", "ask", "mid", "carry_3M"]
    # tenor = forward settle (5 Oct) - spot settle (3 Jul) = 94 days
    assert df["carry_3M"].iloc[0] == pytest.approx(-0.029434, abs=5e-6)
    ticks, rep = frame_to_ticks(df, "USDJPY")
    assert rep["curve_tenors"] == ["3M"] and ticks[0].extra == {"carry": {"3M": pytest.approx(-0.029434, abs=5e-6)}}
    assert ticks[0].bid == 162.63 and rep["columns"]["price"] == "mid"
    assert len(read_sgx(sgx_zip, "USDJPY", start="2026-07-02")) == 3
    with zipfile.ZipFile(sgx_zip) as z:                                 # an already-extracted folder works too
        z.extractall(tmp_path / "x")
    assert len(read_sgx(tmp_path / "x", "USDJPY")) == 6


def test_sgx_errors(sgx_zip, tmp_path):
    with pytest.raises(DataError, match="pairs available"):
        read_sgx(sgx_zip, "EURUSD")
    with pytest.raises(DataError, match="window"):
        read_sgx(sgx_zip, "USDJPY", start="2027-01-01")
    half = tmp_path / "half.zip"
    half.write_bytes(sgx_zip.read_bytes()[:200])                        # interrupted download
    with pytest.raises(DataError, match="not a valid zip"):
        read_sgx(half)
    with pytest.raises(DataError, match="not found"):
        read_sgx(tmp_path / "missing.zip")


# -------------------------------------------------------------- yfinance
def test_yfinance_frame_is_converted(monkeypatch):
    idx = pd.date_range("2024-01-01", periods=4, freq="D", tz="America/New_York", name="Date")
    df = pd.DataFrame({"Open": [1, 2, 3, 4], "Close": [10.0, np.nan, 12.0, 13.0], "Volume": [0, 0, 0, 0]}, index=idx)
    ticks, rep = load_yfinance("AAPL", "5d", "1d", download=lambda *a: df)
    assert [t.price for t in ticks] == [10.0, 12.0, 13.0] and rep["dropped_bad_price"] == 1
    assert math.isclose(ticks[1].ts - ticks[0].ts, 2 * 86400)


def test_yfinance_failures_are_data_errors():
    def boom(*a):
        raise ConnectionError("rate limited")

    with pytest.raises(DataError, match="yfinance failed"):
        load_yfinance("AAPL", download=boom)
    with pytest.raises(DataError, match="no data"):
        load_yfinance("NOPE", download=lambda *a: pd.DataFrame())
    with pytest.raises(DataError, match="interval"):
        load_yfinance("AAPL", interval="7m")
    with pytest.raises(DataError, match="symbol"):
        load_yfinance("  ")
