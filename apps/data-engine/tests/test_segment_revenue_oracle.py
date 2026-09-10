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
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from data_engine.datahub.standards.segment_extraction import consolidated_revenue
from truealpha_contracts.standards import STANDARDS

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
_UNSET = object()
#: What the prod plane returns for AVGO (vps-01, 2026-09-10): the absolute revenue and the
#: period it describes. The period comes from the ORACLE, not the filing's table header, so
#: the parts and the total provably describe the same year.
_ORACLE_ROW = ("63887000000", "2025-11-02")
CIK = 1_730_168  # AVGO
CUTOFF = datetime(2026, 9, 1, tzinfo=UTC)


class _RecordingConnection:
    """Captures every query and its parameters instead of running them.

    The oracle's ADDRESS is decidable without a database — a padded key or an unpadded one,
    the capture plane or the retired table — and so is the SHAPE of what the writer lands.
    A partition is many rows sharing an id, and "did all of them go in with the same id" is
    a property of the calls, not of the storage.
    """

    def __init__(self, row=_UNSET, *, recorded: bool = False) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._row = _ORACLE_ROW if row is _UNSET else row
        self._recorded = recorded

    def execute(self, sql, params):
        self.calls.append((sql, params))
        self._last = sql
        return self

    def fetchone(self):
        if "issuer_segment_revenue_facts" in self._last and "select" in self._last.lower():
            return (1,) if self._recorded else None
        return self._row

    @property
    def sql(self) -> str | None:
        return self.calls[0][0] if self.calls else None

    @property
    def params(self) -> tuple | None:
        return self.calls[0][1] if self.calls else None

    def inserts(self) -> list[tuple]:
        return [params for sql, params in self.calls if sql.lstrip().lower().startswith("insert")]


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
    assert consolidated_revenue(_RecordingConnection((None, None)), CIK, cutoff=CUTOFF) is None


def test_a_total_with_no_stated_period_is_not_an_oracle() -> None:
    """The identity's premise is that the parts and the total describe the same period. A
    total whose period the source never stated cannot certify any period's partition — and
    the plane's `period_end` is NOT NULL precisely because `partition_id` is addressed over
    it, so a null would let two fiscal years of the same segments hash to one set."""
    assert consolidated_revenue(_RecordingConnection(("63887000000", None)), CIK, cutoff=CUTOFF) is None


def test_the_value_arrives_as_decimal_not_float() -> None:
    """The plane stores the number as JSON text (63887000000). Reading it through float would
    put binary error into a monetary comparison the whole design turns on."""
    oracle = consolidated_revenue(_RecordingConnection(), CIK, cutoff=CUTOFF)
    assert oracle.value == Decimal("63887000000")
    assert isinstance(oracle.value, Decimal)
    assert oracle.period_end == date(2025, 11, 2), "AVGO's fiscal year end, as the source states it"


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
        connection=_RecordingConnection(),
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


def _avgo_filing():
    from data_engine.datahub.standards.filing_extraction import FilingDocument

    return FilingDocument(
        cik=CIK,
        accession="0001730168-25-000121",
        form="10-K",
        filing_date=date(2025, 12, 12),
        primary_document="avgo-20251102.htm",
        url="https://www.sec.gov/Archives/edgar/data/1730168/avgo-20251102.htm",
        body=FILING.read_bytes(),
    )


def _run(monkeypatch, connection, *, write: bool):
    from data_engine.datahub.standards import segment_extraction as adapter

    monkeypatch.setattr(adapter, "latest_annual_filing", lambda *a, **k: _avgo_filing())
    return adapter.extract_segment_revenue(
        CIK,
        connection=connection,
        http=None,
        gateway=None,
        standard=STANDARDS["segment_revenue"],
        cutoff=CUTOFF,
        write=write,
    )


#: Column order of the writer's insert, so an assertion names a field instead of an index.
_COLS = (
    "cik segment_name segment_revenue partition_id partition_total partition_residual "
    "knowable_at period_end source evidence_ref extractor confidence"
).split()


def _row(params) -> dict:
    return dict(zip(_COLS, params, strict=True))


def test_a_probe_writes_nothing(monkeypatch) -> None:
    """`write=False` is how an operator measures what a backfill would do. A probe that
    inserted would make the measurement the change."""
    connection = _RecordingConnection()
    outcome = _run(monkeypatch, connection, write=False)
    assert outcome.status == "resolved"
    assert outcome.detail.startswith("would land ")
    assert connection.inserts() == []


def test_write_lands_one_row_per_segment_under_one_partition_id(monkeypatch) -> None:
    """The rows are admissible only as the set they were accepted in, so they carry the set's
    identity. Two rows with different partition ids would be two claims, and a consumer
    computing a share over them would be mixing extractions."""
    connection = _RecordingConnection()
    outcome = _run(monkeypatch, connection, write=True)
    rows = [_row(p) for p in connection.inserts()]

    assert outcome.status == "resolved"
    assert len(rows) == 2
    assert {r["segment_name"] for r in rows} == {"Semiconductor solutions", "Infrastructure software"}
    assert {r["segment_revenue"] for r in rows} == {Decimal("36858000000"), Decimal("27029000000")}
    assert len({r["partition_id"] for r in rows}) == 1
    assert rows[0]["partition_id"].startswith("segment-partition:")
    assert outcome.detail.startswith(rows[0]["partition_id"] + " landed ")


def test_every_landed_row_carries_the_identity_it_was_accepted_under(monkeypatch) -> None:
    """`partition_total` and `partition_residual` on every row is what lets a reader RE-CHECK
    a set instead of trusting it — the difference between a share it can defend and one it
    inherited."""
    connection = _RecordingConnection()
    _run(monkeypatch, connection, write=True)
    rows = [_row(p) for p in connection.inserts()]

    assert {r["partition_total"] for r in rows} == {Decimal("63887000000")}
    assert {r["partition_residual"] for r in rows} == {Decimal("0")}
    assert sum(r["segment_revenue"] for r in rows) + rows[0]["partition_residual"] == rows[0]["partition_total"]
    assert {r["period_end"] for r in rows} == {date(2025, 11, 2)}, "the ORACLE's period, not the table header's"
    assert {r["source"] for r in rows} == {"10k-segment-extraction"}
    assert all("accession=0001730168-25-000121" in r["evidence_ref"] for r in rows)
    assert all(r["confidence"] > 0 for r in rows)


def test_knowable_at_is_the_filing_date_and_never_an_insertion_clock(monkeypatch) -> None:
    """AGENTS.md's most-repeated defect shape: a fact stamped at insert time is look-ahead for
    every historical cutoff, and it passes every test built on the founding assumption."""
    connection = _RecordingConnection()
    _run(monkeypatch, connection, write=True)
    rows = [_row(p) for p in connection.inserts()]
    assert {r["knowable_at"] for r in rows} == {datetime(2025, 12, 12, tzinfo=UTC)}
    assert all(r["knowable_at"] < datetime.now(UTC) for r in rows)


def test_re_extracting_the_same_filing_collapses_instead_of_duplicating(monkeypatch) -> None:
    """`partition_id` is content-addressed, so the second run addresses the set the first one
    wrote. Landing it again would double every segment and halve every share."""
    connection = _RecordingConnection(recorded=True)
    outcome = _run(monkeypatch, connection, write=True)
    assert outcome.status == "already_recorded"
    assert connection.inserts() == []
    assert "already holds" in outcome.detail


def test_the_partition_id_is_addressed_by_what_the_set_is() -> None:
    """Stable across runs, distinct across periods. If the period were not part of the
    address, two fiscal years of the same segment names would collide and a restatement would
    look like a re-run."""
    from data_engine.datahub.standards.segment_extraction import partition_id_for

    parts = [("Semiconductor solutions", Decimal("36858000000")), ("Infrastructure software", Decimal("27029000000"))]
    first = partition_id_for(CIK, date(2025, 11, 2), parts)
    assert first == partition_id_for(CIK, date(2025, 11, 2), list(reversed(parts))), "a set is not an order"
    assert first != partition_id_for(CIK, date(2024, 11, 3), parts), "a different period is a different set"
    assert first != partition_id_for(CIK + 1, date(2025, 11, 2), parts)
    changed = [parts[0], ("Infrastructure software", Decimal("27029000001"))]
    assert first != partition_id_for(CIK, date(2025, 11, 2), changed), "a restatement is a new set"


def test_a_holding_company_files_under_the_issuer_it_is_now(monkeypatch) -> None:
    """#496: `cik` is where the FILING comes from, `record_cik` is who the fact is ABOUT.
    The backfill's predecessor fallback passes both, and the first version of this adapter
    discarded `record_cik` — so a post-reorganization issuer's segments would have landed
    under the CIK it no longer trades as, invisible to every reader looking it up.

    The oracle follows the issuer too: checking a holdco's parts against the predecessor's
    consolidated revenue balances two different entities against each other.
    """
    from data_engine.datahub.standards import segment_extraction as adapter

    predecessor, issuer = 34_088, CIK  # the filing's CIK vs. the issuer's
    monkeypatch.setattr(adapter, "latest_annual_filing", lambda *a, **k: _avgo_filing())
    connection = _RecordingConnection()
    outcome = adapter.extract_segment_revenue(
        predecessor,
        connection=connection,
        http=None,
        gateway=None,
        standard=STANDARDS["segment_revenue"],
        cutoff=CUTOFF,
        write=True,
        record_cik=issuer,
    )
    rows = [_row(p) for p in connection.inserts()]
    assert outcome.cik == issuer
    assert {r["cik"] for r in rows} == {issuer}, "the fact is filed under the issuer, not the filing's CIK"
    assert connection.calls[0][1][0] == f"companyfacts:CIK{issuer:010d}", "and so is the total it was checked against"


def test_the_oracle_s_annotation_matches_what_it_returns() -> None:
    """A signature that lies is worse than no signature: `-> Decimal | None` on a function
    returning `ConsolidatedRevenue` type-checks a caller into `oracle * 2` (review on #806).
    Asserted from the runtime annotation so it cannot drift back without this going red."""
    import typing

    from data_engine.datahub.standards.segment_extraction import ConsolidatedRevenue

    hints = typing.get_type_hints(consolidated_revenue)
    assert hints["return"] == ConsolidatedRevenue | None
    assert isinstance(consolidated_revenue(_RecordingConnection(), CIK, cutoff=CUTOFF), ConsolidatedRevenue)


def _filing(name: str, cik: int):
    from datetime import date

    from data_engine.datahub.standards.filing_extraction import FilingDocument

    path = REPO_ROOT / "apps" / "data-engine" / "samples" / "filings" / name
    return FilingDocument(
        cik=cik,
        accession="0001628280-26-012494",
        form="10-K",
        filing_date=date(2026, 2, 27),
        primary_document=name,
        url=f"https://www.sec.gov/Archives/edgar/data/{cik}/{name}",
        body=path.read_bytes(),
    )


def test_a_single_segment_issuer_lands_the_whole_company_as_one_part(monkeypatch) -> None:
    """A pure-play is the purest name under its theme, and every one of them was refused.

    The partition is the consolidated revenue in ONE part, which satisfies the accounting
    identity by construction — so the row has to say that out loud: a distinct extractor id
    the confidence policy prices lower, and the filing's own sentence in `evidence_ref`,
    because the identity gives these rows no independent check.
    """
    from data_engine.datahub.standards import segment_extraction as adapter
    from factors.shared.extraction import RULE_SINGLE_SEGMENT

    duol_cik = 1562088
    monkeypatch.setattr(
        adapter, "latest_annual_filing", lambda *a, **k: _filing("DUOL_10K_000162828026012494.html", duol_cik)
    )
    connection = _RecordingConnection(("748000000", "2025-12-31"))
    outcome = adapter.extract_segment_revenue(
        duol_cik,
        connection=connection,
        http=None,
        gateway=None,
        standard=STANDARDS["segment_revenue"],
        cutoff=CUTOFF,
        write=True,
    )
    rows = [_row(p) for p in connection.inserts()]

    assert outcome.status == "resolved", outcome.detail
    assert outcome.extractor == RULE_SINGLE_SEGMENT, "named apart from the checked-partition rule"
    assert len(rows) == 1
    assert rows[0]["segment_revenue"] == Decimal("748000000"), "the one part IS the consolidated revenue"
    assert rows[0]["partition_residual"] == Decimal(0), "exhaustive by construction"
    assert "single_segment_statement=" in rows[0]["evidence_ref"], "the sentence the claim rests on"
    assert "single operating segment" in rows[0]["evidence_ref"]
    assert rows[0]["confidence"] < Decimal("0.90"), "priced below a set checked against an independent total"


def test_an_issuer_that_reports_segments_never_takes_the_single_segment_path(monkeypatch) -> None:
    """A false positive here would REPLACE a real segment table with one undifferentiated
    part, and the resulting purity would be 0 or 1 for every theme — confidently wrong rather
    than refused."""
    from data_engine.datahub.standards import segment_extraction as adapter
    from factors.shared.extraction import RULE_EXHAUSTIVE_PARTITION

    monkeypatch.setattr(adapter, "latest_annual_filing", lambda *a, **k: _avgo_filing())
    outcome = adapter.extract_segment_revenue(
        CIK,
        connection=_RecordingConnection(),
        http=None,
        gateway=None,
        standard=STANDARDS["segment_revenue"],
        cutoff=CUTOFF,
        write=False,
    )
    assert outcome.status == "resolved"
    assert outcome.extractor == RULE_EXHAUSTIVE_PARTITION, "AVGO's two segments, checked against its total"
    assert "Semiconductor solutions=36858" in outcome.detail
