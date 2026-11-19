"""Build the bundled SGX replay files from the raw challenge download (nested zips read directly).

usage: python scripts/build_sgx.py [data/sgx/Challenge1.zip | data/sgx/extracted]
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stormchaser.adapters.sgx import available, read_sgx  # noqa: E402

OUT = ROOT / "stormchaser" / "data"


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "sgx" / "Challenge1.zip"
    print("pairs/tenors:", available(src))
    for pair in ("USDJPY", "EURUSD", "USDSGD"):
        df = read_sgx(src, pair)
        for c in ("bid", "ask", "mid"):
            df[c] = df[c].astype("float64")
        out = OUT / f"sgx_{pair.lower()}_2026.parquet"
        df.to_parquet(out, index=False, compression="zstd")
        print(pair, len(df), "rows", df["timestamp"].min(), "->", df["timestamp"].max(), f"{out.stat().st_size / 1e6:.2f} MB",
              "NaN carry rows:", int(df.filter(like="carry_").isna().any(axis=1).sum()))
    print(df.tail(2).T)


if __name__ == "__main__":
    main()
