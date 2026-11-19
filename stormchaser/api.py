"""FastAPI backend. Run: uvicorn stormchaser.api:app"""
from __future__ import annotations

import asyncio
import json
import math
import time
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .adapters.replay import MAX_SPEED, MIN_SPEED, DataError
from .adapters.yf import PRESETS
from .alerts import AlertManager
from .exposure import compute_exposure
from .models import USD_LIKE, split_symbol
from .score import StormConfig
from .session import DATASETS, SessionManager, clean
from .simulate import simulate

TAGLINE = "Cloudy with a chance of margin calls"
WEB_DIST = Path(os.environ.get("STORMCHASER_WEB_DIR", Path(__file__).resolve().parents[1] / "web" / "dist"))
STARTED = time.time()


def scoring_summary(cfg: StormConfig | None = None) -> dict:
    """How the score is built, straight from StormConfig, for the dashboard's explainer."""
    c = cfg or StormConfig()
    return {
        "sensors": [
            {"key": "vol", "weight": c.w_vol, "core": True},
            {"key": "jump", "weight": c.w_jump, "core": True},
            {"key": "drawdown", "weight": c.w_dd, "core": True},
            {"key": "spread", "weight": c.w_spread, "core": False},
            {"key": "curve", "weight": c.w_curve, "core": False},
        ],
        "mean_share": 0.5, "worst_share": 0.5,       # score = 100 * (0.5 * weighted mean + 0.5 * worst sensor)
        "aux_cap": c.aux_cap,
        "thresholds": {"watch": c.thr_watch, "warning": c.thr_warning, "tornado": c.thr_tornado},
        "hysteresis": c.hysteresis, "cooldown_bars": c.cooldown_bars,
        "squall_min": c.squall_s / 60.0, "heat_halflife_min": c.heat_halflife_s / 60.0,
    }


def create_app(alerts: AlertManager | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        loop = asyncio.get_running_loop()
        if mgr.alerts._dispatch is None:              # never block the scoring loop on a slow notifier
            mgr.alerts._dispatch = lambda fn, text: loop.run_in_executor(None, fn, text)
        yield
        await mgr.shutdown()

    mgr = SessionManager(alerts)
    app = FastAPI(title="Storm Chaser", description=TAGLINE, version="0.1.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.state.manager = mgr

    def ok(payload: Any, status: int = 200) -> JSONResponse:
        return JSONResponse(clean(payload), status_code=status)

    def need(symbol: str | None):
        s = mgr.get(symbol)
        if s is None:
            if symbol:
                raise HTTPException(404, f"no session for {symbol!r}; active: {sorted(mgr.sessions)}")
            raise HTTPException(409, "no session running yet; start one with /replay, /live or /yfinance")
        return s

    def parse_speed(speed: str | None) -> float:
        if speed is None or str(speed).lower() in ("max", "fast", ""):
            return MAX_SPEED
        try:
            v = float(speed)
        except ValueError:
            raise HTTPException(422, f"speed must be a number between {MIN_SPEED} and {MAX_SPEED:g}, or 'max'")
        if not math.isfinite(v) or v <= 0:
            raise HTTPException(422, f"speed must be a positive finite number ({MIN_SPEED} to {MAX_SPEED:g}), or 'max'")
        return v

    @app.exception_handler(DataError)
    async def data_error(_, exc: DataError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    has_ui = (WEB_DIST / "index.html").is_file()

    @app.get("/")
    async def root(request: Request):
        if has_ui and "text/html" in request.headers.get("accept", ""):
            return RedirectResponse("/ui/")           # a browser gets the dashboard, a script gets JSON
        return ok({"name": "Storm Chaser", "tagline": TAGLINE, "docs": "/docs", "ui": "/ui/" if has_ui else None,
                   "endpoints": ["/health", "/datasets", "/replay", "/pause", "/resume", "/speed", "/live",
                                 "/yfinance", "/score", "/stream", "/history", "/exposure", "/simulate", "/alerts",
                                 "/stop"]})

    @app.get("/health")
    async def health():
        sessions = {k: {"kind": s.kind, "state": s.state, "feed": s.snapshot()["state"], "error": s.error,
                        "progress": s.progress()} for k, s in mgr.sessions.items()}
        degraded = any(v["feed"] in ("stale", "error") for v in sessions.values())
        return ok({"status": "degraded" if degraded else "ok", "uptime_s": time.time() - STARTED,
                   "sessions": sessions, "latest": mgr.latest,
                   "notifiers": [getattr(n, "name", "?") for n in mgr.alerts.notifiers], "ui": has_ui})

    @app.get("/datasets")
    async def datasets():
        return ok({"datasets": {k: {x: v[x] for x in v if x != "file"} for k, v in DATASETS.items()},
                   "yfinance_presets": PRESETS, "live": ["BTCUSDT", "ETHUSDT"],
                   "speed": {"min": MIN_SPEED, "max": MAX_SPEED}, "scoring": scoring_summary()})

    def started(s):
        return ok({"started": True, "symbol": s.symbol, "kind": s.kind, "meta": s.meta,
                   "speed": getattr(s.source, "speed", None), "bar_seconds": s.engine.cfg.bar_seconds,
                   "warmup_bars": s.engine.cfg.warmup_bars})

    @app.api_route("/replay", methods=["GET", "POST"])
    async def replay(dataset: str | None = None, speed: str | None = "3600", path: str | None = None,
                     symbol: str | None = None, bar_seconds: float | None = Query(None, gt=0, le=86400 * 7),
                     select: str | None = None, warmup_bars: int | None = Query(None, ge=5, le=100000),
                     pause_on: str | None = None):
        s = await mgr.start_replay(dataset, parse_speed(speed), path, symbol, bar_seconds, select, warmup_bars,
                                   pause_on)
        return started(s)

    @app.api_route("/resume", methods=["GET", "POST"])
    async def resume(symbol: str | None = None):
        return ok({"resumed": need(symbol).resume()})

    @app.api_route("/pause", methods=["GET", "POST"])
    async def pause(symbol: str | None = None):
        return ok({"pausing": need(symbol).pause()})

    @app.api_route("/speed", methods=["GET", "POST"])
    async def speed_set(speed: str, symbol: str | None = None):
        return ok(need(symbol).set_speed(parse_speed(speed)))

    @app.api_route("/yfinance", methods=["GET", "POST"])
    async def yfinance(symbol: str, period: str = "5d", interval: str = "5m", speed: str | None = "max",
                       warmup_bars: int | None = Query(None, ge=5, le=100000)):
        try:
            s = await mgr.start_yfinance(PRESETS.get(symbol, symbol), period, interval, parse_speed(speed), warmup_bars)
        except DataError as e:
            raise HTTPException(502 if "yfinance" in str(e) else 400, str(e))
        return started(s)

    @app.api_route("/live", methods=["GET", "POST"])
    async def live(symbol: str = "BTCUSDT", bar_seconds: float = Query(2.0, ge=0.1, le=3600),
                   stale_after_s: float = Query(10.0, ge=1, le=3600), warmup_bars: int = Query(90, ge=5, le=100000)):
        return started(await mgr.start_live(symbol, bar_seconds, stale_after_s, warmup_bars))

    @app.api_route("/stop", methods=["GET", "POST"])
    async def stop(symbol: str | None = None):
        return ok({"stopped": await mgr.stop(symbol)})

    @app.get("/score")
    async def score(symbol: str | None = None):
        return ok(need(symbol).snapshot())

    @app.get("/history")
    async def history(symbol: str | None = None, n: int = Query(500, ge=1, le=20000)):
        return ok(need(symbol).series(n))

    @app.get("/alerts")
    async def alerts_log():
        return ok({"alerts": list(mgr.alerts.history), "suppressed_by_debounce": mgr.alerts.suppressed,
                   "debounce_s": mgr.alerts.debounce_s})

    @app.get("/stream")
    async def stream(symbol: str | None = None, interval: float = Query(0.25, ge=0.05, le=10),
                     max_events: int | None = Query(None, ge=1)):
        """Server-sent events: one JSON snapshot per `interval` while data flows, a heartbeat snapshot at
        least every 2s otherwise (so a stale feed is visible), and a final `event: end`."""
        sess = need(symbol)

        async def gen():
            sent, last_v, last_emit, n_tr = 0, -1, 0.0, 0
            while True:
                s = mgr.get(sess.symbol) or sess
                now = time.monotonic()
                if s.version != last_v or now - last_emit >= 2.0:
                    last_v, last_emit = s.version, now
                    snap = s.snapshot()
                    tr = s.engine.transitions
                    snap["new_transitions"] = [t.__dict__ for t in tr[n_tr:]][-50:]
                    n_tr = len(tr)
                    yield f"data: {json.dumps(clean(snap), allow_nan=False)}\n\n"
                    sent += 1
                    if s.state != "running":
                        yield f"event: end\ndata: {json.dumps({'state': s.state, 'error': s.error})}\n\n"
                        return
                    if max_events and sent >= max_events:
                        return
                await asyncio.sleep(interval)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/exposure")
    async def exposure(body: dict = Body(default={})):
        positions = body.get("positions", [])
        if not isinstance(positions, list):
            raise HTTPException(422, "positions must be a list of {asset, size, currency?, entry_price?}")
        if len(positions) > 1000:
            raise HTTPException(422, "at most 1000 positions")
        res = compute_exposure(positions, mgr.regime, body.get("horizon_bars", 60), body.get("confidence", 0.99))
        res["known_assets"] = sorted(mgr.sessions)
        return ok(res)

    async def run_sim(p: dict):
        sess = need(p.get("symbol") or p.get("asset"))
        reg = sess.engine.regime()
        value = p.get("value")
        if value is None and p.get("size") is not None and reg.get("price"):
            try:
                value = float(p["size"]) * reg["price"]
            except (TypeError, ValueError):
                raise HTTPException(422, "size must be a number")
        kw = {k: p[k] for k in ("steps", "n_paths", "method", "seed", "alpha", "beta", "vol_mult") if p.get(k) is not None}
        try:
            res = await asyncio.to_thread(simulate, reg, value, **kw)
        except TypeError as e:
            raise HTTPException(422, f"bad simulation parameter: {e}")
        quote = split_symbol(sess.symbol)[1]
        res.update(symbol=sess.symbol, level=sess.snapshot()["level"], currency="USD" if quote in USD_LIKE else quote)
        return ok(res, 200 if res.get("status") == "ok" else 409)

    @app.post("/simulate")
    async def simulate_post(body: dict = Body(default={})):
        return await run_sim(body)

    @app.get("/simulate")
    async def simulate_get(symbol: str | None = None, steps: int = 60, n_paths: int = 2000, method: str = "garch",
                           size: float | None = None, value: float | None = None, seed: int | None = None):
        return await run_sim({"symbol": symbol, "steps": steps, "n_paths": n_paths, "method": method, "size": size,
                              "value": value, "seed": seed})

    if has_ui:
        app.mount("/ui", StaticFiles(directory=WEB_DIST, html=True), name="ui")
    return app


app = create_app()
