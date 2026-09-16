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
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

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


def test_no_schedule_ever_forces_a_fetch() -> None:
    """#874: forcing is an operator's decision. A scheduled tick keeps #635's reuse
    window, which is what protects the vendor budget across the night's three ticks."""
    tick = datetime(2026, 7, 20, 6, 15, 0, tzinfo=UTC)
    for declared in capture.TICKS:
        if declared.cron is None:
            continue
        schedule = defs.get_schedule_def(declared.schedule_name)
        request = schedule(dg.build_schedule_context(scheduled_execution_time=tick))
        config = request.run_config["ops"][declared.op_name]["config"]
        assert config.get("force_fetch", False) is False, declared.key
    assert ToptLiveTickConfig(executed_at=tick.isoformat()).force_fetch is False


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
    # #544: the plausibility gate reads mart through the connection; these tests fake the
    # connection, so the gate is stubbed to a passing verdict (its own tests are
    # production_topt/test_plausibility_gate.py, against a real database).
    from data_engine.datahub.production_topt.plausibility_gate import Verdict

    monkeypatch.setattr(capture, "judge_run", lambda *a, **k: Verdict("v1", None, (), ()))


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


def _logged_tick(monkeypatch, op, registration: PointerRegistration) -> tuple[list[str], dict]:
    """Run `op` with its info/warning lines recorded (Dagster's log manager does not
    reach caplog) and return (lines, output metadata)."""
    context = dg.build_op_context()
    lines: list[str] = []
    monkeypatch.setattr(context.log, "info", lambda message, *args, **kwargs: lines.append(message))
    monkeypatch.setattr(context.log, "warning", lambda message, *args, **kwargs: lines.append(message))
    op(context, ToptLiveTickConfig(executed_at=TICK))
    return lines, context.get_output_metadata("result")


def test_lost_corroborations_reach_the_tick_summary(monkeypatch, caplog) -> None:
    """#885: the adapters' "a second origin never fails the primary" is kept, but a
    revoked key or a dead OpenD must be told apart from "the vendor had nothing". The
    capture runs the DEPLOYED market-price adapter with a second origin that raises on
    every cell; each loss is a warning, and the tick summary and op metadata carry the
    per-origin count that the tick bound around the capture."""
    import logging
    from datetime import date
    from decimal import Decimal

    from data_engine.datahub.production_topt.executor import FetchSuccess
    from data_engine.datahub.production_topt.market_price_adapter import (
        CorroboratingOrigin,
        MarketPriceAdapter,
        MarketPriceQuote,
        MarketPriceTarget,
    )
    from truealpha_contracts.datahub import CaptureWorkItem

    registration = PointerRegistration(run_id="capture-run:" + "a" * 64, sequence=11, unmet=())
    _fake_tick(monkeypatch, registration)

    def revoked(symbol: str, cutoff: date) -> MarketPriceQuote:
        raise PermissionError("twelve data key revoked")

    quote = MarketPriceQuote(
        raw_bytes=b"bar",
        close=Decimal("150.25"),
        as_of=date(2026, 7, 30),
        knowable_at=datetime(2026, 7, 30, tzinfo=UTC),
    )
    items = [
        CaptureWorkItem(
            campaign_id="capture-campaign:" + "1" * 64,
            source_request_id="source-request:" + digit * 64,
            schedule_policy_id="schedule-policy:" + "2" * 64,
        )
        for digit in ("3", "4")
    ]
    adapter = MarketPriceAdapter(
        {
            item.work_item_id: MarketPriceTarget(
                symbol, date(2026, 7, 30), "issuer:x", "security:y", f"listing:{symbol}"
            )
            for item, symbol in zip(items, ("AAPL", "MSFT"), strict=True)
        },
        lambda symbol, cutoff: quote,
        corroborating_origins=(
            CorroboratingOrigin(
                origin="twelve-data",
                parser_version="twelve-data-parser:v3",
                mapping_version="twelve-data-map:v3",
                value_key="close",
                confidence=Decimal("0.85"),
                fetch=revoked,
            ),
        ),
    )
    pipeline = ToptPipelineResult(
        run_id="capture-run:" + "a" * 64,
        release_manifest_id="release-manifest:" + "b" * 64,
        core_result_count=20,
        quality_report_id="datahub-quality-report:" + "c" * 64,
        quality=dict(_QUALITY),
    )

    def capture_through_the_adapter(*args, **kwargs) -> ToptPipelineResult:
        for item in items:
            assert isinstance(adapter.fetch(item), FetchSuccess), "the primary capture must not fail"
        return pipeline

    monkeypatch.setattr(capture, "run_topt_pipeline", capture_through_the_adapter)
    with caplog.at_level(logging.WARNING):
        lines, metadata = _logged_tick(monkeypatch, run_topt_live_tick, registration)

    [summary] = [line for line in lines if line.startswith(f"topt live tick {TICK}: capture ")]
    assert (
        "; corroborations refused 2 (twelve-data fetch 2); capacity refused 0; served by failover 0; pointer sequence 11"
        in summary
    )
    assert metadata["corroborations_refused"] == 2
    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 2 and all("twelve-data" in w and "PermissionError" in w for w in warnings)


def test_a_tick_that_lost_nothing_says_so(monkeypatch) -> None:
    """The canary's summary shape (`… (available x/y)`) carries the count too, and a tick
    whose origins all answered reports zero rather than omitting the figure."""
    from data_engine.lanes.capture import run_canary_live_tick

    registration = PointerRegistration(run_id="capture-run:" + "a" * 64, sequence=3, unmet=())
    _fake_tick(monkeypatch, registration)
    lines, metadata = _logged_tick(monkeypatch, run_canary_live_tick, registration)
    assert lines[-1] == (
        f"canary tick {TICK}: capture capture-run:{'a' * 64} (available 84/84); "
        "corroborations refused 0; capacity refused 0; served by failover 0; pointer sequence 3"
    )
    assert metadata["corroborations_refused"] == 0
    assert (metadata["capacity_refused"], metadata["budget_exhausted"]) == (0, 0)
    assert metadata["served_by_failover"] == 0


def test_a_tick_names_the_cells_served_by_failover(monkeypatch) -> None:
    """#862: a primary outage no longer empties cells, so it must not vanish either — the
    quality report's count of failover-served cells reaches the tick's summary line and
    its op metadata."""
    registration = PointerRegistration(run_id="capture-run:" + "a" * 64, sequence=4, unmet=())
    _fake_tick(monkeypatch, registration)
    monkeypatch.setattr(
        capture,
        "run_topt_pipeline",
        lambda *args, **kwargs: ToptPipelineResult(
            run_id="capture-run:" + "a" * 64,
            release_manifest_id="release-manifest:" + "b" * 64,
            core_result_count=20,
            quality_report_id="datahub-quality-report:" + "c" * 64,
            quality={**_QUALITY, "served_by_failover_count": 2},
        ),
    )
    lines, metadata = _logged_tick(monkeypatch, run_topt_live_tick, registration)
    [summary] = [line for line in lines if line.startswith(f"topt live tick {TICK}: capture ")]
    assert "; corroborations refused 0; capacity refused 0; served by failover 2; pointer sequence 4" in summary
    assert metadata["served_by_failover"] == 2


def _spent_today(call_ledger, source: str, count: int) -> None:
    from data_engine.sources import gateway

    now = datetime.now(UTC)
    # Today, and (except in the first minutes after midnight) outside every rate window;
    # the gate checks the budget before it queues, so the exception is harmless.
    at = max(now - timedelta(minutes=5), datetime.combine(now.date(), datetime.min.time(), tzinfo=UTC))
    call_ledger.extend(
        [gateway.CallRecord(source=source, endpoint="x", caller="earlier", called_at=at, ok=True)] * count
    )


def test_an_exhausted_budget_is_named_by_the_deployed_tick(monkeypatch, call_ledger) -> None:
    """Rule 6 through the deployed entry point (#729): the tick binds the capacity gate, so
    with production's 480-credit share of the shared Twelve Data key spent, the REAL
    Twelve Data fetcher is refused before it reaches the vendor — the cell stays
    single-origin with the loss at the `budget` stage — and a spent Yahoo budget defers
    the primary cell. The tick summary and op metadata name both. Red against a tick
    that only records: the fake vendor below fails any request that gets through."""
    from datetime import date

    from data_engine.config import settings
    from data_engine.datahub.production_topt import twelve_data_origin
    from data_engine.datahub.production_topt.executor import FetchFailure, FetchSuccess
    from data_engine.datahub.production_topt.market_price_adapter import (
        MarketPriceAdapter,
        MarketPriceQuote,
        MarketPriceTarget,
        yahoo_quote_fetcher,
    )
    from data_engine.sources import yahoo
    from truealpha_contracts.datahub import CaptureWorkItem
    from truealpha_contracts.obligation_reason_codes import ObligationReasonCode

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "twelve_data_api_key", "k")
    monkeypatch.setattr(twelve_data_origin.time, "sleep", lambda _seconds: None)
    registration = PointerRegistration(run_id="capture-run:" + "a" * 64, sequence=12, unmet=())
    _fake_tick(monkeypatch, registration)

    def no_request(*args, **kwargs):
        raise AssertionError("a refused request reached the vendor")

    class NoRequestClient:
        def __init__(self, **_kwargs: object) -> None: ...

        def __enter__(self):
            return self

        def __exit__(self, *_exc: object) -> None: ...

        get = staticmethod(no_request)

    monkeypatch.setattr(twelve_data_origin.urllib.request, "urlopen", no_request)
    monkeypatch.setattr(yahoo.httpx, "Client", NoRequestClient)
    _spent_today(call_ledger, "twelvedata", 480)
    origin = twelve_data_origin.twelve_data_origin()
    assert origin is not None
    items = [
        CaptureWorkItem(
            campaign_id="capture-campaign:" + "1" * 64,
            source_request_id="source-request:" + digit * 64,
            schedule_policy_id="schedule-policy:" + "2" * 64,
        )
        for digit in ("3", "4")
    ]
    target = MarketPriceTarget("AAPL", date(2026, 7, 30), "issuer:x", "security:y", "listing:aapl")
    quote = MarketPriceQuote(b"bar", Decimal("150.25"), date(2026, 7, 30), datetime(2026, 7, 30, tzinfo=UTC))
    corroborated = MarketPriceAdapter(
        {items[0].work_item_id: target}, lambda s, c: quote, corroborating_origins=(origin,)
    )
    deployed_primary = MarketPriceAdapter({items[1].work_item_id: target}, yahoo_quote_fetcher)
    outcomes: list = []

    def capture_under_the_gate(*args, **kwargs) -> ToptPipelineResult:
        outcomes.append(corroborated.fetch(items[0]))
        _spent_today(call_ledger, "yahoo", 2000)
        outcomes.append(deployed_primary.fetch(items[1]))
        return ToptPipelineResult(
            run_id="capture-run:" + "a" * 64,
            release_manifest_id="release-manifest:" + "b" * 64,
            core_result_count=20,
            quality_report_id="datahub-quality-report:" + "c" * 64,
            quality=dict(_QUALITY),
        )

    monkeypatch.setattr(capture, "run_topt_pipeline", capture_under_the_gate)
    lines, metadata = _logged_tick(monkeypatch, run_topt_live_tick, registration)

    single_origin, deferred = outcomes
    assert isinstance(single_origin, FetchSuccess) and single_origin.corroborations == ()
    assert isinstance(deferred, FetchFailure) and deferred.reason_code is ObligationReasonCode.DEFERRED_CAPACITY
    [summary] = [line for line in lines if line.startswith(f"topt live tick {TICK}: capture ")]
    assert (
        "; corroborations refused 1 (twelve-data budget 1); capacity refused 2 (twelvedata budget 1, yahoo budget 1);"
        in summary
    )
    assert (metadata["budget_exhausted"], metadata["capacity_refused"]) == (2, 2)


def test_a_failed_tick_still_says_what_the_gate_refused(monkeypatch, call_ledger) -> None:
    """A capture refused for its shortfall (#538) raises before the summary line; the run
    log must still name the budget that caused it."""
    import pytest
    from data_engine.config import settings
    from data_engine.sources import gateway

    monkeypatch.setattr(settings, "app_env", "staging")
    _fake_tick(monkeypatch, PointerRegistration(run_id="capture-run:" + "a" * 64, sequence=1, unmet=()))
    _spent_today(call_ledger, "sec", 5000)

    def refused_capture(*args, **kwargs):
        with gateway.record_call("sec", "companyfacts", caller="test"):
            raise AssertionError("a refused request ran")

    monkeypatch.setattr(capture, "run_topt_pipeline", refused_capture)
    context = dg.build_op_context()
    lines: list[str] = []
    monkeypatch.setattr(context.log, "warning", lambda message, *args, **kwargs: lines.append(message))
    with pytest.raises(gateway.BudgetExhausted):
        run_topt_live_tick(context, ToptLiveTickConfig(executed_at=TICK))
    assert lines == [f"topt live tick {TICK} failed; corroborations refused 0; capacity refused 1 (sec budget 1)"]


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


# -- #72 scope 4: ticks are declarations; the factory builds what they say -------------------


def test_every_declared_tick_is_deployed_under_its_own_names_and_is_a_trigger_target() -> None:
    """A universe is one entry in `capture.TICKS`. Each entry must reach the deployed
    root as a job whose only op carries the declared op name (operators launch by it,
    the sensor dispatches by it), and the manual-trigger sensor must target it."""
    from data_engine.lanes import triggers

    sensor_targets = {job.name for job in triggers.pipeline_trigger_sensor.jobs}
    for tick in capture.TICKS:
        job = defs.get_job_def(tick.job_name)
        assert {node.name for node in job.graph.node_defs} == {tick.op_name}, tick.key
        if tick.cron is not None:
            schedule = defs.get_schedule_def(tick.schedule_name)
            assert schedule.cron_schedule == tick.cron and schedule.job.name == tick.job_name
        assert tick.job_name in sensor_targets, f"{tick.key} is not a manual-trigger target"
    assert capture.TICK_BY_JOB == {tick.job_name: tick for tick in capture.TICKS}


# -- #874: a manual tick can force a fresh vendor fetch -----------------------------------------


def _capturing_tick(monkeypatch, calls: list[dict]) -> None:
    """`_fake_tick`, except the pipeline records the keywords the op called it with and
    reports back the forcing it was asked for, the way the real one does."""
    _fake_tick(monkeypatch, PointerRegistration(run_id="capture-run:" + "a" * 64, sequence=10, unmet=()))

    def pipeline(*args, **kwargs):
        calls.append(kwargs)
        return ToptPipelineResult(
            run_id="capture-run:" + "a" * 64,
            release_manifest_id="release-manifest:" + "b" * 64,
            core_result_count=20,
            quality_report_id="datahub-quality-report:" + "c" * 64,
            quality=dict(_QUALITY),
            forced_fetch=kwargs.get("force_fetch", False),
        )

    monkeypatch.setattr(capture, "run_topt_pipeline", pipeline)


def test_a_forced_launch_reaches_the_pipeline_and_says_so_in_op_metadata(monkeypatch) -> None:
    """Through the deployed op, not the pipeline function: the config an operator
    launches with (GraphQL or the admin trigger) must arrive at `run_topt_pipeline`."""
    calls: list[dict] = []
    _capturing_tick(monkeypatch, calls)

    forced = dg.build_op_context()
    run_topt_live_tick(forced, ToptLiveTickConfig(executed_at=TICK, force_fetch=True))
    ordinary = dg.build_op_context()
    run_topt_live_tick(ordinary, ToptLiveTickConfig(executed_at=TICK))

    assert [call["force_fetch"] for call in calls] == [True, False]
    # One tick time, one version handed down; the pipeline owns the forced identity.
    assert calls[0]["version"] == calls[1]["version"] == live_version_for(datetime.fromisoformat(TICK))
    assert forced.get_output_metadata("result")["forced_fetch"] is True
    assert ordinary.get_output_metadata("result")["forced_fetch"] is False


def _documented_launch() -> dict:
    root = next(parent for parent in Path(__file__).resolve().parents if (parent / "docs").is_dir())
    text = (root / "docs" / "datahub-quality-report.md").read_text(encoding="utf-8")
    section = text.split("## Manual re-run with a forced fetch", 1)
    assert len(section) == 2, "docs/datahub-quality-report.md lost its forced re-run section"
    blocks = re.findall(r"```json\n(.*?)```", section[1], flags=re.DOTALL)
    variables = [json.loads(block) for block in blocks if '"executionParams"' in block]
    assert len(variables) == 1, "the section documents exactly one GraphQL launch payload"
    return variables[0]


def test_the_documented_graphql_launch_is_a_valid_forced_topt_tick() -> None:
    """The operator doc is copied verbatim into a GraphQL client, so it is checked like
    code: the job, the op and the config it names must be the deployed ones."""
    params = _documented_launch()["executionParams"]
    assert params["selector"]["jobName"] == capture.topt_live_pipeline_job.name
    assert params["selector"]["repositoryName"] == "__repository__"
    run_config = params["runConfigData"]
    assert set(run_config["ops"]) == {"run_topt_live_tick"}
    config = run_config["ops"]["run_topt_live_tick"]["config"]
    assert set(config) == {"executed_at", "force_fetch"} and config["force_fetch"] is True
    datetime.fromisoformat(config["executed_at"])
    assert dg.validate_run_config(capture.topt_live_pipeline_job, run_config)
