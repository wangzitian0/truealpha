"""Persistent Dagster definitions for the bounded TOPT Staging capture."""

import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import dagster as dg
from dagster import AssetExecutionContext, ScheduleEvaluationContext
from truealpha_contracts import CaptureManifest, CaptureScope, canonical_sha256
from truealpha_runtime import EnvironmentTier

from data_engine import db
from data_engine.capture import manifest as manifest_builder
from data_engine.capture import repository, runs, source_results
from data_engine.capture.topt import build_topt_scope
from data_engine.capture.topt_identity import capture as capture_identity
from data_engine.capture.topt_identity import emit_source_results as emit_identity_results
from data_engine.capture.topt_sources import (
    capture_moomoo_domains,
    capture_sec_filings,
    capture_sec_financials,
    capture_yahoo_prices,
)
from data_engine.config import settings

GROUP_NAME = "topt_staging_capture"
JOB_NAME = "topt_staging_capture_job"
SCHEDULE_NAME = "topt_staging_daily_schedule"
CODE_VERSION = "topt-capture:1"
RETRY_POLICY = dg.RetryPolicy(
    max_retries=2,
    delay=30,
    backoff=dg.Backoff.EXPONENTIAL,
    jitter=dg.Jitter.PLUS_MINUS,
)

ScopeOutput = dict[str, str]
PhaseOutput = dict[str, Any]
Phase = Callable[[Any, str, CaptureScope, int], tuple[int, ...]]


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise dg.Failure(f"{name} must bind the promoted release before capture starts")
    return value


def _reuse_openfigi_raw() -> bool:
    return os.getenv("TRUEALPHA_REUSE_OPENFIGI_RAW", "").strip().lower() in {"1", "true", "yes"}


def _scope_output(binding: runs.CaptureRunBinding) -> ScopeOutput:
    return {
        "capture_scope_id": binding.capture_scope_id,
        "release_manifest_id": binding.release_manifest_id,
        "image_digest": binding.image_digest,
        "configuration_sha256": binding.configuration_sha256,
        "started_at": binding.started_at.isoformat(),
    }


def _bound_scope(conn, *, run_id: str, expected: ScopeOutput) -> tuple[runs.CaptureRunBinding, CaptureScope]:
    binding = runs.get(conn, run_id)
    if binding is None:
        raise dg.Failure(f"capture run {run_id} has no immutable pre-run binding")
    if _scope_output(binding) != expected:
        raise dg.Failure(f"capture run {run_id} input does not match its immutable binding")
    return binding, repository.get_scope(conn, binding.capture_scope_id)


def _phase_output(conn, *, result_ids: tuple[int, ...], phase_name: str) -> dg.Output[PhaseOutput]:
    digest = source_results.evidence_digest(conn, result_ids)
    return dg.Output(
        value={
            "phase": phase_name,
            "source_result_count": len(result_ids),
            "evidence_sha256": digest,
        },
        metadata={
            "source_result_count": len(result_ids),
            "evidence_sha256": digest,
        },
        data_version=dg.DataVersion(digest),
    )


def _run_phase(
    context: AssetExecutionContext,
    scope_output: ScopeOutput,
    *,
    phase_name: str,
    phase: Phase,
) -> dg.Output[PhaseOutput]:
    conn = db.connect()
    try:
        _binding, scope = _bound_scope(conn, run_id=context.run.run_id, expected=scope_output)
        result_ids = phase(conn, context.run.run_id, scope, context.retry_number)
        conn.commit()
        return _phase_output(conn, result_ids=result_ids, phase_name=phase_name)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@dg.asset(
    group_name=GROUP_NAME,
    code_version=CODE_VERSION,
    retry_policy=RETRY_POLICY,
    owners=["team:data"],
    description="Freeze the exact TOPT Staging scope and promoted deployment before source calls.",
)
def topt_capture_scope(context: AssetExecutionContext) -> dg.Output[ScopeOutput]:
    if settings.environment_tier is EnvironmentTier.PRODUCTION:
        raise dg.Failure("the TOPT Staging scope cannot execute with APP_ENV=production")
    scope = build_topt_scope(approved_by=os.getenv("TRUEALPHA_CAPTURE_APPROVED_BY", "issue:63"))
    conn = db.connect()
    try:
        repository.put_scope(conn, scope)
        existing = runs.get(conn, context.run.run_id)
        binding = runs.CaptureRunBinding(
            run_id=context.run.run_id,
            capture_scope_id=scope.capture_scope_id,
            release_manifest_id=_required_env("TRUEALPHA_RELEASE_MANIFEST_ID"),
            image_digest=_required_env("TRUEALPHA_DATA_ENGINE_IMAGE_DIGEST"),
            configuration_sha256=_required_env("TRUEALPHA_CONFIGURATION_SHA256"),
            schedule_name=context.run.tags.get("dagster/schedule_name", "manual:topt_staging_capture_job"),
            started_at=existing.started_at if existing is not None else datetime.now(UTC),
        )
        runs.put(conn, scope=scope, binding=binding)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    output = _scope_output(binding)
    return dg.Output(
        value=output,
        metadata={
            "capture_scope_id": scope.capture_scope_id,
            "subject_count": len(scope.subjects),
            "required_cell_count": len(scope.requirements),
            "release_manifest_id": binding.release_manifest_id,
            "image_digest": binding.image_digest,
            "configuration_sha256": binding.configuration_sha256,
        },
        data_version=dg.DataVersion(scope.capture_scope_id.removeprefix("capture-scope:")),
    )


def _identity_phase(conn, run_id: str, scope: CaptureScope, attempt: int) -> tuple[int, ...]:
    result = capture_identity(conn, reuse_openfigi_raw=_reuse_openfigi_raw())
    return emit_identity_results(conn, run_id=run_id, scope=scope, result=result, attempt=attempt)


def _sec_financial_phase(conn, run_id: str, scope: CaptureScope, attempt: int) -> tuple[int, ...]:
    return capture_sec_financials(conn, run_id=run_id, scope=scope, attempt=attempt)


def _sec_filing_phase(conn, run_id: str, scope: CaptureScope, attempt: int) -> tuple[int, ...]:
    return capture_sec_filings(conn, run_id=run_id, scope=scope, attempt=attempt)


def _yahoo_phase(conn, run_id: str, scope: CaptureScope, attempt: int) -> tuple[int, ...]:
    return capture_yahoo_prices(conn, run_id=run_id, scope=scope, attempt=attempt)


def _moomoo_phase(conn, run_id: str, scope: CaptureScope, attempt: int) -> tuple[int, ...]:
    return capture_moomoo_domains(conn, run_id=run_id, scope=scope, attempt=attempt)


@dg.asset(group_name=GROUP_NAME, code_version=CODE_VERSION, retry_policy=RETRY_POLICY, owners=["team:data"])
def topt_identity(context: AssetExecutionContext, topt_capture_scope: ScopeOutput) -> dg.Output[PhaseOutput]:
    return _run_phase(context, topt_capture_scope, phase_name="identity", phase=_identity_phase)


@dg.asset(group_name=GROUP_NAME, code_version=CODE_VERSION, retry_policy=RETRY_POLICY, owners=["team:data"])
def topt_sec_financials(
    context: AssetExecutionContext,
    topt_capture_scope: ScopeOutput,
    topt_identity: PhaseOutput,
) -> dg.Output[PhaseOutput]:
    del topt_identity
    return _run_phase(context, topt_capture_scope, phase_name="sec_financials", phase=_sec_financial_phase)


@dg.asset(group_name=GROUP_NAME, code_version=CODE_VERSION, retry_policy=RETRY_POLICY, owners=["team:data"])
def topt_sec_filings(
    context: AssetExecutionContext,
    topt_capture_scope: ScopeOutput,
    topt_identity: PhaseOutput,
) -> dg.Output[PhaseOutput]:
    del topt_identity
    return _run_phase(context, topt_capture_scope, phase_name="sec_filings", phase=_sec_filing_phase)


@dg.asset(group_name=GROUP_NAME, code_version=CODE_VERSION, retry_policy=RETRY_POLICY, owners=["team:data"])
def topt_yahoo_prices(
    context: AssetExecutionContext,
    topt_capture_scope: ScopeOutput,
    topt_identity: PhaseOutput,
) -> dg.Output[PhaseOutput]:
    del topt_identity
    return _run_phase(context, topt_capture_scope, phase_name="yahoo_prices", phase=_yahoo_phase)


@dg.asset(group_name=GROUP_NAME, code_version=CODE_VERSION, retry_policy=RETRY_POLICY, owners=["team:data"])
def topt_moomoo_domains(
    context: AssetExecutionContext,
    topt_capture_scope: ScopeOutput,
    topt_identity: PhaseOutput,
) -> dg.Output[PhaseOutput]:
    del topt_identity
    return _run_phase(context, topt_capture_scope, phase_name="moomoo_domains", phase=_moomoo_phase)


def _manifest_data_version(manifest: CaptureManifest) -> str:
    return canonical_sha256(
        {
            "capture_scope_id": manifest.scope.capture_scope_id,
            "cells": [
                {
                    "key": (cell.subject_id, cell.domain.value, cell.partition_key),
                    "status": cell.status.value,
                    "content_sha256": cell.content_sha256,
                }
                for cell in manifest.cells
            ],
        }
    )


@dg.asset(group_name=GROUP_NAME, code_version=CODE_VERSION, retry_policy=RETRY_POLICY, owners=["team:data"])
def topt_capture_manifest(
    context: AssetExecutionContext,
    topt_capture_scope: ScopeOutput,
    topt_identity: PhaseOutput,
    topt_sec_financials: PhaseOutput,
    topt_sec_filings: PhaseOutput,
    topt_yahoo_prices: PhaseOutput,
    topt_moomoo_domains: PhaseOutput,
) -> dg.Output[dict[str, Any]]:
    del topt_identity, topt_sec_financials, topt_sec_filings, topt_yahoo_prices, topt_moomoo_domains
    conn = db.connect()
    try:
        binding, scope = _bound_scope(conn, run_id=context.run.run_id, expected=topt_capture_scope)
        manifest = repository.get_manifest_for_run(
            capture_scope_id=scope.capture_scope_id, run_id=context.run.run_id, conn=conn
        )
        if manifest is None:
            for requirement in scope.requirements:
                if source_results.for_cell(conn, context.run.run_id, requirement):
                    source_results.finalize(conn, run_id=context.run.run_id, requirement=requirement)
            completed_at = datetime.now(UTC)
            manifest = manifest_builder.build(
                conn,
                scope=scope,
                run_id=context.run.run_id,
                image_digest=binding.image_digest,
                as_of=completed_at,
                started_at=binding.started_at,
                completed_at=completed_at,
            )
            repository.put_manifest(conn, manifest)
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    complete_count = sum(cell.status.value == "complete" for cell in manifest.cells)
    value = {
        "capture_manifest_id": manifest.capture_manifest_id,
        "capture_scope_id": scope.capture_scope_id,
        "status": manifest.status.value,
        "cell_count": len(manifest.cells),
        "complete_cell_count": complete_count,
        "blockers": list(manifest.blockers),
    }
    return dg.Output(
        value=value,
        metadata={
            "capture_manifest_id": manifest.capture_manifest_id,
            "capture_scope_id": scope.capture_scope_id,
            "status": manifest.status.value,
            "cell_count": len(manifest.cells),
            "complete_cell_count": complete_count,
            "blockers": dg.MetadataValue.json(list(manifest.blockers)),
        },
        data_version=dg.DataVersion(_manifest_data_version(manifest)),
    )


@dg.asset_check(asset=topt_capture_manifest, blocking=True, description="Every frozen TOPT cell must pass.")
def topt_capture_manifest_complete(topt_capture_manifest: dict[str, Any]) -> dg.AssetCheckResult:
    passed = topt_capture_manifest["status"] == "pass"
    return dg.AssetCheckResult(
        passed=passed,
        metadata={
            "capture_manifest_id": str(topt_capture_manifest["capture_manifest_id"]),
            "complete_cell_count": int(topt_capture_manifest["complete_cell_count"]),
            "cell_count": int(topt_capture_manifest["cell_count"]),
            "blockers": dg.MetadataValue.json(topt_capture_manifest["blockers"]),
        },
        description=None if passed else "The persisted row-complete manifest has blocking cells.",
    )


topt_staging_capture_job = dg.define_asset_job(
    JOB_NAME,
    selection=dg.AssetSelection.groups(GROUP_NAME),
    description="Bounded real-source TOPT Staging capture; manual launches do not satisfy the schedule gate.",
)


@dg.schedule(
    cron_schedule="30 6 * * *",
    execution_timezone="UTC",
    job=topt_staging_capture_job,
    name=SCHEDULE_NAME,
    default_status=dg.DefaultScheduleStatus.RUNNING,
)
def topt_staging_daily_schedule(context: ScheduleEvaluationContext):
    if settings.environment_tier is not EnvironmentTier.STAGING:
        return dg.SkipReason("TOPT real-source schedule only runs with APP_ENV=staging")
    scheduled_at = context.scheduled_execution_time
    if scheduled_at is None:
        return dg.SkipReason("scheduled execution time is unavailable")
    return dg.RunRequest(
        run_key=f"topt-staging:{scheduled_at.isoformat()}",
        tags={
            "truealpha/environment": "staging",
            "truealpha/capture_scope": "topt-accession-000207169126012475",
            "truealpha/scheduled_at": scheduled_at.isoformat(),
        },
    )


defs = dg.Definitions(
    assets=[
        topt_capture_scope,
        topt_identity,
        topt_sec_financials,
        topt_sec_filings,
        topt_yahoo_prices,
        topt_moomoo_domains,
        topt_capture_manifest,
    ],
    asset_checks=[topt_capture_manifest_complete],
    jobs=[topt_staging_capture_job],
    schedules=[topt_staging_daily_schedule],
)
