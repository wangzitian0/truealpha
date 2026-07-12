"""Executable fixture-backed Dagster probe for the Gate 1 runner contract."""

from datetime import datetime, timedelta
from typing import Any, Protocol, cast

import dagster as dg
from dagster import AssetExecutionContext
from factors.base.registered_semantic_probe import (
    PROBE_FACTOR_VERSION,
    PROBE_IMPLEMENTATION_SHA256,
    registered_semantic_probe,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator
from truealpha_contracts.execution import (
    FactorExecution,
    FactorInvocationTemplate,
    FactorKind,
    InputReadEvent,
    MaterializedFactorBatch,
    MaterializedFactorOutput,
    RunnerInputSelection,
    SnapshotManifest,
    build_runner_input_selection,
    materialize_factor_output,
)
from truealpha_contracts.models import _require_aware
from truealpha_contracts.universe import SubjectRef

SNAPSHOT_ASSET_NAME = "mvp_fixture_snapshot"
PROBE_ASSET_NAME = "mvp_contract_probe_output"


class ProbeExecutionSpec(BaseModel):
    """Immutable runner inputs injected into the local/CI Definitions object."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    template: FactorInvocationTemplate
    subject: SubjectRef
    started_at: datetime
    runner_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/@+-]*$")
    runner_version: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/@+-]*$")
    runner_implementation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repository_commit_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/@+-]*$")

    @field_validator("started_at")
    @classmethod
    def validate_started_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "started_at")

    def model_post_init(self, _context: object) -> None:
        if self.template.factor_kind is not FactorKind.BASE:
            raise ValueError("the fixture probe requires a base factor template")
        if (
            self.template.factor_id != "registered_semantic_probe"
            or self.template.factor_version != PROBE_FACTOR_VERSION
            or self.template.factor_implementation_sha256 != PROBE_IMPLEMENTATION_SHA256
        ):
            raise ValueError("the fixture probe template does not bind the registered implementation exactly")


class ProbeExecutionResult(BaseModel):
    """Complete runner-owned evidence produced by the Dagster factor asset."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    execution: FactorExecution
    selection: RunnerInputSelection
    read_events: tuple[InputReadEvent, ...]
    output: MaterializedFactorOutput
    batch: MaterializedFactorBatch


class ProbeRepository(Protocol):
    def resolve_snapshot(self) -> SnapshotManifest: ...

    def put_output(self, output: MaterializedFactorOutput) -> bool: ...

    def put_batch(self, batch: MaterializedFactorBatch) -> bool: ...


class FixtureProbeRepository:
    """Content-preserving in-memory repository used only by local/CI probes."""

    def __init__(self, snapshot: SnapshotManifest) -> None:
        self._active_snapshot_id = snapshot.snapshot_id
        self._snapshots = {snapshot.snapshot_id: snapshot}
        self._outputs: dict[str, MaterializedFactorOutput] = {}
        self._batches: dict[str, MaterializedFactorBatch] = {}
        self.resolution_count = 0

    def resolve_snapshot(self) -> SnapshotManifest:
        self.resolution_count += 1
        snapshot = self.get(self._active_snapshot_id)
        if snapshot is None:
            raise ValueError("the active fixture snapshot is missing")
        return snapshot

    def put(self, snapshot: SnapshotManifest) -> bool:
        existing = self._snapshots.get(snapshot.snapshot_id)
        if existing is not None and existing != snapshot:
            raise ValueError("snapshot ID collision")
        self._snapshots[snapshot.snapshot_id] = snapshot
        return existing is None

    def get(self, snapshot_id: str) -> SnapshotManifest | None:
        return self._snapshots.get(snapshot_id)

    def put_output(self, output: MaterializedFactorOutput) -> bool:
        existing = self._outputs.get(output.materialized_output_id)
        if existing is not None and existing != output:
            raise ValueError("factor output ID collision")
        self._outputs[output.materialized_output_id] = output
        return existing is None

    def put_batch(self, batch: MaterializedFactorBatch) -> bool:
        existing = self._batches.get(batch.materialized_batch_id)
        if existing is not None and existing != batch:
            raise ValueError("factor batch ID collision")
        self._batches[batch.materialized_batch_id] = batch
        return existing is None

    def get_output(self, output_id: str) -> MaterializedFactorOutput | None:
        return self._outputs.get(output_id)

    def get_batch(self, batch_id: str) -> MaterializedFactorBatch | None:
        return self._batches.get(batch_id)


def execute_contract_probe(
    *,
    repository: ProbeRepository,
    snapshot: SnapshotManifest,
    spec: ProbeExecutionSpec,
) -> ProbeExecutionResult:
    """Run the ordinary factor function while the runner derives all evidence."""

    if spec.started_at < snapshot.resolved_at:
        raise ValueError("factor execution cannot start before snapshot resolution")
    execution = FactorExecution(
        template=spec.template,
        snapshot_id=snapshot.snapshot_id,
        snapshot_sha256=snapshot.content_sha256,
        ordered_subjects=(spec.subject,),
        started_at=spec.started_at,
    )
    selection = build_runner_input_selection(
        execution=execution,
        snapshot=snapshot,
        selected_at=spec.started_at,
        runner_id=spec.runner_id,
        runner_version=spec.runner_version,
        runner_implementation_sha256=spec.runner_implementation_sha256,
    )
    draft = registered_semantic_probe(subject=spec.subject, inputs=selection.factor_inputs)
    read_events = tuple(
        InputReadEvent(
            factor_execution_id=execution.factor_execution_id,
            selection_id=selection.selection_id,
            requirement_handle_id=binding.handle.requirement_handle_id,
            output_key=draft.output_key,
            read_index=index,
            trace_id=f"trace:mvp-probe:{index}",
            occurred_at=spec.started_at + timedelta(microseconds=index + 1),
        )
        for index, binding in enumerate(selection.bindings)
    )
    materialized_at = spec.started_at + timedelta(microseconds=len(read_events) + 1)
    output = materialize_factor_output(
        execution=execution,
        snapshot=snapshot,
        selection=selection,
        draft=draft,
        read_events=read_events,
        materialized_at=materialized_at,
    )
    batch = MaterializedFactorBatch(
        factor_execution_id=execution.factor_execution_id,
        snapshot_id=snapshot.snapshot_id,
        output_ids=(output.materialized_output_id,),
        repository_commit_id=spec.repository_commit_id,
        persisted_at=materialized_at + timedelta(microseconds=1),
    )
    repository.put_output(output)
    repository.put_batch(batch)
    return ProbeExecutionResult(
        execution=execution,
        selection=selection,
        read_events=read_events,
        output=output,
        batch=batch,
    )


def _repository(context: AssetExecutionContext) -> ProbeRepository:
    return cast(ProbeRepository, context.resources.mvp_probe_repository)


def _spec(context: AssetExecutionContext) -> ProbeExecutionSpec:
    return cast(ProbeExecutionSpec, context.resources.mvp_probe_spec)


@dg.asset(
    name=SNAPSHOT_ASSET_NAME,
    group_name="mvp_execution_probe",
    required_resource_keys={"mvp_probe_repository"},
    description="Resolve the complete fixture snapshot atomically through the injected repository.",
)
def materialize_mvp_fixture_snapshot(context: AssetExecutionContext) -> dg.Output[SnapshotManifest]:
    snapshot = _repository(context).resolve_snapshot()
    return dg.Output(
        snapshot,
        metadata={
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_as_of": snapshot.request.as_of.isoformat(),
        },
        data_version=dg.DataVersion(snapshot.content_sha256),
    )


@dg.asset(
    name=PROBE_ASSET_NAME,
    group_name="mvp_execution_probe",
    required_resource_keys={"mvp_probe_repository", "mvp_probe_spec"},
    description="Delegate the snapshot to the registered probe factor and persist runner-owned output evidence.",
)
def materialize_mvp_contract_probe(
    context: AssetExecutionContext,
    mvp_fixture_snapshot: SnapshotManifest,
) -> dg.Output[ProbeExecutionResult]:
    spec = _spec(context)
    result = execute_contract_probe(
        repository=_repository(context),
        snapshot=mvp_fixture_snapshot,
        spec=spec,
    )
    return dg.Output(
        result,
        metadata={
            "factor_id": spec.template.factor_id,
            "factor_version": spec.template.factor_version,
            "factor_template_id": spec.template.factor_template_id,
            "snapshot_id": mvp_fixture_snapshot.snapshot_id,
            "materialized_output_id": result.output.materialized_output_id,
            "materialized_batch_id": result.batch.materialized_batch_id,
        },
        data_version=dg.DataVersion(result.output.content_sha256),
    )


MVP_PROBE_ASSETS = (materialize_mvp_fixture_snapshot, materialize_mvp_contract_probe)


def build_mvp_probe_definitions(
    *,
    repository: ProbeRepository,
    spec: ProbeExecutionSpec,
) -> dg.Definitions:
    return dg.Definitions(
        assets=list(MVP_PROBE_ASSETS),
        resources={
            "mvp_probe_repository": cast(Any, repository),
            "mvp_probe_spec": spec,
        },
    )


__all__ = [
    "FixtureProbeRepository",
    "MVP_PROBE_ASSETS",
    "PROBE_ASSET_NAME",
    "ProbeExecutionResult",
    "ProbeExecutionSpec",
    "ProbeRepository",
    "SNAPSHOT_ASSET_NAME",
    "build_mvp_probe_definitions",
    "execute_contract_probe",
    "materialize_mvp_contract_probe",
    "materialize_mvp_fixture_snapshot",
]
