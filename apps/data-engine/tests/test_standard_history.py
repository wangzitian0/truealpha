"""The month-end history mode of the standards backfill (#1137).

The backtest reads headcount with `knowable_at <= cutoff`. The weekly backfill runs at "now"
only, so every cutoff before the first extracted filing finds no headcount. The history mode
runs the same backfill at month-end cutoffs for the last N months, derived from the tick.

The DB-backed tests run on a real Postgres. A wrapper turns `commit` into a no-op, so every
row stays in the test transaction and the rollback at the end removes it. The fact table is
append-only: a committed test row could not be deleted.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import dagster as dg
import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.production_topt.headcount import PostgresHeadcountExtractor
from data_engine.datahub.standards import backfill as backfill_module
from data_engine.datahub.standards.planner import UniverseIssuer
from data_engine.sources import llm
from data_engine.sources.gateway import SourceCapacity, SourceGateway
from truealpha_contracts.models import RawCapture, RawIngestionEnvelope, RawObjectRef

TICK = datetime(2026, 10, 12, 9, 7, tzinfo=UTC)  # a Monday
MONTHS = 36
UNIVERSE = "topt"
STANDARD = "employees_total"


# --- the month-end cutoffs --------------------------------------------------------------------


def _cutoffs():
    from data_engine.datahub.standards.history import month_end_cutoffs

    return month_end_cutoffs


def test_cutoffs_are_the_last_month_ends_at_or_before_the_tick_oldest_first() -> None:
    cutoffs = _cutoffs()(TICK, 36)
    assert len(cutoffs) == 36
    assert cutoffs[0] == datetime(2023, 10, 31, 23, 59, 59, tzinfo=UTC)
    assert cutoffs[-1] == datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC)
    assert cutoffs == sorted(cutoffs)
    assert len({(c.year, c.month) for c in cutoffs}) == 36


def test_cutoffs_move_with_the_tick_and_never_come_from_a_literal_date() -> None:
    later = _cutoffs()(datetime(2027, 1, 4, 9, 7, tzinfo=UTC), 36)
    assert later[0] == datetime(2024, 1, 31, 23, 59, 59, tzinfo=UTC)
    assert later[-1] == datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)


def test_a_month_end_after_the_tick_is_not_a_cutoff() -> None:
    """The tick falls on 30 September, before that day ends: September has not closed."""
    cutoffs = _cutoffs()(datetime(2026, 9, 30, 9, 7, tzinfo=UTC), 2)
    assert cutoffs == [
        datetime(2026, 7, 31, 23, 59, 59, tzinfo=UTC),
        datetime(2026, 8, 31, 23, 59, 59, tzinfo=UTC),
    ]


def test_february_of_a_leap_year_ends_on_the_twenty_ninth() -> None:
    cutoffs = _cutoffs()(datetime(2024, 3, 5, 9, 7, tzinfo=UTC), 2)
    assert cutoffs == [
        datetime(2024, 1, 31, 23, 59, 59, tzinfo=UTC),
        datetime(2024, 2, 29, 23, 59, 59, tzinfo=UTC),
    ]


def test_a_tick_in_another_zone_is_read_in_utc() -> None:
    from datetime import timedelta, timezone

    tick = datetime(2026, 10, 1, 4, 0, tzinfo=timezone(timedelta(hours=8)))  # 2026-09-30 20:00 UTC
    assert _cutoffs()(tick, 1) == [datetime(2026, 8, 31, 23, 59, 59, tzinfo=UTC)]


def test_a_naive_tick_and_an_out_of_range_month_count_are_refused() -> None:
    from data_engine.datahub.standards.history import MAX_HISTORY_MONTHS

    with pytest.raises(ValueError, match="timezone-aware"):
        _cutoffs()(datetime(2026, 10, 12, 9, 7), 3)
    with pytest.raises(ValueError, match="months"):
        _cutoffs()(TICK, 0)
    with pytest.raises(ValueError, match="months"):
        _cutoffs()(TICK, MAX_HISTORY_MONTHS + 1)


# --- the fixture: two issuers, four 10-K filings ---------------------------------------------


@dataclass(frozen=True)
class Filing:
    cik: int
    filed: date
    accession: str  # with dashes, as EDGAR writes it
    total: int
    other: int

    @property
    def html(self) -> bytes:
        year = self.filed.year - 1
        return (
            "<html><body><p>"
            f"As of December 31, {year}, we employed approximately {self.total:,} people worldwide. "
            f"We employed approximately {self.other:,} people in pharmaceutical research and development activities."
            "</p></body></html>"
        ).encode()


CIK_A, CIK_B = 9_900_001, 9_900_002
# Issuer A: three annual filings. The stub model picks the larger figure.
A_FILINGS = [
    Filing(CIK_A, date(2024, 2, 21), "0009900001-24-000001", 40_000, 11_000),
    Filing(CIK_A, date(2025, 2, 19), "0009900001-25-000001", 41_000, 11_500),
    Filing(CIK_A, date(2026, 2, 18), "0009900001-26-000001", 42_000, 12_000),
]
# Issuer B: one filing. The stub model declines it, so the cell stays open at every later cutoff.
B_FILINGS = [Filing(CIK_B, date(2025, 2, 19), "0009900002-25-000001", 20_000, 6_000)]
ALL_FILINGS = [*A_FILINGS, *B_FILINGS]
ISSUERS = [
    UniverseIssuer("issuer:cik:0009900001", "HISTA", "listing:xnas:hista", CIK_A),
    UniverseIssuer("issuer:cik:0009900002", "HISTB", "listing:xnas:histb", CIK_B),
]


class _Response:
    def __init__(self, *, json_body: Any = None, content: bytes = b"") -> None:
        self._json = json_body
        self.content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._json


class FakeEdgar:
    """EDGAR as fixtures: one submissions index per CIK, newest filing first, one document each."""

    def __init__(self, filings: list[Filing]) -> None:
        self.filings = filings
        self.urls: list[str] = []

    def get(self, url: str) -> _Response:
        self.urls.append(url)
        if "/submissions/CIK" in url:
            cik = int(url.rsplit("CIK", 1)[1].removesuffix(".json"))
            rows = sorted((f for f in self.filings if f.cik == cik), key=lambda f: f.filed, reverse=True)
            return _Response(
                json_body={
                    "filings": {
                        "recent": {
                            "form": ["10-K" for _ in rows],
                            "filingDate": [f.filed.isoformat() for f in rows],
                            "accessionNumber": [f.accession for f in rows],
                            "primaryDocument": [f"doc-{f.accession}.htm" for f in rows],
                        }
                    }
                }
            )
        for filing in self.filings:
            if filing.accession.replace("-", "") in url:
                return _Response(content=filing.html)
        raise AssertionError(f"unexpected url {url}")


class FakeStore:
    def store(self, capture: RawCapture) -> RawIngestionEnvelope:
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


class _NoCommit:
    """A connection whose `commit` does nothing, so the test's rollback removes every row."""

    def __init__(self, connection: psycopg.Connection) -> None:
        self._connection = connection

    def commit(self) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        yield _NoCommit(active)
    finally:
        active.rollback()
        active.close()


class StubModel:
    """The provider transport. It records every question it is asked.

    Issuer HISTB gets a decline. Any other issuer gets the largest candidate.
    """

    def __init__(self) -> None:
        self.asks: list[tuple[str, int | None]] = []

    def __call__(self, url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        user = json.loads(body)["messages"][1]["content"]
        ticker = re.search(r"Issuer: (\w+) ", user).group(1)  # type: ignore[union-attr]
        values = [int(v.replace(",", "")) for v in re.findall(r"^\[\d+\] ([\d,]+):", user, flags=re.M)]
        if ticker == "HISTB":
            decision: dict[str, Any] = {"value": None, "candidate_index": None, "reason": "both are subsets"}
            self.asks.append((ticker, None))
        else:
            chosen = max(values)
            decision = {"value": chosen, "candidate_index": values.index(chosen), "reason": "company-wide"}
            self.asks.append((ticker, chosen))
        payload = {
            "choices": [{"message": {"content": json.dumps(decision)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            "model": "glm-test",
        }
        return 200, json.dumps(payload).encode()


def _gateway(connection: Any) -> SourceGateway:
    return SourceGateway(
        connection,
        caller="test-history",
        capacities={"sec": SourceCapacity("sec", 1.0, 100_000, 1_000_000)},
        clock=lambda: 0.0,
        sleep=lambda _s: None,
        now=lambda: TICK,
    )


@dataclass
class Runs:
    first: Any
    second: Any
    asks_after_first: list[tuple[str, int | None]]
    asks_after_second: list[tuple[str, int | None]]


def _run_twice(connection: Any, monkeypatch: pytest.MonkeyPatch) -> Runs:
    from data_engine.datahub.standards.history import run_standard_history

    leftovers = connection.execute(
        "select count(*) from staging.issuer_headcount_facts where cik = any(%s)", ([CIK_A, CIK_B],)
    ).fetchone()[0]
    assert leftovers == 0, "the fixture CIKs must start without facts"
    model = StubModel()
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_model", "glm-test")
    monkeypatch.setattr(llm, "_gateway_transport", model)
    monkeypatch.setattr(backfill_module, "universe_issuers", lambda _c, _u: ISSUERS)
    edgar = FakeEdgar(ALL_FILINGS)

    def once() -> Any:
        return run_standard_history(
            connection,
            universe=UNIVERSE,
            standard_name=STANDARD,
            tick=TICK,
            months=MONTHS,
            http=edgar,
            gateway=_gateway(connection),
            store=FakeStore(),
            log=lambda _line: None,
        )

    first = once()
    asks_after_first = list(model.asks)
    second = once()
    return Runs(first, second, asks_after_first, list(model.asks))


def _newest(filings: list[Filing], cik: int, cutoff: datetime) -> Filing | None:
    eligible = [f for f in filings if f.cik == cik and f.filed <= cutoff.date()]
    return max(eligible, key=lambda f: f.filed, default=None)


# --- the DB-backed acceptance checks ----------------------------------------------------------


def test_each_cutoff_reads_the_newest_filing_filed_at_or_before_it(connection, monkeypatch) -> None:
    runs = _run_twice(connection, monkeypatch)

    visited = [result.cutoff for result in runs.first.cutoffs]
    assert visited == _cutoffs()(TICK, MONTHS)
    seen_statuses: Counter[str] = Counter()
    for result in runs.first.cutoffs:
        for cell in result.report.cells:
            if cell["status"] not in ("resolved", "model_declined", "no_annual_filing"):
                pytest.fail(f"unexpected outcome {cell['status']} at {result.cutoff}: {cell['detail']}")
            expected = _newest(ALL_FILINGS, cell["cik"], result.cutoff)
            assert cell["filing_date"] == (expected.filed.isoformat() if expected else None), (
                f"cutoff {result.cutoff.date()} cik {cell['cik']} read the wrong filing"
            )
            seen_statuses[cell["status"]] += 1
    assert seen_statuses["resolved"] == 3
    assert seen_statuses["model_declined"] == 20
    assert seen_statuses["no_annual_filing"] == 4 + 16

    # What the backtest sees: the reader at each month end returns the filing then newest.
    reader = PostgresHeadcountExtractor(connection)
    for cutoff in visited:
        expected = _newest(ALL_FILINGS, CIK_A, cutoff)
        fact = reader(CIK_A, cutoff.date())
        if expected is None:
            assert fact is None, f"{cutoff.date()} has a headcount before the first filing"
        else:
            assert fact is not None, f"{cutoff.date()} has no headcount"
            assert fact.value == Decimal(expected.total)
            assert fact.knowable_at == datetime.combine(expected.filed, datetime.min.time(), tzinfo=UTC)
    assert reader(CIK_B, visited[-1].date()) is None  # a declined filing lands no fact


def test_the_model_is_asked_once_per_filing_and_a_second_run_asks_zero_times(connection, monkeypatch) -> None:
    runs = _run_twice(connection, monkeypatch)

    assert Counter(runs.asks_after_first) == {
        ("HISTA", 40_000): 1,
        ("HISTA", 41_000): 1,
        ("HISTA", 42_000): 1,
        ("HISTB", None): 1,
    }
    assert runs.first.counts() == {
        "cutoffs_visited": 36,
        "cells_attempted": 43,
        "cells_failed": 0,
        "extractions_asked": 4,
        "replays": 19,
        "declines": 20,
        "rows_written": 3,
    }
    # The second run replays the declined filing at each of its 20 visits and asks nobody.
    assert runs.asks_after_second == runs.asks_after_first
    assert runs.second.counts()["extractions_asked"] == 0
    assert runs.second.counts()["replays"] == 20
    assert runs.second.counts()["rows_written"] == 0
    stored = connection.execute(
        "select count(*) from staging.model_invocations where subject_cik = any(%s)", ([CIK_A, CIK_B],)
    ).fetchone()[0]
    assert stored == 4
    facts = connection.execute(
        "select count(*) from staging.issuer_headcount_facts where cik = any(%s)", ([CIK_A, CIK_B],)
    ).fetchone()[0]
    assert facts == 3


def test_no_written_row_is_knowable_after_the_cutoff_that_wrote_it(connection, monkeypatch) -> None:
    runs = _run_twice(connection, monkeypatch)

    checked = 0
    for result in runs.first.cutoffs:
        fact_ids = [cell["fact_id"] for cell in result.report.cells if cell["fact_id"] is not None]
        if not fact_ids:
            continue
        rows = connection.execute(
            "select id, knowable_at from staging.issuer_headcount_facts where id = any(%s)", (fact_ids,)
        ).fetchall()
        assert len(rows) == len(fact_ids)
        for fact_id, knowable_at in rows:
            assert knowable_at <= result.cutoff, f"fact {fact_id} is knowable after cutoff {result.cutoff}"
            checked += 1
    assert checked == 3
    total = connection.execute(
        "select count(*) from staging.issuer_headcount_facts where cik = any(%s)", ([CIK_A, CIK_B],)
    ).fetchone()[0]
    assert total == checked, "a row exists that no cutoff reported"


# --- the guard against a run that is green while empty ---------------------------------------


def _report_with(outcomes: dict[str, int]) -> Any:
    from data_engine.datahub.standards.backfill import BackfillReport
    from data_engine.datahub.standards.history import CutoffResult, HistoryReport

    backfill = BackfillReport(universe=UNIVERSE, standard=STANDARD, mode="backfill", cutoff=TICK)
    backfill.outcomes.update(outcomes)
    for status, count in outcomes.items():
        for _ in range(count):
            backfill.cells.append(
                {"status": status, "model_replayed": None, "fact_id": 10 if status == "resolved" else None}
            )
    return HistoryReport(
        universe=UNIVERSE,
        standard=STANDARD,
        tick=TICK,
        cutoffs=[CutoffResult(cutoff=TICK, report=backfill)],
    )


def test_a_run_where_every_attempt_failed_raises() -> None:
    from data_engine.datahub.standards.history import HistoryFailure, raise_if_every_attempt_failed

    with pytest.raises(HistoryFailure, match="3 of 3"):
        raise_if_every_attempt_failed(_report_with({"error": 2, "deferred_capacity": 1}))


def test_a_run_with_one_landed_or_declined_attempt_or_none_does_not_raise() -> None:
    from data_engine.datahub.standards.history import raise_if_every_attempt_failed

    raise_if_every_attempt_failed(_report_with({"error": 2, "resolved": 1}))
    raise_if_every_attempt_failed(_report_with({"error": 2, "model_declined": 1}))
    raise_if_every_attempt_failed(_report_with({}))


# --- registration in the standards lane -----------------------------------------------------


def test_the_history_job_is_deployed_in_the_standards_lane() -> None:
    from data_engine.dagster_defs import defs
    from data_engine.lanes import standards

    job = defs.get_job_def(standards.STANDARD_HISTORY_JOB_NAME)
    assert [node.name for node in job.graph.node_defs] == ["run_standard_history"]
    assert standards.STANDARD_HISTORY_JOB_NAME in {item.name for item in standards.defs.jobs or ()}


def test_the_history_schedule_is_a_running_weekly_schedule_clear_of_the_backfill_day() -> None:
    from data_engine.dagster_defs import defs
    from data_engine.lanes import standards

    schedule = defs.get_schedule_def("standard_history_schedule")
    assert schedule.job.name == standards.STANDARD_HISTORY_JOB_NAME
    assert schedule.default_status == dg.DefaultScheduleStatus.RUNNING
    assert schedule.cron_schedule == standards.STANDARD_HISTORY_CRON
    minute, hour, day_of_month, month, day_of_week = standards.STANDARD_HISTORY_CRON.split()
    assert (day_of_month, month) == ("*", "*") and day_of_week.isdigit(), "the cadence is one fixed weekday"
    # Another weekday than the weekly backfill, so the two runs never share one daily SEC budget.
    assert day_of_week != standards.STANDARD_BACKFILL_CRON.split()[4]


def test_every_history_run_request_names_its_tick_and_is_a_valid_run_config() -> None:
    from data_engine.lanes import standards

    context = dg.build_schedule_context(scheduled_execution_time=datetime(2026, 10, 12, 9, 7, tzinfo=UTC))
    requests = list(standards.standard_history_schedule.evaluate_tick(context).run_requests)
    assert [request.run_key for request in requests] == [
        f"history:2026-10-12T09:07:00+00:00:{universe}" for universe in standards.STANDARD_HISTORY_UNIVERSES
    ]
    for request, universe in zip(requests, standards.STANDARD_HISTORY_UNIVERSES, strict=True):
        assert set(request.run_config["ops"]) == {"run_standard_history"}
        config = standards.StandardHistoryConfig(**request.run_config["ops"]["run_standard_history"]["config"])
        assert config.executed_at == "2026-10-12T09:07:00+00:00"
        assert config.universe == universe
        assert config.months == standards.STANDARD_HISTORY_MONTHS == 36
        assert dg.validate_run_config(standards.standard_history_backfill_pipeline_job, request.run_config)


def test_the_manual_path_launches_the_same_job_with_an_explicit_config() -> None:
    """The manual path is a Dagster launch (launchpad or GraphQL) with this config.

    `pipeline_trigger_requests` cannot carry it: its CHECK admits three capture job names.
    """
    from data_engine.lanes import standards

    run_config = {
        "ops": {
            "run_standard_history": {
                "config": {
                    "executed_at": "2026-10-12T09:07:00+00:00",
                    "universe": "topt",
                    "standard": "employees_total",
                    "months": 12,
                }
            }
        }
    }
    assert dg.validate_run_config(standards.standard_history_backfill_pipeline_job, run_config)
    assert standards.StandardHistoryConfig(**run_config["ops"]["run_standard_history"]["config"]).months == 12


def test_the_standards_with_a_history_run_are_registered_standards() -> None:
    from data_engine.lanes import standards
    from truealpha_contracts.standards import STANDARDS

    assert standards.STANDARDS_WITH_HISTORY, "no standard has a history run"
    assert set(standards.STANDARDS_WITH_HISTORY) <= set(STANDARDS)
    assert standards.history_standards("") == standards.STANDARDS_WITH_HISTORY
    assert standards.history_standards("employees_total") == ("employees_total",)
    with pytest.raises(ValueError, match="unknown standard"):
        standards.history_standards("employees")


def test_the_history_op_reports_counts_only_and_fails_when_every_attempt_failed(monkeypatch) -> None:
    from data_engine.datahub.standards.history import HistoryFailure
    from data_engine.lanes import standards

    class _Connection:
        def __enter__(self) -> _Connection:
            return self

        def __exit__(self, *_exc: Any) -> bool:
            return False

    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: _Connection())
    config = standards.StandardHistoryConfig(executed_at="2026-10-12T09:07:00+00:00", universe="topt")

    monkeypatch.setattr(
        standards, "_run_standard_history", lambda *_a, **_k: _report_with({"resolved": 2, "model_declined": 1})
    )
    summary = json.loads(standards.run_standard_history(dg.build_op_context(), config))
    assert summary == [
        {
            "universe": "topt",
            "standard": "employees_total",
            "cutoffs_visited": 1,
            "cells_attempted": 3,
            "cells_failed": 0,
            "extractions_asked": 0,
            "replays": 0,
            "declines": 1,
            "rows_written": 2,
        }
    ]

    monkeypatch.setattr(standards, "_run_standard_history", lambda *_a, **_k: _report_with({"error": 4}))
    with pytest.raises(HistoryFailure):
        standards.run_standard_history(dg.build_op_context(), config)
