"""Point-in-time semantics of the headcount fact plane (#70).

The value of moving headcount out of a dict is not that it is now in a table — it is that
corrections become insertions and a replay of an old cutoff is unaffected by them. These
tests assert that property, because it is the one a well-meaning future "just upsert the
latest figure" change would quietly destroy.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from decimal import Decimal

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.production_topt.headcount import PostgresHeadcountExtractor, record_headcount

_CIK = 999000


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        active.execute("select 1")
        yield active
    finally:
        active.rollback()
        active.close()


def _land(connection, cik: int, value: str, knowable: datetime, source: str = "test") -> int:
    return record_headcount(
        connection,
        cik=cik,
        headcount=Decimal(value),
        knowable_at=knowable,
        source=source,
        evidence_ref="test-evidence",
        confidence=Decimal("0.7"),
    )


def test_an_issuer_with_no_fact_is_an_honest_gap(connection) -> None:
    """None, never a default. A borrowed or invented denominator turns
    `missing_headcount` into a wrong GPPE, which is strictly worse than a stated gap."""
    assert PostgresHeadcountExtractor(connection)(_CIK + 1, date(2026, 7, 27)) is None


def test_a_correction_supersedes_without_rewriting_history(connection) -> None:
    """The whole reason the table is append-only.

    A figure corrected today must not change what a replay of an earlier cutoff resolves —
    otherwise every historical run silently re-scores whenever someone fixes a number.
    """
    _land(connection, _CIK, "20000", datetime(2026, 1, 1, tzinfo=UTC))
    _land(connection, _CIK, "37000", datetime(2026, 6, 1, tzinfo=UTC), source="10k-extraction")
    extractor = PostgresHeadcountExtractor(connection)

    today = extractor(_CIK, date(2026, 7, 27))
    assert today is not None and today.value == Decimal("37000")

    replay = extractor(_CIK, date(2026, 3, 31))
    assert replay is not None
    assert replay.value == Decimal("20000"), "an old cutoff must still see what was knowable then"


def test_a_fact_knowable_after_the_cutoff_is_invisible(connection) -> None:
    _land(connection, _CIK, "50000", datetime(2026, 9, 1, tzinfo=UTC))
    assert PostgresHeadcountExtractor(connection)(_CIK, date(2026, 7, 27)) is None


def test_the_extractor_reports_the_knowable_time_it_resolved(connection) -> None:
    """The adapter uses this to stamp the record's transaction time, so a headcount that
    silently claimed the cutoff would misdate every fact it enriched."""
    _land(connection, _CIK, "164000", datetime(2026, 2, 3, tzinfo=UTC))
    fact = PostgresHeadcountExtractor(connection)(_CIK, date(2026, 7, 27))
    assert fact is not None
    assert fact.knowable_at == datetime(2026, 2, 3, tzinfo=UTC)


def test_history_cannot_be_edited_away(connection) -> None:
    """Append-only is enforced by the database, not by convention — a producer that
    "corrects" by UPDATE would destroy the replay property above."""
    fact_id = _land(connection, _CIK, "1000", datetime(2026, 1, 1, tzinfo=UTC))
    with pytest.raises(psycopg.errors.RaiseException):
        connection.execute("update staging.issuer_headcount_facts set headcount = 2 where id = %s", (fact_id,))
    connection.rollback()
    fact_id = _land(connection, _CIK, "1000", datetime(2026, 1, 1, tzinfo=UTC))
    with pytest.raises(psycopg.errors.RaiseException):
        connection.execute("delete from staging.issuer_headcount_facts where id = %s", (fact_id,))


def test_a_nonpositive_headcount_is_rejected_at_the_boundary(connection) -> None:
    """A zero denominator is not a small company, it is a parse failure."""
    with pytest.raises(psycopg.errors.CheckViolation):
        _land(connection, _CIK, "0", datetime(2026, 1, 1, tzinfo=UTC))
