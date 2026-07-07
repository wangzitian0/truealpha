"""Phase -1 sampling: pull SEC company-facts for the test names and print a quick
availability summary per ticker (which revenue/gross-profit/headcount tags exist).

Usage:
    uv run --package truealpha-data-engine python apps/data-engine/scripts/pull_sec_samples.py [TICKER ...]

Output lands in apps/data-engine/data/samples/ (gitignored — raw material, not code).
"""

import json
import sys
from pathlib import Path

from data_engine.sources.sec import save_sample

DEFAULT_TICKERS = ["DDOG", "NICE", "SHOP", "DUOL"]  # init.md Section 11
OUT_DIR = Path(__file__).resolve().parents[1] / "data" / "samples"

# Tags worth eyeballing first; absence is a finding, not an error.
INTERESTING_TAGS = [
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "GrossProfit",
    "CostOfRevenue",
    "EntityNumberOfEmployees",
]


def summarize(path: Path) -> None:
    facts = json.loads(path.read_text())
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    dei = facts.get("facts", {}).get("dei", {})
    print(f"\n{facts.get('entityName')} ({path.name})")
    print(f"  us-gaap tags: {len(us_gaap)}, dei tags: {len(dei)}")
    for tag in INTERESTING_TAGS:
        hit = us_gaap.get(tag) or dei.get(tag)
        n = sum(len(v) for v in hit.get("units", {}).values()) if hit else 0
        print(f"  {'✓' if hit else '✗'} {tag}: {n} data points")


def main() -> None:
    tickers = sys.argv[1:] or DEFAULT_TICKERS
    for ticker in tickers:
        try:
            path = save_sample(ticker, OUT_DIR)
        except Exception as e:  # keep sampling the rest; a failed source is itself a finding
            print(f"\n{ticker}: FAILED — {e}")
            continue
        summarize(path)
    print(f"\nRaw JSON saved under {OUT_DIR}")


if __name__ == "__main__":
    main()
