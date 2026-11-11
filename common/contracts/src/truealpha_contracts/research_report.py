"""Canonical research-report model, deterministic assembler, and renderers (#369)."""

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
    COMPANY = "company"
    ETF = "etf"
    THEME_RANKING = "theme_ranking"


class ReportSectionKind(StrEnum):
    OPERATING_EFFICIENCY = "operating_efficiency"
    VALUATION = "valuation"
    PEG_CONVENTIONS = "peg_conventions"
    SUPPLY_CHAIN = "supply_chain"
    ANALYST_HISTORY = "analyst_history"
    ETF_VIRTUAL_COMPANY = "etf_virtual_company"
    PURE_BLOOD = "pure_blood"
    STRATEGY_SUMMARY = "strategy_summary"
    RANKING = "ranking"


class EvidenceTrace(_StrictFrozenModel):
    reference_id: str = Field(min_length=1)
    materialized_output_id: str | None = None
    snapshot_id: str | None = None
    factor_execution_id: str | None = None


class ResultValue(_StrictFrozenModel):
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
    section_kind: ReportSectionKind
    title: str = Field(min_length=1)
    availability: AvailabilityStatus
    validation_status: FactorValidationStatus | None = None
    results: tuple[ResultValue, ...] = ()
    notes: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()


class ReportSubject(_StrictFrozenModel):
    subject_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    rank: int | None = Field(default=None, ge=1)
    sections: tuple[ReportSection, ...]


class ResearchReportRequest(_StrictFrozenModel):
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
        return cls(
            report_kind=report_kind,
            title=title,
            cutoff_at=cutoff_at,
            generated_from=generated_from,
            subjects=subjects,
        )


class ResearchReadPort(Protocol):
    @property
    def provenance_label(self) -> str: ...
    def load_subjects(self, *, request: ResearchReportRequest, context: AccessContext) -> tuple[ReportSubject, ...]: ...


def _select_sections(
    sections: tuple[ReportSection, ...],
    section_kinds: tuple[ReportSectionKind, ...],
) -> tuple[ReportSection, ...]:
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
    return json.dumps(report.model_dump(mode="json"), sort_keys=True, indent=2, ensure_ascii=True) + "\n"


def _render_result_row(result: ResultValue) -> str:
    unit = result.currency if result.currency is not None else result.unit
    value = "—" if result.value is None else (f"{result.value} {unit}" if unit is not None else result.value)
    cells = [
        result.label,
        value,
        "—" if result.period is None else result.period,
        result.availability.value,
        "—" if result.confidence is None else str(result.confidence),
        "—" if result.factor_version is None else result.factor_version,
        "—" if result.trace is None else result.trace.reference_id,
    ]
    return "| " + " | ".join(cells) + " |"


def _render_section_markdown(section: ReportSection) -> list[str]:
    lines = [f"### {section.title}", "", f"- Availability: `{section.availability.value}`"]
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
    if section.notes:
        lines.extend(f"> {note}" for note in section.notes)
        lines.append("")
    return lines


def render_report_markdown(report: ResearchReport) -> str:
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
        rank = f" (rank {subject.rank})" if subject.rank is not None else ""
        lines.extend([f"## {subject.display_name}{rank}", "", f"- Subject: `{subject.subject_id}`", ""])
        for section in subject.sections:
            lines.extend(_render_section_markdown(section))
    return "\n".join(lines).rstrip("\n") + "\n"
