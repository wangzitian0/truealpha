"""Seed the reviewed headcount facts the deployed pipeline used to hold as a dict (#70).

These are the exact 21 figures that were literals in
`datahub/production_topt/headcount.py`. Moving them here does not make them better data —
it makes them *data*: superseding one is an insert, and the row states plainly what it is.

They are recorded honestly as what they are: a manual entry with no verified filing
pointer. Hence `source='manual-review'` and a confidence of 0.70 rather than the 1.0 the
code implied by having no confidence at all. When #70's extraction lands, it writes rows
with real accessions and evidence spans at a higher confidence, and those supersede these
by carrying a later `knowable_at` — no deletion, no edit, no deploy.

Known suspect, left as-is deliberately: AVGO at 20,000 looks stale against Broadcom's
recent disclosures (~37k). Silently "fixing" a number I cannot cite would be the exact
behaviour this table exists to prevent — it should be superseded by a row that names its
filing. Tracked as its own issue.

`knowable_at` is set to the seed's declared as-of, not the insertion clock: a row stamped
at insert time would be look-ahead for every historical cutoff before today.

Usage:
    uv run --package truealpha-data-engine python apps/data-engine/scripts/seed_issuer_headcounts.py [--dry-run]
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from decimal import Decimal

import psycopg
from data_engine.config import settings
from data_engine.datahub.production_topt.headcount import record_headcount
from data_engine.sources import sec

# The figures as the deployed pipeline carried them, by ticker.
REVIEWED_HEADCOUNTS: dict[str, str] = {
    "AAPL": "164000",
    "MSFT": "228000",
    "GOOG": "182502",
    "GOOGL": "182502",
    "NVDA": "29600",
    "META": "67317",
    "AMZN": "1556000",
    "TSLA": "140473",
    "AVGO": "20000",
    "COST": "316000",
    "NFLX": "14000",
    "MU": "48000",
    "WMT": "2100000",
    "LLY": "43000",
    "ABBV": "50000",
    "JNJ": "138100",
    "XOM": "62000",
    "JPM": "309926",
    "MA": "33400",
    "V": "28800",
    "BRK.B": "392400",
}
# What these figures were knowable as of. Chosen well before any cutoff the spine runs at,
# and stated explicitly rather than defaulted to now(), so a replay of an older cutoff
# resolves them instead of finding nothing.
_KNOWABLE_AT = datetime(2026, 1, 1, tzinfo=UTC)
_SOURCE = "manual-review"
_EVIDENCE = "10-K human capital disclosure; manual entry, no verified accession (#70 supersedes)"
_CONFIDENCE = Decimal("0.70")


def _sec_ticker(ticker: str) -> str:
    return ticker.replace(".", "-")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="resolve CIKs and print, write nothing")
    args = parser.parse_args()

    index = sec.ticker_cik_index()
    resolved: list[tuple[str, int, str]] = []
    for ticker, value in sorted(REVIEWED_HEADCOUNTS.items()):
        cik = index.get(_sec_ticker(ticker))
        if cik is None:
            print(f"  {ticker}: no SEC CIK mapping, skipped")
            continue
        resolved.append((ticker, cik, value))

    if args.dry_run:
        for ticker, cik, value in resolved:
            print(f"  {ticker:<6} CIK{cik:010d}  {value}")
        print(f"{len(resolved)} facts would be written")
        return 0

    with psycopg.connect(settings.database_url) as connection:
        for ticker, cik, value in resolved:
            fact_id = record_headcount(
                connection,
                cik=cik,
                headcount=Decimal(value),
                knowable_at=_KNOWABLE_AT,
                source=_SOURCE,
                evidence_ref=_EVIDENCE,
                confidence=_CONFIDENCE,
            )
            print(f"  {ticker:<6} CIK{cik:010d}  {value}  -> staging.issuer_headcount_facts:{fact_id}")
        connection.commit()
    print(f"{len(resolved)} headcount facts recorded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
