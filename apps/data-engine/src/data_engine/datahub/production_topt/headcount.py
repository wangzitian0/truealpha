"""Headcount behind the `HeadcountExtractor` port, read from the PIT fact table (#70).

Employee headcount is the GPPE denominator and it is not an XBRL concept: SEC
company-facts publishes no employee count for any issuer (every `Employee*` concept there
belongs to share-based compensation), so the number exists only as prose in a 10-K. That
makes it an extraction problem, not a mapping problem — and extraction needs judgement.

Judgement runs on a slower plane than the daily tick. `staging.issuer_headcount_facts` is
the seam: low-frequency producers write facts there (a reviewed manual entry today, #70's
text extraction later, a vendor feed), each carrying its own source, evidence pointer and
confidence; the high-frequency capture only reads. Adding issuers is an insert, never a
deploy — which is what makes a larger universe a data change rather than a code change.

This module used to hold the 21 figures as a dict literal. Those values now live in
`scripts/seed_issuer_headcounts.py` as a reviewed, evidence-carrying seed, so the code no
longer decides what any company's headcount is.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from psycopg import Connection

from data_engine.datahub.production_topt.sec_financial_adapter import HeadcountFact


class PostgresHeadcountExtractor:
    """`HeadcountExtractor` over the PIT fact table.

    Point-in-time by construction: only facts knowable at or before the cutoff are
    eligible, and among those the latest knowable one wins. A correction landed today
    therefore does not rewrite what a replay of last month's cutoff sees — the older run
    still resolves the figure that was knowable then, which is the whole reason the table
    is append-only rather than upserted.

    An issuer with no eligible row returns None, so the factor reports `missing_headcount`
    honestly rather than borrowing another issuer's figure or a stale default.
    """

    def __init__(self, connection: Connection[Any]) -> None:
        self._connection = connection

    def __call__(self, cik: int, cutoff: date) -> HeadcountFact | None:
        boundary = datetime.combine(cutoff, datetime.max.time(), tzinfo=UTC)
        row = self._connection.execute(
            """
            select headcount, knowable_at
            from staging.issuer_headcount_facts
            where cik = %s and knowable_at <= %s
            order by knowable_at desc, id desc
            limit 1
            """,
            (cik, boundary),
        ).fetchone()
        if row is None:
            return None
        return HeadcountFact(value=Decimal(row[0]), knowable_at=row[1])


def record_headcount(
    connection: Connection[Any],
    *,
    cik: int,
    headcount: Decimal,
    knowable_at: datetime,
    source: str,
    evidence_ref: str,
    confidence: Decimal,
    period_end: date | None = None,
) -> int:
    """Land one headcount fact. The only write path, shared by every producer.

    Deliberately not an upsert: a corrected figure is a NEW row with a later
    `knowable_at`, so history stays readable and a replay of an older cutoff is unaffected.
    """
    row = connection.execute(
        """
        insert into staging.issuer_headcount_facts
            (cik, headcount, knowable_at, period_end, source, evidence_ref, confidence)
        values (%s, %s, %s, %s, %s, %s, %s)
        returning id
        """,
        (cik, headcount, knowable_at, period_end, source, evidence_ref, confidence),
    ).fetchone()
    assert row is not None
    return int(row[0])
