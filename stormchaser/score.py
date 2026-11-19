"""Storm score engine: rolling features -> 0-100 score -> green/watch/warning/tornado.

Design notes
- Ticks are validated, de-duplicated and fat-finger filtered before anything else.
- Features are sampled every `bar_seconds` of *data* time (first tick at/after the boundary).
  Returns across longer gaps (weekends, outages) are divided by sqrt(elapsed bars), so a
  gap is not mistaken for a jump unless it is large even for the time that passed.
- The baseline is an exponentially weighted mean/variance of log fast-vol. Because it keeps
  learning, a new permanent regime stops alerting once it has been stable for a few baseline
  half-lives ("regime re-baselining").
"""
from __future__ import annotations

import math
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from .models import Tick

LEVELS = ["green", "watch", "warning", "tornado"]
SECONDS_PER_YEAR = 365.25 * 86400


@dataclass
class StormConfig:
    bar_seconds: float = 60.0
    warmup_bars: int = 60          # samples needed before a score is published
    vol_halflife: float = 15.0     # fast realised-vol EWMA (bars)
    base_halflife: float = 720.0   # baseline / re-baselining speed (bars)
    base_std_floor: float = 0.25   # minimum std of log-vol used in the z-score
    vol_z_lo: float = 1.5
    vol_z_hi: float = 5.0
    jump_k: float = 6.0            # a return beyond k baseline sigmas is a jump
    jump_k_hi: float = 25.0
    jump_halflife: float = 10.0
    dd_window: int = 60            # bars for drawdown speed
    dd_lo: float = 2.5             # drawdown in units of sigma_base*sqrt(dd_window)
    dd_hi: float = 8.0
    spread_lo: float = 2.0         # spread / baseline spread
    spread_hi: float = 6.0
    spread_halflife: float = 5.0
    w_vol: float = 0.40
    w_jump: float = 0.25
    w_dd: float = 0.25
    w_spread: float = 0.10
    w_curve: float = 0.15          # forward-curve dislocation (only when tenors are supplied)
    curve_lo: float = 4.0          # RMS z of per-tenor carry changes
    curve_hi: float = 12.0
    reopen_mask_s: float = 3600.0  # ignore spread after a gap (weekend reopen is always wide)
    squall_s: float = 5400.0       # alert time (decayed) below this is a squall, above it a storm
    heat_halflife_s: float = 21600.0
    aux_cap: float = 0.5           # spread/curve alone can reach 'watch', never 'warning'
    thr_watch: float = 25.0
    thr_warning: float = 50.0
    thr_tornado: float = 75.0
    hysteresis: float = 8.0        # score must fall this far below a threshold ...
    cooldown_bars: int = 5         # ... for this many consecutive samples to downgrade
    outlier_sigma: float = 10.0    # tick beyond this many sigmas is held until confirmed
    outlier_min_ret: float = 0.003
    outlier_warmup_ret: float = 0.20
    max_gap_bars: float = 5.0
    stale_after_s: float = 10.0
    regime_window: int = 500

    def __post_init__(self):
        if not (self.bar_seconds > 0 and math.isfinite(self.bar_seconds)):
            raise ValueError("bar_seconds must be a positive finite number")
        self.warmup_bars = max(5, int(self.warmup_bars))
        self.dd_window, self.cooldown_bars = max(2, int(self.dd_window)), max(1, int(self.cooldown_bars))
        self.regime_window = max(50, int(self.regime_window))

    @property
    def min_sigma(self) -> float:
        return 2e-6 * math.sqrt(self.bar_seconds)


def _alpha(halflife: float) -> float:
    return 1.0 - 0.5 ** (1.0 / max(halflife, 1e-9))


def _clip01(x: float) -> float:
    return 0.0 if x <= 0 else 1.0 if x >= 1 else x


def _fin(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


@dataclass
class Transition:
    ts: float
    frm: str
    to: str
    score: float
    price: float
    why: str = ""                  # dominant component at the moment of the change
    kind: str = ""                 # calm / squall / storm after the change
    components: dict = field(default_factory=dict)
    carry_unwind: bool = False


class StormEngine:
    def __init__(self, symbol: str = "", config: StormConfig | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 on_transition: Callable[[Transition], None] | None = None):
        self.symbol = symbol
        self.cfg = config or StormConfig()
        self.clock = clock
        self.on_transition = on_transition
        c = self.cfg
        self._a_vol, self._a_base = _alpha(c.vol_halflife), _alpha(c.base_halflife)
        self._d_jump, self._a_spread = 0.5 ** (1 / c.jump_halflife), _alpha(c.spread_halflife)
        # feed state
        self.rejected: Counter = Counter()
        self.accepted = 0
        self.gaps = 0
        self.last_rx: float | None = None
        self.last_ts: float | None = None       # last valid tick seen (incl. pending)
        self.price: float | None = None         # last accepted price
        self.price_ts: float | None = None
        self.first_price: float | None = None
        self.bid: float | None = None
        self.ask: float | None = None
        self._pending: tuple | None = None
        # sampling state
        self._s_ts: float | None = None
        self._s_price: float | None = None
        self.n_ret = 0
        self.var_fast = 0.0
        self._base_n = 0
        self.base_mean = 0.0
        self.base_var = 0.0
        self.jump = 0.0
        self.last_jump_z = 0.0
        self._win: deque = deque(maxlen=max(2, c.dd_window))
        self.returns: deque = deque(maxlen=c.regime_window)
        self._sp_fast: float | None = None
        self._sp_base: float | None = None
        self._sp_n = 0
        self._mask_left = 0
        self._sp_hour: dict[int, list] = {}
        self._curve: dict[str, list] = {}      # tenor -> [last carry, ew var of change, n]
        self.curve_peak = 0.0
        self.carry: dict = {}
        self.elevated_bars = 0
        self.heat = 0.0                       # decayed seconds spent at warning or above
        self._dt = 0.0
        # output state
        self.components: dict[str, float] = {}
        self.raw: dict[str, float] = {}
        self.score: float | None = None
        self.level_idx = 0
        self._below = 0
        self.transitions: list[Transition] = []
        self.peak_score = 0.0

    # ------------------------------------------------------------------ ingest
    def update(self, tick: Tick) -> bool:
        """Feed one tick. Returns True if it was accepted (or held pending confirmation)."""
        ts, price, bid, ask = tick.ts, tick.price, tick.bid, tick.ask
        if not _fin(ts):
            self.rejected["bad_timestamp"] += 1
            return False
        if not (_fin(bid) and _fin(ask) and bid > 0 and ask > 0):
            if bid is not None or ask is not None:
                self.rejected["bad_quote"] += 1
            bid = ask = None
        elif bid > ask:
            self.rejected["crossed_quote"] += 1
            bid = ask = None
        if price is None and bid is not None:
            price = 0.5 * (bid + ask)
        if not _fin(price) or price <= 0:
            self.rejected["bad_price"] += 1
            return False
        if self.last_ts is not None:
            if ts < self.last_ts:
                self.rejected["out_of_order"] += 1
                return False
            if ts == self.last_ts:
                self.rejected["duplicate_ts"] += 1
                return False
        self.last_ts = ts
        self.last_rx = self.clock()
        cur = (ts, float(price), bid, ask, tick.extra)
        if self._pending is not None:
            pend, self._pending = self._pending, None
            if abs(math.log(price / pend[1])) <= abs(math.log(price / self.price)):
                self._ingest(*pend)          # next tick agrees with it: a real move
            else:
                self.rejected["outlier"] += 1  # reverted: fat finger, never scored
        if self.price is not None and self._suspect(ts, price):
            self._pending = cur
            return True
        self._ingest(*cur)
        return True

    def _sigma_base(self) -> float | None:
        if self._base_n < 5:
            return None
        return max(math.exp(self.base_mean), self.cfg.min_sigma)

    def _sigma_fast(self) -> float:
        return max(math.sqrt(self.var_fast), self.cfg.min_sigma)

    def _suspect(self, ts: float, price: float) -> bool:
        c = self.cfg
        r = abs(math.log(price / self.price))
        sb = self._sigma_base()
        if sb is None or self.n_ret < c.warmup_bars:
            return r > c.outlier_warmup_ret
        n = max(1.0, (ts - (self.price_ts or ts)) / c.bar_seconds)
        return r > max(c.outlier_sigma * max(sb, self._sigma_fast()) * math.sqrt(n), c.outlier_min_ret)

    def _ingest(self, ts: float, price: float, bid, ask, extra=None) -> None:
        self.accepted += 1
        self.price, self.price_ts, self.bid, self.ask = price, ts, bid, ask
        if self.first_price is None:
            self.first_price = price
        if self._s_ts is None:
            self._s_ts, self._s_price = ts, price
            self._win.append(price)
            return
        elapsed = (ts - self._s_ts) / self.cfg.bar_seconds
        if elapsed >= 1.0 - 1e-9:
            self._sample(ts, price, elapsed, bid, ask, extra)

    # ---------------------------------------------------------------- features
    def _sample(self, ts: float, price: float, n: float, bid, ask, extra=None) -> None:
        c = self.cfg
        if n > c.max_gap_bars:
            self.gaps += 1
            self._mask_left = max(3, math.ceil(c.reopen_mask_s / c.bar_seconds))
        rs = math.log(price / self._s_price) / math.sqrt(n)   # per-bar-equivalent return
        self._dt = n * c.bar_seconds
        self._s_ts, self._s_price = ts, price
        sb = self._sigma_base()

        # jump: measured against the baseline that existed *before* this return
        zj = abs(rs) / sb if sb else 0.0
        self.last_jump_z = zj
        self.jump = max(self.jump * self._d_jump, zj)

        # fast realised vol
        self.n_ret += 1
        a = max(self._a_vol, 1.0 / self.n_ret)
        self.var_fast += a * (rs * rs - self.var_fast)
        self.returns.append(rs)
        x = math.log(self._sigma_fast())

        # vol z-score vs baseline (before the baseline absorbs this observation)
        if sb:
            z = (x - self.base_mean) / max(math.sqrt(self.base_var), c.base_std_floor)
        else:
            z = 0.0
        if self.n_ret >= 5:
            self._base_n += 1
            ab = max(self._a_base, 1.0 / self._base_n)
            d = x - self.base_mean
            self.base_mean += ab * d
            self.base_var = (1 - ab) * (self.base_var + ab * d * d)

        # drawdown speed over the window, in units of baseline sigma * sqrt(window)
        self._win.append(price)
        hi, lo = max(self._win), min(self._win)
        dd, ru = 1.0 - price / hi, price / lo - 1.0
        ref = (sb or self._sigma_fast()) * math.sqrt(len(self._win))
        dd_z, ru_z = dd / ref, ru / ref

        comps = {
            "vol": _clip01((z - c.vol_z_lo) / (c.vol_z_hi - c.vol_z_lo)),
            "jump": _clip01((self.jump - c.jump_k) / (c.jump_k_hi - c.jump_k)),
            "drawdown": _clip01((max(dd_z, 0.5 * ru_z) - c.dd_lo) / (c.dd_hi - c.dd_lo)),
        }
        weights = {"vol": c.w_vol, "jump": c.w_jump, "drawdown": c.w_dd}
        self.raw = {"vol_z": z, "jump_z": zj, "jump_peak_z": self.jump, "drawdown": dd,
                    "runup": ru, "drawdown_z": dd_z}

        # spread widening (only when a valid two-sided quote exists)
        masked = self._mask_left > 0
        if masked:
            self._mask_left -= 1
            if self._sp_n >= 5:
                comps["spread"], weights["spread"] = 0.0, c.w_spread
            self.raw["spread_masked"] = 1.0       # reopen after a gap: wide quotes are expected, not a storm
        elif bid is not None and ask is not None:
            sp = max((ask - bid) / (0.5 * (ask + bid)), 0.0)
            self._sp_n += 1
            if self._sp_fast is None:
                self._sp_fast = self._sp_base = sp
            else:
                self._sp_fast += max(self._a_spread, 1.0 / self._sp_n) * (sp - self._sp_fast)
            # baseline = the wider of the rolling norm and the norm for this hour on *previous* days, so a
            # spread that widens every day at the same time (the 21:00 UTC roll) is learned, not alerted
            hid = int(ts // 3600)
            hb = self._sp_hour.setdefault(hid % 24, [None, 0.0, 0, hid])
            if hb[3] != hid:                               # a new day's visit to this hour: bank the old one
                if hb[2]:
                    m = hb[1] / hb[2]
                    hb[0] = m if hb[0] is None else hb[0] + 0.3 * (m - hb[0])
                hb[1], hb[2], hb[3] = 0.0, 0, hid
            hb[1] += sp
            hb[2] += 1
            ratio = self._sp_fast / max(self._sp_base, hb[0] or 0.0, 1e-7)
            self._sp_base += max(self._a_base, 1.0 / self._sp_n) * (sp - self._sp_base)
            if self._sp_n >= 5:
                comps["spread"] = _clip01((ratio - c.spread_lo) / (c.spread_hi - c.spread_lo))
                weights["spread"] = c.w_spread
            self.raw.update(spread=sp, spread_ratio=ratio)

        # forward curve: per-tenor implied carry; dislocation = RMS z-score of this bar's carry changes
        curve = (extra or {}).get("carry") if isinstance(extra, dict) else None
        if curve:
            zs, vals = [], []
            for tenor, v in curve.items():
                if not _fin(v):
                    continue
                vals.append(v)
                st = self._curve.get(tenor)
                if st is None:
                    self._curve[tenor] = [v, 0.0, 0]
                    continue
                d = (v - st[0]) / math.sqrt(n)
                if st[2] >= 30 and st[1] > 0 and not masked:
                    zs.append(min(abs(d) / math.sqrt(st[1]), 25.0))
                st[2] += 1
                st[1] += max(self._a_base, 1.0 / st[2]) * (d * d - st[1])
                st[0] = v
            zc = math.sqrt(sum(z * z for z in zs) / len(zs)) if zs else 0.0
            self.curve_peak = max(self.curve_peak * self._d_jump, zc)
            if zs or "curve" in self.components:
                comps["curve"] = _clip01((self.curve_peak - c.curve_lo) / (c.curve_hi - c.curve_lo))
                weights["curve"] = c.w_curve
            if vals:
                med = sorted(vals)[len(vals) // 2]
                long_earns = med < 0                    # quote rate below base rate: long base earns carry
                adverse = dd_z if long_earns else ru_z  # the move that hurts whoever is earning the carry
                unwind = _clip01((adverse - c.dd_lo) / (c.dd_hi - c.dd_lo)) * (0.5 + 0.5 * comps.get("curve", 0.0))
                self.carry = {"curve": {k: v for k, v in curve.items() if _fin(v)}, "median": med, "earning_side": "long" if long_earns else "short",
                              "dislocation_z": zc, "dislocation_peak_z": self.curve_peak,
                              "unwind_score": unwind, "unwind": unwind >= 0.5}

        self.components = comps
        if self.n_ret < c.warmup_bars or not sb:
            self.score = None
            return
        wsum = sum(weights.values())
        wmean = sum(comps[k] * weights[k] for k in comps if k in weights) / wsum
        core = max(comps["vol"], comps["jump"], comps["drawdown"])
        aux = max(comps.get("spread", 0.0), comps.get("curve", 0.0))
        self.score = round(100.0 * (0.5 * wmean + 0.5 * max(core, c.aux_cap * aux)), 2)
        self.peak_score = max(self.peak_score, self.score)
        self._update_level(ts, price)

    def _update_level(self, ts: float, price: float) -> None:
        c = self.cfg
        thr = [0.0, c.thr_watch, c.thr_warning, c.thr_tornado]
        s = self.score
        target = max(i for i in range(4) if s >= thr[i])
        old = self.level_idx
        if target > old:
            self.level_idx, self._below = target, 0
        elif old > 0 and s < thr[old] - c.hysteresis:
            self._below += 1
            if self._below >= c.cooldown_bars:
                self.level_idx = max(i for i in range(4) if s >= thr[i] - c.hysteresis or i == 0)
                self._below = 0
        else:
            self._below = 0
        self.elevated_bars = self.elevated_bars + 1 if self.level_idx > 0 else 0
        self.heat *= 0.5 ** (self._dt / c.heat_halflife_s)
        if self.level_idx >= 2:
            self.heat += c.bar_seconds
        if self.level_idx != old:
            t = Transition(ts, LEVELS[old], LEVELS[self.level_idx], s, price,
                           max(self.components, key=lambda k: self.components[k] * (
                               self.cfg.aux_cap if k in ("spread", "curve") else 1.0)),
                           self.kind, dict(self.components))
            t.carry_unwind = bool(self.carry.get("unwind"))
            self.transitions.append(t)
            if self.on_transition:
                self.on_transition(t)

    # ----------------------------------------------------------------- outputs
    @property
    def ready(self) -> bool:
        return self.score is not None

    @property
    def level(self) -> str:
        return LEVELS[self.level_idx] if self.ready else "warming_up"

    @property
    def kind(self) -> str:
        """calm / squall (short-lived alert, e.g. a data release) / storm (alerts that persist or cluster)."""
        if not self.ready or self.level_idx == 0:
            return "calm"
        return "squall" if self.heat < self.cfg.squall_s else "storm"

    def is_stale(self, now: float | None = None) -> bool:
        if self.last_rx is None:
            return False
        return ((self.clock() if now is None else now) - self.last_rx) > self.cfg.stale_after_s

    def annualise(self, sigma_bar: float) -> float:
        return sigma_bar * math.sqrt(SECONDS_PER_YEAR / self.cfg.bar_seconds)

    def regime(self) -> dict:
        """What exposure and the simulator need to know about the current regime."""
        sb = self._sigma_base()
        return {
            "ready": self.ready,
            "price": self.price,
            "bar_seconds": self.cfg.bar_seconds,
            "sigma_fast": self._sigma_fast() if self.n_ret else None,
            "sigma_base": sb,
            "returns": np.asarray(self.returns, dtype=float),
            "level": self.level,
        }

    def snapshot(self, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        age = None if self.last_rx is None else max(0.0, now - self.last_rx)
        if self.last_rx is None:
            state = "no_data"
        elif age > self.cfg.stale_after_s:
            state = "stale"
        elif not self.ready:
            state = "warming_up"
        else:
            state = "live"
        live = state == "live"
        sb = self._sigma_base()
        sf = self._sigma_fast() if self.n_ret else None
        return {
            "symbol": self.symbol,
            "state": state,
            # never a false green: level mirrors the state unless the score is trustworthy
            "level": LEVELS[self.level_idx] if live else state,
            "score": self.score if live else None,
            "last_score": self.score,
            "last_level": self.level,
            "peak_score": self.peak_score,
            "kind": self.kind,
            "elevated_bars": self.elevated_bars,
            "storm_heat_min": round(self.heat / 60.0, 1),
            "carry": self.carry or None,
            "price": self.price,
            "bid": self.bid,
            "ask": self.ask,
            "ts": self.price_ts,
            "components": {k: round(v, 4) for k, v in self.components.items()},
            "features": {k: round(v, 6) for k, v in self.raw.items()},
            "vol": {
                "sigma_bar_fast": sf,
                "sigma_bar_base": sb,
                "annualised_fast": self.annualise(sf) if sf else None,
                "annualised_base": self.annualise(sb) if sb else None,
            },
            "warmup": {"have": self.n_ret, "need": self.cfg.warmup_bars},
            "feed": {
                "age_s": age,
                "stale_after_s": self.cfg.stale_after_s,
                "accepted": self.accepted,
                "rejected": dict(self.rejected),
                "gaps": self.gaps,
                "pending_outlier": self._pending is not None,
            },
        }
