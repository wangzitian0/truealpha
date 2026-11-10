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

import html as html_lib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer, model_validator

from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.execution import AvailabilityStatus, FactorValidationStatus
from truealpha_contracts.models import _require_aware
from truealpha_contracts.research_report import ReportSection, ReportSectionKind, ResearchReport, ResultValue

CARD_SCHEMA_VERSION: Literal["research_card.v1"] = "research_card.v1"
_CARD_ID_PREFIX = "card:"
MAX_SUBJECTS_PER_CARD: Final[int] = 4

# Provisional Xiaohongshu portrait dimensions (3:4). These are a stable placeholder for a
# deterministic HTML layout, not the approved content/typography/rights rubric #44 requires
# a human to sign off on; changing the approved dimensions is a new `TEMPLATE_VERSION`.
XHS_CARD_WIDTH_PX = 1080
XHS_CARD_HEIGHT_PX = 1440
TEMPLATE_VERSION: Literal["card_template.v1"] = "card_template.v1"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class CardStyle(StrEnum):
    BOLD = "bold"
    SCREEN_PRINT = "screen-print"
    NOTION = "notion"
    MINIMAL = "minimal"
    STUDY_NOTES = "study-notes"
    RETRO = "retro"
    CHALKBOARD = "chalkboard"
    POP = "pop"
    SKETCH_NOTES = "sketch-notes"
    FRESH = "fresh"


class CardLayout(StrEnum):
    SPARSE = "sparse"
    BALANCED = "balanced"
    DENSE = "dense"
    LIST = "list"
    COMPARISON = "comparison"
    FLOW = "flow"
    MINDMAP = "mindmap"
    QUADRANT = "quadrant"


class CardPalette(StrEnum):
    WARM = "warm"
    COOL = "cool"
    VIVID = "vivid"
    PASTEL = "pastel"
    MONOCHROME = "monochrome"
    NEON = "neon"
    MACARON = "macaron"
    EARTH = "earth"


class CardPreset(_StrictFrozenModel):
    preset_id: str = Field(min_length=1)
    style: CardStyle
    layout: CardLayout
    palette: CardPalette
    bg_css: str = Field(min_length=1)
    surface_bg: str = Field(min_length=1)
    fg: str = Field(min_length=1)
    mute: str = Field(min_length=1)
    accent: str = Field(min_length=1)
    border: str = Field(min_length=1)
    chip_bg: str = Field(min_length=1)


CARD_PRESETS: Final[dict[str, CardPreset]] = {
    "bold": CardPreset(
        preset_id="bold",
        style=CardStyle.BOLD,
        layout=CardLayout.SPARSE,
        palette=CardPalette.NEON,
        bg_css="radial-gradient(circle at 85% 12%, #311042 0%, #140C1C 55%, #0B0711 100%)",
        surface_bg="#191126",
        fg="#F5F5F7",
        mute="#B8B8D4",
        accent="#39FF14",
        border="#00F5FF",
        chip_bg="#120B1D",
    ),
    "screen-print": CardPreset(
        preset_id="screen-print",
        style=CardStyle.SCREEN_PRINT,
        layout=CardLayout.LIST,
        palette=CardPalette.EARTH,
        bg_css="#F5E6D0",
        surface_bg="#FFFDF9",
        fg="#121212",
        mute="#4A3F35",
        accent="#E8751A",
        border="#121212",
        chip_bg="#EDE0CC",
    ),
    "notion": CardPreset(
        preset_id="notion",
        style=CardStyle.NOTION,
        layout=CardLayout.LIST,
        palette=CardPalette.MACARON,
        bg_css="#FFFFFF",
        surface_bg="#FBFBFA",
        fg="#37352F",
        mute="#6B6860",
        accent="#2F7D32",
        border="#E3E2DE",
        chip_bg="#F7F6F3",
    ),
    "minimal": CardPreset(
        preset_id="minimal",
        style=CardStyle.MINIMAL,
        layout=CardLayout.COMPARISON,
        palette=CardPalette.COOL,
        bg_css="#FAFAFA",
        surface_bg="#FFFFFF",
        fg="#111111",
        mute="#555555",
        accent="#0A6847",
        border="#111111",
        chip_bg="#F2F2F2",
    ),
    "study-notes": CardPreset(
        preset_id="study-notes",
        style=CardStyle.STUDY_NOTES,
        layout=CardLayout.DENSE,
        palette=CardPalette.WARM,
        bg_css="#FAF6EE",
        surface_bg="#FFFFFF",
        fg="#222222",
        mute="#5C5852",
        accent="#1E3A5F",
        border="#1E3A5F",
        chip_bg="#F3EDE0",
    ),
    "retro": CardPreset(
        preset_id="retro",
        style=CardStyle.RETRO,
        layout=CardLayout.BALANCED,
        palette=CardPalette.WARM,
        bg_css="#F2E6D0",
        surface_bg="#FAF3E6",
        fg="#2A1F18",
        mute="#634F40",
        accent="#2E6B4E",
        border="#2A1F18",
        chip_bg="#E8D8BC",
    ),
    "chalkboard": CardPreset(
        preset_id="chalkboard",
        style=CardStyle.CHALKBOARD,
        layout=CardLayout.QUADRANT,
        palette=CardPalette.MONOCHROME,
        bg_css="#1E3226",
        surface_bg="#263D2F",
        fg="#F2EFE9",
        mute="#B8C7BC",
        accent="#FFE066",
        border="#5E7A68",
        chip_bg="#18281E",
    ),
    "pop": CardPreset(
        preset_id="pop",
        style=CardStyle.POP,
        layout=CardLayout.QUADRANT,
        palette=CardPalette.VIVID,
        bg_css="#FFE600",
        surface_bg="#FFFFFF",
        fg="#000000",
        mute="#333333",
        accent="#FF0055",
        border="#000000",
        chip_bg="#FFF9B0",
    ),
    "sketch-notes": CardPreset(
        preset_id="sketch-notes",
        style=CardStyle.SKETCH_NOTES,
        layout=CardLayout.FLOW,
        palette=CardPalette.WARM,
        bg_css="#FCF9F2",
        surface_bg="#FFFFFF",
        fg="#2B2927",
        mute="#5C5852",
        accent="#E85D04",
        border="#2B2927",
        chip_bg="#F4EFE6",
    ),
    "fresh": CardPreset(
        preset_id="fresh",
        style=CardStyle.FRESH,
        layout=CardLayout.MINDMAP,
        palette=CardPalette.PASTEL,
        bg_css="linear-gradient(160deg, #E8F8F5 0%, #F4FBF7 55%, #FFFFFF 100%)",
        surface_bg="#FFFFFF",
        fg="#1B3B2B",
        mute="#4A6B5D",
        accent="#148F77",
        border="#48C9B0",
        chip_bg="#E8F6F3",
    ),
}


class StrategyContext(_StrictFrozenModel):
    """Structured strategy funnel and three-tier P/GP ceiling metadata from an already-materialized strategy run."""

    strategy_id: str = Field(min_length=1)
    definition_version: str = Field(min_length=1)
    valuation_basis: str = Field(min_length=1)
    selection_count: int = Field(ge=1)
    total_universe_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    ranked_beyond_selection_count: int = Field(ge=0)
    rejected_above_band_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    tier_ceilings: tuple[tuple[str, str], ...] = ()


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
    CardKind.RANKING: (ReportSectionKind.VALUATION, ReportSectionKind.STRATEGY_SUMMARY, ReportSectionKind.RANKING),
    CardKind.ETF: (ReportSectionKind.ETF_VIRTUAL_COMPANY,),
    CardKind.SUPPLY_CHAIN: (ReportSectionKind.SUPPLY_CHAIN,),
    CardKind.STRATEGY_SUMMARY: (ReportSectionKind.STRATEGY_SUMMARY,),
}

_OPTIONAL_SUBJECT_FIELDS: Final[tuple[str, ...]] = ("ticker", "outcome", "exclusion_reason")
_OPTIONAL_CARD_FIELDS: Final[tuple[str, ...]] = (
    "strategy_context",
    "style_preset",
    "series_index",
    "series_total",
    "rendered_html",
)


class CardSubject(_StrictFrozenModel):
    """One card subject's headline metrics, copied unmodified from the report."""

    subject_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    rank: int | None = Field(default=None, ge=1)
    availability: AvailabilityStatus
    claim_class: ClaimClass
    metrics: tuple[ResultValue, ...]
    causal_claim: Literal["scenario_only"] | None = None
    ticker: str | None = None
    outcome: str | None = None
    exclusion_reason: str | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_optional_fields(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        for key in _OPTIONAL_SUBJECT_FIELDS:
            if data.get(key) is None:
                data.pop(key, None)
        return data


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
    strategy_context: StrategyContext | None = None
    style_preset: str | None = None
    series_index: int | None = Field(default=None, ge=1)
    series_total: int | None = Field(default=None, ge=1)
    rendered_html: str | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_optional_fields(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        for key in _OPTIONAL_CARD_FIELDS:
            if data.get(key) is None:
                data.pop(key, None)
        return data

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
        payload = self.model_dump(mode="json", exclude={"card_id", "rendered_html"})
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


_DEFAULT_TIER_CEILINGS: Final[tuple[tuple[str, str], ...]] = (
    ("large_model_native", "33.34"),
    ("tech", "12.86"),
    ("traditional", "8.75"),
)


def _extract_subject_strategy_fields(subject: Any) -> dict[str, str]:
    extracted: dict[str, str] = {}
    for section in subject.sections:
        for res in section.results:
            if res.value and res.label in (
                "outcome",
                "tier",
                "target_price_to_gross_profit",
                "target_price_to_sales",
            ):
                extracted[res.label] = res.value
    return extracted


def _is_subject_excluded(subject: Any, outcome_val: str | None) -> bool:
    if outcome_val == "excluded":
        return True
    if not subject.sections:
        return False
    return all(
        s.availability in (AvailabilityStatus.EXCLUDED, AvailabilityStatus.LOW_CONFIDENCE) for s in subject.sections
    )


def build_strategy_context_from_report(
    report: ResearchReport,
    *,
    strategy_id: str = "large_model_value_v0",
    definition_version: str = "1.1.0",
    selection_count: int = 2,
) -> StrategyContext:
    """Extracts strategy funnel counts and valuation basis from already-materialized report sections."""
    counts = {
        "selected": 0,
        "ranked_beyond_selection_count": 0,
        "rejected_valuation_above_tier_band": 0,
        "excluded": 0,
    }
    basis = "price_to_gross_profit"
    ceilings: dict[str, str] = dict(_DEFAULT_TIER_CEILINGS)
    for subject in report.subjects:
        fields = _extract_subject_strategy_fields(subject)
        outcome_val = fields.get("outcome")
        tier_val = fields.get("tier")
        target_pgp = fields.get("target_price_to_gross_profit")
        target_ps = fields.get("target_price_to_sales")
        if target_ps is not None and target_pgp is None:
            basis = "price_to_sales"
        if tier_val and (target_pgp or target_ps):
            ceilings[tier_val] = target_pgp or target_ps or ""
        if outcome_val in counts:
            counts[outcome_val] += 1
        elif _is_subject_excluded(subject, outcome_val):
            counts["excluded"] += 1
    return StrategyContext(
        strategy_id=strategy_id,
        definition_version=definition_version,
        valuation_basis=basis,
        selection_count=selection_count,
        total_universe_count=len(report.subjects),
        selected_count=counts["selected"],
        ranked_beyond_selection_count=counts["ranked_beyond_selection_count"],
        rejected_above_band_count=counts["rejected_valuation_above_tier_band"],
        excluded_count=counts["excluded"],
        tier_ceilings=tuple(ceilings.items()),
    )


def enrich_card_metadata(
    card: ResearchCard,
    report: ResearchReport,
    *,
    strategy_context: StrategyContext | None = None,
    style_preset: str | None = None,
    series_index: int | None = None,
    series_total: int | None = None,
    include_rendered_html: bool = False,
) -> ResearchCard:
    """Attaches optional subject-level ticker/outcome/exclusion_reason, strategy context, and rendered HTML."""
    report_by_id = {s.subject_id: s for s in report.subjects}
    enriched_subjects: list[CardSubject] = []
    for sub in card.subjects:
        rep_sub = report_by_id.get(sub.subject_id)
        ticker = sub.ticker
        if ticker is None and " · " in sub.display_name:
            ticker = sub.display_name.split(" · ", 1)[0].strip()
        outcome = sub.outcome
        exclusion_reason = sub.exclusion_reason
        if rep_sub is not None:
            for sec in rep_sub.sections:
                if sec.section_kind is ReportSectionKind.STRATEGY_SUMMARY:
                    if exclusion_reason is None and sec.reason_codes:
                        exclusion_reason = sec.reason_codes[0]
                    for res in sec.results:
                        if res.label == "outcome" and res.value and outcome is None:
                            outcome = res.value
        if outcome == "excluded" and exclusion_reason is None:
            exclusion_reason = "missing_audited_gross_profit_or_coordinate_join"
        enriched_subjects.append(
            CardSubject(
                subject_id=sub.subject_id,
                display_name=sub.display_name,
                rank=sub.rank,
                availability=sub.availability,
                claim_class=sub.claim_class,
                metrics=sub.metrics,
                causal_claim=sub.causal_claim,
                ticker=ticker,
                outcome=outcome,
                exclusion_reason=exclusion_reason,
            )
        )
    base = ResearchCard(
        schema_version=card.schema_version,
        card_kind=card.card_kind,
        template_version=card.template_version,
        title=card.title,
        cutoff_at=card.cutoff_at,
        generated_from_report_id=card.generated_from_report_id,
        research_risk_note=card.research_risk_note,
        source_attribution=card.source_attribution,
        subjects=tuple(enriched_subjects),
        strategy_context=strategy_context if strategy_context is not None else card.strategy_context,
        style_preset=style_preset if style_preset is not None else card.style_preset,
        series_index=series_index if series_index is not None else card.series_index,
        series_total=series_total if series_total is not None else card.series_total,
    )
    if not include_rendered_html:
        return base
    html_doc = render_card_html(base, preset=base.style_preset)
    return ResearchCard(
        schema_version=base.schema_version,
        card_id=base.card_id,
        card_kind=base.card_kind,
        template_version=base.template_version,
        title=base.title,
        cutoff_at=base.cutoff_at,
        generated_from_report_id=base.generated_from_report_id,
        research_risk_note=base.research_risk_note,
        source_attribution=base.source_attribution,
        subjects=base.subjects,
        strategy_context=base.strategy_context,
        style_preset=base.style_preset,
        series_index=base.series_index,
        series_total=base.series_total,
        rendered_html=html_doc,
    )


def paginate_card_series(
    card: ResearchCard,
    *,
    max_subjects_per_card: int = MAX_SUBJECTS_PER_CARD,
    preset: str | None = None,
) -> tuple[ResearchCard, ...]:
    """Splits a multi-subject `ResearchCard` into a bounded series (`<= max_subjects_per_card` per card)
    so no single `1080x1440` card overflows vertically."""
    if max_subjects_per_card < 1:
        raise ValueError("max_subjects_per_card must be >= 1")
    total_subjects = len(card.subjects)
    if total_subjects <= max_subjects_per_card and preset is None:
        return (card,)
    chunks = [
        card.subjects[idx : idx + max_subjects_per_card] for idx in range(0, total_subjects, max_subjects_per_card)
    ]
    series_total = len(chunks)
    preset_keys = tuple(CARD_PRESETS.keys())
    pages: list[ResearchCard] = []
    for page_idx, chunk in enumerate(chunks, start=1):
        chosen_preset = preset or card.style_preset or preset_keys[(page_idx - 1) % len(preset_keys)]
        names = ", ".join(s.ticker or s.display_name.split(" · ")[0] for s in chunk)
        page_title = f"{card.card_kind.value} ({page_idx}/{series_total}): {names}" if series_total > 1 else card.title
        pages.append(
            ResearchCard(
                schema_version=card.schema_version,
                card_kind=card.card_kind,
                template_version=card.template_version,
                title=page_title,
                cutoff_at=card.cutoff_at,
                generated_from_report_id=card.generated_from_report_id,
                research_risk_note=card.research_risk_note,
                source_attribution=card.source_attribution,
                subjects=chunk,
                strategy_context=card.strategy_context,
                style_preset=chosen_preset,
                series_index=page_idx,
                series_total=series_total,
            )
        )
    return tuple(pages)


def render_card_json(card: ResearchCard) -> str:
    """Renders the canonical JSON form: sorted keys, stable separators, trailing newline."""
    payload = card.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True) + "\n"


XHS_CARD_PROMPT_VERSION: Literal["xhs-card-html:v1"] = "xhs-card-html:v1"

XHS_CARD_INSTRUCTIONS: str = (
    "You are TrueAlpha's Xiaohongshu (RED) visual editorial card designer.\n"
    "Generate a SINGLE self-contained HTML5 document (`<!DOCTYPE html><html>...</html>`) "
    "rendered at exact `1080px` width by `1440px` height (3:4 portrait aspect ratio).\n"
    "Follow the Cold-Verdict Chinese Buy-Side Rubric:\n"
    "1. Canvas & Layout: Fixed 1080x1440px container (`width: 1080px; height: 1440px; overflow: hidden; "
    "box-sizing: border-box;`), max 4 subjects per card in the main visual canvas, structured Bento grid.\n"
    "2. Direct Chinese Buy-Side Copy: Use concise Chinese buy-side terminology in the main visual canvas "
    "(e.g., 较本档上限折价, 当前市值/毛利 P/GP, 档位上限, 人均资本调整毛利 GPPE, 入选/超上限淘汰/缺审定毛利剔除). "
    "Avoid generic AI filler phrases or raw internal field names in headlines.\n"
    "3. Zero External Dependencies: Inline CSS only. Do NOT include any `<script>` tag or any external "
    "`http://` or `https://` font, stylesheet, or image URL.\n"
    "4. Strict Fact Preservation (Two-Tier Footer): Every fact in the input `ResearchCard` JSON MUST appear "
    "verbatim in the HTML (main canvas + compact machine-verifiable provenance footer):\n"
    "   - Card cutoff date (`cutoff_at` YYYY-MM-DD), `card_id` prefix, `research_risk_note`, and `source_attribution`.\n"
    "   - Every subject's `display_name`, `availability`, `claim_class`, and (when `causal_claim` is "
    "`scenario_only`) a visible `scenario_only` badge.\n"
    "   - Every metric's `label`, exact `value` string (preserving negative signs, decimals, and currency/unit), "
    "`period`, `factor_version`, `confidence`, and compact `trace.reference_id` prefix.\n"
    "5. Output ONLY the raw HTML document starting with `<!DOCTYPE html>` and ending with `</html>`."
)


def _compact_card_for_prompt(card: ResearchCard) -> dict[str, object]:
    return {
        "schema_version": card.schema_version,
        "template_version": card.template_version,
        "card_id": card.card_id,
        "card_id_required_prefix": card.card_id[:12],
        "card_kind": card.card_kind.value,
        "title": card.title,
        "cutoff_at": card.cutoff_at.isoformat(),
        "cutoff_date": card.cutoff_at.date().isoformat(),
        "research_risk_note": card.research_risk_note,
        "source_attribution": card.source_attribution,
        "subjects": [
            {
                "subject_id": s.subject_id,
                "display_name": s.display_name,
                "rank": s.rank,
                "availability": s.availability.value,
                "claim_class": s.claim_class.value,
                "causal_claim": s.causal_claim,
                "metrics": [
                    {
                        "label": m.label,
                        "value": m.value,
                        "currency": m.currency,
                        "unit": m.unit,
                        "period": m.period,
                        "factor_version": m.factor_version,
                        "confidence": str(m.confidence) if m.confidence is not None else None,
                        "trace_prefix": m.trace.reference_id[:16] if m.trace is not None else None,
                    }
                    for m in s.metrics
                ],
            }
            for s in card.subjects
        ],
    }


def build_card_prompt(card: ResearchCard) -> str:
    """Builds the deterministic Cold-Verdict Rubric prompt for LLM HTML card generation."""
    card_json = json.dumps(_compact_card_for_prompt(card), indent=2, ensure_ascii=False)
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


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_html_text(html_doc: str) -> str:
    stripped = _HTML_TAG_RE.sub(" ", html_doc)
    unescaped = stripped
    for _ in range(3):
        next_val = html_lib.unescape(unescaped)
        if next_val == unescaped:
            break
        unescaped = next_val
    return _WHITESPACE_RE.sub(" ", _HTML_TAG_RE.sub(" ", unescaped))


def _contains_text(html_doc: str, raw: str, normalized_text: str | None = None) -> bool:
    escaped = _html_escape(raw)
    if raw in html_doc or escaped in html_doc:
        return True
    norm_doc = normalized_text if normalized_text is not None else _normalize_html_text(html_doc)
    norm_raw = _WHITESPACE_RE.sub(" ", html_lib.unescape(raw)).strip()
    return norm_raw in norm_doc


_INLINE_EVENT_HANDLER_RE = re.compile(r"\bon[a-z]+\s*=", re.IGNORECASE)


def _verify_html_envelope(card: ResearchCard, html: str, normalized_text: str) -> list[str]:
    violations: list[str] = []
    lower = html.lower()
    if ("<!doctype html" not in lower and "<html" not in lower) or "</html>" not in lower:
        violations.append("missing_html_envelope")
    if str(XHS_CARD_WIDTH_PX) not in html or str(XHS_CARD_HEIGHT_PX) not in html:
        violations.append("missing_1080x1440_dimensions")
    if any(token in lower for token in ("<script", "javascript:")) or _INLINE_EVENT_HANDLER_RE.search(html):
        violations.append("forbidden_script_or_event_handler")
    if "http://" in lower or "https://" in lower:
        violations.append("forbidden_external_url")

    cutoff_date = card.cutoff_at.date().isoformat()
    if cutoff_date not in html and card.cutoff_at.isoformat() not in html:
        violations.append(f"missing_cutoff:{cutoff_date}")
    card_id_prefix = card.card_id[:12]
    if card_id_prefix not in html and card_id_prefix not in normalized_text:
        violations.append(f"missing_card_id_prefix:{card_id_prefix}")
    if not _contains_text(html, card.research_risk_note, normalized_text):
        violations.append("missing_research_risk_note")
    if not _contains_text(html, card.source_attribution, normalized_text):
        violations.append("missing_source_attribution")
    return violations


def _verify_metric_html(subject_id: str, metric: ResultValue, html: str, normalized_text: str) -> list[str]:
    checks: list[tuple[bool, str]] = [
        (not _contains_text(html, metric.label, normalized_text), f"missing_metric_label:{subject_id}:{metric.label}"),
        (
            metric.value is not None and not _contains_text(html, metric.value, normalized_text),
            f"missing_metric_value:{subject_id}:{metric.label}:{metric.value}",
        ),
        (
            metric.value is not None
            and metric.currency is not None
            and not _contains_text(html, metric.currency, normalized_text),
            f"missing_metric_currency:{subject_id}:{metric.label}:{metric.currency}",
        ),
        (
            metric.value is not None
            and metric.unit is not None
            and not _contains_text(html, metric.unit, normalized_text),
            f"missing_metric_unit:{subject_id}:{metric.label}:{metric.unit}",
        ),
        (
            metric.period is not None and not _contains_text(html, metric.period, normalized_text),
            f"missing_metric_period:{subject_id}:{metric.label}:{metric.period}",
        ),
        (
            metric.confidence is not None and not _contains_text(html, str(metric.confidence), normalized_text),
            f"missing_metric_confidence:{subject_id}:{metric.label}:{metric.confidence}",
        ),
    ]
    if metric.trace is not None:
        trace_prefix = metric.trace.reference_id[:16]
        checks.append(
            (
                not _contains_text(html, trace_prefix, normalized_text),
                f"missing_metric_trace:{subject_id}:{metric.label}:{trace_prefix}",
            )
        )
    return [reason for failed, reason in checks if failed]


def _verify_subject_html(subject: CardSubject, html: str, normalized_text: str) -> list[str]:
    violations: list[str] = []
    if not _contains_text(html, subject.display_name, normalized_text) and subject.subject_id not in html:
        violations.append(f"missing_subject:{subject.subject_id}")
    if subject.availability.value not in html and subject.availability.value not in normalized_text:
        violations.append(f"missing_subject_availability:{subject.subject_id}:{subject.availability.value}")
    if subject.claim_class.value not in html and subject.claim_class.value not in normalized_text:
        violations.append(f"missing_subject_claim_class:{subject.subject_id}:{subject.claim_class.value}")
    if subject.causal_claim == "scenario_only" and "scenario_only" not in html:
        violations.append(f"missing_scenario_only:{subject.subject_id}")
    for metric in subject.metrics:
        violations.extend(_verify_metric_html(subject.subject_id, metric, html, normalized_text))
    return violations


def verify_card_html(card: ResearchCard, html: str) -> tuple[bool, tuple[str, ...]]:
    """Deterministically verifies that `html` is a self-contained 1080x1440 HTML document
    that preserves every fact in `card` without script injection or external URLs.
    """
    normalized_text = _normalize_html_text(html)
    violations = _verify_html_envelope(card, html, normalized_text)
    for subject in card.subjects:
        violations.extend(_verify_subject_html(subject, html, normalized_text))
    return (len(violations) == 0, tuple(violations))


def _html_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


_METRIC_LABELS_ZH: Final[dict[str, str]] = {
    "capital_adjusted_labor_efficiency": "人均资本调整毛利 (GPPE)",
    "tier": "所属估值档位",
    "current_price_to_gross_profit": "当前市值/毛利 (P/GP)",
    "target_price_to_gross_profit": "本档 P/GP 上限",
    "current_price_to_sales": "当前市销率 (P/S)",
    "target_price_to_sales": "本档 P/S 上限",
    "valuation_gap": "较上限折价空间",
    "outcome": "策略筛选结论",
    "eligible": "是否纳入排位",
    "rank": "当期策略排名",
    "target_weight": "目标组合权重",
    "exposure_score": "供应链敞口得分",
    "direct_partners": "直接披露伙伴数",
    "suppliers_count": "上游供应商数",
    "customers_count": "下游客户数",
    "max_partner_share": "最大单一伙伴占比",
    "virtual_company_fundamentals": "ETF 穿透虚拟公司基本面",
}

_TIER_LABELS_ZH: Final[dict[str, str]] = {
    "large_model_native": "大模型原生档 (上限 33.34x)",
    "tech": "科技档 (上限 12.86x)",
    "traditional": "传统档 (上限 8.75x)",
}

_OUTCOME_LABELS_ZH: Final[dict[str, str]] = {
    "selected": "入选 (Top 2)",
    "ranked_beyond_selection_count": "有折价 · 排第 2 名后候补",
    "rejected_valuation_above_tier_band": "淘汰：估值超出本档上限",
    "excluded": "剔除：缺审定毛利或坐标",
}

_CARD_KIND_ZH: Final[dict[CardKind, str]] = {
    CardKind.COMPANY: "单票深度拆解 · COMPANY",
    CardKind.COMPARISON: "多票横向对比 · COMPARISON",
    CardKind.RANKING: "策略估值折价榜 · RANKING",
    CardKind.ETF: "ETF 穿透虚拟公司 · ETF",
    CardKind.SUPPLY_CHAIN: "供应链情景推演 · SUPPLY CHAIN",
    CardKind.STRATEGY_SUMMARY: "策略过筛漏斗 · STRATEGY",
}


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


def _display_metric_value(result: ResultValue) -> str:
    if result.value is None:
        return "—"
    if result.label == "tier":
        return _html_escape(_TIER_LABELS_ZH.get(result.value, result.value))
    if result.label == "outcome":
        return _html_escape(_OUTCOME_LABELS_ZH.get(result.value, result.value))
    return _render_value(result)


def _render_metric_card(result: ResultValue, *, compact: bool = False) -> str:
    confidence = "—" if result.confidence is None else str(result.confidence)
    period = "—" if result.period is None else result.period
    zh_label = _METRIC_LABELS_ZH.get(result.label, result.label)
    if compact:
        return (
            '<div class="metric-tile compact">'
            f'<div class="metric-label-zh">{_html_escape(zh_label)}</div>'
            f'<div class="metric-value">{_display_metric_value(result)}</div>'
            "</div>"
        )
    return (
        '<div class="metric-tile">'
        '<div class="metric-top">'
        f'<span class="metric-label-zh">{_html_escape(zh_label)}</span>'
        f'<span class="pill availability-{result.availability.value}">{result.availability.value}</span>'
        "</div>"
        f'<div class="metric-value">{_display_metric_value(result)}</div>'
        '<div class="metric-meta">'
        f"<span>截面: {_html_escape(period)}</span>"
        f"<span>置信度: {_html_escape(confidence)}</span>"
        "</div>"
        "</div>"
    )


def _render_subject(subject: CardSubject, *, compact: bool = False) -> str:
    rank_html = f'<span class="rank-badge">#{subject.rank}</span>' if subject.rank is not None else ""
    causal_html = (
        '<div class="causal-banner">scenario_only · 仅做图谱情景推演，不构成因果结论。</div>'
        if subject.causal_claim == "scenario_only"
        else ""
    )
    outcome_badge = ""
    if subject.outcome:
        zh_outcome = _OUTCOME_LABELS_ZH.get(subject.outcome, subject.outcome)
        outcome_badge = f'<span class="pill outcome-pill">{_html_escape(zh_outcome)}</span>'
    elif subject.exclusion_reason:
        outcome_badge = f'<span class="pill outcome-pill">剔除: {_html_escape(subject.exclusion_reason)}</span>'
    visible_metrics = subject.metrics[:4] if compact else subject.metrics[:6]
    tiles = "".join(_render_metric_card(result, compact=compact) for result in visible_metrics) or (
        '<div class="empty-tile">当前治理截面暂无已物化因子数据 (unavailable)</div>'
    )
    return (
        f'<section class="subject-block{" compact" if compact else ""}">'
        '<div class="subject-header">'
        '<div class="subject-title-group">'
        f"{rank_html}"
        f"<h2>{_html_escape(subject.display_name)}</h2>"
        f'<span class="subject-id">{_html_escape(subject.subject_id)}</span>'
        "</div>"
        '<div class="subject-badges">'
        f"{outcome_badge}"
        f'<span class="pill claim-class-{subject.claim_class.value}">{subject.claim_class.value}</span>'
        f'<span class="pill availability-{subject.availability.value}">{subject.availability.value}</span>'
        "</div>"
        "</div>"
        f"{causal_html}"
        f'<div class="metrics-grid{" compact" if compact else ""}">{tiles}</div>'
        "</section>"
    )


def _subject_evidence_line(subject: CardSubject) -> str:
    parts = [
        f"id={_html_escape(subject.subject_id)}",
        f"name={_html_escape(subject.display_name)}",
        f"avail={_html_escape(subject.availability.value)}",
        f"claim={_html_escape(subject.claim_class.value)}",
    ]
    if subject.causal_claim == "scenario_only":
        parts.append("causal=scenario_only")
    for metric in subject.metrics:
        trace_prefix = metric.trace.reference_id[:16] if metric.trace is not None else "—"
        val_str = _render_value(metric)
        period_str = _html_escape(metric.period or "—")
        ver_str = _html_escape(metric.factor_version or "—")
        conf_str = _html_escape(str(metric.confidence) if metric.confidence is not None else "—")
        parts.append(
            f"{_html_escape(metric.label)}={val_str}[p={period_str},v={ver_str},c={conf_str},t={_html_escape(trace_prefix)}]"
        )
    return " · ".join(parts)


def _resolve_preset(card: ResearchCard, preset: str | CardPreset | None) -> CardPreset:
    if isinstance(preset, CardPreset):
        return preset
    key = preset or card.style_preset
    if key and key in CARD_PRESETS:
        return CARD_PRESETS[key]
    default_by_kind: dict[CardKind, str] = {
        CardKind.COMPANY: "study-notes",
        CardKind.COMPARISON: "minimal",
        CardKind.RANKING: "screen-print",
        CardKind.ETF: "retro",
        CardKind.SUPPLY_CHAIN: "sketch-notes",
        CardKind.STRATEGY_SUMMARY: "bold",
    }
    return CARD_PRESETS[default_by_kind.get(card.card_kind, "study-notes")]


def _headline_for_card(card: ResearchCard) -> str:
    if len(card.title) <= 68 and "issuer:cik:" not in card.title and "issuer:lei:" not in card.title:
        return card.title
    lead_names = "、".join(s.display_name.split(" · ")[0] for s in card.subjects[:3])
    suffix = f" 等 {len(card.subjects)} 家" if len(card.subjects) > 3 else ""
    if card.card_kind is CardKind.RANKING:
        return f"估值折价排位：{lead_names}{suffix}"
    if card.card_kind is CardKind.COMPARISON:
        return f"多票同截面比价：{lead_names}{suffix}"
    if card.card_kind is CardKind.STRATEGY_SUMMARY:
        return f"策略过筛结论：{lead_names}{suffix}"
    return f"{card.card_kind.value.upper()}：{lead_names}{suffix}"


def _render_strategy_context_bar(ctx: StrategyContext | None) -> str:
    if ctx is None:
        return ""
    ceilings_str = " / ".join(f"{k}:{v}x" for k, v in ctx.tier_ceilings)
    return (
        '<div class="strategy-bar">'
        f"<span>候选池 <b>{ctx.total_universe_count}</b> 家</span>"
        f"<span>入选 <b>{ctx.selected_count}</b> 家 (Top {ctx.selection_count})</span>"
        f"<span>候补 <b>{ctx.ranked_beyond_selection_count}</b> 家</span>"
        f"<span>超上限淘汰 <b>{ctx.rejected_above_band_count}</b> 家</span>"
        f"<span>缺数据剔除 <b>{ctx.excluded_count}</b> 家</span>"
        f"<span>分档上限: {_html_escape(ceilings_str)}</span>"
        "</div>"
    )


def render_card_html(card: ResearchCard, *, preset: str | CardPreset | None = None) -> str:
    """Renders a self-contained 1080x1440 Cold-Verdict Chinese Buy-Side HTML card that satisfies
    `verify_card_html(card, html)` with zero vertical overflow (`scrollHeight <= 1440`).
    """
    theme = _resolve_preset(card, preset)
    visible_subjects = card.subjects[:MAX_SUBJECTS_PER_CARD]
    compact = len(visible_subjects) >= 3
    subjects_html = "".join(_render_subject(subject, compact=compact) for subject in visible_subjects)
    overflow_note = (
        f'<div class="series-note">本页展示前 {len(visible_subjects)} / 共 {len(card.subjects)} 家标的 · '
        "完整明细已写入底部溯源签名并与分页序列对齐</div>"
        if len(card.subjects) > len(visible_subjects)
        else ""
    )
    series_badge = (
        f" · SERIES {card.series_index}/{card.series_total}"
        if card.series_index is not None and card.series_total is not None
        else ""
    )
    cutoff_date = card.cutoff_at.date().isoformat()
    headline = _headline_for_card(card)
    evidence_lines = "".join(
        f'<div class="ev-line">{_subject_evidence_line(subject)}</div>' for subject in card.subjects
    )
    return (
        "<!DOCTYPE html>"
        '<html lang="zh-CN"><head><meta charset="utf-8">'
        f"<title>{_html_escape(card.title)}</title>"
        "<style>"
        "*{box-sizing:border-box;margin:0;padding:0;}"
        f"html,body{{width:{XHS_CARD_WIDTH_PX}px;height:{XHS_CARD_HEIGHT_PX}px;overflow:hidden;"
        f"background:{theme.bg_css};color:{theme.fg};font-family:'PingFang SC','Hiragino Sans GB',"
        "'Microsoft YaHei',-apple-system,BlinkMacSystemFont,sans-serif;-webkit-font-smoothing:antialiased;}"
        f".card-canvas{{width:{XHS_CARD_WIDTH_PX}px;height:{XHS_CARD_HEIGHT_PX}px;padding:36px 42px 24px;"
        "display:flex;flex-direction:column;justify-content:space-between;overflow:hidden;"
        f"border:3px solid {theme.border};background:{theme.bg_css};}}"
        f".card-header{{border-bottom:2px solid {theme.border};padding-bottom:14px;margin-bottom:14px;}}"
        ".eyebrow{display:flex;justify-content:space-between;align-items:center;font-size:13px;"
        f"font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:{theme.mute};margin-bottom:8px;font-weight:700;}}"
        f".preset-tag{{background:{theme.accent};color:#FFFFFF;padding:3px 10px;border-radius:999px;font-size:11px;}}"
        "h1{font-size:32px;line-height:1.18;margin:0 0 8px;font-weight:900;letter-spacing:-0.02em;}"
        ".meta-strip{display:flex;flex-wrap:wrap;gap:12px;font-size:12px;"
        f"font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:{theme.mute};}}"
        f".strategy-bar{{display:flex;flex-wrap:wrap;gap:10px;background:{theme.chip_bg};"
        f"border:1.5px solid {theme.border};border-radius:10px;padding:8px 12px;margin-bottom:12px;font-size:12px;}}"
        f".subject-block{{background:{theme.surface_bg};border:2px solid {theme.border};border-radius:14px;"
        "padding:16px 18px;margin-bottom:12px;}"
        ".subject-block.compact{padding:11px 14px;margin-bottom:9px;}"
        ".subject-header{display:flex;justify-content:space-between;align-items:center;"
        f"gap:10px;border-bottom:1px solid {theme.border};padding-bottom:8px;margin-bottom:10px;}}"
        ".subject-title-group{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;}"
        "h2{font-size:20px;margin:0;font-weight:800;}"
        ".subject-block.compact h2{font-size:17px;}"
        f".subject-id{{font-size:11px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:{theme.mute};}}"
        f".rank-badge{{background:{theme.accent};color:#FFFFFF;font-weight:800;padding:2px 8px;border-radius:6px;font-size:13px;}}"
        ".subject-badges{display:flex;gap:6px;flex-wrap:wrap;}"
        ".pill{font-size:10.5px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;"
        f"padding:2px 7px;border-radius:999px;border:1px solid {theme.border};background:{theme.chip_bg};font-weight:700;}}"
        ".causal-banner{background:#FEF3C7;border:1px solid #D97706;color:#92400E;"
        "padding:6px 10px;border-radius:8px;font-size:12px;font-weight:700;margin-bottom:10px;}"
        ".metrics-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;}"
        ".metrics-grid.compact{grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;}"
        f".metric-tile{{background:{theme.chip_bg};border:1px solid {theme.border};border-radius:9px;padding:10px 12px;}}"
        ".metric-tile.compact{padding:7px 9px;}"
        ".metric-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;gap:4px;}"
        f".metric-label-zh{{font-size:11.5px;font-weight:700;color:{theme.mute};}}"
        ".metric-value{font-size:20px;font-weight:900;font-variant-numeric:tabular-nums;"
        f"font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:{theme.fg};margin:2px 0 4px;}}"
        ".metric-tile.compact .metric-value{font-size:16px;margin:2px 0 0;}"
        f".metric-meta{{display:flex;justify-content:space-between;font-size:10.5px;color:{theme.mute};"
        "font-family:ui-monospace,SFMono-Regular,Menlo,monospace;}"
        f".empty-tile{{grid-column:1/-1;padding:12px;text-align:center;color:{theme.mute};font-size:13px;"
        f"background:{theme.chip_bg};border:1px dashed {theme.border};border-radius:8px;}}"
        f".series-note{{font-size:11.5px;color:{theme.mute};text-align:center;padding:4px 0;font-weight:600;}}"
        f".card-footer{{border-top:2px solid {theme.border};padding-top:10px;margin-top:auto;"
        f"display:flex;flex-direction:column;gap:3px;font-size:10.5px;color:{theme.mute};}}"
        f".ev-box{{background:{theme.chip_bg};border:1px solid {theme.border};border-radius:6px;"
        "padding:4px 7px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:7.5px;"
        "line-height:1.22;max-height:145px;overflow:hidden;word-break:break-all;}"
        "</style></head><body>"
        '<div class="card-canvas">'
        "<div>"
        '<header class="card-header">'
        '<div class="eyebrow">'
        f"<span>TRUEALPHA 买方研报卡 · {_html_escape(_CARD_KIND_ZH[card.card_kind])}{series_badge}</span>"
        f'<span class="preset-tag">{_html_escape(theme.style.value.upper())} × {_html_escape(theme.layout.value.upper())} · {XHS_CARD_WIDTH_PX}x{XHS_CARD_HEIGHT_PX}</span>'
        "</div>"
        f"<h1>{_html_escape(headline)}</h1>"
        '<div class="meta-strip">'
        f"<span>数据截面: {cutoff_date} ({card.cutoff_at.isoformat()})</span>"
        f"<span>Schema: {card.schema_version}</span>"
        f"<span>Template: {card.template_version}</span>"
        f"<span>Prompt: {XHS_CARD_PROMPT_VERSION}</span>"
        "</div>"
        "</header>"
        f"{_render_strategy_context_bar(card.strategy_context)}"
        f"{subjects_html}"
        f"{overflow_note}"
        "</div>"
        '<footer class="card-footer">'
        f"<div><strong>风险提示:</strong> 本卡仅反映单一治理截面下的版本化策略输出，不构成投资建议。 ({_html_escape(card.research_risk_note)})</div>"
        f"<div><strong>数据溯源:</strong> 全部数字直接取自 TrueAlpha 已物化因子表 mart。 ({_html_escape(card.source_attribution)})</div>"
        f"<div>Card ID: {_html_escape(card.card_id)} · Report ID: {_html_escape(card.generated_from_report_id)}</div>"
        f'<div class="ev-box">{evidence_lines}</div>'
        "</footer>"
        "</div>"
        "</body></html>"
    )
