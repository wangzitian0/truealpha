"""Nightly verdicts (#876 W1) and the model-provider key probe (#876 W2).

Every check asserted here through the deployed job (`execute_in_process` on the job the lane
registers), with the collaborators faked and the verdict writer captured: a red check must
write `ok = false` AND leave the Dagster run red; a green one writes `ok = true`; a check whose
name no lane declares writes nothing. The probe is driven through the real source gateway with
`urllib.request.urlopen` faked, so the ledger row it leaves is the one production leaves.
"""

from __future__ import annotations

import io
import json
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

import dagster as dg
import psycopg
import pytest
from data_engine.config import settings
from data_engine.lanes import quality, standards
from data_engine.quality import model_key_health, nightly_verdicts
from data_engine.sources import gateway, llm
from truealpha_runtime.testing import skip_or_fail

TICK = "2026-09-16T00:15:00+00:00"
FAKE_KEY = "sk-test-not-a-real-key-0123456789"


@pytest.fixture
def written(monkeypatch) -> list[dict[str, Any]]:
    """Every verdict the checks record, instead of a database row."""
    rows: list[dict[str, Any]] = []

    def record(name: str, **kwargs: Any) -> None:
        rows.append({"check": name, **kwargs})

    monkeypatch.setattr(nightly_verdicts, "record", record)
    return rows


# --- the recorder ----------------------------------------------------------------------


class _Sink:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.rows: list[tuple] = []
        self.kwargs: dict[str, Any] = {}

    def __call__(self, *_args: Any, **kwargs: Any) -> _Sink:
        self.kwargs = kwargs
        return self

    def __enter__(self) -> _Sink:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple) -> None:
        if self.fail:
            raise psycopg.OperationalError("connection to server failed")
        assert "insert into mart.nightly_verdicts" in sql
        self.rows.append(params)


def test_a_green_check_records_ok_on_its_own_autocommit_connection(monkeypatch) -> None:
    sink = _Sink()
    monkeypatch.setattr(psycopg, "connect", sink)
    tick = datetime(2026, 9, 16, 0, 15, tzinfo=UTC)
    with nightly_verdicts.verdict(
        "output_invariants", registered={"output_invariants"}, run_id="r" * 36, tick=tick
    ) as o:
        o.summary = "19 held, 0 deferred, 0 empty"
    assert sink.kwargs == {"autocommit": True}, "the verdict must survive the check's own transaction"
    assert sink.rows == [("output_invariants", tick, True, "19 held, 0 deferred, 0 empty", "r" * 36)]


def test_a_failing_check_records_red_and_still_raises(monkeypatch) -> None:
    sink = _Sink()
    monkeypatch.setattr(psycopg, "connect", sink)
    with (
        pytest.raises(dg.Failure, match="suite failed"),
        nightly_verdicts.verdict(
            "output_invariants", registered={"output_invariants"}, run_id="abcdef12-x", tick=None
        ) as o,
    ):
        o.summary = "exit 1: 1 failed (peg_bounded)"
        raise dg.Failure("suite failed")
    ((name, ran_at, ok, summary, run_id),) = sink.rows
    assert (name, ok, summary, run_id) == (
        "output_invariants",
        False,
        "failed: exit 1: 1 failed (peg_bounded)",
        "abcdef12-x",
    )
    # No tick: a manual run is dated when it completed, which is a measurement, not a default.
    assert abs((datetime.now(UTC) - ran_at).total_seconds()) < 60


def test_a_crash_is_summarized_by_its_type_never_its_text(monkeypatch) -> None:
    """A connection error's text names its server; the summary is published on /health."""
    sink = _Sink()
    monkeypatch.setattr(psycopg, "connect", sink)
    with (
        pytest.raises(psycopg.OperationalError),
        nightly_verdicts.verdict("theme_purity@topt", registered={"theme_purity@topt"}, run_id="0123abcd-9", tick=None),
    ):
        raise psycopg.OperationalError('connection to server at "192.0.2.10", port 5432 failed')
    ((_, _, ok, summary, _),) = sink.rows
    assert ok is False
    assert summary == "failed: OperationalError — see Dagster run 0123abcd"
    assert "192.0.2.10" not in summary


def test_a_red_verdict_that_cannot_be_written_does_not_hide_the_checks_own_failure(monkeypatch) -> None:
    monkeypatch.setattr(psycopg, "connect", _Sink(fail=True))
    with (
        pytest.raises(dg.Failure, match="the real failure"),
        nightly_verdicts.verdict("output_invariants", registered={"output_invariants"}, run_id="r", tick=None),
    ):
        raise dg.Failure("the real failure")


def test_a_green_verdict_that_cannot_be_written_turns_the_run_red(monkeypatch) -> None:
    """Otherwise the watchdog reads yesterday's row and the loss is invisible."""
    monkeypatch.setattr(psycopg, "connect", _Sink(fail=True))
    with (
        pytest.raises(psycopg.OperationalError),
        nightly_verdicts.verdict("output_invariants", registered={"output_invariants"}, run_id="r", tick=None),
    ):
        pass


def test_an_undeclared_name_records_nothing(written) -> None:
    """A manual run over a universe no schedule ticks is not a nightly check."""
    with nightly_verdicts.verdict(
        "theme_purity@universe:adhoc", registered=standards.NIGHTLY_VERDICTS, run_id="r", tick=None
    ):
        pass
    assert written == []


def test_a_malformed_tick_tag_dates_the_verdict_by_completion_instead_of_failing() -> None:
    """Raising here would fail the run before `verdict()` is entered, leaving no row at all."""
    context = dg.build_op_context(run_tags={nightly_verdicts.TICK_TAG: "yesterday-ish"})
    assert nightly_verdicts.tick_of(context) is None
    context = dg.build_op_context(run_tags={nightly_verdicts.TICK_TAG: "2026-09-16T00:15:00"})
    assert nightly_verdicts.tick_of(context) == datetime(2026, 9, 16, 0, 15, tzinfo=UTC)
    assert nightly_verdicts.tick_of(dg.build_op_context()) is None


def test_a_summary_is_one_bounded_line() -> None:
    assert nightly_verdicts.bounded("a\nb   c") == "a b c"
    assert len(nightly_verdicts.bounded("x" * 1000)) == nightly_verdicts.SUMMARY_LIMIT
    assert nightly_verdicts.bounded("  ") == "no summary"


def test_an_invalid_name_is_refused_before_the_database_is() -> None:
    with pytest.raises(ValueError, match="not a verdict name"):
        nightly_verdicts.record("Theme Purity", ran_at=datetime.now(UTC), ok=True, summary="x", run_id="r")


# --- the quality lane, through its deployed jobs ---------------------------------------


def _suite(monkeypatch, *, exit_code: int, printed: str) -> None:
    def main(_argv: list[str]) -> int:
        print(printed)
        return exit_code

    monkeypatch.setattr(quality, "_suite_path", lambda: "suite.py")
    monkeypatch.setattr(quality.runpy, "run_path", lambda *_a, **_k: {"main": main})


def _surfaces(monkeypatch, *, mismatched: bool) -> None:
    from data_engine.quality import surface_proof

    run = "capture-run:" + "a" * 64
    verdicts = (
        surface_proof.SurfaceVerdict("rankings", "strategy-run-repository.ts", run, run),
        surface_proof.SurfaceVerdict(
            "themes", "theme-purity.ts", run, "capture-run:" + "b" * 64 if mismatched else run
        ),
    )
    monkeypatch.setattr(surface_proof, "prove", lambda *_a, **_k: verdicts)
    # Every universe settled: the wait and the snapshot's own check find nothing in flight.
    monkeypatch.setattr(quality, "settling", lambda *_a, **_k: {})
    monkeypatch.setattr(psycopg, "connect", _Sink())


def test_the_nightly_job_records_both_verdicts_green_at_its_tick(monkeypatch, written) -> None:
    _suite(
        monkeypatch, exit_code=0, printed="  ok       peg_bounded: 20 row(s) examined\n  DEFERRED gppe: 1 violation(s)"
    )
    _surfaces(monkeypatch, mismatched=False)
    result = quality.output_invariants_job.execute_in_process(tags={nightly_verdicts.TICK_TAG: TICK})
    assert result.success
    by_name = {row["check"]: row for row in written}
    assert set(by_name) == {"output_invariants", "report_surface_proof"}
    assert all(row["ok"] for row in written)
    assert all(row["ran_at"] == datetime.fromisoformat(TICK) for row in written), "ran_at is the tick"
    assert by_name["output_invariants"]["summary"] == "1 held, 1 deferred, 0 empty"
    assert by_name["report_surface_proof"]["summary"] == "2/2 surfaces serve the governed head"


def test_a_violated_invariant_is_a_red_verdict_and_a_red_run(monkeypatch, written) -> None:
    _suite(
        monkeypatch,
        exit_code=1,
        printed="  ok       gppe: 20 row(s) examined\ninvariant failed: peg_bounded: 2 violation(s) — PEG is bounded. Offending rows: NVDA 999",
    )
    _surfaces(monkeypatch, mismatched=True)
    result = quality.output_invariants_job.execute_in_process(
        tags={nightly_verdicts.TICK_TAG: TICK}, raise_on_error=False
    )
    assert not result.success, "the Dagster run stays red"
    by_name = {row["check"]: row for row in written}
    assert by_name["output_invariants"]["ok"] is False
    assert (
        by_name["output_invariants"]["summary"] == "failed: exit 1: 1 failed (peg_bounded); 1 held, 0 deferred, 0 empty"
    )
    assert "NVDA" not in by_name["output_invariants"]["summary"], "offending rows stay in the run log"
    assert by_name["report_surface_proof"]["ok"] is False
    assert (
        by_name["report_surface_proof"]["summary"] == "failed: 1/2 surfaces serve the governed head; mismatched: themes"
    )


def test_the_nightly_schedule_stamps_its_tick_on_the_run() -> None:
    tick = datetime(2026, 9, 16, 0, 15, tzinfo=UTC)
    request = quality.output_invariants_schedule(dg.build_schedule_context(scheduled_execution_time=tick))
    assert request.run_key == tick.isoformat()
    assert request.tags[nightly_verdicts.TICK_TAG] == tick.isoformat()


def test_the_confidence_report_records_a_verdict_per_universe(monkeypatch, written) -> None:
    from data_engine.datahub import confidence_report

    monkeypatch.setattr(psycopg, "connect", _Sink())
    monkeypatch.setattr(confidence_report, "compile_report", lambda *_a, **_k: None)
    for universe in quality.CONFIDENCE_REPORT_UNIVERSES:
        config = quality.ConfidenceReportConfig(executed_at="2026-09-16T00:45:00+00:00", universe=universe)
        quality.run_confidence_report(dg.build_op_context(), config)
    assert [(row["check"], row["ok"], row["summary"]) for row in written] == [
        (f"datahub_confidence_report@{universe}", True, "no governed head; no report")
        for universe in quality.CONFIDENCE_REPORT_UNIVERSES
    ]
    assert all(row["ran_at"] == datetime.fromisoformat("2026-09-16T00:45:00+00:00") for row in written)


def test_a_crashing_confidence_report_is_a_red_verdict(monkeypatch, written) -> None:
    from data_engine.datahub import confidence_report

    def crash(*_a: Any, **_k: Any) -> None:
        raise KeyError("families")

    monkeypatch.setattr(psycopg, "connect", _Sink())
    monkeypatch.setattr(confidence_report, "compile_report", crash)
    config = quality.ConfidenceReportConfig(executed_at="2026-09-16T00:45:00+00:00", universe="topt")
    with pytest.raises(KeyError):
        quality.run_confidence_report(dg.build_op_context(), config)
    assert [(row["check"], row["ok"]) for row in written] == [("datahub_confidence_report@topt", False)]


# --- the surface proof judges a settled head (2026-09-17) --------------------------------


class _Snapshot(_Sink):
    """The proof's connection: records how it was configured before anything was read."""

    def __init__(self) -> None:
        super().__init__()
        self.isolation_level = None
        self.read_only = None


def _proof_run(monkeypatch, written, *, pending: list[dict[str, str]], verdicts=None) -> tuple[list, list]:
    """Drive the proof op with `settling` answering from `pending` in turn (the last answer
    repeats); returns what `prove` was handed and every sleep the wait asked for."""
    from data_engine.quality import surface_proof

    run = "capture-run:" + "a" * 64
    answers = list(pending)
    proved: list = []
    slept: list = []

    def settling(connection, instance, *, now):
        assert isinstance(instance, dg.DagsterInstance), "the wait reads the run list of the proof's own instance"
        return dict(answers.pop(0) if len(answers) > 1 else answers[0])

    def prove(connection, *, executed_at, settling):
        proved.append({"connection": connection, "settling": dict(settling)})
        return verdicts or (
            surface_proof.SurfaceVerdict("rankings", "strategy-run-repository.ts", run, run, universe="topt"),
            surface_proof.SurfaceVerdict("themes", "theme-purity.ts", run, run, universe="topt"),
        )

    monkeypatch.setattr(quality, "settling", settling)
    monkeypatch.setattr(surface_proof, "prove", prove)
    monkeypatch.setattr(quality, "_sleep", slept.append)
    snapshot = _Snapshot()
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: snapshot)
    quality.run_report_surface_proof(dg.build_op_context(run_tags={nightly_verdicts.TICK_TAG: TICK}))
    return proved, slept


def test_the_proof_waits_while_a_tick_is_running_then_judges_the_settled_head(monkeypatch, written) -> None:
    """00:15 on 2026-09-17: the QQQ tick was still running. The proof now waits for it instead
    of racing its commit, and judges inside one repeatable-read snapshot."""
    tick = {"universe-list:qqq": "qqq_live_pipeline run 1a2b3c4d is started"}
    proved, slept = _proof_run(monkeypatch, written, pending=[tick, tick, {}])
    assert slept == [quality.SURFACE_SETTLE_POLL_SECONDS] * 2
    ((call),) = proved
    assert call["settling"] == {}, "judged once nothing was in flight"
    assert call["connection"].isolation_level == psycopg.IsolationLevel.REPEATABLE_READ
    assert call["connection"].read_only is True
    assert [(row["check"], row["ok"], row["summary"]) for row in written] == [
        ("report_surface_proof", True, "2/2 surfaces serve the governed head")
    ]


def test_a_universe_still_settling_when_the_wait_runs_out_is_named_in_progress_not_failed(monkeypatch, written) -> None:
    from datetime import timedelta

    from data_engine.quality import surface_proof

    monkeypatch.setattr(quality, "SURFACE_SETTLE_TIMEOUT", timedelta(0))
    run = "capture-run:" + "a" * 64
    why = "qqq_live_pipeline run 1a2b3c4d is started"
    verdicts = (
        surface_proof.SurfaceVerdict("rankings", "strategy-run-repository.ts", run, run, universe="topt"),
        surface_proof.SurfaceVerdict(
            "coverage [qqq]",
            "datahub-stats.ts",
            run,
            "capture-run:" + "b" * 64,
            universe="universe-list:qqq",
            in_progress=why,
        ),
    )
    proved, slept = _proof_run(monkeypatch, written, pending=[{"universe-list:qqq": why}], verdicts=verdicts)
    assert slept == [], "the wait is bounded"
    assert proved[0]["settling"] == {"universe-list:qqq": why}, "the snapshot is told what has not settled"
    assert [(row["check"], row["ok"], row["summary"]) for row in written] == [
        ("report_surface_proof", True, "1/2 surfaces serve the governed head; in progress: coverage [qqq]")
    ]


def test_a_mismatch_beside_an_in_progress_surface_still_fails_the_run(monkeypatch, written) -> None:
    from datetime import timedelta

    from data_engine.quality import surface_proof

    monkeypatch.setattr(quality, "SURFACE_SETTLE_TIMEOUT", timedelta(0))
    run = "capture-run:" + "a" * 64
    verdicts = (
        surface_proof.SurfaceVerdict("themes", "theme-purity.ts", run, None, universe="topt"),
        surface_proof.SurfaceVerdict(
            "coverage [qqq]", "datahub-stats.ts", run, None, universe="universe-list:qqq", in_progress="settling"
        ),
    )
    with pytest.raises(dg.Failure, match="MISMATCH themes"):
        _proof_run(monkeypatch, written, pending=[{"universe-list:qqq": "settling"}], verdicts=verdicts)
    ((row),) = written
    assert row["ok"] is False
    assert row["summary"] == (
        "failed: 0/2 surfaces serve the governed head; in progress: coverage [qqq]; mismatched: themes"
    )


def _add_run(instance: dg.DagsterInstance, job_name: str, status: dg.DagsterRunStatus, run_config=None) -> str:
    import uuid

    run_id = str(uuid.uuid4())
    instance.add_run(dg.DagsterRun(job_name=job_name, run_id=run_id, status=status, run_config=run_config or {}))
    return run_id


def test_the_runs_that_hold_a_universe_are_its_ticks_and_its_report_writers() -> None:
    from data_engine.lanes.capture import CANARY_JOB_NAME, QQQ_LIVE_JOB_NAME, TOPT_LIVE_JOB_NAME

    instance = dg.DagsterInstance.ephemeral()
    assert quality.runs_in_flight(instance) == {}

    qqq_tick = _add_run(instance, QQQ_LIVE_JOB_NAME, dg.DagsterRunStatus.STARTED)
    _add_run(instance, TOPT_LIVE_JOB_NAME, dg.DagsterRunStatus.SUCCESS)
    _add_run(instance, CANARY_JOB_NAME, dg.DagsterRunStatus.STARTED)  # no report surface follows the canary
    assert quality.runs_in_flight(instance) == {
        "universe-list:qqq": f"{QQQ_LIVE_JOB_NAME} run {qqq_tick[:8]} is started"
    }

    # A head-reports run a sensor has created but not yet submitted is committed to running;
    # its universe comes from its config, so one an operator launched without tags is waited on
    # too. (QUEUED counts the same way; a queued run cannot be built here without a code origin.)
    reports = _add_run(
        instance,
        standards.HEAD_REPORTS_JOB_NAME,
        dg.DagsterRunStatus.NOT_STARTED,
        {"ops": {"run_theme_purity": {"config": {"universe": "topt", "executed_at": TICK}}}},
    )
    assert quality.runs_in_flight(instance)["topt"] == (
        f"{standards.HEAD_REPORTS_JOB_NAME} run {reports[:8]} is not started"
    )
    assert dg.DagsterRunStatus.QUEUED in quality.UNFINISHED_RUN_STATUSES

    # An unfinished run from half a day ago is a dead worker, not a reason to wait.
    assert quality.runs_in_flight(instance, now=datetime.now(UTC) + quality.IN_FLIGHT_MAX_AGE) == {}

    # A backfill configured with no universe runs over the config's default one.
    instance = dg.DagsterInstance.ephemeral()
    _add_run(
        instance,
        standards.STANDARD_BACKFILL_JOB_NAME,
        dg.DagsterRunStatus.STARTING,
        {"ops": {"run_standard_backfill": {"config": {"executed_at": TICK}}}},
    )
    assert list(quality.runs_in_flight(instance)) == ["universe-list:qqq"]


def test_every_universe_the_proof_judges_has_a_tick_it_waits_for() -> None:
    """A universe whose tick the wait cannot see would be judged mid-commit again."""
    from data_engine.datahub.question_coverage import UNIVERSE_PREFIXES
    from data_engine.lanes.capture import TICKS

    watched = {tick.universe_head_kind or "topt" for tick in TICKS}
    assert set(UNIVERSE_PREFIXES) <= watched


# --- the standards lane's daily head reports -------------------------------------------


def test_head_reports_record_purity_and_coverage_per_universe(monkeypatch, written) -> None:
    from data_engine.datahub import question_coverage
    from data_engine.datahub.production_topt import theme_purity

    class _NoHead(_Sink):
        def execute(self, *_a: Any, **_k: Any) -> _NoHead:
            return self

        def fetchone(self) -> None:
            return None

        def commit(self) -> None:
            return None

    monkeypatch.setattr(psycopg, "connect", _NoHead())
    monkeypatch.setattr(question_coverage, "governed_head", lambda *_a, **_k: None)
    monkeypatch.setattr(question_coverage, "compile_report", lambda *_a, **_k: None)
    monkeypatch.setattr(theme_purity, "materialize_theme_purity", lambda *_a, **_k: ())
    tick = datetime(2026, 9, 16, 23, 30, tzinfo=UTC)
    for request in standards.head_reports_schedule.evaluate_tick(
        dg.build_schedule_context(scheduled_execution_time=tick)
    ).run_requests:
        result = standards.head_reports_pipeline_job.execute_in_process(run_config=request.run_config)
        assert result.success
    assert sorted(row["check"] for row in written) == sorted(standards.NIGHTLY_VERDICTS)
    assert all(row["ok"] and row["ran_at"] == tick for row in written)


def test_the_purity_summary_counts_rows_and_carries_no_value(monkeypatch, written) -> None:
    """The line is public: counts of (issuer, theme) rows, never a purity value."""
    from decimal import Decimal
    from types import SimpleNamespace

    from data_engine.datahub import question_coverage
    from data_engine.datahub.production_topt import theme_purity

    run = "capture-run:" + "c" * 64
    head = question_coverage.GovernedHead("universe:topt-us-2026-03-31", run, datetime(2026, 9, 16, 22, 45, tzinfo=UTC))
    rows = tuple(
        SimpleNamespace(entity_id=f"issuer:cik:{cik}", theme=theme, result=SimpleNamespace(value=value))
        for cik, theme, value in (
            (1, "ai", Decimal("0.8123")),
            (1, "cloud", None),
            (2, "ai", Decimal("0.4567")),
        )
    )

    class _Conn(_Sink):
        def commit(self) -> None:
            return None

    monkeypatch.setattr(psycopg, "connect", _Conn())
    monkeypatch.setattr(question_coverage, "governed_head", lambda *_a, **_k: head)
    monkeypatch.setattr(standards, "universe_issuers", lambda *_a, **_k: [])
    monkeypatch.setattr(theme_purity, "materialize_theme_purity", lambda *_a, **_k: rows)
    config = standards.StandardBackfillConfig(executed_at="2026-09-16T23:30:00+00:00", universe="topt")
    standards.run_theme_purity(dg.build_op_context(), config, "{}")
    ((row),) = written
    assert (row["check"], row["ok"]) == ("theme_purity@topt", True)
    assert row["summary"] == f"2/3 theme-purity rows published on {run[:24]}"
    assert "0.8123" not in row["summary"] and "0.4567" not in row["summary"]


def test_a_failing_purity_op_is_a_red_verdict_and_a_red_run(monkeypatch, written) -> None:
    from data_engine.datahub import question_coverage

    def crash(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("filing-extraction-model: HTTP 401")

    monkeypatch.setattr(psycopg, "connect", _Sink())
    monkeypatch.setattr(question_coverage, "governed_head", crash)
    # The run the pointer sensor launches: it recomputes, so the purity op is the first to ask
    # for the head (the fallback's start op would ask first, and fail before purity ran).
    request = standards.head_reports_request(
        standards.STANDARD_BACKFILL_UNIVERSES[0],
        "2026-09-16T23:30:00+00:00",
        run_key="head:test",
        only_if_stale=False,
    )
    result = standards.head_reports_pipeline_job.execute_in_process(run_config=request.run_config, raise_on_error=False)
    assert not result.success
    universe = standards.STANDARD_BACKFILL_UNIVERSES[0]
    assert [(row["check"], row["ok"]) for row in written] == [(f"theme_purity@{universe}", False)]
    assert written[0]["summary"].startswith("failed: RuntimeError — see Dagster run ")
    # Coverage never ran, so it wrote nothing: its verdict goes stale and the tool names it.


# --- the model-provider key probe (W2) --------------------------------------------------


class _Answer:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Answer:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def _provider(monkeypatch, answer: Any) -> list[urllib.request.Request]:
    """Fake the network under the real gateway: `answer` is an int (an HTTP error status),
    an exception to raise, or a body to return with 200."""
    asked: list[urllib.request.Request] = []

    def urlopen(request: urllib.request.Request, timeout: float) -> _Answer:
        asked.append(request)
        if isinstance(answer, int):
            raise urllib.error.HTTPError(
                request.full_url, answer, "error", {}, io.BytesIO(b'{"error": {"message": "token expired"}}')
            )
        if isinstance(answer, BaseException):
            raise answer
        return _Answer(answer)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(settings, "llm_api_key", FAKE_KEY)
    return asked


_OK_BODY = json.dumps(
    {
        "model": "glm-served",
        "choices": [{"message": {"content": '{"ok": true}'}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
    }
).encode()


@pytest.mark.parametrize(
    ("answer", "outcome", "status"),
    [
        (401, "auth-rejected", 401),
        (403, "auth-rejected", 403),
        (429, "provider-error", 429),
        (503, "provider-error", 503),
        (urllib.error.URLError("[Errno 8] nodename nor servname provided: provider.example"), "unreachable", None),
        (TimeoutError("timed out"), "unreachable", None),
        (b"<html>not json</html>", "provider-error", None),
        (_OK_BODY, "answered", None),
    ],
)
def test_the_probe_classifies_every_outcome_and_leaks_nothing(
    monkeypatch, call_ledger, answer, outcome, status
) -> None:
    _provider(monkeypatch, answer)
    with gateway.run_scope("dagster:probe-run"):
        health = model_key_health.probe()
    assert (health.outcome, health.status_code) == (outcome, status)
    assert health.ok is (outcome == "answered")
    for secret in (FAKE_KEY, FAKE_KEY[-10:], "provider.example", "token expired"):
        assert secret not in health.summary, "the verdict is public: no key, no host, no vendor text"
    if status is not None:
        assert f"HTTP {status}" in health.summary
    # One ledger row, under the watchdog's caller, attributed to the run that asked (#729).
    ((row),) = call_ledger
    assert (row.source, row.caller, row.run_key) == (llm.SOURCE, model_key_health.CALLER, "dagster:probe-run")
    assert row.ok is (outcome == "answered")


def test_no_seated_key_is_a_red_verdict_without_a_call(monkeypatch, call_ledger) -> None:
    asked = _provider(monkeypatch, _OK_BODY)
    monkeypatch.setattr(settings, "llm_api_key", "")
    health = model_key_health.probe()
    assert (health.ok, health.outcome) == (False, "not-configured")
    assert asked == [] and list(call_ledger) == []


def test_the_probe_is_one_minimal_ask_that_persists_nothing(monkeypatch, call_ledger) -> None:
    asked = _provider(monkeypatch, _OK_BODY)
    persisted: list[Any] = []
    monkeypatch.setattr(llm, "_replay", lambda *a, **k: persisted.append(a) or None)
    model_key_health.probe()
    (request,) = asked
    body = json.loads(request.data or b"{}")
    assert body["max_tokens"] == model_key_health.PROBE_TASK.max_tokens <= 16
    assert body["messages"][1]["content"] == "ping"
    assert request.get_header("Authorization") == f"Bearer {FAKE_KEY}"
    assert persisted == [], "persist=False: no replay lookup, no invocation row"


def test_a_revoked_key_is_a_red_run_and_a_red_verdict_through_the_deployed_job(
    monkeypatch, written, call_ledger
) -> None:
    """#832, end to end: the provider answers 401, the job goes red, the verdict says why."""
    _provider(monkeypatch, 401)
    tick = "2026-09-16T06:00:00+00:00"
    result = quality.model_key_health_job.execute_in_process(
        tags={nightly_verdicts.TICK_TAG: tick}, raise_on_error=False
    )
    assert not result.success
    ((row),) = written
    assert (row["check"], row["ok"], row["ran_at"]) == ("model_key_health", False, datetime.fromisoformat(tick))
    assert "auth-rejected" in row["summary"] and "HTTP 401" in row["summary"]
    assert FAKE_KEY not in row["summary"]
    ((ledger_row),) = call_ledger
    assert ledger_row.caller == model_key_health.CALLER
    assert ledger_row.run_key == f"dagster:{result.run_id}"


def test_a_working_key_is_a_green_verdict_through_the_deployed_job(monkeypatch, written) -> None:
    _provider(monkeypatch, _OK_BODY)
    result = quality.model_key_health_job.execute_in_process(tags={nightly_verdicts.TICK_TAG: TICK})
    assert result.success
    assert [(row["check"], row["ok"]) for row in written] == [("model_key_health", True)]


def test_the_probe_is_scheduled_daily_before_the_watchdog_reads_it() -> None:
    """deploy-freshness reads the verdicts at 07:00 UTC."""
    schedule = quality.defs.get_schedule_def("model_key_health_schedule")
    minute, hour, *rest = schedule.cron_schedule.split()
    assert rest == ["*", "*", "*"] and int(hour) < 7
    assert schedule.default_status == dg.DefaultScheduleStatus.RUNNING
    tick = datetime(2026, 9, 16, int(hour), int(minute), tzinfo=UTC)
    request = schedule(dg.build_schedule_context(scheduled_execution_time=tick))
    assert request.tags[nightly_verdicts.TICK_TAG] == tick.isoformat()


def test_every_declared_verdict_name_fits_the_column() -> None:
    from data_engine.lanes import nightly_verdict_names

    assert all(nightly_verdicts.is_valid_name(name) for name in nightly_verdict_names())


# --- persistence (real schema; skips without a local Postgres) ---------------------------


def _connection() -> psycopg.Connection:
    try:
        connection = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            raise
        pytest.skip("no local Postgres")
    (table,) = connection.execute("select to_regclass('mart.nightly_verdicts')").fetchone() or (None,)
    if table is None:
        connection.close()
        skip_or_fail("mart.nightly_verdicts missing (make db-migrate)")
    return connection


def test_verdicts_append_and_are_never_rewritten() -> None:
    """The recorder's own statement against the migrated table, inside a transaction that is
    rolled back: the table is append-only, so a committed test row could never be removed."""
    connection = _connection()
    try:
        tick = datetime(2026, 9, 16, 0, 15, tzinfo=UTC)
        for ok, summary in ((False, "failed: x"), (True, "ok")):
            connection.execute(nightly_verdicts.INSERT_SQL, ("output_invariants", tick, ok, summary, "test-run"))
        rows = connection.execute(
            "select ok, summary from mart.nightly_verdicts where dagster_run_id = 'test-run' order by verdict_id"
        ).fetchall()
        assert rows == [(False, "failed: x"), (True, "ok")]
        with pytest.raises(psycopg.errors.RaiseException), connection.transaction():
            connection.execute("update mart.nightly_verdicts set ok = true where dagster_run_id = 'test-run'")
        with pytest.raises(psycopg.errors.RaiseException), connection.transaction():
            connection.execute("delete from mart.nightly_verdicts where dagster_run_id = 'test-run'")
        for bad in (("Not A Name", "x"), ("output_invariants", ""), ("output_invariants", "x" * 301)):
            with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
                connection.execute(nightly_verdicts.INSERT_SQL, (bad[0], tick, True, bad[1], "test-run"))
        # The health endpoint's read (llm_service.main.NIGHTLY_VERDICTS_SQL) orders the newest
        # row per check by (ran_at, recorded_at): the later of two same-tick rows wins.
        newest = connection.execute(
            "select distinct on (check_name) ok from mart.nightly_verdicts "
            "where dagster_run_id = 'test-run' order by check_name, ran_at desc, recorded_at desc"
        ).fetchone()
        assert newest == (True,)
    finally:
        connection.rollback()
        connection.close()
