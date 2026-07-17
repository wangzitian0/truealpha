"""Versioned card schemas + deterministic renderer from `ResearchReport` — see #372.

Provisional slice of `#44` (the full Xiaohongshu card vision). It lands the deterministic
schemas and renderer; it explicitly does **not** claim `#44`'s full acceptance — in
particular the product-owner-approved content/typography/rights rubric and the human
sign-off that gates snapshots becoming the regression baseline remain open and require a
human, not this code.

Contract: `build_card` is a pure transform from an already-assembled `#369`
`ResearchReport` plus a versioned template selector to a `ResearchCard`. It performs no
mart query (it never imports a read port or repository — a card is built from a report
object already in memory) and no factor/cross-row computation: it selects which sections'
`ResultValue`s appear on the card, unwraps them unmodified, and attaches static, versioned
research-risk/attribution copy. It never recomputes, rounds, or reinterprets a value.

Card language keeps two boundaries structural, not just documented:

- `ClaimClass` is derived by a fixed lookup from the section's own
  `FactorValidationStatus` (already on the report) — a versioned research hypothesis
  is never presented as an empirically validated claim.
- A `SUPPLY_CHAIN` card's `causal_claim` field is pinned to the single literal
  `"scenario_only"` by the schema itself; nothing here can construct a supply-chain card
  that claims causality (init.md Section 1, rule 10).

Negative, multi-currency, missing, and unavailable values flow through the shared
`ResultValue` type from `#369` unmodified — the card renderer never clips or reformats a
displayed value differently from the report it was built from.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.execution import AvailabilityStatus, FactorValidationStatus
from truealpha_contracts.models import _require_aware
from truealpha_contracts.research_report import ReportSection, ReportSectionKind, ResearchReport, ResultValue

CARD_SCHEMA_VERSION: Literal["research_card.v1"] = "research_card.v1"
_CARD_ID_PREFIX = "card:"

# Provisional Xiaohongshu portrait dimensions (3:4). These are a stable placeholder for a
# deterministic HTML layout, not the approved content/typography/rights rubric #44 requires
# a human to sign off on; changing the approved dimensions is a new `TEMPLATE_VERSION`.
XHS_CARD_WIDTH_PX = 1080
XHS_CARD_HEIGHT_PX = 1440
TEMPLATE_VERSION: Literal["card_template.v1"] = "card_template.v1"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class CardKind(StrEnum):
    COMPANY = "company"
    COMPARISON = "comparison"
    RANKING = "ranking"
    ETF = "etf"
    SUPPLY_CHAIN = "supply_chain"
    STRATEGY_SUMMARY = "strategy_summary"


class ClaimClass(StrEnum):
    """Distinguishes a versioned research hypothesis from an empirically validated claim
    (`#372` acceptance) — derived from the section's own `FactorValidationStatus`, never
    invented at render time."""

    RESEARCH_HYPOTHESIS = "research_hypothesis"
    EMPIRICALLY_VALIDATED = "empirically_validated"
    REJECTED_DO_NOT_USE = "rejected_do_not_use"
    NOT_APPLICABLE = "not_applicable"


def _claim_class(validation_status: FactorValidationStatus | None) -> ClaimClass:
    if validation_status is None:
        return ClaimClass.NOT_APPLICABLE
    return {
        FactorValidationStatus.ACCEPTED: ClaimClass.EMPIRICALLY_VALIDATED,
        FactorValidationStatus.REJECTED: ClaimClass.REJECTED_DO_NOT_USE,
        FactorValidationStatus.NOT_EVALUATED: ClaimClass.RESEARCH_HYPOTHESIS,
    }[validation_status]


# Fixed, versioned per-kind copy (`#372` acceptance: required research-risk/source
# attribution). This is static text keyed by card kind, not a value computed from report
# data.
_RESEARCH_RISK_NOTE: dict[CardKind, str] = {
    CardKind.COMPANY: (
        "Research hypothesis, not investment advice. Confidence reflects source evidence, "
        "not independent formula validation unless marked empirically validated."
    ),
    CardKind.COMPARISON: (
        "Side-by-side comparison of independently materialized results at one cutoff; no "
        "cross-issuer metric is computed for this card."
    ),
    CardKind.RANKING: (
        "Ranking reflects one versioned strategy run at one cutoff; historical rank is not a forward-looking guarantee."
    ),
    CardKind.ETF: (
        "ETF virtual-company view is delayed by public holdings-disclosure lag and is a "
        "research hypothesis, not investment advice."
    ),
    CardKind.SUPPLY_CHAIN: (
        "Scenario propagation over a disclosed relationship graph. High edge confidence "
        "does not by itself establish a causal effect; this is not causal evidence."
    ),
    CardKind.STRATEGY_SUMMARY: (
        "Reflects one versioned strategy's decision at one cutoff, not a performance guarantee."
    ),
}

_SOURCE_ATTRIBUTION = "TrueAlpha research — traceable to a materialized factor output; see the compact trace id."

# Which report section each card kind draws its headline metrics from. A card never
# invents a section; it selects among what the report already carries.
_CARD_SECTIONS: dict[CardKind, tuple[ReportSectionKind, ...]] = {
    CardKind.COMPANY: (ReportSectionKind.OPERATING_EFFICIENCY, ReportSectionKind.VALUATION),
    CardKind.COMPARISON: (ReportSectionKind.VALUATION,),
    CardKind.RANKING: (ReportSectionKind.VALUATION, ReportSectionKind.STRATEGY_SUMMARY),
    CardKind.ETF: (ReportSectionKind.ETF_VIRTUAL_COMPANY,),
    CardKind.SUPPLY_CHAIN: (ReportSectionKind.SUPPLY_CHAIN,),
    CardKind.STRATEGY_SUMMARY: (ReportSectionKind.STRATEGY_SUMMARY,),
}


class CardSubject(_StrictFrozenModel):
    """One card subject's headline metrics, copied unmodified from the report."""

    subject_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    rank: int | None = Field(default=None, ge=1)
    availability: AvailabilityStatus
    claim_class: ClaimClass
    metrics: tuple[ResultValue, ...]
    causal_claim: Literal["scenario_only"] | None = None


class ResearchCard(_StrictFrozenModel):
    """The canonical, content-addressed research card.

    `card_id` is a pure function of the card's content, exactly like `#369`'s
    `ResearchReport.report_id`: a template/report change produces a new revision.
    """

    schema_version: Literal["research_card.v1"] = CARD_SCHEMA_VERSION
    card_id: str = ""
    card_kind: CardKind
    template_version: Literal["card_template.v1"] = TEMPLATE_VERSION
    title: str = Field(min_length=1)
    cutoff_at: datetime
    generated_from_report_id: str = Field(min_length=1)
    research_risk_note: str = Field(min_length=1)
    source_attribution: str = Field(min_length=1)
    subjects: tuple[CardSubject, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _identify(self) -> ResearchCard:
        _require_aware(self.cutoff_at, "cutoff_at")
        for subject in self.subjects:
            if self.card_kind is CardKind.SUPPLY_CHAIN and subject.causal_claim != "scenario_only":
                raise ValueError("a supply-chain card subject must carry the fixed scenario_only causal_claim")
            if self.card_kind is not CardKind.SUPPLY_CHAIN and subject.causal_claim is not None:
                raise ValueError("only a supply-chain card may carry a causal_claim field")
        computed = self._content_hash()
        if self.card_id == "":
            object.__setattr__(self, "card_id", computed)
        elif self.card_id != computed:
            raise ValueError("card_id does not match card content")
        return self

    def _content_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude={"card_id"})
        return f"{_CARD_ID_PREFIX}{canonical_sha256(payload)}"


def _select_metrics(
    sections: tuple[ReportSection, ...], wanted: tuple[ReportSectionKind, ...]
) -> tuple[ResultValue, ...]:
    metrics: list[ResultValue] = []
    for section in sections:
        if section.section_kind in wanted:
            metrics.extend(section.results)
    return tuple(metrics)


def _section_status(
    sections: tuple[ReportSection, ...], wanted: tuple[ReportSectionKind, ...]
) -> tuple[AvailabilityStatus, FactorValidationStatus | None]:
    matched = [section for section in sections if section.section_kind in wanted]
    if not matched:
        return AvailabilityStatus.UNAVAILABLE, None
    # A card is only as available as its least-available contributing section; ties on the
    # enum's declared order are broken by first occurrence (deterministic, no arithmetic).
    priority = (
        AvailabilityStatus.ERROR,
        AvailabilityStatus.UNAVAILABLE,
        AvailabilityStatus.EXCLUDED,
        AvailabilityStatus.LOW_CONFIDENCE,
        AvailabilityStatus.STALE,
        AvailabilityStatus.AVAILABLE,
    )
    worst = min(matched, key=lambda section: priority.index(section.availability))
    return worst.availability, worst.validation_status


def _card_subject(
    *,
    subject_id: str,
    display_name: str,
    rank: int | None,
    sections: tuple[ReportSection, ...],
    card_kind: CardKind,
) -> CardSubject:
    wanted = _CARD_SECTIONS[card_kind]
    metrics = _select_metrics(sections, wanted)
    availability, validation_status = _section_status(sections, wanted)
    causal_claim: Literal["scenario_only"] | None = "scenario_only" if card_kind is CardKind.SUPPLY_CHAIN else None
    return CardSubject(
        subject_id=subject_id,
        display_name=display_name,
        rank=rank,
        availability=availability,
        claim_class=_claim_class(validation_status),
        metrics=metrics,
        causal_claim=causal_claim,
    )


def _default_card_title(card_kind: CardKind, report: ResearchReport) -> str:
    names = ", ".join(subject.display_name for subject in report.subjects)
    return f"{card_kind.value} card: {names}"


def build_card(report: ResearchReport, card_kind: CardKind, *, title: str | None = None) -> ResearchCard:
    """Deterministically builds a `ResearchCard` from an already-assembled `ResearchReport`.

    This selects section results and attaches fixed, versioned research-risk/attribution
    copy; it computes no new metric, ranking, or classification, and it queries nothing —
    `report` is the only input.
    """
    subjects = tuple(
        _card_subject(
            subject_id=subject.subject_id,
            display_name=subject.display_name,
            rank=subject.rank,
            sections=subject.sections,
            card_kind=card_kind,
        )
        for subject in report.subjects
    )
    return ResearchCard(
        card_kind=card_kind,
        title=title if title is not None else _default_card_title(card_kind, report),
        cutoff_at=report.cutoff_at,
        generated_from_report_id=report.report_id,
        research_risk_note=_RESEARCH_RISK_NOTE[card_kind],
        source_attribution=_SOURCE_ATTRIBUTION,
        subjects=subjects,
    )


def render_card_json(card: ResearchCard) -> str:
    """Renders the canonical JSON form: sorted keys, stable separators, trailing newline."""
    payload = card.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True) + "\n"


def _html_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _render_value(result: ResultValue) -> str:
    if result.value is None:
        return "—"
    if result.currency is not None:
        return _html_escape(f"{result.value} {result.currency}")
    if result.unit is not None:
        return _html_escape(f"{result.value} {result.unit}")
    return _html_escape(result.value)


def _render_metric_row(result: ResultValue) -> str:
    trace = result.trace.reference_id if result.trace is not None else "—"
    confidence = "—" if result.confidence is None else str(result.confidence)
    return (
        "<tr>"
        f'<td class="label">{_html_escape(result.label)}</td>'
        f'<td class="value">{_render_value(result)}</td>'
        f'<td class="availability availability-{result.availability.value}">{result.availability.value}</td>'
        f'<td class="confidence">{_html_escape(confidence)}</td>'
        f'<td class="trace">{_html_escape(trace)}</td>'
        "</tr>"
    )


def _render_subject(subject: CardSubject) -> str:
    rank_html = f'<span class="rank">#{subject.rank}</span>' if subject.rank is not None else ""
    causal_html = (
        '<p class="causal-disclaimer">Scenario propagation only — not causal evidence.</p>'
        if subject.causal_claim == "scenario_only"
        else ""
    )
    rows = "".join(_render_metric_row(result) for result in subject.metrics) or (
        '<tr><td colspan="5" class="empty">No materialized result for this card.</td></tr>'
    )
    return (
        '<section class="subject">'
        f"<h2>{_html_escape(subject.display_name)} {rank_html}</h2>"
        f'<p class="claim-class claim-class-{subject.claim_class.value}">{subject.claim_class.value}</p>'
        f'<p class="availability availability-{subject.availability.value}">{subject.availability.value}</p>'
        f"{causal_html}"
        "<table><thead><tr><th>Metric</th><th>Value</th><th>Availability</th><th>Confidence</th><th>Trace</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        "</section>"
    )


def render_card_html(card: ResearchCard) -> str:
    """Renders a deterministic, self-contained HTML card at the provisional Xiaohongshu
    portrait dimensions.

    Pure transform: every printed value comes from `card`'s fields. This produces the
    `HTML` artifact named in `#372`'s scope; rasterizing HTML to a PNG/JPEG image is a
    mechanical downstream step (a headless renderer) with no computation of its own and is
    explicit follow-up, not implemented here (no such renderer is currently a dependency of
    this repository).
    """
    subjects_html = "".join(_render_subject(subject) for subject in card.subjects)
    return (
        "<!doctype html>"
        '<html lang="en"><head><meta charset="utf-8">'
        f"<title>{_html_escape(card.title)}</title>"
        "<style>"
        f"body{{width:{XHS_CARD_WIDTH_PX}px;min-height:{XHS_CARD_HEIGHT_PX}px;margin:0;"
        "font-family:system-ui,sans-serif;background:#0d0e12;color:#f3f4f6;padding:32px;box-sizing:border-box;}"
        "h1{font-size:28px;margin:0 0 8px;} h2{font-size:20px;margin:16px 0 4px;}"
        "table{width:100%;border-collapse:collapse;font-size:14px;margin-top:8px;}"
        "th,td{padding:6px 8px;text-align:left;border-bottom:1px solid #23262f;}"
        ".meta{color:#9ca3af;font-size:13px;} .footer{margin-top:24px;color:#9ca3af;font-size:12px;}"
        ".causal-disclaimer{color:#f59e0b;font-size:13px;}"
        "</style></head><body>"
        f"<h1>{_html_escape(card.title)}</h1>"
        f'<p class="meta">Cutoff: {card.cutoff_at.isoformat()} · Schema {card.schema_version} · '
        f"Template {card.template_version} · Report {_html_escape(card.generated_from_report_id)}</p>"
        f"{subjects_html}"
        f'<p class="footer">{_html_escape(card.research_risk_note)}</p>'
        f'<p class="footer">{_html_escape(card.source_attribution)}</p>'
        "</body></html>"
    )
