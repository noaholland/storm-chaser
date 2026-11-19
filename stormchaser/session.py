"""Session manager: one running feed (replay / live / yfinance) per symbol, each with its own engine."""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from collections import deque
from pathlib import Path

from .adapters.binance import BinanceSource
from .adapters.replay import MAX_SPEED, DataError, ReplaySource, clamp_speed, load_ticks
from .adapters.yf import load_yfinance
from .alerts import AlertManager
from .models import norm_symbol, same_asset
from .score import StormConfig, StormEngine, Transition

log = logging.getLogger("stormchaser.session")
PKG_DATA = Path(__file__).parent / "data"
EXTRA_DATA = Path(os.environ.get("STORMCHASER_DATA_DIR", Path(__file__).resolve().parents[1] / "data"))

DATASETS: dict[str, dict] = {
    "btc_ftx_2022": {
        "file": "btc_ftx_2022.csv", "symbol": "BTCUSDT", "title": "Bitcoin, FTX collapse (6-12 Nov 2022)",
        "source": "Binance public klines, 1m", "description": "BTC falls from 21.3k to 15.6k as FTX fails.",
    },
    "gbpusd_minibudget_2022": {
        "file": "gbpusd_minibudget_2022.csv", "symbol": "GBPUSD", "title": "GBPUSD, UK mini-budget (19-30 Sep 2022)",
        "source": "HF Yllvar/fx-historical-data, 15m", "description": "Sterling flash-crashes to 1.035 on 26 Sep.",
    },
    "sgx_usdjpy_storm": {
        "file": "sgx_usdjpy_2026.parquet", "symbol": "USDJPY", "start": "2026-07-27", "end": "2026-08-05",
        "title": "SGX USDJPY storm (27 Jul - 4 Aug 2026)", "source": "SGX FX challenge dataset, 1m bid/ask + forward curve",
        "description": "360bp range on 30 Jul, follow-through to 155.26 on 3 Aug. Spread and carry-curve features live.",
    },
    "sgx_usdjpy_2026": {
        "file": "sgx_usdjpy_2026.parquet", "symbol": "USDJPY", "title": "SGX USDJPY (Jul - Aug 2026)",
        "source": "SGX FX challenge dataset, 1m bid/ask + forward curve", "description": "Full two months.",
    },
    "sgx_eurusd_2026": {
        "file": "sgx_eurusd_2026.parquet", "symbol": "EURUSD", "title": "SGX EURUSD (Jul - Aug 2026)",
        "source": "SGX FX challenge dataset, 1m bid/ask + forward curve", "description": "Full two months.",
    },
    "sgx_usdsgd_2026": {
        "file": "sgx_usdsgd_2026.parquet", "symbol": "USDSGD", "title": "SGX USDSGD (Jul - Aug 2026)",
        "source": "SGX FX challenge dataset, 1m bid/ask + forward curve", "description": "Full two months.",
    },
}
DATASETS = {k: DATASETS[k] for k in ["sgx_usdjpy_storm", "btc_ftx_2022", "gbpusd_minibudget_2022", "sgx_usdjpy_2026",
                                     "sgx_eurusd_2026", "sgx_usdsgd_2026"]}   # demo order: the SGX storm first


def dataset_path(name: str) -> Path:
    meta = DATASETS.get(name)
    if meta is None:
        raise DataError(f"unknown dataset {name!r}; available: {sorted(DATASETS)}")
    f = Path(meta["file"])
    return f if f.is_absolute() else PKG_DATA / f


def clean(o):
    """Make any payload strict-JSON safe (no NaN/inf, no numpy types)."""
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, deque)):
        return [clean(v) for v in o]
    if isinstance(o, bool) or o is None or isinstance(o, str):
        return o
    if isinstance(o, int):
        return o
    if hasattr(o, "tolist"):
        return clean(o.tolist())
    try:
        f = float(o)
    except (TypeError, ValueError):
        return str(o)
    return f if math.isfinite(f) else None


class Session:
    def __init__(self, symbol: str, kind: str, source, engine: StormEngine, meta: dict | None = None):
        self.symbol, self.kind, self.source, self.engine = symbol, kind, source, engine
        self.meta = meta or {}
        self.state = "running"                 # running | ended | error | stopped
        self.error: str | None = None
        self.started = time.time()
        self.history: deque = deque(maxlen=50_000)
        self.version = 0
        self.task: asyncio.Task | None = None
        self.stale_alerted = False
        self._last_n = 0
        self.pause_on: str | None = None       # replay only: freeze once when this level is reached
        self.paused = False
        self.pause_reason: str | None = None   # the level that froze the replay, or "user"
        self.ahead: dict | None = None         # replay hindsight, computed at the moment of a pause
        self._pause_req = False
        self._resume = asyncio.Event()

    def resume(self) -> bool:
        was = self.paused or self._pause_req
        self._pause_req = False
        self._resume.set()
        return was

    def pause(self) -> bool:
        """Ask a running replay to hold at its next tick. False if there is nothing to pause."""
        if self.state != "running" or self.paused or not hasattr(self.source, "ticks"):
            return False
        self._pause_req = True
        return True

    def set_speed(self, speed: float) -> dict:
        """Change replay pace mid-flight; the stale threshold follows so a slow replay is not 'stale'."""
        if not hasattr(self.source, "ticks"):
            raise DataError("speed only applies to replay / yfinance sessions")
        self.source.speed, clamped = clamp_speed(speed)
        cfg = self.engine.cfg
        cfg.stale_after_s = max(10.0, 5.0 * cfg.bar_seconds / self.source.speed + 3.0)
        self.version += 1
        return {"speed": self.source.speed, "speed_clamped": clamped}

    def lookahead(self) -> dict | None:
        """Replay only: what the rest of the file does from here (the low/high still to come).
        Hindsight for the demo callout; a live feed has no such thing and returns None."""
        ticks = getattr(self.source, "ticks", None)
        pos, px = getattr(self.source, "position", 0), self.engine.price
        if not ticks or not px or pos >= len(ticks):
            return None
        rest = [t for t in ticks[pos:] if t.price and t.price > 0]
        if not rest:
            return None
        lo, hi = min(rest, key=lambda t: t.price), max(rest, key=lambda t: t.price)
        return {"from_ts": self.engine.price_ts, "from_price": px, "end_ts": rest[-1].ts,
                "low": lo.price, "low_ts": lo.ts, "pct_to_low": min(0.0, lo.price / px - 1.0),
                "high": hi.price, "high_ts": hi.ts, "pct_to_high": max(0.0, hi.price / px - 1.0)}

    async def run(self) -> None:
        eng = self.engine
        try:
            async for tick in self.source:
                n_tr = len(eng.transitions)
                eng.update(tick)
                self.version += 1
                self.stale_alerted = False
                if eng.n_ret != self._last_n:
                    self._last_n = eng.n_ret
                    self.history.append((eng.price_ts, eng.price, eng.score, eng.level))
                hit = bool(self.pause_on) and any(t.to == self.pause_on for t in eng.transitions[n_tr:])
                if hit or self._pause_req:
                    self.pause_reason = self.pause_on if hit else "user"
                    if hit:
                        self.pause_on = None
                    self._pause_req, self.paused = False, True
                    self.ahead = self.lookahead()
                    self._resume.clear()
                    self.version += 1
                    await self._resume.wait()   # the storm is frozen on screen until /resume
                    self.paused, self.pause_reason, self.ahead = False, None, None
                    self.version += 1
                    eng.last_rx = eng.clock()
            self.state = "ended"
        except asyncio.CancelledError:
            self.state = "stopped"
            raise
        except Exception as e:  # noqa: BLE001 - a broken feed must not take the API down
            self.state, self.error = "error", f"{type(e).__name__}: {e}"[:300]
            log.exception("session %s failed", self.symbol)
        finally:
            self.version += 1

    def snapshot(self) -> dict:
        s = self.engine.snapshot()
        if self.paused and self.state == "running":
            # deliberately frozen replay: the reading is real, the feed is not stale
            s["state"] = "paused"
            s["level"], s["score"] = s["last_level"], s["last_score"]
        elif self.state != "running":
            # a finished replay is not a stale feed: report the final reading, clearly flagged
            final = "ended" if self.state in ("ended", "stopped") else "error"
            s["state"] = final
            s["level"] = s["last_level"] if (final == "ended" and self.engine.ready) else final
            s["score"] = s["last_score"] if final == "ended" else None
        s.update(session={"kind": self.kind, "state": "paused" if self.paused else self.state, "error": self.error,
                          "started": self.started,
                          "dataset": self.meta.get("dataset"), "speed": getattr(self.source, "speed", None),
                          "progress": self.progress(), "reconnects": getattr(self.source, "reconnects", None),
                          "feed_error": getattr(self.source, "last_error", None),
                          "pause_on": self.pause_on, "pause_reason": self.pause_reason, "ahead": self.ahead,
                          "title": self.meta.get("title"), "bar_seconds": self.engine.cfg.bar_seconds,
                          "curve_tenors": (self.meta.get("report") or {}).get("curve_tenors") or []})
        s["playbook"] = playbook(s)
        return s

    def progress(self) -> float | None:
        n = len(getattr(self.source, "ticks", []) or [])
        return round(getattr(self.source, "position", 0) / n, 4) if n else None

    def series(self, n: int = 500) -> dict:
        h = list(self.history)
        step = max(1, math.ceil(len(h) / max(1, n)))
        h = h[::step] if step > 1 else h
        return {"symbol": self.symbol, "points": len(h), "stride": step, "ts": [x[0] for x in h],
                "price": [x[1] for x in h], "score": [x[2] for x in h], "level": [x[3] for x in h],
                "transitions": [t.__dict__ for t in self.engine.transitions[-5000:]]}


def playbook(s: dict) -> dict:
    """Plain-language guidance for the four pillars. Heuristic, not advice."""
    lvl, f = s.get("level"), s.get("features") or {}
    vol = s.get("vol") or {}
    ratio = (vol.get("sigma_bar_fast") or 0) / vol["sigma_bar_base"] if vol.get("sigma_bar_base") else None
    safe = {
        "green": "Calm. Keep normal limits.",
        "watch": "Clouds forming. Check stops and margin headroom.",
        "warning": "Storm warning. Cut leverage, widen margin buffers, avoid new illiquid risk.",
        "tornado": "Tornado. Reduce or hedge now; expect gaps, wide spreads and margin calls.",
        "stale": "Feed is stale: treat risk as unknown and do not trust the last score.",
        "warming_up": "Warming up: not enough history to judge yet.",
    }.get(lvl, "No reading.")
    opp = None
    if lvl in ("warning", "tornado") and ratio:
        opp = (f"Volatility is {ratio:.1f}x its baseline: optionality is expensive, so hedged sellers of vol and "
               "liquidity providers are paid most now; wait for the score to roll over before fading the move.")
    elif lvl in ("green", "watch") and (s.get("peak_score") or 0) >= 75 and ratio and ratio < 1.5:
        opp = "Storm has passed and vol is normalising: dislocations from the move are the opportunity set."
    return {"stay_safe": safe, "ideas": trade_ideas(s, ratio),
            "early_warning": f"vol z {f.get('vol_z', 0):+.1f}, largest recent jump {f.get('jump_peak_z', 0):.1f} sigma, "
                             f"drawdown {100 * f.get('drawdown', 0):.2f}%" if f else None,
            "whats_next": "POST /simulate for the P&L fan from this regime.",
            "opportunity": opp, "disclaimer": "Heuristic reading of the data, not investment advice."}


def trade_ideas(s: dict, ratio: float | None) -> list[dict]:
    """Illustrative trades for the 'spot opportunities' pillar: a hedge and a long-volatility view, sized by
    the regime. Each is a talking point with the numbers behind it, never an instruction."""
    lvl, vol, carry = s.get("level"), s.get("vol") or {}, s.get("carry") or {}
    af, ab = vol.get("annualised_fast"), vol.get("annualised_base")
    if lvl not in ("green", "watch", "warning", "tornado") or not af or not ab or not ratio:
        return []
    day = 100 * af / math.sqrt(365.25)                        # one-sigma daily move implied by the fast vol
    now = f"Realised vol is {100 * af:.1f}% annualised, {ratio:.1f}x the calm {100 * ab:.1f}% (about +/-{day:.2f}% a day)."
    sym = s.get("symbol") or "the asset"
    out = []
    if lvl in ("warning", "tornado"):
        out.append({"kind": "hedge", "title": "Hedge the delta first",
                    "detail": f"{now} An offsetting forward or futures position in {sym} cuts the loss one for one "
                              "and stays cheap to put on even after option premia have jumped."})
        out.append({"kind": "long_vol", "title": "Long volatility, with defined risk",
                    "detail": "Options now price a storm. Prefer a spread (a put spread or a strangle financed by "
                              "selling further wings) over an outright straddle: it pays if the move extends and "
                              "caps what you lose if vol collapses."})
    elif lvl == "watch":
        out.append({"kind": "long_vol", "title": "Long volatility before the storm",
                    "detail": f"{now} Clouds are forming but options have not fully repriced: a straddle or strangle "
                              "pays if the move extends in either direction."})
        out.append({"kind": "hedge", "title": "Part-hedge and tighten stops",
                    "detail": f"Trim or offset part of the {sym} position with a forward while spreads are still normal."})
    else:
        out.append({"kind": "long_vol", "title": "Buy protection while it is cheap",
                    "detail": f"{now} Out-of-the-money options cost least in calm markets, before a storm reprices them."})
    if carry.get("unwind") and carry.get("median") is not None:
        side = carry.get("earning_side") or "carry"
        out.append({"kind": "carry", "title": "Carry unwind under way",
                    "detail": f"The {side} side earns about {100 * abs(carry['median']):.1f}% a year in carry and is "
                              "being squeezed out while the forward curve dislocates. That is the crowded exit: "
                              "cut or hedge carry positions rather than add to them."})
    return out


class SessionManager:
    def __init__(self, alerts: AlertManager | None = None):
        self.sessions: dict[str, Session] = {}
        self.alerts = alerts or AlertManager()
        self.latest: str | None = None
        self._watchdog: asyncio.Task | None = None

    # ------------------------------------------------------------- lookups
    def get(self, symbol: str | None = None) -> Session | None:
        if not symbol:
            return self.sessions.get(self.latest) if self.latest else None
        key = norm_symbol(symbol)
        if key in self.sessions:
            return self.sessions[key]
        for k, s in self.sessions.items():
            if same_asset(k, key) or norm_symbol(s.meta.get("dataset") or "") == key:
                return s
        return None

    def regime(self, asset: str) -> dict | None:
        s = self.get(asset) if asset else None
        if s is None:
            return None
        r = s.engine.regime()
        r["symbol"] = s.symbol
        return r

    # ------------------------------------------------------------- starting
    async def _start(self, symbol: str, kind: str, source, cfg: StormConfig, meta: dict, pause_on=None) -> Session:
        key = norm_symbol(symbol)
        await self.stop(key)
        eng = StormEngine(symbol, cfg)
        eng.on_transition = lambda t, sym=symbol: self._on_transition(sym, t)
        sess = Session(symbol, kind, source, eng, meta)
        sess.pause_on = pause_on
        self.sessions[key] = sess
        self.latest = key
        sess.task = asyncio.create_task(sess.run(), name=f"session-{key}")
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(self._watch(), name="stale-watchdog")
        return sess

    def _on_transition(self, symbol: str, t: Transition) -> None:
        note = f"driver: {t.why}" + (", carry unwind" if t.carry_unwind else "")
        self.alerts.on_transition(symbol, t.frm, t.to, t.score, t.price, t.ts, note)

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            for s in list(self.sessions.values()):
                if s.state == "running" and not s.paused and not s.stale_alerted and s.engine.is_stale():
                    s.stale_alerted = True
                    self.alerts.on_stale(s.symbol, s.engine.snapshot()["feed"]["age_s"] or 0.0)

    async def start_replay(self, dataset: str | None = None, speed: float = 3600.0, path: str | None = None,
                           symbol: str | None = None, bar_seconds: float | None = None,
                           select: str | None = None, warmup_bars: int | None = None,
                           pause_on: str | None = None) -> Session:
        if pause_on and pause_on not in ("watch", "warning", "tornado"):
            raise DataError("pause_on must be one of watch, warning, tornado")
        if dataset:
            p, meta = dataset_path(dataset), dict(DATASETS[dataset], dataset=dataset)
            select = select or meta.get("select")
        elif path:
            p = Path(path)
            if not p.is_absolute():
                p = EXTRA_DATA / p
            p = p.resolve()
            if not any(p.is_relative_to(root.resolve()) for root in (EXTRA_DATA, PKG_DATA)):
                raise DataError("path must be inside the data directory")
            meta = {"dataset": p.name, "title": p.name}
        else:
            raise DataError(f"dataset is required; available: {sorted(DATASETS)}")
        symbol = symbol or meta.get("symbol") or select or p.stem.upper()
        ticks, report = await asyncio.to_thread(load_ticks, p, symbol, select=select, start=meta.get("start"),
                                                end=meta.get("end"))
        return await self._start_ticks(symbol, "replay", ticks, report, speed, bar_seconds, meta, warmup_bars,
                                       pause_on)

    async def start_yfinance(self, symbol: str, period: str = "5d", interval: str = "5m", speed: float = MAX_SPEED,
                             warmup_bars: int | None = None) -> Session:
        ticks, report = await asyncio.to_thread(load_yfinance, symbol, period, interval)
        meta = {"dataset": f"yfinance:{symbol}:{period}:{interval}", "title": f"{symbol} via yfinance"}
        return await self._start_ticks(symbol, "yfinance", ticks, report, speed, None, meta, warmup_bars)

    async def _start_ticks(self, symbol, kind, ticks, report, speed, bar_seconds, meta, warmup_bars,
                           pause_on=None) -> Session:
        bar = bar_seconds if bar_seconds and bar_seconds > 0 else report["median_step_s"]
        src = ReplaySource(ticks, speed=speed)
        cfg = StormConfig(bar_seconds=bar, stale_after_s=max(10.0, 5.0 * bar / src.speed + 3.0))
        if warmup_bars:
            cfg.warmup_bars = max(5, int(warmup_bars))
        if len(ticks) <= cfg.warmup_bars:
            meta["warning"] = (f"only {len(ticks)} rows: fewer than the {cfg.warmup_bars} needed to warm up, "
                               "the score will stay 'warming_up'")
        meta.update(report=report, bar_seconds=bar, speed_clamped=src.speed_clamped)
        return await self._start(symbol, kind, src, cfg, meta, pause_on)

    async def start_live(self, symbol: str = "BTCUSDT", bar_seconds: float = 1.0, stale_after_s: float = 10.0,
                         warmup_bars: int = 60) -> Session:
        sym = norm_symbol(symbol)
        if not sym:
            raise DataError("symbol is required")
        cfg = StormConfig(bar_seconds=bar_seconds, stale_after_s=stale_after_s, warmup_bars=warmup_bars)
        return await self._start(sym, "live", BinanceSource(sym), cfg, {"dataset": f"binance:{sym}"})

    async def stop(self, symbol: str | None = None) -> bool:
        s = self.get(symbol)
        if s is None:
            return False
        if s.task and not s.task.done():
            s.task.cancel()
            try:
                await s.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        return True

    async def shutdown(self) -> None:
        for k in list(self.sessions):
            await self.stop(k)
        if self._watchdog:
            self._watchdog.cancel()
