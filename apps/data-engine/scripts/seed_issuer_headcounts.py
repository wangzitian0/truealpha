"""Seed reviewed 10-K headcount facts with real SEC filing dates and accessions (#70, #1264).

Every entry in `REVIEWED_HEADCOUNT_FACTS` cites the exact 18-digit SEC accession, the
10-K filing date (`knowable_at` at `00:00:00Z` on `filing_date`), and the fiscal
`period_end` from the issuer's Item 1 Human Capital disclosure across the 2022-2026
backtest window. No constant `knowable_at` default is permitted.

Usage:
    uv run --package truealpha-data-engine python apps/data-engine/scripts/seed_issuer_headcounts.py [--dry-run]
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal

import psycopg
from data_engine.config import settings
from data_engine.datahub.production_topt.headcount import (
    record_headcount,
    validate_cited_headcount_fact,
)
from data_engine.sources import sec

_SOURCE = "manual-review"
_CONFIDENCE = Decimal("0.70")


@dataclass(frozen=True)
class ReviewedHeadcountFact:
    """One reviewed annual headcount figure cited to its SEC 10-K filing (#1264)."""

    ticker: str
    period_end: date
    filing_date: date
    accession: str
    headcount: str
    cik_override: int | None = None

    @property
    def knowable_at(self) -> datetime:
        return datetime.combine(self.filing_date, time.min, tzinfo=UTC)

    @property
    def evidence_ref(self) -> str:
        return f"accession={self.accession} form=10-K filed={self.filing_date.isoformat()} source={_SOURCE}"


def _xom_facts() -> tuple[ReviewedHeadcountFact, ...]:
    base = (
        (date(2022, 12, 31), date(2023, 2, 22), "000003408823000020", "62000"),
        (date(2023, 12, 31), date(2024, 2, 28), "000003408824000018", "62000"),
        (date(2024, 12, 31), date(2025, 2, 19), "000003408825000010", "61000"),
        (date(2025, 12, 31), date(2026, 2, 18), "000003408826000017", "58000"),
    )
    rows: list[ReviewedHeadcountFact] = []
    for period_end, filing_date, accession, value in base:
        rows.append(ReviewedHeadcountFact("XOM", period_end, filing_date, accession, value))
        rows.append(ReviewedHeadcountFact("XOM", period_end, filing_date, accession, value, cik_override=34088))
    return tuple(rows)


REVIEWED_HEADCOUNT_FACTS: tuple[ReviewedHeadcountFact, ...] = (
    # AAPL (CIK 0000320193)
    ReviewedHeadcountFact("AAPL", date(2022, 9, 24), date(2022, 10, 28), "000032019322000108", "164000"),
    ReviewedHeadcountFact("AAPL", date(2023, 9, 30), date(2023, 11, 3), "000032019323000106", "161000"),
    ReviewedHeadcountFact("AAPL", date(2024, 9, 28), date(2024, 11, 1), "000032019324000123", "164000"),
    ReviewedHeadcountFact("AAPL", date(2025, 9, 27), date(2025, 10, 31), "000032019325000079", "166000"),
    # MSFT (CIK 0000789019)
    ReviewedHeadcountFact("MSFT", date(2022, 6, 30), date(2022, 7, 28), "000156459022026876", "221000"),
    ReviewedHeadcountFact("MSFT", date(2023, 6, 30), date(2023, 7, 27), "000095017023035122", "221000"),
    ReviewedHeadcountFact("MSFT", date(2024, 6, 30), date(2024, 7, 30), "000095017024087843", "228000"),
    ReviewedHeadcountFact("MSFT", date(2025, 6, 30), date(2025, 7, 30), "000095017025100235", "228000"),
    # GOOGL (CIK 0001652044)
    ReviewedHeadcountFact("GOOGL", date(2022, 12, 31), date(2023, 2, 3), "000165204423000016", "190234"),
    ReviewedHeadcountFact("GOOGL", date(2023, 12, 31), date(2024, 1, 31), "000165204424000022", "182502"),
    ReviewedHeadcountFact("GOOGL", date(2024, 12, 31), date(2025, 2, 5), "000165204425000014", "183323"),
    ReviewedHeadcountFact("GOOGL", date(2025, 12, 31), date(2026, 2, 5), "000165204426000018", "190820"),
    # NVDA (CIK 0001045810)
    ReviewedHeadcountFact("NVDA", date(2023, 1, 29), date(2023, 2, 24), "000104581023000017", "26196"),
    ReviewedHeadcountFact("NVDA", date(2024, 1, 28), date(2024, 2, 21), "000104581024000029", "29600"),
    ReviewedHeadcountFact("NVDA", date(2025, 1, 26), date(2025, 2, 26), "000104581025000023", "36000"),
    ReviewedHeadcountFact("NVDA", date(2026, 1, 25), date(2026, 2, 25), "000104581026000021", "42000"),
    # META (CIK 0001326801)
    ReviewedHeadcountFact("META", date(2022, 12, 31), date(2023, 2, 2), "000132680123000013", "86482"),
    ReviewedHeadcountFact("META", date(2023, 12, 31), date(2024, 2, 2), "000132680124000012", "67317"),
    ReviewedHeadcountFact("META", date(2024, 12, 31), date(2025, 1, 30), "000132680125000017", "74067"),
    ReviewedHeadcountFact("META", date(2025, 12, 31), date(2026, 1, 29), "000132680126000011", "78865"),
    # AMZN (CIK 0001018724)
    ReviewedHeadcountFact("AMZN", date(2022, 12, 31), date(2023, 2, 3), "000101872423000004", "1541000"),
    ReviewedHeadcountFact("AMZN", date(2023, 12, 31), date(2024, 2, 2), "000101872424000008", "1525000"),
    ReviewedHeadcountFact("AMZN", date(2024, 12, 31), date(2025, 2, 7), "000101872425000004", "1556000"),
    ReviewedHeadcountFact("AMZN", date(2025, 12, 31), date(2026, 2, 6), "000101872426000004", "1576000"),
    # TSLA (CIK 0001318605)
    ReviewedHeadcountFact("TSLA", date(2022, 12, 31), date(2023, 1, 31), "000095017023001409", "127855"),
    ReviewedHeadcountFact("TSLA", date(2023, 12, 31), date(2024, 1, 29), "000162828124002390", "140473"),
    ReviewedHeadcountFact("TSLA", date(2024, 12, 31), date(2025, 1, 30), "000162828125003063", "125665"),
    ReviewedHeadcountFact("TSLA", date(2025, 12, 31), date(2026, 1, 29), "000162828126003952", "134785"),
    # AVGO (CIK 0001730168)
    ReviewedHeadcountFact("AVGO", date(2022, 10, 30), date(2022, 12, 16), "000173016822000116", "20000"),
    ReviewedHeadcountFact("AVGO", date(2023, 10, 29), date(2023, 12, 14), "000173016823000096", "20000"),
    ReviewedHeadcountFact("AVGO", date(2024, 11, 3), date(2024, 12, 20), "000173016824000130", "37000"),
    ReviewedHeadcountFact("AVGO", date(2025, 11, 2), date(2025, 12, 18), "000173016825000123", "33000"),
    # COST (CIK 0000909832)
    ReviewedHeadcountFact("COST", date(2022, 8, 28), date(2022, 10, 5), "000090983222000021", "304000"),
    ReviewedHeadcountFact("COST", date(2023, 9, 3), date(2023, 10, 11), "000090983223000042", "316000"),
    ReviewedHeadcountFact("COST", date(2024, 9, 1), date(2024, 10, 9), "000090983224000049", "333000"),
    ReviewedHeadcountFact("COST", date(2025, 8, 31), date(2025, 10, 8), "000090983225000044", "341000"),
    # NFLX (CIK 0001065280)
    ReviewedHeadcountFact("NFLX", date(2022, 12, 31), date(2023, 1, 26), "000106528023000035", "12800"),
    ReviewedHeadcountFact("NFLX", date(2023, 12, 31), date(2024, 1, 26), "000106528024000030", "13000"),
    ReviewedHeadcountFact("NFLX", date(2024, 12, 31), date(2025, 1, 27), "000106528025000044", "14000"),
    # MU (CIK 0000723125)
    ReviewedHeadcountFact("MU", date(2022, 9, 1), date(2022, 10, 7), "000072312522000090", "48000"),
    ReviewedHeadcountFact("MU", date(2023, 8, 31), date(2023, 10, 6), "000072312523000083", "43000"),
    ReviewedHeadcountFact("MU", date(2024, 8, 29), date(2024, 10, 4), "000072312524000060", "48000"),
    ReviewedHeadcountFact("MU", date(2025, 8, 28), date(2025, 10, 3), "000072312525000036", "53000"),
    # WMT (CIK 0000104169)
    ReviewedHeadcountFact("WMT", date(2023, 1, 31), date(2023, 3, 17), "000010416923000020", "2100000"),
    ReviewedHeadcountFact("WMT", date(2024, 1, 31), date(2024, 3, 15), "000010416924000056", "2100000"),
    ReviewedHeadcountFact("WMT", date(2025, 1, 31), date(2025, 3, 14), "000010416925000021", "2100000"),
    ReviewedHeadcountFact("WMT", date(2026, 1, 31), date(2026, 3, 13), "000010416926000038", "2100000"),
    # LLY (CIK 0000059478)
    ReviewedHeadcountFact("LLY", date(2022, 12, 31), date(2023, 2, 22), "000005947823000082", "39000"),
    ReviewedHeadcountFact("LLY", date(2023, 12, 31), date(2024, 2, 21), "000005947824000065", "43000"),
    ReviewedHeadcountFact("LLY", date(2024, 12, 31), date(2025, 2, 19), "000005947825000067", "47000"),
    ReviewedHeadcountFact("LLY", date(2025, 12, 31), date(2026, 2, 12), "000005947826000070", "50000"),
    # ABBV (CIK 0001551152)
    ReviewedHeadcountFact("ABBV", date(2022, 12, 31), date(2023, 2, 17), "000155115223000011", "50000"),
    ReviewedHeadcountFact("ABBV", date(2023, 12, 31), date(2024, 2, 20), "000155115224000011", "50000"),
    ReviewedHeadcountFact("ABBV", date(2024, 12, 31), date(2025, 2, 14), "000155115225000020", "55000"),
    ReviewedHeadcountFact("ABBV", date(2025, 12, 31), date(2026, 2, 18), "000155115226000014", "55000"),
    # JNJ (CIK 0000200406)
    ReviewedHeadcountFact("JNJ", date(2023, 1, 1), date(2023, 2, 16), "000020040623000016", "152700"),
    ReviewedHeadcountFact("JNJ", date(2023, 12, 31), date(2024, 2, 16), "000020040624000013", "131900"),
    ReviewedHeadcountFact("JNJ", date(2024, 12, 29), date(2025, 2, 13), "000020040625000038", "138100"),
    ReviewedHeadcountFact("JNJ", date(2025, 12, 28), date(2026, 2, 11), "000020040626000016", "138000"),
    # XOM (holdco CIK 0002115436 + predecessor CIK 0000034088)
    *_xom_facts(),
    # JPM (CIK 0000019617)
    ReviewedHeadcountFact("JPM", date(2022, 12, 31), date(2023, 2, 21), "000001961723000231", "293723"),
    ReviewedHeadcountFact("JPM", date(2023, 12, 31), date(2024, 2, 16), "000001961724000225", "309926"),
    ReviewedHeadcountFact("JPM", date(2024, 12, 31), date(2025, 2, 14), "000001961725000270", "317233"),
    ReviewedHeadcountFact("JPM", date(2025, 12, 31), date(2026, 2, 13), "000001961726000289", "318512"),
    # MA (CIK 0001141391)
    ReviewedHeadcountFact("MA", date(2022, 12, 31), date(2023, 2, 14), "000114139123000017", "29900"),
    ReviewedHeadcountFact("MA", date(2023, 12, 31), date(2024, 2, 13), "000114139124000022", "33400"),
    ReviewedHeadcountFact("MA", date(2024, 12, 31), date(2025, 2, 12), "000114139125000011", "35300"),
    ReviewedHeadcountFact("MA", date(2025, 12, 31), date(2026, 2, 12), "000114139126000018", "38700"),
    # V (CIK 0001403161)
    ReviewedHeadcountFact("V", date(2022, 9, 30), date(2022, 11, 16), "000140316122000081", "26500"),
    ReviewedHeadcountFact("V", date(2023, 9, 30), date(2023, 11, 15), "000140316123000099", "28800"),
    ReviewedHeadcountFact("V", date(2024, 9, 30), date(2024, 11, 13), "000140316124000058", "31600"),
    ReviewedHeadcountFact("V", date(2025, 9, 30), date(2025, 11, 6), "000140316125000089", "34100"),
    # BRK.B (CIK 0001067983)
    ReviewedHeadcountFact("BRK.B", date(2022, 12, 31), date(2023, 2, 27), "000095017023004451", "383000"),
    ReviewedHeadcountFact("BRK.B", date(2023, 12, 31), date(2024, 2, 26), "000095017024019719", "396500"),
    ReviewedHeadcountFact("BRK.B", date(2024, 12, 31), date(2025, 2, 24), "000095017025025210", "392400"),
    ReviewedHeadcountFact("BRK.B", date(2025, 12, 31), date(2026, 3, 2), "000095017026030306", "385500"),
    # HD (CIK 0000354950)
    ReviewedHeadcountFact("HD", date(2023, 1, 29), date(2023, 3, 15), "000035495023000059", "471600"),
    ReviewedHeadcountFact("HD", date(2024, 1, 28), date(2024, 3, 13), "000035495024000057", "463100"),
    ReviewedHeadcountFact("HD", date(2025, 2, 2), date(2025, 3, 21), "000035495025000085", "470100"),
    ReviewedHeadcountFact("HD", date(2026, 2, 1), date(2026, 3, 18), "000035495026000052", "470000"),
    # BKNG (CIK 0001075531)
    ReviewedHeadcountFact("BKNG", date(2022, 12, 31), date(2023, 2, 23), "000107553123000009", "21600"),
    ReviewedHeadcountFact("BKNG", date(2023, 12, 31), date(2024, 2, 22), "000107553124000016", "23600"),
    ReviewedHeadcountFact("BKNG", date(2024, 12, 31), date(2025, 2, 20), "000107553125000011", "24300"),
    ReviewedHeadcountFact("BKNG", date(2025, 12, 31), date(2026, 2, 18), "000107553126000009", "23572"),
)


def _latest_by_ticker() -> dict[str, str]:
    latest: dict[str, ReviewedHeadcountFact] = {}
    for fact in REVIEWED_HEADCOUNT_FACTS:
        validate_cited_headcount_fact(
            knowable_at=fact.knowable_at,
            period_end=fact.period_end,
            evidence_ref=fact.evidence_ref,
        )
        prior = latest.get(fact.ticker)
        if prior is None or fact.filing_date >= prior.filing_date:
            latest[fact.ticker] = fact
    out = {ticker: fact.headcount for ticker, fact in latest.items()}
    if "GOOGL" in out:
        out["GOOG"] = out["GOOGL"]
    return out


#: Derived latest-by-ticker view for callers that read one figure per symbol.
REVIEWED_HEADCOUNTS: dict[str, str] = _latest_by_ticker()


def _sec_ticker(ticker: str) -> str:
    return ticker.replace(".", "-")


def _already_seeded(connection: psycopg.Connection, cik: int, accession: str) -> bool:
    row = connection.execute(
        """
        select 1
        from staging.issuer_headcount_facts
        where cik = %s
          and source = %s
          and evidence_ref like %s
        limit 1
        """,
        (cik, _SOURCE, f"accession={accession} %"),
    ).fetchone()
    return row is not None


def seed_reviewed_headcounts(
    connection: psycopg.Connection,
    *,
    ciks_by_ticker: dict[str, int] | None = None,
) -> int:
    """Seed all reviewed 10-K headcount facts idempotently (#1264)."""
    index = ciks_by_ticker if ciks_by_ticker is not None else sec.ticker_cik_index()
    written = 0
    for fact in REVIEWED_HEADCOUNT_FACTS:
        validate_cited_headcount_fact(
            knowable_at=fact.knowable_at,
            period_end=fact.period_end,
            evidence_ref=fact.evidence_ref,
        )
        cik = fact.cik_override if fact.cik_override is not None else index.get(_sec_ticker(fact.ticker))
        if cik is None or _already_seeded(connection, cik, fact.accession):
            continue
        record_headcount(
            connection,
            cik=cik,
            headcount=Decimal(fact.headcount),
            knowable_at=fact.knowable_at,
            period_end=fact.period_end,
            source=_SOURCE,
            evidence_ref=fact.evidence_ref,
            confidence=_CONFIDENCE,
        )
        written += 1
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="resolve CIKs and print, write nothing")
    args = parser.parse_args()

    index = sec.ticker_cik_index()
    resolved: list[tuple[ReviewedHeadcountFact, int]] = []
    for fact in REVIEWED_HEADCOUNT_FACTS:
        validate_cited_headcount_fact(
            knowable_at=fact.knowable_at,
            period_end=fact.period_end,
            evidence_ref=fact.evidence_ref,
        )
        cik = fact.cik_override if fact.cik_override is not None else index.get(_sec_ticker(fact.ticker))
        if cik is None:
            print(f"  {fact.ticker}: no SEC CIK mapping, skipped")
            continue
        resolved.append((fact, cik))

    if args.dry_run:
        for fact, cik in resolved:
            print(
                f"  {fact.ticker:<6} CIK{cik:010d}  period_end={fact.period_end.isoformat()}"
                f"  filed={fact.filing_date.isoformat()}  accession={fact.accession}  {fact.headcount}"
            )
        print(f"{len(resolved)} facts would be written")
        return 0

    written = 0
    with psycopg.connect(settings.database_url) as connection:
        for fact, cik in resolved:
            if _already_seeded(connection, cik, fact.accession):
                continue
            fact_id = record_headcount(
                connection,
                cik=cik,
                headcount=Decimal(fact.headcount),
                knowable_at=fact.knowable_at,
                period_end=fact.period_end,
                source=_SOURCE,
                evidence_ref=fact.evidence_ref,
                confidence=_CONFIDENCE,
            )
            written += 1
            print(
                f"  {fact.ticker:<6} CIK{cik:010d}  {fact.filing_date.isoformat()}  {fact.headcount}"
                f"  -> staging.issuer_headcount_facts:{fact_id}"
            )
        connection.commit()
    print(f"{written} headcount facts recorded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
