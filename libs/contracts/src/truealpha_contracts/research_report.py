"""Canonical research-report model, deterministic assembler, and renderers — see #369.

This is a provisional slice of `#43` (the full research-report vision). It lands the
deterministic report builder that later card (`#372`/`#44`), document-persistence
(`#235`), and chat (`#46`) work all consume. It does **not** claim `#43`'s full
acceptance.

The contract here is strict about one thing: `build_research_report` *selects*
already-materialized sections and their trace links; it never computes a new metric,
ranking, or classification. Any joint computation across factors or time belongs in
`libs/factors` and must be materialized into `mart` (init.md Section 1, rule 2). The read
side (`ResearchReadPort`) supplies fully-formed, provenance-neutral sections whose values
were materialized upstream; the builder only filters by the request, orders subjects, and
binds a stable content hash.

`#41` (the stable seven-module mart read contract) is still open. Until it freezes, the
shipped `ResearchReadPort` implementation reads checked-in fixtures that reproduce the
already-materialized strategy-run values exactly (same pattern as `#347`'s
`FixtureStrategyRunRepository`). Swapping in a real mart-backed port later changes only the
port implementation, not this model, the builder, or the renderers.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from truealpha_contracts.access import AccessContext
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.execution import AvailabilityStatus, FactorValidationStatus
from truealpha_contracts.models import _require_aware

SCHEMA_VERSION: Literal["research_report.v1"] = "research_report.v1"
_REPORT_ID_PREFIX = "report:"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ResearchReportKind(StrEnum):
    """The three report shapes named in `#369`'s acceptance criteria."""

    COMPANY = "company"
    ETF = "etf"
    THEME_RANKING = "theme_ranking"


class ReportSectionKind(StrEnum):
    """One materialized research surface. Maps to the seven modules (init.md Section 7)
    plus the composite strategy summary and its ranking projection."""

    OPERATING_EFFICIENCY = "operating_efficiency"  # module 2 (gross profit / employee)
    VALUATION = "valuation"  # module 7 tier + price-to-sales
    PEG_CONVENTIONS = "peg_conventions"  # module 1
    SUPPLY_CHAIN = "supply_chain"  # module 3
    ANALYST_HISTORY = "analyst_history"  # module 4
    ETF_VIRTUAL_COMPANY = "etf_virtual_company"  # module 5
    PURE_BLOOD = "pure_blood"  # module 6
    STRATEGY_SUMMARY = "strategy_summary"  # large_model_value_v0 decision
    RANKING = "ranking"  # theme/ranking projection


class EvidenceTrace(_StrictFrozenModel):
    """A compact link back to the materialized output and its snapshot lineage.

    It carries identifiers only — never raw bytes (init.md Section 6: traceability
    resolves output -> snapshot -> normalized/raw references without returning raw
    payloads by default).
    """

    reference_id: str = Field(min_length=1)
    materialized_output_id: str | None = None
    snapshot_id: str | None = None
    factor_execution_id: str | None = None


class ResultValue(_StrictFrozenModel):
    """One already-materialized displayed result.

    Every displayed result retains confidence, availability, period/cutoff, factor
    version, and an evidence trace (`#369` acceptance). `value` is kept as an exact
    string token (or None) so the builder and renderers never coerce, round, or
    recompute a numeric — the byte-exact materialized value flows straight through.
    """

    label: str = Field(min_length=1)
    value: str | None = None
    unit: str | None = None
    currency: str | None = None
    period: str | None = None
    cutoff_at: datetime
    availability: AvailabilityStatus
    confidence: Decimal | None = Field(default=None, ge=0, le=1)
    factor_version: str | None = None
    trace: EvidenceTrace | None = None

    @model_validator(mode="after")
    def _validate(self) -> ResultValue:
        _require_aware(self.cutoff_at, "cutoff_at")
        return self


class ReportSection(_StrictFrozenModel):
    """One research surface for a subject, with a section-level availability rollup and
    a separate factor-validation status (init.md Section 8: consumers display source
    evidence and formula validation as distinct dimensions)."""

    section_kind: ReportSectionKind
    title: str = Field(min_length=1)
    availability: AvailabilityStatus
    validation_status: FactorValidationStatus | None = None
    results: tuple[ResultValue, ...] = ()
    notes: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()


class ReportSubject(_StrictFrozenModel):
    """One report subject (an issuer, an ETF, or a ranked theme member)."""

    subject_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    rank: int | None = Field(default=None, ge=1)
    sections: tuple[ReportSection, ...]


class ResearchReportRequest(_StrictFrozenModel):
    """A request for a deterministic research report.

    `cutoff_at` is the explicit point-in-time the report is anchored to; nothing is
    inferred from the wall clock. An empty `section_kinds` means "every section the read
    port can supply"; a non-empty tuple filters to exactly those sections.
    """

    report_kind: ResearchReportKind
    target_entity_ids: tuple[str, ...] = Field(min_length=1)
    cutoff_at: datetime
    section_kinds: tuple[ReportSectionKind, ...] = ()
    strategy_id: str | None = None
    title: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> ResearchReportRequest:
        _require_aware(self.cutoff_at, "cutoff_at")
        return self


class ResearchReport(_StrictFrozenModel):
    """The canonical, content-addressed research report.

    `report_id` is a pure function of the report's content: identical read models yield
    an identical report ID and hash. Build one through :meth:`assemble` (which fills the
    ID); constructing one directly requires a `report_id` that matches its content.
    """

    schema_version: Literal["research_report.v1"] = SCHEMA_VERSION
    report_id: str = ""
    report_kind: ResearchReportKind
    title: str = Field(min_length=1)
    cutoff_at: datetime
    generated_from: str = Field(min_length=1)
    subjects: tuple[ReportSubject, ...]

    @model_validator(mode="after")
    def _identify(self) -> ResearchReport:
        _require_aware(self.cutoff_at, "cutoff_at")
        computed = self._content_hash()
        if self.report_id == "":
            object.__setattr__(self, "report_id", computed)
        elif self.report_id != computed:
            raise ValueError("report_id does not match report content")
        return self

    def _content_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude={"report_id"})
        return f"{_REPORT_ID_PREFIX}{canonical_sha256(payload)}"

    @classmethod
    def assemble(
        cls,
        *,
        report_kind: ResearchReportKind,
        title: str,
        cutoff_at: datetime,
        generated_from: str,
        subjects: tuple[ReportSubject, ...],
    ) -> ResearchReport:
        """Constructs a report and derives its stable `report_id` from the content."""
        return cls(
            report_kind=report_kind,
            title=title,
            cutoff_at=cutoff_at,
            generated_from=generated_from,
            subjects=subjects,
        )


class ResearchReadPort(Protocol):
    """The read boundary the builder assembles over.

    Implementations return fully-formed, already-materialized subjects and sections.
    They own the mapping from materialized `mart` outputs (or, provisionally, checked-in
    fixtures) to report sections; the builder never computes across their values.
    """

    @property
    def provenance_label(self) -> str: ...

    def load_subjects(self, *, request: ResearchReportRequest, context: AccessContext) -> tuple[ReportSubject, ...]: ...


def _select_sections(
    sections: tuple[ReportSection, ...],
    section_kinds: tuple[ReportSectionKind, ...],
) -> tuple[ReportSection, ...]:
    """Filters to the requested section kinds, preserving read-port order. Selection
    only — no value ever changes."""
    if not section_kinds:
        return sections
    wanted = set(section_kinds)
    return tuple(section for section in sections if section.section_kind in wanted)


def _select_subject(
    subject: ReportSubject,
    section_kinds: tuple[ReportSectionKind, ...],
) -> ReportSubject:
    return ReportSubject(
        subject_id=subject.subject_id,
        display_name=subject.display_name,
        rank=subject.rank,
        sections=_select_sections(subject.sections, section_kinds),
    )


def _default_title(request: ResearchReportRequest) -> str:
    targets = ", ".join(request.target_entity_ids)
    return f"{request.report_kind.value} research report: {targets}"


def build_research_report(
    request: ResearchReportRequest,
    repository: ResearchReadPort,
    *,
    context: AccessContext,
) -> ResearchReport:
    """Deterministically assembles a :class:`ResearchReport` from already-materialized
    reads.

    Contract: this selects sections and trace links only. It performs no arithmetic and
    computes no new metric, ranking, or classification — a boundary test asserts the
    builder's source carries no arithmetic (see `tests/test_research_report.py`).
    """
    subjects = repository.load_subjects(request=request, context=context)
    selected = tuple(_select_subject(subject, request.section_kinds) for subject in subjects)
    title = request.title if request.title is not None else _default_title(request)
    return ResearchReport.assemble(
        report_kind=request.report_kind,
        title=title,
        cutoff_at=request.cutoff_at,
        generated_from=repository.provenance_label,
        subjects=selected,
    )


def render_report_json(report: ResearchReport) -> str:
    """Renders the canonical JSON form: sorted keys, stable separators, trailing newline.

    The JSON is the content the `report_id` hashes over (plus the id itself), so two runs
    that produce equal read models produce byte-identical JSON.
    """
    payload = report.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True) + "\n"


def _render_confidence(value: Decimal | None) -> str:
    return "—" if value is None else str(value)


def _render_optional(value: str | None) -> str:
    return "—" if value is None else value


def _render_trace(trace: EvidenceTrace | None) -> str:
    return "—" if trace is None else trace.reference_id


def _render_result_row(result: ResultValue) -> str:
    unit = result.currency if result.currency is not None else result.unit
    value = _render_optional(result.value)
    if unit is not None and result.value is not None:
        value = f"{value} {unit}"
    cells = [
        result.label,
        value,
        _render_optional(result.period),
        result.availability.value,
        _render_confidence(result.confidence),
        _render_optional(result.factor_version),
        _render_trace(result.trace),
    ]
    return "| " + " | ".join(cells) + " |"


def _render_section_markdown(section: ReportSection) -> list[str]:
    lines = [f"### {section.title}", ""]
    lines.append(f"- Availability: `{section.availability.value}`")
    if section.validation_status is not None:
        lines.append(f"- Factor validation: `{section.validation_status.value}`")
    if section.reason_codes:
        lines.append(f"- Reasons: {', '.join(section.reason_codes)}")
    lines.append("")
    if section.results:
        lines.append("| Result | Value | Period | Availability | Confidence | Factor version | Trace |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        lines.extend(_render_result_row(result) for result in section.results)
        lines.append("")
    for note in section.notes:
        lines.append(f"> {note}")
    if section.notes:
        lines.append("")
    return lines


def render_report_markdown(report: ResearchReport) -> str:
    """Renders a deterministic Markdown view.

    Every printed value comes from a model field; the renderer introduces no computed
    value absent from the model (`#369` acceptance).
    """
    lines = [
        f"# {report.title}",
        "",
        f"- Report ID: `{report.report_id}`",
        f"- Kind: `{report.report_kind.value}`",
        f"- Cutoff: `{report.cutoff_at.isoformat()}`",
        f"- Source: `{report.generated_from}`",
        f"- Schema: `{report.schema_version}`",
        "",
    ]
    for subject in report.subjects:
        heading = subject.display_name
        rank = f" (rank {subject.rank})" if subject.rank is not None else ""
        lines.append(f"## {heading}{rank}")
        lines.append("")
        lines.append(f"- Subject: `{subject.subject_id}`")
        lines.append("")
        for section in subject.sections:
            lines.extend(_render_section_markdown(section))
    return "\n".join(lines).rstrip("\n") + "\n"
