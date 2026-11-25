"""Headcount PIT lookup, active source filtering, and 18-digit SEC accession enforcement (#1264).

Every active headcount source (`HEADCOUNT_RECONCILIATION_POLICY.source_priority`) must cite an 18-digit SEC
accession and filing date on write and on read. Retired `'curated-10k-2025'` rows and
legacy uncited `2026-01-01` `manual-review` rows remain in the append-only table by
trigger design and are filtered out by `lookup_headcount(connection, cik, cutoff)` so a
constant-stamped row can never outrank a cited 10-K filing.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from data_engine.datahub.capture_core.headcount import (
    PostgresHeadcountExtractor,
    lookup_headcount,
    record_headcount,
)
from truealpha_runtime.reconciliation_policies import HEADCOUNT_RECONCILIATION_POLICY

_CIK = 9991264


def test_active_headcount_sources_excludes_curated_10k_2025() -> None:
    """#1264: `'curated-10k-2025'` is not a source of the headcount policy."""
    active = HEADCOUNT_RECONCILIATION_POLICY.source_priority
    assert "curated-10k-2025" not in active
    assert set(active) == {"10k-extraction", "manual-review"}


def test_record_headcount_enforces_18_digit_sec_accession_for_active_sources() -> None:
    """#1264: `record_headcount` rejects retired `'curated-10k-2025'` and any active-source
    row lacking an 18-digit SEC accession or matching `filed=YYYY-MM-DD`."""
    with pytest.raises(ValueError, match="curated-10k-2025"):
        record_headcount(
            None,  # type: ignore[arg-type]
            cik=_CIK,
            headcount=Decimal("34100"),
            knowable_at=datetime(2025, 11, 6, tzinfo=UTC),
            period_end=date(2025, 9, 30),
            source="curated-10k-2025",
            evidence_ref="accession=000140316125000089 form=10-K filed=2025-11-06",
            confidence=Decimal("0.70"),
        )

    for source in HEADCOUNT_RECONCILIATION_POLICY.source_priority:
        with pytest.raises(ValueError, match="18-digit SEC accession"):
            record_headcount(
                None,  # type: ignore[arg-type]
                cik=_CIK,
                headcount=Decimal("34100"),
                knowable_at=datetime(2026, 1, 1, tzinfo=UTC),
                period_end=date(2025, 9, 30),
                source=source,
                evidence_ref="10-K human capital disclosure; manual entry, no verified accession (#70 supersedes)",
                confidence=Decimal("0.50"),
            )

        with pytest.raises(ValueError, match="18-digit SEC accession"):
            record_headcount(
                None,  # type: ignore[arg-type]
                cik=_CIK,
                headcount=Decimal("34100"),
                knowable_at=datetime(2025, 11, 6, tzinfo=UTC),
                period_end=date(2025, 9, 30),
                source=source,
                evidence_ref="accession=12345 form=10-K filed=2025-11-06",
                confidence=Decimal("0.70"),
            )


def test_lookup_headcount_ignores_legacy_uncited_2026_01_01_and_curated_rows(connection) -> None:
    """#1264: `staging.issuer_headcount_facts` is append-only, so legacy uncited
    `2026-01-01` `manual-review` rows and retired `'curated-10k-2025'` rows remain on
    Staging/Production. `lookup_headcount(connection, cik, cutoff)` and
    `PostgresHeadcountExtractor(connection)(cik, cutoff)` must filter them out so the
    cited 10-K filing wins at `cutoff = 2026-03-31`."""
    visa_cik = 1403161
    # Insert a cited FY2025 10-K row filed 2025-11-06 (knowable_at < 2026-01-01).
    record_headcount(
        connection,
        cik=visa_cik,
        headcount=Decimal("34100"),
        knowable_at=datetime(2025, 11, 6, tzinfo=UTC),
        period_end=date(2025, 9, 30),
        source="manual-review",
        evidence_ref="accession=000140316125000089 form=10-K filed=2025-11-06 source=manual-review",
        confidence=Decimal("0.70"),
    )
    # Simulate the pre-#1287 legacy uncited row stamped at 2026-01-01 and a retired
    # curated-10k-2025 row already sitting in the append-only table.
    connection.execute(
        """
        insert into staging.issuer_headcount_facts
            (cik, headcount, knowable_at, period_end, source, evidence_ref, confidence)
        values
            (%s, %s, %s, null, 'manual-review', %s, 0.50),
            (%s, %s, %s, %s, 'curated-10k-2025', %s, 0.90)
        """,
        (
            visa_cik,
            Decimal("31600"),
            datetime(2026, 1, 1, tzinfo=UTC),
            "10-K human capital disclosure; manual entry, no verified accession (#70 supersedes)",
            visa_cik,
            Decimal("99999"),
            datetime(2026, 2, 1, tzinfo=UTC),
            date(2025, 9, 30),
            "accession=000140316125000089 form=10-K filed=2026-02-01 source=curated-10k-2025",
        ),
    )

    fact = lookup_headcount(connection, visa_cik, date(2026, 3, 31))
    assert fact is not None
    assert fact.value == Decimal("34100")
    assert fact.knowable_at == datetime(2025, 11, 6, tzinfo=UTC)
    assert fact.period_end == date(2025, 9, 30)
    assert "accession=000140316125000089" in fact.evidence_ref

    extractor_fact = PostgresHeadcountExtractor(connection)(visa_cik, date(2026, 3, 31))
    assert extractor_fact == fact
