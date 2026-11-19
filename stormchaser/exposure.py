"""Exposure: what the current storm means for a user's positions.

P&L now, parametric VaR / ES at the current regime's volatility (vs the calm baseline), and a stressed
loss: the worst horizon-length move actually seen in the current regime window, never less than ES.
"""
from __future__ import annotations

import math
from typing import Callable

import numpy as np

from .models import USD_LIKE, norm_symbol, split_symbol

MAX_HORIZON = 100_000
Z = {0.90: 1.2815515655, 0.95: 1.6448536270, 0.975: 1.9599639845, 0.99: 2.3263478740, 0.995: 2.5758293035}


def norm_ppf(p: float) -> float:
    """Acklam's rational approximation (abs err < 1.2e-9); avoids a scipy dependency."""
    if p in Z:
        return Z[p]
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02, 1.383577518672690e+02,
         -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02, 6.680131188771972e+01,
         -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00, -2.549732539343734e+00,
         4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    if p < 0.02425:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > 1 - 0.02425:
        return -norm_ppf(1 - p)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def _fin(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def var_es(value: float, sigma_h: float, conf: float) -> tuple[float, float]:
    """Loss VaR and ES (positive numbers) for a position worth `value` (signed) when the horizon
    log-return is N(0, sigma_h^2):  VaR = |V| * (1 - exp(-z * sigma_h)) for a long, and
    |V| * (exp(z * sigma_h) - 1) for a short; ES uses z_ES = pdf(z) / (1 - conf) in place of z.
    Fat tails and jumps are deliberately left to the stressed loss and the simulator."""
    if value == 0 or sigma_h <= 0:
        return 0.0, 0.0
    z = norm_ppf(conf)
    es_z = math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi) / (1 - conf)
    sgn = 1.0 if value > 0 else -1.0
    var = -value * math.expm1(-sgn * min(z * sigma_h, 5))
    es = -value * math.expm1(-sgn * min(es_z * sigma_h, 5))
    return max(0.0, var), max(0.0, es)


def worst_move(returns: np.ndarray, h: int, sign: float) -> float:
    """Most adverse h-bar log return in the sample for a long (sign>0) or short (sign<0)."""
    if len(returns) == 0:
        return 0.0
    if len(returns) > h:
        cs = np.concatenate([[0.0], np.cumsum(returns)])
        w = cs[h:] - cs[:-h]
    else:                                   # not enough history for a full window: scale by sqrt(time)
        w = returns * math.sqrt(h)
    return float(w.min() if sign > 0 else w.max())


def compute_exposure(positions: list[dict], lookup: Callable[[str], dict | None], horizon_bars: int = 60,
                     confidence: float = 0.99) -> dict:
    """`lookup(asset)` returns an engine regime dict (see StormEngine.regime) or None if unknown."""
    notes: list[str] = []
    if not _fin(confidence) or not (0.5 <= confidence < 1.0):
        notes.append("confidence out of range; using 0.99")
        confidence = 0.99
    confidence = min(confidence, 0.9999)
    if not _fin(horizon_bars) or horizon_bars < 1:
        notes.append("horizon_bars must be >= 1; using 1")
        horizon_bars = 1
    if horizon_bars > MAX_HORIZON:
        notes.append(f"horizon_bars clamped to {MAX_HORIZON}")
        horizon_bars = MAX_HORIZON
    h = int(horizon_bars)

    rows, by_ccy = [], {}
    for i, p in enumerate(positions or []):
        row = {"index": i, "asset": p.get("asset") if isinstance(p, dict) else None, "status": "ok"}
        rows.append(row)
        if not isinstance(p, dict) or not isinstance(p.get("asset"), str) or not norm_symbol(p["asset"]):
            row.update(status="error", error="position needs an 'asset' string")
            continue
        size = p.get("size")
        if not _fin(size):
            row.update(status="error", error="size must be a finite number")
            continue
        reg = lookup(p["asset"])
        price = p.get("price") if _fin(p.get("price")) and p.get("price") > 0 else (reg or {}).get("price")
        if reg is None and not _fin(price):
            row.update(status="unknown_asset", error=f"no feed or price for {p['asset']!r}; start a replay/live "
                                                     "session for it or pass 'price'")
            continue
        if not _fin(price) or price <= 0:
            row.update(status="error", error="no valid price yet")
            continue
        base, quote = split_symbol((reg or {}).get("symbol") or p["asset"])
        ccy = norm_symbol(str(p.get("currency") or ""))
        if not ccy or ccy == base:
            units = float(size)
        elif ccy == quote or (ccy in USD_LIKE and quote in USD_LIKE):
            units = float(size) / price                      # size given as notional in quote currency
        else:
            row.update(status="error", error=f"currency {ccy} is neither {base} (units) nor {quote} (notional)")
            continue
        rep = "USD" if quote in USD_LIKE else quote
        value = units * price
        row.update(units=units, price=price, value=value, currency=rep)
        entry = p.get("entry_price")
        row["pnl"] = units * (price - entry) if _fin(entry) and entry > 0 else None
        tot = by_ccy.setdefault(rep, {"value": 0.0, "gross": 0.0, "pnl": 0.0, "var": 0.0, "es": 0.0,
                                      "var_baseline": 0.0, "stressed_loss": 0.0, "_var2": 0.0, "_incomplete": False})
        tot["value"] += value
        tot["gross"] += abs(value)
        tot["pnl"] += row["pnl"] or 0.0
        if units == 0:
            row.update(status="zero", var=0.0, es=0.0, var_baseline=0.0, stressed_loss=0.0)
            continue
        sig = p.get("sigma_bar") if _fin(p.get("sigma_bar")) and p.get("sigma_bar") > 0 else None
        sf = sig or (reg or {}).get("sigma_fast")
        sb = sig or (reg or {}).get("sigma_base")
        if not (reg and reg.get("ready")) and sig is None or not sf or not sb:
            row.update(status="warming_up", var=None, es=None, var_baseline=None, stressed_loss=None,
                       note="risk numbers unavailable until the feed has warmed up")
            tot["_incomplete"] = True
            continue
        rets = (reg or {}).get("returns")
        rets = rets[np.isfinite(rets)] if rets is not None else np.array([])
        sq = math.sqrt(h)
        var, es = var_es(value, sf * sq, confidence)
        var_b, _ = var_es(value, sb * sq, confidence)
        wm = worst_move(rets, h, 1.0 if value > 0 else -1.0)
        stressed = max(es, -value * math.expm1(max(min(wm, 5), -5)))
        row.update(var=var, es=es, var_baseline=var_b, stressed_loss=stressed, method="normal",
                   sigma_bar=sf, sigma_bar_baseline=sb, vol_ratio=sf / sb, level=(reg or {}).get("level"),
                   worst_regime_move=math.expm1(wm))
        for k, v in (("var", var), ("es", es), ("var_baseline", var_b), ("stressed_loss", stressed)):
            tot[k] += v
        tot["_var2"] += var * var

    totals = {}
    for ccy, t in by_ccy.items():
        totals[ccy] = {
            "value": t["value"], "gross": t["gross"], "pnl": t["pnl"],
            "var": t["var"],                                  # undiversified: every storm hits at once
            "var_diversified": math.sqrt(t["_var2"]),         # zero-correlation floor
            "es": t["es"], "var_baseline": t["var_baseline"], "stressed_loss": t["stressed_loss"],
            "stressed_loss_pct_gross": t["stressed_loss"] / t["gross"] if t["gross"] > 0 else 0.0,
            "incomplete": t["_incomplete"],
        }
    if not positions:
        notes.append("empty portfolio: nothing at risk")
    elif not totals or all(t["gross"] == 0 for t in totals.values()):
        notes.append("no valued, non-zero positions: nothing at risk")
    return {"horizon_bars": h, "confidence": confidence, "positions": rows, "totals": totals, "notes": notes}
