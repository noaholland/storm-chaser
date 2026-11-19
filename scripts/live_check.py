"""Live data-source check (needs internet): Binance websocket for BTCUSDT + ETHUSDT, then yfinance.

usage: python scripts/live_check.py [seconds=25]
"""
import sys
import time

from _harness import Api, utc


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 25
    api, ok = Api(), True
    try:
        api.wait()
        for sym in ("BTCUSDT", "ETHUSDT"):
            api.post("/live", symbol=sym, **({"bar_seconds": 1, "warmup_bars": 15} if secs < 200 else {}))
        t0 = time.time()
        while time.time() - t0 < secs:
            time.sleep(5 if secs < 200 else 30)
            for sym in ("BTCUSDT", "ETHUSDT"):
                s = api.get("/score", symbol=sym)
                print(f"  t+{time.time() - t0:4.0f}s {sym} state={s['state']:<10} level={s['level']:<10} score={s['score']} "
                      f"bid={s['bid']} ask={s['ask']} ticks={s['feed']['accepted']} reconnects={s['session']['reconnects']} "
                      f"err={s['session']['feed_error']}")
        for sym in ("BTCUSDT", "ETHUSDT"):
            s = api.get("/score", symbol=sym)
            tr = api.get("/history", symbol=sym)["transitions"]
            print(f"  {sym} level changes: {[(utc(t['ts'])[-5:], t['to'], t['why']) for t in tr] or 'none'}")
            good = s["state"] == "live" and s["feed"]["accepted"] > 20 and s["bid"] and "spread" in s["components"]
            ok &= bool(good)
            print(f"[{'PASS' if good else 'FAIL'}] Binance {sym} live: {s['feed']['accepted']} ticks, components {s['components']}")
        for sym, period, interval in (("GBPUSD=X", "5d", "5m"), ("Gold", "1mo", "1h"), ("^GSPC", "1y", "1d")):
            r = api.post("/yfinance", symbol=sym, period=period, interval=interval)
            if r.get("_status"):
                ok = False
                print(f"[FAIL] yfinance {sym}: {r}")
                continue
            for _ in range(300):
                s = api.get("/score", symbol=r["symbol"])
                if s["state"] == "ended":
                    break
                time.sleep(0.1)
            rep = r["meta"]["report"]
            good = s["state"] == "ended" and rep["rows_out"] > 50
            ok &= good
            print(f"[{'PASS' if good else 'FAIL'}] yfinance {r['symbol']} {period}/{interval}: {rep['rows_out']} rows "
                  f"{utc(rep['start'])} -> {utc(rep['end'])}, final level {s['level']}, peak score {s['peak_score']}")
    finally:
        api.close()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
