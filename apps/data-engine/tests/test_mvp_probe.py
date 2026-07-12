import runpy
from datetime import timedelta
from functools import cache
from pathlib import Path
from typing import Any

import dagster as dg
import pytest
from data_engine.mvp_probe import (
    PROBE_ASSET_NAME,
    FixtureProbeRepository,
    ProbeExecutionResult,
    ProbeExecutionSpec,
    build_mvp_probe_definitions,
)
from factors.base.registered_semantic_probe import PROBE_FACTOR_VERSION, PROBE_IMPLEMENTATION_SHA256
from pydantic import ValidationError
from truealpha_contracts.execution import FactorInvocationTemplate, FactorKind, SnapshotManifest


@cache
def _contract_fixtures() -> dict[str, Any]:
    return runpy.run_path(str(Path(__file__).with_name("test_contract_repository.py")))


def _snapshot() -> SnapshotManifest:
    snapshot = _contract_fixtures()["_snapshot"]()
    assert isinstance(snapshot, SnapshotManifest)
    return snapshot


def _template(snapshot: SnapshotManifest, *, implementation_sha256: str = PROBE_IMPLEMENTATION_SHA256):
    return FactorInvocationTemplate(
        factor_id="registered_semantic_probe",
        factor_version=PROBE_FACTOR_VERSION,
        factor_implementation_sha256=implementation_sha256,
        factor_kind=FactorKind.BASE,
        parameter_model_key="contracts:NoParameters",
        parameter_schema_sha256="a" * 64,
        canonical_parameters_sha256="b" * 64,
        data_requirement_ids=tuple(cell.requirement_id for cell in snapshot.request.demand_cells),
    )


def _spec(snapshot: SnapshotManifest) -> ProbeExecutionSpec:
    return ProbeExecutionSpec(
        template=_template(snapshot),
        subject=snapshot.resolved_subjects[0],
        started_at=snapshot.resolved_at + timedelta(seconds=1),
        runner_id="mvp-probe-runner",
        runner_version="1.0.0",
        runner_implementation_sha256="c" * 64,
        repository_commit_id="fixture:mvp-probe",
    )


def test_definitions_execute_fixture_snapshot_to_persisted_probe_batch() -> None:
    snapshot = _snapshot()
    repository = FixtureProbeRepository(snapshot)
    definitions = build_mvp_probe_definitions(repository=repository, spec=_spec(snapshot))

    dg.Definitions.validate_loadable(definitions)
    result = definitions.get_implicit_global_asset_job_def().execute_in_process()

    assert result.success
    probe = result.output_for_node(PROBE_ASSET_NAME)
    assert isinstance(probe, ProbeExecutionResult)
    assert repository.resolution_count == 1
    assert repository.get(snapshot.snapshot_id) == snapshot
    assert repository.put(snapshot) is False
    assert probe.output.as_of == snapshot.request.as_of
    assert probe.output.minimum_consumed_confidence == min(record.confidence for record in snapshot.normalized_records)
    assert repository.get_output(probe.output.materialized_output_id) == probe.output
    assert repository.get_batch(probe.batch.materialized_batch_id) == probe.batch
    materialization = next(
        event.event_specific_data.materialization
        for event in result.all_events
        if event.is_step_materialization and event.step_key == PROBE_ASSET_NAME
    )
    metadata = materialization.metadata
    assert metadata["factor_version"].value == PROBE_FACTOR_VERSION
    assert metadata["snapshot_id"].value == snapshot.snapshot_id


def test_probe_spec_rejects_an_unregistered_implementation_digest() -> None:
    snapshot = _snapshot()

    with pytest.raises(ValidationError, match="registered implementation exactly"):
        ProbeExecutionSpec(
            **_spec(snapshot).model_dump(exclude={"template"}),
            template=_template(snapshot, implementation_sha256="d" * 64),
        )
