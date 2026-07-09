"""Phase -1 sampling: daily price bars via a direct Yahoo Finance chart-endpoint
call (see data_engine.sources.yahoo — NOT the yfinance package, which gets
HTTP 429 on the VPS).

Usage:
    uv run --package truealpha-data-engine python apps/data-engine/scripts/pull_price_samples.py [TICKER ...]

CSV lands in apps/data-engine/data/samples/ (gitignored).
"""

import csv
import sys
from pathlib import Path

from data_engine.sources.yahoo import fetch_daily_bars

DEFAULT_TICKERS = ["DDOG", "NICE", "SHOP", "DUOL"]
OUT_DIR = Path(__file__).resolve().parents[1] / "data" / "samples"


def main() -> None:
    tickers = sys.argv[1:] or DEFAULT_TICKERS
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for ticker in tickers:
        try:
            bars = fetch_daily_bars(ticker)
        except Exception as e:
            print(f"{ticker}: FAILED — {e}")
            continue
        if not bars:
            print(f"{ticker}: FAILED — empty response")
            continue
        path = OUT_DIR / f"{ticker.upper()}_prices_1y.csv"
        with path.open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume"])
            for bar in bars:
                writer.writerow([bar.date, bar.open, bar.high, bar.low, bar.close, bar.adj_close, bar.volume])
        print(f"{ticker}: {len(bars)} daily bars, {bars[0].date} → {bars[-1].date}, saved {path.name}")


if __name__ == "__main__":
    main()
