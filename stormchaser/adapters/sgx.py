"""SGX FX challenge dataset adapter.

Layout: Challenge1.zip -> 30 inner zips "<PAIR>_<CCY>_<SPOT_SPOT|FORWARD_<tenor>>_<notional>_<from>-<to>.zip"
-> one CSV per day (1-minute bars, UTC, bid/ask/mid OHLC; forwards add *_fp forward points).
This module reads the nested zips directly (no manual extraction) and derives the forward curve:
forward points -> annualised implied rate differential ("carry") per tenor.
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

import pandas as pd

from .replay import DataError

TENORS = ["1W", "2W", "3W", "1M", "2M", "3M", "6M", "9M", "1Y"]
TENOR_DAYS = {"1W": 7, "2W": 14, "3W": 21, "1M": 30, "2M": 61, "3M": 91, "6M": 182, "9M": 273, "1Y": 365}
_NAME = re.compile(r"(?P<pair>[A-Z]{6})_[A-Z]{3}_(?P<kind>SPOT|FORWARD)_(?P<tenor>[A-Z0-9]+)_\d+_\d{8}-\d{8}\.zip$")


def implied_carry(fwd_points, spot, days):
    """Annualised implied rate differential (quote minus base), simple compounding, ACT/365.

    Covered interest parity: F/S = (1 + r_quote*T)/(1 + r_base*T)  =>  r_quote - r_base ~= (F - S)/S / T.
    Negative for USDJPY (JPY rates below USD): being long the base currency earns the carry.
    """
    return fwd_points / spot * (365.0 / days)


def _inner_zips(path: Path) -> dict[tuple[str, str], bytes | Path]:
    """Map (pair, tenor) -> inner zip (bytes if nested in an outer zip, Path if already extracted)."""
    out: dict[tuple[str, str], bytes | Path] = {}
    if path.is_dir():
        for p in sorted(path.rglob("*.zip")):
            m = _NAME.search(p.name)
            if m and "__MACOSX" not in p.parts:
                out[(m["pair"], m["tenor"])] = p
        return out
    if not path.exists():
        raise DataError(f"SGX data not found: {path.name}")
    m = _NAME.search(path.name)
    if m:                                               # a single inner zip
        return {(m["pair"], m["tenor"]): path}
    try:
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                m = _NAME.search(n)
                if m and "__MACOSX" not in n:
                    out[(m["pair"], m["tenor"])] = z.read(n)
    except zipfile.BadZipFile as e:
        raise DataError(f"{path.name} is not a valid zip (incomplete download?)") from e
    return out


def _read_days(src: bytes | Path, cols: list[str]) -> pd.DataFrame:
    frames = []
    try:
        with zipfile.ZipFile(io.BytesIO(src) if isinstance(src, bytes) else src) as z:
            for n in sorted(z.namelist()):
                if n.lower().endswith(".csv") and "__MACOSX" not in n and not Path(n).name.startswith("._"):
                    with z.open(n) as f:
                        try:
                            frames.append(pd.read_csv(f, usecols=lambda c: c in cols))
                        except pd.errors.EmptyDataError:
                            continue                    # an empty day file is not an error
    except zipfile.BadZipFile as e:
        raise DataError("corrupt inner zip in SGX dataset") from e
    if not frames:
        raise DataError("no CSV files inside SGX zip")
    return pd.concat(frames, ignore_index=True)


def available(path: str | Path) -> dict[str, list[str]]:
    pairs: dict[str, list[str]] = {}
    for pair, tenor in _inner_zips(Path(path)):
        pairs.setdefault(pair, []).append(tenor)
    return {k: sorted(v) for k, v in sorted(pairs.items())}


def read_sgx(path: str | Path, pair: str = "USDJPY", curve: bool = True, start: str | None = None,
             end: str | None = None) -> pd.DataFrame:
    """Spot 1m bars (timestamp, bid, ask, mid) joined with carry_<tenor> columns for every forward found."""
    inner = _inner_zips(Path(path))
    pair = pair.upper()
    if (pair, "SPOT") not in inner:
        have = sorted({p for p, _ in inner})
        raise DataError(f"no SGX spot data for {pair!r}; pairs available: {have}")
    spot = _read_days(inner[(pair, "SPOT")], ["timestamp", "settlement_date", "close_bid", "close_ask", "close_mid"])
    spot["spot_settle"] = pd.to_datetime(spot.pop("settlement_date"), utc=True, errors="coerce")
    spot = spot.rename(columns={"close_bid": "bid", "close_ask": "ask", "close_mid": "mid"})
    spot["timestamp"] = pd.to_datetime(spot["timestamp"], utc=True, errors="coerce")
    spot = spot.dropna(subset=["timestamp"]).drop_duplicates("timestamp").sort_values("timestamp")
    if curve:
        for tenor in TENORS:
            if (pair, tenor) not in inner:
                continue
            f = _read_days(inner[(pair, tenor)], ["timestamp", "settlement_date", "close_mid_fp"])
            f["timestamp"] = pd.to_datetime(f["timestamp"], utc=True, errors="coerce")
            f = f.dropna(subset=["timestamp"]).drop_duplicates("timestamp")
            f = f.assign(**{f"fp_{tenor}": f["close_mid_fp"],
                            f"settle_{tenor}": pd.to_datetime(f["settlement_date"], utc=True, errors="coerce")})[
                ["timestamp", f"fp_{tenor}", f"settle_{tenor}"]]
            spot = spot.merge(f, on="timestamp", how="left")
            # tenor length = forward settlement - spot settlement (both roll together at the NY close)
            days = (spot.pop(f"settle_{tenor}") - spot["spot_settle"]).dt.days
            spot[f"days_{tenor}"] = days.where(days > 0, TENOR_DAYS[tenor]).fillna(TENOR_DAYS[tenor])
            spot[f"carry_{tenor}"] = implied_carry(spot[f"fp_{tenor}"], spot["mid"], spot[f"days_{tenor}"]
                                                   ).astype("float32")
            spot = spot.drop(columns=[f"fp_{tenor}", f"days_{tenor}"])
    spot = spot.drop(columns=["spot_settle"])
    if start:
        spot = spot[spot["timestamp"] >= pd.Timestamp(start, tz="UTC")]
    if end:
        spot = spot[spot["timestamp"] < pd.Timestamp(end, tz="UTC")]
    if len(spot) == 0:
        raise DataError(f"no SGX rows for {pair} in the requested window")
    return spot.reset_index(drop=True)
