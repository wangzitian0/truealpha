"""The oracle a segment partition is checked against (#772, q6; review on #805).

A partition is only as good as the number it is checked against, and this one was checked
against nothing at all. The first version of `consolidated_revenue()` read
`staging.financial_facts` — a table init.md §3 retires with the sentence "It holds 0 rows in
Production", confirmed against prod on 2026-09-10. The query was well-formed, the tests were
green, and every issuer in the universe would have refused `no_total` forever.

That is the repository's most-repeated defect shape (`GREEN-WHILE-EMPTY`): a real source, a
real query, an empty answer, and nothing that objects. So the checks below are about WHERE
the number comes from and WHAT SHAPE the key has, not about the arithmetic on top of it:

1. No deployed module reads the retired table, whatever it is reading it for.
2. The capture plane is addressed the way the capture layer writes it — zero-padded to ten
   digits. `companyfacts:CIK1730168` matches no row that exists; `companyfacts:CIK0001730168`
   is AVGO.
3. The read is point-in-time: an observation is eligible only if it was knowable at the
   cutoff.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from data_engine.datahub.standards.segment_extraction import consolidated_revenue

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_TREES = (
    REPO_ROOT / "apps" / "data-engine" / "src",
    REPO_ROOT / "apps" / "llm-service" / "src",
    REPO_ROOT / "libs" / "contracts" / "src",
    REPO_ROOT / "libs" / "factors" / "src",
    REPO_ROOT / "libs" / "runtime" / "src",
)

#: A SQL reference to the table, not a mention of its name. The keyword in front is what
#: separates `from staging.financial_facts` from a comment explaining why nothing reads it.
RETIRED_TABLE_SQL = re.compile(
    r"\b(?:from|join|into|update)\s+staging\.financial_facts\b",
    re.IGNORECASE,
)

FILING = REPO_ROOT / "apps" / "data-engine" / "samples" / "filings" / "AVGO_10K_000173016825000121.html"
CIK = 1_730_168  # AVGO
CUTOFF = datetime(2026, 9, 1, tzinfo=UTC)


class _RecordingConnection:
    """Captures the query and its parameters instead of running them.

    The point is the ADDRESS, which is decidable without a database: a padded key or an
    unpadded one, the capture plane or the retired table.
    """

    def __init__(self, row=None) -> None:
        self.sql: str | None = None
        self.params: tuple | None = None
        self._row = row

    def execute(self, sql, params):
        self.sql, self.params = sql, params
        return self

    def fetchone(self):
        return self._row


def test_no_deployed_module_reads_the_retired_financial_facts_table() -> None:
    """init.md §3: the table is retired because it sat in the datahub's schema solving the
    factor layer's problem, and it never acquired a writer. Reading it is not a stale
    dependency — it is a query that can only ever return nothing."""
    offenders = []
    for tree in SOURCE_TREES:
        for path in tree.rglob("*.py"):
            if RETIRED_TABLE_SQL.search(path.read_text(encoding="utf-8")):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], (
        f"these modules query staging.financial_facts, which holds 0 rows in Production (init.md §3): {offenders}"
    )


def test_the_capture_plane_is_addressed_with_a_zero_padded_cik() -> None:
    """`companyfacts:CIK{cik}` is how nothing is addressed. The capture layer writes ten
    digits, so an unpadded key silently matches no vintage and every extraction refuses."""
    connection = _RecordingConnection()
    consolidated_revenue(connection, CIK, cutoff=CUTOFF)
    assert connection.params[0] == "companyfacts:CIK0001730168"
    assert connection.params[0] != f"companyfacts:CIK{CIK}"


def test_the_oracle_reads_the_plane_that_holds_revenue() -> None:
    connection = _RecordingConnection()
    consolidated_revenue(connection, CIK, cutoff=CUTOFF)
    sql = connection.sql.lower()
    assert "staging.capture_observation_payloads" in sql
    assert "staging.financial_facts" not in sql
    assert "'revenue'" in sql


def test_the_read_is_point_in_time() -> None:
    """A cutoff that is not applied is look-ahead: a partition checked against a total the
    filing's own reader could not have known makes a replay disagree with history."""
    connection = _RecordingConnection()
    consolidated_revenue(connection, CIK, cutoff=CUTOFF)
    assert CUTOFF in connection.params
    sql = connection.sql.lower()
    assert "knowable_at <= %s" in sql
    assert "order by o.knowable_at desc" in sql, "the newest observation knowable then, not the newest row"


def test_a_missing_observation_is_none_rather_than_zero() -> None:
    """Zero revenue would make every partition OVER; None makes the caller refuse `no_total`,
    which is the truth — the issuer has nothing to check against."""
    assert consolidated_revenue(_RecordingConnection(None), CIK, cutoff=CUTOFF) is None
    assert consolidated_revenue(_RecordingConnection((None,)), CIK, cutoff=CUTOFF) is None


def test_the_value_arrives_as_decimal_not_float() -> None:
    """The plane stores the number as JSON text (63887000000). Reading it through float would
    put binary error into a monetary comparison the whole design turns on."""
    value = consolidated_revenue(_RecordingConnection(("63887000000",)), CIK, cutoff=CUTOFF)
    assert value == Decimal("63887000000")
    assert isinstance(value, Decimal)


def test_the_whole_adapter_resolves_the_real_filing_against_the_real_total(monkeypatch) -> None:
    """The deployed entry point, end to end, on the two real inputs.

    The parts above each prove one half. This one is what production actually calls: the
    packaged AVGO 10-K plus the consolidated revenue as the prod plane holds it
    (63,887,000,000, read 2026-09-10). It is the criterion that can fail where production
    calls — the unit tests would all stay green if `extract_segment_revenue` stopped scaling,
    stopped reading the oracle, or handed the identity the wrong window.
    """
    from datetime import date

    from data_engine.datahub.standards import segment_extraction as adapter
    from data_engine.datahub.standards.filing_extraction import FilingDocument

    filing = FilingDocument(
        cik=CIK,
        accession="0001730168-25-000121",
        form="10-K",
        filing_date=date(2025, 12, 12),
        primary_document="avgo-20251102.htm",
        url="https://www.sec.gov/Archives/edgar/data/1730168/avgo-20251102.htm",
        body=FILING.read_bytes(),
    )
    monkeypatch.setattr(adapter, "latest_annual_filing", lambda *a, **k: filing)

    outcome = adapter.extract_segment_revenue(
        CIK,
        connection=_RecordingConnection(("63887000000",)),
        http=None,
        gateway=None,
        standard=None,
        cutoff=CUTOFF,
        write=False,
    )
    assert outcome.status == "resolved", outcome.detail
    assert "Semiconductor solutions=36858" in outcome.detail
    assert "Infrastructure software=27029" in outcome.detail
    assert "residual 0" in outcome.detail
    assert outcome.accession == "0001730168-25-000121"


def test_a_capacity_signal_defers_the_cell_instead_of_crashing_the_backfill(monkeypatch) -> None:
    """A throttled vendor means "come back later for THIS issuer", not "abandon the other
    hundred". `extract_headcount` has always returned `deferred_capacity`; this adapter let
    the exception escape and took the whole run with it (review on #805)."""
    from data_engine.datahub.standards import segment_extraction as adapter
    from data_engine.sources.gateway import CapacityExceeded

    def raise_capacity(*_a, **_k):
        raise CapacityExceeded("sec", "0 calls left in window")

    monkeypatch.setattr(adapter, "latest_annual_filing", raise_capacity)
    outcome = adapter.extract_segment_revenue(
        CIK,
        connection=_RecordingConnection(),
        http=None,
        gateway=None,
        standard=None,
        cutoff=CUTOFF,
        write=False,
    )
    assert outcome.status == "deferred_capacity"
    assert "0 calls left" in outcome.detail


def test_an_unexpected_failure_is_reported_per_cell(monkeypatch) -> None:
    from data_engine.datahub.standards import segment_extraction as adapter

    def boom(*_a, **_k):
        raise TimeoutError("read timed out")

    monkeypatch.setattr(adapter, "latest_annual_filing", boom)
    outcome = adapter.extract_segment_revenue(
        CIK,
        connection=_RecordingConnection(),
        http=None,
        gateway=None,
        standard=None,
        cutoff=CUTOFF,
        write=False,
    )
    assert outcome.status == "error"
    assert "TimeoutError: read timed out" == outcome.detail


def test_write_mode_says_it_wrote_nothing(monkeypatch) -> None:
    """The writer does not exist yet (#772 lands it with the standard's registration). Until
    it does, `write=True` must not produce the same sentence a probe produces — an operator
    reading "2 segments accounting for ..." would reasonably believe a fact landed."""
    from datetime import date

    from data_engine.datahub.standards import segment_extraction as adapter
    from data_engine.datahub.standards.filing_extraction import FilingDocument

    filing = FilingDocument(
        cik=CIK,
        accession="0001730168-25-000121",
        form="10-K",
        filing_date=date(2025, 12, 12),
        primary_document="avgo-20251102.htm",
        url="https://www.sec.gov/Archives/edgar/data/1730168/avgo-20251102.htm",
        body=FILING.read_bytes(),
    )
    monkeypatch.setattr(adapter, "latest_annual_filing", lambda *a, **k: filing)
    common = dict(
        connection=_RecordingConnection(("63887000000",)),
        http=None,
        gateway=None,
        standard=None,
        cutoff=CUTOFF,
    )
    probed = adapter.extract_segment_revenue(CIK, write=False, **common)
    written = adapter.extract_segment_revenue(CIK, write=True, **common)

    assert probed.status == written.status == "resolved"
    assert written.detail.startswith("would land "), written.detail
    assert probed.detail != written.detail, "write mode must not read like a landed fact"
