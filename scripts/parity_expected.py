"""Reference output for the static-demo parity check, produced by the Python stack alone (no tapes involved).

    python scripts/parity_expected.py <out_dir>

For each bundled replay it writes
  <dataset>.ticks.jsonl   the session snapshot after every tick (flowing, and as it would read if frozen there),
                          from a plain loop over StormEngine.update()
  <dataset>.api.json      responses of the real FastAPI app: frozen at the first tornado, then run to the end
and simulate_stats.json   Monte Carlo results (20,000 paths) for the simulator's distribution check.
web/scripts/parity.mjs then answers the same questions with the code the static site ships and compares.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from stormchaser.adapters.replay import MAX_SPEED, ReplaySource, load_ticks  # noqa: E402
from stormchaser.alerts import AlertManager  # noqa: E402
from stormchaser.api import create_app  # noqa: E402
from stormchaser.score import StormConfig, StormEngine  # noqa: E402
from stormchaser.session import DATASETS, Session, clean, dataset_path  # noqa: E402
from stormchaser.simulate import simulate  # noqa: E402

STATIC_SETS = ["sgx_usdjpy_storm", "btc_ftx_2022", "gbpusd_minibudget_2022"]


def dump(obj) -> str:
    return json.dumps(clean(obj), allow_nan=False, separators=(",", ":"))


def per_tick(dataset: str, out: Path) -> int:
    """Tick-by-tick reference: the engine driven by a plain loop, snapshot taken through Session.snapshot()."""
    meta = dict(DATASETS[dataset], dataset=dataset)
    ticks, report = load_ticks(dataset_path(dataset), meta["symbol"], select=meta.get("select"),
                               start=meta.get("start"), end=meta.get("end"))
    bar = report["median_step_s"]
    cfg = StormConfig(bar_seconds=bar, stale_after_s=max(10.0, 5.0 * bar / MAX_SPEED + 3.0))
    eng = StormEngine(meta["symbol"], cfg, clock=lambda: 0.0)
    meta.update(report=report, bar_seconds=bar)
    sess = Session(meta["symbol"], "replay", ReplaySource(ticks, speed=MAX_SPEED), eng, meta)
    alerts = AlertManager(notifiers=[])
    eng.on_transition = lambda t: alerts.on_transition(     # as SessionManager._on_transition wires it
        meta["symbol"], t.frm, t.to, t.score, t.price, t.ts, f"driver: {t.why}" + (", carry unwind" if t.carry_unwind else ""))
    n_hist, last_n = 0, 0
    with out.open("w") as f:
        for i, t in enumerate(ticks):
            eng.update(t)
            if eng.n_ret != last_n:                         # Session.run: one chart point per sampled bar
                last_n, n_hist = eng.n_ret, n_hist + 1
            ret_sum = 0.0
            for r in eng.returns:
                ret_sum += r
            seen = {"transitions": len(eng.transitions), "history": n_hist, "alerts": len(alerts.history),
                    "suppressed": alerts.suppressed, "returns_len": len(eng.returns), "returns_sum": ret_sum,
                    "returns_last": eng.returns[-1] if eng.returns else None}
            run = sess.snapshot()
            sess.paused = True
            paused = sess.snapshot()
            sess.paused = False
            f.write(dump({"i": i, "run": run, "paused": paused, "seen": seen}) + "\n")
    return len(ticks)


def exposure_requests(symbol: str, price: float) -> list[dict]:
    base = [{"asset": symbol, "size": 1_000_000}]
    book = [
        {"asset": symbol, "size": 1_000_000, "entry_price": price * 1.01},
        {"asset": symbol, "size": -250_000.5},
        {"asset": symbol, "size": 5_000_000, "currency": symbol[-3:]},      # notional in the quote currency
        {"asset": "DOGE", "size": 250_000},                                 # unknown asset
        {"asset": symbol, "size": 0},
        {"asset": "", "size": 1}, {"asset": symbol, "size": "lots"}, "junk",
        {"asset": "XAUUSD", "size": 3, "price": 2400.0, "sigma_bar": 0.0007},
        {"asset": symbol, "size": 10, "currency": "CHF"},
    ]
    reqs = [{"positions": base, "horizon_bars": h, "confidence": c}
            for h in (1, 15, 60, 240, 1440, 5000) for c in (0.95, 0.99)]
    reqs += [{"positions": book, "horizon_bars": 60, "confidence": 0.99},
             {"positions": book, "horizon_bars": 17.9, "confidence": 0.97},
             {"positions": [{"asset": symbol, "size": -1_000_000}], "horizon_bars": 240, "confidence": 0.999},
             {"positions": base, "horizon_bars": 0, "confidence": 1.5},
             {"positions": base, "horizon_bars": 10_000_000, "confidence": 0.99999},
             {"positions": []}, {}]
    return reqs


def simulate_requests(symbol: str) -> list[dict]:
    return [
        {"symbol": symbol, "size": 1_000_000, "steps": 60, "method": "garch", "n_paths": 2000, "seed": 1},
        {"symbol": symbol, "size": None, "steps": 1440, "method": "bootstrap", "n_paths": 2000, "seed": 1},
        {"symbol": symbol, "size": -5, "steps": 5000, "method": "nope", "n_paths": 2000, "seed": 1},
        {"symbol": symbol, "size": 2, "steps": 15, "method": "garch", "n_paths": 50, "seed": 1, "alpha": 0.4,
         "beta": 0.9, "vol_mult": 25},
    ]


def api_run(dataset: str) -> tuple[dict, list[dict]]:
    """The real app, call for call: jump to the first tornado, then resume to the end of the file."""
    app = create_app(AlertManager(notifiers=[]))
    regimes = []
    with TestClient(app) as c:
        cat = c.get("/datasets").json()
        symbol = c.post("/replay", params={"dataset": dataset, "speed": "max", "pause_on": "tornado"}).json()["symbol"]

        def settle():
            t0 = time.time()
            while time.time() - t0 < 120:
                s = c.get("/score").json()
                if s["session"]["state"] != "running":
                    return s
                time.sleep(0.05)
            raise RuntimeError(f"{dataset}: replay did not settle")

        def stage(name: str) -> dict:
            score = settle()
            reg = app.state.manager.regime(symbol)
            regimes.append({"name": f"{dataset} {name}", "regime": clean(reg)})
            return {
                "score": score,
                "history": c.get("/history", params={"n": 1200}).json(),
                "history_50": c.get("/history", params={"n": 50}).json(),
                "alerts": c.get("/alerts").json(),
                "health": c.get("/health").json(),
                "exposure": [{"request": r, "response": c.post("/exposure", json=r).json()}
                             for r in exposure_requests(symbol, score["price"])],
                "simulate": [{"request": r, "response": c.post("/simulate", json=r).json()}
                             for r in simulate_requests(symbol)],
            }

        frozen = stage("frozen")
        c.post("/resume")
        finished = stage("finished")
    assert finished["score"]["session"]["state"] == "ended"
    return {"catalogue_entry": cat["datasets"][dataset], "scoring": cat["scoring"], "frozen": frozen,
            "finished": finished}, regimes


def fan_steps(steps: int) -> list[int]:
    return np.unique(np.linspace(0, steps - 1, min(steps, 200)).round().astype(int)).tolist()


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    cases = []
    for name in STATIC_SETS:
        n = per_tick(name, out / f"{name}.ticks.jsonl")
        api, regimes = api_run(name)
        (out / f"{name}.api.json").write_text(dump(api))
        for r in regimes:
            if not r["regime"]["ready"]:
                continue
            for method, steps, value in (("garch", 60, None), ("garch", 240, -1_000_000.0), ("bootstrap", 60, 1_000_000.0)):
                params = {"steps": steps, "n_paths": 20000, "method": method}
                reg = dict(r["regime"], returns=np.asarray(r["regime"]["returns"], dtype=float))
                cases.append({"name": f"{r['name']} {method} {steps}", "regime": r["regime"], "value": value,
                              "params": params, "result": clean(simulate(reg, value, seed=7, **params))})
        frozen = api["frozen"]["score"]
        print(f"{name}: {n} ticks; frozen at {frozen['session']['pause_reason']} "
              f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(frozen['ts']))} UTC, score {frozen['score']:.1f}, "
              f"pct_to_low {(frozen['session']['ahead'] or {}).get('pct_to_low')}")
    (out / "simulate_stats.json").write_text(dump({
        "cases": cases, "fan_steps": [[s, fan_steps(s)] for s in (1, 2, 3, 15, 60, 199, 200, 201, 240, 399, 400, 401,
                                                                  1000, 1440, 4999, 5000)]}))
    (out / "datasets.json").write_text(json.dumps(STATIC_SETS))


if __name__ == "__main__":
    main()
