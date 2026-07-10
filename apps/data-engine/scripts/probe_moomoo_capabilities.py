"""Phase -1 audit: does moomoo actually have usable historical analyst-rating
data for our test universe? (init.md Section 5 — "unverified, don't assume".)

REQUIRES the moomoo OpenD gateway already running and logged into a real
account (download: https://www.moomoo.com/download/OpenAPI). This script
cannot start or log into OpenD itself — that's an interactive 2FA/app flow.

Spends at most 3 calls per ticker x len(tickers) — small on purpose, this is
a capability check, not a data pull. Every call is gated by moomoo_ledger
(a self-imposed precautionary cap, init.md Section 1 rule 6 — not a real
moomoo-side monthly quota, see init.md Section 5's 2026-07-10 correction).

Usage:
    uv run --package truealpha-data-engine python apps/data-engine/scripts/probe_moomoo_capabilities.py [TICKER ...]
"""

import sys

from data_engine.sources import moomoo as mm
from data_engine.sources.moomoo_ledger import BudgetExceededError, calls_this_month

DEFAULT_TICKERS = ["DDOG", "NICE", "SHOP", "DUOL"]


def probe_ticker(ctx, ticker: str) -> None:
    code = f"US.{ticker}"
    print(f"\n{ticker} ({code})")

    try:
        consensus = mm.get_analyst_consensus(ctx, code)
        print(f"  analyst_consensus: {consensus}")
    except mm.MoomooConnectionError as e:
        print(f"  analyst_consensus: FAILED — {e}")

    try:
        summary = mm.get_rating_summary(ctx, code, analyst_dimension=True)
        n = len(summary) if hasattr(summary, "__len__") else "?"
        print(f"  rating_summary (analyst dimension): {n} rows")
        if hasattr(summary, "head"):
            print(summary.head(3).to_string())
    except mm.MoomooConnectionError as e:
        print(f"  rating_summary: FAILED — {e}")


def main() -> None:
    tickers = sys.argv[1:] or DEFAULT_TICKERS
    print(f"moomoo calls used this month before this run: {calls_this_month()}")

    try:
        with mm.connect() as ctx:
            for ticker in tickers:
                probe_ticker(ctx, ticker)

            print("\nUS market-wide recent rating changes (not per-ticker history):")
            changes = mm.get_market_rating_changes(ctx, count=10)
            print(changes)
    except mm.MoomooConnectionError as e:
        print(f"\nFAILED: {e}")
    except BudgetExceededError as e:
        print(f"\nBLOCKED: {e}")

    print(f"\nmoomoo calls used this month after this run: {calls_this_month()}")


if __name__ == "__main__":
    main()
