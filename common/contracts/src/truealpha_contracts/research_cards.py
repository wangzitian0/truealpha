"""Versioned card schemas and deterministic renderer from `ResearchReport` (#372, #44)."""

from __future__ import annotations

import html as html_lib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import Field, SerializerFunctionWrapHandler, model_serializer, model_validator

from truealpha_contracts.common import StrictFrozenModel, canonical_sha256, drop_none_fields, require_aware
from truealpha_contracts.execution import AvailabilityStatus, FactorValidationStatus
from truealpha_contracts.research_report import ReportSection, ReportSectionKind, ResearchReport, ResultValue

CARD_SCHEMA_VERSION: Literal["research_card.v1"] = "research_card.v1"
_CARD_ID_PREFIX = "card:"
MAX_SUBJECTS_PER_CARD: Final[int] = 4
XHS_CARD_WIDTH_PX = 1080
XHS_CARD_HEIGHT_PX = 1440
TEMPLATE_VERSION: Literal["card_template.v1"] = "card_template.v1"


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


class CardPreset(StrictFrozenModel):
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


_PRESET_ROWS = (
    (
        "bold",
        "sparse",
        "neon",
        "radial-gradient(circle at 85% 12%, #311042 0%, #140C1C 55%, #0B0711 100%)",
        "#191126",
        "#F5F5F7",
        "#B8B8D4",
        "#39FF14",
        "#00F5FF",
        "#120B1D",
    ),
    ("screen-print", "list", "earth", "#F5E6D0", "#FFFDF9", "#121212", "#4A3F35", "#E8751A", "#121212", "#EDE0CC"),
    ("notion", "list", "macaron", "#FFFFFF", "#FBFBFA", "#37352F", "#6B6860", "#2F7D32", "#E3E2DE", "#F7F6F3"),
    ("minimal", "comparison", "cool", "#FAFAFA", "#FFFFFF", "#111111", "#555555", "#0A6847", "#111111", "#F2F2F2"),
    ("study-notes", "dense", "warm", "#FAF6EE", "#FFFFFF", "#222222", "#5C5852", "#1E3A5F", "#1E3A5F", "#F3EDE0"),
    ("retro", "balanced", "warm", "#F2E6D0", "#FAF3E6", "#2A1F18", "#634F40", "#2E6B4E", "#2A1F18", "#E8D8BC"),
    (
        "chalkboard",
        "quadrant",
        "monochrome",
        "#1E3226",
        "#263D2F",
        "#F2EFE9",
        "#B8C7BC",
        "#FFE066",
        "#5E7A68",
        "#18281E",
    ),
    ("pop", "quadrant", "vivid", "#FFE600", "#FFFFFF", "#000000", "#333333", "#FF0055", "#000000", "#FFF9B0"),
    ("sketch-notes", "flow", "warm", "#FCF9F2", "#FFFFFF", "#2B2927", "#5C5852", "#E85D04", "#2B2927", "#F4EFE6"),
    (
        "fresh",
        "mindmap",
        "pastel",
        "linear-gradient(160deg, #E8F8F5 0%, #F4FBF7 55%, #FFFFFF 100%)",
        "#FFFFFF",
        "#1B3B2B",
        "#4A6B5D",
        "#148F77",
        "#48C9B0",
        "#E8F6F3",
    ),
)

CARD_PRESETS: Final[dict[str, CardPreset]] = {
    pid: CardPreset(
        preset_id=pid,
        style=CardStyle(pid),
        layout=CardLayout(layout),
        palette=CardPalette(pal),
        bg_css=bg,
        surface_bg=sbg,
        fg=fg,
        mute=mute,
        accent=acc,
        border=bdr,
        chip_bg=chip,
    )
    for pid, layout, pal, bg, sbg, fg, mute, acc, bdr, chip in _PRESET_ROWS
}


class StrategyContext(StrictFrozenModel):
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

_CARD_SECTIONS: dict[CardKind, tuple[ReportSectionKind, ...]] = {
    CardKind.COMPANY: (ReportSectionKind.OPERATING_EFFICIENCY, ReportSectionKind.VALUATION),
    CardKind.COMPARISON: (ReportSectionKind.VALUATION,),
    CardKind.RANKING: (ReportSectionKind.VALUATION, ReportSectionKind.STRATEGY_SUMMARY, ReportSectionKind.RANKING),
    CardKind.ETF: (ReportSectionKind.ETF_VIRTUAL_COMPANY,),
    CardKind.SUPPLY_CHAIN: (ReportSectionKind.SUPPLY_CHAIN,),
    CardKind.STRATEGY_SUMMARY: (ReportSectionKind.STRATEGY_SUMMARY,),
}


class CardSubject(StrictFrozenModel):
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
        return drop_none_fields(handler(self), "ticker", "outcome", "exclusion_reason")


class ResearchCard(StrictFrozenModel):
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
        return drop_none_fields(
            handler(self), "strategy_context", "style_preset", "series_index", "series_total", "rendered_html"
        )

    @model_validator(mode="after")
    def _identify(self) -> ResearchCard:
        require_aware(self.cutoff_at, "cutoff_at")
        for s in self.subjects:
            if self.card_kind is CardKind.SUPPLY_CHAIN and s.causal_claim != "scenario_only":
                raise ValueError("a supply-chain card subject must carry the fixed scenario_only causal_claim")
            if self.card_kind is not CardKind.SUPPLY_CHAIN and s.causal_claim is not None:
                raise ValueError("only a supply-chain card may carry a causal_claim field")
        payload = self.model_dump(mode="json", exclude={"card_id", "rendered_html"})
        computed = f"{_CARD_ID_PREFIX}{canonical_sha256(payload)}"
        if self.card_id not in ("", computed):
            raise ValueError("card_id does not match card content")
        object.__setattr__(self, "card_id", computed)
        return self


_AVAILABILITY_RANK = {
    AvailabilityStatus.ERROR: 0,
    AvailabilityStatus.UNAVAILABLE: 1,
    AvailabilityStatus.EXCLUDED: 2,
    AvailabilityStatus.LOW_CONFIDENCE: 3,
    AvailabilityStatus.STALE: 4,
    AvailabilityStatus.AVAILABLE: 5,
}


def _section_status(
    sections: tuple[ReportSection, ...], wanted: tuple[ReportSectionKind, ...]
) -> tuple[AvailabilityStatus, FactorValidationStatus | None]:
    matched = [s for s in sections if s.section_kind in wanted]
    if not matched:
        return AvailabilityStatus.UNAVAILABLE, None
    worst_avail = min(matched, key=lambda s: _AVAILABILITY_RANK[s.availability]).availability
    statuses = [s.validation_status for s in matched if s.validation_status is not None]
    if not statuses:
        worst_val = None
    elif FactorValidationStatus.REJECTED in statuses:
        worst_val = FactorValidationStatus.REJECTED
    elif FactorValidationStatus.NOT_EVALUATED in statuses:
        worst_val = FactorValidationStatus.NOT_EVALUATED
    else:
        worst_val = FactorValidationStatus.ACCEPTED
    return worst_avail, worst_val


def build_card(report: ResearchReport, card_kind: CardKind, *, title: str | None = None) -> ResearchCard:
    wanted = _CARD_SECTIONS[card_kind]
    causal: Literal["scenario_only"] | None = "scenario_only" if card_kind is CardKind.SUPPLY_CHAIN else None
    subjects = []
    for sub in report.subjects:
        avail, val_status = _section_status(sub.sections, wanted)
        metrics = tuple(r for sec in sub.sections if sec.section_kind in wanted for r in sec.results)
        subjects.append(
            CardSubject(
                subject_id=sub.subject_id,
                display_name=sub.display_name,
                rank=sub.rank,
                availability=avail,
                claim_class=_claim_class(val_status),
                metrics=metrics,
                causal_claim=causal,
            )
        )
    default_title = f"{card_kind.value} card: {', '.join(s.display_name for s in report.subjects)}"
    return ResearchCard(
        card_kind=card_kind,
        title=title if title is not None else default_title,
        cutoff_at=report.cutoff_at,
        generated_from_report_id=report.report_id,
        research_risk_note=_RESEARCH_RISK_NOTE[card_kind],
        source_attribution=_SOURCE_ATTRIBUTION,
        subjects=tuple(subjects),
    )


_DEFAULT_TIER_CEILINGS: Final[tuple[tuple[str, str], ...]] = (
    ("large_model_native", "33.34"),
    ("tech", "12.86"),
    ("traditional", "8.75"),
)


def build_strategy_context_from_report(
    report: ResearchReport,
    *,
    strategy_id: str = "large_model_value_v0",
    definition_version: str = "1.1.0",
    selection_count: int = 2,
) -> StrategyContext:
    counts = {
        "selected": 0,
        "ranked_beyond_selection_count": 0,
        "rejected_valuation_above_tier_band": 0,
        "excluded": 0,
    }
    basis = "price_to_gross_profit"
    ceilings: dict[str, str] = dict(_DEFAULT_TIER_CEILINGS)
    wanted_labels = {"outcome", "tier", "target_price_to_gross_profit", "target_price_to_sales"}
    for sub in report.subjects:
        fields = {r.label: r.value for sec in sub.sections for r in sec.results if r.value and r.label in wanted_labels}
        outcome_val, tier_val = fields.get("outcome"), fields.get("tier")
        t_pgp, t_ps = fields.get("target_price_to_gross_profit"), fields.get("target_price_to_sales")
        if t_ps is not None and t_pgp is None:
            basis = "price_to_sales"
        if tier_val and (t_pgp or t_ps):
            ceilings[tier_val] = t_pgp or t_ps or ""
        if outcome_val in counts:
            counts[outcome_val] += 1
        elif sub.sections and all(
            s.availability in (AvailabilityStatus.EXCLUDED, AvailabilityStatus.LOW_CONFIDENCE) for s in sub.sections
        ):
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
    report_by_id = {s.subject_id: s for s in report.subjects}
    enriched: list[CardSubject] = []
    for sub in card.subjects:
        rep_sub = report_by_id.get(sub.subject_id)
        ticker = sub.ticker or (sub.display_name.split(" · ", 1)[0].strip() if " · " in sub.display_name else None)
        outcome, excl = sub.outcome, sub.exclusion_reason
        if rep_sub is not None:
            for sec in rep_sub.sections:
                if sec.section_kind is ReportSectionKind.STRATEGY_SUMMARY:
                    if excl is None and sec.reason_codes:
                        excl = sec.reason_codes[0]
                    for res in sec.results:
                        if res.label == "outcome" and res.value and outcome is None:
                            outcome = res.value
        if outcome == "excluded" and excl is None:
            excl = "missing_audited_gross_profit_or_coordinate_join"
        enriched.append(
            CardSubject(
                **{
                    **sub.model_dump(),
                    "ticker": ticker,
                    "outcome": outcome,
                    "exclusion_reason": excl,
                }
            )
        )
    base_data = {
        **card.model_dump(exclude={"card_id", "rendered_html"}),
        "subjects": tuple(enriched),
        "strategy_context": strategy_context if strategy_context is not None else card.strategy_context,
        "style_preset": style_preset if style_preset is not None else card.style_preset,
        "series_index": series_index if series_index is not None else card.series_index,
        "series_total": series_total if series_total is not None else card.series_total,
    }
    base = ResearchCard(**base_data)
    return (
        base
        if not include_rendered_html
        else ResearchCard(**base_data, rendered_html=render_card_html(base, preset=base.style_preset))
    )


def paginate_card_series(
    card: ResearchCard,
    *,
    max_subjects_per_card: int = MAX_SUBJECTS_PER_CARD,
    preset: str | None = None,
) -> tuple[ResearchCard, ...]:
    if max_subjects_per_card < 1:
        raise ValueError("max_subjects_per_card must be >= 1")
    total = len(card.subjects)
    if total <= max_subjects_per_card and preset is None:
        return (card,)
    chunks = [card.subjects[i : i + max_subjects_per_card] for i in range(0, total, max_subjects_per_card)]
    series_total = len(chunks)
    preset_keys = tuple(CARD_PRESETS.keys())
    base_fields = card.model_dump(
        exclude={"card_id", "title", "subjects", "style_preset", "series_index", "series_total", "rendered_html"}
    )
    return tuple(
        ResearchCard(
            **base_fields,
            title=(
                f"{card.card_kind.value} ({idx}/{series_total}): {', '.join(s.ticker or s.display_name.split(' · ')[0] for s in chunk)}"
                if series_total > 1
                else card.title
            ),
            subjects=chunk,
            style_preset=preset or card.style_preset or preset_keys[(idx - 1) % len(preset_keys)],
            series_index=idx,
            series_total=series_total,
        )
        for idx, chunk in enumerate(chunks, start=1)
    )


def render_card_json(card: ResearchCard) -> str:
    return json.dumps(card.model_dump(mode="json"), sort_keys=True, indent=2, ensure_ascii=True) + "\n"


XHS_CARD_PROMPT_VERSION: Literal["xhs-card-html:v1"] = "xhs-card-html:v1"
XHS_CARD_INSTRUCTIONS: str = (
    "You are TrueAlpha's Xiaohongshu (RED) visual editorial card designer.\n"
    "Generate a SINGLE self-contained HTML5 document (`<!DOCTYPE html><html>...</html>`) "
    "rendered at exact `1080px` width by `1440px` height (3:4 portrait aspect ratio).\n"
    "1. Fixed 1080x1440px container (`overflow: hidden; box-sizing: border-box;`), max 4 subjects per card.\n"
    "2. Direct Chinese buy-side copy in the main visual canvas; no generic AI filler.\n"
    "3. Inline CSS only. No `<script>` tags, event handlers, or external `http://`/`https://` URLs.\n"
    "4. Preserve verbatim every fact in `ResearchCard` JSON: cutoff date, `card_id` prefix, `research_risk_note`, "
    "`source_attribution`, subject `display_name`/`availability`/`claim_class`/`scenario_only`, and metric "
    "`label`/`value`/`currency`/`unit`/`period`/`factor_version`/`confidence`/`trace` prefix.\n"
    "5. Output ONLY raw HTML starting with `<!DOCTYPE html>` and ending with `</html>`."
)


def build_card_prompt(card: ResearchCard) -> str:
    payload = {
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
    card_json = json.dumps(payload, indent=2, ensure_ascii=False)
    return (
        f"{XHS_CARD_INSTRUCTIONS}\n\nPROMPT_VERSION: {XHS_CARD_PROMPT_VERSION}\n"
        f"REQUIRED_DIMENSIONS: {XHS_CARD_WIDTH_PX}x{XHS_CARD_HEIGHT_PX}\nINPUT_RESEARCH_CARD_JSON:\n{card_json}"
    )


def extract_html_document(raw_text: str) -> str:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    lower = cleaned.lower()
    doctype_idx = lower.find("<!doctype html")
    start_idx = doctype_idx if doctype_idx != -1 else lower.find("<html")
    end_idx = lower.rfind("</html>")
    return cleaned[start_idx : end_idx + len("</html>")] if start_idx != -1 and end_idx >= start_idx else cleaned


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_INLINE_EVENT_HANDLER_RE = re.compile(r"\bon[a-z]+\s*=", re.IGNORECASE)


def _esc(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _normalize_html_text(html_doc: str) -> str:
    unescaped = _HTML_TAG_RE.sub(" ", html_doc)
    for _ in range(3):
        next_val = html_lib.unescape(unescaped)
        if next_val == unescaped:
            break
        unescaped = next_val
    return _WHITESPACE_RE.sub(" ", _HTML_TAG_RE.sub(" ", unescaped))


def _contains_text(html_doc: str, raw: str, norm_doc: str) -> bool:
    if raw in html_doc or _esc(raw) in html_doc:
        return True
    return _WHITESPACE_RE.sub(" ", html_lib.unescape(raw)).strip() in norm_doc


def verify_card_html(card: ResearchCard, html: str) -> tuple[bool, tuple[str, ...]]:
    norm = _normalize_html_text(html)
    violations: list[str] = []
    lower = html.lower()
    if ("<!doctype html" not in lower and "<html" not in lower) or "</html>" not in lower:
        violations.append("missing_html_envelope")
    if str(XHS_CARD_WIDTH_PX) not in html or str(XHS_CARD_HEIGHT_PX) not in html:
        violations.append("missing_1080x1440_dimensions")
    if any(t in lower for t in ("<script", "javascript:")) or _INLINE_EVENT_HANDLER_RE.search(html):
        violations.append("forbidden_script_or_event_handler")
    if "http://" in lower or "https://" in lower:
        violations.append("forbidden_external_url")
    cutoff_date = card.cutoff_at.date().isoformat()
    if cutoff_date not in html and card.cutoff_at.isoformat() not in html:
        violations.append(f"missing_cutoff:{cutoff_date}")
    prefix = card.card_id[:12]
    if prefix not in html and prefix not in norm:
        violations.append(f"missing_card_id_prefix:{prefix}")
    if not _contains_text(html, card.research_risk_note, norm):
        violations.append("missing_research_risk_note")
    if not _contains_text(html, card.source_attribution, norm):
        violations.append("missing_source_attribution")
    for sub in card.subjects:
        if not _contains_text(html, sub.display_name, norm) and sub.subject_id not in html:
            violations.append(f"missing_subject:{sub.subject_id}")
        if sub.availability.value not in html and sub.availability.value not in norm:
            violations.append(f"missing_subject_availability:{sub.subject_id}:{sub.availability.value}")
        if sub.claim_class.value not in html and sub.claim_class.value not in norm:
            violations.append(f"missing_subject_claim_class:{sub.subject_id}:{sub.claim_class.value}")
        if sub.causal_claim == "scenario_only" and "scenario_only" not in html:
            violations.append(f"missing_scenario_only:{sub.subject_id}")
        for m in sub.metrics:
            pairs = (
                (m.label, "label"),
                (m.value, "value"),
                (m.currency if m.value is not None else None, "currency"),
                (m.unit if m.value is not None else None, "unit"),
                (m.period, "period"),
                (str(m.confidence) if m.confidence is not None else None, "confidence"),
                (m.trace.reference_id[:16] if m.trace is not None else None, "trace"),
            )
            for text, kind in pairs:
                if text is not None and not _contains_text(html, text, norm):
                    violations.append(
                        f"missing_metric_{kind}:{sub.subject_id}:{m.label}{f':{text}' if kind != 'label' else ''}"
                    )
    return (len(violations) == 0, tuple(violations))


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
_TIER_LABELS_ZH = {
    "large_model_native": "大模型原生档 (上限 33.34x)",
    "tech": "科技档 (上限 12.86x)",
    "traditional": "传统档 (上限 8.75x)",
}
_OUTCOME_LABELS_ZH = {
    "selected": "入选 (Top 2)",
    "ranked_beyond_selection_count": "有折价 · 排第 2 名后候补",
    "rejected_valuation_above_tier_band": "淘汰：估值超出本档上限",
    "excluded": "剔除：缺审定毛利或坐标",
}
_CARD_KIND_ZH = {
    CardKind.COMPANY: "单票深度拆解 · COMPANY",
    CardKind.COMPARISON: "多票横向对比 · COMPARISON",
    CardKind.RANKING: "策略估值折价榜 · RANKING",
    CardKind.ETF: "ETF 穿透虚拟公司 · ETF",
    CardKind.SUPPLY_CHAIN: "供应链情景推演 · SUPPLY CHAIN",
    CardKind.STRATEGY_SUMMARY: "策略过筛漏斗 · STRATEGY",
}
_DEFAULT_PRESET_BY_KIND = {
    CardKind.COMPANY: "study-notes",
    CardKind.COMPARISON: "minimal",
    CardKind.RANKING: "screen-print",
    CardKind.ETF: "retro",
    CardKind.SUPPLY_CHAIN: "sketch-notes",
    CardKind.STRATEGY_SUMMARY: "bold",
}


def _render_value(r: ResultValue) -> str:
    if r.value is None:
        return "—"
    if r.currency is not None and r.unit is not None:
        return _esc(f"{r.value} {r.currency} ({r.unit})")
    if r.currency is not None or r.unit is not None:
        return _esc(f"{r.value} {r.currency or r.unit}")
    return _esc(r.value)


def _render_metric_card(r: ResultValue, *, compact: bool = False) -> str:
    zh = _esc(_METRIC_LABELS_ZH.get(r.label, r.label))
    val = (
        "—"
        if r.value is None
        else (
            _esc(_TIER_LABELS_ZH.get(r.value, r.value))
            if r.label == "tier"
            else (_esc(_OUTCOME_LABELS_ZH.get(r.value, r.value)) if r.label == "outcome" else _render_value(r))
        )
    )
    if compact:
        return f'<div class="metric-tile compact"><div class="metric-label-zh">{zh}</div><div class="metric-value">{val}</div></div>'
    conf = _esc("—" if r.confidence is None else str(r.confidence))
    av = r.availability.value
    return (
        f'<div class="metric-tile"><div class="metric-top"><span class="metric-label-zh">{zh}</span>'
        f'<span class="pill availability-{av}">{av}</span></div><div class="metric-value">{val}</div>'
        f'<div class="metric-meta"><span>截面: {_esc(r.period or "—")}</span><span>置信度: {conf}</span></div></div>'
    )


def _render_subject(sub: CardSubject, *, compact: bool = False) -> str:
    rank_html = f'<span class="rank-badge">#{sub.rank}</span>' if sub.rank is not None else ""
    causal = (
        '<div class="causal-banner">scenario_only · 仅做图谱情景推演，不构成因果结论。</div>'
        if sub.causal_claim == "scenario_only"
        else ""
    )
    badge = (
        f'<span class="pill outcome-pill">{_esc(_OUTCOME_LABELS_ZH.get(sub.outcome, sub.outcome))}</span>'
        if sub.outcome
        else (
            f'<span class="pill outcome-pill">剔除: {_esc(sub.exclusion_reason)}</span>' if sub.exclusion_reason else ""
        )
    )
    tiles = "".join(_render_metric_card(r, compact=compact) for r in sub.metrics[: (4 if compact else 6)]) or (
        '<div class="empty-tile">当前治理截面暂无已物化因子数据 (unavailable)</div>'
    )
    cls_sfx = " compact" if compact else ""
    return (
        f'<section class="subject-block{cls_sfx}"><div class="subject-header">'
        f'<div class="subject-title-group">{rank_html}<h2>{_esc(sub.display_name)}</h2>'
        f'<span class="subject-id">{_esc(sub.subject_id)}</span></div>'
        f'<div class="subject-badges">{badge}'
        f'<span class="pill claim-class-{sub.claim_class.value}">{sub.claim_class.value}</span>'
        f'<span class="pill availability-{sub.availability.value}">{sub.availability.value}</span>'
        f"</div></div>{causal}<div class='metrics-grid{cls_sfx}'>{tiles}</div></section>"
    )


def _subject_evidence_line(sub: CardSubject) -> str:
    parts = [
        f"id={_esc(sub.subject_id)}",
        f"name={_esc(sub.display_name)}",
        f"avail={_esc(sub.availability.value)}",
        f"claim={_esc(sub.claim_class.value)}",
    ]
    if sub.causal_claim == "scenario_only":
        parts.append("causal=scenario_only")
    for m in sub.metrics:
        tr = m.trace.reference_id[:16] if m.trace is not None else "—"
        parts.append(
            f"{_esc(m.label)}={_render_value(m)}[p={_esc(m.period or '—')},v={_esc(m.factor_version or '—')},"
            f"c={_esc(str(m.confidence) if m.confidence is not None else '—')},t={_esc(tr)}]"
        )
    return " · ".join(parts)


def _card_stylesheet(t: CardPreset) -> str:
    mono = "ui-monospace,SFMono-Regular,Menlo,monospace"
    w, h = XHS_CARD_WIDTH_PX, XHS_CARD_HEIGHT_PX
    return (
        f"*{{box-sizing:border-box;margin:0;padding:0;}}html,body{{width:{w}px;height:{h}px;overflow:hidden;background:{t.bg_css};color:{t.fg};font-family:'PingFang SC','Microsoft YaHei',sans-serif;}}"
        f".card-canvas{{width:{w}px;height:{h}px;padding:36px 42px 24px;display:flex;flex-direction:column;justify-content:space-between;overflow:hidden;border:3px solid {t.border};background:{t.bg_css};}}"
        f".card-header{{border-bottom:2px solid {t.border};padding-bottom:14px;margin-bottom:14px;}}.eyebrow{{display:flex;justify-content:space-between;align-items:center;font-size:13px;font-family:{mono};color:{t.mute};margin-bottom:8px;font-weight:700;}}"
        f".preset-tag{{background:{t.accent};color:#FFF;padding:3px 10px;border-radius:999px;font-size:11px;}}h1{{font-size:32px;line-height:1.18;margin:0 0 8px;font-weight:900;}}"
        f".meta-strip{{display:flex;flex-wrap:wrap;gap:12px;font-size:12px;font-family:{mono};color:{t.mute};}}.strategy-bar{{display:flex;flex-wrap:wrap;gap:10px;background:{t.chip_bg};border:1.5px solid {t.border};border-radius:10px;padding:8px 12px;margin-bottom:12px;font-size:12px;}}"
        f".subject-block{{background:{t.surface_bg};border:2px solid {t.border};border-radius:14px;padding:16px 18px;margin-bottom:12px;}}.subject-block.compact{{padding:11px 14px;margin-bottom:9px;}}"
        f".subject-header{{display:flex;justify-content:space-between;align-items:center;gap:10px;border-bottom:1px solid {t.border};padding-bottom:8px;margin-bottom:10px;}}.subject-title-group{{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;}}"
        f"h2{{font-size:20px;margin:0;font-weight:800;}}.subject-block.compact h2{{font-size:17px;}}.subject-id{{font-size:11px;font-family:{mono};color:{t.mute};}}"
        f".rank-badge{{background:{t.accent};color:#FFF;font-weight:800;padding:2px 8px;border-radius:6px;font-size:13px;}}.subject-badges{{display:flex;gap:6px;flex-wrap:wrap;}}"
        f".pill{{font-size:10.5px;font-family:{mono};padding:2px 7px;border-radius:999px;border:1px solid {t.border};background:{t.chip_bg};font-weight:700;}}"
        ".causal-banner{background:#FEF3C7;border:1px solid #D97706;color:#92400E;padding:6px 10px;border-radius:8px;font-size:12px;font-weight:700;margin-bottom:10px;}"
        ".metrics-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;}.metrics-grid.compact{grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;}"
        f".metric-tile{{background:{t.chip_bg};border:1px solid {t.border};border-radius:9px;padding:10px 12px;}}.metric-tile.compact{{padding:7px 9px;}}.metric-top{{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;gap:4px;}}"
        f".metric-label-zh{{font-size:11.5px;font-weight:700;color:{t.mute};}}.metric-value{{font-size:20px;font-weight:900;font-family:{mono};color:{t.fg};margin:2px 0 4px;}}.metric-tile.compact .metric-value{{font-size:16px;margin:2px 0 0;}}"
        f".metric-meta{{display:flex;justify-content:space-between;font-size:10.5px;color:{t.mute};font-family:{mono};}}.empty-tile{{grid-column:1/-1;padding:12px;text-align:center;color:{t.mute};font-size:13px;background:{t.chip_bg};border:1px dashed {t.border};border-radius:8px;}}"
        f".series-note{{font-size:11.5px;color:{t.mute};text-align:center;padding:4px 0;font-weight:600;}}.card-footer{{border-top:2px solid {t.border};padding-top:10px;margin-top:auto;display:flex;flex-direction:column;gap:3px;font-size:10.5px;color:{t.mute};}}"
        f".ev-box{{background:{t.chip_bg};border:1px solid {t.border};border-radius:6px;padding:4px 7px;font-family:{mono};font-size:7.5px;line-height:1.22;max-height:145px;overflow:hidden;word-break:break-all;}}"
    )


def render_card_html(card: ResearchCard, *, preset: str | CardPreset | None = None) -> str:
    theme = (
        preset
        if isinstance(preset, CardPreset)
        else CARD_PRESETS.get(preset or card.style_preset or "")
        or CARD_PRESETS[_DEFAULT_PRESET_BY_KIND.get(card.card_kind, "study-notes")]
    )
    visible = card.subjects[:MAX_SUBJECTS_PER_CARD]
    compact = len(visible) >= 3
    subjects_html = "".join(_render_subject(s, compact=compact) for s in visible)
    overflow_note = (
        f'<div class="series-note">本页展示前 {len(visible)} / 共 {len(card.subjects)} 家标的 · 完整明细已写入底部溯源签名并与分页序列对齐</div>'
        if len(card.subjects) > len(visible)
        else ""
    )
    series_badge = (
        f" · SERIES {card.series_index}/{card.series_total}"
        if card.series_index is not None and card.series_total is not None
        else ""
    )
    headline = (
        card.title
        if len(card.title) <= 68 and "issuer:cik:" not in card.title and "issuer:lei:" not in card.title
        else (
            f"{ ({CardKind.RANKING: '估值折价排位', CardKind.COMPARISON: '多票同截面比价', CardKind.STRATEGY_SUMMARY: '策略过筛结论'}.get(card.card_kind, card.card_kind.value.upper())) }："
            f"{'、'.join(s.display_name.split(' · ')[0] for s in card.subjects[:3])}"
            f"{f' 等 {len(card.subjects)} 家' if len(card.subjects) > 3 else ''}"
        )
    )
    ctx = card.strategy_context
    ctx_bar = (
        ""
        if ctx is None
        else (
            f'<div class="strategy-bar"><span>候选池 <b>{ctx.total_universe_count}</b> 家</span>'
            f"<span>入选 <b>{ctx.selected_count}</b> 家 (Top {ctx.selection_count})</span>"
            f"<span>候补 <b>{ctx.ranked_beyond_selection_count}</b> 家</span>"
            f"<span>超上限淘汰 <b>{ctx.rejected_above_band_count}</b> 家</span>"
            f"<span>缺数据剔除 <b>{ctx.excluded_count}</b> 家</span>"
            f"<span>分档上限: {_esc(' / '.join(f'{k}:{v}x' for k, v in ctx.tier_ceilings))}</span></div>"
        )
    )
    cutoff_date = card.cutoff_at.date().isoformat()
    ev_lines = "".join(f'<div class="ev-line">{_subject_evidence_line(s)}</div>' for s in card.subjects)
    return (
        f'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>{_esc(card.title)}</title>'
        f'<style>{_card_stylesheet(theme)}</style></head><body><div class="card-canvas"><div>'
        '<header class="card-header"><div class="eyebrow">'
        f"<span>TRUEALPHA 买方研报卡 · {_esc(_CARD_KIND_ZH[card.card_kind])}{series_badge}</span>"
        f'<span class="preset-tag">{_esc(theme.style.value.upper())} × {_esc(theme.layout.value.upper())} · {XHS_CARD_WIDTH_PX}x{XHS_CARD_HEIGHT_PX}</span>'
        f'</div><h1>{_esc(headline)}</h1><div class="meta-strip">'
        f"<span>数据截面: {cutoff_date} ({card.cutoff_at.isoformat()})</span>"
        f"<span>Schema: {card.schema_version}</span><span>Template: {card.template_version}</span>"
        f"<span>Prompt: {XHS_CARD_PROMPT_VERSION}</span></div></header>"
        f"{ctx_bar}{subjects_html}{overflow_note}</div>"
        '<footer class="card-footer">'
        f"<div><strong>风险提示:</strong> 本卡仅反映单一治理截面下的版本化策略输出，不构成投资建议。 ({_esc(card.research_risk_note)})</div>"
        f"<div><strong>数据溯源:</strong> 全部数字直接取自 TrueAlpha 已物化因子表 mart。 ({_esc(card.source_attribution)})</div>"
        f"<div>Card ID: {_esc(card.card_id)} · Report ID: {_esc(card.generated_from_report_id)}</div>"
        f'<div class="ev-box">{ev_lines}</div></footer></div></body></html>'
    )
