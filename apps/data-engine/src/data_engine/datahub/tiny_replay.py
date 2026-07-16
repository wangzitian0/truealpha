"""Deterministic Local/CI replay for the frozen D5 tiny corpus."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from truealpha_contracts import SubjectKind, SubjectRef, UniverseRef, canonical_sha256
from truealpha_contracts.capture_control import (
    CaptureCheckpoint,
    CaptureListObligation,
    CaptureListVersion,
    CaptureRecapturePlan,
    CheckpointPhase,
)
from truealpha_contracts.datahub import FetchAttemptOutcome, RecapturePredicate, RetryPolicy

from data_engine.datahub.control_plane import AttemptLedger, expand_obligations

_RUN_ID = "capture-run:89218d2ccfd82036527934f2fbcdb03776b9e6ce36d3dfb9e10b2b11338867ae"
_AT = datetime(2026, 4, 1, tzinfo=UTC)
_TERMINAL_STATES = ("failed", "skipped_by_policy", "success", "unavailable", "unchanged")
_MUTABLE_TOKENS = {"*", "current", "default", "head", "latest", "main", "stable", "tip"}


@dataclass(frozen=True)
class ResumeResult:
    scenario_id: str
    expected_resume: str
    checkpoint_id: str
    append_count: int
    replay_append_count: int


@dataclass(frozen=True)
class TinyReplayReport:
    corpus_id: str
    list_count: int
    obligation_count: int
    shared_obligation_count: int
    shared_provider_work_item_count: int
    attempt_counts: tuple[tuple[str, int], ...]
    raw_object_count: int
    observation_event_count: int
    terminal_states: tuple[str, ...]
    resume_results: tuple[ResumeResult, ...]
    recapture_plan_id: str
    recapture_selection: tuple[str, ...]
    source_calls: int
    report_sha256: str = ""

    def as_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "corpus_id": self.corpus_id,
            "list_count": self.list_count,
            "obligation_count": self.obligation_count,
            "shared_obligation_count": self.shared_obligation_count,
            "shared_provider_work_item_count": self.shared_provider_work_item_count,
            "attempt_counts": [
                {"scenario_id": scenario_id, "attempt_count": count} for scenario_id, count in self.attempt_counts
            ],
            "raw_object_count": self.raw_object_count,
            "observation_event_count": self.observation_event_count,
            "terminal_states": list(self.terminal_states),
            "resume_results": [
                {
                    "scenario_id": result.scenario_id,
                    "expected_resume": result.expected_resume,
                    "checkpoint_id": result.checkpoint_id,
                    "append_count": result.append_count,
                    "replay_append_count": result.replay_append_count,
                }
                for result in self.resume_results
            ],
            "recapture_plan_id": self.recapture_plan_id,
            "recapture_selection": list(self.recapture_selection),
            "source_calls": self.source_calls,
        }
        if include_hash:
            result["report_sha256"] = self.report_sha256
        return result


def _retry_policy(max_attempts: int) -> RetryPolicy:
    return RetryPolicy(
        max_attempts=max_attempts,
        retryable_outcomes=(
            FetchAttemptOutcome.INTERRUPTED,
            FetchAttemptOutcome.RATE_LIMITED,
            FetchAttemptOutcome.SERVER_ERROR,
            FetchAttemptOutcome.TRANSPORT_ERROR,
        ),
        terminal_outcomes=(
            FetchAttemptOutcome.FAILED,
            FetchAttemptOutcome.SUCCESS,
            FetchAttemptOutcome.UNAVAILABLE,
            FetchAttemptOutcome.UNCHANGED,
        ),
    )


def _universe(corpus: Mapping[str, Any]) -> UniverseRef:
    denominator = corpus["topt_denominator"]
    return UniverseRef(
        universe_id=denominator["universe_id"],
        universe_version="topt-candidate-2026-03-31-v1",
        content_sha256="8b2f885e6161c01603b9d78882d411c7984ff6a3dbf35d636cb11e8c2ecfcf8f",
    )


def _lists(corpus: Mapping[str, Any]) -> tuple[CaptureListVersion, ...]:
    universe = _universe(corpus)
    versions = tuple(
        CaptureListVersion(
            universe=universe,
            members=tuple(SubjectRef(kind=SubjectKind.SECURITY, id=value) for value in row["members"]),
            effective_at=_AT,
        )
        for row in corpus["tiny_lists"]
    )
    expected_ids = tuple(row["list_version_id"] for row in corpus["tiny_lists"])
    if tuple(version.list_version_id for version in versions) != expected_ids:
        raise ValueError("frozen tiny list identity drift")
    return versions


def _obligations(corpus: Mapping[str, Any]) -> tuple[CaptureListObligation, ...]:
    return tuple(
        obligation
        for version in _lists(corpus)
        for obligation in expand_obligations(
            run_id=_RUN_ID,
            list_version=version,
            semantic_types=("market-price",),
            partition=corpus["topt_denominator"]["report_date"],
        )
    )


def _run_attempt_scenario(row: Mapping[str, Any], maximum_attempts: int) -> int:
    scenario_id = str(row["scenario_id"])
    outcomes = row.get("outcomes")
    if not isinstance(outcomes, list):
        return 0
    ledger = AttemptLedger(
        work_item_id=f"capture-work-item:{canonical_sha256({'scenario_id': scenario_id})}",
        retry_policy=_retry_policy(maximum_attempts),
    )
    for offset, raw_outcome in enumerate(outcomes):
        attempt = ledger.start(started_at=_AT + timedelta(seconds=offset * 2))
        outcome = FetchAttemptOutcome(raw_outcome)
        source_vintage_id = None
        reused_source_vintage_id = None
        if outcome is FetchAttemptOutcome.SUCCESS:
            source_vintage_id = f"source-vintage:{canonical_sha256({'scenario_id': scenario_id})}"
        elif outcome is FetchAttemptOutcome.UNCHANGED:
            reused_source_vintage_id = f"source-vintage:{canonical_sha256({'scenario_id': scenario_id})}"
        ledger.finish(
            attempt=attempt,
            completed_at=_AT + timedelta(seconds=offset * 2 + 1),
            outcome=outcome,
            source_vintage_id=source_vintage_id,
            reused_source_vintage_id=reused_source_vintage_id,
        )
    if not ledger.is_terminal:
        raise ValueError(f"attempt scenario {scenario_id} did not reach a terminal outcome")
    expected_count = row.get("expected_attempt_count")
    if expected_count is not None and len(ledger.attempts) != expected_count:
        raise ValueError(f"attempt count drift for {scenario_id}")
    return len(ledger.attempts)


def replay_attempt_scenarios(corpus: Mapping[str, Any]) -> tuple[tuple[str, int], ...]:
    maximum_attempts = int(corpus["identity_policy"]["maximum_attempts"])
    results: list[tuple[str, int]] = []
    for row in corpus["attempt_scenarios"]:
        scenario_id = str(row["scenario_id"])
        if "expected_error" in row or "capture_cycles" in row:
            continue
        results.append((scenario_id, _run_attempt_scenario(row, maximum_attempts)))
    return tuple(sorted(results))


def reject_out_of_order_attempt(corpus: Mapping[str, Any]) -> None:
    row = next(item for item in corpus["attempt_scenarios"] if "expected_error" in item)
    ledger = AttemptLedger(
        work_item_id=f"capture-work-item:{canonical_sha256({'scenario_id': row['scenario_id']})}",
        retry_policy=_retry_policy(int(corpus["identity_policy"]["maximum_attempts"])),
    )
    first = ledger.start(started_at=_AT)
    ledger.finish(
        attempt=first,
        completed_at=_AT,
        outcome=FetchAttemptOutcome.SUCCESS,
        source_vintage_id=f"source-vintage:{'1' * 64}",
    )
    ledger.start(started_at=_AT)


def _resume_phase(checkpoint: str) -> CheckpointPhase:
    return {
        "plan-persisted": CheckpointPhase.PLANNED,
        "raw-object-persisted": CheckpointPhase.RAW_LANDED,
        "observation-persisted": CheckpointPhase.NORMALIZED,
        "one-of-two-obligations-terminal": CheckpointPhase.MANIFEST_PERSISTED,
    }[checkpoint]


def replay_resume_scenarios(corpus: Mapping[str, Any]) -> tuple[ResumeResult, ...]:
    obligations = _obligations(corpus)
    results: list[ResumeResult] = []
    for sequence, row in enumerate(corpus["resume_scenarios"], start=1):
        completed = (obligations[0].obligation_id,) if row["checkpoint"] == "one-of-two-obligations-terminal" else ()
        checkpoint = CaptureCheckpoint(
            run_id=_RUN_ID,
            sequence=sequence,
            phase=_resume_phase(str(row["checkpoint"])),
            completed_obligation_ids=completed,
            recorded_at=_AT,
        )
        # Replaying the same immutable checkpoint yields no additional append.
        replay = CaptureCheckpoint.model_validate(checkpoint.model_dump(mode="json"))
        if replay != checkpoint:
            raise ValueError("checkpoint replay identity drift")
        complete_artifacts = {"attempt", "manifest", "observation", "plan", "raw"}
        existing_artifacts = {
            "plan-persisted": {"plan"},
            "raw-object-persisted": {"attempt", "plan", "raw"},
            "observation-persisted": {"attempt", "observation", "plan", "raw"},
            "one-of-two-obligations-terminal": {"attempt", "observation", "plan", "raw"},
        }[str(row["checkpoint"])]
        resumed_artifacts = set(existing_artifacts)
        before_resume = len(resumed_artifacts)
        resumed_artifacts.update(complete_artifacts)
        append_count = len(resumed_artifacts) - before_resume
        before_replay = len(resumed_artifacts)
        resumed_artifacts.update(complete_artifacts)
        results.append(
            ResumeResult(
                scenario_id=str(row["scenario_id"]),
                expected_resume=str(row["expected_resume"]),
                checkpoint_id=checkpoint.checkpoint_id,
                append_count=append_count,
                replay_append_count=len(resumed_artifacts) - before_replay,
            )
        )
    return tuple(results)


def _replay_identical_bytes(corpus: Mapping[str, Any]) -> tuple[int, int]:
    scenario = next(
        row for row in corpus["attempt_scenarios"] if row["scenario_id"] == "identical-bytes-distinct-capture-cycles"
    )
    raw_objects: set[str] = set()
    observations: set[str] = set()
    attempts = 0
    source_vintage_id = f"source-vintage:{scenario['raw_content_sha256']}"
    for cycle in scenario["capture_cycles"]:
        ledger = AttemptLedger(work_item_id=cycle["work_item_id"], retry_policy=_retry_policy(1))
        attempt = ledger.start(started_at=_AT)
        outcome = FetchAttemptOutcome(cycle["outcomes"][0])
        ledger.finish(
            attempt=attempt,
            completed_at=_AT,
            outcome=outcome,
            source_vintage_id=source_vintage_id if outcome is FetchAttemptOutcome.SUCCESS else None,
            reused_source_vintage_id=source_vintage_id if outcome is FetchAttemptOutcome.UNCHANGED else None,
        )
        attempts += len(ledger.attempts)
        raw_objects.add(str(scenario["raw_content_sha256"]))
        observations.add(canonical_sha256({"cycle_id": cycle["cycle_id"], "raw": scenario["raw_content_sha256"]}))
    if attempts != scenario["expected_attempt_count"]:
        raise ValueError("identical-byte attempt evidence drift")
    if len(raw_objects) != scenario["expected_raw_object_count"]:
        raise ValueError("identical bytes did not reuse one raw object")
    if len(observations) != scenario["expected_observation_event_count"]:
        raise ValueError("a later unchanged observation event was erased")
    return len(raw_objects), len(observations)


def _terminal_state_coverage(corpus: Mapping[str, Any]) -> tuple[str, ...]:
    states = {"skipped_by_policy"}
    for outcome in (
        FetchAttemptOutcome.FAILED,
        FetchAttemptOutcome.SUCCESS,
        FetchAttemptOutcome.UNAVAILABLE,
        FetchAttemptOutcome.UNCHANGED,
    ):
        scenario = {"scenario_id": f"terminal-{outcome.value}", "outcomes": [outcome.value]}
        _run_attempt_scenario(scenario, int(corpus["identity_policy"]["maximum_attempts"]))
        states.add(outcome.value)
    result = tuple(sorted(states))
    if result != _TERMINAL_STATES:
        raise ValueError("terminal-state coverage drift")
    return result


def _validate_recapture_predicates(predicates: Mapping[str, Any]) -> None:
    if not predicates:
        raise ValueError("unbounded_or_mutable_recapture_predicate")
    for value in predicates.values():
        values: Sequence[Any] = value if isinstance(value, (list, tuple)) else (value,)
        if any(isinstance(item, str) and item.lower() in _MUTABLE_TOKENS for item in values):
            raise ValueError("unbounded_or_mutable_recapture_predicate")


def select_recapture(corpus: Mapping[str, Any], predicates: Mapping[str, Any]) -> tuple[CaptureListObligation, ...]:
    _validate_recapture_predicates(predicates)
    metadata = corpus["identity_policy"]
    selected: list[CaptureListObligation] = []
    for obligation in _obligations(corpus):
        candidate = {
            "list_version_id": obligation.list_version_id,
            "instrument_id": obligation.subject.id,
            "source_version_id": metadata["source_version_id"],
            "semantic_type": obligation.capture_requirement_id.removesuffix(":v1"),
            "partition": obligation.partition,
            "outcome": "failed" if obligation.subject.id == "security:cusip:67066G104" else "success",
            "freshness_state": "stale" if obligation.subject.id == "security:cusip:67066G104" else "fresh",
            "parser_version_id": metadata["parser_version_id"],
            "mapping_version_id": metadata["mapping_version_id"],
            "confidence_state": "unavailable" if obligation.subject.id == "security:cusip:67066G104" else "available",
        }
        if all(candidate.get(key) == value for key, value in predicates.items()):
            selected.append(obligation)
    if not selected:
        raise ValueError("empty_recapture_selection")
    return tuple(sorted(selected, key=lambda item: item.obligation_id))


def build_recapture_plan(corpus: Mapping[str, Any]) -> CaptureRecapturePlan:
    scenario = corpus["recapture_scenarios"][0]
    selected = select_recapture(corpus, scenario["predicates"])
    expected = tuple(item["obligation_id"] for item in scenario["expected_selected_obligations"])
    selected_ids = tuple(item.obligation_id for item in selected)
    if selected_ids != expected:
        raise ValueError("recapture selection drift")
    return CaptureRecapturePlan(
        selection_cutoff=datetime.fromisoformat(str(scenario["selection_cutoff"]).replace("Z", "+00:00")),
        predicate=RecapturePredicate(subject_ids=(scenario["predicates"]["instrument_id"],)),
        selected_obligation_ids=selected_ids,
        planner_version="d5-tiny-replay:v1",
    )


def execute_recapture(plan: CaptureRecapturePlan, selected_obligation_ids: Sequence[str]) -> tuple[str, ...]:
    selection = tuple(sorted(selected_obligation_ids))
    if selection != plan.selected_obligation_ids:
        raise ValueError("recapture execution differs from frozen dry run")
    return selection


def run_tiny_replay(corpus: Mapping[str, Any]) -> TinyReplayReport:
    obligations = _obligations(corpus)
    overlap = corpus["tiny_lists"][1]["shared_work_item"]
    shared = tuple(item for item in obligations if item.subject.id == overlap["instrument_id"])
    if len(shared) != overlap["expected_obligation_count"]:
        raise ValueError("overlapping list obligations collapsed")
    provider_keys = {(item.subject.id, item.capture_requirement_id, item.partition) for item in shared}
    if len(provider_keys) != overlap["expected_provider_work_item_count"]:
        raise ValueError("compatible provider work was not shared")

    raw_object_count, observation_event_count = _replay_identical_bytes(corpus)
    plan = build_recapture_plan(corpus)
    selection = execute_recapture(plan, plan.selected_obligation_ids)
    report = TinyReplayReport(
        corpus_id=str(corpus["corpus_id"]),
        list_count=len(corpus["tiny_lists"]),
        obligation_count=len(obligations),
        shared_obligation_count=len(shared),
        shared_provider_work_item_count=len(provider_keys),
        attempt_counts=replay_attempt_scenarios(corpus),
        raw_object_count=raw_object_count,
        observation_event_count=observation_event_count,
        terminal_states=_terminal_state_coverage(corpus),
        resume_results=replay_resume_scenarios(corpus),
        recapture_plan_id=plan.plan_id,
        recapture_selection=selection,
        source_calls=0,
    )
    report_hash = canonical_sha256(report.as_dict(include_hash=False))
    return TinyReplayReport(**{**report.__dict__, "report_sha256": report_hash})
