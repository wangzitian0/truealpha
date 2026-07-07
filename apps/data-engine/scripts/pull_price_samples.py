"""Phase -1 sampling: daily price bars via yfinance (the FALLBACK price source —
no SLA, init.md Section 9; Twelve Data is the primary once an API key exists).

Usage:
    uv run --package truealpha-data-engine python apps/data-engine/scripts/pull_price_samples.py [TICKER ...]

CSV lands in apps/data-engine/data/samples/ (gitignored).
"""

import sys
from pathlib import Path

import yfinance as yf

DEFAULT_TICKERS = ["DDOG", "NICE", "SHOP", "DUOL"]
OUT_DIR = Path(__file__).resolve().parents[1] / "data" / "samples"


def main() -> None:
    tickers = sys.argv[1:] or DEFAULT_TICKERS
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for ticker in tickers:
        try:
            df = yf.download(ticker, period="1y", interval="1d", progress=False, auto_adjust=False)
        except Exception as e:
            print(f"{ticker}: FAILED — {e}")
            continue
        if df is None or df.empty:
            print(f"{ticker}: FAILED — empty response")
            continue
        path = OUT_DIR / f"{ticker.upper()}_prices_1y.csv"
        df.to_csv(path)
        print(f"{ticker}: {len(df)} daily bars, {df.index.min().date()} → {df.index.max().date()}, saved {path.name}")


if __name__ == "__main__":
    main()
