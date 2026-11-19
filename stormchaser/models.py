"""Common tick model shared by every data adapter."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Tick:
    """One market observation. `ts` is data time in epoch seconds (UTC)."""

    ts: float
    price: float | None = None
    bid: float | None = None
    ask: float | None = None
    symbol: str = ""
    extra: dict | None = None      # optional extras, e.g. {"carry": {"1M": -0.031, ...}} from a forward curve


USD_LIKE = {"USD", "USDT", "USDC", "BUSD", "FDUSD"}
_QUOTES = ("USDT", "USDC", "BUSD", "FDUSD", "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD", "BTC", "ETH")


def norm_symbol(s: str) -> str:
    """BTC-USD, btc/usdt, BTCUSDT, GBPUSD=X -> canonical upper-case without separators."""
    s = (s or "").upper().strip()
    if s.endswith("=X"):
        s = s[:-2]
    return "".join(ch for ch in s if ch.isalnum())


def split_symbol(sym: str) -> tuple[str, str]:
    """Return (base, quote). Unknown quote -> (sym, 'USD')."""
    s = norm_symbol(sym)
    for q in _QUOTES:
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)], q
    return s, "USD"


def same_asset(a: str, b: str) -> bool:
    """BTC == BTCUSDT == BTC-USD; GBPUSD == GBPUSD=X. Quote currencies in USD_LIKE are interchangeable."""
    na, nb = norm_symbol(a), norm_symbol(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ba, qa = split_symbol(na)
    bb, qb = split_symbol(nb)
    usd_a, usd_b = qa in USD_LIKE, qb in USD_LIKE
    return ba == bb and (qa == qb or (usd_a and usd_b))
