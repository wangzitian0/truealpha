"""The weekly standards lane runs the backfill, then module 6's theme purity (#772), then
the question-coverage report (#748) — with one configuration per universe. A run request
that configured only some of the ops would fail at launch, so the schedule is asserted here
rather than discovered on Sunday.

The order is the data dependency, not a preference: purity consumes the segment partitions
the backfill lands, and coverage counts the purity column."""

from __future__ import annotations

from datetime import UTC, datetime

import dagster as dg
from data_engine.lanes.standards import (
    STANDARD_BACKFILL_UNIVERSES,
    standard_backfill_pipeline_job,
    standard_backfill_schedule,
)


def test_the_job_chains_all_modules_to_coverage() -> None:
    """Each op consumes the one before it, so the chain is enforced by the dependency rather
    than by ordering luck: backfill -> theme purity -> supply chain -> analyst ratings -> coverage."""
    assert [node.name for node in standard_backfill_pipeline_job.graph.node_defs] == [
        "run_standard_backfill",
        "run_theme_purity",
        "run_supply_chain_exposure",
        "run_analyst_ratings",
        "run_question_coverage",
    ]


def test_every_run_request_configures_every_op_for_its_universe() -> None:
    context = dg.build_schedule_context(scheduled_execution_time=datetime(2026, 9, 13, 9, 7, tzinfo=UTC))
    requests = list(standard_backfill_schedule.evaluate_tick(context).run_requests)
    assert [request.run_key for request in requests] == [
        f"2026-09-13T09:07:00+00:00:{universe}" for universe in STANDARD_BACKFILL_UNIVERSES
    ]
    for request, universe in zip(requests, STANDARD_BACKFILL_UNIVERSES, strict=True):
        ops = request.run_config["ops"]
        assert set(ops) == {
            "run_standard_backfill",
            "run_theme_purity",
            "run_supply_chain_exposure",
            "run_analyst_ratings",
            "run_question_coverage",
        }
        for op in ops.values():
            assert op["config"]["universe"] == universe
            assert op["config"]["executed_at"] == "2026-09-13T09:07:00+00:00"
    assert dg.validate_run_config(standard_backfill_pipeline_job, requests[0].run_config)


def test_the_weekly_run_covers_every_registered_standard() -> None:
    """The defect this pins: `StandardBackfillConfig.standard` defaulted to
    `"employees_total"`, and the schedule passes no standard — so the weekly run backfilled
    exactly one metric. `segment_revenue` had a plane, an adapter and a confidence policy
    (#804/#805) and was invoked by nothing deployed.

    init.md rule 22 is that the registry is the metric list. A default that names a metric is
    that list wearing a different hat, and the loop being generic (#799/#800) does not help if
    the only thing that runs it names one.
    """
    from data_engine.lanes.standards import standards_to_run
    from truealpha_contracts.standards import STANDARDS

    context = dg.build_schedule_context(scheduled_execution_time=datetime(2026, 9, 13, 9, 7, tzinfo=UTC))
    for request in standard_backfill_schedule.evaluate_tick(context).run_requests:
        selected = request.run_config["ops"]["run_standard_backfill"]["config"].get("standard", "")
        assert set(standards_to_run(selected)) == set(STANDARDS), (
            "a registered standard the weekly run never reaches is code that merely exists"
        )


def test_a_manual_run_can_still_bound_itself_to_one_standard() -> None:
    """The generalization must not cost the operator the narrow run — probing one metric over
    one universe is the source-research instrument the lane's docstring describes."""
    from data_engine.lanes.standards import standards_to_run

    assert standards_to_run("segment_revenue") == ("segment_revenue",)


def test_an_unknown_standard_fails_at_the_run_rather_than_silently_doing_nothing() -> None:
    """A typo used to reach `STANDARDS[name]` and raise KeyError deep in the backfill; naming
    the registered set at the boundary is the difference between a fixable message and a
    stack trace."""
    import pytest
    from data_engine.lanes.standards import standards_to_run

    with pytest.raises(ValueError, match="unknown standard"):
        standards_to_run("employees")


def test_each_op_body_actually_runs(monkeypatch) -> None:
    """The gap this closes: `run_theme_purity` imported a module that does not exist
    (`production_topt.governed_read`; `governed_head` lives in `question_coverage`). Every
    test above asserted the lane's SHAPE — the ops, their order, their config — and none of
    them executed a body, so a `ModuleNotFoundError` would have waited until Sunday 09:07 UTC
    to appear. mypy caught it; this is the check that catches the next one.

    The no-governed-head branch is the one to drive: it reaches every import and every
    settings read in the body, touches the database only to ask for a head, and returns
    without writing — so it needs no fixture beyond a connection that answers nothing.
    """
    import json as _json

    import psycopg
    from data_engine.lanes.standards import run_theme_purity

    class _NoHead:
        def execute(self, *_a, **_k):
            return self

        def fetchone(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    from data_engine.lanes.standards import StandardBackfillConfig

    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: _NoHead())
    context = dg.build_op_context()
    config = StandardBackfillConfig(executed_at="2026-09-13T09:07:00+00:00", universe="topt")
    out = _json.loads(run_theme_purity(context, config, "{}"))
    assert out == {"universe": "topt", "rows": 0, "reason": "no_governed_head"}


def test_theme_purity_finds_the_head_the_capture_tick_registers_in_every_deployment(monkeypatch) -> None:
    """#826. On staging this op logged "no governed head" on every weekly tick and wrote no
    q6 row, while `run_question_coverage` in the SAME run found the head and persisted its
    report. The pointer's `environment` is the capture tier the A1 registration stamps
    (`CaptureEnvironment.PRODUCTION`, for staging's real-vendor capture too); the op asked for
    `settings.app_env`, which is `staging` there.

    The test above cannot see that: its connection answers nothing to every query, so it
    passes whatever environment the op asks for. This one answers ONLY for the stamped tier,
    and runs with the deployment set to staging, which is where the defect lived.
    """
    import json as _json

    import psycopg
    from data_engine.config import settings
    from data_engine.datahub.production_topt import theme_purity
    from data_engine.lanes.standards import StandardBackfillConfig, run_theme_purity
    from truealpha_contracts.common import CaptureEnvironment

    head = ("universe:topt-us-2026-03-31", "capture-run:" + "a" * 64, datetime(2026, 9, 14, 22, 45, tzinfo=UTC))

    class _Pointer:
        def __init__(self) -> None:
            self._row = None

        def execute(self, sql, params=()):
            # #756: the op must resolve the head under the environment THIS DATABASE declares.
            # The stub answers only a query that filters on `mart.environment_identity`, so a
            # reversion to a named environment -- #826's `production` literal, or any other --
            # finds no head here and this test says so. What the literal actually cost on
            # staging was invisible: two lineages for one universe, the literal-pinned readers
            # green on the frozen one.
            resolves_the_declared_environment = (
                "current_pointer_head" in sql
                and "mart.environment_identity" in sql
                and CaptureEnvironment.PRODUCTION.value not in tuple(params)
            )
            self._row = head if resolves_the_declared_environment else None
            return self

        def fetchone(self):
            return self._row

        def commit(self) -> None:
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(settings, "app_env", "staging")
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: _Pointer())
    materialized = []
    monkeypatch.setattr(
        theme_purity, "materialize_theme_purity", lambda _c, **kwargs: materialized.append(kwargs) or ()
    )
    # The corpus the op names its members from (#849); the pointer stub answers no corpus query.
    from data_engine.datahub.standards.planner import UniverseIssuer
    from data_engine.lanes import standards as lane

    monkeypatch.setattr(
        lane, "universe_issuers", lambda _c, _u: [UniverseIssuer("issuer:lei:X", "NFLX", "listing:x", None)]
    )

    config = StandardBackfillConfig(executed_at="2026-09-20T09:07:00+00:00", universe="topt")
    out = _json.loads(run_theme_purity(dg.build_op_context(), config, "{}"))

    assert out.get("reason") != "no_governed_head", (
        "the op must find the head through mart.environment_identity, not through a named environment"
    )
    assert out["run_id"] == head[1]
    assert materialized == [{"run_id": head[1], "cutoff": head[2], "tickers": {"issuer:lei:X": "NFLX"}}], (
        "rows are written for THAT run, at ITS cutoff, and the classifier is told the ticker (#849)"
    )


def test_the_daily_head_reports_job_configures_every_op_for_its_universe() -> None:
    """#855 C1/C2: module 6 and the coverage report follow TODAY's head every day, not only
    the Sunday backfill — otherwise /research/themes and /admin/datahub serve yesterday's head
    while /research/rankings serves today's (measured on staging 2026-09-16)."""
    from data_engine.lanes.standards import head_reports_pipeline_job, head_reports_schedule

    assert [node.name for node in head_reports_pipeline_job.graph.node_defs] == [
        "head_reports_start",
        "run_theme_purity",
        "run_question_coverage",
    ]
    context = dg.build_schedule_context(scheduled_execution_time=datetime(2026, 9, 16, 23, 30, tzinfo=UTC))
    requests = list(head_reports_schedule.evaluate_tick(context).run_requests)
    assert [request.run_key for request in requests] == [
        f"2026-09-16T23:30:00+00:00:{universe}" for universe in STANDARD_BACKFILL_UNIVERSES
    ]
    for request, universe in zip(requests, STANDARD_BACKFILL_UNIVERSES, strict=True):
        ops = request.run_config["ops"]
        assert set(ops) == {"head_reports_start", "run_theme_purity", "run_question_coverage"}
        assert all(op["config"]["universe"] == universe for op in ops.values())
    assert dg.validate_run_config(head_reports_pipeline_job, requests[0].run_config)


# --- head reports follow the pointer (2026-09-17) ---------------------------------------
#
# Production, 2026-09-16: the 23:30 head-reports schedule read the QQQ head the 23:20 tick had
# not replaced yet (the tick ran until 23:55), and the 00:15 proof found /admin/datahub serving
# `capture-run:1991…` against the head `capture-run:a62d…`. The sensor below launches the
# reports when the pointer moves; the schedule stays as a fallback that recomputes nothing
# when the head's reports already exist.

TOPT_OLD = "capture-run:" + "1" * 64
TOPT_NEW = "capture-run:" + "2" * 64
QQQ_HEAD = "capture-run:" + "3" * 64
TOPT_ID = "universe:topt-us-2026-03-31"
QQQ_ID = "universe:qqq-us-2026-06-30"
HEAD_CUTOFF = datetime(2026, 9, 16, 22, 15, tzinfo=UTC)


class _Connection:
    """A connection nothing reads through: the head and the stored report are faked above it."""

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def commit(self) -> None:
        return None


def _pointer(monkeypatch, *, heads: dict[str, str | None], stored: dict[str, str | None]) -> None:
    """`heads`: universe -> the run its governed pointer names; `stored`: universe id -> the run
    the newest stored coverage report names."""
    import psycopg
    from data_engine.datahub import question_coverage

    ids = {"topt": TOPT_ID, "universe-list:qqq": QQQ_ID}

    def governed_head(_connection, *, universe_prefix):
        # #756 replaced #826's literal: the head is resolved under the environment the
        # database declares, so there is no argument left to pin. The signature does it.
        universe = next(key for key, prefix in question_coverage.UNIVERSE_PREFIXES.items() if prefix == universe_prefix)
        run = heads.get(universe)
        return None if run is None else question_coverage.GovernedHead(ids[universe], run, HEAD_CUTOFF)

    monkeypatch.setattr(question_coverage, "governed_head", governed_head)
    monkeypatch.setattr(question_coverage, "stored_report_run", lambda _c, universe_id: stored.get(universe_id))
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: _Connection())


def _tick(cursor: dict[str, str] | None = None):
    """One evaluation the way the daemon asks for it (`evaluate_tick`): the run requests, the
    skip message and the cursor the daemon persists for the next look."""
    import json as _json

    from data_engine.lanes.standards import head_reports_sensor

    context = dg.build_sensor_context(cursor=None if cursor is None else _json.dumps(cursor))
    return head_reports_sensor.evaluate_tick(context)


def _cursor(data) -> dict[str, str]:
    import json as _json

    return _json.loads(data.cursor)


def test_a_pointer_advance_launches_exactly_one_head_reports_run_for_that_universe(monkeypatch) -> None:
    from data_engine.lanes.standards import HEAD_RUN_TAG, UNIVERSE_TAG, head_reports_pipeline_job

    # TOPT advanced since the sensor last looked; QQQ did not. An earlier run already stored a
    # coverage report naming the new TOPT head — the advance is launched anyway, because only a
    # run launched FOR this head writes its purity rows too.
    _pointer(monkeypatch, heads={"topt": TOPT_NEW, "universe-list:qqq": QQQ_HEAD}, stored={TOPT_ID: TOPT_NEW})
    data = _tick({"topt": TOPT_OLD, "universe-list:qqq": QQQ_HEAD})
    (request,) = data.run_requests

    assert request.run_key == f"head:topt:{TOPT_NEW}", "deduped by the head's run id"
    # The sensor-name tag is the scope Dagster dedupes a run key in.
    assert request.tags == {
        UNIVERSE_TAG: "topt",
        HEAD_RUN_TAG: TOPT_NEW,
        "dagster/sensor_name": "head_reports_on_pointer_advance",
    }
    ops = request.run_config["ops"]
    assert set(ops) == {"head_reports_start", "run_theme_purity", "run_question_coverage"}
    assert all(op["config"]["universe"] == "topt" for op in ops.values())
    assert ops["head_reports_start"]["config"]["only_if_stale"] is False, "an advance always recomputes"
    assert dg.validate_run_config(head_reports_pipeline_job, request.run_config)
    assert _cursor(data) == {"topt": TOPT_NEW, "universe-list:qqq": QQQ_HEAD}


def test_a_re_evaluation_of_the_same_head_does_not_relaunch(monkeypatch) -> None:
    _pointer(monkeypatch, heads={"topt": TOPT_NEW, "universe-list:qqq": QQQ_HEAD}, stored={})
    first = _tick({"topt": TOPT_OLD, "universe-list:qqq": QQQ_HEAD})
    (launched,) = first.run_requests

    # The daemon persists the cursor the first evaluation left; the next look finds no advance,
    # and neither does the one after it.
    for _ in range(2):
        again = _tick(_cursor(first))
        assert list(again.run_requests) == []
        assert "no governed head has advanced" in (again.skip_message or "")
        assert _cursor(again) == _cursor(first)

    # A restart that lost the cursor asks again for a head whose reports are still missing, under
    # the SAME run key — which Dagster launches once per sensor (`fetch_existing_runs`).
    lost = _tick()
    assert launched.run_key in {request.run_key for request in lost.run_requests}


def test_a_head_first_seen_with_its_reports_written_is_not_relaunched(monkeypatch) -> None:
    """The sensor's first look after the deploy that introduces it: the schedule or an operator
    has already reported both heads, and nothing is launched."""
    _pointer(
        monkeypatch,
        heads={"topt": TOPT_NEW, "universe-list:qqq": QQQ_HEAD},
        stored={TOPT_ID: TOPT_NEW, QQQ_ID: QQQ_HEAD},
    )
    data = _tick()
    assert list(data.run_requests) == [] and data.skip_message
    assert _cursor(data) == {"topt": TOPT_NEW, "universe-list:qqq": QQQ_HEAD}


def test_a_universe_with_no_governed_head_launches_nothing(monkeypatch) -> None:
    """Staging runs no QQQ tick: no head, no run, and no cursor entry to trip over later."""
    _pointer(monkeypatch, heads={"topt": TOPT_NEW, "universe-list:qqq": None}, stored={TOPT_ID: TOPT_NEW})
    data = _tick({"topt": TOPT_NEW})
    assert list(data.run_requests) == [] and data.skip_message
    assert _cursor(data) == {"topt": TOPT_NEW}


def test_the_sensor_is_deployed_running_against_the_head_reports_job() -> None:
    from data_engine.dagster_defs import defs
    from data_engine.lanes.standards import HEAD_REPORTS_JOB_NAME, HEAD_REPORTS_SENSOR_NAME

    sensor = defs.get_sensor_def(HEAD_REPORTS_SENSOR_NAME)
    assert sensor.default_status == dg.DefaultSensorStatus.RUNNING
    assert sensor.job_name == HEAD_REPORTS_JOB_NAME
    assert sensor.minimum_interval_seconds <= 60, "a head's reports follow it within a minute"


def _run_fallback(monkeypatch, *, stored: str | None) -> tuple[list, list, list, list]:
    """Execute the fallback schedule's request for TOPT through the deployed job, with the head
    on TOPT_NEW and the newest stored report naming `stored`."""
    from data_engine.datahub import question_coverage
    from data_engine.datahub.production_topt import theme_purity
    from data_engine.lanes import standards
    from data_engine.quality import nightly_verdicts

    _pointer(monkeypatch, heads={"topt": TOPT_NEW}, stored={TOPT_ID: stored})
    purity_calls: list = []
    compiled: list = []
    persisted: list = []
    written: list = []
    monkeypatch.setattr(
        theme_purity, "materialize_theme_purity", lambda _c, **kwargs: purity_calls.append(kwargs) or ()
    )
    monkeypatch.setattr(standards, "universe_issuers", lambda *_a, **_k: [])
    report = {"universe_id": TOPT_ID, "denominator": 20, "questions": {}}
    monkeypatch.setattr(question_coverage, "compile_report", lambda *_a, **kwargs: compiled.append(kwargs) or report)
    monkeypatch.setattr(question_coverage, "persist", lambda _c, r: persisted.append(r) or "question-coverage-report:x")
    monkeypatch.setattr(question_coverage, "summary_line", lambda _r: "summary")
    monkeypatch.setattr(nightly_verdicts, "record", lambda name, **kwargs: written.append({"check": name, **kwargs}))

    tick = datetime(2026, 9, 17, 4, 0, tzinfo=UTC)
    requests = {
        request.tags[standards.UNIVERSE_TAG]: request
        for request in standards.head_reports_schedule.evaluate_tick(
            dg.build_schedule_context(scheduled_execution_time=tick)
        ).run_requests
    }
    request = requests["topt"]
    assert request.run_config["ops"]["head_reports_start"]["config"]["only_if_stale"] is True
    result = standards.head_reports_pipeline_job.execute_in_process(run_config=request.run_config)
    assert result.success
    return purity_calls, compiled, persisted, written


def test_the_fallback_recomputes_nothing_when_the_heads_reports_are_current(monkeypatch) -> None:
    purity_calls, compiled, persisted, written = _run_fallback(monkeypatch, stored=TOPT_NEW)
    assert purity_calls == [] and compiled == [] and persisted == [], "a no-op on a reported head"
    # ...and still a green verdict each, so a day the pointer does not move is not a stale check.
    assert [(row["check"], row["ok"]) for row in written] == [
        ("theme_purity@topt", True),
        ("question_coverage@topt", True),
    ]
    assert all(row["summary"] == f"reports already current on {TOPT_NEW[:24]}; nothing recomputed" for row in written)
    assert all(row["ran_at"] == datetime(2026, 9, 17, 4, 0, tzinfo=UTC) for row in written)


def test_the_fallback_recomputes_a_head_the_sensor_missed(monkeypatch) -> None:
    purity_calls, compiled, persisted, written = _run_fallback(monkeypatch, stored=TOPT_OLD)
    assert [call["run_id"] for call in purity_calls] == [TOPT_NEW]
    assert len(compiled) == 1 and len(persisted) == 1
    assert [(row["check"], row["ok"]) for row in written] == [
        ("theme_purity@topt", True),
        ("question_coverage@topt", True),
    ]
    assert "already current" not in " ".join(row["summary"] for row in written)


def test_the_fallback_runs_after_the_proofs_longest_wait_and_before_the_watchdog() -> None:
    from data_engine.lanes import quality
    from data_engine.lanes.standards import HEAD_REPORTS_CRON, head_reports_schedule

    assert head_reports_schedule.cron_schedule == HEAD_REPORTS_CRON
    minute, hour = (int(field) for field in HEAD_REPORTS_CRON.split()[:2])
    proof_minute, proof_hour = (int(field) for field in quality.OUTPUT_INVARIANTS_CRON.split()[:2])
    proof_done = proof_hour * 60 + proof_minute + quality.SURFACE_SETTLE_TIMEOUT.total_seconds() / 60
    assert proof_done <= hour * 60 + minute < 7 * 60, "after the proof can still be waiting, before 07:00"


# --- a registered standard is backfilled when the build that registers it is up ---------
#
# Production, 2026-09-17: zero rows in staging.issuer_segment_revenue_facts, ever. Its Sunday
# runs (09-06, 09-13) ran v0.0.51 and earlier, whose schedule backfilled `employees_total`
# only; the release that fixed that (#806) reached production on Tuesday 09-15.

BUILD = "sha256:" + "4" * 64


def _catchup(monkeypatch, *, missing: list[tuple[str, str]]) -> None:
    import psycopg
    from data_engine.lanes import standards

    monkeypatch.setattr(standards.settings, "data_engine_image_digest", BUILD)
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: _Connection())
    monkeypatch.setattr(standards, "never_backfilled", lambda _c, **_k: list(missing))


def test_the_catchup_only_arms_on_a_builds_first_look(monkeypatch) -> None:
    from data_engine.lanes.standards import standard_backfill_catchup_sensor

    _catchup(monkeypatch, missing=[("topt", "segment_revenue")])
    context = dg.build_sensor_context(cursor="sha256:" + "0" * 64)
    (skip,) = list(standard_backfill_catchup_sensor(context))
    assert isinstance(skip, dg.SkipReason) and "armed" in skip.skip_message
    assert context.cursor == BUILD, "the launch waits for the next look, clear of the deploy walk"


def test_a_standard_never_backfilled_here_is_backfilled_once_per_universe(monkeypatch) -> None:
    from data_engine.lanes.standards import (
        UNIVERSE_TAG,
        standard_backfill_catchup_sensor,
        standard_backfill_pipeline_job,
    )

    _catchup(monkeypatch, missing=[("universe-list:qqq", "segment_revenue"), ("topt", "segment_revenue")])
    requests = list(standard_backfill_catchup_sensor(dg.build_sensor_context(cursor=BUILD)))
    assert [(r.run_key, r.tags[UNIVERSE_TAG]) for r in requests] == [
        ("catchup:universe-list:qqq:segment_revenue", "universe-list:qqq"),
        ("catchup:topt:segment_revenue", "topt"),
    ]
    for request in requests:
        ops = request.run_config["ops"]
        assert ops["run_standard_backfill"]["config"]["standard"] == "segment_revenue", "bounded to what is missing"
        assert ops["run_standard_backfill"]["config"]["mode"] == "backfill"
        assert {op["config"]["universe"] for op in ops.values()} == {request.tags[UNIVERSE_TAG]}
        assert dg.validate_run_config(standard_backfill_pipeline_job, request.run_config)


def test_several_missing_standards_run_as_one_full_backfill(monkeypatch) -> None:
    from data_engine.lanes.standards import standard_backfill_catchup_sensor

    _catchup(monkeypatch, missing=[("topt", "employees_total"), ("topt", "segment_revenue")])
    (request,) = list(standard_backfill_catchup_sensor(dg.build_sensor_context(cursor=BUILD)))
    assert request.run_key == "catchup:topt:employees_total+segment_revenue"
    assert request.run_config["ops"]["run_standard_backfill"]["config"]["standard"] == ""


def test_nothing_is_launched_where_every_standard_has_been_backfilled(monkeypatch) -> None:
    from data_engine.lanes.standards import standard_backfill_catchup_sensor

    _catchup(monkeypatch, missing=[])
    (skip,) = list(standard_backfill_catchup_sensor(dg.build_sensor_context(cursor=BUILD)))
    assert isinstance(skip, dg.SkipReason)


def _db():
    import os

    import psycopg
    import pytest
    from data_engine.config import settings

    try:
        return psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            raise
        pytest.skip("no local Postgres")


def test_only_a_completed_backfill_counts_as_backfilled() -> None:
    """Against the real health log: the row a backfill writes at its END counts; a probe's row,
    or a per-status row a run wrote before dying, does not. Rolled back."""
    from data_engine.datahub.standards.backfill import HEALTH_LOG_SOURCE, completed_metric, never_backfilled

    universes = ("universe-list:test-catchup-a", "universe-list:test-catchup-b")
    standards = ("employees_total", "segment_revenue")
    connection = _db()
    try:
        insert = "insert into staging.ingestion_health_log (source, metric, value, note) values (%s, %s, 0, '{}')"
        connection.execute(insert, (HEALTH_LOG_SOURCE, completed_metric("employees_total", universes[0])))
        connection.execute(insert, (HEALTH_LOG_SOURCE, completed_metric("employees_total", universes[1])))
        connection.execute(insert, (HEALTH_LOG_SOURCE, f"segment_revenue:{universes[0]}:probe:open_cells"))
        connection.execute(insert, (HEALTH_LOG_SOURCE, f"segment_revenue:{universes[1]}:backfill:resolved"))
        connection.execute(insert, ("another-source", completed_metric("segment_revenue", universes[1])))
        assert never_backfilled(connection, universes=universes, standards=standards) == [
            (universes[0], "segment_revenue"),
            (universes[1], "segment_revenue"),
        ]
        connection.execute(insert, (HEALTH_LOG_SOURCE, completed_metric("segment_revenue", universes[0])))
        assert never_backfilled(connection, universes=universes, standards=standards) == [
            (universes[1], "segment_revenue")
        ]
    finally:
        connection.rollback()
        connection.close()


def test_the_backfill_writes_the_row_the_catchup_reads() -> None:
    """The metric the sensor looks for is the one `_persist_summary` writes, not a copy of it."""
    from collections import Counter

    from data_engine.datahub.standards import backfill

    executed: list[tuple] = []

    class _Log:
        def execute(self, _sql, params):
            executed.append(params)

    report = backfill.BackfillReport(
        universe="topt", standard="segment_revenue", mode="backfill", cutoff=HEAD_CUTOFF, outcomes=Counter()
    )
    backfill._persist_summary(_Log(), report)
    assert (backfill.HEALTH_LOG_SOURCE, backfill.completed_metric("segment_revenue", "topt")) in {
        params[:2] for params in executed
    }
