"""The standard→wide-row loop, slices 1 and 3 (#735, #70, #733): planner, single-candidate
rule, landing with evidence, and probe mode writing nothing."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from data_engine.datahub.standards import backfill as backfill_module
from data_engine.datahub.standards.backfill import run_standard_backfill
from data_engine.datahub.standards.filing_extraction import (
    EXTRACTION_SOURCE,
    RULE_SINGLE_CANDIDATE,
    FilingCandidate,
    candidates,
    extract_headcount,
    filing_plain_text,
    parse_as_of,
    select_total,
)
from data_engine.datahub.standards.planner import UniverseIssuer, open_cells, resolve_missing_ciks
from data_engine.sources.gateway import SourceCapacity, SourceGateway
from truealpha_contracts import RawCapture, RawIngestionEnvelope, RawObjectRef
from truealpha_contracts.standards import STANDARDS

CUTOFF = datetime(2026, 9, 4, 22, 15, tzinfo=UTC)
STANDARD = STANDARDS["employees_total"]


# --- fakes -----------------------------------------------------------------------------


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConnection:
    """Answers the exact statements the loop issues; records every write."""

    def __init__(self, facts: dict[int, list[tuple[str, datetime]]] | None = None) -> None:
        self.facts = facts or {}
        self.headcount_rows: list[tuple] = []
        self.ledger_rows: list[tuple] = []
        self.raw_rows: list[tuple] = []
        self.health_rows: list[tuple] = []
        self.commits = 0

    def execute(self, sql: str, params: tuple = ()):
        text = " ".join(sql.split())
        if text.startswith("select source, knowable_at from staging.issuer_headcount_facts"):
            cik, cutoff = params
            rows = sorted((r for r in self.facts.get(cik, []) if r[1] <= cutoff), key=lambda r: r[1], reverse=True)
            return _Result(rows[:1])
        if text.startswith("select 1 from staging.issuer_headcount_facts"):
            cik, _source, like = params
            accession = like.removeprefix("accession=").split(" ")[0]
            hits = [r for r in self.headcount_rows if r[0] == cik and f"accession={accession} " in r[5]]
            return _Result([(1,)] if hits else [])
        if text.startswith("select count(*) from staging.api_call_ledger"):
            return _Result([(0,)])
        if text.startswith("insert into staging.api_call_ledger"):
            self.ledger_rows.append(params)
            return _Result([])
        if text.startswith("insert into raw.fetches"):
            self.raw_rows.append(params)
            return _Result([(len(self.raw_rows),)])
        if text.startswith("insert into staging.issuer_headcount_facts"):
            self.headcount_rows.append(params)
            return _Result([(len(self.headcount_rows),)])
        if text.startswith("insert into staging.ingestion_health_log"):
            self.health_rows.append(params)
            return _Result([])
        raise AssertionError(f"unexpected statement: {text[:80]}")

    def commit(self) -> None:
        self.commits += 1


class FakeStore:
    def __init__(self) -> None:
        self.stored: list[RawCapture] = []

    def store(self, capture: RawCapture) -> RawIngestionEnvelope:
        self.stored.append(capture)
        import hashlib

        sha = hashlib.sha256(capture.body).hexdigest()
        ref = RawObjectRef(
            bucket="raw", key=f"k/{sha}", sha256=sha, byte_length=len(capture.body), content_type=capture.content_type
        )
        return RawIngestionEnvelope(
            source=capture.source,
            source_record_id=capture.source_record_id,
            object=ref,
            fetched_at=capture.fetched_at,
            source_published_at=capture.source_published_at,
            metadata=capture.metadata,
        )

    def get(self, ref: RawObjectRef) -> bytes:  # pragma: no cover - not exercised
        raise NotImplementedError


class _Response:
    def __init__(self, *, json_body=None, content: bytes = b"") -> None:
        self._json = json_body
        self.content = content

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._json


class FakeHttp:
    """EDGAR as fixtures: submissions index per CIK, one primary document per accession."""

    def __init__(self, filings: dict[int, list[tuple[str, str, str, bytes]]]) -> None:
        # cik -> [(form, filing_date, accession_with_dashes, body)], newest first
        self.filings = filings
        self.urls: list[str] = []

    def get(self, url: str) -> _Response:
        self.urls.append(url)
        if "/submissions/CIK" in url:
            cik = int(url.rsplit("CIK", 1)[1].removesuffix(".json"))
            rows = self.filings.get(cik, [])
            return _Response(
                json_body={
                    "filings": {
                        "recent": {
                            "form": [r[0] for r in rows],
                            "filingDate": [r[1] for r in rows],
                            "accessionNumber": [r[2] for r in rows],
                            "primaryDocument": [f"doc-{i}.htm" for i in range(len(rows))],
                        }
                    }
                }
            )
        for rows in self.filings.values():
            for form, _filed, accession, body in rows:
                if accession.replace("-", "") in url:
                    return _Response(content=body)
        raise AssertionError(f"unexpected url {url}")


def _gateway(connection) -> SourceGateway:
    return SourceGateway(
        connection,
        caller="test",
        capacities={"sec": SourceCapacity("sec", 1.0, 100, 1000)},
        clock=lambda: 0.0,
        sleep=lambda _s: None,
        now=lambda: CUTOFF,
    )


def _html(*sentences: str) -> bytes:
    return (
        "<html><ix:header>us-gaap:EmployeeMember 999,999 employees</ix:header><body><p>"
        + " ".join(sentences)
        + "</p></body></html>"
    ).encode()


NVDA = "As of the end of fiscal year 2026, we had approximately 42,000 employees in 38 countries engaged in research and development, sales, marketing, and operations."
LLY_TOTAL = "As of December 31, 2025, we employed approximately 50,000 people, including approximately 27,000 employees outside the United States."
LLY_RD = "We employed approximately 12,000 people in pharmaceutical research and development activities."
BRK_TOTAL = (
    "Berkshire and its operating subsidiaries employed approximately 392,400 people worldwide as of December 31, 2025."
)
BRK_SEGMENT = "Our subsidiaries employed approximately 42,600 people in our insurance segment."


# --- selection rule ----------------------------------------------------------------------


def test_plain_text_drops_the_ix_header_and_tags() -> None:
    text = filing_plain_text(_html(NVDA))
    assert "999,999" not in text and "<p>" not in text and "42,000 employees" in text


def test_a_single_company_wide_statement_resolves_by_rule() -> None:
    found = candidates(filing_plain_text(_html(NVDA)))
    status, chosen = select_total(found)
    assert status == "resolved" and chosen is not None
    assert chosen.value == 42000 and chosen.as_of is None


def test_a_qualifier_after_including_belongs_to_the_subset_not_the_total() -> None:
    (candidate,) = [c for c in candidates(filing_plain_text(_html(LLY_TOTAL))) if c.value == 50000]
    assert candidate.partial is False


def test_a_qualifier_in_the_previous_sentence_does_not_mark_this_one_partial() -> None:
    text = filing_plain_text(_html("We also engage seasonal contractors. " + NVDA))
    (candidate,) = candidates(text)
    assert candidate.partial is False and candidate.value == 42000


def test_two_distinct_totals_defer_to_the_model_rather_than_guess() -> None:
    """LLY: 50,000 company-wide and 12,000 in R&D read as two totals to a pattern;
    choosing is a judgement, so the rule refuses (#70: precision is the model's half)."""
    found = candidates(filing_plain_text(_html(LLY_TOTAL, LLY_RD)))
    status, chosen = select_total(found)
    assert status == "needs_model_selection" and chosen is None
    assert {c.value for c in found} == {50000, 12000}


def test_a_segment_qualified_count_is_marked_partial_and_does_not_block_the_total() -> None:
    found = candidates(filing_plain_text(_html(BRK_TOTAL, BRK_SEGMENT)))
    by_value = {c.value: c for c in found}
    assert by_value[42600].partial is True
    assert by_value[392400].partial is False
    # each candidate carries ITS sentence, not the neighbour's
    assert "392,400" in by_value[392400].sentence and "42,600" not in by_value[392400].sentence
    assert by_value[392400].as_of == "December 31, 2025"
    status, chosen = select_total(found)
    assert status == "resolved" and chosen is not None and chosen.value == 392400


def test_stacked_workforce_qualifiers_from_the_first_real_probe_are_candidates() -> None:
    """AAPL, AMAT and AMGN were no_candidate on the first real QQQ probe (2026-09-04)."""
    text = filing_plain_text(
        _html(
            "As of September 27, 2025, the Company had approximately 166,000 full-time equivalent employees.",
            "As of October 26, 2025, we employed approximately 36,500 regular full-time employees spanning 25 countries.",
            "As of December 31, 2025, we had approximately 28,000 staff members.",
            "As of December 31, 2025, we employed approximately 1,556,000 full-time and part-time employees.",
        )
    )
    assert sorted(c.value for c in candidates(text)) == [28000, 36500, 166000, 1556000]
    assert all(not c.partial for c in candidates(text))


def test_a_holding_company_total_and_a_subsidiary_count_defer_to_the_model() -> None:
    """AEP on the first real probe: 17,581 across subsidiaries vs 6,994 at the service
    company — two company-wide-looking statements, one judgement."""
    text = filing_plain_text(
        _html(
            "As of December 31, 2025, the subsidiaries of AEP had a total of 17,581 employees.",
            "As of December 31, 2025, AEPSC had 6,994 employees.",
        )
    )
    status, chosen = select_total(candidates(text))
    assert status == "needs_model_selection" and chosen is None


def test_a_filing_with_no_statement_is_no_candidate_never_a_number() -> None:
    assert select_total(candidates(filing_plain_text(_html("We make things.")))) == ("no_candidate", None)


def test_only_subset_counts_is_no_candidate_not_a_model_question() -> None:
    """Review on #740: when every statement is partial there is no company-wide total
    to choose; deferring to a model would imply the filing has one."""
    found = candidates(filing_plain_text(_html(BRK_SEGMENT, "We employed approximately 300 part-time employees.")))
    assert found and all(c.partial for c in found)
    assert select_total(found) == ("no_candidate", None)


def test_officer_counts_and_absurd_values_are_not_candidates() -> None:
    text = filing_plain_text(_html("Our board of 9 people oversees us.", "We had 7,000,000,000 people as customers."))
    assert candidates(text) == []


def test_as_of_dates_parse_in_both_comma_forms() -> None:
    assert parse_as_of("December 31, 2025") == date(2025, 12, 31)
    assert parse_as_of("November 2 2025") == date(2025, 11, 2)
    assert parse_as_of("fiscal 2025") is None
    assert parse_as_of(None) is None


# --- planner ----------------------------------------------------------------------------


def _issuer(cik: int | None, ticker: str = "T") -> UniverseIssuer:
    return UniverseIssuer(
        issuer_id=f"issuer:cik:{cik:010d}" if cik else "issuer:lei:X",
        ticker=ticker,
        listing_id="listing:xnas:t",
        cik=cik,
    )


def test_open_cells_follow_the_three_rules() -> None:
    fresh_cited = [(EXTRACTION_SOURCE, CUTOFF - timedelta(days=30))]
    stale_cited = [(EXTRACTION_SOURCE, CUTOFF - timedelta(days=STANDARD.max_age_days + 1))]
    seed = [("manual-review", datetime(2026, 1, 1, tzinfo=UTC))]
    future_only = [(EXTRACTION_SOURCE, CUTOFF + timedelta(days=1))]
    connection = FakeConnection({1: fresh_cited, 2: stale_cited, 3: seed, 4: future_only})
    issuers = [_issuer(1), _issuer(2), _issuer(3), _issuer(4), _issuer(5)]

    cells = {cell.issuer.cik: cell.reason for cell in open_cells(connection, issuers, standard=STANDARD, cutoff=CUTOFF)}

    assert cells == {2: "stale", 3: "seed_only", 4: "no_fact", 5: "no_fact"}


def test_open_cells_refuses_a_naive_cutoff() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        open_cells(FakeConnection(), [_issuer(1)], standard=STANDARD, cutoff=datetime(2026, 9, 4))


def test_lei_keyed_issuers_resolve_through_the_sec_crosswalk_in_hyphen_form() -> None:
    issuers = [UniverseIssuer("issuer:lei:A", "BRK.B", "listing:xnys:brk.b", None), _issuer(7, "X")]
    resolved = resolve_missing_ciks(issuers, {"BRK-B": 1067983})
    assert [i.cik for i in resolved] == [1067983, 7]


# --- extraction landing ------------------------------------------------------------------


def _fixture_edgar() -> FakeHttp:
    return FakeHttp(
        {
            1045810: [("10-K", "2026-02-25", "0001045810-26-000021", _html(NVDA))],
            59478: [("10-K", "2026-02-19", "0000059478-26-000010", _html(LLY_TOTAL, LLY_RD))],
            1730168: [
                ("10-Q", "2026-06-10", "0001730168-26-000050", b"<html>quarterly</html>"),
                (
                    "10-K",
                    "2025-12-18",
                    "0001730168-25-000121",
                    _html("As of November 2, 2025, we had approximately 33,000 employees worldwide."),
                ),
            ],
        }
    )


def test_backfill_lands_the_cited_fact_with_the_filing_date_as_knowable_at() -> None:
    connection, store = FakeConnection(), FakeStore()
    outcome = extract_headcount(
        1730168,
        connection=connection,
        http=_fixture_edgar(),
        gateway=_gateway(connection),
        standard=STANDARD,
        cutoff=CUTOFF,
        write=True,
        store=store,
        now=CUTOFF,
    )

    assert outcome.status == "resolved" and outcome.value == 33000
    assert outcome.form == "10-K" and outcome.accession == "000173016825000121"  # the 10-Q is skipped
    assert outcome.as_of == date(2025, 11, 2)
    (row,) = connection.headcount_rows
    cik, headcount, knowable_at, period_end, source, evidence_ref, confidence = row
    assert (cik, headcount, source) == (1730168, Decimal(33000), EXTRACTION_SOURCE)
    assert knowable_at == datetime(2025, 12, 18, tzinfo=UTC)  # the filing date, not the fetch clock
    assert period_end == date(2025, 11, 2)
    assert confidence == Decimal("0.85")
    assert "accession=000173016825000121" in evidence_ref and RULE_SINGLE_CANDIDATE in evidence_ref
    assert "raw=raw.fetches:1" in evidence_ref and "33,000 employees" in evidence_ref
    assert len(store.stored) == 1 and store.stored[0].source_published_at == datetime(2025, 12, 18, tzinfo=UTC)
    assert [r[0] for r in connection.ledger_rows] == ["sec", "sec"]  # submissions + archive, both recorded


def test_a_second_backfill_of_the_same_filing_records_nothing_twice() -> None:
    connection, store, http = FakeConnection(), FakeStore(), _fixture_edgar()
    first = extract_headcount(
        1045810,
        connection=connection,
        http=http,
        gateway=_gateway(connection),
        standard=STANDARD,
        cutoff=CUTOFF,
        write=True,
        store=store,
    )
    second = extract_headcount(
        1045810,
        connection=connection,
        http=http,
        gateway=_gateway(connection),
        standard=STANDARD,
        cutoff=CUTOFF,
        write=True,
        store=store,
    )
    assert first.status == "resolved" and second.status == "already_recorded"
    assert len(connection.headcount_rows) == 1


def test_probe_mode_enumerates_and_selects_but_writes_no_fact_and_no_raw_object() -> None:
    connection, store = FakeConnection(), FakeStore()
    outcome = extract_headcount(
        1045810,
        connection=connection,
        http=_fixture_edgar(),
        gateway=_gateway(connection),
        standard=STANDARD,
        cutoff=CUTOFF,
        write=False,
        store=store,
    )
    assert outcome.status == "resolved" and outcome.value == 42000
    assert connection.headcount_rows == [] and connection.raw_rows == [] and store.stored == []
    assert len(connection.ledger_rows) == 2  # the vendor calls are still capacity spent


def test_a_filing_after_the_cutoff_is_not_knowable() -> None:
    connection = FakeConnection()
    early = datetime(2025, 12, 1, tzinfo=UTC)
    outcome = extract_headcount(
        1730168,
        connection=connection,
        http=_fixture_edgar(),
        gateway=_gateway(connection),
        standard=STANDARD,
        cutoff=early,
        write=True,
        store=FakeStore(),
    )
    assert outcome.status == "no_annual_filing"


def test_capacity_exhaustion_defers_the_cell_instead_of_failing_the_run() -> None:
    connection = FakeConnection()
    gateway = SourceGateway(
        connection,
        caller="t",
        capacities={"sec": SourceCapacity("sec", 1.0, 100, 1)},
        clock=lambda: 0.0,
        sleep=lambda _s: None,
        now=lambda: CUTOFF,
    )
    gateway.call("sec", "warm", lambda: None)  # spends the whole daily budget
    outcome = extract_headcount(
        1045810,
        connection=connection,
        http=_fixture_edgar(),
        gateway=gateway,
        standard=STANDARD,
        cutoff=CUTOFF,
        write=True,
        store=FakeStore(),
    )
    assert outcome.status == "deferred_capacity"


# --- the run over a universe ----------------------------------------------------------------


def test_run_over_a_universe_plans_only_open_cells_and_persists_the_report(monkeypatch) -> None:
    connection = FakeConnection({1045810: [(EXTRACTION_SOURCE, CUTOFF - timedelta(days=10))]})  # NVDA already cited
    issuers = [_issuer(1045810, "NVDA"), _issuer(59478, "LLY"), _issuer(1730168, "AVGO")]
    monkeypatch.setattr(backfill_module, "universe_issuers", lambda _c, _u: issuers)
    lines: list[str] = []

    report = run_standard_backfill(
        connection,
        universe="universe-list:qqq",
        standard_name="employees_total",
        cutoff=CUTOFF,
        mode="backfill",
        http=_fixture_edgar(),
        gateway=_gateway(connection),
        store=FakeStore(),
        log=lines.append,
    )

    assert report.issuers == 3 and report.open == 2 and dict(report.open_by_reason) == {"no_fact": 2}
    assert dict(report.outcomes) == {"needs_model_selection": 1, "resolved": 1}
    assert [r[0] for r in connection.headcount_rows] == [1730168]
    assert connection.commits >= 3  # one per cell plus the report
    metrics = {r[1]: r[2] for r in connection.health_rows}
    assert metrics["employees_total:universe-list:qqq:backfill:resolved"] == 1
    assert metrics["employees_total:universe-list:qqq:backfill:open_cells"] == 2
    assert any("LLY" in line and "needs_model_selection" in line for line in lines)


def test_probe_run_writes_only_the_ledger_and_the_report(monkeypatch) -> None:
    connection = FakeConnection()
    monkeypatch.setattr(backfill_module, "universe_issuers", lambda _c, _u: [_issuer(1045810, "NVDA")])
    report = run_standard_backfill(
        connection,
        universe="topt",
        standard_name="employees_total",
        cutoff=CUTOFF,
        mode="probe",
        http=_fixture_edgar(),
        gateway=_gateway(connection),
        store=FakeStore(),
        log=lambda _l: None,
    )
    assert dict(report.outcomes) == {"resolved": 1}
    assert connection.headcount_rows == [] and connection.raw_rows == []
    assert connection.ledger_rows and connection.health_rows


def test_candidate_records_are_frozen_values() -> None:
    candidate = FilingCandidate(1, None, "s", False)
    with pytest.raises(Exception):
        candidate.value = 2  # type: ignore[misc]
