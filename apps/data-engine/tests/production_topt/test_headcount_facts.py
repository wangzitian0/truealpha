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
    filed_iso = knowable.astimezone(UTC).date().isoformat()
    return record_headcount(
        connection,
        cik=cik,
        headcount=Decimal(value),
        knowable_at=knowable,
        period_end=knowable.astimezone(UTC).date(),
        source=source,
        evidence_ref=f"accession=000099900026000001 form=10-K filed={filed_iso}",
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


def test_validate_cited_headcount_fact_rejects_legacy_2026_01_01_and_uncited_rows() -> None:
    """#1264 acceptance check: fails against the 21 legacy `2026-01-01` uncited rows and
    any row whose `knowable_at` does not match its cited filing date."""
    from data_engine.datahub.production_topt.headcount import validate_cited_headcount_fact

    legacy_evidence = "10-K human capital disclosure; manual entry, no verified accession (#70 supersedes)"
    with pytest.raises(ValueError, match="18-digit SEC accession"):
        validate_cited_headcount_fact(
            knowable_at=datetime(2026, 1, 1, tzinfo=UTC),
            period_end=None,
            evidence_ref=legacy_evidence,
        )
    with pytest.raises(ValueError, match="18-digit SEC accession"):
        record_headcount(
            None,  # type: ignore[arg-type]
            cik=_CIK,
            headcount=Decimal("1000"),
            knowable_at=datetime(2026, 1, 1, tzinfo=UTC),
            source="manual-review",
            evidence_ref=legacy_evidence,
            confidence=Decimal("0.50"),
        )

    with pytest.raises(ValueError, match="precedes cited filing date"):
        validate_cited_headcount_fact(
            knowable_at=datetime(2024, 1, 1, tzinfo=UTC),
            period_end=date(2023, 12, 31),
            evidence_ref="accession=000162828124002390 form=10-K filed=2024-01-29 source=manual-review",
        )

    with pytest.raises(ValueError, match="does not match cited filing date"):
        validate_cited_headcount_fact(
            knowable_at=datetime(2026, 1, 1, tzinfo=UTC),
            period_end=date(2023, 12, 31),
            evidence_ref="accession=000162828124002390 form=10-K filed=2024-01-29 source=manual-review",
        )

    with pytest.raises(ValueError, match="period_end must be set"):
        validate_cited_headcount_fact(
            knowable_at=datetime(2024, 1, 29, tzinfo=UTC),
            period_end=None,
            evidence_ref="accession=000162828124002390 form=10-K filed=2024-01-29 source=manual-review",
        )


def test_seed_issuer_headcounts_carries_real_sec_accessions_across_backtest_window() -> None:
    """#1264: every row in `seed_issuer_headcounts.py` cites a real 10-K accession and
    filing date, and covers every TOPT issuer across the 2023-2026 backtest window."""
    import importlib.util
    import sys
    from pathlib import Path

    from data_engine.datahub.production_topt.headcount import validate_cited_headcount_fact
    from topt_symbols import TOPT_SYMBOLS

    script_path = Path(__file__).resolve().parents[2] / "scripts" / "seed_issuer_headcounts.py"
    spec = importlib.util.spec_from_file_location("seed_issuer_headcounts", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["seed_issuer_headcounts"] = module
    spec.loader.exec_module(module)

    facts = module.REVIEWED_HEADCOUNT_FACTS
    assert len(facts) >= 80
    for fact in facts:
        validate_cited_headcount_fact(
            knowable_at=fact.knowable_at,
            period_end=fact.period_end,
            evidence_ref=fact.evidence_ref,
        )
        assert fact.knowable_at != datetime(2026, 1, 1, tzinfo=UTC)

    by_ticker: dict[str, list] = {}
    for fact in facts:
        by_ticker.setdefault(fact.ticker, []).append(fact)

    earliest_backtest_cutoff = datetime(2023, 4, 28, 23, 59, 59, tzinfo=UTC)
    for symbol in TOPT_SYMBOLS:
        assert symbol in by_ticker, f"missing reviewed headcount facts for {symbol}"
        eligible = [fact for fact in by_ticker[symbol] if fact.knowable_at <= earliest_backtest_cutoff]
        assert eligible, f"{symbol} has no headcount fact knowable on or before {earliest_backtest_cutoff.date()}"
