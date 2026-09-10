"""Structured-extraction primitive: candidate -> selection -> evidence (#769, #70).

AGENTS.md ("libs/factors/shared"): "the shared structured-extraction primitive. Do not
reimplement extraction per factor." Before #769 this file was a stub (`extract_metric`
raised `NotImplementedError`) while the real, production rule — one company-wide filing
statement is unambiguous, more than one needs a judgement — was built and lived entirely
inside `apps/data-engine/src/data_engine/datahub/standards/filing_extraction.py`
(`select_total`, `RULE_SINGLE_CANDIDATE = "rule:single-candidate:v1"

#: The set-valued rule (#772): recall found parts that ADD UP to a total measured
#: elsewhere, so the set is the answer. Same persistence contract as the id above —
#: it lands in `evidence_ref` and is compared verbatim.
RULE_EXHAUSTIVE_PARTITION = "rule:exhaustive-partition:v1"`). #70's headcount
slice became the primitive's first REAL use beside the primitive, not as it — exactly the
drift AGENTS.md warns about. This module is now that primitive; `filing_extraction.py`
is its SEC-filing adapter (recall regex -> `Candidate` -> `select_single_candidate`), and
`libs/factors/tests/test_extraction_ownership.py` fails CI the next time a module outside
`libs/factors/shared/` reimplements this rule instead of calling it.

Recall (finding every stated value with its evidentiary sentence) is always domain-
specific — an adapter's own regex, XBRL tag, or table walk — and stays out of this module.
Selection is not: given the candidates recall found, "exactly one distinct value" is the
same deterministic judgement no matter which factor or filing type produced them, so it
lives here once as `select_single_candidate`. When recall leaves more than one distinct
value, the choice needs a judgement this module refuses to fake — that is the `Selector`
protocol's job, implemented by a model behind the source gateway
(`data_engine.sources.llm`). The dependency direction is data_engine -> libs, never the
reverse (init.md, `libs/factors` has no such dependency in its `pyproject.toml`), so this
module depends only on the shapes a selector must satisfy, never on a concrete one.

Extractor identity strings are persisted evidence (`staging.issuer_headcount_facts
.evidence_ref`, compared in tests) and MUST NOT change shape: `rule:single-candidate:v1`
for this module's own rule, `model:<served-model>:<prompt sha256[:12]>` for a model
selection (minted by the selector itself, e.g. `data_engine.sources.llm.ModelSelection
.extractor` — this module never invents a model's identity).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

#: The deterministic rule this module owns: recall found exactly one distinct value, so
#: there is nothing to choose. Persisted in `evidence_ref` and compared in tests verbatim
#: — never rename or reformat without a migration for every row that already carries it.
RULE_SINGLE_CANDIDATE = "rule:single-candidate:v1"

#: The set-valued rule (#772): recall found parts that ADD UP to a total measured
#: elsewhere, so the set is the answer. Same persistence contract as the id above —
#: it lands in `evidence_ref` and is compared verbatim.
RULE_EXHAUSTIVE_PARTITION = "rule:exhaustive-partition:v1"


@dataclass(frozen=True)
class Candidate:
    """One value an adapter's recall pass found, with the evidence it was found in.

    `value` is left as `int | float` rather than `Decimal` because selection only ever
    compares candidates for equality — an adapter that needs monetary precision converts
    on its own side of the boundary (init.md: never use binary floating point where
    monetary precision matters; that constraint is on the LANDED fact, not this
    intermediate candidate). `as_of` is the source's own as-of text (a filing's stated
    date, a table's period label, ...), opaque to this module — parsing it into a `date`
    is the adapter's job, same as recall itself.
    """

    value: int | float
    sentence: str
    as_of: str | None = None


@dataclass(frozen=True)
class Selection:
    """Which candidate was chosen, by which extractor, and why.

    `candidate_index` indexes the SAME candidate sequence the selector or rule was given
    — the adapter maps it back to its own richer candidate type (e.g. `FilingCandidate`)
    to read whatever domain fields `Candidate` does not carry. `invocation_id` is `None`
    for the deterministic rule and set for a model selection (the append-only invocation
    record's id — init.md Section 9); its presence alone tells you which kind of
    selection happened, without parsing `extractor`.
    """

    value: int | float
    candidate_index: int
    extractor: str
    reason: str
    invocation_id: str | None = None


@dataclass(frozen=True)
class Extraction:
    """The full evidence package: what recall found, and what selection concluded (or
    left open). An adapter with its own richer, domain-specific evidence — a filing's
    accession, form, and raw-object pointer — builds its own outcome type around this
    (see `data_engine.datahub.standards.filing_extraction.ExtractionOutcome`) rather than
    replacing it; `Extraction` is the minimum every adapter's outcome is built from, not
    a replacement for a landed fact.
    """

    candidates: tuple[Candidate, ...]
    selection: Selection | None


@runtime_checkable
class Selector(Protocol):
    """A judgement-based chooser among candidates the rule left ambiguous.

    Implemented outside this library — typically a model behind the source gateway
    (`data_engine.sources.llm`, gated by `init.md` rule 6 and its own ledger/replay
    behaviour) — so this module can require the shape without importing a concrete
    implementation. A selector may decline (return `None`); it may never invent a value
    that is not among `candidates` (init.md §9: the fact stays anchored to a verbatim
    span).
    """

    def __call__(self, candidates: Sequence[Candidate]) -> Selection | None: ...


def select_single_candidate(candidates: Sequence[Candidate]) -> Selection | None:
    """The deterministic rule: exactly one DISTINCT value among the candidates needs no
    judgement. Zero candidates, or two or more distinct values, return `None` — there is
    nothing to select, or a `Selector` must be asked; this function never guesses.

    The chosen `candidate_index` is the first candidate in the input sequence carrying
    that value (stable, order-preserving — an adapter that de-duplicates before calling
    this keeps whichever candidate it put first, e.g. by discovery order in the source
    text).
    """
    if not candidates:
        return None
    distinct = {candidate.value for candidate in candidates}
    if len(distinct) != 1:
        return None
    return Selection(
        value=candidates[0].value,
        candidate_index=0,
        extractor=RULE_SINGLE_CANDIDATE,
        reason="every candidate states the same value; nothing to choose",
    )


class PartitionRefusal(StrEnum):
    """Why a candidate set is NOT the answer. Every value is a different problem with a
    different fix, which is the reason this is an enum rather than a bool.

    `SHORT` and `OVER` matter most for a revenue-share ranking: a missed part silently
    RAISES every remaining part's share, and a double-counted one lowers them. Either way
    the ranking inverts while every number still looks like a number — so neither may be
    reported as a share computed over whatever was found.
    """

    NO_CANDIDATES = "no_candidates"
    NO_TOTAL = "no_total"
    SHORT = "sums_short_of_total"
    OVER = "sums_over_total"


@dataclass(frozen=True)
class Partition:
    """A set-valued selection: N candidates that TOGETHER are the value (#772).

    `Selection` answers "which ONE of these is the value" and is right for a metric stated
    once in several places (a headcount). A metric that is DECOMPOSED — segment revenue,
    where each part is its own row and the parts are the answer — needs the other shape:
    the same candidate sequence, several indices, and an accounting identity that says the
    set is complete.

    `residual` is kept rather than discarded because it is the honest size of the doubt: a
    partition accepted within tolerance still missed or over-counted `residual`, and a
    consumer computing shares should be able to see it.
    """

    candidate_indices: tuple[int, ...]
    total: Decimal
    parts_sum: Decimal
    residual: Decimal
    extractor: str
    reason: str
    invocation_id: str | None = None


@runtime_checkable
class Partitioner(Protocol):
    """A judgement-based proposer of a partition, for the cases the rule cannot settle.

    Same contract as `Selector`: implemented outside this library, may decline, and may
    never invent a part that is not among `candidates`. It proposes; the accounting
    identity still decides — a proposal whose parts do not sum to the total is refused
    exactly as a rule-built one would be.
    """

    def __call__(self, candidates: Sequence[Candidate], *, total: Decimal) -> tuple[int, ...] | None: ...


def select_exhaustive_partition(
    candidates: Sequence[Candidate],
    *,
    total: Decimal | None,
    tolerance: Decimal,
    indices: Sequence[int] | None = None,
) -> Partition | PartitionRefusal:
    """Accept a candidate set only when its parts sum to a total measured ELSEWHERE.

    The check that makes a decomposition safe is not "did recall find plausible parts" but
    "do the parts account for the whole". For segment revenue the whole is already on the
    wide row — the issuer's consolidated `revenue`, SEC-sourced, computed by a different
    path — so the partition has an independent oracle without a new source.

    `indices` selects a subset (what a `Partitioner` proposed); omitted, every candidate is
    a part. Refusals are typed rather than `None`: "recall found nothing", "there is no
    total to check against", "the parts miss some of the whole" and "the parts exceed it"
    are four different problems, and a caller that collapses them into one cannot say which
    happened. Never raises and never guesses — a refused set is a refused set, not a share
    computed over whatever was found.

    `tolerance` is absolute and in the total's units; the caller owns it, because what
    counts as a rounding difference is a property of the source's own reporting precision,
    not of this rule.
    """
    if not candidates:
        return PartitionRefusal.NO_CANDIDATES
    if total is None:
        return PartitionRefusal.NO_TOTAL

    chosen = tuple(indices) if indices is not None else tuple(range(len(candidates)))
    if not chosen:
        return PartitionRefusal.NO_CANDIDATES
    # `Candidate.value` is int|float by contract (see its docstring); the sum is taken in
    # Decimal because the ACCEPTANCE is monetary, and str() is what keeps a float's binary
    # representation error out of it.
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
