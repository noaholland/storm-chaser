"""Offline run of the storm engine over a file: prints level transitions and time-in-level.

usage: python scripts/backtest.py <csv|parquet> [bar_seconds] [select_symbol]
"""
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stormchaser.adapters.replay import load_ticks  # noqa: E402
from stormchaser.score import StormConfig, StormEngine  # noqa: E402


def fmt(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts))


def main():
    path = sys.argv[1]
    sel = sys.argv[3] if len(sys.argv) > 3 and "=" not in sys.argv[3] and not sys.argv[3].startswith("-") else None
    win = dict(a.split("=") for a in sys.argv[2:] if a.startswith(("start=", "end=")))
    ticks, rep = load_ticks(path, select=sel, **win)
    bar = float(sys.argv[2]) if len(sys.argv) > 2 and "=" not in sys.argv[2] and not sys.argv[2].startswith("-") else rep["median_step_s"]
    print({k: v for k, v in rep.items() if k != "columns"}, rep["columns"], "bar", bar)
    over = {a.split("=")[0]: float(a.split("=")[1]) for a in sys.argv[2:] if "=" in a and a not in [f"{k}={v}" for k, v in win.items()]}   # e.g. base_halflife=720
    eng = StormEngine("X", StormConfig(bar_seconds=bar, **over), clock=lambda: 0.0)
    levels = Counter()
    kinds, ep_start, episodes, unwind = Counter(), None, [], []
    for t in ticks:
        eng.update(t)
        levels[eng.level] += 1
        kinds[eng.kind] += 1
        if eng.level_idx >= 2 and ep_start is None:
            ep_start = (t.ts, eng.level_idx)
        elif ep_start and eng.level_idx >= 2:
            ep_start = (ep_start[0], max(ep_start[1], eng.level_idx))
        elif ep_start and eng.level_idx < 1:
            episodes.append((ep_start[0], t.ts, ep_start[1]))
            ep_start = None
        if eng.carry.get("unwind") and (not unwind or t.ts - unwind[-1] > 3600):
            unwind.append(t.ts)
    print("kinds", dict(kinds), "| carry", {k: round(v, 4) for k, v in (eng.carry.get("curve") or {}).items()})
    print("episodes (warning+ until back to green):")
    for a, b, lv in episodes if "--episodes" in sys.argv else []:
        print(f"  {fmt(a)} -> {fmt(b)}  {(b - a) / 60:6.0f} min  max {['', '', 'warning', 'TORNADO'][lv]}")
    dur = sorted((b - a) / 60 for a, b, _ in episodes)
    print(f"{len(episodes)} warning+ episodes, {sum(lv == 3 for *_, lv in episodes)} reached tornado; "
          f"median {dur[len(dur) // 2] if dur else 0:.0f} min, longest {dur[-1] if dur else 0:.0f} min")
    print("carry-unwind signals:", [fmt(u) for u in unwind])
    lo = min(ticks, key=lambda t: t.price)
    print("low", fmt(lo.ts), lo.price, "| rejected", dict(eng.rejected), "gaps", eng.gaps)
    print("time in level", dict(levels), "peak", eng.peak_score)
    for tr in eng.transitions:
        if "--transitions" in sys.argv and ("tornado" in (tr.frm, tr.to) or "warning" in (tr.frm, tr.to)):
            print(f"  {fmt(tr.ts)}  {tr.frm:>8} -> {tr.to:<8} score {tr.score:5.1f}  price {tr.price:.6g}  {tr.why} "
                  + " ".join(f"{k}={v:.2f}" for k, v in tr.components.items()))
    print("transitions total", len(eng.transitions))


if __name__ == "__main__":
    main()
