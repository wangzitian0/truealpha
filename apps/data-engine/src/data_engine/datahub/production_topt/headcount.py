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

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from psycopg import Connection
from truealpha_contracts import reconciliation_policies

from data_engine.datahub.production_topt.sec_financial_adapter import HeadcountFact

# The source order of the headcount plane is `reconciliation_policies.HEADCOUNT_RECONCILIATION_POLICY`
# (rule 12). Each function reads the policy through the module, so one edit there reaches this reader.

_ACCESSION_RE = re.compile(r"(?:^|\s)accession=(?:\d{18}|\d{10}-\d{2}-\d{6}|\d{4}-\d{2}-\d{6})(?:\s|$)")
_STRICT_18_ACCESSION_RE = re.compile(r"(?:^|\s)accession=(?:\d{18}|\d{10}-\d{2}-\d{6})(?:\s|$)")
_FILED_RE = re.compile(r"(?:^|\s)filed=(\d{4}-\d{2}-\d{2})(?:\s|$)")


def validate_cited_headcount_fact(
    *,
    knowable_at: datetime,
    period_end: date | None,
    evidence_ref: str,
    require_period_end: bool = True,
    allow_short_cik_accession: bool = False,
) -> None:
    """Reject uncited or misdated headcount facts (#1264).

    Every headcount fact must cite an 18-digit SEC accession and filing date in
    `evidence_ref`, carry a fiscal `period_end` on or before that filing date, and have
    `knowable_at` anchored to the filing date (never earlier than the filing it cites, and
    never stamped to a literal constant).
    """
    accession_re = _ACCESSION_RE if allow_short_cik_accession else _STRICT_18_ACCESSION_RE
    accession_match = accession_re.search(evidence_ref)
    if accession_match is None:
        raise ValueError(f"headcount evidence_ref does not cite an 18-digit SEC accession: {evidence_ref!r}")
    filed_match = _FILED_RE.search(evidence_ref)
    if filed_match is None:
        raise ValueError(f"headcount evidence_ref does not cite a filing date (filed=YYYY-MM-DD): {evidence_ref!r}")
    filed_date = date.fromisoformat(filed_match.group(1))
    knowable_date = knowable_at.astimezone(UTC).date()
    if knowable_date < filed_date:
        raise ValueError(
            f"headcount knowable_at ({knowable_date.isoformat()}) precedes cited filing date ({filed_date.isoformat()})"
        )
    if knowable_date != filed_date:
        raise ValueError(
            f"headcount knowable_at ({knowable_date.isoformat()}) does not match cited filing date ({filed_date.isoformat()})"
        )
    if require_period_end and period_end is None:
        raise ValueError("headcount period_end must be set to the fiscal period end of the cited filing")
    if period_end is not None and period_end > filed_date:
        raise ValueError(
            f"headcount period_end ({period_end.isoformat()}) is after cited filing date ({filed_date.isoformat()})"
        )


def _is_eligible_headcount_row(
    *,
    knowable_at: datetime,
    source: str,
    evidence_ref: str,
    period_end: date | None,
) -> bool:
    """True when a stored row satisfies the active-source SEC accession contract (#1264).

    Legacy uncited rows (`#70 supersedes` stamped at `2026-01-01` or retired
    `curated-10k-2025` rows) remain in the append-only table by trigger design and are
    excluded at read time so they never outrank cited 10-K filings.
    """
    if source == "curated-10k-2025":
        return False
    if source in reconciliation_policies.HEADCOUNT_RECONCILIATION_POLICY.source_priority:
        accession_re = _ACCESSION_RE if source == "10k-extraction" else _STRICT_18_ACCESSION_RE
        if accession_re.search(evidence_ref) is None or _FILED_RE.search(evidence_ref) is None:
            return False
        if source == "manual-review":
            try:
                validate_cited_headcount_fact(
                    knowable_at=knowable_at,
                    period_end=period_end,
                    evidence_ref=evidence_ref,
                    require_period_end=True,
                    allow_short_cik_accession=False,
                )
            except ValueError:
                return False
    return True


_SELECT_HEADCOUNT_SQL = """
select headcount, knowable_at, source, evidence_ref, period_end, confidence
from staging.issuer_headcount_facts
where cik = %s and knowable_at <= %s
order by array_position(%s::text[], source) nulls last, knowable_at desc, id desc
"""


def lookup_headcount(connection: Connection[Any], cik: int, cutoff: date) -> HeadcountFact | None:
    """Resolve the point-in-time headcount fact for `cik` at `cutoff` (#70, #1264).

    Orders by the `source_priority` of `HEADCOUNT_RECONCILIATION_POLICY` first and `knowable_at desc` second,
    filtering out retired `curated-10k-2025` rows and uncited legacy `manual-review` rows
    that lack an 18-digit SEC accession.
    """
    boundary = datetime.combine(cutoff, datetime.max.time(), tzinfo=UTC)
    params = (cik, boundary, list(reconciliation_policies.HEADCOUNT_RECONCILIATION_POLICY.source_priority))
    row = connection.execute(f"{_SELECT_HEADCOUNT_SQL} limit 1", params).fetchone()
    if row is None:
        return None
    if not _is_eligible_headcount_row(
        knowable_at=row[1],
        source=str(row[2]),
        evidence_ref=str(row[3]),
        period_end=row[4],
    ):
        rows = connection.execute(_SELECT_HEADCOUNT_SQL, params).fetchall()
        row = next(
            (
                candidate
                for candidate in rows
                if _is_eligible_headcount_row(
                    knowable_at=candidate[1],
                    source=str(candidate[2]),
                    evidence_ref=str(candidate[3]),
                    period_end=candidate[4],
                )
            ),
            None,
        )
        if row is None:
            return None
    return HeadcountFact(
        value=Decimal(row[0]),
        knowable_at=row[1],
        source=row[2],
        evidence_ref=row[3],
        period_end=row[4],
        confidence=None if row[5] is None else Decimal(row[5]),
    )


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
        return lookup_headcount(self._connection, cik, cutoff)


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
    if source == "curated-10k-2025":
        raise ValueError("headcount source 'curated-10k-2025' is retired; cite an 18-digit SEC accession (#1264)")
    if source in reconciliation_policies.HEADCOUNT_RECONCILIATION_POLICY.source_priority:
        validate_cited_headcount_fact(
            knowable_at=knowable_at,
            period_end=period_end,
            evidence_ref=evidence_ref,
            require_period_end=(source == "manual-review"),
            allow_short_cik_accession=(source == "10k-extraction"),
        )
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
