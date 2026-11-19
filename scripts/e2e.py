"""End-to-end demo: start the API, replay real crises, watch the score reach TORNADO, freeze the storm and
ask "what is my exposure?" and "what happens next?". Exits non-zero on any failed check.

usage: python scripts/e2e.py [dataset ...]      (default: SGX USDJPY storm, FTX collapse, GBP mini-budget)
"""
import sys
import time

from _harness import Api, utc

CHECKS = []
DEFAULT = ["sgx_usdjpy_storm", "btc_ftx_2022", "gbpusd_minibudget_2022"]


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def money(x):
    return "n/a" if x is None else f"{x:,.0f}"


def day(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def run_dataset(api: Api, name: str, meta: dict):
    sym = meta["symbol"]
    print(f"\n=== {name}: {meta['title']} ===")
    r = api.post("/replay", dataset=name, speed=200_000, pause_on="tornado")
    rep = r.get("meta", {}).get("report", {})
    check("replay started", r.get("started"), f"{rep.get('rows_out')} rows, bar {r.get('bar_seconds'):g}s, "
          f"speed {r.get('speed'):g}x")
    print(f"  data {utc(rep['start'])} -> {utc(rep['end'])} | price={rep['columns']['price']} "
          f"quotes={rep['has_quotes']} curve tenors={len(rep.get('curve_tenors', []))}")

    last, frozen, n_events, shown, trans = None, None, 0, 0, []
    for event, snap in api.stream(symbol=sym, interval=0.1):
        if event == "end":
            break
        n_events += 1
        last = snap
        for t in snap.get("new_transitions", []):
            trans.append(t)
            if t["to"] in ("warning", "tornado") and shown < 8:
                shown += 1
                print(f"  {utc(t['ts'])}  {t['frm']:>8} -> {t['to'].upper():<8} score {t['score']:5.1f}  "
                      f"price {t['price']:,.6g}  driver={t['why']} kind={t['kind']}"
                      + ("  CARRY UNWIND" if t.get("carry_unwind") else ""))
        if snap["state"] == "paused" and frozen is None:
            # the storm is frozen at its first TORNADO: ask the two questions an investor would ask
            fx = len(sym) == 6 and sym.isalpha() and not sym.endswith("USD")     # USDJPY: 1m USD of base
            size = 1_000_000 if fx else 1_000_000 / snap["price"]
            frozen = {
                "snap": snap,
                "exposure": api.post("/exposure", {"positions": [{"asset": sym, "size": size}], "horizon_bars": 60}),
                "simulate": api.post("/simulate", {"symbol": sym, "size": size, "steps": 60, "n_paths": 2000, "seed": 7}),
            }
            api.post("/resume", symbol=sym)
    hist = api.get("/history", symbol=sym, n=20000)
    lo_i = min(range(hist["points"]), key=lambda i: hist["price"][i])
    lo_ts, lo_px = hist["ts"][lo_i], hist["price"][lo_i]
    tornado = next((t for t in trans if t["to"] == "tornado"), None)

    check("stream delivered snapshots", n_events >= 3, f"{n_events} events, {len(trans)} level changes")
    warm = [i for i, lv in enumerate(hist["level"]) if lv == "warming_up"]
    check("warm-up is reported as warming_up with no score, never a false green",
          warm and warm == list(range(len(warm))) and all(hist["score"][i] is None for i in warm)
          and hist["level"][len(warm)] == "green", f"first {len(warm)} bars")
    check("score reached TORNADO", tornado is not None, f"peak score {last['peak_score']}")
    if tornado:
        check("TORNADO fired before/at the low of the crash", tornado["ts"] <= lo_ts,
              f"alert {utc(tornado['ts'])} @ {tornado['price']:,.6g}; low {utc(lo_ts)} @ {lo_px:,.6g}; "
              f"{(lo_ts - tornado['ts']) / 3600:.1f}h earlier, {100 * (lo_px / tornado['price'] - 1):.1f}% still to fall")
    check("replay finished cleanly", api.get("/score", symbol=sym)["state"] == "ended",
          f"accepted {last['feed']['accepted']}, rejected {last['feed']['rejected'] or 'none'}, gaps {last['feed']['gaps']}")

    if name.startswith("sgx"):
        alert_days = sorted({day(t["ts"]) for t in trans if t["to"] in ("warning", "tornado")})
        check("SGX: warning/tornado on 29-30 Jul 2026", {"2026-07-29", "2026-07-30"} <= set(alert_days), str(alert_days))
        check("SGX: 30 Jul escalates from squall to STORM", any(t["kind"] == "storm" and day(t["ts"]) == "2026-07-30"
                                                              for t in trans))
        check("SGX: carry-unwind signal during the storm", any(t.get("carry_unwind") for t in trans))
        if frozen:
            c = frozen["snap"]["carry"]
            curve = " ".join(f"{k}={100 * v:+.2f}%" for k, v in c["curve"].items())
            print(f"  forward curve (implied rate differential): {curve}; carry earned by the {c['earning_side']} side")
            check("SGX: spread + forward-curve features live", {"spread", "curve"} <= set(frozen["snap"]["components"]))

    check("replay froze at the first TORNADO for inspection", frozen is not None)
    if frozen:
        ex, sim = frozen["exposure"], frozen["simulate"]
        ccy, tot = next(iter(ex["totals"].items()), ("?", {}))
        pos = ex["positions"][0]
        print(f"  frozen at {utc(frozen['snap']['ts'])}: long {sym} worth {money(tot.get('value'))} {ccy}, next 60 bars, 99%:")
        print(f"    VaR {money(tot.get('var'))} (calm baseline {money(tot.get('var_baseline'))})"
              f" | ES {money(tot.get('es'))} | stressed loss {money(tot.get('stressed_loss'))} {ccy}")
        check("/exposure: VaR <= ES <= stressed loss, storm VaR > calm VaR", pos["status"] == "ok" and
              0 < tot["var_baseline"] < tot["var"] <= tot["es"] <= tot["stressed_loss"],
              f"vol is {pos.get('vol_ratio', 0):.1f}x baseline")
        import math
        hand = abs(pos["value"]) * (1 - math.exp(-2.3263478740 * pos["sigma_bar"] * math.sqrt(60)))
        check("/exposure: VaR equals the closed form |V| x (1 - exp(-2.3263 x sigma_bar x sqrt(60)))",
              abs(hand - tot["var"]) < 1e-6 * abs(pos["value"]),
              f"{abs(pos['value']):,.0f} x (1 - exp(-2.3263 x {pos['sigma_bar']:.6f} x 7.7460)) = {hand:,.0f}")
        f, term = sim.get("fan", {}), sim.get("terminal", {})
        check("/simulate: ordered 5/50/95 P&L fan", sim.get("status") == "ok" and
              all(a <= b <= c for a, b, c in zip(f["pnl_p5"], f["pnl_p50"], f["pnl_p95"])),
              f"{sim.get('n_paths')} paths x {sim.get('steps')} steps, {sim.get('method')}")
        print(f"    cyclone simulator: P&L p5 {money(term.get('pnl_p5'))} / p50 {money(term.get('pnl_p50'))} / "
              f"p95 {money(term.get('pnl_p95'))} {ccy}; P(loss) {term.get('prob_loss'):.0%}")
    return sym


def squalls(api: Api):
    """Scheduled data releases (12:31 UTC) must show up as short squalls, not regime shifts."""
    print("\n=== sgx_usdjpy_2026: two full months, scheduled data releases ===")
    api.post("/replay", dataset="sgx_usdjpy_2026", speed="max")
    last = None
    for event, snap in api.stream(symbol="USDJPY", interval=0.5):
        if event == "end":
            break
        last = snap
    trans = api.get("/history", symbol="USDJPY", n=100)["transitions"]
    check("full SGX file replayed", last and last["feed"]["accepted"] > 64000,
          f"{last['feed']['accepted']} ticks, {last['feed']['gaps']} weekend/holiday gaps, rejected "
          f"{last['feed']['rejected'] or 'none'}")
    for d in ("2026-07-14", "2026-08-07"):
        win = [t for t in trans if day(t["ts"]) == d and "12:25" <= utc(t["ts"])[-5:] <= "15:00"]
        up = next((t for t in win if t["to"] in ("warning", "tornado")), None)
        down = next((t for t in win if t["to"] == "green"), None)
        mins = (down["ts"] - up["ts"]) / 60 if up and down else None
        check(f"{d} 12:31 release: alert, labelled squall, green again within 2.5h",
              up and down and all(t["kind"] != "storm" for t in win),
              f"alert {utc(up['ts'])[-5:]} ({up['to']}), back to green after {mins:.0f} min" if mins else "")
    roll = [t for t in trans if utc(t["ts"])[-5:-3] == "21" and t["to"] in ("warning", "tornado")
            and "2026-07-13" <= day(t["ts"]) < "2026-07-28"]
    check("daily 21:00 roll / Sunday reopen wide spreads never raise a warning", not roll, f"{len(roll)} alerts")


def main():
    api = Api()
    try:
        h = api.wait()
        print(f"Storm Chaser API up on {api.base}: {h['status']}, notifiers {h['notifiers']}")
        print("\n=== edge cases over HTTP ===")
        check("/score before any feed -> 409, not a fake green", api.get("/score").get("_status") == 409)
        check("unknown dataset -> 400", api.post("/replay", dataset="nope").get("_status") == 400)
        check("negative replay speed -> 422", api.post("/replay", dataset="btc_ftx_2022", speed=-5).get("_status") == 422)
        check("empty portfolio -> zero risk, 200", api.post("/exposure", {"positions": []}).get("totals") == {})

        catalogue = api.get("/datasets")["datasets"]
        names = sys.argv[1:] or DEFAULT
        syms = [run_dataset(api, name, catalogue[name]) for name in names]
        if not sys.argv[1:]:
            squalls(api)

        print("\n=== after the storm ===")
        sym = syms[0]
        ex = api.post("/exposure", {"positions": [{"asset": sym, "size": 1}, {"asset": "DOGE", "size": 5},
                                                  {"asset": sym, "size": 0}, {"asset": sym, "size": "lots"}]})
        st = [p["status"] for p in ex["positions"]]
        check("mixed portfolio handled per position", st == ["ok", "unknown_asset", "zero", "error"], str(st))
        big = api.post("/simulate", {"symbol": sym, "steps": 10**9, "n_paths": 10**9, "alpha": 5, "beta": 5})
        check("extreme Monte Carlo params are clamped, still answers", big.get("status") == "ok" and big["clamped"],
              "; ".join(big.get("clamped", []))[:100])
        check("bootstrap simulator works", api.post("/simulate", {"symbol": sym, "method": "bootstrap"}).get("status") == "ok")
        al = api.get("/alerts")
        check("alerts fired and were debounced", len(al["alerts"]) > 0,
              f"{len(al['alerts'])} kept in log, {al['suppressed_by_debounce']} suppressed")
        if al["alerts"]:
            print(f"  last alert: {al['alerts'][-1]['text']}")
        check("/health ok", api.get("/health")["status"] == "ok")
    finally:
        api.close()
    failed = [n for n, okay in CHECKS if not okay]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
