"""Re-download the bundled replay datasets (already committed under stormchaser/data).

BTCUSDT 1m klines from Binance public REST; GBPUSD ticks from Dukascopy, aggregated to 1m bid/ask.
"""
import csv, json, lzma, struct, sys, time, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "stormchaser" / "data"
UA = {"User-Agent": "Mozilla/5.0 stormchaser"}


def get(url, tries=8):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return r.read()
        except Exception:  # noqa: BLE001
            if i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def btc():
    start = int(datetime(2022, 11, 6, tzinfo=timezone.utc).timestamp() * 1000)
    end = int(datetime(2022, 11, 13, tzinfo=timezone.utc).timestamp() * 1000)
    rows, t = [], start
    while t < end:
        k = json.loads(get(f"https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1m&startTime={t}&endTime={end - 1}&limit=1000"))
        if not k:
            break
        rows += k
        t = k[-1][0] + 60000
    with open(OUT / "btc_ftx_2022.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for r in rows:
            w.writerow([datetime.fromtimestamp(r[0] / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), *[float(x) for x in r[1:6]]])
    print("btc rows", len(rows))


def gbp():
    day, end = datetime(2022, 9, 21, tzinfo=timezone.utc), datetime(2022, 9, 29, tzinfo=timezone.utc)
    bars = {}
    while day < end:
        url = f"https://datafeed.dukascopy.com/datafeed/GBPUSD/{day.year}/{day.month - 1:02d}/{day.day:02d}/{day.hour:02d}h_ticks.bi5"
        raw = get(url)
        if raw:
            data = lzma.decompress(raw)
            for i in range(0, len(data), 20):
                ms, ask, bid, _, _ = struct.unpack(">IIIff", data[i:i + 20])
                ts = day + timedelta(milliseconds=ms)
                bars[ts.replace(second=0, microsecond=0)] = (bid / 1e5, ask / 1e5)  # last quote of the minute
        day += timedelta(hours=1)
    with open(OUT / "gbpusd_minibudget_2022.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "bid", "ask"])
        for ts in sorted(bars):
            w.writerow([ts.strftime("%Y-%m-%dT%H:%M:%SZ"), f"{bars[ts][0]:.5f}", f"{bars[ts][1]:.5f}"])
    print("gbp rows", len(bars))


def gbp_hf():
    """Fallback: 15m OHLC bars from the HF dataset Yllvar/fx-historical-data (no bid/ask)."""
    import io

    import pandas as pd

    raw = get("https://huggingface.co/datasets/Yllvar/fx-historical-data/resolve/main/GBPUSD/GBPUSD_M15.parquet")
    d = pd.read_parquet(io.BytesIO(raw))
    print("hf M15 coverage", d["Time"].min(), d["Time"].max(), len(d))
    d = d[(d["Time"] >= "2022-09-19") & (d["Time"] < "2022-10-01")].sort_values("Time")
    out = pd.DataFrame({
        "timestamp": d["Time"].dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "open": d["Open"], "high": d["High"], "low": d["Low"], "close": d["Close"],
    })
    out.to_csv(OUT / "gbpusd_minibudget_2022.csv", index=False)
    print("gbp rows (hf 15m)", len(out))


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    which = sys.argv[1:] or ["btc", "gbp"]
    if "btc" in which:
        btc()
    if "gbp" in which:
        try:
            gbp()
        except Exception as e:  # noqa: BLE001
            print("dukascopy failed, falling back to HF 15m:", repr(e))
            gbp_hf()
    if "gbp_hf" in which:
        gbp_hf()
