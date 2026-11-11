"""Structured-extraction primitive: candidate -> selection -> evidence (#769, #772)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

RULE_SINGLE_CANDIDATE = "rule:single-candidate:v1"
RULE_EXHAUSTIVE_PARTITION = "rule:exhaustive-partition:v1"
RULE_SINGLE_SEGMENT = "rule:single-segment:v4"
RULE_NO_DESCRIPTION = "rule:no-description:v1"


@dataclass(frozen=True)
class Candidate:
    value: int | float
    sentence: str
    as_of: str | None = None


@dataclass(frozen=True)
class Selection:
    value: int | float
    candidate_index: int
    extractor: str
    reason: str
    invocation_id: str | None = None


@dataclass(frozen=True)
class Extraction:
    candidates: tuple[Candidate, ...]
    selection: Selection | None


@runtime_checkable
class Selector(Protocol):
    def __call__(self, candidates: Sequence[Candidate]) -> Selection | None: ...


def select_single_candidate(candidates: Sequence[Candidate]) -> Selection | None:
    if not candidates or len({c.value for c in candidates}) != 1:
        return None
    return Selection(
        value=candidates[0].value,
        candidate_index=0,
        extractor=RULE_SINGLE_CANDIDATE,
        reason="every candidate states the same value; nothing to choose",
    )


def extract_metric(candidates: Sequence[Candidate], selector: Selector | None = None) -> Extraction:
    if not candidates:
        return Extraction(candidates=(), selection=None)
    items = tuple(candidates)
    if (rule_selection := select_single_candidate(items)) is not None:
        return Extraction(candidates=items, selection=rule_selection)
    return Extraction(candidates=items, selection=selector(items) if selector is not None else None)


class PartitionRefusal(StrEnum):
    NO_CANDIDATES = "no_candidates"
    NO_TOTAL = "no_total"
    INDEX_OUT_OF_RANGE = "index_out_of_range"
    DUPLICATE_INDEX = "duplicate_index"
    SHORT = "sums_short_of_total"
    OVER = "sums_over_total"


@dataclass(frozen=True)
class Partition:
    candidate_indices: tuple[int, ...]
    total: Decimal
    parts_sum: Decimal
    residual: Decimal
    extractor: str
    reason: str
    invocation_id: str | None = None


@runtime_checkable
class Partitioner(Protocol):
    def __call__(self, candidates: Sequence[Candidate], *, total: Decimal) -> tuple[int, ...] | None: ...


def select_exhaustive_partition(
    candidates: Sequence[Candidate],
    *,
    total: Decimal | None,
    tolerance: Decimal,
    indices: Sequence[int] | None = None,
) -> Partition | PartitionRefusal:
    if not candidates:
        return PartitionRefusal.NO_CANDIDATES
    if total is None:
        return PartitionRefusal.NO_TOTAL
    chosen = tuple(indices) if indices is not None else tuple(range(len(candidates)))
    if not chosen:
        return PartitionRefusal.NO_CANDIDATES
    if len(set(chosen)) != len(chosen):
        return PartitionRefusal.DUPLICATE_INDEX
    if any(index < 0 or index >= len(candidates) for index in chosen):
        return PartitionRefusal.INDEX_OUT_OF_RANGE
    parts_sum = sum((Decimal(str(candidates[index].value)) for index in chosen), Decimal(0))
    residual = total - parts_sum
    if residual > tolerance:
        return PartitionRefusal.SHORT
    if -residual > tolerance:
        return PartitionRefusal.OVER
    return Partition(
        candidate_indices=chosen,
        total=total,
        parts_sum=parts_sum,
        residual=residual,
        extractor=RULE_EXHAUSTIVE_PARTITION,
        reason=f"{len(chosen)} parts sum to {parts_sum} against a measured {total} (residual {residual})",
    )


@dataclass(frozen=True)
class SetPartition:
    set_index: int
    completion_index: int | None
    partition: Partition


def select_first_balancing_set(
    candidate_sets: Sequence[Sequence[Candidate]],
    *,
    total: Decimal | None,
    tolerances: Sequence[Decimal],
    completions: Sequence[Sequence[Candidate]] | None = None,
) -> SetPartition | tuple[PartitionRefusal, ...]:
    if len(tolerances) != len(candidate_sets):
        raise ValueError(f"{len(tolerances)} tolerances for {len(candidate_sets)} candidate sets")
    if completions is not None and len(completions) != len(candidate_sets):
        raise ValueError(f"{len(completions)} completion lists for {len(candidate_sets)} candidate sets")
    if not candidate_sets:
        return (PartitionRefusal.NO_CANDIDATES,)
    refusals: list[PartitionRefusal] = []
    for index, candidates in enumerate(candidate_sets):
        verdict = select_exhaustive_partition(candidates, total=total, tolerance=tolerances[index])
        if isinstance(verdict, Partition):
            return SetPartition(index, None, verdict)
        refusals.append(verdict)
    if completions is not None:
        for index, candidates in enumerate(candidate_sets):
            if refusals[index] is not PartitionRefusal.SHORT:
                continue
            for comp_index, completion in enumerate(completions[index]):
                verdict = select_exhaustive_partition(
                    [*candidates, completion], total=total, tolerance=tolerances[index]
                )
                if isinstance(verdict, Partition):
                    return SetPartition(index, comp_index, verdict)
    return tuple(refusals)
