"""Deterministic Local/CI replay for the frozen D5 tiny corpus."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from truealpha_contracts import SubjectKind, SubjectRef, canonical_sha256
from truealpha_contracts.capture_control import (
    CaptureCheckpoint,
    CaptureListObligation,
    CaptureListVersion,
    CaptureObligationWorkBinding,
    CaptureRecapturePlan,
    CheckpointPhase,
)
from truealpha_contracts.datahub import (
    AssessmentFreshness,
    CaptureWorkItem,
    FetchAttemptOutcome,
    ObligationTerminalState,
    RecapturePredicate,
)

from data_engine.datahub.control_plane import (
    AttemptLedger,
    expand_obligations,
    frozen_topt_universe,
    replay_retry_policy,
)

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
class FrozenRecapturePlan:
    plan_id: str
    contract_plan: CaptureRecapturePlan
    predicates: tuple[tuple[str, str], ...]
    selected_obligation_ids: tuple[str, ...]


@dataclass(frozen=True)
class RecaptureCandidateState:
    obligation_id: str
    recorded_at: datetime
    coordinates: Mapping[str, str]


@dataclass(frozen=True)
class SharedProviderWork:
    work_item: CaptureWorkItem
    bindings: tuple[CaptureObligationWorkBinding, ...]


@dataclass
class _TinyReplayStore:
    planned_obligation_ids: tuple[str, ...]
    attempts: dict[str, str]
    attempt_results: dict[str, str]
    raw_objects: set[str]
    observations: dict[str, str]
    terminal_results: dict[str, str]
    manifest_obligation_ids: tuple[str, ...] | None = None

    @classmethod
    def for_obligations(cls, obligations: Sequence[CaptureListObligation]) -> _TinyReplayStore:
        return cls(
            planned_obligation_ids=tuple(sorted(item.obligation_id for item in obligations)),
            attempts={},
            attempt_results={},
            raw_objects=set(),
            observations={},
            terminal_results={},
        )

    @classmethod
    def from_persisted_records(
        cls,
        obligations: Sequence[CaptureListObligation],
        records: Mapping[str, Any],
    ) -> _TinyReplayStore:
        store = cls.for_obligations(obligations)
        by_ordinal = {index: obligation.obligation_id for index, obligation in enumerate(obligations)}

        def obligation_id(value: Any) -> str:
            if not isinstance(value, int) or value not in by_ordinal:
                raise ValueError("persisted replay record names an unknown obligation ordinal")
            return by_ordinal[value]

        for ordinal in records.get("attempt_obligation_ordinals", ()):
            store.put_attempt(obligation_id(ordinal))
        for ordinal in records.get("attempt_result_obligation_ordinals", ()):
            store.put_attempt_result(obligation_id(ordinal))
        for ordinal in records.get("raw_object_obligation_ordinals", ()):
            store.put_raw(obligation_id(ordinal))
        for ordinal in records.get("observation_obligation_ordinals", ()):
            store.put_observation(obligation_id(ordinal))
        for terminal in records.get("terminal_results", ()):
            if not isinstance(terminal, Mapping):
                raise ValueError("persisted terminal result must be a record")
            store.put_terminal(obligation_id(terminal.get("obligation_ordinal")), str(terminal.get("state")))
        if records.get("manifest_obligation_ordinals") is not None:
            manifest = tuple(sorted(obligation_id(value) for value in records["manifest_obligation_ordinals"]))
            if manifest != store.planned_obligation_ids:
                raise ValueError("persisted manifest cannot hide a pending obligation")
            store.manifest_obligation_ids = manifest
        return store

    @staticmethod
    def _put(mapping: dict[str, str], key: str, value: str) -> bool:
        existing = mapping.get(key)
        if existing is not None and existing != value:
            raise ValueError("append-only replay identity conflict")
        if existing is not None:
            return False
        mapping[key] = value
        return True

    def put_attempt(self, obligation_id: str, attempt_id: str | None = None) -> bool:
        return self._put(
            self.attempts,
            obligation_id,
            attempt_id or f"fetch-attempt:{canonical_sha256({'obligation_id': obligation_id})}",
        )

    def put_attempt_result(self, obligation_id: str, result_id: str | None = None) -> bool:
        if obligation_id not in self.attempts:
            raise ValueError("attempt result without an append-only attempt")
        return self._put(
            self.attempt_results,
            obligation_id,
            result_id or f"fetch-attempt-result:{canonical_sha256({'obligation_id': obligation_id})}",
        )

    def put_raw(self, obligation_id: str) -> bool:
        raw_id = f"raw-object:{canonical_sha256({'obligation_id': obligation_id})}"
        before = len(self.raw_objects)
        self.raw_objects.add(raw_id)
        return len(self.raw_objects) > before

    def put_observation(self, obligation_id: str) -> bool:
        if obligation_id not in self.attempts:
            raise ValueError("observation without an append-only attempt")
        return self._put(
            self.observations,
            obligation_id,
            f"normalized-observation:{canonical_sha256({'obligation_id': obligation_id})}",
        )

    def put_terminal(self, obligation_id: str, state: str) -> bool:
        if state != "skipped_by_policy" and obligation_id not in self.attempt_results:
            raise ValueError("non-skipped terminal result without an attempt result")
        return self._put(self.terminal_results, obligation_id, state)

    def put_policy_skip(self, obligation_id: str) -> bool:
        if obligation_id in self.attempts:
            raise ValueError("a policy skip cannot follow a source attempt")
        return self.put_terminal(obligation_id, "skipped_by_policy")

    def put_manifest(self) -> bool:
        completed = tuple(sorted(self.terminal_results))
        if completed != self.planned_obligation_ids:
            raise ValueError("manifest cannot hide a pending obligation")
        if self.manifest_obligation_ids is not None and self.manifest_obligation_ids != completed:
            raise ValueError("append-only manifest identity conflict")
        if self.manifest_obligation_ids is not None:
            return False
        self.manifest_obligation_ids = completed
        return True

    def validate_checkpoint(self, checkpoint: CaptureCheckpoint) -> None:
        if checkpoint.phase is CheckpointPhase.RAW_LANDED and (not self.attempts or not self.raw_objects):
            raise ValueError("raw_landed checkpoint is missing persisted attempt or raw object")
        if checkpoint.phase is CheckpointPhase.NORMALIZED and (
            not self.attempts or not self.raw_objects or not self.observations
        ):
            raise ValueError("normalized checkpoint is missing persisted attempt, raw object, or observation")
        if checkpoint.phase is CheckpointPhase.MANIFEST_PERSISTED and (
            not self.attempts
            or not self.attempt_results
            or not self.raw_objects
            or not self.observations
            or not self.terminal_results
        ):
            raise ValueError("manifest checkpoint is missing persisted capture artifacts")
        if set(checkpoint.completed_obligation_ids) != set(self.terminal_results):
            raise ValueError("checkpoint completion set differs from persisted terminal results")


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
    terminal_obligation_count: int
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
            "terminal_obligation_count": self.terminal_obligation_count,
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


def _lists(corpus: Mapping[str, Any]) -> tuple[CaptureListVersion, ...]:
    universe = frozen_topt_universe(corpus)
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
        retry_policy=replay_retry_policy(maximum_attempts),
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
        retry_policy=replay_retry_policy(int(corpus["identity_policy"]["maximum_attempts"])),
    )
    first = ledger.start(started_at=_AT)
    ledger.finish(
        attempt=first,
        completed_at=_AT,
        outcome=FetchAttemptOutcome.SUCCESS,
        source_vintage_id=f"source-vintage:{'1' * 64}",
    )
    ledger.start(started_at=_AT)


def replay_resume_scenarios(corpus: Mapping[str, Any]) -> tuple[ResumeResult, ...]:
    obligations = _obligations(corpus)[:2]
    results: list[ResumeResult] = []
    for sequence, row in enumerate(corpus["resume_scenarios"], start=1):
        persisted_checkpoint = row.get("persisted_checkpoint")
        persisted_records = row.get("persisted_records")
        if not isinstance(persisted_checkpoint, Mapping) or not isinstance(persisted_records, Mapping):
            raise ValueError("resume scenario requires persisted checkpoint and artifact records")
        completed = tuple(
            obligations[int(ordinal)].obligation_id
            for ordinal in persisted_checkpoint.get("completed_obligation_ordinals", ())
        )
        checkpoint = CaptureCheckpoint(
            run_id=_RUN_ID,
            sequence=sequence,
            phase=CheckpointPhase(str(persisted_checkpoint["phase"])),
            completed_obligation_ids=completed,
            recorded_at=_AT,
        )
        replay = CaptureCheckpoint.model_validate(checkpoint.model_dump(mode="json"))
        if replay != checkpoint:
            raise ValueError("checkpoint replay identity drift")
        store = _TinyReplayStore.from_persisted_records(obligations, persisted_records)
        first_id = obligations[0].obligation_id
        store.validate_checkpoint(checkpoint)

        append_count = _resume_capture(store, obligations)
        first_terminal = store.terminal_results[first_id]
        replay_append_count = _resume_capture(store, obligations)
        if store.terminal_results[first_id] != first_terminal:
            raise ValueError("resume rewrote an already terminal obligation")
        results.append(
            ResumeResult(
                scenario_id=str(row["scenario_id"]),
                expected_resume=str(row["expected_resume"]),
                checkpoint_id=checkpoint.checkpoint_id,
                append_count=append_count,
                replay_append_count=replay_append_count,
            )
        )
    return tuple(results)


def _resume_capture(store: _TinyReplayStore, obligations: Sequence[CaptureListObligation]) -> int:
    appends = 0
    for obligation in obligations:
        obligation_id = obligation.obligation_id
        if obligation_id in store.terminal_results:
            continue
        if obligation_id not in store.attempts:
            ledger = AttemptLedger(
                work_item_id=f"capture-work-item:{canonical_sha256({'obligation_id': obligation_id})}",
                retry_policy=replay_retry_policy(1),
            )
            attempt = ledger.start(started_at=_AT)
            result = ledger.finish(
                attempt=attempt,
                completed_at=_AT,
                outcome=FetchAttemptOutcome.SUCCESS,
                source_vintage_id=f"source-vintage:{canonical_sha256({'obligation_id': obligation_id})}",
            )
            appends += store.put_attempt(obligation_id, attempt.attempt_id)
            appends += store.put_attempt_result(obligation_id, result.attempt_result_id)
        else:
            appends += store.put_attempt_result(obligation_id)
        appends += store.put_raw(obligation_id)
        appends += store.put_observation(obligation_id)
        appends += store.put_terminal(obligation_id, "success")
    appends += store.put_manifest()
    return appends


def _replay_identical_bytes(corpus: Mapping[str, Any]) -> tuple[int, int]:
    scenario = next(
        row for row in corpus["attempt_scenarios"] if row["scenario_id"] == "identical-bytes-distinct-capture-cycles"
    )
    raw_objects: set[str] = set()
    observations: set[str] = set()
    attempts = 0
    source_vintage_id = f"source-vintage:{scenario['raw_content_sha256']}"
    for cycle in scenario["capture_cycles"]:
        ledger = AttemptLedger(work_item_id=cycle["work_item_id"], retry_policy=replay_retry_policy(1))
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


def _terminal_state_coverage(corpus: Mapping[str, Any]) -> tuple[int, tuple[str, ...]]:
    obligations = _obligations(corpus)
    states = tuple(corpus["tiny_lists"][0]["expected_terminal_states"])
    if len(obligations) != len(states):
        raise ValueError("the frozen terminal-state denominator differs from obligations")
    store = _TinyReplayStore.for_obligations(obligations)
    for obligation, state in zip(obligations, states, strict=True):
        obligation_id = obligation.obligation_id
        if state == "skipped_by_policy":
            store.put_policy_skip(obligation_id)
            continue
        outcome = FetchAttemptOutcome(str(state))
        ledger = AttemptLedger(
            work_item_id=f"capture-work-item:{canonical_sha256({'obligation_id': obligation_id})}",
            retry_policy=replay_retry_policy(1),
        )
        attempt = ledger.start(started_at=_AT)
        source_vintage_id = (
            f"source-vintage:{canonical_sha256({'obligation_id': obligation_id})}"
            if outcome is FetchAttemptOutcome.SUCCESS
            else None
        )
        reused_source_vintage_id = (
            f"source-vintage:{canonical_sha256({'obligation_id': obligation_id})}"
            if outcome is FetchAttemptOutcome.UNCHANGED
            else None
        )
        attempt_result = ledger.finish(
            attempt=attempt,
            completed_at=_AT,
            outcome=outcome,
            source_vintage_id=source_vintage_id,
            reused_source_vintage_id=reused_source_vintage_id,
        )
        store.put_attempt(obligation_id, attempt.attempt_id)
        store.put_attempt_result(obligation_id, attempt_result.attempt_result_id)
        store.put_terminal(obligation_id, str(state))
    store.put_manifest()
    result = tuple(sorted(store.terminal_results.values()))
    if result != _TERMINAL_STATES:
        raise ValueError("terminal-state coverage drift")
    if tuple(sorted(store.terminal_results)) != store.planned_obligation_ids:
        raise ValueError("a frozen obligation has no terminal result")
    return len(store.terminal_results), result


def _validate_recapture_predicates(predicates: Mapping[str, Any]) -> None:
    if not predicates:
        raise ValueError("unbounded_or_mutable_recapture_predicate")
    for value in predicates.values():
        values: Sequence[Any] = value if isinstance(value, (list, tuple)) else (value,)
        if any(isinstance(item, str) and item.lower() in _MUTABLE_TOKENS for item in values):
            raise ValueError("unbounded_or_mutable_recapture_predicate")


def _recapture_candidate_history(
    corpus: Mapping[str, Any],
    obligations: Sequence[CaptureListObligation],
) -> tuple[RecaptureCandidateState, ...]:
    metadata = corpus["identity_policy"]
    history: list[RecaptureCandidateState] = []
    for obligation in obligations:
        failed = obligation.subject.id == "security:cusip:67066G104"
        base = {
            "list_version_id": obligation.list_version_id,
            "instrument_id": obligation.subject.id,
            "source_version_id": str(metadata["source_version_id"]),
            "semantic_type": obligation.capture_requirement_id.removesuffix(":v1"),
            "partition": obligation.partition,
            "outcome": "failed" if failed else "success",
            "freshness_state": "stale" if failed else "fresh",
            "parser_version_id": str(metadata["parser_version_id"]),
            "mapping_version_id": str(metadata["mapping_version_id"]),
            "confidence_state": "unavailable" if failed else "available",
        }
        history.append(
            RecaptureCandidateState(
                obligation_id=obligation.obligation_id,
                recorded_at=_AT - timedelta(seconds=1),
                coordinates=base,
            )
        )
        if failed:
            history.append(
                RecaptureCandidateState(
                    obligation_id=obligation.obligation_id,
                    recorded_at=_AT + timedelta(seconds=1),
                    coordinates={**base, "outcome": "success", "freshness_state": "fresh"},
                )
            )
    return tuple(history)


def select_recapture(
    corpus: Mapping[str, Any],
    predicates: Mapping[str, Any],
    *,
    selection_cutoff: datetime | None = None,
) -> tuple[CaptureListObligation, ...]:
    _validate_recapture_predicates(predicates)
    cutoff = selection_cutoff or datetime.fromisoformat(
        str(corpus["recapture_scenarios"][0]["selection_cutoff"]).replace("Z", "+00:00")
    )
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("selection_cutoff must be timezone-aware")
    obligations = _obligations(corpus)
    by_id = {obligation.obligation_id: obligation for obligation in obligations}
    states: dict[str, RecaptureCandidateState] = {}
    for state in _recapture_candidate_history(corpus, obligations):
        if state.recorded_at <= cutoff and (
            state.obligation_id not in states or states[state.obligation_id].recorded_at < state.recorded_at
        ):
            states[state.obligation_id] = state
    selected: list[CaptureListObligation] = []
    for obligation_id, state in states.items():
        if all(state.coordinates.get(key) == value for key, value in predicates.items()):
            selected.append(by_id[obligation_id])
    if not selected:
        raise ValueError("empty_recapture_selection")
    return tuple(sorted(selected, key=lambda item: item.obligation_id))


def build_recapture_plan(corpus: Mapping[str, Any]) -> FrozenRecapturePlan:
    scenario = corpus["recapture_scenarios"][0]
    selection_cutoff = datetime.fromisoformat(str(scenario["selection_cutoff"]).replace("Z", "+00:00"))
    selected = select_recapture(corpus, scenario["predicates"], selection_cutoff=selection_cutoff)
    expected = tuple(item["obligation_id"] for item in scenario["expected_selected_obligations"])
    selected_ids = tuple(item.obligation_id for item in selected)
    if selected_ids != expected:
        raise ValueError("recapture selection drift")
    raw_predicates = {str(key): str(value) for key, value in scenario["predicates"].items()}
    contract_plan = CaptureRecapturePlan(
        selection_cutoff=selection_cutoff,
        predicate=RecapturePredicate(
            universe_refs=(frozen_topt_universe(corpus),),
            subject_ids=(raw_predicates["instrument_id"],),
            source_policy_ids=(raw_predicates["source_version_id"],),
            semantic_types=(raw_predicates["semantic_type"],),
            partitions=(raw_predicates["partition"],),
            terminal_states=(ObligationTerminalState(raw_predicates["outcome"]),),
            freshness_states=(AssessmentFreshness(raw_predicates["freshness_state"]),),
            parser_versions=(raw_predicates["parser_version_id"],),
            mapping_versions=(raw_predicates["mapping_version_id"],),
            assessment_policy_ids=(f"confidence-state:{raw_predicates['confidence_state']}",),
        ),
        selected_obligation_ids=selected_ids,
        planner_version="d5-tiny-replay:v1",
    )
    predicates = tuple(sorted(raw_predicates.items()))
    plan_content = {
        "contract_plan": contract_plan.model_dump(mode="json"),
        "predicates": dict(predicates),
        "selected_obligation_ids": selected_ids,
    }
    return FrozenRecapturePlan(
        plan_id=f"frozen-recapture-plan:{canonical_sha256(plan_content)}",
        contract_plan=contract_plan,
        predicates=predicates,
        selected_obligation_ids=selected_ids,
    )


def execute_recapture(plan: FrozenRecapturePlan, selected_obligation_ids: Sequence[str]) -> tuple[str, ...]:
    selection = tuple(sorted(selected_obligation_ids))
    if selection != plan.selected_obligation_ids:
        raise ValueError("recapture execution differs from frozen dry run")
    return selection


def materialize_shared_provider_work(
    corpus: Mapping[str, Any],
    obligations: Sequence[CaptureListObligation] | None = None,
) -> SharedProviderWork:
    if obligations is None:
        overlap_id = str(corpus["tiny_lists"][1]["shared_work_item"]["instrument_id"])
        obligations = tuple(item for item in _obligations(corpus) if item.subject.id == overlap_id)
    if not obligations:
        raise ValueError("shared provider work requires at least one obligation")
    coordinates = {(item.subject.id, item.capture_requirement_id, item.partition) for item in obligations}
    if len(coordinates) != 1:
        raise ValueError("shared provider work received incompatible obligations")
    provider_coordinate = next(iter(coordinates))
    work_item = CaptureWorkItem(
        campaign_id=str(corpus["identity_policy"]["campaign_id"]),
        source_request_id=f"source-request:{canonical_sha256({'provider_coordinate': provider_coordinate})}",
        schedule_policy_id=f"schedule-policy:{canonical_sha256({'policy': 'd5-tiny-replay:v1'})}",
    )
    bindings = tuple(
        CaptureObligationWorkBinding(obligation_id=item.obligation_id, work_item_id=work_item.work_item_id)
        for item in sorted(obligations, key=lambda value: value.obligation_id)
    )
    return SharedProviderWork(work_item=work_item, bindings=bindings)


def run_tiny_replay(corpus: Mapping[str, Any]) -> TinyReplayReport:
    obligations = _obligations(corpus)
    overlap = corpus["tiny_lists"][1]["shared_work_item"]
    shared = tuple(item for item in obligations if item.subject.id == overlap["instrument_id"])
    if len(shared) != overlap["expected_obligation_count"]:
        raise ValueError("overlapping list obligations collapsed")
    shared_work = materialize_shared_provider_work(corpus, shared)
    provider_work_item_ids = {binding.work_item_id for binding in shared_work.bindings}
    if len(provider_work_item_ids) != overlap["expected_provider_work_item_count"]:
        raise ValueError("compatible provider work was not shared")
    if {binding.obligation_id for binding in shared_work.bindings} != {item.obligation_id for item in shared}:
        raise ValueError("shared provider work lost an obligation binding")

    raw_object_count, observation_event_count = _replay_identical_bytes(corpus)
    terminal_obligation_count, terminal_states = _terminal_state_coverage(corpus)
    plan = build_recapture_plan(corpus)
    selection = execute_recapture(plan, plan.selected_obligation_ids)
    report = TinyReplayReport(
        corpus_id=str(corpus["corpus_id"]),
        list_count=len(corpus["tiny_lists"]),
        obligation_count=len(obligations),
        shared_obligation_count=len(shared),
        shared_provider_work_item_count=len(provider_work_item_ids),
        attempt_counts=replay_attempt_scenarios(corpus),
        raw_object_count=raw_object_count,
        observation_event_count=observation_event_count,
        terminal_obligation_count=terminal_obligation_count,
        terminal_states=terminal_states,
        resume_results=replay_resume_scenarios(corpus),
        recapture_plan_id=plan.plan_id,
        recapture_selection=selection,
        source_calls=0,
    )
    report_hash = canonical_sha256(report.as_dict(include_hash=False))
    return TinyReplayReport(**{**report.__dict__, "report_sha256": report_hash})
