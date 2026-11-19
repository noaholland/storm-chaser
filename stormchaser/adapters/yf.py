"""yfinance fallback for equities / commodities / FX. History is fetched once and replayed."""
from __future__ import annotations

from ..models import Tick
from .replay import DataError, frame_to_ticks

# dropdown for the frontend
PRESETS = {
    "S&P 500": "^GSPC", "Nasdaq 100": "^NDX", "FTSE 100": "^FTSE", "Straits Times": "^STI", "VIX": "^VIX",
    "Gold": "GC=F", "Brent": "BZ=F", "WTI": "CL=F", "GBPUSD": "GBPUSD=X", "EURUSD": "EURUSD=X",
    "USDJPY": "JPY=X", "USDSGD": "SGD=X", "Bitcoin": "BTC-USD", "Strategy (MSTR)": "MSTR", "Nvidia": "NVDA",
}
INTERVALS = {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "1d", "5d", "1wk", "1mo"}
PERIODS = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"}


def load_yfinance(symbol: str, period: str = "5d", interval: str = "5m", download=None) -> tuple[list[Tick], dict]:
    if not symbol or not symbol.strip():
        raise DataError("symbol is required")
    if interval not in INTERVALS:
        raise DataError(f"interval must be one of {sorted(INTERVALS)}")
    if period not in PERIODS:
        raise DataError(f"period must be one of {sorted(PERIODS)}")
    try:
        if download is None:
            import yfinance as yf

            df = yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=True)
        else:
            df = download(symbol, period, interval)
    except Exception as e:  # noqa: BLE001 - network, rate limit, delisted
        raise DataError(f"yfinance failed for {symbol}: {type(e).__name__}: {e}") from e
    if df is None or len(df) == 0:
        raise DataError(f"yfinance returned no data for {symbol!r} (unknown ticker, or period/interval not available)")
    ticks, report = frame_to_ticks(df, symbol=symbol, price_col="Close" if "Close" in df.columns else None)
    report["source"] = f"yfinance {symbol} {period}/{interval}"
    return ticks, report
