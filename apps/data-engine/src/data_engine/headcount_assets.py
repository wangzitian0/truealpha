"""Explicit Local/CI Dagster composition for the H0 E1 evidence rung."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import dagster as dg
from dagster import AssetExecutionContext
from psycopg import Connection
from pydantic import BaseModel, ConfigDict, Field, model_validator
from truealpha_contracts import RawObjectStore
from truealpha_contracts.release import ReleaseManifest

from data_engine.headcount_models import (
    D1_RUNTIME_HANDOFF_ID,
    D1_RUNTIME_HANDOFF_SHA256,
    HEADCOUNT_CORPUS_SHA256,
)
from data_engine.headcount_pipeline import H0E1Evidence, run_headcount_e1

H0_E1_ASSET_NAME = "core_headcount_extraction_e1_evidence"


class H0E1Activation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: Literal["H0-core-headcount-extraction"] = "H0-core-headcount-extraction"
    environment: Literal["local", "ci"]
    expected_corpus_sha256: str = Field(default=HEADCOUNT_CORPUS_SHA256, pattern=r"^[0-9a-f]{64}$")
    expected_d1_handoff_id: str = Field(
        default=D1_RUNTIME_HANDOFF_ID,
        pattern=r"^mvp-normalization-handoff:[0-9a-f]{64}$",
    )
    expected_d1_handoff_sha256: str = Field(
        default=D1_RUNTIME_HANDOFF_SHA256,
        pattern=r"^[0-9a-f]{64}$",
    )
    live_source_allowed: Literal[False] = False
    live_model_allowed: Literal[False] = False
    staging_allowed: Literal[False] = False
    release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_frozen_inputs(self) -> "H0E1Activation":
        if self.expected_corpus_sha256 != HEADCOUNT_CORPUS_SHA256:
            raise ValueError("H0 E1 activation corpus checksum drifted")
        if (
            self.expected_d1_handoff_id != D1_RUNTIME_HANDOFF_ID
            or self.expected_d1_handoff_sha256 != D1_RUNTIME_HANDOFF_SHA256
        ):
            raise ValueError("H0 E1 activation D1 handoff identity drifted")
        return self


@dataclass(frozen=True)
class H0E1RunnerResource:
    repository_root: Path
    connection: Connection[Any]
    raw_store: RawObjectStore
    activation: H0E1Activation

    def run(self) -> H0E1Evidence:
        evidence = run_headcount_e1(
            repository_root=self.repository_root,
            connection=self.connection,
            raw_store=self.raw_store,
            environment=self.activation.environment,
        )
        if (
            evidence.corpus_sha256 != self.activation.expected_corpus_sha256
            or evidence.runtime_handoff_id != self.activation.expected_d1_handoff_id
            or evidence.runtime_handoff_sha256 != self.activation.expected_d1_handoff_sha256
        ):
            raise ValueError("materialized H0 E1 evidence does not match its activation")
        return evidence


@dg.asset(
    name=H0_E1_ASSET_NAME,
    group_name="core_headcount_extraction_e1",
    required_resource_keys={"h0_e1_runner"},
    description="Run the frozen H0 corpus in Local/CI without live or release activation.",
)
def materialize_core_headcount_extraction_e1(
    context: AssetExecutionContext,
) -> dg.Output[H0E1Evidence]:
    runner = cast(H0E1RunnerResource, context.resources.h0_e1_runner)
    evidence = runner.run()
    return dg.Output(
        evidence,
        metadata={
            "evidence_id": evidence.evidence_id,
            "environment": evidence.environment,
            "case_count": len(evidence.case_results),
            "persisted_result_count": evidence.persisted_result_count,
            "stable_handoff": evidence.stable_handoff,
        },
        data_version=dg.DataVersion(evidence.content_sha256),
    )


def build_h0_e1_definitions(
    *,
    repository_root: Path,
    connection: Connection[Any],
    raw_store: RawObjectStore,
    activation: H0E1Activation | ReleaseManifest,
) -> dg.Definitions:
    if not isinstance(activation, H0E1Activation):
        raise ValueError("H0 E1 cannot be activated by a release manifest")
    return dg.Definitions(
        assets=[materialize_core_headcount_extraction_e1],
        resources={
            "h0_e1_runner": cast(
                Any,
                H0E1RunnerResource(
                    repository_root=repository_root,
                    connection=connection,
                    raw_store=raw_store,
                    activation=activation,
                ),
            )
        },
    )


__all__ = [
    "H0_E1_ASSET_NAME",
    "H0E1Activation",
    "H0E1RunnerResource",
    "build_h0_e1_definitions",
    "materialize_core_headcount_extraction_e1",
]
