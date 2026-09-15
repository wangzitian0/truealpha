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
from dataclasses import replace
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
    monkeypatch.setattr(adapter, "fetch_annual_filing", lambda *a, **k: filing)

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
    assert "Semiconductor Solutions=36858000000" in outcome.detail
    assert "Infrastructure Software=27029000000" in outcome.detail
    assert "residual 0" in outcome.detail
    assert outcome.accession == "0001730168-25-000121"


def test_a_capacity_signal_defers_the_cell_instead_of_crashing_the_backfill(monkeypatch) -> None:
    """A throttled vendor means "come back later for THIS issuer", not "abandon the other
    hundred". `extract_headcount` has always returned `deferred_capacity`; this adapter let
    the exception escape and took the whole run with it (review on #805)."""
    from data_engine.datahub.standards import filing_extraction
    from data_engine.datahub.standards import segment_extraction as adapter
    from data_engine.sources.gateway import CapacityExceeded

    def raise_capacity(*_a, **_k):
        raise CapacityExceeded("sec", "0 calls left in window")

    # Patched at the VENDOR call, not at the catcher, so the real `fetch_annual_filing` is the
    # thing being tested. Patching the catcher would assert that a stub returns what the stub
    # was told to return.
    monkeypatch.setattr(filing_extraction, "latest_annual_filing", raise_capacity)
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
    from data_engine.datahub.standards import filing_extraction
    from data_engine.datahub.standards import segment_extraction as adapter

    def boom(*_a, **_k):
        raise TimeoutError("read timed out")

    monkeypatch.setattr(filing_extraction, "latest_annual_filing", boom)
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

    monkeypatch.setattr(adapter, "fetch_annual_filing", lambda *a, **k: _avgo_filing())
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
    assert {r["segment_name"] for r in rows} == {"Semiconductor Solutions", "Infrastructure Software"}
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

    rule = "rule:exhaustive-partition:v1"
    parts = [("Semiconductor solutions", Decimal("36858000000")), ("Infrastructure software", Decimal("27029000000"))]
    first = partition_id_for(CIK, date(2025, 11, 2), parts, extractor=rule)
    assert first == partition_id_for(CIK, date(2025, 11, 2), list(reversed(parts)), extractor=rule), (
        "a set is not an order"
    )
    assert first != partition_id_for(CIK, date(2024, 11, 3), parts, extractor=rule), (
        "a different period is a different set"
    )
    assert first != partition_id_for(CIK + 1, date(2025, 11, 2), parts, extractor=rule)
    changed = [parts[0], ("Infrastructure software", Decimal("27029000001"))]
    assert first != partition_id_for(CIK, date(2025, 11, 2), changed, extractor=rule), "a restatement is a new set"
    # #822: the same parts accepted by another rule are another claim. Were they the same id, a
    # withdrawn rule's rows would block the replacement rule from landing the parts it accepts.
    assert first != partition_id_for(CIK, date(2025, 11, 2), parts, extractor="rule:exhaustive-partition:v2")


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
    monkeypatch.setattr(adapter, "fetch_annual_filing", lambda *a, **k: _avgo_filing())
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
        adapter, "fetch_annual_filing", lambda *a, **k: _filing("DUOL_10K_000162828026012494.html", duol_cik)
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
    assert "segment_count=us-gaap:NumberOfReportableSegments=1@2025-12-31" in rows[0]["evidence_ref"], (
        "the filer's own tagged count is what the claim rests on (#822)"
    )
    assert "single_segment_statement=" in rows[0]["evidence_ref"], "and the sentence it is tagged in"
    assert "it has a single reportable segment" in rows[0]["evidence_ref"], (
        "DUOL tags no segment note, so the tagged sentence stays its description (#841)"
    )
    assert rows[0]["segment_name"] == "Single reportable segment"
    assert rows[0]["confidence"] < Decimal("0.90"), "priced below a set checked against an independent total"


def test_an_issuer_that_reports_segments_never_takes_the_single_segment_path(monkeypatch) -> None:
    """A false positive here would REPLACE a real segment table with one undifferentiated
    part, and the resulting purity would be 0 or 1 for every theme — confidently wrong rather
    than refused."""
    from data_engine.datahub.standards import segment_extraction as adapter
    from factors.shared.extraction import RULE_EXHAUSTIVE_PARTITION

    monkeypatch.setattr(adapter, "fetch_annual_filing", lambda *a, **k: _avgo_filing())
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
    assert "Semiconductor Solutions=36858000000" in outcome.detail


# --- #830: segment revenue as the filer tags it ----------------------------------------------


def _packaged(name: str, *, cik: int, filed: date):
    from data_engine.datahub.standards.filing_extraction import FilingDocument

    return FilingDocument(
        cik=cik,
        accession="0000000000-26-000830",
        form="10-K",
        filing_date=filed,
        primary_document=name,
        url=f"https://www.sec.gov/Archives/edgar/data/{cik}/{name}",
        body=(REPO_ROOT / "apps" / "data-engine" / "samples" / "filings" / name).read_bytes(),
    )


def _extract(monkeypatch, document, oracle, *, write: bool = True):
    from data_engine.datahub.standards import segment_extraction as adapter

    monkeypatch.setattr(adapter, "fetch_annual_filing", lambda *a, **k: document)
    connection = _RecordingConnection(oracle)
    outcome = adapter.extract_segment_revenue(
        document.cik,
        connection=connection,
        http=None,
        gateway=None,
        standard=STANDARDS["segment_revenue"],
        cutoff=CUTOFF,
        write=write,
    )
    return outcome, [_row(p) for p in connection.inserts()]


def test_tagged_segment_revenue_lands_before_any_table_is_read(monkeypatch) -> None:
    """AAPL tags its five geographic reportable segments, and they sum to the consolidated
    revenue the capture plane holds to the dollar. The row says the parts were TAGGED and names
    each member, so every part can be found in the filing."""
    from factors.shared.extraction import RULE_EXHAUSTIVE_PARTITION

    aapl = _packaged("AAPL_10K_000032019325000079.html", cik=320193, filed=date(2025, 10, 31))
    outcome, rows = _extract(monkeypatch, aapl, ("416161000000", "2025-09-27"))

    assert (outcome.status, outcome.extractor) == ("resolved", RULE_EXHAUSTIVE_PARTITION), outcome.detail
    assert {r["segment_name"]: r["segment_revenue"] for r in rows} == {
        "Americas": Decimal("178353000000"),
        "Europe": Decimal("111032000000"),
        "Greater China": Decimal("64377000000"),
        "Japan": Decimal("28703000000"),
        "Rest of Asia Pacific": Decimal("33696000000"),
    }
    assert {r["partition_residual"] for r in rows} == {Decimal(0)}
    evidence = rows[0]["evidence_ref"]
    assert "recall=xbrl concept=us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax" in evidence
    assert "aapl:GreaterChinaSegmentMember" in evidence


def test_a_tagged_set_that_does_not_balance_is_refused_with_each_sets_reason(monkeypatch) -> None:
    """ADP's two segments exceed its total by the intersegment revenue; JPM's three bank segments
    fall short by Corporate, whose +7,025M is off by the -3,134M reconciling item beside it. Each is
    a refusal naming the set, its shape and which way — never a landed set, and never a negative
    part to make one balance."""
    adp = _packaged("ADP_10K_000000867026000030.html", cik=8670, filed=date(2026, 8, 6))
    outcome, rows = _extract(monkeypatch, adp, ("21947400000", "2026-06-30"))
    assert outcome.status == "no_candidate" and rows == []
    assert "RevenueFromContractWithCustomerExcludingAssessedTax: sums_over_total (2 members)" in outcome.detail

    jpm = _packaged("JPM_10K_000162828026008131.html", cik=19617, filed=date(2026, 2, 13))
    outcome, rows = _extract(monkeypatch, jpm, ("182447000000", "2025-12-31"))
    assert outcome.status == "no_candidate" and rows == []
    assert "RevenuesNetOfInterestExpense: sums_short_of_total (3 members)" in outcome.detail
    assert "no single reconciling item closes it" in outcome.detail


def test_a_short_set_is_completed_by_one_positive_corporate_item(monkeypatch) -> None:
    """#835, measured on ADM: its three tagged segments are short of its 80,269M revenue by exactly
    the 449M it tags as `CorporateNonSegmentMember`. That is revenue ADM earns, so the set lands
    with it as a named part — and the row says which item completed it."""
    adm = _packaged("ADM_10K_000000708426000011.html", cik=7084, filed=date(2026, 2, 17))
    outcome, rows = _extract(monkeypatch, adm, ("80269000000", "2025-12-31"))
    assert outcome.status == "resolved", outcome.detail
    parts = {r["segment_name"]: r["segment_revenue"] for r in rows}
    assert parts["Corporate and other"] == Decimal("449000000")
    assert sum(parts.values()) == Decimal("80269000000")
    assert "reconciling=us-gaap:CorporateNonSegmentMember" in rows[0]["evidence_ref"]


def test_each_context_shape_is_its_own_set(monkeypatch) -> None:
    """#835, measured on Berkshire: it tags its segments twice, on the segment axis alone and with
    `OperatingSegmentsMember`, with different values. Merged, one member carried two values and
    the set was refused; apart, the segment-axis set IS the consolidated revenue."""
    both = ("srt:ConsolidationItemsAxis", "us-gaap:OperatingSegmentsMember")
    contexts = (
        _annual_context("c-1", "x:InsuranceMember"),
        _annual_context("c-2", "x:RailMember"),
        _annual_context("c-3", "x:InsuranceMember", extra=both),
        _annual_context("c-4", "x:RailMember", extra=both),
    )
    body = _ixbrl(
        _revenue_tag("c-1", "Insurance", "110"),
        _revenue_tag("c-2", "Rail", "40"),
        _revenue_tag("c-3", "Insurance", "100"),
        _revenue_tag("c-4", "Rail", "40"),
        contexts=contexts,
    )
    document = _packaged("AVGO_10K_000173016825000121.html", cik=1067983, filed=date(2026, 3, 2))
    outcome, rows = _extract(monkeypatch, replace(document, body=body), ("150000000", "2025-12-31"))
    assert outcome.status == "resolved", outcome.detail
    assert {r["segment_name"]: r["segment_revenue"] for r in rows} == {
        "Insurance": Decimal("110000000"),
        "Rail": Decimal("40000000"),
    }
    assert "shape=segment-axis " in rows[0]["evidence_ref"], "the one shape that is the whole revenue"


def test_one_consistent_set_split_across_both_shapes_is_answered_by_their_union(monkeypatch) -> None:
    """#835, measured on Kraft Heinz: North America and International are tagged on the segment
    axis alone, Emerging Markets with `OperatingSegmentsMember`. Neither shape is the revenue on its
    own; the union, with no member tagged twice, is exactly it."""
    both = ("srt:ConsolidationItemsAxis", "us-gaap:OperatingSegmentsMember")
    contexts = (
        _annual_context("c-1", "x:NorthAmericaMember"),
        _annual_context("c-2", "x:InternationalMember"),
        _annual_context("c-3", "x:EmergingMarketsMember", extra=both),
    )
    body = _ixbrl(
        _revenue_tag("c-1", "NA", "110"),
        _revenue_tag("c-2", "Intl", "25"),
        _revenue_tag("c-3", "EM", "15"),
        contexts=contexts,
    )
    document = _packaged("AVGO_10K_000173016825000121.html", cik=1637459, filed=date(2026, 2, 12))
    outcome, rows = _extract(monkeypatch, replace(document, body=body), ("150000000", "2025-12-31"))
    assert outcome.status == "resolved", outcome.detail
    assert {r["segment_name"] for r in rows} == {"North America", "International", "Emerging Markets"}
    assert "shape=all " in rows[0]["evidence_ref"]


def test_a_reconciling_item_that_does_not_close_the_gap_or_is_negative_refuses(monkeypatch) -> None:
    corporate = "us-gaap:CorporateNonSegmentMember"
    contexts = (
        _annual_context("c-1", "x:AMember"),
        _annual_context("c-2", "x:BMember"),
        _consolidation_context("c-3", corporate),
    )

    def run(corporate_value: str, *, sign: str = ""):
        body = _ixbrl(
            _revenue_tag("c-1", "A", "100"),
            _revenue_tag("c-2", "B", "40"),
            _revenue_tag("c-3", "Corporate", corporate_value, sign=sign),
            contexts=contexts,
        )
        document = _packaged("AVGO_10K_000173016825000121.html", cik=1166691, filed=date(2026, 1, 29))
        return _extract(monkeypatch, replace(document, body=body), ("150000000", "2025-12-31"))

    outcome, rows = run("2")
    assert (
        outcome.status == "no_candidate" and rows == [] and "no single reconciling item closes it" in outcome.detail
    ), "10 short, 2 offered: 8 over the tolerance of five of the tagged unit"
    outcome, rows = run("10", sign="-")
    assert (outcome.status, rows) == ("no_candidate", []), "a negative item is not revenue"
    outcome, rows = run("10")
    assert outcome.status == "resolved" and len(rows) == 3


def test_a_filing_that_tags_its_segments_is_never_answered_by_a_printed_table(monkeypatch) -> None:
    """Measured on Comcast (#830): its five tagged segments do not balance (they include
    intersegment revenue), and the retired table reader DID balance a table — "United States,
    United Kingdom, Other", its geography. The filing below prints exactly such a table beside
    tagged segments that do not balance, and the answer stays a refusal (#833)."""
    table = (
        "Revenue by segment (in millions) United States 100 United Kingdom 50 Total 150 "
        "Revenue by segment (in millions) United States 100 United Kingdom 50 Total 150"
    )
    body = _ixbrl(
        table,
        f"Media {_revenue_tag('c-1', 'Media', '100')} Studios {_revenue_tag('c-2', 'Studios', '80')}",
        contexts=(
            _annual_context("c-1", "cmcsa:MediaSegmentMember"),
            _annual_context("c-2", "cmcsa:StudiosSegmentMember"),
        ),
    )
    document = _packaged("AVGO_10K_000173016825000121.html", cik=1166691, filed=date(2026, 1, 29))
    outcome, rows = _extract(monkeypatch, replace(document, body=body), ("150000000", "2025-12-31"))
    assert rows == [], outcome.detail
    assert outcome.status == "no_candidate"
    assert "sums_over_total (2 members)" in outcome.detail


def test_a_segment_tagged_twice_a_negative_part_or_another_period_refuses(monkeypatch) -> None:
    def run(*tags: str, contexts: tuple[str, ...], oracle=("150000000", "2025-12-31")):
        document = _packaged("AVGO_10K_000173016825000121.html", cik=1166691, filed=date(2026, 1, 29))
        body = _ixbrl(" ".join(tags), contexts=contexts)
        return _extract(monkeypatch, replace(document, body=body), oracle)

    contexts = (_annual_context("c-1", "x:AMember"), _annual_context("c-2", "x:BMember"))
    outcome, rows = run(
        _revenue_tag("c-1", "A", "100"),
        _revenue_tag("c-1", "A", "90"),
        _revenue_tag("c-2", "B", "50"),
        contexts=contexts,
    )
    assert rows == [] and "member_tagged_twice" in outcome.detail

    outcome, rows = run(_revenue_tag("c-1", "A", "200"), _revenue_tag("c-2", "B", "50", sign="-"), contexts=contexts)
    assert rows == [] and "negative_part" in outcome.detail, "an elimination is not a segment"

    outcome, rows = run(
        _revenue_tag("c-1", "A", "100"),
        _revenue_tag("c-2", "B", "50"),
        contexts=contexts,
        oracle=("150000000", "2024-12-31"),
    )
    assert rows == [] and "tagged for 2025-12-31, revenue on file is for 2024-12-31" in outcome.detail


def _annual_context(context: str, member: str, *, end: str = "2025-12-31", extra: tuple[str, str] | None = None) -> str:
    second = f'<xbrldi:explicitMember dimension="{extra[0]}">{extra[1]}</xbrldi:explicitMember>' if extra else ""
    return (
        f'<xbrli:context id="{context}"><xbrli:entity>'
        '<xbrli:identifier scheme="http://www.sec.gov/CIK">0000000001</xbrli:identifier>'
        '<xbrli:segment><xbrldi:explicitMember dimension="us-gaap:StatementBusinessSegmentsAxis">'
        f"{member}</xbrldi:explicitMember>{second}</xbrli:segment></xbrli:entity>"
        f"<xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate>"
        "</xbrli:period></xbrli:context>"
    )


def _consolidation_context(context: str, member: str, *, end: str = "2025-12-31") -> str:
    return (
        f'<xbrli:context id="{context}"><xbrli:entity>'
        '<xbrli:identifier scheme="http://www.sec.gov/CIK">0000000001</xbrli:identifier>'
        '<xbrli:segment><xbrldi:explicitMember dimension="srt:ConsolidationItemsAxis">'
        f"{member}</xbrldi:explicitMember></xbrli:segment></xbrli:entity>"
        f"<xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate>"
        "</xbrli:period></xbrli:context>"
    )


def _revenue_tag(context: str, _label: str, shown: str, *, sign: str = "") -> str:
    signed = f' sign="{sign}"' if sign else ""
    return (
        f'<ix:nonFraction unitRef="usd" contextRef="{context}" decimals="-6" scale="6"{signed} '
        f'name="us-gaap:Revenues" format="ixt:num-dot-decimal">{shown}</ix:nonFraction>'
    )


def test_the_reader_applies_scale_sign_words_and_formats() -> None:
    """Every value the adapters trust passes through one parser, so its edge cases are pinned
    once: scale, a negative sign, a tagged word, a fixed zero, and a comma-decimal number."""
    from data_engine.datahub.standards.inline_xbrl import InlineXbrl

    context = _context("c-1", "2025-12-31")

    def value(attributes: str, shown: str):
        body = _ixbrl(
            f'<ix:nonFraction contextRef="c-1" name="us-gaap:Revenues" {attributes}>{shown}</ix:nonFraction>',
            contexts=(context,),
        )
        (fact,) = InlineXbrl(body).facts({"us-gaap:Revenues"})
        return fact.value

    assert value('scale="6" format="ixt:num-dot-decimal"', "36,858") == Decimal("36858000000")
    assert value('scale="6" sign="-" format="ixt:num-dot-decimal"', "449") == Decimal("-449000000")
    assert value('scale="0" format="ixt-sec:numwordsen"', "seven") == Decimal(7)
    assert value('scale="6" format="ixt:fixed-zero"', "—") == Decimal(0)
    assert value('scale="3" format="ixt:num-comma-decimal"', "1.234,5") == Decimal("1234500")
    assert value('scale="6"', "n/a") is None, "unreadable is None, never a guessed number"


# --- #822: what decides that an issuer has ONE segment ----------------------------------------


def _count_tag(concept: str, context: str, shown: str) -> str:
    return (
        f'<ix:nonFraction unitRef="segment" contextRef="{context}" decimals="INF" '
        f'name="us-gaap:{concept}" format="ixt-sec:numwordsen" scale="0">{shown}</ix:nonFraction>'
    )


def _context(context: str, end: str, *, subsidiary: str | None = None) -> str:
    dimension = (
        '<xbrli:segment><xbrldi:explicitMember dimension="dei:LegalEntityAxis">'
        f"{subsidiary}</xbrldi:explicitMember></xbrli:segment>"
        if subsidiary
        else ""
    )
    return (
        f'<xbrli:context id="{context}"><xbrli:entity>'
        f'<xbrli:identifier scheme="http://www.sec.gov/CIK">0000000001</xbrli:identifier>{dimension}'
        f"</xbrli:entity><xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate>"
        f"<xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>"
    )


def _ixbrl(*paragraphs: str, contexts: tuple[str, ...], hidden: str = "") -> bytes:
    """The smallest inline-XBRL filing that carries a segment count: contexts and any hidden
    facts in the header, then the printed paragraphs. Constructed rather than packaged because
    each one isolates the ONE property a real filing broke; the packaged filings are measured in
    test_segment_extraction.py and the recall census."""
    header = f"<ix:header><ix:hidden>{hidden}</ix:hidden><ix:resources>{''.join(contexts)}</ix:resources></ix:header>"
    return f"<html><body><div style='display:none'>{header}</div>{''.join(f'<p>{p}</p>' for p in paragraphs)}</body></html>".encode()


def _segment_note(heading: str, *parts: str) -> list[str]:
    """A tagged segment note as a filing lays it out: the `ix:nonNumeric` holds the heading and
    the body follows in the `continuedAt` chain of `ix:continuation` elements it names."""
    chain = [heading, *parts]
    paragraphs = []
    for index, text in enumerate(chain):
        following = f' continuedAt="note-{index + 1}"' if index + 1 < len(chain) else ""
        if index == 0:
            paragraphs.append(
                f'<ix:nonNumeric contextRef="c-1" name="us-gaap:SegmentReportingDisclosureTextBlock" id="note-0"'
                f'{following} escape="true">{text}</ix:nonNumeric>'
            )
        else:
            paragraphs.append(f'<ix:continuation id="note-{index}"{following}>{text}</ix:continuation>')
    return paragraphs


def test_a_count_tagged_for_a_subsidiary_registrant_is_not_the_filers_count() -> None:
    """AEP and Exelon file one 10-K for the parent and its subsidiary registrants, and tag
    "ComEd has a single operating segment" under the subsidiary's dimension. The parent has
    several; a dimensional fact is about a part of the filer."""
    from data_engine.datahub.standards.segment_extraction import declared_segment_count

    body = _ixbrl(
        f"ComEd has a {_count_tag('NumberOfReportableSegments', 'c-2', 'one')} reportable segment.",
        contexts=(_context("c-1", "2025-12-31"), _context("c-2", "2025-12-31", subsidiary="exc:ComEdMember")),
    )
    assert declared_segment_count(body) is None


def test_the_latest_period_decides() -> None:
    """Western Digital tags `two` for the day before its Flash separation and `one` for the
    fiscal year. The first tag in the document is the stale one."""
    from data_engine.datahub.standards.segment_extraction import declared_segment_count

    body = _ixbrl(
        f"Prior to the Separation, the Company operated under {_count_tag('NumberOfReportableSegments', 'c-1', 'two')}"
        " reportable segments: HDD and Flash.",
        f"The Company has {_count_tag('NumberOfReportableSegments', 'c-2', 'one')} reportable segment: HDD.",
        contexts=(_context("c-1", "2025-02-20"), _context("c-2", "2026-07-03")),
    )
    declared = declared_segment_count(body)
    assert declared is not None
    assert (declared.value, declared.period_end) == (1, date(2026, 7, 3))
    assert declared.statement is not None and "one reportable segment: HDD" in declared.statement


def test_the_reportable_count_outranks_the_operating_count() -> None:
    """Booking tags five operating segments aggregated into one reportable segment. The
    standard measures reportable segments, so it is one — and an issuer that tags only the
    operating count is answered by that."""
    from data_engine.datahub.standards.segment_extraction import declared_segment_count

    contexts = (_context("c-1", "2025-12-31"),)
    booking = _ixbrl(
        f"The portfolio is organized into {_count_tag('NumberOfOperatingSegments', 'c-1', 'five')} operating segments.",
        f"They are aggregated into {_count_tag('NumberOfReportableSegments', 'c-1', 'one')} reportable segment.",
        contexts=contexts,
    )
    declared = declared_segment_count(booking)
    assert declared is not None
    assert (declared.concept, declared.value) == ("us-gaap:NumberOfReportableSegments", 1)

    operating_only = _ixbrl(
        f"We have {_count_tag('NumberOfOperatingSegments', 'c-1', 'one')} operating segment.", contexts=contexts
    )
    declared = declared_segment_count(operating_only)
    assert declared is not None
    assert (declared.concept, declared.value) == ("us-gaap:NumberOfOperatingSegments", 1)


def test_tags_that_disagree_at_the_latest_period_declare_nothing_usable() -> None:
    from data_engine.datahub.standards.segment_extraction import declared_segment_count

    body = _ixbrl(
        f"We operate in {_count_tag('NumberOfReportableSegments', 'c-1', 'one')} reportable segment.",
        f"Our {_count_tag('NumberOfReportableSegments', 'c-1', 'two')} reportable segments are:",
        contexts=(_context("c-1", "2026-06-27"),),
    )
    declared = declared_segment_count(body)
    assert declared is not None and declared.value is None


def _run_single(monkeypatch, body: bytes, *, oracle=("371444000000", "2025-12-31"), cik: int = 1_067_983):
    from datetime import date as _date

    from data_engine.datahub.standards import segment_extraction as adapter
    from data_engine.datahub.standards.filing_extraction import FilingDocument

    document = FilingDocument(
        cik=cik,
        accession="0000000000-26-000001",
        form="10-K",
        filing_date=_date(2026, 3, 2),
        primary_document="constructed.htm",
        url="https://www.sec.gov/Archives/edgar/data/constructed.htm",
        body=body,
    )
    monkeypatch.setattr(adapter, "fetch_annual_filing", lambda *a, **k: document)
    connection = _RecordingConnection(oracle)
    outcome = adapter.extract_segment_revenue(
        cik,
        connection=connection,
        http=None,
        gateway=None,
        standard=STANDARDS["segment_revenue"],
        cutoff=CUTOFF,
        write=True,
    )
    return outcome, connection


def test_berkshire_is_not_one_segment_because_a_sentence_mentions_one(monkeypatch) -> None:
    """The row that was landed on staging (#822): CIK 1067983 recorded as ONE segment of
    371,444M on the sentence below, which is about how its chief operating decision maker
    weighs expenses ACROSS segments. The filing tags seven.

    A one-part partition balances by construction, so this was not refused — it replaced
    Berkshire's segment table with one undifferentiated part, and every theme purity computed
    from it would have been 0 or 1."""
    from factors.shared.extraction import RULE_SINGLE_SEGMENT

    body = _ixbrl(
        "Expenses considered significant for one operating segment may not be significant in others.",
        f"Berkshire has {_count_tag('NumberOfReportableSegments', 'c-1', 'seven')} reportable business segments.",
        contexts=(_context("c-1", "2025-12-31"),),
    )
    outcome, connection = _run_single(monkeypatch, body)
    assert outcome.extractor != RULE_SINGLE_SEGMENT, outcome.detail
    assert outcome.status == "no_candidate"
    assert connection.inserts() == [], "no partition lands from a sentence"


def test_a_filing_that_tags_no_count_is_not_one_segment_whatever_its_prose_says(monkeypatch) -> None:
    """Visa's row rested on ASU 2023-07 boilerplate — "new segment disclosure requirements for
    entities with a single reportable segment" — which every adopter may print. It was right by
    accident; the next issuer to print it need not be."""
    from factors.shared.extraction import RULE_SINGLE_SEGMENT

    body = _ixbrl(
        "The standard provides new segment disclosure requirements for entities with a single reportable segment.",
        contexts=(_context("c-1", "2025-09-30"),),
    )
    outcome, connection = _run_single(monkeypatch, body, oracle=("40000000000", "2025-09-30"), cik=1_403_161)
    assert outcome.extractor != RULE_SINGLE_SEGMENT, outcome.detail
    assert connection.inserts() == []


def test_a_count_declared_for_another_period_than_the_total_refuses(monkeypatch) -> None:
    """The partition is filed under the ORACLE's period. A count declared for a different year
    says nothing about that one — an issuer that reorganized in between would be landed as one
    segment for a year it had several."""
    body = _ixbrl(
        f"The Company has {_count_tag('NumberOfReportableSegments', 'c-1', 'one')} reportable segment.",
        contexts=(_context("c-1", "2025-12-31"),),
    )
    outcome, connection = _run_single(monkeypatch, body, oracle=("65179000000", "2024-12-31"), cik=59_478)
    assert outcome.status == "no_candidate"
    assert "declares a single segment for 2025-12-31" in outcome.detail
    assert "on file is for 2024-12-31" in outcome.detail
    assert connection.inserts() == []


def test_a_declared_single_segment_lands_with_its_tagged_sentence(monkeypatch) -> None:
    """A filer that tags no segment note is described by the sentence its count is tagged in."""
    from factors.shared.extraction import RULE_SINGLE_SEGMENT

    body = _ixbrl(
        f"The Company has {_count_tag('NumberOfReportableSegments', 'c-1', 'one')} reportable segment, Payment"
        " Services.",
        contexts=(_context("c-1", "2025-09-30"),),
    )
    outcome, connection = _run_single(monkeypatch, body, oracle=("40000000000", "2025-09-30"), cik=1_403_161)
    assert (outcome.status, outcome.extractor) == ("resolved", RULE_SINGLE_SEGMENT), outcome.detail
    (row,) = [_row(p) for p in connection.inserts()]
    assert row["extractor"] == "rule:single-segment:v4"
    assert row["evidence_ref"].endswith(
        "segment_count=us-gaap:NumberOfReportableSegments=1@2025-09-30 "
        "single_segment_statement=The Company has one reportable segment, Payment Services."
    ), "the statement is LAST: the theme-purity reader takes everything after its marker"


def test_the_description_is_the_segment_note_when_the_filer_tags_one(monkeypatch) -> None:
    """#841. Visa tags its count in a sentence about which expenses reach the CODM, and the note
    it tags says "one reportable segment, Payment Services". The note is the filer's statement
    of its segments, so it is what the classifier is shown — read through the continuation
    chain, because the tag itself holds only the heading. Against v2's precedence this is red:
    `DeclaredSegmentCount.statement` is the expenses sentence."""
    from data_engine.datahub.standards.segment_extraction import declared_segment_count
    from factors.shared.extraction import RULE_SINGLE_SEGMENT

    body = _ixbrl(
        *_segment_note(
            "Note 14—Segment Information",
            "All significant operating decisions are based on analysis of Visa as a single global business.",
            "The Company has one reportable segment, Payment Services. The CODM uses consolidated net income.",
        ),
        "Significant expenses that are regularly provided to the CODM for the Company's"
        f" {_count_tag('NumberOfReportableSegments', 'c-1', 'one')} reportable segment are presented on the"
        " consolidated statements of operations.",
        contexts=(_context("c-1", "2025-09-30"),),
    )
    declared = declared_segment_count(body)
    assert declared is not None and declared.statement is not None
    assert "Significant expenses" in declared.statement, "the sentence the count is tagged in"

    outcome, connection = _run_single(monkeypatch, body, oracle=("40000000000", "2025-09-30"), cik=1_403_161)
    assert (outcome.status, outcome.extractor) == ("resolved", RULE_SINGLE_SEGMENT), outcome.detail
    (row,) = [_row(p) for p in connection.inserts()]
    description = row["evidence_ref"].split("single_segment_statement=", 1)[1]
    assert "The Company has one reportable segment, Payment Services." in description
    assert "single global business" in description, "the lead before the declaring sentence, from the note"
    assert "Significant expenses" not in description, "the sentence the count is tagged in is not the description"


def test_a_note_that_declares_in_no_pattern_is_described_by_its_opening(monkeypatch) -> None:
    """AbbVie: "operates as a single global business segment dedicated to the research and
    development … of innovative medicines". No pattern matches it, and the note's first sentences
    are still what the company does — better than the count's sentence, which need not be."""
    from factors.shared.extraction import RULE_SINGLE_SEGMENT

    body = _ixbrl(
        *_segment_note(
            "Segment and Geographic Area Information",
            "<hr/>",  # a page break: a continuation holding only markup, which is not a word
            "AbbVie operates as a single global business segment dedicated to the research and development,"
            " manufacturing, commercialization and sale of innovative medicines and therapies.",
        ),
        f"The company has {_count_tag('NumberOfOperatingSegments', 'c-1', 'one')} operating segment.",
        contexts=(_context("c-1", "2025-12-31"),),
    )
    outcome, connection = _run_single(monkeypatch, body, oracle=("60000000000", "2025-12-31"), cik=1_551_152)
    assert (outcome.status, outcome.extractor) == ("resolved", RULE_SINGLE_SEGMENT), outcome.detail
    (row,) = [_row(p) for p in connection.inserts()]
    description = row["evidence_ref"].split("single_segment_statement=", 1)[1]
    assert description.startswith("Segment and Geographic Area Information AbbVie operates as a single global business")
    assert "innovative medicines" in description


def test_a_note_that_declares_without_describing_is_joined_by_the_business_opening(monkeypatch) -> None:
    """#849. Netflix's note says "operates as one operating segment" and nothing about the
    company; on that alone the classifier answered as NVIDIA. The filing's organization note
    says what the company does, so it travels on the row after `Business:` — and a filing that
    tags no such note (Visa, below in the packaged test) keeps the declaration alone."""
    from factors.shared.extraction import RULE_SINGLE_SEGMENT

    organization = (
        '<ix:nonNumeric contextRef="c-1" name="us-gaap:OrganizationConsolidationAndPresentationOfFinancialStatements'
        'DisclosureAndSignificantAccountingPoliciesTextBlock" id="org-0" escape="true">Organization and Summary of'
        " Significant Accounting Policies Description of Business Netflix, Inc. (the “Company”) was incorporated on"
        " August 29, 1997. The Company is one of the world’s leading entertainment services offering TV series, films,"
        " games and live programming.</ix:nonNumeric>"
    )
    body = _ixbrl(
        organization,
        *_segment_note(
            "Segment and Geographic Information",
            "The Company operates as one operating segment. The CODM reviews financial information on a"
            " consolidated basis.",
        ),
        f"The Company has {_count_tag('NumberOfReportableSegments', 'c-1', 'one')} reportable segment.",
        contexts=(_context("c-1", "2025-12-31"),),
    )
    outcome, connection = _run_single(monkeypatch, body, oracle=("45183036000", "2025-12-31"), cik=1_065_280)
    assert (outcome.status, outcome.extractor) == ("resolved", RULE_SINGLE_SEGMENT), outcome.detail
    (row,) = [_row(p) for p in connection.inserts()]
    description = row["evidence_ref"].split("single_segment_statement=", 1)[1]
    declaration, business = description.split(" Business: ", 1)
    assert "The Company operates as one operating segment." in declaration
    assert business.startswith("Organization and Summary of Significant Accounting Policies")
    assert "leading entertainment services offering TV series, films, games" in business


def test_a_malformed_note_is_read_as_far_as_it_is_well_formed_and_no_further() -> None:
    """A regex reader over HTML that is not XML: a part that never closes is unreadable rather
    than the rest of the document, and a chain that names a part already read ends there rather
    than being read again until the limit (review on #842)."""
    from data_engine.datahub.standards.inline_xbrl import InlineXbrl

    note = "us-gaap:SegmentReportingDisclosureTextBlock"
    contexts = (_context("c-1", "2025-12-31"),)

    unclosed = _ixbrl(
        f'<ix:nonNumeric contextRef="c-1" name="{note}" id="note-0" escape="true">Segment Information',
        "Everything after an unclosed tag is not its text.",
        contexts=contexts,
    )
    assert InlineXbrl(unclosed).text_block(note, limit=600) is None

    cycle = _ixbrl(
        f'<ix:nonNumeric contextRef="c-1" name="{note}" id="note-0" continuedAt="note-1" escape="true">Segment'
        " Information</ix:nonNumeric>",
        '<ix:continuation id="note-1" continuedAt="note-1">We have one reportable segment.</ix:continuation>',
        contexts=contexts,
    )
    assert InlineXbrl(cycle).text_block(note, limit=600) == "Segment Information We have one reportable segment."

    unclosed_later = _ixbrl(
        f'<ix:nonNumeric contextRef="c-1" name="{note}" id="note-0" continuedAt="note-1" escape="true">Segment'
        " Information</ix:nonNumeric>",
        '<ix:continuation id="note-1">We have one reportable segment.',
        "Not the note.",
        contexts=contexts,
    )
    assert InlineXbrl(unclosed_later).text_block(note, limit=600) == "Segment Information", (
        "what was read before the unreadable part stands; the part itself is not guessed at"
    )

    # Three page breaks between the heading and the sentence: parts that hold only markup are
    # neither a word gap nor length, so a limit the sentence would fit under is not spent on them.
    page_breaks = _ixbrl(
        *_segment_note("Segment Information", "<hr/>", "<hr/>", "<hr/>", "We have one reportable segment."),
        contexts=contexts,
    )
    assert InlineXbrl(page_breaks).text_block(note, limit=23) == "Segment Information We "
    # The heading is 19 characters. A limit of 20 has room for one more, so the chain is
    # followed — the count is the joined text's length, with no separator charged after the last part.
    assert InlineXbrl(page_breaks).text_block(note, limit=20) == "Segment Information "


def test_visas_description_is_its_segment_note_not_the_sentence_its_count_is_tagged_in(monkeypatch) -> None:
    """#841 on the packaged filing the row was landed from (staging, v0.0.58–v0.0.60): the count
    is tagged inside "Significant expenses that are regularly provided to the CODM for the
    Company's one reportable segment …", and Note 14 says "The Company has one reportable
    segment, Payment Services."."""
    from data_engine.datahub.standards.segment_extraction import declared_segment_count

    visa = _packaged("V_10K_000140316125000089.html", cik=1_403_161, filed=date(2025, 11, 6))
    declared = declared_segment_count(visa.body)
    assert declared is not None and (declared.value, declared.period_end) == (1, date(2025, 9, 30))
    assert declared.statement is not None and "Significant expenses" in declared.statement, "what v2 carried"

    outcome, rows = _extract(monkeypatch, visa, ("40000000000", "2025-09-30"))
    assert outcome.status == "resolved", outcome.detail
    (row,) = rows
    description = row["evidence_ref"].split("single_segment_statement=", 1)[1]
    assert "The Company has one reportable segment, Payment Services." in description
    assert "Significant expenses" not in description
    assert " Business: " not in description, "Visa tags no nature-of-business block; the declaration stands alone"


def test_a_refusal_says_which_half_of_the_answer_is_missing(monkeypatch) -> None:
    """Two refusals with different owners, kept apart (#833). DUOL declares one segment and this
    environment has no revenue to file it against — the capture plane's gap. PLUG's 2021 filing
    predates segment tagging and declares nothing — the filing's gap, which no amount of revenue
    capture would close."""
    duol = _packaged("DUOL_10K_000162828026012494.html", cik=1562088, filed=date(2026, 2, 27))
    outcome, rows = _extract(monkeypatch, duol, None)
    assert (outcome.status, rows) == ("no_candidate", [])
    assert outcome.detail == (
        "declares a single segment (us-gaap:NumberOfReportableSegments=1@2025-12-31), but this environment "
        "holds no consolidated revenue to file the partition against"
    )

    plug = _packaged("PLUG_10K_000155837021007147.html", cik=1093691, filed=date(2021, 3, 1))
    outcome, rows = _extract(monkeypatch, plug, ("337000000", "2020-12-31"))
    assert (outcome.status, rows) == ("no_candidate", [])
    assert outcome.detail == "the filing tags no segment revenue on the business-segment axis for its latest year"
