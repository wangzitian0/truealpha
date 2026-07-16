"""Terminal D5 hardening replay over the frozen Local/CI corpus."""

from __future__ import annotations

import json
import time
import tracemalloc
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from truealpha_contracts import canonical_sha256
from truealpha_contracts.datahub import FetchAttemptOutcome

from data_engine.datahub.control_plane import AttemptLedger, replay_retry_policy
from data_engine.datahub.medium_replay import ToptMediumReplayReport, run_topt_medium_replay
from data_engine.datahub.tiny_replay import (
    build_recapture_plan,
    execute_recapture,
    reject_out_of_order_attempt,
    replay_attempt_scenarios,
    replay_resume_scenarios,
    run_tiny_replay,
)

_AT = datetime(2026, 4, 1, tzinfo=UTC)
_EXPECTED_NEGATIVE_CONTROLS = (
    "collision_rejected",
    "denominator_shrink_rejected",
    "out_of_order_attempt_rejected",
    "parser_failure_terminalized",
    "partial_write_rejected",
    "rate_limit_recovered_within_retry_budget",
    "recapture_overreach_rejected",
    "resume_is_idempotent",
)


@dataclass(frozen=True)
class HardeningResourceCeilings:
    wall_time_ms: int = 10_000
    cpu_time_ms: int = 10_000
    peak_traced_bytes: int = 128 * 1024 * 1024
    database_record_bytes: int = 2 * 1024 * 1024
    object_payload_bytes: int = 2 * 1024 * 1024
    peak_queue_depth: int = 84
    retry_amplification_ppm: int = 3_000_000
    provider_calls: int = 0
    source_cost_microunits: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class HardeningResourceObservation:
    wall_time_ms: int
    cpu_time_ms: int
    peak_traced_bytes: int
    database_record_bytes: int
    object_payload_bytes: int
    peak_queue_depth: int
    throughput_milli_obligations_per_second: int
    retry_amplification_ppm: int
    overfetch_count: int
    provider_calls: int
    source_cost_microunits: int

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class HardeningScopeMetric:
    scope_kind: str
    scope_id: str
    obligation_count: int
    terminal_obligation_count: int
    denominator_completeness_ppm: int
    freshness_age_seconds: int
    attempt_count: int
    retry_amplification_ppm: int
    overfetch_count: int
    provider_calls: int
    source_cost_microunits: int

    def as_dict(self) -> dict[str, int | str]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class ToptHardeningReplayReport:
    corpus_id: str
    medium_report_sha256: str
    tiny_report_sha256: str
    issuer_count: int
    instrument_count: int
    obligation_count_per_run: int
    total_terminal_obligation_count: int
    denominator_completeness_ppm: int
    recapture_plan_id: str
    recapture_selection_count: int
    resume_checkpoint_count: int
    negative_controls: tuple[str, ...]
    scope_metrics: tuple[HardeningScopeMetric, ...]
    resource_metric_semantics: tuple[str, ...]
    resource_observation: HardeningResourceObservation
    resource_ceilings: HardeningResourceCeilings
    source_calls: int
    identity_sha256: str = ""

    def identity_dict(self) -> dict[str, Any]:
        """Return the deterministic evidence identity, excluding runtime observations."""

        return {
            "corpus_id": self.corpus_id,
            "medium_report_sha256": self.medium_report_sha256,
            "tiny_report_sha256": self.tiny_report_sha256,
            "issuer_count": self.issuer_count,
            "instrument_count": self.instrument_count,
            "obligation_count_per_run": self.obligation_count_per_run,
            "total_terminal_obligation_count": self.total_terminal_obligation_count,
            "denominator_completeness_ppm": self.denominator_completeness_ppm,
            "recapture_plan_id": self.recapture_plan_id,
            "recapture_selection_count": self.recapture_selection_count,
            "resume_checkpoint_count": self.resume_checkpoint_count,
            "negative_controls": list(self.negative_controls),
            "scope_metrics": [metric.as_dict() for metric in self.scope_metrics],
            "resource_metric_semantics": list(self.resource_metric_semantics),
            "resource_ceilings": self.resource_ceilings.as_dict(),
            "source_calls": self.source_calls,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.identity_dict(),
            "resource_observation": self.resource_observation.as_dict(),
            "identity_sha256": self.identity_sha256,
        }


def _expect_value_error(action: Callable[[], object], *, message: str) -> None:
    try:
        action()
    except ValueError:
        return
    raise ValueError(message)


def _run_negative_controls(corpus: Mapping[str, Any]) -> tuple[str, ...]:
    denominator_shrink = deepcopy(corpus)
    denominator_shrink["topt_denominator"]["instruments"].pop()
    _expect_value_error(
        lambda: run_topt_medium_replay(denominator_shrink),
        message="denominator shrink negative control did not fail closed",
    )

    collision = deepcopy(corpus)
    collision["topt_denominator"]["instruments"][1][2] = collision["topt_denominator"]["instruments"][0][2]
    _expect_value_error(
        lambda: run_topt_medium_replay(collision),
        message="identity collision negative control did not fail closed",
    )
    _expect_value_error(
        lambda: reject_out_of_order_attempt(corpus),
        message="out-of-order attempt negative control did not fail closed",
    )

    partial_write = deepcopy(corpus)
    partial_write["resume_scenarios"][1]["persisted_records"]["raw_object_obligation_ordinals"] = []
    _expect_value_error(
        lambda: replay_resume_scenarios(partial_write),
        message="partial-write negative control did not fail closed",
    )

    plan = build_recapture_plan(corpus)
    _expect_value_error(
        lambda: execute_recapture(plan, ()),
        message="recapture overreach negative control did not fail closed",
    )

    attempts = dict(replay_attempt_scenarios(corpus))
    if attempts.get("rate-limit-then-success") != 2:
        raise ValueError("rate-limit recovery exceeded the frozen retry budget")
    if any(result.replay_append_count for result in replay_resume_scenarios(corpus)):
        raise ValueError("resume replay appended duplicate logical work")

    ledger = AttemptLedger(
        work_item_id=f"capture-work-item:{canonical_sha256({'negative_control': 'parser_failure'})}",
        retry_policy=replay_retry_policy(1),
    )
    attempt = ledger.start(started_at=_AT)
    result = ledger.finish(
        attempt=attempt,
        completed_at=_AT,
        outcome=FetchAttemptOutcome.FAILED,
        error_code="parser_failure",
    )
    if not ledger.is_terminal or result.outcome is not FetchAttemptOutcome.FAILED:
        raise ValueError("parser failure was not retained as an explicit terminal result")
    return _EXPECTED_NEGATIVE_CONTROLS


def _serialized_bytes(value: object) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _scope_metrics(medium: ToptMediumReplayReport) -> tuple[HardeningScopeMetric, ...]:
    campaign_metrics = tuple(
        HardeningScopeMetric(
            scope_kind="campaign",
            scope_id=summary.campaign_id,
            obligation_count=summary.obligation_count,
            terminal_obligation_count=summary.terminal_obligation_count,
            denominator_completeness_ppm=summary.terminal_obligation_count * 1_000_000 // summary.obligation_count,
            freshness_age_seconds=86_400 if summary.outcome == FetchAttemptOutcome.UNCHANGED.value else 0,
            attempt_count=summary.attempt_count,
            retry_amplification_ppm=summary.attempt_count * 1_000_000 // summary.work_item_count,
            overfetch_count=summary.attempt_count - summary.obligation_count,
            provider_calls=0,
            source_cost_microunits=0,
        )
        for summary in medium.run_summaries
    )
    list_metric = HardeningScopeMetric(
        scope_kind="list",
        scope_id=medium.list_version_id,
        obligation_count=medium.total_obligation_count,
        terminal_obligation_count=medium.total_terminal_obligation_count,
        denominator_completeness_ppm=(
            medium.total_terminal_obligation_count * 1_000_000 // medium.total_obligation_count
        ),
        freshness_age_seconds=max(metric.freshness_age_seconds for metric in campaign_metrics),
        attempt_count=medium.total_attempt_count,
        retry_amplification_ppm=medium.total_attempt_count * 1_000_000 // medium.total_work_item_count,
        overfetch_count=medium.total_attempt_count - medium.total_obligation_count,
        provider_calls=0,
        source_cost_microunits=0,
    )
    return (list_metric, *campaign_metrics)


def _resource_observation(
    *,
    corpus: Mapping[str, Any],
    medium: ToptMediumReplayReport,
    elapsed_ns: int,
    cpu_ns: int,
    peak_traced_bytes: int,
) -> HardeningResourceObservation:
    attempt_counts = dict(replay_attempt_scenarios(corpus))
    fault_work_items = len(attempt_counts)
    fault_attempts = sum(attempt_counts.values())
    wall_time_ms = max(1, (elapsed_ns + 999_999) // 1_000_000)
    return HardeningResourceObservation(
        wall_time_ms=wall_time_ms,
        cpu_time_ms=max(1, (cpu_ns + 999_999) // 1_000_000),
        peak_traced_bytes=peak_traced_bytes,
        database_record_bytes=_serialized_bytes(medium.as_dict()),
        object_payload_bytes=_serialized_bytes(corpus["topt_denominator"]) * 2,
        peak_queue_depth=medium.obligation_count_per_run,
        throughput_milli_obligations_per_second=(medium.total_terminal_obligation_count * 1_000_000 // wall_time_ms),
        retry_amplification_ppm=fault_attempts * 1_000_000 // fault_work_items,
        overfetch_count=medium.total_attempt_count - medium.total_obligation_count,
        provider_calls=0,
        source_cost_microunits=0,
    )


def _enforce_resource_ceilings(
    observation: HardeningResourceObservation,
    ceilings: HardeningResourceCeilings,
) -> None:
    for field in (
        "wall_time_ms",
        "cpu_time_ms",
        "peak_traced_bytes",
        "database_record_bytes",
        "object_payload_bytes",
        "peak_queue_depth",
        "retry_amplification_ppm",
        "provider_calls",
        "source_cost_microunits",
    ):
        if getattr(observation, field) > getattr(ceilings, field):
            raise ValueError(f"hardening resource ceiling exceeded: {field}")


def run_topt_hardening_replay(corpus: Mapping[str, Any]) -> ToptHardeningReplayReport:
    """Exercise terminal Local/CI controls without source or environment activation."""

    started_tracing = not tracemalloc.is_tracing()
    if started_tracing:
        tracemalloc.start()
    wall_started = time.perf_counter_ns()
    cpu_started = time.process_time_ns()
    try:
        medium = run_topt_medium_replay(corpus)
        tiny = run_tiny_replay(corpus)
        negative_controls = _run_negative_controls(corpus)
        plan = build_recapture_plan(corpus)
        resume_results = replay_resume_scenarios(corpus)
        cpu_ns = time.process_time_ns() - cpu_started
        elapsed_ns = time.perf_counter_ns() - wall_started
        _, peak_traced_bytes = tracemalloc.get_traced_memory()
    finally:
        if started_tracing:
            tracemalloc.stop()

    ceilings = HardeningResourceCeilings()
    observation = _resource_observation(
        corpus=corpus,
        medium=medium,
        elapsed_ns=elapsed_ns,
        cpu_ns=cpu_ns,
        peak_traced_bytes=peak_traced_bytes,
    )
    _enforce_resource_ceilings(observation, ceilings)
    if medium.source_calls or tiny.source_calls:
        raise ValueError("hardening replay attempted a source call")
    if medium.total_terminal_obligation_count != medium.total_obligation_count:
        raise ValueError("hardening replay did not terminalize the exact denominator")

    report = ToptHardeningReplayReport(
        corpus_id=medium.corpus_id,
        medium_report_sha256=medium.report_sha256,
        tiny_report_sha256=tiny.report_sha256,
        issuer_count=medium.issuer_count,
        instrument_count=medium.instrument_count,
        obligation_count_per_run=medium.obligation_count_per_run,
        total_terminal_obligation_count=medium.total_terminal_obligation_count,
        denominator_completeness_ppm=1_000_000,
        recapture_plan_id=plan.plan_id,
        recapture_selection_count=len(plan.selected_obligation_ids),
        resume_checkpoint_count=len(resume_results),
        negative_controls=negative_controls,
        scope_metrics=_scope_metrics(medium),
        resource_metric_semantics=(
            "database_record_bytes is canonical serialized Local/CI evidence, not physical database allocation",
            "object_payload_bytes is two frozen logical fixture waves, not live-source object-store allocation",
            "wall, CPU, traced memory, queue depth, throughput, retries, overfetch, calls, and cost are measured in-process",
        ),
        resource_observation=observation,
        resource_ceilings=ceilings,
        source_calls=0,
    )
    return ToptHardeningReplayReport(**{**report.__dict__, "identity_sha256": canonical_sha256(report.identity_dict())})
