"""HTTP API end to end (in-process): sessions, score, stream, exposure, simulate, error paths."""
import asyncio
import json
import time

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import stormchaser.session as session_mod
from stormchaser.alerts import AlertManager
from stormchaser.api import create_app
from stormchaser.models import Tick


class Capture:
    name = "capture"

    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return True


@pytest.fixture
def cap():
    return Capture()


@pytest.fixture
def client(cap):
    with TestClient(create_app(AlertManager([cap]))) as c:
        yield c


def wait_for(client, cond, symbol=None, timeout=30.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = client.get("/score", params={"symbol": symbol} if symbol else None).json()
        if cond(s):
            return s
        time.sleep(0.05)
    raise AssertionError(f"timed out; last snapshot state={s.get('state')} level={s.get('level')}")


def ended(s):
    return s["state"] == "ended"


def test_health_and_catalogue_before_any_session(client):
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["sessions"] == {} and h["notifiers"] == ["capture"]
    d = client.get("/datasets").json()
    assert list(d["datasets"])[0] == "sgx_usdjpy_storm" and {"btc_ftx_2022", "gbpusd_minibudget_2022"} <= set(d["datasets"])
    assert "file" not in d["datasets"]["btc_ftx_2022"] and d["yfinance_presets"]["Gold"] == "GC=F"
    assert client.get("/").json()["tagline"] == "Cloudy with a chance of margin calls"


def test_catalogue_explains_the_score_from_the_config(client):
    from stormchaser.score import StormConfig
    c, sc = StormConfig(), client.get("/datasets").json()["scoring"]
    assert {s["key"]: s["weight"] for s in sc["sensors"]} == {
        "vol": c.w_vol, "jump": c.w_jump, "drawdown": c.w_dd, "spread": c.w_spread, "curve": c.w_curve}
    assert [s["key"] for s in sc["sensors"] if not s["core"]] == ["spread", "curve"]
    assert sc["thresholds"] == {"watch": c.thr_watch, "warning": c.thr_warning, "tornado": c.thr_tornado}
    assert sc["hysteresis"] == c.hysteresis and sc["cooldown_bars"] == c.cooldown_bars
    assert sc["squall_min"] == 90 and sc["mean_share"] + sc["worst_share"] == 1


def test_no_session_is_an_explicit_error_never_a_fake_score(client):
    assert client.get("/score").status_code == 409
    assert client.get("/stream").status_code == 409
    assert client.get("/history").status_code == 409
    assert client.post("/simulate", json={}).status_code == 409
    assert client.get("/score", params={"symbol": "DOGE"}).status_code == 404
    assert client.post("/stop").json() == {"stopped": False}


@pytest.mark.parametrize("params,code", [
    ({"dataset": "nope"}, 400), ({}, 400), ({"dataset": "btc_ftx_2022", "speed": "-1"}, 422),
    ({"dataset": "btc_ftx_2022", "speed": "0"}, 422), ({"dataset": "btc_ftx_2022", "speed": "nan"}, 422),
    ({"dataset": "btc_ftx_2022", "speed": "inf"}, 422), ({"dataset": "btc_ftx_2022", "speed": "warp"}, 422),
    ({"dataset": "btc_ftx_2022", "bar_seconds": 0}, 422), ({"dataset": "btc_ftx_2022", "pause_on": "green"}, 400),
    ({"path": "../../etc/passwd"}, 400), ({"path": "/etc/passwd"}, 400),
])
def test_replay_rejects_bad_requests(client, params, code):
    r = client.post("/replay", params=params)
    assert r.status_code == code and "detail" in r.json()


def test_replay_speed_above_limit_is_clamped_not_rejected(client):
    r = client.get("/replay", params={"dataset": "gbpusd_minibudget_2022", "speed": "1e30"}).json()
    assert r["speed"] == 1_000_000 and r["meta"]["speed_clamped"] is True and r["bar_seconds"] == 900
    wait_for(client, ended)


def test_ftx_replay_end_to_end(client, cap):
    r = client.post("/replay", params={"dataset": "btc_ftx_2022", "speed": "max", "pause_on": "tornado"}).json()
    assert r["started"] and r["symbol"] == "BTCUSDT" and r["meta"]["report"]["rows_out"] == 10080
    s = wait_for(client, lambda s: s["state"] == "paused")              # frozen at the first tornado
    assert s["level"] == "tornado" and s["score"] >= 75 and s["kind"] in ("squall", "storm")
    assert time.strftime("%Y-%m-%d", time.gmtime(s["ts"])) == "2022-11-08"
    assert client.get("/health").json()["status"] == "ok"               # paused is not stale

    ex = client.post("/exposure", json={"positions": [
        {"asset": "BTC", "size": 2, "entry_price": 21000}, {"asset": "BTC-USD", "size": 5000, "currency": "USD"},
        {"asset": "DOGE", "size": 10}], "horizon_bars": 60}).json()
    assert [p["status"] for p in ex["positions"]] == ["ok", "ok", "unknown_asset"]
    tot = ex["totals"]["USD"]
    assert tot["pnl"] < 0 and 0 < tot["var_baseline"] < tot["var"] <= tot["es"] <= tot["stressed_loss"]
    assert ex["positions"][0]["vol_ratio"] > 2 and ex["known_assets"] == ["BTCUSDT"]

    sim = client.post("/simulate", json={"symbol": "BTC", "size": 2, "steps": 60, "n_paths": 1000, "seed": 1}).json()
    assert sim["status"] == "ok" and sim["level"] == "tornado" and len(sim["fan"]["pnl_p5"]) == 60
    assert sim["position_value"] == pytest.approx(2 * s["price"])
    assert client.get("/simulate", params={"steps": 10, "method": "bootstrap"}).json()["method"] == "bootstrap"

    assert client.post("/resume").json() == {"resumed": True}
    s = wait_for(client, ended)
    assert s["level"] in ("green", "watch") and s["score"] is not None and s["session"]["progress"] == 1.0
    assert s["playbook"]["stay_safe"] and s["feed"]["rejected"] == {}
    h = client.get("/history", params={"n": 300}).json()
    assert 250 <= h["points"] <= 300 and len(h["price"]) == h["points"] and h["stride"] > 1
    assert any(t["to"] == "tornado" for t in h["transitions"])
    al = client.get("/alerts").json()
    assert al["alerts"] and any("TORNADO" in a["text"] for a in al["alerts"])
    assert any("TORNADO" in t for t in cap.sent)                        # notifier was actually called


def test_stream_emits_snapshots_and_end_event(client):
    client.post("/replay", params={"dataset": "gbpusd_minibudget_2022", "speed": "max"})
    events, saw_end = [], False
    with client.stream("GET", "/stream", params={"interval": 0.05}) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line.startswith("event: end"):
                saw_end = True
            elif line.startswith("data:") and not saw_end:
                events.append(json.loads(line[5:]))                     # strict JSON: would fail on NaN
    assert saw_end and events and events[-1]["state"] == "ended"
    assert {"symbol", "level", "score", "price", "components", "new_transitions", "playbook"} <= set(events[-1])
    seen = [t["to"] for e in events for t in e["new_transitions"]]
    assert "tornado" in seen
    with client.stream("GET", "/stream", params={"max_events": 1}) as r:
        assert len([ln for ln in r.iter_lines() if ln.startswith("data:")]) >= 1


def test_sgx_storm_replay_has_spread_curve_and_carry(client):
    r = client.post("/replay", params={"dataset": "sgx_usdjpy_storm", "speed": "max", "pause_on": "tornado"}).json()
    assert r["meta"]["report"]["has_quotes"] and len(r["meta"]["report"]["curve_tenors"]) == 9
    s = wait_for(client, lambda s: s["state"] == "paused")
    assert time.strftime("%Y-%m-%d", time.gmtime(s["ts"])) in ("2026-07-29", "2026-07-30")
    assert {"vol", "jump", "drawdown", "spread", "curve"} <= set(s["components"])
    assert s["carry"]["earning_side"] == "long" and -0.05 < s["carry"]["curve"]["3M"] < -0.01
    ex = client.post("/exposure", json={"positions": [{"asset": "USDJPY", "size": 1_000_000}]}).json()
    assert ex["totals"]["JPY"]["var"] > ex["totals"]["JPY"]["var_baseline"] > 0
    client.post("/resume")
    wait_for(client, ended)


class FakeFeed:
    """A live feed that sends a few ticks and then goes silent (socket up, no data)."""

    kind, reconnects, last_error = "live", 0, None

    def __init__(self, symbol, **kw):
        self.symbol = symbol

    async def __aiter__(self):
        for i in range(30):
            yield Tick(time.time() + i * 1e-3, 100.0 + 0.01 * i, 99.99, 100.01, self.symbol)
        await asyncio.sleep(3600)


def test_live_feed_going_silent_becomes_stale_and_alerts(client, cap, monkeypatch):
    monkeypatch.setattr(session_mod, "BinanceSource", FakeFeed)
    r = client.post("/live", params={"symbol": "ethusdt", "stale_after_s": 1, "bar_seconds": 0.1, "warmup_bars": 5})
    assert r.status_code == 200 and r.json()["symbol"] == "ETHUSDT"
    s = wait_for(client, lambda s: s["state"] == "stale", timeout=10)
    assert s["level"] == "stale" and s["score"] is None                 # not a false green
    assert client.get("/health").json()["status"] == "degraded"
    t0 = time.time()
    while not any("STALE" in t for t in cap.sent) and time.time() - t0 < 5:
        time.sleep(0.1)
    assert any("STALE" in t and "ETHUSDT" in t for t in cap.sent)
    ex = client.post("/exposure", json={"positions": [{"asset": "ETH", "size": 3}]}).json()
    assert ex["positions"][0]["status"] in ("ok", "warming_up") and ex["positions"][0]["value"] > 0
    assert client.post("/stop", params={"symbol": "ETHUSDT"}).json() == {"stopped": True}
    assert client.get("/score").json()["state"] == "ended"
    assert client.post("/live", params={"symbol": "!!!"}).status_code == 400
    assert client.post("/live", params={"bar_seconds": -1}).status_code == 422


def test_feed_crash_is_reported_as_error_not_green(client, monkeypatch):
    class Exploding(FakeFeed):
        async def __aiter__(self):
            yield Tick(time.time(), 100.0)
            raise ConnectionError("gave up")

    monkeypatch.setattr(session_mod, "BinanceSource", Exploding)
    client.post("/live", params={"symbol": "BTCUSDT"})
    s = wait_for(client, lambda s: s["state"] == "error", timeout=5)
    assert s["level"] == "error" and s["score"] is None and "gave up" in s["session"]["error"]
    assert client.get("/health").json()["status"] == "degraded"


def test_short_file_stays_warming_up_and_custom_path(client, monkeypatch, tmp_path):
    monkeypatch.setattr(session_mod, "EXTRA_DATA", tmp_path)
    pd.DataFrame({"Date": pd.date_range("2024-01-01", periods=20, freq="D"), "Close": range(100, 120)}
                 ).to_csv(tmp_path / "tiny.csv", index=False)
    r = client.post("/replay", params={"path": "tiny.csv", "speed": "max"}).json()
    assert "fewer than" in r["meta"]["warning"] and r["bar_seconds"] == 86400 and r["symbol"] == "TINY"
    s = wait_for(client, ended)
    assert s["level"] == "ended" and s["score"] is None and s["last_level"] == "warming_up"
    ex = client.post("/exposure", json={"positions": [{"asset": "TINY", "size": 1}]}).json()
    assert ex["positions"][0]["status"] == "warming_up" and ex["positions"][0]["value"] == 119
    assert client.post("/simulate", json={"symbol": "TINY"}).status_code == 409
    (tmp_path / "empty.csv").write_text("")
    assert client.post("/replay", params={"path": "empty.csv"}).status_code == 400


@pytest.mark.parametrize("body,code", [
    ({"positions": "all of it"}, 422), ({"positions": [{}] * 1001}, 422), ({}, 200), ({"positions": []}, 200),
    ({"positions": [{"asset": "BTC", "size": 0}]}, 200), ({"positions": [None, 5, "x"]}, 200),
])
def test_exposure_input_validation(client, body, code):
    r = client.post("/exposure", json=body)
    assert r.status_code == code
    if code == 200:
        assert r.json()["totals"] == {} and r.json()["notes"]


def test_simulate_bad_inputs(client):
    client.post("/replay", params={"dataset": "gbpusd_minibudget_2022", "speed": "max"})
    wait_for(client, ended)
    assert client.post("/simulate", json={"size": "big"}).status_code == 422
    assert client.post("/simulate", json={"symbol": "NOPE"}).status_code == 404
    r = client.post("/simulate", json={"steps": 10**9, "n_paths": -4, "method": "voodoo", "value": -1e6}).json()
    assert r["status"] == "ok" and len(r["clamped"]) >= 3 and r["symbol"] == "GBPUSD"


def test_yfinance_route(client, monkeypatch):
    idx = pd.date_range("2024-01-01", periods=120, freq="5min", tz="UTC", name="Datetime")
    df = pd.DataFrame({"Close": [100 + 0.01 * i for i in range(120)]}, index=idx)
    monkeypatch.setattr(session_mod, "load_yfinance", lambda sym, period, interval: __import__(
        "stormchaser.adapters.yf", fromlist=["x"]).load_yfinance(sym, period, interval, download=lambda *a: df))
    r = client.post("/yfinance", params={"symbol": "Gold"}).json()
    assert r["symbol"] == "GC=F" and r["kind"] == "yfinance" and r["bar_seconds"] == 300
    assert wait_for(client, ended)["level"] == "green"

    def fail(sym, period, interval):
        raise session_mod.DataError(f"yfinance failed for {sym}: rate limited")

    monkeypatch.setattr(session_mod, "load_yfinance", fail)
    assert client.post("/yfinance", params={"symbol": "AAPL"}).status_code == 502
    assert client.get("/health").json()["status"] == "ok"               # a failed start leaves the API healthy


def test_two_symbols_run_side_by_side_and_restart_replaces(client):
    client.post("/replay", params={"dataset": "gbpusd_minibudget_2022", "speed": "max"})
    client.post("/replay", params={"dataset": "btc_ftx_2022", "speed": "max"})
    wait_for(client, ended, "GBPUSD")
    wait_for(client, ended, "BTC")
    assert set(client.get("/health").json()["sessions"]) == {"GBPUSD", "BTCUSDT"}
    ex = client.post("/exposure", json={"positions": [{"asset": "GBPUSD", "size": 1e6}, {"asset": "BTC", "size": 1}]})
    assert ex.json()["totals"]["USD"]["gross"] > 1e6
    client.post("/replay", params={"dataset": "btc_ftx_2022", "speed": "max"})   # restart same symbol
    assert len(client.get("/health").json()["sessions"]) == 2
    wait_for(client, ended, "BTCUSDT")


# ---------------------------------------------------------------- dashboard support: pause, speed, hindsight, UI

def test_user_pause_resume_and_speed_change(client):
    assert client.post("/pause").status_code == 409                      # nothing running yet
    client.post("/replay", params={"dataset": "btc_ftx_2022", "speed": "6000"})
    assert client.post("/pause").json() == {"pausing": True}
    s = wait_for(client, lambda s: s["state"] == "paused")
    assert s["session"]["pause_reason"] == "user" and s["level"] != "stale"
    frozen = s["feed"]["accepted"]
    time.sleep(0.3)
    assert client.get("/score").json()["feed"]["accepted"] == frozen     # really frozen
    ahead = s["session"]["ahead"]                                        # hindsight: the low is still to come
    assert ahead["low"] <= ahead["from_price"] <= ahead["high"] and ahead["pct_to_low"] <= 0 <= ahead["pct_to_high"]
    assert client.post("/pause").json() == {"pausing": False}            # already paused
    assert client.post("/speed", params={"speed": "-1"}).status_code == 422
    assert client.post("/speed", params={"speed": "max"}).json() == {"speed": 1_000_000.0, "speed_clamped": False}
    assert client.post("/resume").json() == {"resumed": True}
    s = wait_for(client, ended)
    assert s["feed"]["accepted"] == 10080 and s["session"]["ahead"] is None and s["session"]["pause_reason"] is None
    assert client.post("/pause").json() == {"pausing": False}            # a finished replay cannot be paused


def test_slow_replay_speed_does_not_turn_stale(client):
    client.post("/replay", params={"dataset": "btc_ftx_2022", "speed": "max", "pause_on": "tornado"})
    wait_for(client, lambda s: s["state"] == "paused")
    assert client.post("/speed", params={"speed": "0.1"}).json()["speed"] == 0.1
    s = client.get("/score").json()
    assert s["session"]["speed"] == 0.1 and s["feed"]["stale_after_s"] == pytest.approx(5 * 60 / 0.1 + 3)


def test_tornado_freeze_carries_hindsight_for_the_callout(client):
    client.post("/replay", params={"dataset": "sgx_usdjpy_storm", "speed": "max", "pause_on": "tornado"})
    s = wait_for(client, lambda s: s["state"] == "paused")
    sess = s["session"]
    assert sess["pause_reason"] == "tornado" and sess["pause_on"] is None and "1M" in sess["curve_tenors"]
    a = sess["ahead"]
    assert time.strftime("%Y-%m-%d %H:%M", time.gmtime(a["from_ts"])) == "2026-07-29 18:01"
    assert a["low"] == pytest.approx(155.264, abs=0.01)
    assert time.strftime("%Y-%m-%d", time.gmtime(a["low_ts"])) == "2026-08-03"
    assert a["pct_to_low"] == pytest.approx(155.264 / 163.4945 - 1, abs=1e-4)      # -5.03%: "5% still to fall"


def test_live_session_has_no_speed_or_pause(client, monkeypatch):
    class Quiet:
        kind = "live"

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            await asyncio.sleep(3600)
            yield None

    monkeypatch.setattr(session_mod, "BinanceSource", lambda sym: Quiet())
    client.post("/live", params={"symbol": "BTCUSDT"})
    assert client.post("/pause").json() == {"pausing": False}
    assert client.post("/speed", params={"speed": "10"}).status_code == 400
    assert client.get("/score").json()["session"]["ahead"] is None


def test_dashboard_is_served_when_built(tmp_path, monkeypatch, cap):
    import stormchaser.api as api_mod
    (tmp_path / "index.html").write_text("<html><title>Storm Chaser</title></html>")
    monkeypatch.setattr(api_mod, "WEB_DIST", tmp_path)
    with TestClient(create_app(AlertManager([cap]))) as c:
        assert "Storm Chaser" in c.get("/ui/").text
        r = c.get("/", headers={"accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == "/ui/"
        assert c.get("/").json()["ui"] == "/ui/" and c.get("/health").json()["ui"] is True
    monkeypatch.setattr(api_mod, "WEB_DIST", tmp_path / "missing")       # not built: API still works, no /ui
    with TestClient(create_app(AlertManager([cap]))) as c:
        assert c.get("/ui/").status_code == 404 and c.get("/").json()["ui"] is None
        assert c.get("/", headers={"accept": "text/html"}).json()["tagline"]


def test_playbook_ideas_follow_the_level(client):
    from stormchaser.session import playbook
    vol = {"sigma_bar_fast": 0.000391487523470348, "sigma_bar_base": 4.883377030040973e-05,
           "annualised_fast": 0.2839188872267719, "annualised_base": 0.035415763955579195}
    carry = {"median": -0.0298, "earning_side": "long", "unwind": True}
    pb = playbook({"level": "tornado", "symbol": "USDJPY", "vol": vol, "carry": carry, "features": {}})
    assert [i["kind"] for i in pb["ideas"]] == ["hedge", "long_vol", "carry"]
    # hand check: 28.4% / sqrt(365.25) = 1.49% a day; 0.000391 / 0.0000488 = 8.0x
    assert "28.4% annualised, 8.0x the calm 3.5% (about +/-1.49% a day)" in pb["ideas"][0]["detail"]
    assert "earns about 3.0% a year" in pb["ideas"][2]["detail"] and pb["disclaimer"]
    calm = playbook({"level": "green", "symbol": "USDJPY", "features": {},
                     "vol": dict(vol, sigma_bar_fast=vol["sigma_bar_base"], annualised_fast=vol["annualised_base"])})
    assert [i["kind"] for i in calm["ideas"]] == ["long_vol"]
    assert [i["kind"] for i in playbook({"level": "watch", "symbol": "X", "vol": vol, "features": {}})["ideas"]] == \
        ["long_vol", "hedge"]
    for lvl in ("stale", "warming_up", "error"):                          # no reading, no trade ideas
        assert playbook({"level": lvl, "vol": vol})["ideas"] == []
    assert playbook({"level": "tornado", "vol": {}})["ideas"] == []
    client.post("/replay", params={"dataset": "sgx_usdjpy_storm", "speed": "max", "pause_on": "tornado"})
    s = wait_for(client, lambda s: s["state"] == "paused")
    assert s["session"]["bar_seconds"] == 60 and len(s["playbook"]["ideas"]) == 3
    assert client.post("/simulate", json={"symbol": "USDJPY", "size": 1}).json()["currency"] == "JPY"
