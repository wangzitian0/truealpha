"""The deployable Dagster entrypoint (#27): the module infra2's Staging/Production
daemon + webserver load with `-m data_engine.dagster_defs`.

Import-time / definition-load assertions — no database, no network. Importing the
module and building its `Definitions` is exactly what `dagster -m` and CI collection
do, so this proves the deploy target loads hermetically, that the deployed job graph
carries NO fixture seeding (#429 invariant I2), and that the schedule carries #27's
idempotency semantics.

The op-body checks at the bottom keep that promise: they invoke `run_topt_live_tick`
directly with every collaborator faked, so the op's reporting surface (#536: which
service objective withheld the governed pointer) is asserted without a database.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

import dagster as dg
import psycopg
from data_engine import dagster_defs
from data_engine.dagster_defs import (
    CORE_STRATEGY_FIXTURE_CANARY_JOB_NAME,
    TOPT_LIVE_CRON,
    ToptLiveTickConfig,
    defs,
    fixture_canary_definitions,
    run_topt_live_tick,
    topt_live_schedule,
)
from data_engine.datahub.a1_evidence import PointerRegistration, UnmetObjective
from data_engine.datahub.production_topt.composition import ToptPipelineResult, live_version_for
from data_engine.lanes import LANE_MODULES, capture, lane_definitions


def test_defs_is_exactly_the_union_of_the_registered_lanes() -> None:
    """`dagster -m data_engine.dagster_defs` resolves `defs`; it must build with no
    database and be exactly what the registered lanes declare — no job listed by name
    anywhere in the root (#731), and the fixture canary NOT part of it (#429 I2)."""
    assert isinstance(defs, dg.Definitions)
    lanes = lane_definitions()
    assert set(lanes) == set(LANE_MODULES)

    def names(definitions: dg.Definitions, attribute: str) -> set[str]:
        return {item.name for item in getattr(definitions, attribute) or ()}

    for attribute in ("jobs", "schedules", "sensors"):
        declared = set().union(*(names(lane, attribute) for lane in lanes.values()))
        assert names(defs, attribute) == declared, f"root {attribute} != union of lane {attribute}"
    assert names(defs, "jobs"), "the lanes declare no job at all"
    assert CORE_STRATEGY_FIXTURE_CANARY_JOB_NAME not in names(defs, "jobs")
    # Sensors target jobs from another lane (triggers -> capture); the merged
    # repository must resolve them, which only the repository build proves.
    defs.get_repository_def()


def test_every_lane_module_is_registered() -> None:
    """A module under data_engine/lanes that LANE_MODULES does not name would load
    nowhere: its jobs exist in the image and run never. Registration is one line,
    and this makes forgetting it red."""
    from pathlib import Path

    package = Path(capture.__file__).parent
    on_disk = {f"data_engine.lanes.{path.stem}" for path in package.glob("*.py") if path.stem != "__init__"}
    assert on_disk == set(LANE_MODULES), (
        f"lane modules on disk {sorted(on_disk)} != registered {sorted(LANE_MODULES)}; "
        "add the module to data_engine.lanes.LANE_MODULES or delete it"
    )


#: Definitions a lane module imports from another lane in order to target them
#: (a sensor's `jobs=`), and therefore does not own or list. Anything else a
#: module builds must be in its own `defs`.
IMPORTED_NOT_OWNED: dict[str, set[str]] = {"data_engine.lanes.triggers": {"jobs"}}


def test_every_definition_a_lane_module_declares_is_in_its_defs() -> None:
    """The other half of "no frozen list": a job, schedule or sensor built in a lane
    module but left out of that module's `defs` is deployed nowhere. Assert over the
    objects the module actually built, not over a list someone remembered to update."""
    kinds = {"jobs": dg.JobDefinition, "schedules": dg.ScheduleDefinition, "sensors": dg.SensorDefinition}
    for module_name, lane in lane_definitions().items():
        module = __import__(module_name, fromlist=["defs"])
        for attribute, kind in kinds.items():
            if attribute in IMPORTED_NOT_OWNED.get(module_name, set()):
                continue
            built = {value.name for value in vars(module).values() if isinstance(value, kind)}
            listed = {item.name for item in getattr(lane, attribute) or ()}
            missing = built - listed
            assert not missing, f"{module_name} builds {attribute} {sorted(missing)} but its defs omit them"


def test_deployed_module_contains_no_fixture_seeding() -> None:
    # The deployed op must never seed golden-fixture inputs. The retired fixture
    # seeder is only reachable inside the explicitly named tests-only factory.
    op_source = inspect.getsource(dagster_defs.run_topt_live_tick)
    assert "seed_strategy_backtest_inputs" not in op_source
    assert "_load_corpus" not in op_source
    # Module-level imports carry no fixture seeder either — it is imported lazily
    # inside fixture_canary_definitions() alone.
    assert not hasattr(dagster_defs, "seed_strategy_backtest_inputs")


def test_schedule_is_enabled_hourly_with_tick_driven_identity() -> None:
    # ENABLED is deliberate (#27 appended acceptance: schedule running in Staging).
    assert topt_live_schedule.default_status == dg.DefaultScheduleStatus.RUNNING
    assert topt_live_schedule.cron_schedule == TOPT_LIVE_CRON

    # Same tick -> same run_key + executed_at (idempotent retry); distinct ticks ->
    # distinct run_key (two-cycle proof). No wall clock.
    tick = datetime(2026, 7, 20, 6, 15, 0, tzinfo=UTC)
    context = dg.build_schedule_context(scheduled_execution_time=tick)
    first = topt_live_schedule(context)
    second = topt_live_schedule(context)
    assert first.run_key == second.run_key == tick.isoformat()
    assert first.run_config["ops"]["run_topt_live_tick"]["config"]["executed_at"] == tick.isoformat()

    later = dg.build_schedule_context(scheduled_execution_time=datetime(2026, 7, 20, 7, 15, 0, tzinfo=UTC))
    assert topt_live_schedule(later).run_key != first.run_key


def test_live_version_is_tick_deterministic() -> None:
    tick = datetime(2026, 7, 20, 6, 15, 0, tzinfo=UTC)
    assert live_version_for(tick) == "live-20260720T0615"
    assert live_version_for(tick) == live_version_for(tick)  # retry-stable
    assert live_version_for(datetime(2026, 7, 20, 7, 15, 0, tzinfo=UTC)) != live_version_for(tick)


def test_fixture_canary_stays_buildable_and_explicitly_named() -> None:
    # The retired fixture path remains provable in tests, under a name that cannot
    # be mistaken for a real-source run.
    fixture_defs = fixture_canary_definitions()
    assert fixture_defs.get_job_def(CORE_STRATEGY_FIXTURE_CANARY_JOB_NAME) is not None
    assert "fixture" in CORE_STRATEGY_FIXTURE_CANARY_JOB_NAME


def test_packaged_corpus_matches_the_tests_fixture_byte_for_byte() -> None:
    # The deployed image has only site-packages, so the live pipeline reads the
    # corpus from package data. This pins the packaged copy to the canonical
    # tests fixture so the two can never drift.
    import hashlib
    from importlib import resources
    from pathlib import Path

    packaged = resources.files("data_engine.datahub.data").joinpath("corpus.v1.json").read_bytes()
    fixture = (
        Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "capture_control" / "corpus.v1.json"
    ).read_bytes()
    assert hashlib.sha256(packaged).hexdigest() == hashlib.sha256(fixture).hexdigest()


# -- the op's reporting surface (#536) --------------------------------------------------

TICK = "2026-07-30T22:15:00+00:00"
_QUALITY = {
    "available_count": 84,
    "requested_count": 84,
    "independent_reconciliation": "0.0000",
}


class _FakeConnection:
    """Stands in for the tick's psycopg connection: opened, committed, closed."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def commit(self) -> None:
        return None


def _fake_tick(monkeypatch, registration: PointerRegistration) -> None:
    """Fake every collaborator the op calls, so only the op's own reporting is under
    test. `register_run_evidence` returns the given verdict verbatim."""
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: _FakeConnection())
    monkeypatch.setattr(
        capture,
        "run_topt_pipeline",
        lambda *args, **kwargs: ToptPipelineResult(
            run_id="capture-run:" + "a" * 64,
            release_manifest_id="release-manifest:" + "b" * 64,
            core_result_count=20,
            quality_report_id="datahub-quality-report:" + "c" * 64,
            quality=dict(_QUALITY),
        ),
    )
    monkeypatch.setattr(capture, "seed_strategy_inputs_from_capture", lambda *a, **k: 21)
    monkeypatch.setattr(capture, "persist_strategy_input_coverage", lambda *a, **k: (20, 20))
    monkeypatch.setattr(
        capture,
        "run_strategy_replay_for_cutoff",
        lambda *a, **k: ("strategy-run:" + "d" * 64, 20, "snapshot:" + "e" * 64),
    )
    monkeypatch.setattr(capture, "register_run_evidence", lambda *a, **k: registration)


def _run_tick(monkeypatch, registration: PointerRegistration):
    _fake_tick(monkeypatch, registration)
    context = dg.build_op_context()
    run_topt_live_tick(context, ToptLiveTickConfig(executed_at=TICK))
    return context.get_output_metadata("result")


def test_a_withheld_pointer_names_the_failing_objective_in_op_metadata(monkeypatch) -> None:
    # #536 acceptance: the failing objective is visible without reading the database.
    metadata = _run_tick(
        monkeypatch,
        PointerRegistration(
            run_id="capture-run:" + "a" * 64,
            sequence=9,
            unmet=(UnmetObjective(objective="corroborated_share", required="0.95", observed="0.2353"),),
        ),
    )

    assert metadata["pointer_advanced"] is False
    assert metadata["pointer_sequence"] == 9  # the incumbent head, untouched
    assert "corroborated_share" in metadata["unmet_service_objectives"]
    assert "required >= 0.95" in metadata["unmet_service_objectives"]
    assert "observed 0.2353" in metadata["unmet_service_objectives"]


def test_an_accepted_pointer_reports_the_advance_it_made(monkeypatch) -> None:
    metadata = _run_tick(
        monkeypatch,
        PointerRegistration(run_id="capture-run:" + "a" * 64, sequence=10, unmet=()),
    )

    assert metadata["pointer_advanced"] is True
    assert metadata["pointer_sequence"] == 10
    assert metadata["unmet_service_objectives"] == "every objective met"


def test_the_two_environments_never_tick_at_the_same_instant() -> None:
    """One shared Twelve Data key, an 8-requests-per-MINUTE ceiling, two environments:
    same-instant crons put ~15 req/min on it and each env lost ~6 of 21 second-origin
    cells every scheduled tick (2026-08-15/16: both envs 15/21 agreed, the governed
    head frozen three days). No single-environment test could see this — the collision
    is a cross-environment property, so the guard asserts the SCHEDULING, not a fetch.
    """
    from data_engine.dagster_defs import live_topt_cron

    production = live_topt_cron("production")
    staging = live_topt_cron("staging")
    assert production != staging, "same-instant crons re-create the per-minute collision"
    # Both stay in the settled after-close window the schedule's rationale requires.
    for cron in (production, staging):
        minute, hour, dom, month, dow = cron.split()
        assert (hour, dom, month, dow) == ("22", "*", "*", "*")
        assert 0 <= int(minute) <= 59
    # The full half-hour the fix promises: one env's ~6-minute request train plus
    # throttling drift must never reach the other's window.
    assert abs(int(production.split()[0]) - int(staging.split()[0])) >= 30
    # Alias safety: APP_ENV=prod is production, not an accidental staging slot.
    assert live_topt_cron("prod") == production
    assert live_topt_cron("PRODUCTION") == production
    assert live_topt_cron("dev") == staging, "unrecognized envs share the lower-stakes slot"
