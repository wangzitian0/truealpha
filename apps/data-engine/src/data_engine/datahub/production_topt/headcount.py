"""Stopgap headcount extraction behind the `HeadcountExtractor` port (#70 / #171 A2b).

Employee headcount is not a reliable XBRL concept, so the financial-fact adapter takes it
through a port rather than a branch in generic capture code. The real extraction plane —
filing text → reviewed semantic fact with evidence spans — is #70 and stays out of scope
here; until it lands, this module supplies the same public 10-K figures the deployed
pipeline used, *through the port*, so wiring the real extractor later replaces one
injected object and touches nothing else.

The figures are public 10-K disclosures, knowable well before any cutoff this spine runs
at; the extractor still stamps them at the cutoff rather than claiming a filing date it
did not read.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal

from data_engine.datahub.production_topt.sec_financial_adapter import HeadcountFact

# Public 10-K employee counts for the TOPT universe, by ticker.
STOPGAP_HEADCOUNTS: Mapping[str, str] = {
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


class StopgapHeadcountExtractor:
    """`HeadcountExtractor` serving the frozen public figures, keyed by CIK.

    Returns None for an issuer the map does not cover, so a new universe member is
    honestly headcount-less (and its GPPE `missing_headcount`) rather than silently
    borrowing another issuer's figure.
    """

    def __init__(self, by_cik: Mapping[int, Decimal]) -> None:
        self._by_cik = dict(by_cik)

    def __call__(self, cik: int, cutoff: date) -> HeadcountFact | None:
        value = self._by_cik.get(cik)
        if value is None:
            return None
        return HeadcountFact(
            value=value,
            knowable_at=datetime.combine(cutoff, datetime.min.time(), tzinfo=UTC),
        )


def headcounts_by_cik(cik_by_ticker: Mapping[str, int]) -> dict[int, Decimal]:
    """Project the ticker-keyed figures onto the run's resolved CIKs."""
    return {
        cik_by_ticker[ticker]: Decimal(value) for ticker, value in STOPGAP_HEADCOUNTS.items() if ticker in cik_by_ticker
    }
