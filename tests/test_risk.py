"""Exposure, cyclone simulator and alerts."""
import math

import numpy as np
import pytest
from conftest import finite_tree

from stormchaser.alerts import AlertManager, NullNotifier, TelegramNotifier, default_notifiers
from stormchaser.exposure import compute_exposure, norm_ppf, var_es, worst_move
from stormchaser.simulate import MAX_PATHS, MAX_STEPS, simulate


def regime(price=20000.0, sf=0.001, sb=0.0005, n=0, ready=True, symbol="BTCUSDT", seed=0):
    rets = np.random.default_rng(seed).standard_t(4, n) * sf / math.sqrt(2) if n else np.array([])
    return {"ready": ready, "price": price, "bar_seconds": 60.0, "sigma_fast": sf, "sigma_base": sb, "returns": rets,
            "level": "warning", "symbol": symbol}


def book(**assets):
    return lambda a: next((r for k, r in assets.items() if a.upper().replace("-", "").startswith(k)), None)


# -------------------------------------------------------------- exposure
def test_var_hand_check_normal():
    """1 BTC at 20,000; sigma 0.1%/bar; 60 bars -> sigma_h = 0.7746%.
    VaR99 = 20000 * (1 - exp(-2.3263 * 0.007746)) = 357.17
    ES99  = 20000 * (1 - exp(-2.6652 * 0.007746)) = 408.69
    Calm baseline (sigma 0.05%): VaR99 = 20000 * (1 - exp(-2.3263 * 0.003873)) = 179.39"""
    r = compute_exposure([{"asset": "BTC", "size": 1, "entry_price": 21000}], book(BTC=regime()), 60, 0.99)
    p, t = r["positions"][0], r["totals"]["USD"]
    assert p["method"] == "normal" and p["value"] == 20000 and p["pnl"] == -1000
    assert p["var"] == pytest.approx(357.17, abs=0.01)
    assert p["es"] == pytest.approx(408.66, abs=0.01)
    assert p["var_baseline"] == pytest.approx(179.39, abs=0.01) and p["vol_ratio"] == 2.0
    assert t["var"] == p["var"] and t["stressed_loss"] >= t["es"] >= t["var"]


def test_norm_ppf_and_short_side():
    assert norm_ppf(0.99) == pytest.approx(2.326348, abs=1e-6) and norm_ppf(0.9772499) == pytest.approx(2.0, abs=1e-6)
    assert norm_ppf(0.001) == pytest.approx(-3.090232, abs=1e-6)
    long_var, _ = var_es(20000, 0.007746, 0.99)
    short_var, _ = var_es(-20000, 0.007746, 0.99)
    assert short_var == pytest.approx(20000 * (math.exp(2.326348 * 0.007746) - 1), abs=0.01) and short_var > long_var


def test_empty_and_zero_portfolios():
    for positions in ([], None):
        r = compute_exposure(positions, book())
        assert r["totals"] == {} and r["positions"] == [] and "empty portfolio" in r["notes"][0]
    r = compute_exposure([{"asset": "BTC", "size": 0}], book(BTC=regime()))
    assert r["positions"][0]["status"] == "zero" and r["totals"]["USD"]["var"] == 0
    assert r["totals"]["USD"]["stressed_loss_pct_gross"] == 0 and "nothing at risk" in r["notes"][0]


def test_unknown_asset_and_bad_inputs_are_reported_per_position():
    r = compute_exposure([
        {"asset": "BTC", "size": 1}, {"asset": "DOGE", "size": 100}, {"asset": "BTC", "size": float("nan")},
        {"asset": "BTC", "size": "ten"}, {"size": 1}, "junk", {"asset": "", "size": 1},
        {"asset": "BTC", "size": 1, "currency": "EUR"}, {"asset": "GOLD", "size": 2, "price": 2000, "sigma_bar": 0.001},
    ], book(BTC=regime()))
    st = [p["status"] for p in r["positions"]]
    assert st == ["ok", "unknown_asset", "error", "error", "error", "error", "error", "error", "ok"]
    assert "DOGE" in r["positions"][1]["error"]
    assert r["totals"]["USD"]["value"] == 20000 + 4000 and finite_tree(r)


def test_notional_currency_netting_and_fx_quote():
    r = compute_exposure([{"asset": "BTCUSDT", "size": 10000, "currency": "USD"},
                          {"asset": "BTC", "size": -0.25, "currency": "BTC"},
                          {"asset": "USDJPY", "size": 1_000_000}],
                         book(BTC=regime(), USDJPY=regime(price=160.0, symbol="USDJPY")))
    a, b, c = r["positions"]
    assert a["units"] == 0.5 and a["value"] == 10000 and b["value"] == -5000
    assert c["currency"] == "JPY" and c["value"] == 160_000_000
    assert set(r["totals"]) == {"USD", "JPY"} and r["totals"]["USD"]["value"] == 5000
    assert r["totals"]["USD"]["gross"] == 15000
    assert r["totals"]["USD"]["var_diversified"] < r["totals"]["USD"]["var"]


def test_warming_up_asset_has_pnl_but_no_risk_numbers():
    r = compute_exposure([{"asset": "BTC", "size": 2, "entry_price": 19000}], book(BTC=regime(ready=False)))
    p = r["positions"][0]
    assert p["status"] == "warming_up" and p["pnl"] == 2000 and p["var"] is None
    assert r["totals"]["USD"]["incomplete"] is True


@pytest.mark.parametrize("h,c", [(0, 0.99), (-5, 2.0), (float("nan"), float("nan")), (10**12, 0.2), ("x", None)])
def test_extreme_exposure_parameters_are_clamped(h, c):
    r = compute_exposure([{"asset": "BTC", "size": 1}], book(BTC=regime(n=300)), h, c)
    assert r["notes"] and finite_tree(r) and r["positions"][0]["status"] == "ok"
    assert 1 <= r["horizon_bars"] <= 100_000 and 0.5 <= r["confidence"] < 1


def test_stressed_loss_uses_the_worst_move_seen_in_the_regime():
    reg = regime(n=400)
    reg["returns"][200:230] = -0.004                                     # a 30-bar slide of 11.3% in the window
    r = compute_exposure([{"asset": "BTC", "size": 1}, {"asset": "BTC", "size": -1}], book(BTC=reg), 30, 0.99)
    long, short = r["positions"]
    assert long["worst_regime_move"] == pytest.approx(math.expm1(-0.12), abs=1e-9)
    assert long["stressed_loss"] == pytest.approx(20000 * (1 - math.exp(-0.12)), abs=0.01)   # 2,261.59
    assert long["stressed_loss"] > long["es"] > long["var"] > 0
    assert short["stressed_loss"] >= short["es"] > short["var"] > 0 and short["stressed_loss"] < long["stressed_loss"]
    assert worst_move(reg["returns"], 30, 1.0) < 0 < worst_move(reg["returns"], 30, -1.0)
    assert worst_move(np.array([]), 30, 1.0) == 0.0
    assert worst_move(np.array([0.01, -0.02]), 4, 1.0) == pytest.approx(-0.04)   # short history: sqrt-time scaling


# ------------------------------------------------------------- simulator
def test_simulator_fan_is_ordered_reproducible_and_reverts_to_baseline():
    reg = regime(n=400)
    a = simulate(reg, value=100_000, steps=120, n_paths=4000, seed=42)
    b = simulate(reg, value=100_000, steps=120, n_paths=4000, seed=42)
    assert a == b and a["status"] == "ok" and a["clamped"] == []
    f = a["fan"]
    assert len(f["step"]) == 120 and f["step"][0] == 1 and f["step"][-1] == 120
    assert all(lo <= mid <= hi for lo, mid, hi in zip(f["pnl_p5"], f["pnl_p50"], f["pnl_p95"]))
    assert f["pnl_p5"][-1] < f["pnl_p5"][0] < 0 < f["pnl_p95"][0] < f["pnl_p95"][-1]      # fan widens with time
    # storm vol 0.1% decays toward the 0.05% baseline: band is narrower than constant storm vol would give
    assert abs(f["pnl_p5"][-1]) < 100_000 * 1.645 * 0.001 * math.sqrt(120)
    assert a["terminal"]["expected_shortfall_5"] <= a["terminal"]["pnl_p5"]
    assert a["model"]["sigma_start"] == 0.001 and a["model"]["sigma_long_run"] == 0.0005


def test_short_position_fan_is_mirrored():
    reg = regime(n=400)
    long = simulate(reg, value=50_000, steps=30, seed=1)
    short = simulate(reg, value=-50_000, steps=30, seed=1)
    assert short["fan"]["pnl_p5"][-1] == pytest.approx(-long["fan"]["pnl_p95"][-1])
    assert all(lo <= hi for lo, hi in zip(short["fan"]["pnl_p5"], short["fan"]["pnl_p95"]))


@pytest.mark.parametrize("kw", [
    {"steps": 10**9, "n_paths": 10**9}, {"steps": -1, "n_paths": 0}, {"steps": float("nan"), "n_paths": float("inf")},
    {"alpha": 5, "beta": 5}, {"alpha": -1, "beta": 0.9999}, {"vol_mult": 1e9}, {"vol_mult": 0}, {"method": "magic"},
    {"steps": "many"}, {"seed": float("nan")},
])
def test_extreme_monte_carlo_parameters_are_clamped_and_finite(kw):
    r = simulate(regime(n=400), **kw)
    assert r["status"] == "ok" and (r["clamped"] or "seed" in kw)
    assert 1 <= r["steps"] <= MAX_STEPS and r["n_paths"] <= MAX_PATHS and r["steps"] * r["n_paths"] <= 4_000_000
    assert finite_tree(r) and len(r["fan"]["step"]) <= 200


def test_simulator_refuses_without_a_calibrated_regime():
    assert simulate(regime(ready=False))["status"] == "warming_up"
    assert simulate({"price": None})["status"] == "no_price"
    assert simulate(regime(price=float("nan")))["status"] == "no_price"
    assert simulate(regime(sf=0.0))["status"] == "warming_up"


def test_bootstrap_and_its_fallback():
    r = simulate(regime(n=400), method="bootstrap", steps=50, seed=3)
    assert r["method"] == "bootstrap" and finite_tree(r)
    r = simulate(regime(n=10), method="bootstrap")
    assert r["method"] == "garch" and "bootstrap" in r["clamped"][0]


def test_wild_regime_returns_do_not_break_the_simulator():
    reg = regime(n=400)
    reg["returns"][::7] = np.nan
    reg["returns"][3] = 50.0
    assert finite_tree(simulate(reg, steps=200, seed=1)) and finite_tree(simulate(reg, method="bootstrap", seed=1))


# ---------------------------------------------------------------- alerts
class Capture:
    name = "capture"

    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return True


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def test_alert_rules_and_debounce():
    cap, clock = Capture(), Clock()
    am = AlertManager([cap], min_level="warning", debounce_s=300, clock=clock)
    assert am.on_transition("BTC", "green", "watch", 30, 100) is False       # below min level
    assert am.on_transition("BTC", "watch", "warning", 55, 100) is True
    assert am.on_transition("BTC", "tornado", "warning", 55, 100) is False   # downgrade: never alert
    assert am.on_transition("BTC", "watch", "warning", 56, 100) is False     # flap inside the window: debounced
    assert am.on_transition("BTC", "warning", "tornado", 80, 95) is True     # escalation always goes out
    assert am.on_transition("ETH", "watch", "warning", 55, 10) is True       # other symbol is independent
    assert am.on_transition("BTC", "green", "storm", 99, 1) is False         # unknown level
    clock.t = 301
    assert am.on_transition("BTC", "watch", "warning", 57, 100) is True      # window passed
    assert len(cap.sent) == 4 and am.suppressed == 1 and "TORNADO" in cap.sent[1] and "80/100" in cap.sent[1]


def test_replay_alerts_debounce_in_data_time():
    cap = Capture()
    am = AlertManager([cap], debounce_s=300, clock=Clock())                   # wall clock never moves
    assert am.on_transition("BTC", "green", "tornado", 90, 100, ts=1000.0)
    assert not am.on_transition("BTC", "green", "tornado", 90, 100, ts=1200.0)
    assert am.on_transition("BTC", "green", "tornado", 90, 100, ts=1400.0, note="driver: jump")
    assert "driver: jump" in cap.sent[-1]


def test_stale_alert_and_failing_notifier_is_isolated():
    class Broken:
        name = "broken"

        def send(self, text):
            raise RuntimeError("boom")

    cap = Capture()
    am = AlertManager([Broken(), cap], clock=Clock())
    assert am.on_stale("BTC", 12.0) and "STALE" in cap.sent[0]
    assert not am.on_stale("BTC", 13.0)                                       # debounced
    assert am.history[0]["kind"] == "stale"


def test_telegram_is_a_noop_without_env(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    calls = []
    tg = TelegramNotifier(post=lambda *a: calls.append(a))
    assert tg.enabled is False and tg.send("hi") is False and calls == []
    assert [n.name for n in default_notifiers()] == ["log"] and NullNotifier().send("x") is False
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")                             # only one of the two set
    assert TelegramNotifier().enabled is False


def test_telegram_sends_when_configured_and_swallows_errors(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    calls = []

    class Resp:
        status_code = 200

    tg = TelegramNotifier(post=lambda url, payload: calls.append((url, payload)) or Resp())
    assert tg.enabled and tg.send("storm") is True
    assert calls[0][0].endswith("/bot123:abc/sendMessage") and calls[0][1]["chat_id"] == "42"
    assert [n.name for n in default_notifiers()] == ["log", "telegram"]

    def boom(url, payload):
        raise TimeoutError("network down")

    assert TelegramNotifier(post=boom).send("storm") is False                 # never raises into the engine
    Resp.status_code = 401
    assert tg.send("storm") is False
