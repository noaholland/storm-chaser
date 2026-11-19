"""Precompute the bundled crisis replays into static "tapes" for the backend-free demo site.

Every number on a tape comes out of the real stack: the replay runs through SessionManager / Session /
StormEngine exactly as `POST /replay` would run it, and after each tick the session's own snapshot() is
recorded. The browser only plays the tape back (web/src/static/), it never scores anything.

    python scripts/build_static.py [out_dir]        # default: docs/data

Tape layout (JSON, columnar):
    cols        "run:<path>" / "paused:<path>" -> column, one value per tick, for every leaf of snapshot().
                run = the reading while the replay is flowing, paused = the same tick while frozen
                (only the leaves that differ are stored). A column is {"k": v} (constant),
                {"a": [...]} (plain values), {"d": [unique values], "i": [index per tick, -1 = absent]}
                or {"=": "other column"} (identical to another column).
    tick_ts / tick_price    the raw replay file, for the "how far it still had to fall" hindsight callout
    hist        tick indices at which the session appended a point to its chart history
    transitions / tr_tick   every level change and the tick it happened on
    alerts / alert_tick / suppressed_tick   the alert log as the AlertManager produced it
    returns / ret_tick      per-bar returns as the engine stored them (its regime window is the last 500)
    ended       the final snapshot once the replay has run out
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stormchaser.adapters.replay import MAX_SPEED, MIN_SPEED, ReplaySource  # noqa: E402
from stormchaser.alerts import AlertManager  # noqa: E402
from stormchaser.api import scoring_summary  # noqa: E402
from stormchaser.session import DATASETS, SessionManager, clean  # noqa: E402

STATIC_SETS = ["sgx_usdjpy_storm", "btc_ftx_2022", "gbpusd_minibudget_2022"]
# produced by the player in the browser (they depend on wall time, speed and where the user paused)
PLAYER_KEYS = {"session", "new_transitions"}
PLAYER_FEED_KEYS = {"age_s", "stale_after_s"}
SEP = "."


class RecordingSource(ReplaySource):
    """A ReplaySource that calls `after(i)` once the session has fully processed tick i."""

    def __init__(self, ticks, after):
        super().__init__(ticks, speed=MAX_SPEED)
        self._after = after

    async def _gen(self):
        for i, t in enumerate(self.ticks):
            if i % 500 == 0:
                await asyncio.sleep(0)
            self.position = i + 1
            yield t
            self._after(i)


class LoggedDeque(deque):
    """The engine's regime window, with every append also kept in order."""

    def __init__(self, maxlen, on_append):
        super().__init__(maxlen=maxlen)
        self._on_append = on_append

    def append(self, x):
        self._on_append(x)
        super().append(x)


def flatten(o, prefix="", out=None) -> dict:
    """Nested dict -> {path: leaf}. Lists and empty dicts are leaves."""
    out = {} if out is None else out
    if isinstance(o, dict) and o:
        for k, v in o.items():
            assert SEP not in k, f"key {k!r} contains the path separator"
            flatten(v, f"{prefix}{SEP}{k}" if prefix else k, out)
    else:
        out[prefix] = o
    return out


def strip(snap: dict) -> dict:
    """Drop the parts of a snapshot that the player produces itself."""
    s = {k: v for k, v in snap.items() if k not in PLAYER_KEYS}
    s["feed"] = {k: v for k, v in snap["feed"].items() if k not in PLAYER_FEED_KEYS}
    return s


def encode_columns(rows: list[dict]) -> dict:
    """rows[i] = {column name: value} (a missing name = absent at that tick)."""
    names: list[str] = []
    for r in rows:
        for k in r:
            if k not in names:
                names.append(k)
    cols, seen = {}, {}
    absent = object()
    for name in names:
        vals = [r.get(name, absent) for r in rows]
        keys = [None if v is absent else json.dumps(v, sort_keys=True, allow_nan=False) for v in vals]
        sig = "\x00".join("\x01" if k is None else k for k in keys)
        if sig in seen:
            cols[name] = {"=": seen[sig]}
            continue
        seen[sig] = name
        uniq: dict[str, int] = {}
        for k in keys:
            if k is not None and k not in uniq:
                uniq[k] = len(uniq)
        has_absent = any(k is None for k in keys)
        if len(uniq) == 1 and not has_absent:
            cols[name] = {"k": vals[0]}
        elif not has_absent and len(uniq) > len(vals) // 3:
            cols[name] = {"a": vals}
        else:
            cols[name] = {"d": [json.loads(k) for k in uniq], "i": [-1 if k is None else uniq[k] for k in keys]}
    return cols


async def record(dataset: str) -> dict:
    mgr = SessionManager(AlertManager(notifiers=[]))
    sess = await mgr.start_replay(dataset, MAX_SPEED)
    # the session task has been created but has not run yet: swap in the recording source and a frozen clock
    eng, ticks = sess.engine, sess.source.ticks
    rows, hist, tr_tick, alert_tick, suppressed_tick, returns, ret_tick = [], [], [], [], [], [], []
    state = {"i": -1, "hist": 0, "tr": 0, "alerts": 0, "suppressed": 0}
    eng.clock = lambda: 0.0
    eng.returns = LoggedDeque(eng.cfg.regime_window, lambda x: (returns.append(float(x)), ret_tick.append(state["i"] + 1)))

    def after(i: int) -> None:
        state["i"] = i
        run = flatten(strip(clean(sess.snapshot())))
        sess.paused = True
        paused = flatten(strip(clean(sess.snapshot())))
        sess.paused = False
        row = {f"run:{k}": v for k, v in run.items()}
        row.update({f"paused:{k}": v for k, v in paused.items() if k not in run or run[k] != v})
        assert set(paused) == set(run), "paused snapshot has a different shape"
        rows.append(row)
        if len(sess.history) != state["hist"]:
            assert len(sess.history) == state["hist"] + 1 and len(sess.history) < sess.history.maxlen
            state["hist"] = len(sess.history)
            h = sess.history[-1]
            assert (h[0], h[1], h[2], h[3]) == (eng.price_ts, eng.price, eng.score, eng.level)
            hist.append(i)
        for _ in range(len(eng.transitions) - state["tr"]):
            tr_tick.append(i)
        state["tr"] = len(eng.transitions)
        for _ in range(len(mgr.alerts.history) - state["alerts"]):
            alert_tick.append(i)
        state["alerts"] = len(mgr.alerts.history)
        for _ in range(mgr.alerts.suppressed - state["suppressed"]):
            suppressed_tick.append(i)
        state["suppressed"] = mgr.alerts.suppressed
        if i % 997 == 0:                      # the logged returns really are the engine's regime window
            assert list(eng.returns) == returns[-eng.cfg.regime_window:]

    sess.source = RecordingSource(ticks, after)
    await sess.task
    assert sess.state == "ended" and len(rows) == len(ticks), (sess.state, sess.error)
    ended = clean(sess.snapshot())
    session = {k: ended["session"][k] for k in ("kind", "error", "dataset", "reconnects", "feed_error", "title",
                                                "bar_seconds", "curve_tenors")}
    # paused columns that never differ from the running reading are dropped; the player falls back to run:
    cols = encode_columns(rows)
    alerts = [{k: v for k, v in a.items() if k != "wall_time"} for a in clean(list(mgr.alerts.history))]
    assert len(mgr.alerts.history) < mgr.alerts.history.maxlen
    return {
        "v": 1, "dataset": dataset, "symbol": sess.symbol, "n": len(ticks), "session": session,
        "warmup_bars": eng.cfg.warmup_bars, "regime_window": eng.cfg.regime_window,
        "debounce_s": mgr.alerts.debounce_s,
        "tick_ts": [t.ts for t in ticks], "tick_price": [t.price for t in ticks],
        "cols": cols, "hist": hist,
        "transitions": clean([t.__dict__ for t in eng.transitions]), "tr_tick": tr_tick,
        "alerts": alerts, "alert_tick": alert_tick, "suppressed_tick": suppressed_tick,
        "returns": returns, "ret_tick": ret_tick,
        "ended": strip(ended),
    }


def catalogue() -> dict:
    return {
        "datasets": {k: {x: v for x, v in DATASETS[k].items() if x != "file"} for k in STATIC_SETS},
        "yfinance_presets": {}, "live": [],
        "speed": {"min": MIN_SPEED, "max": MAX_SPEED}, "scoring": scoring_summary(),
    }


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "data"
    out.mkdir(parents=True, exist_ok=True)
    (out / "catalogue.json").write_text(json.dumps(clean(catalogue()), allow_nan=False, separators=(",", ":")))
    for name in STATIC_SETS:
        tape = asyncio.run(record(name))
        p = out / f"{name}.json"
        p.write_text(json.dumps(tape, allow_nan=False, separators=(",", ":")))
        print(f"{name}: {tape['n']} ticks, {len(tape['transitions'])} level changes, {len(tape['alerts'])} alerts, "
              f"{p.stat().st_size / 1e6:.2f} MB")
    (out.parent / ".nojekyll").write_text("")


if __name__ == "__main__":
    main()
