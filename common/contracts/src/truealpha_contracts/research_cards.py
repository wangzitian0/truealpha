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
    """Worst availability and most-conservative validation status, computed independently
    across every matched section — not read off whichever section happens to be worst by
    availability. A card pulling multiple sections (e.g. COMPANY: OPERATING_EFFICIENCY +
    VALUATION) must not claim EMPIRICALLY_VALIDATED unless every contributing section is;
    reading validation_status off a single "worst" section could misclassify claim_class
    by availability-tie section order (Copilot review on #390)."""
    matched = [section for section in sections if section.section_kind in wanted]
    if not matched:
        return AvailabilityStatus.UNAVAILABLE, None
    # A card is only as available as its least-available contributing section; ties on the
    # enum's declared order are broken by first occurrence (deterministic, no arithmetic).
    # A dict, not tuple.index(...) inside the min() key: avoids a repeated linear scan of
    # the priority order for every matched section (Copilot review on #390).
    availability_rank = {
        AvailabilityStatus.ERROR: 0,
        AvailabilityStatus.UNAVAILABLE: 1,
        AvailabilityStatus.EXCLUDED: 2,
        AvailabilityStatus.LOW_CONFIDENCE: 3,
        AvailabilityStatus.STALE: 4,
        AvailabilityStatus.AVAILABLE: 5,
    }
    worst_availability = min(matched, key=lambda section: availability_rank[section.availability]).availability

    # Most conservative validation status across every matched section: REJECTED beats
    # NOT_EVALUATED beats ACCEPTED. A section with no validation_status is ignored unless
    # every matched section has none, matching the original all-None -> None/NOT_APPLICABLE
    # behavior (never silently upgrades a card to empirically validated).
    statuses = [section.validation_status for section in matched if section.validation_status is not None]
    if not statuses:
        worst_validation = None
    elif FactorValidationStatus.REJECTED in statuses:
        worst_validation = FactorValidationStatus.REJECTED
    elif FactorValidationStatus.NOT_EVALUATED in statuses:
        worst_validation = FactorValidationStatus.NOT_EVALUATED
    else:
        worst_validation = FactorValidationStatus.ACCEPTED

    return worst_availability, worst_validation


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


XHS_CARD_PROMPT_VERSION: Literal["xhs-card-html:v1"] = "xhs-card-html:v1"

XHS_CARD_INSTRUCTIONS: str = (
    "You are TrueAlpha's Xiaohongshu (RED) visual editorial card designer.\n"
    "Generate a SINGLE self-contained HTML5 document (`<!DOCTYPE html><html>...</html>`) "
    "rendered at exact `1080px` width by `1440px` height (3:4 portrait aspect ratio).\n"
    "Follow the Cold-Verdict Editorial Rubric:\n"
    "1. Canvas & Layout: Fixed 1080x1440px container (`overflow: hidden; box-sizing: border-box;`), "
    "warm editorial paper background (`#F7F5F0`), high-contrast ink (`#111315`), structured Bento grid.\n"
    "2. Zero External Dependencies: Inline CSS only. Do NOT include any `<script>` tag or any external "
    "`http://` or `https://` font, stylesheet, or image URL.\n"
    "3. Strict Fact Preservation (Zero Mutation): Every fact in the input `ResearchCard` JSON MUST appear "
    "verbatim in the HTML:\n"
    "   - Card cutoff date (`cutoff_at` YYYY-MM-DD), `card_id` prefix, `research_risk_note`, and `source_attribution`.\n"
    "   - Every subject's `display_name`, `availability`, `claim_class`, and (when `causal_claim` is "
    "`scenario_only`) a visible `scenario_only` badge.\n"
    "   - Every metric's `label`, exact `value` string (preserving negative signs, decimals, and currency/unit), "
    "`period`, `factor_version`, `confidence`, and compact `trace.reference_id` prefix.\n"
    "4. Output ONLY the raw HTML document starting with `<!DOCTYPE html>` and ending with `</html>`."
)


def build_card_prompt(card: ResearchCard) -> str:
    """Builds the deterministic Cold-Verdict Rubric prompt for LLM HTML card generation."""
    card_json = render_card_json(card)
    return (
        f"{XHS_CARD_INSTRUCTIONS}\n\n"
        f"PROMPT_VERSION: {XHS_CARD_PROMPT_VERSION}\n"
        f"REQUIRED_DIMENSIONS: {XHS_CARD_WIDTH_PX}x{XHS_CARD_HEIGHT_PX}\n"
        "INPUT_RESEARCH_CARD_JSON:\n"
        f"{card_json}"
    )


def extract_html_document(raw_text: str) -> str:
    """Extracts the raw HTML document from model output, stripping optional markdown fences."""
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    lower = cleaned.lower()
    doctype_idx = lower.find("<!doctype html")
    html_idx = lower.find("<html")
    start_idx = doctype_idx if doctype_idx != -1 else html_idx
    end_idx = lower.rfind("</html>")
    if start_idx != -1 and end_idx != -1 and end_idx >= start_idx:
        return cleaned[start_idx : end_idx + len("</html>")]
    return cleaned


def _contains_text(html: str, raw: str) -> bool:
    escaped = _html_escape(raw)
    return raw in html or escaped in html


def _verify_html_envelope(card: ResearchCard, html: str) -> list[str]:
    violations: list[str] = []
    lower = html.lower()
    if ("<!doctype html" not in lower and "<html" not in lower) or "</html>" not in lower:
        violations.append("missing_html_envelope")
    if str(XHS_CARD_WIDTH_PX) not in html or str(XHS_CARD_HEIGHT_PX) not in html:
        violations.append("missing_1080x1440_dimensions")
    if any(token in lower for token in ("<script", "javascript:", "onerror=", "onload=")):
        violations.append("forbidden_script_or_event_handler")
    if "http://" in lower or "https://" in lower:
        violations.append("forbidden_external_url")

    cutoff_date = card.cutoff_at.date().isoformat()
    if cutoff_date not in html and card.cutoff_at.isoformat() not in html:
        violations.append(f"missing_cutoff:{cutoff_date}")
    card_id_prefix = card.card_id[:20]
    if card_id_prefix not in html:
        violations.append(f"missing_card_id_prefix:{card_id_prefix}")
    if not _contains_text(html, card.research_risk_note):
        violations.append("missing_research_risk_note")
    if not _contains_text(html, card.source_attribution):
        violations.append("missing_source_attribution")
    return violations


def _verify_metric_html(subject_id: str, metric: ResultValue, html: str) -> list[str]:
    checks: list[tuple[bool, str]] = [
        (not _contains_text(html, metric.label), f"missing_metric_label:{subject_id}:{metric.label}"),
        (
            metric.value is not None and metric.value not in html,
            f"missing_metric_value:{subject_id}:{metric.label}:{metric.value}",
        ),
        (
            metric.value is not None and metric.currency is not None and metric.currency not in html,
            f"missing_metric_currency:{subject_id}:{metric.label}:{metric.currency}",
        ),
        (
            metric.value is not None and metric.unit is not None and not _contains_text(html, metric.unit),
            f"missing_metric_unit:{subject_id}:{metric.label}:{metric.unit}",
        ),
        (
            metric.period is not None and not _contains_text(html, metric.period),
            f"missing_metric_period:{subject_id}:{metric.label}:{metric.period}",
        ),
        (
            metric.confidence is not None and str(metric.confidence) not in html,
            f"missing_metric_confidence:{subject_id}:{metric.label}:{metric.confidence}",
        ),
    ]
    if metric.trace is not None:
        trace_prefix = metric.trace.reference_id[:16]
        checks.append(
            (not _contains_text(html, trace_prefix), f"missing_metric_trace:{subject_id}:{metric.label}:{trace_prefix}")
        )
    return [reason for failed, reason in checks if failed]


def _verify_subject_html(subject: CardSubject, html: str) -> list[str]:
    violations: list[str] = []
    if not _contains_text(html, subject.display_name) and subject.subject_id not in html:
        violations.append(f"missing_subject:{subject.subject_id}")
    if subject.availability.value not in html:
        violations.append(f"missing_subject_availability:{subject.subject_id}:{subject.availability.value}")
    if subject.claim_class.value not in html:
        violations.append(f"missing_subject_claim_class:{subject.subject_id}:{subject.claim_class.value}")
    if subject.causal_claim == "scenario_only" and "scenario_only" not in html:
        violations.append(f"missing_scenario_only:{subject.subject_id}")
    for metric in subject.metrics:
        violations.extend(_verify_metric_html(subject.subject_id, metric, html))
    return violations


def verify_card_html(card: ResearchCard, html: str) -> tuple[bool, tuple[str, ...]]:
    """Deterministically verifies that `html` is a self-contained 1080x1440 HTML document
    that preserves every fact in `card` without script injection or external URLs.
    """
    violations = _verify_html_envelope(card, html)
    for subject in card.subjects:
        violations.extend(_verify_subject_html(subject, html))
    return (len(violations) == 0, tuple(violations))


def _html_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _render_value(result: ResultValue) -> str:
    if result.value is None:
        return "—"
    if result.currency is not None and result.unit is not None:
        return _html_escape(f"{result.value} {result.currency} ({result.unit})")
    if result.currency is not None:
        return _html_escape(f"{result.value} {result.currency}")
    if result.unit is not None:
        return _html_escape(f"{result.value} {result.unit}")
    return _html_escape(result.value)


def _render_metric_card(result: ResultValue) -> str:
    trace = result.trace.reference_id if result.trace is not None else "—"
    confidence = "—" if result.confidence is None else str(result.confidence)
    period = "—" if result.period is None else result.period
    convention = "—" if result.factor_version is None else result.factor_version
    return (
        '<div class="metric-tile">'
        '<div class="metric-top">'
        f'<span class="metric-label">{_html_escape(result.label)}</span>'
        f'<span class="pill availability-{result.availability.value}">{result.availability.value}</span>'
        "</div>"
        f'<div class="metric-value">{_render_value(result)}</div>'
        '<div class="metric-meta">'
        f"<span>Period: {_html_escape(period)}</span>"
        f"<span>Ver: {_html_escape(convention)}</span>"
        f"<span>Conf: {_html_escape(confidence)}</span>"
        "</div>"
        f'<div class="metric-trace">Trace: {_html_escape(trace)}</div>'
        "</div>"
    )


def _render_subject(subject: CardSubject) -> str:
    rank_html = f'<span class="rank-badge">#{subject.rank}</span>' if subject.rank is not None else ""
    causal_html = (
        '<div class="causal-banner">scenario_only · Scenario propagation only — not causal evidence.</div>'
        if subject.causal_claim == "scenario_only"
        else ""
    )
    tiles = "".join(_render_metric_card(result) for result in subject.metrics) or (
        '<div class="empty-tile">No materialized result for this subject at the governed cutoff.</div>'
    )
    return (
        '<section class="subject-block">'
        '<div class="subject-header">'
        '<div class="subject-title-group">'
        f"{rank_html}"
        f"<h2>{_html_escape(subject.display_name)}</h2>"
        f'<span class="subject-id">{_html_escape(subject.subject_id)}</span>'
        "</div>"
        '<div class="subject-badges">'
        f'<span class="pill claim-class-{subject.claim_class.value}">{subject.claim_class.value}</span>'
        f'<span class="pill availability-{subject.availability.value}">{subject.availability.value}</span>'
        "</div>"
        "</div>"
        f"{causal_html}"
        f'<div class="metrics-grid">{tiles}</div>'
        "</section>"
    )


def render_card_html(card: ResearchCard) -> str:
    """Renders a self-contained 1080x1440 Cold-Verdict Rubric HTML card that satisfies
    `verify_card_html(card, html)` when an LLM-generated card is not yet materialized or
    as the deterministic reference fallback.
    """
    subjects_html = "".join(_render_subject(subject) for subject in card.subjects)
    cutoff_date = card.cutoff_at.date().isoformat()
    return (
        "<!DOCTYPE html>"
        '<html lang="en"><head><meta charset="utf-8">'
        f"<title>{_html_escape(card.title)}</title>"
        "<style>"
        f"html,body{{width:{XHS_CARD_WIDTH_PX}px;min-height:{XHS_CARD_HEIGHT_PX}px;margin:0;padding:0;"
        "background:#F7F5F0;color:#111315;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',"
        "'Segoe UI',sans-serif;box-sizing:border-box;}}"
        f".card-canvas{{width:{XHS_CARD_WIDTH_PX}px;min-height:{XHS_CARD_HEIGHT_PX}px;padding:44px;"
        "box-sizing:border-box;display:flex;flex-direction:column;justify-content:space-between;"
        "border:2px solid #111315;background:#F7F5F0;}"
        ".card-header{border-bottom:2px solid #111315;padding-bottom:20px;margin-bottom:24px;}"
        ".eyebrow{display:flex;justify-content:space-between;align-items:center;font-size:13px;"
        "font-family:ui-monospace,SFMono-Regular,Menlo,monospace;text-transform:uppercase;"
        "letter-spacing:0.08em;color:#4B5563;margin-bottom:10px;}"
        "h1{font-size:34px;line-height:1.15;margin:0 0 10px;font-weight:800;letter-spacing:-0.02em;}"
        ".meta-strip{display:flex;flex-wrap:wrap;gap:12px;font-size:13px;"
        "font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:#374151;}"
        ".subject-block{background:#FFFFFF;border:1.5px solid #111315;border-radius:14px;"
        "padding:22px;margin-bottom:20px;box-shadow:4px 4px 0 #111315;}"
        ".subject-header{display:flex;justify-content:space-between;align-items:flex-start;"
        "gap:12px;border-bottom:1px solid #E5E7EB;padding-bottom:12px;margin-bottom:16px;}"
        ".subject-title-group{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;}"
        "h2{font-size:22px;margin:0;font-weight:700;}"
        ".subject-id{font-size:12px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:#6B7280;}"
        ".rank-badge{background:#111315;color:#F7F5F0;font-weight:700;padding:2px 8px;border-radius:6px;font-size:14px;}"
        ".subject-badges{display:flex;gap:8px;flex-wrap:wrap;}"
        ".pill{font-size:11px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;"
        "padding:3px 8px;border-radius:999px;border:1px solid #111315;background:#F3F4F6;font-weight:600;}"
        ".causal-banner{background:#FEF3C7;border:1px solid #D97706;color:#92400E;"
        "padding:8px 12px;border-radius:8px;font-size:13px;font-weight:600;margin-bottom:14px;}"
        ".metrics-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;}"
        ".metric-tile{background:#F9FAFB;border:1px solid #D1D5DB;border-radius:10px;padding:14px;}"
        ".metric-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;}"
        ".metric-label{font-size:13px;font-weight:700;color:#374151;text-transform:uppercase;letter-spacing:0.03em;}"
        ".metric-value{font-size:26px;font-weight:800;font-variant-numeric:tabular-nums;"
        "font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:#111315;margin:4px 0 8px;}"
        ".metric-meta{display:flex;flex-wrap:wrap;gap:10px;font-size:12px;color:#4B5563;"
        "font-family:ui-monospace,SFMono-Regular,Menlo,monospace;margin-bottom:6px;}"
        ".metric-trace{font-size:11px;color:#6B7280;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;"
        "word-break:break-all;}"
        ".empty-tile{grid-column:1/-1;padding:18px;text-align:center;color:#6B7280;font-size:14px;"
        "background:#F9FAFB;border:1px dashed #D1D5DB;border-radius:8px;}"
        ".card-footer{border-top:2px solid #111315;padding-top:16px;margin-top:auto;"
        "display:flex;flex-direction:column;gap:6px;font-size:12px;color:#374151;}"
        ".footer-ids{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;color:#6B7280;}"
        "</style></head><body>"
        '<div class="card-canvas">'
        "<div>"
        '<header class="card-header">'
        '<div class="eyebrow">'
        f"<span>TRUEALPHA RESEARCH · {card.card_kind.value.upper()}</span>"
        f"<span>CUTOFF {cutoff_date} · {XHS_CARD_WIDTH_PX}x{XHS_CARD_HEIGHT_PX}</span>"
        "</div>"
        f"<h1>{_html_escape(card.title)}</h1>"
        '<div class="meta-strip">'
        f"<span>Cutoff: {card.cutoff_at.isoformat()}</span>"
        f"<span>Schema: {card.schema_version}</span>"
        f"<span>Template: {card.template_version}</span>"
        f"<span>Prompt: {XHS_CARD_PROMPT_VERSION}</span>"
        "</div>"
        "</header>"
        f"{subjects_html}"
        "</div>"
        '<footer class="card-footer">'
        f"<div><strong>Risk Disclosure:</strong> {_html_escape(card.research_risk_note)}</div>"
        f"<div><strong>Source Attribution:</strong> {_html_escape(card.source_attribution)}</div>"
        f'<div class="footer-ids">Card ID: {_html_escape(card.card_id)} · '
        f"Report ID: {_html_escape(card.generated_from_report_id)}</div>"
        "</footer>"
        "</div>"
        "</body></html>"
    )
