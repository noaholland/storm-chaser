"""Cyclone simulator: Monte Carlo fan of price and P&L over the next N bars.

- "garch": GARCH(1,1) with variance targeting. Starts at the *current* (storm) variance and mean-reverts
  to the calm baseline; compound-Poisson jumps calibrated from the regime's own tail returns.
- "bootstrap": i.i.d. resampling of the current regime's returns.
All inputs are clamped; the output is always finite.
"""
from __future__ import annotations

import math

import numpy as np

MAX_STEPS, MAX_PATHS, MAX_CELLS, MAX_POINTS = 5000, 20000, 4_000_000, 200
MIN_PATHS = 200


def _fin(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _clamp(name, val, lo, hi, default, clamped, integer=True):
    if not _fin(val):
        clamped.append(f"{name}: invalid, using {default}")
        return default
    v = min(max(val, lo), hi)
    v = int(v) if integer else float(v)
    if v != val:
        clamped.append(f"{name}: {val} -> {v}")
    return v


def simulate(regime: dict, value: float | None = None, steps: int = 60, n_paths: int = 2000, method: str = "garch",
             seed: int | None = None, alpha: float = 0.08, beta: float = 0.90, jump_k: float = 4.0,
             vol_mult: float = 1.0) -> dict:
    clamped: list[str] = []
    price = regime.get("price")
    if not _fin(price) or price <= 0:
        return {"status": "no_price", "error": "no valid price for this asset yet"}
    sf, sb = regime.get("sigma_fast"), regime.get("sigma_base")
    if not regime.get("ready") or not _fin(sf) or not _fin(sb) or sf <= 0 or sb <= 0:
        return {"status": "warming_up", "error": "not enough history to calibrate the simulator yet"}
    steps = _clamp("steps", steps, 1, MAX_STEPS, 60, clamped)
    n_paths = _clamp("n_paths", n_paths, MIN_PATHS, MAX_PATHS, 2000, clamped)
    if steps * n_paths > MAX_CELLS:
        new = max(MIN_PATHS, MAX_CELLS // steps)
        clamped.append(f"n_paths: {n_paths} -> {new} (steps*paths budget)")
        n_paths = new
    vol_mult = _clamp("vol_mult", vol_mult, 0.1, 10.0, 1.0, clamped, integer=False)
    alpha = _clamp("alpha", alpha, 0.0, 0.5, 0.08, clamped, integer=False)
    beta = _clamp("beta", beta, 0.0, 0.999, 0.90, clamped, integer=False)
    if alpha + beta >= 0.999:
        beta = 0.999 - alpha
        clamped.append(f"beta: reduced to {beta:.3f} so alpha+beta<1 (stationary)")
    if method not in ("garch", "bootstrap"):
        clamped.append(f"method: {method!r} unknown, using garch")
        method = "garch"
    if not _fin(value):
        value = price                                   # default: one unit of the asset
    rets = np.asarray(regime.get("returns", []), dtype=float)
    rets = rets[np.isfinite(rets)]
    if method == "bootstrap" and len(rets) < 30:
        clamped.append("bootstrap: fewer than 30 regime returns, using garch")
        method = "garch"

    rng = np.random.default_rng(seed if _fin(seed) else None)
    sf, sb = sf * vol_mult, sb * vol_mult
    info: dict = {}
    if method == "bootstrap":
        r = rng.choice(rets, size=(n_paths, steps), replace=True) * vol_mult
    else:
        jumps = rets[np.abs(rets) > jump_k * sb / vol_mult] * vol_mult if len(rets) else np.array([])
        lam = min(len(jumps) / max(len(rets), 1), 0.25)
        j_sd = float(np.sqrt(np.mean(jumps ** 2))) if len(jumps) else 0.0
        j_mu = float(jumps.mean()) if len(jumps) else 0.0
        long_var = sb * sb
        omega = (1 - alpha - beta) * long_var
        var = np.full(n_paths, sf * sf)
        r = np.empty((n_paths, steps))
        cap = (50 * max(sf, sb)) ** 2
        for t in range(steps):
            eps = np.sqrt(var) * rng.standard_normal(n_paths)
            r[:, t] = eps
            var = np.minimum(omega + alpha * eps * eps + beta * var, cap)
        if lam > 0 and j_sd > 0:
            n_j = rng.poisson(lam, size=(n_paths, steps))
            r += n_j * j_mu + np.sqrt(n_j) * j_sd * rng.standard_normal((n_paths, steps))
        info = {"jump_prob_per_bar": lam, "jump_sd": j_sd, "alpha": alpha, "beta": beta,
                "sigma_start": sf, "sigma_long_run": sb}
    r = np.clip(np.nan_to_num(r, nan=0.0, posinf=0.5, neginf=-0.5), -0.5, 0.5)
    paths = np.exp(np.clip(np.cumsum(r, axis=1), -20, 20))      # price relative to now
    idx = np.unique(np.linspace(0, steps - 1, min(steps, MAX_POINTS)).round().astype(int))
    q = np.quantile(paths[:, idx], [0.05, 0.5, 0.95], axis=0)
    pnl = value * (q - 1.0)
    if value < 0:                                               # short: the bad tail is the up-move
        pnl = pnl[::-1]
    term = value * (paths[:, -1] - 1.0)
    worst = value * ((paths.min(axis=1) if value >= 0 else paths.max(axis=1)) - 1.0)
    q5 = float(np.quantile(term, 0.05))
    bs = regime.get("bar_seconds") or 0
    return {
        "status": "ok", "method": method, "steps": steps, "n_paths": n_paths, "bar_seconds": bs,
        "horizon_seconds": steps * bs, "start_price": price, "position_value": value, "clamped": clamped,
        "fan": {
            "step": (idx + 1).tolist(),
            "price_p5": (price * q[0]).tolist(), "price_p50": (price * q[1]).tolist(),
            "price_p95": (price * q[2]).tolist(),
            "pnl_p5": pnl[0].tolist(), "pnl_p50": pnl[1].tolist(), "pnl_p95": pnl[2].tolist(),
        },
        "terminal": {
            "pnl_p5": q5, "pnl_p50": float(np.median(term)), "pnl_p95": float(np.quantile(term, 0.95)),
            "expected_shortfall_5": float(term[term <= q5].mean()), "prob_loss": float((term < 0).mean()),
            "worst_drawdown_p5": float(np.quantile(worst, 0.05)),
        },
        "model": info,
    }
