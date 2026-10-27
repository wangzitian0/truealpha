"""A variant universe record reaches every reader (#1522 step 1).

The record in `environment_contract.v1.json` is the only list of universes. A variant record schedules the
S&P 500 and adds one more universe (`dow`). Every reader that builds from the record at import (the schedules,
the verdict names, the in-flight list, the surface proof, the head-reports sensor) must then serve all four
universes. A fresh interpreter loads the variant before it imports `data_engine`, because the deployable
builds its definitions at import. The ops that read the record at run time run in the same interpreter.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from truealpha_contracts import universes
from truealpha_contracts.environment_contract import UnknownUniverse

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
from variant_contract import NEW_ID_PREFIX, NEW_KEY, NEW_LANE, serve_variant, variant_text  # noqa: E402

TICK = "2026-10-27T00:45:00+00:00"
#: The four universes of the variant, as the lanes name them.
VARIANT_LANES = ("topt", "universe-list:qqq", "universe-list:sp500", NEW_LANE)
NEW_LANES = ("universe-list:sp500", NEW_LANE)


class _Sink:
    """A database that answers the environment query and serves one fresh TOPT head, and nothing else."""

    def __call__(self, *_a: Any, **_k: Any) -> _Sink:
        return self

    def __enter__(self) -> _Sink:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def commit(self) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> _Sink:
        self._sql = sql
        return self

    def fetchall(self) -> list:
        if "from mart.served_head" in getattr(self, "_sql", ""):
            return [("universe:topt-us-2026-03-31", "fresh", "available", None)]
        return []

    def fetchone(self) -> Any:
        declares = getattr(self, "_sql", "").strip() == "select environment from mart.environment_identity"
        return ("production",) if declares else None


# --- the readers that build at import (a fresh interpreter loads the variant first) ------------------


def _child_build() -> None:
    import dagster as dg
    import psycopg
    from data_engine import dagster_defs
    from data_engine.datahub import question_coverage
    from data_engine.lanes import capture, nightly_verdict_names, quality, standards
    from data_engine.quality import surface_proof

    # build_definitions: both universes get a job, and both schedules run, because the variant schedules them.
    definitions = dagster_defs.build_definitions("production")
    jobs = {job.name for job in definitions.jobs}
    statuses = {schedule.name: schedule.default_status for schedule in definitions.schedules}
    for key in ("sp500", NEW_KEY):
        assert f"{key}_live_pipeline" in jobs, f"no job for {key}"
        assert statuses[f"{key}_live_schedule"] == dg.DefaultScheduleStatus.RUNNING, f"{key} schedule is not running"
    assert statuses["canary_daily_schedule"] == dg.DefaultScheduleStatus.RUNNING
    assert [tick.key for tick in capture.TICKS] == ["topt", "qqq", "sp500", "canary", NEW_KEY]

    # verdict names: every lane that records a per-universe verdict names all four universes.
    names = nightly_verdict_names()
    for lane in VARIANT_LANES:
        for check in ("theme_purity", "question_coverage", "datahub_confidence_report"):
            assert f"{check}@{lane}" in names, f"{check}@{lane} is no verdict name"

    # the in-flight list: the unfinished tick of each universe holds that universe.
    instance = dg.DagsterInstance.ephemeral()
    import uuid

    for key in ("topt", "qqq", "sp500", NEW_KEY):
        instance.add_run(
            dg.DagsterRun(job_name=f"{key}_live_pipeline", run_id=str(uuid.uuid4()), status=dg.DagsterRunStatus.STARTED)
        )
    assert sorted(quality.runs_in_flight(instance)) == sorted(VARIANT_LANES)

    # the surface proof: a head recorded a minute ago, with no reports yet, settles in every universe.
    now = datetime(2026, 10, 27, 0, 45, tzinfo=UTC)
    head = question_coverage.GovernedHead("universe:x", "capture-run:" + "a" * 64, now)

    class _Recorded(_Sink):
        def fetchone(self) -> Any:
            return (now.replace(minute=44),)

    surface_proof.governed_head = lambda _c, *, universe_prefix: head
    surface_proof.stored_report_run = lambda _c, _universe_id: None
    surface_proof.declared_environment = lambda _c: "production"
    settling = quality.settling(_Recorded(), dg.DagsterInstance.ephemeral(), now=now)
    assert sorted(settling) == sorted(VARIANT_LANES), "the surface proof skips a universe of the record"

    # the head-reports sensor: it launches the reports of every scheduled universe. The S&P 500 used to raise
    # KeyError before the first request.
    heads = {
        universes.record(key).id_prefix: question_coverage.GovernedHead(
            f"{universes.record(key).id_prefix}2026", f"capture-run:{key}", now
        )
        for key in ("topt", "qqq", "sp500", NEW_KEY)
    }
    question_coverage.governed_head = lambda _c, *, universe_prefix: heads[universe_prefix]
    question_coverage.stored_report_run = lambda _c, _universe_id: None
    psycopg.connect = _Sink()
    data = standards.head_reports_sensor.evaluate_tick(dg.build_sensor_context())
    launched = sorted(request.tags[standards.UNIVERSE_TAG] for request in data.run_requests)
    assert launched == sorted(VARIANT_LANES), f"the sensor launched {launched}"


def _child_pending() -> None:
    import dagster as dg
    import psycopg
    from data_engine.datahub import question_coverage
    from data_engine.lanes import quality, standards
    from data_engine.quality import nightly_verdicts

    written: list[dict[str, Any]] = []
    nightly_verdicts.record = lambda name, **kwargs: written.append({"check": name, **kwargs})
    psycopg.connect = _Sink()
    question_coverage.governed_head = lambda *_a, **_k: None
    for lane in NEW_LANES:
        quality.run_confidence_report(
            dg.build_op_context(), quality.ConfidenceReportConfig(executed_at=TICK, universe=lane)
        )
        config = standards.StandardBackfillConfig(executed_at=TICK, universe=lane)
        standards.run_theme_purity(dg.build_op_context(), config, "{}")
        standards.run_question_coverage(dg.build_op_context(), config, "{}")
    assert len(written) == 6, [row["check"] for row in written]
    for row in written:
        assert row["ok"] is None, f"{row['check']} records ok={row['ok']} for a universe without a head"
    assert {row["summary"] for row in written} == {question_coverage.NO_HEAD_SUMMARY}, "the summaries differ"


def _run_child() -> subprocess.CompletedProcess[str]:
    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "variant.json"
        path.write_text(variant_text(), encoding="utf-8")
        env = {**os.environ, "APP_ENV": "production", "VARIANT_CONTRACT": str(path)}
        return subprocess.run(
            [sys.executable, str(Path(__file__).resolve())],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )


def test_a_variant_record_reaches_every_reader() -> None:
    result = _run_child()
    assert result.returncode == 0, result.stderr[-3000:]


# --- the readers that read the record at run time ---------------------------------------------------


def test_a_key_id_or_lane_outside_the_record_raises(monkeypatch) -> None:
    import dagster as dg
    import psycopg
    from data_engine.lanes import quality

    monkeypatch.setattr(psycopg, "connect", _Sink())

    with pytest.raises(UnknownUniverse):
        universes.record("nope")
    with pytest.raises(UnknownUniverse):
        universes.universe_of("universe:nope-us-2026-03-31")
    with pytest.raises(UnknownUniverse):
        universes.key_of_lane("universe-list:nope")
    config = quality.ConfidenceReportConfig(executed_at=TICK, universe="universe-list:nope")
    with pytest.raises(UnknownUniverse):
        quality.run_confidence_report(dg.build_op_context(), config)


def test_the_longest_id_prefix_wins_and_the_id_is_not_split() -> None:
    with serve_variant():
        assert universes.universe_of("universe:topt-us-2026-03-31") == "topt"
        assert universes.universe_of(f"{NEW_ID_PREFIX}2026-06-30") == NEW_KEY
        # A key that is the start of another key still resolves by prefix length, not by position.
        assert universes.universe_of("universe:sp500-us-2026-06-30") == "sp500"


def test_a_scheduled_universe_is_scheduled_for_every_reader_of_the_contract() -> None:
    from truealpha_contracts.environment_contract import load_environment_contract

    with serve_variant():
        contract = load_environment_contract()
        assert contract.scheduled_universes("production") == ("topt", "qqq", "sp500", NEW_KEY)
        assert contract.is_head_scheduled(f"{NEW_ID_PREFIX}2026-06-30", "production")
        assert not contract.is_head_scheduled(f"{NEW_ID_PREFIX}2026-06-30", "staging")
        assert contract.expects_verdict(f"theme_purity@{NEW_LANE}", "production")
        assert not contract.expects_verdict(f"theme_purity@{NEW_LANE}", "staging")


if __name__ == "__main__":
    # The child interpreter of `_run_child`: it loads the variant contract, then imports `data_engine`.
    with serve_variant(Path(os.environ["VARIANT_CONTRACT"]).read_text(encoding="utf-8")):
        _child_build()
        _child_pending()
