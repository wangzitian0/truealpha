"""Point-in-time issuer headcount fact lookups and writes."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from psycopg import Connection
from truealpha_contracts import reconciliation_policies

from data_engine.datahub.production_topt.sec_financial_adapter import HeadcountFact

_ACCESSION_RE = re.compile(r"(?:^|\s)accession=(?:\d{18}|\d{10}-\d{2}-\d{6}|\d{4}-\d{2}-\d{6})(?:\s|$)")
_STRICT_18_ACCESSION_RE = re.compile(r"(?:^|\s)accession=(?:\d{18}|\d{10}-\d{2}-\d{6})(?:\s|$)")
_FILED_RE = re.compile(r"(?:^|\s)filed=(\d{4}-\d{2}-\d{2})(?:\s|$)")

_SELECT_HEADCOUNT_SQL = """
select headcount, knowable_at, source, evidence_ref, period_end, confidence
from staging.issuer_headcount_facts
where cik = %s and knowable_at <= %s
order by array_position(%s::text[], source) nulls last, knowable_at desc, id desc
"""


def validate_cited_headcount_fact(
    *,
    knowable_at: datetime,
    period_end: date | None,
    evidence_ref: str,
    require_period_end: bool = True,
    allow_short_cik_accession: bool = False,
) -> None:
    accession_re = _ACCESSION_RE if allow_short_cik_accession else _STRICT_18_ACCESSION_RE
    if accession_re.search(evidence_ref) is None:
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


def is_eligible_headcount_row(
    *,
    knowable_at: datetime,
    source: str,
    evidence_ref: str,
    period_end: date | None,
) -> bool:
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


def lookup_headcount(connection: Connection[Any], cik: int, cutoff: date) -> HeadcountFact | None:
    boundary = datetime.combine(cutoff, datetime.max.time(), tzinfo=UTC)
    params = (cik, boundary, list(reconciliation_policies.HEADCOUNT_RECONCILIATION_POLICY.source_priority))
    row = connection.execute(f"{_SELECT_HEADCOUNT_SQL} limit 1", params).fetchone()
    if row is None:
        return None
    if not is_eligible_headcount_row(
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
                if is_eligible_headcount_row(
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
