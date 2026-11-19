"""Storm score engine: every feed edge case the judges listed, plus the real crisis datasets."""
import math
import time

import pytest
from conftest import DATA, FakeClock, extend, run, walk

from stormchaser.adapters.replay import load_ticks
from stormchaser.models import Tick
from stormchaser.score import LEVELS, StormConfig, StormEngine


# ------------------------------------------------------------------ warm-up
def test_no_data_is_not_green():
    snap = StormEngine("X").snapshot()
    assert snap["state"] == "no_data" and snap["level"] == "no_data" and snap["score"] is None


def test_too_little_history_reports_warming_up_not_zero():
    eng, levels = run(walk(40), warmup_bars=60)
    snap = eng.snapshot()
    assert set(levels) == {"warming_up"}
    assert snap["state"] == "warming_up" and snap["level"] == "warming_up"
    assert snap["score"] is None and snap["last_score"] is None        # not 0, not green
    assert snap["warmup"] == {"have": 39, "need": 60}


def test_score_appears_after_warmup_and_calm_market_is_green():
    eng, levels = run(walk(600))
    assert levels[59] == "warming_up" and levels[61] == "green"
    assert eng.snapshot()["state"] == "live" and 0 <= eng.score < 25
    assert levels.count("warning") + levels.count("tornado") == 0       # no false alarm on a plain random walk


def test_single_tick_and_constant_price_do_not_crash():
    eng, _ = run([Tick(1.0, 100.0)])
    assert eng.snapshot()["state"] == "warming_up"
    eng, levels = run([Tick(60.0 * i, 100.0) for i in range(300)])      # zero variance
    assert levels[-1] == "green" and eng.score == 0


# ------------------------------------------------------------- bad ticks
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), 0.0, -5.0, None, "abc", True])
def test_invalid_prices_are_rejected_and_do_not_move_the_score(bad):
    ticks = walk(300)
    clean, _ = run(ticks)
    dirty_ticks = []
    for i, t in enumerate(ticks):
        dirty_ticks.append(t)
        if i % 10 == 5:
            dirty_ticks.append(Tick(t.ts + 1.0, bad))
    dirty, _ = run(dirty_ticks)
    assert dirty.rejected["bad_price"] == 30
    assert dirty.score == clean.score and dirty.price == clean.price and dirty.n_ret == clean.n_ret


@pytest.mark.parametrize("ts", [float("nan"), float("inf"), None, "yesterday"])
def test_invalid_timestamps_are_rejected(ts):
    eng = StormEngine("X")
    assert eng.update(Tick(ts, 100.0)) is False
    assert eng.rejected["bad_timestamp"] == 1 and eng.price is None


def test_bad_or_crossed_quotes_drop_the_quote_but_keep_the_price():
    eng = StormEngine("X")
    assert eng.update(Tick(1.0, 100.0, bid=101.0, ask=99.0))            # crossed
    assert eng.update(Tick(2.0, 100.0, bid=float("nan"), ask=100.1))
    assert eng.update(Tick(3.0, 100.0, bid=-1.0, ask=100.1))
    assert eng.rejected["crossed_quote"] == 1 and eng.rejected["bad_quote"] == 2
    assert eng.price == 100.0 and eng.bid is None and eng.accepted == 3
    assert eng.update(Tick(4.0, None, bid=99.0, ask=101.0)) and eng.price == 100.0   # mid from quote


def test_duplicate_and_out_of_order_timestamps_are_dropped():
    ticks = walk(300)
    clean, _ = run(ticks)
    noisy = []
    for i, t in enumerate(ticks):
        noisy.append(t)
        if i % 7 == 3:
            noisy.append(Tick(t.ts, t.price * 1.5))                     # duplicate timestamp, different price
        if i % 11 == 4 and i > 5:
            noisy.append(Tick(ticks[i - 3].ts, ticks[i - 3].price * 0.5))   # late, out of order
    eng, _ = run(noisy)
    assert eng.rejected["duplicate_ts"] == len([i for i in range(300) if i % 7 == 3])
    assert eng.rejected["out_of_order"] == len([i for i in range(6, 300) if i % 11 == 4])
    assert eng.score == clean.score and eng.price == clean.price


# ------------------------------------------------------------------ gaps
def test_weekend_gap_is_not_a_jump():
    ticks = walk(500)
    # two days closed; price reopens 0.5% away, which is small for 48h at this vol (sigma*sqrt(2880) ~ 5%)
    reopen = Tick(ticks[-1].ts + 2 * 86400, ticks[-1].price * 1.005)
    ticks2 = extend(ticks + [reopen], 200, seed=5)
    eng, levels = run(ticks2)
    assert eng.gaps == 1
    assert set(levels[500:]) == {"green"}
    assert eng.raw["jump_peak_z"] < 4


def test_gap_with_a_genuinely_large_move_still_alerts():
    ticks = walk(500)
    reopen = Tick(ticks[-1].ts + 2 * 86400, ticks[-1].price * 0.75)     # -25% over a weekend
    eng, levels = run(extend(ticks + [reopen], 30, seed=5))
    assert max(LEVELS.index(lv) for lv in levels[500:]) >= 1


# ------------------------------------------------------------- fat finger
def test_single_outlier_tick_is_ignored_as_fat_finger():
    ticks = walk(400)
    clean, clean_levels = run(ticks)
    dirty = ticks[:300] + [Tick(ticks[299].ts + 1.0, ticks[299].price * 1.5)] + ticks[300:]
    eng, levels = run(dirty)
    assert eng.rejected["outlier"] == 1
    assert max(t.price for t in dirty) > 140 and eng.peak_score == clean.peak_score
    assert set(levels[300:]) == {"green"} and eng.score == clean.score


def test_fat_finger_during_warmup_is_ignored():
    ticks = walk(200)
    dirty = ticks[:20] + [Tick(ticks[19].ts + 1.0, ticks[19].price * 10)] + ticks[20:]
    eng, levels = run(dirty)
    assert eng.rejected["outlier"] == 1 and levels[-1] == "green"


def test_real_move_is_confirmed_by_the_next_tick_and_alerts():
    ticks = walk(400)
    crash = [Tick(ticks[-1].ts + 60.0 * (i + 1), ticks[-1].price * 0.90 * (1 - 0.0005 * i)) for i in range(20)]
    eng, levels = run(ticks + crash)
    assert eng.rejected["outlier"] == 0                                # held one tick, then accepted
    assert eng.price == pytest.approx(crash[-1].price)
    assert "tornado" in levels[400:]
    assert eng.snapshot()["feed"]["pending_outlier"] is False


def test_staircase_crash_is_not_mistaken_for_outliers():
    ticks = walk(400)
    px, crash = ticks[-1].price, []
    for i in range(10):
        px *= 0.97
        crash.append(Tick(ticks[-1].ts + 60.0 * (i + 1), px))
    eng, levels = run(ticks + crash + [Tick(crash[-1].ts + 60, px)])
    assert eng.rejected["outlier"] == 0 and eng.price == pytest.approx(px) and "tornado" in levels


# ---------------------------------------------------------------- storms
def test_volatility_storm_reaches_tornado_then_decays():
    ticks = extend(walk(600), 120, sigma=0.012, drift=-0.004, seed=3)   # 12x vol, falling
    eng, levels = run(extend(ticks, 1500, seed=4))
    assert "tornado" in levels[600:760]
    assert levels[-1] == "green"                                        # storm over, calm again
    order = [t.to for t in eng.transitions]
    assert order.index("tornado") < len(order) - 1


def test_regime_rebaselining_new_permanent_vol_level_stops_alerting():
    cfg = StormConfig(base_halflife=100)
    ticks = extend(walk(500, sigma=0.0005), 3000, sigma=0.003, seed=9)  # vol jumps 6x and stays there
    eng, levels = run(ticks, cfg)
    assert max(LEVELS.index(lv) for lv in levels[500:700]) >= 2          # the shift itself alerts
    assert set(levels[-1000:]) == {"green"}                             # ... the new normal does not
    assert eng._sigma_base() == pytest.approx(0.003, rel=0.35)          # baseline learned the new regime


def test_permanent_price_level_shift_stops_alerting_once_stable():
    ticks = walk(500)
    step = [Tick(ticks[-1].ts + 60.0, ticks[-1].price * 0.92)]
    eng, levels = run(extend(ticks + step, 600, seed=6))
    assert max(LEVELS.index(lv) for lv in levels[500:520]) >= 2
    assert set(levels[-300:]) == {"green"}


def test_upside_melt_up_also_counts_as_a_storm():
    eng, levels = run(extend(walk(600), 120, sigma=0.012, drift=+0.004, seed=3))
    assert max(LEVELS.index(lv) for lv in levels[600:]) >= 2


# ------------------------------------------------------------ hysteresis
def drive(eng, scores):
    out = []
    for i, s in enumerate(scores):
        eng.score = s
        eng.components = {"vol": s / 100}
        eng._update_level(float(i), 100.0)
        out.append(eng.level_idx)
    return out


def test_hysteresis_no_flapping_around_a_threshold():
    eng = StormEngine("X")
    lv = drive(eng, [49, 51, 49, 51, 49.5, 50.5, 48, 52] * 5)
    assert lv[0] == 1 and set(lv[1:]) == {2}                            # up once, never back down
    assert len(eng.transitions) == 2                                    # green->watch, watch->warning


def test_downgrade_needs_margin_and_cooldown():
    eng = StormEngine("X", StormConfig(hysteresis=8, cooldown_bars=5))
    assert drive(eng, [80])[-1] == 3
    assert drive(eng, [70] * 20)[-1] == 3                               # inside the 8-point band: hold
    assert drive(eng, [60] * 4)[-1] == 3                                # below band but only 4 bars
    assert drive(eng, [80, 60, 60, 60, 60])[-1] == 3                    # cooldown restarts
    assert drive(eng, [60] * 5)[-1] == 2                                # 5 consecutive: step down
    assert drive(eng, [5] * 5)[-1] == 0
    assert drive(eng, [90])[-1] == 3                                    # upgrades are immediate


# ----------------------------------------------------------------- stale
def test_stale_feed_is_reported_as_stale_not_green(clock):
    eng = StormEngine("X", StormConfig(stale_after_s=5), clock=clock)
    for t in walk(300):
        clock.t += 0.01
        eng.update(t)
    assert eng.snapshot()["level"] == "green"
    clock.t += 5.1
    snap = eng.snapshot()
    assert eng.is_stale() and snap["state"] == "stale" and snap["level"] == "stale"
    assert snap["score"] is None and snap["last_level"] == "green"      # last reading kept, clearly not current
    assert snap["feed"]["age_s"] == pytest.approx(5.1)
    eng.update(Tick(1_800_000_000.0, eng.price))                        # feed comes back
    assert eng.snapshot()["state"] == "live"


def test_invalid_ticks_do_not_keep_a_dead_feed_alive(clock):
    eng = StormEngine("X", StormConfig(stale_after_s=5), clock=clock)
    for t in walk(100):
        eng.update(t)
    clock.t += 10
    eng.update(Tick(2_000_000_000.0, float("nan")))                     # garbage only
    assert eng.snapshot()["state"] == "stale"


# ---------------------------------------------------------------- spread
def quoted(ticks, spread=0.0002):
    return [Tick(t.ts, t.price, t.price * (1 - spread / 2), t.price * (1 + spread / 2)) for t in ticks]


def test_spread_widening_raises_the_score_but_alone_is_only_a_watch():
    base = quoted(walk(400))
    wide = quoted(extend(base, 30, seed=8)[400:], spread=0.004)         # 20x wider
    eng, levels = run(base + wide)
    assert eng.components["spread"] == 1.0 and eng.raw["spread_ratio"] > 6
    assert set(levels[405:]) == {"watch"}                               # liquidity warning, not a storm call
    calm, _ = run(base)
    assert calm.components["spread"] == 0.0


def test_weekend_reopen_spread_is_masked():
    base = quoted(walk(400))
    after = extend(base, 40, seed=8, gap=2 * 86400)[400:]
    eng, levels = run(base + quoted(after, spread=0.004))               # wide quotes right after the gap
    assert eng.gaps == 1 and eng.raw.get("spread_masked") == 1.0
    assert eng.components["spread"] == 0.0 and set(levels[400:]) == {"green"}


def test_no_quotes_means_no_spread_component():
    eng, _ = run(walk(200))
    assert "spread" not in eng.components and eng.score is not None


# ----------------------------------------------------------------- curve
def with_curve(ticks, level=-0.03, shock_from=None, shock=0.0):
    out = []
    for i, t in enumerate(ticks):
        bump = shock if shock_from is not None and i >= shock_from else 0.0
        wob = 1e-5 * math.sin(i)                                        # normal quote noise
        out.append(Tick(t.ts, t.price, extra={"carry": {"1M": level + wob + bump, "3M": level - wob + bump,
                                                        "1Y": level + 2 * wob + bump, "6M": float("nan")}}))
    return out


def trace(ticks):
    eng = StormEngine("TEST", clock=FakeClock())
    rows = []
    for t in ticks:
        eng.update(t)
        rows.append((eng.level, eng.components.get("curve", 0.0), bool(eng.carry.get("unwind"))))
    return eng, rows


def test_forward_curve_dislocation_and_carry_unwind():
    calm = walk(400)
    crash = extend(calm, 60, sigma=0.004, drift=-0.004, seed=3)
    eng, rows = trace(with_curve(crash, shock_from=405, shock=0.004))   # curve reprices 40bp while spot falls
    assert eng.carry["earning_side"] == "long" and set(eng.carry["curve"]) == {"1M", "3M", "1Y"}   # NaN tenor skipped
    assert max(r[1] for r in rows[:400]) == 0.0                         # quiet curve before the shock
    assert rows[405][1] == 1.0                                          # dislocation seen on the bar it happens
    assert any(r[2] for r in rows[405:]) and not any(r[2] for r in rows[:400])
    quiet, qrows = trace(with_curve(calm))
    assert max(r[1] for r in qrows) == 0.0 and not any(r[2] for r in qrows)
    assert quiet.carry["median"] == pytest.approx(-0.03, abs=1e-4)


def test_curve_alone_cannot_raise_a_warning():
    eng, rows = trace(with_curve(walk(500), shock_from=450, shock=0.01))
    assert rows[450][1] == 1.0 and rows[450][0] == "watch"
    assert {r[0] for r in rows} <= {"warming_up", "green", "watch"}


# ------------------------------------------------------------- real data
def replay(name, **kw):
    ticks, rep = load_ticks(DATA / name, **kw)
    eng = StormEngine("X", StormConfig(bar_seconds=rep["median_step_s"]), clock=FakeClock())
    rows = []
    for t in ticks:
        eng.update(t)
        rows.append((t.ts, eng.level_idx, eng.kind, eng._sigma_base()))
    return eng, ticks, rows


def utc(s):
    return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M")) - time.timezone


def test_ftx_collapse_tornado_fires_before_the_low():
    eng, ticks, _ = replay("btc_ftx_2022.csv")
    low = min(ticks, key=lambda t: t.price)
    first = next(t for t in eng.transitions if t.to == "tornado")
    assert first.ts < utc("2022-11-08 12:00") < low.ts                  # alert on 8 Nov, low on 9 Nov
    assert first.price / low.price - 1 > 0.20                           # >20% of downside still ahead
    assert not [t for t in eng.transitions if t.to == "tornado" and t.ts < utc("2022-11-07 12:00")]


def test_gbp_mini_budget_flash_crash_is_a_tornado():
    eng, ticks, _ = replay("gbpusd_minibudget_2022.csv")
    assert eng.gaps >= 1                                                # the weekend before the crash
    t = [x for x in eng.transitions if x.to == "tornado"]
    assert t and utc("2022-09-26 00:00") <= t[0].ts <= utc("2022-09-26 02:00")


@pytest.fixture(scope="module")
def sgx():
    return replay("sgx_usdjpy_2026.parquet")


def test_sgx_usdjpy_storm_is_detected_and_classified(sgx):
    eng, ticks, rows = sgx
    assert ticks[0].bid is not None and set(ticks[0].extra["carry"]) >= {"1M", "3M", "1Y"}
    hit = [t for t in eng.transitions if t.to in ("warning", "tornado") and utc("2026-07-29 00:00") <= t.ts < utc("2026-07-31 00:00")]
    assert any(t.to == "tornado" for t in hit)
    assert any(k == "storm" for ts, _, k, _ in rows if utc("2026-07-30 00:00") <= ts < utc("2026-07-31 00:00"))
    assert any(t.carry_unwind for t in hit)                             # long-USDJPY carry being unwound
    assert eng.rejected == {} and eng.gaps >= 8                         # weekends in the file


@pytest.mark.parametrize("day", ["2026-07-14", "2026-08-07"])
def test_sgx_data_release_spike_is_a_squall_not_a_regime_shift(sgx, day):
    eng, ticks, rows = sgx
    win = [r for r in rows if utc(f"{day} 12:25") <= r[0] <= utc(f"{day} 15:00")]
    assert max(r[1] for r in win) >= 2                                  # it is noticed ...
    assert all(r[2] != "storm" for r in win)                            # ... labelled a squall ...
    assert win[-1][1] == 0                                              # ... and over within 2.5 hours
    assert win[-1][3] / win[0][3] < 1.25                                # baseline vol barely moved


def test_sgx_daily_roll_and_weekend_reopen_do_not_alert(sgx):
    eng, ticks, rows = sgx
    quiet = [r for r in rows if 21 <= (r[0] // 3600) % 24 < 22 and r[0] < utc("2026-07-28 00:00")
             and r[0] > utc("2026-07-13 00:00")]
    assert max(r[1] for r in quiet) <= 1                                # wide roll-time spreads: watch at most
