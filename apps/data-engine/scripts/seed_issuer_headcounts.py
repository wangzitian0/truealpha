"""Seed reviewed 10-K headcount facts with real SEC filing dates and accessions."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import psycopg
from data_engine.config import settings
from data_engine.datahub.production_topt.headcount import record_headcount, validate_cited_headcount_fact
from data_engine.datahub.production_topt.source_registrations import AsOfAxis, knowable_instant
from data_engine.sources import gateway, sec

_SOURCE = "manual-review"
_CONFIDENCE = Decimal("0.70")


@dataclass(frozen=True)
class ReviewedHeadcountFact:
    ticker: str
    period_end: date
    filing_date: date
    accession: str
    headcount: str
    cik_override: int | None = None

    @property
    def knowable_at(self) -> datetime:
        return knowable_instant(AsOfAxis.ACCESSION, self.filing_date)

    @property
    def evidence_ref(self) -> str:
        return f"accession={self.accession} form=10-K filed={self.filing_date.isoformat()} source={_SOURCE}"


def _rows(ticker: str, *items: tuple[str, str, str, str], dual_cik: int | None = None) -> list[ReviewedHeadcountFact]:
    out: list[ReviewedHeadcountFact] = []
    for p_end, f_date, accn, hc in items:
        pe, fd = date.fromisoformat(p_end), date.fromisoformat(f_date)
        out.append(ReviewedHeadcountFact(ticker, pe, fd, accn, hc))
        if dual_cik is not None:
            out.append(ReviewedHeadcountFact(ticker, pe, fd, accn, hc, cik_override=dual_cik))
    return out


REVIEWED_HEADCOUNT_FACTS: tuple[ReviewedHeadcountFact, ...] = tuple(
    [
        *_rows(
            "AAPL",
            ("2022-09-24", "2022-10-28", "000032019322000108", "164000"),
            ("2023-09-30", "2023-11-03", "000032019323000106", "161000"),
            ("2024-09-28", "2024-11-01", "000032019324000123", "164000"),
            ("2025-09-27", "2025-10-31", "000032019325000079", "166000"),
        ),
        *_rows(
            "MSFT",
            ("2022-06-30", "2022-07-28", "000156459022026876", "221000"),
            ("2023-06-30", "2023-07-27", "000095017023035122", "221000"),
            ("2024-06-30", "2024-07-30", "000095017024087843", "228000"),
            ("2025-06-30", "2025-07-30", "000095017025100235", "228000"),
        ),
        *_rows(
            "GOOGL",
            ("2022-12-31", "2023-02-03", "000165204423000016", "190234"),
            ("2023-12-31", "2024-01-31", "000165204424000022", "182502"),
            ("2024-12-31", "2025-02-05", "000165204425000014", "183323"),
            ("2025-12-31", "2026-02-05", "000165204426000018", "190820"),
        ),
        *_rows(
            "NVDA",
            ("2023-01-29", "2023-02-24", "000104581023000017", "26196"),
            ("2024-01-28", "2024-02-21", "000104581024000029", "29600"),
            ("2025-01-26", "2025-02-26", "000104581025000023", "36000"),
            ("2026-01-25", "2026-02-25", "000104581026000021", "42000"),
        ),
        *_rows(
            "META",
            ("2022-12-31", "2023-02-02", "000132680123000013", "86482"),
            ("2023-12-31", "2024-02-02", "000132680124000012", "67317"),
            ("2024-12-31", "2025-01-30", "000132680125000017", "74067"),
            ("2025-12-31", "2026-01-29", "000132680126000011", "78865"),
        ),
        *_rows(
            "AMZN",
            ("2022-12-31", "2023-02-03", "000101872423000004", "1541000"),
            ("2023-12-31", "2024-02-02", "000101872424000008", "1525000"),
            ("2024-12-31", "2025-02-07", "000101872425000004", "1556000"),
            ("2025-12-31", "2026-02-06", "000101872426000004", "1576000"),
        ),
        *_rows(
            "TSLA",
            ("2022-12-31", "2023-01-31", "000095017023001409", "127855"),
            ("2023-12-31", "2024-01-29", "000162828124002390", "140473"),
            ("2024-12-31", "2025-01-30", "000162828125003063", "125665"),
            ("2025-12-31", "2026-01-29", "000162828126003952", "134785"),
        ),
        *_rows(
            "AVGO",
            ("2022-10-30", "2022-12-16", "000173016822000116", "20000"),
            ("2023-10-29", "2023-12-14", "000173016823000096", "20000"),
            ("2024-11-03", "2024-12-20", "000173016824000130", "37000"),
            ("2025-11-02", "2025-12-18", "000173016825000123", "33000"),
        ),
        *_rows(
            "COST",
            ("2022-08-28", "2022-10-05", "000090983222000021", "304000"),
            ("2023-09-03", "2023-10-11", "000090983223000042", "316000"),
            ("2024-09-01", "2024-10-09", "000090983224000049", "333000"),
            ("2025-08-31", "2025-10-08", "000090983225000044", "341000"),
        ),
        *_rows(
            "NFLX",
            ("2022-12-31", "2023-01-26", "000106528023000035", "12800"),
            ("2023-12-31", "2024-01-26", "000106528024000030", "13000"),
            ("2024-12-31", "2025-01-27", "000106528025000044", "14000"),
        ),
        *_rows(
            "MU",
            ("2022-09-01", "2022-10-07", "000072312522000090", "48000"),
            ("2023-08-31", "2023-10-06", "000072312523000083", "43000"),
            ("2024-08-29", "2024-10-04", "000072312524000060", "48000"),
            ("2025-08-28", "2025-10-03", "000072312525000036", "53000"),
        ),
        *_rows(
            "WMT",
            ("2023-01-31", "2023-03-17", "000010416923000020", "2100000"),
            ("2024-01-31", "2024-03-15", "000010416924000056", "2100000"),
            ("2025-01-31", "2025-03-14", "000010416925000021", "2100000"),
            ("2026-01-31", "2026-03-13", "000010416926000038", "2100000"),
        ),
        *_rows(
            "LLY",
            ("2022-12-31", "2023-02-22", "000005947823000082", "39000"),
            ("2023-12-31", "2024-02-21", "000005947824000065", "43000"),
            ("2024-12-31", "2025-02-19", "000005947825000067", "47000"),
            ("2025-12-31", "2026-02-12", "000005947826000070", "50000"),
        ),
        *_rows(
            "ABBV",
            ("2022-12-31", "2023-02-17", "000155115223000011", "50000"),
            ("2023-12-31", "2024-02-20", "000155115224000011", "50000"),
            ("2024-12-31", "2025-02-14", "000155115225000020", "55000"),
            ("2025-12-31", "2026-02-18", "000155115226000014", "55000"),
        ),
        *_rows(
            "JNJ",
            ("2023-01-01", "2023-02-16", "000020040623000016", "152700"),
            ("2023-12-31", "2024-02-16", "000020040624000013", "131900"),
            ("2024-12-29", "2025-02-13", "000020040625000038", "138100"),
            ("2025-12-28", "2026-02-11", "000020040626000016", "138000"),
        ),
        *_rows(
            "XOM",
            ("2022-12-31", "2023-02-22", "000003408823000020", "62000"),
            ("2023-12-31", "2024-02-28", "000003408824000018", "62000"),
            ("2024-12-31", "2025-02-19", "000003408825000010", "61000"),
            ("2025-12-31", "2026-02-18", "000003408826000017", "58000"),
            dual_cik=34088,
        ),
        *_rows(
            "JPM",
            ("2022-12-31", "2023-02-21", "000001961723000231", "293723"),
            ("2023-12-31", "2024-02-16", "000001961724000225", "309926"),
            ("2024-12-31", "2025-02-14", "000001961725000270", "317233"),
            ("2025-12-31", "2026-02-13", "000001961726000289", "318512"),
        ),
        *_rows(
            "MA",
            ("2022-12-31", "2023-02-14", "000114139123000017", "29900"),
            ("2023-12-31", "2024-02-13", "000114139124000022", "33400"),
            ("2024-12-31", "2025-02-12", "000114139125000011", "35300"),
            ("2025-12-31", "2026-02-12", "000114139126000018", "38700"),
        ),
        *_rows(
            "V",
            ("2022-09-30", "2022-11-16", "000140316122000081", "26500"),
            ("2023-09-30", "2023-11-15", "000140316123000099", "28800"),
            ("2024-09-30", "2024-11-13", "000140316124000058", "31600"),
            ("2025-09-30", "2025-11-06", "000140316125000089", "34100"),
        ),
        *_rows(
            "BRK.B",
            ("2022-12-31", "2023-02-27", "000095017023004451", "383000"),
            ("2023-12-31", "2024-02-26", "000095017024019719", "396500"),
            ("2024-12-31", "2025-02-24", "000095017025025210", "392400"),
            ("2025-12-31", "2026-03-02", "000095017026030306", "385500"),
        ),
        *_rows(
            "HD",
            ("2023-01-29", "2023-03-15", "000035495023000059", "471600"),
            ("2024-01-28", "2024-03-13", "000035495024000057", "463100"),
            ("2025-02-02", "2025-03-21", "000035495025000085", "470100"),
            ("2026-02-01", "2026-03-18", "000035495026000052", "470000"),
        ),
        *_rows(
            "BKNG",
            ("2022-12-31", "2023-02-23", "000107553123000009", "21600"),
            ("2023-12-31", "2024-02-22", "000107553124000016", "23600"),
            ("2024-12-31", "2025-02-20", "000107553125000011", "24300"),
            ("2025-12-31", "2026-02-18", "000107553126000009", "23572"),
        ),
    ]
)


def _sec_ticker(ticker: str) -> str:
    return ticker.replace(".", "-")


def _resolve_reviewed_facts(
    ciks_by_ticker: dict[str, int] | None = None, *, log_skipped: bool = False
) -> list[tuple[ReviewedHeadcountFact, int]]:
    index = ciks_by_ticker if ciks_by_ticker is not None else sec.ticker_cik_index()
    resolved: list[tuple[ReviewedHeadcountFact, int]] = []
    for fact in REVIEWED_HEADCOUNT_FACTS:
        validate_cited_headcount_fact(
            knowable_at=fact.knowable_at, period_end=fact.period_end, evidence_ref=fact.evidence_ref
        )
        cik = fact.cik_override if fact.cik_override is not None else index.get(_sec_ticker(fact.ticker))
        if cik is None:
            if log_skipped:
                print(f"  {fact.ticker}: no SEC CIK mapping, skipped")
            continue
        resolved.append((fact, cik))
    return resolved


def _already_seeded(connection: psycopg.Connection, cik: int, accession: str) -> bool:
    row = connection.execute(
        "select 1 from staging.issuer_headcount_facts where cik = %s and source = %s and evidence_ref like %s limit 1",
        (cik, _SOURCE, f"accession={accession} %"),
    ).fetchone()
    return row is not None


def seed_reviewed_headcounts(
    connection: psycopg.Connection, *, ciks_by_ticker: dict[str, int] | None = None, verbose: bool = False
) -> int:
    written = 0
    for fact, cik in _resolve_reviewed_facts(ciks_by_ticker, log_skipped=verbose):
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
        if verbose:
            print(f"  {fact.ticker:<6} CIK{cik:010d}  {fact.filing_date.isoformat()}  {fact.headcount}  -> {fact_id}")
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        resolved = _resolve_reviewed_facts(log_skipped=True)
        print(f"{len(resolved)} facts would be written")
        return 0
    with psycopg.connect(settings.database_url) as connection:
        written = seed_reviewed_headcounts(connection, verbose=True)
        connection.commit()
    print(f"{written} headcount facts recorded")
    return 0


if __name__ == "__main__":
    with gateway.script_run("seed_issuer_headcounts"):
        raise SystemExit(main())
