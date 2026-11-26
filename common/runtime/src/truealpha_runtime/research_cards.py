"""Deterministic card builder, HTML renderer, and prompt/HTML verifier (#372, #44)."""

from __future__ import annotations

import html as html_lib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import Field, SerializerFunctionWrapHandler, model_serializer, model_validator
from truealpha_contracts.common import (
    StrictFrozenModel,
    canonical_sha256,
    drop_none_fields,
    load_data_json,
    require_aware,
)
from truealpha_contracts.execution import AvailabilityStatus, FactorValidationStatus

from truealpha_runtime.research_report import ReportSection, ReportSectionKind, ResearchReport, ResultValue

CARD_SCHEMA_VERSION: Literal["research_card.v1"] = "research_card.v1"
MAX_SUBJECTS_PER_CARD: Final[int] = 4
XHS_CARD_WIDTH_PX, XHS_CARD_HEIGHT_PX = 1080, 1440
TEMPLATE_VERSION: Literal["card_template.v1"] = "card_template.v1"
XHS_CARD_PROMPT_VERSION: Literal["xhs-card-html:v1"] = "xhs-card-html:v1"
_DATA = load_data_json("research_cards.v1.json")
XHS_CARD_INSTRUCTIONS: str = _DATA["instructions"]
_NE = Field(min_length=1)


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


class CardPreset(StrictFrozenModel):
    preset_id: str = _NE
    style: str = _NE
    layout: str = _NE
    palette: str = _NE
    bg_css: str = _NE
    surface_bg: str = _NE
    fg: str = _NE
    mute: str = _NE
    accent: str = _NE
    border: str = _NE
    chip_bg: str = _NE


def _hex_rgb(color: str) -> tuple[float, float, float]:
    raw = color.strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    if len(raw) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in raw):
        raise ValueError(f"expected #RGB or #RRGGBB hex color, got {color!r}")
    return (int(raw[0:2], 16) / 255.0, int(raw[2:4], 16) / 255.0, int(raw[4:6], 16) / 255.0)


def _relative_luminance(color: str) -> float:
    r, g, b = (c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in _hex_rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(hex_a: str, hex_b: str) -> float:
    """Return WCAG 2.1 contrast ratio (`1.0`..`21.0`) between two hex colors."""
    la, lb = _relative_luminance(hex_a), _relative_luminance(hex_b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


_ON_ACCENT_CANDIDATES: Final[tuple[str, str]] = ("#FFFFFF", "#111111")


def on_accent_color(preset: CardPreset) -> str:
    """Return the candidate text color with the higher contrast over `accent`."""
    return max(_ON_ACCENT_CANDIDATES, key=lambda c: contrast_ratio(c, preset.accent))


def verify_preset_contrast(preset: CardPreset) -> tuple[bool, tuple[str, ...]]:
    """Enforce WCAG AA minimums on each text and background pair that the card CSS template uses."""
    pairs = (
        ("fg", preset.fg, preset.surface_bg, 4.5),
        ("mute", preset.mute, preset.surface_bg, 3.0),
        ("fg_on_chip", preset.fg, preset.chip_bg, 4.5),
        ("mute_on_chip", preset.mute, preset.chip_bg, 3.0),
        ("on_accent", on_accent_color(preset), preset.accent, 4.5),
    )
    violations = [
        f"low_{name}_contrast:{preset.preset_id}:{ratio:.2f}"
        for name, text, background, minimum in pairs
        if (ratio := contrast_ratio(text, background)) < minimum
    ]
    return (not violations, tuple(violations))


CARD_PRESETS: Final[dict[str, CardPreset]] = {
    r[0]: CardPreset(
        preset_id=r[0],
        style=r[0],
        layout=r[1],
        palette=r[2],
        bg_css=r[3],
        surface_bg=r[4],
        fg=r[5],
        mute=r[6],
        accent=r[7],
        border=r[8],
        chip_bg=r[9],
    )
    for r in _DATA["presets"]
}


class StrategyContext(StrictFrozenModel):
    strategy_id: str = _NE
    definition_version: str = _NE
    valuation_basis: str = _NE
    selection_count: int = Field(ge=1)
    total_universe_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    ranked_beyond_selection_count: int = Field(ge=0)
    rejected_above_band_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    tier_ceilings: tuple[tuple[str, str], ...] = ()


class CardSubject(StrictFrozenModel):
    subject_id: str = _NE
    display_name: str = _NE
    rank: int | None = Field(default=None, ge=1)
    availability: AvailabilityStatus
    claim_class: ClaimClass
    metrics: tuple[ResultValue, ...]
    causal_claim: Literal["scenario_only"] | None = None
    ticker: str | None = None
    outcome: str | None = None
    exclusion_reason: str | None = None

    @model_serializer(mode="wrap")
    def _omit(self, h: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return drop_none_fields(h(self), "ticker", "outcome", "exclusion_reason")


class ResearchCard(StrictFrozenModel):
    schema_version: Literal["research_card.v1"] = CARD_SCHEMA_VERSION
    card_id: str = ""
    card_kind: CardKind
    template_version: Literal["card_template.v1"] = TEMPLATE_VERSION
    title: str = _NE
    cutoff_at: datetime
    generated_from_report_id: str = _NE
    research_risk_note: str = _NE
    source_attribution: str = _NE
    subjects: tuple[CardSubject, ...] = Field(min_length=1)
    strategy_context: StrategyContext | None = None
    style_preset: str | None = None
    series_index: int | None = Field(default=None, ge=1)
    series_total: int | None = Field(default=None, ge=1)
    rendered_html: str | None = None
    document_id: str | None = None

    @model_serializer(mode="wrap")
    def _omit(self, h: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return drop_none_fields(
            h(self), "strategy_context", "style_preset", "series_index", "series_total", "rendered_html", "document_id"
        )

    @model_validator(mode="after")
    def _identify(self) -> ResearchCard:
        require_aware(self.cutoff_at, "cutoff_at")
        for s in self.subjects:
            if (self.card_kind is CardKind.SUPPLY_CHAIN) != (s.causal_claim == "scenario_only") and not (
                self.card_kind is not CardKind.SUPPLY_CHAIN and s.causal_claim is None
            ):
                raise ValueError("causal_claim invalid for card_kind")
        computed = f"card:{canonical_sha256(self.model_dump(mode='json', exclude={'card_id', 'rendered_html', 'document_id'}))}"
        if self.card_id not in ("", computed):
            raise ValueError("card_id does not match card content")
        object.__setattr__(self, "card_id", computed)
        return self


_H = _DATA["html"]
_CARD_SECTIONS = {CardKind(k): tuple(ReportSectionKind(s) for s in v) for k, v in _DATA["sections"].items()}
_RISK_NOTE = {CardKind(k): v for k, v in _DATA["risk_note"].items()}
_DEFAULT_PRESET = {CardKind(k): v for k, v in _DATA["default_preset_by_kind"].items()}
_CARD_KIND_ZH = {CardKind(k): v for k, v in _DATA["card_kind_zh"].items()}
_AVAIL_RANK = {
    AvailabilityStatus.ERROR: 0,
    AvailabilityStatus.UNAVAILABLE: 1,
    AvailabilityStatus.EXCLUDED: 2,
    AvailabilityStatus.LOW_CONFIDENCE: 3,
    AvailabilityStatus.STALE: 4,
    AvailabilityStatus.AVAILABLE: 5,
    AvailabilityStatus.NOT_APPLICABLE: 6,
}
_CLAIM_BY_VAL = {
    FactorValidationStatus.ACCEPTED: ClaimClass.EMPIRICALLY_VALIDATED,
    FactorValidationStatus.REJECTED: ClaimClass.REJECTED_DO_NOT_USE,
    FactorValidationStatus.NOT_EVALUATED: ClaimClass.RESEARCH_HYPOTHESIS,
}


def _claim_class(vs: FactorValidationStatus | None) -> ClaimClass:
    return _CLAIM_BY_VAL.get(vs, ClaimClass.NOT_APPLICABLE) if vs else ClaimClass.NOT_APPLICABLE


def _section_status(
    sections: tuple[ReportSection, ...], wanted: tuple[ReportSectionKind, ...]
) -> tuple[AvailabilityStatus, FactorValidationStatus | None]:
    matched = [s for s in sections if s.section_kind in wanted]
    if not matched:
        return AvailabilityStatus.UNAVAILABLE, None
    avail = min(matched, key=lambda s: _AVAIL_RANK[s.availability]).availability
    st = [s.validation_status for s in matched if s.validation_status is not None]
    val = next(
        (
            v
            for v in (
                FactorValidationStatus.REJECTED,
                FactorValidationStatus.NOT_EVALUATED,
                FactorValidationStatus.ACCEPTED,
            )
            if v in st
        ),
        None,
    )
    return avail, val


def build_card(report: ResearchReport, card_kind: CardKind, *, title: str | None = None) -> ResearchCard:
    wanted = _CARD_SECTIONS[card_kind]
    causal: Literal["scenario_only"] | None = "scenario_only" if card_kind is CardKind.SUPPLY_CHAIN else None
    subjects = tuple(
        CardSubject(
            subject_id=sub.subject_id,
            display_name=sub.display_name,
            rank=sub.rank,
            availability=(st := _section_status(sub.sections, wanted))[0],
            claim_class=_claim_class(st[1]),
            metrics=tuple(r for sec in sub.sections if sec.section_kind in wanted for r in sec.results),
            causal_claim=causal,
        )
        for sub in report.subjects
    )
    return ResearchCard(
        card_kind=card_kind,
        title=title
        if title is not None
        else f"{card_kind.value} card: {', '.join(s.display_name for s in report.subjects)}",
        cutoff_at=report.cutoff_at,
        generated_from_report_id=report.report_id,
        research_risk_note=_RISK_NOTE[card_kind],
        source_attribution=_DATA["source_attribution"],
        subjects=subjects,
    )


def build_strategy_context_from_report(
    report: ResearchReport,
    *,
    strategy_id: str = "large_model_value_v0",
    definition_version: str = "1.1.0",
    selection_count: int = 2,
) -> StrategyContext:
    counts = dict.fromkeys(
        ("selected", "ranked_beyond_selection_count", "rejected_valuation_above_tier_band", "excluded"), 0
    )
    basis, ceilings = "price_to_gross_profit", {k: v for k, v in _DATA["default_tier_ceilings"]}
    for sub in report.subjects:
        f = {
            r.label: r.value
            for sec in sub.sections
            for r in sec.results
            if r.value and r.label in {"outcome", "tier", "target_price_to_gross_profit", "target_price_to_sales"}
        }
        if f.get("target_price_to_sales") and not f.get("target_price_to_gross_profit"):
            basis = "price_to_sales"
        if f.get("tier") and (v := f.get("target_price_to_gross_profit") or f.get("target_price_to_sales")):
            ceilings[f["tier"]] = v
        if f.get("outcome") in counts:
            counts[f["outcome"]] += 1
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
    by_id, enriched = {s.subject_id: s for s in report.subjects}, []
    for sub in card.subjects:
        rep_sub = by_id.get(sub.subject_id)
        ticker = sub.ticker or (sub.display_name.split(" · ", 1)[0].strip() if " · " in sub.display_name else None)
        out, excl = sub.outcome, sub.exclusion_reason
        if rep_sub is not None:
            for sec in (s for s in rep_sub.sections if s.section_kind is ReportSectionKind.STRATEGY_SUMMARY):
                excl = sec.reason_codes[0] if excl is None and sec.reason_codes else excl
                out = (
                    next((r.value for r in sec.results if r.label == "outcome" and r.value), out)
                    if out is None
                    else out
                )
        enriched.append(
            CardSubject(
                **{
                    **sub.model_dump(),
                    "ticker": ticker,
                    "outcome": out,
                    "exclusion_reason": "missing_audited_gross_profit_or_coordinate_join"
                    if out == "excluded" and excl is None
                    else excl,
                }
            )
        )
    data = {
        **card.model_dump(exclude={"card_id", "rendered_html"}),
        "subjects": tuple(enriched),
        "strategy_context": strategy_context if strategy_context is not None else card.strategy_context,
        "style_preset": style_preset if style_preset is not None else card.style_preset,
        "series_index": series_index if series_index is not None else card.series_index,
        "series_total": series_total if series_total is not None else card.series_total,
    }
    base = ResearchCard(**data)
    return base


def paginate_card_series(
    card: ResearchCard, *, max_subjects_per_card: int = MAX_SUBJECTS_PER_CARD, preset: str | None = None
) -> tuple[ResearchCard, ...]:
    if max_subjects_per_card < 1:
        raise ValueError("max_subjects_per_card must be >= 1")
    if len(card.subjects) <= max_subjects_per_card and preset is None:
        return (card,)
    chunks, pkeys = (
        [card.subjects[i : i + max_subjects_per_card] for i in range(0, len(card.subjects), max_subjects_per_card)],
        tuple(CARD_PRESETS.keys()),
    )
    base = card.model_dump(
        exclude={"card_id", "title", "subjects", "style_preset", "series_index", "series_total", "rendered_html"}
    )
    return tuple(
        ResearchCard(
            **base,
            title=(
                f"{card.card_kind.value} ({i}/{len(chunks)}): {', '.join(s.ticker or s.display_name.split(' · ')[0] for s in ch)}"
                if len(chunks) > 1
                else card.title
            ),
            subjects=ch,
            style_preset=preset or card.style_preset or pkeys[(i - 1) % len(pkeys)],
            series_index=i,
            series_total=len(chunks),
        )
        for i, ch in enumerate(chunks, start=1)
    )


def render_card_json(card: ResearchCard) -> str:
    return json.dumps(card.model_dump(mode="json"), sort_keys=True, indent=2, ensure_ascii=True) + "\n"


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
    return (
        f"{XHS_CARD_INSTRUCTIONS}\n\n"
        f"PROMPT_VERSION: {XHS_CARD_PROMPT_VERSION}\n"
        f"REQUIRED_DIMENSIONS: {XHS_CARD_WIDTH_PX}x{XHS_CARD_HEIGHT_PX}\n"
        "VERIFICATION_CHECKLIST: Include verbatim card_id_required_prefix, cutoff_date, "
        "research_risk_note, source_attribution, each subject's availability & claim_class "
        "(and scenario_only if present), and every metric's exact raw value string, period, "
        "confidence, and trace_prefix in the HTML text.\n"
        f"INPUT_RESEARCH_CARD_JSON:\n{json.dumps(payload, indent=2, ensure_ascii=False)}"
    )


def extract_html_document(raw_text: str) -> str:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()[1:]
        cleaned = "\n".join(lines[:-1] if lines and lines[-1].strip().startswith("```") else lines).strip()
    low = cleaned.lower()
    start, end = next((i for i in (low.find("<!doctype html"), low.find("<html")) if i != -1), -1), low.rfind("</html>")
    return cleaned[start : end + 7] if start != -1 and end >= start else cleaned


_TAG_RE, _WS_RE, _EVENT_RE = re.compile(r"<[^>]+>"), re.compile(r"\s+"), re.compile(r"\bon[a-z]+\s*=", re.IGNORECASE)


def _esc(v: str) -> str:
    return v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _has(doc: str, raw: str, norm: str) -> bool:
    return raw in doc or _esc(raw) in doc or _WS_RE.sub(" ", html_lib.unescape(raw)).strip() in norm


def verify_card_html(card: ResearchCard, html: str) -> tuple[bool, tuple[str, ...]]:
    u = _TAG_RE.sub(" ", html)
    for _ in range(3):
        if (n := html_lib.unescape(u)) == u:
            break
        u = n
    norm, v, low, cutoff = _WS_RE.sub(" ", _TAG_RE.sub(" ", u)), [], html.lower(), card.cutoff_at.date().isoformat()
    for bad, code in (
        (("<!doctype html" not in low and "<html" not in low) or "</html>" not in low, "missing_html_envelope"),
        (str(XHS_CARD_WIDTH_PX) not in html or str(XHS_CARD_HEIGHT_PX) not in html, "missing_1080x1440_dimensions"),
        ("<script" in low or "javascript:" in low or bool(_EVENT_RE.search(html)), "forbidden_script_or_event_handler"),
        ("http://" in low or "https://" in low, "forbidden_external_url"),
        (cutoff not in html and card.cutoff_at.isoformat() not in html, f"missing_cutoff:{cutoff}"),
        (
            card.card_id[:12] not in html and card.card_id[:12] not in norm,
            f"missing_card_id_prefix:{card.card_id[:12]}",
        ),
        (not _has(html, card.research_risk_note, norm), "missing_research_risk_note"),
        (not _has(html, card.source_attribution, norm), "missing_source_attribution"),
    ):
        if bad:
            v.append(code)
    preset_id = card.style_preset or _DEFAULT_PRESET.get(card.card_kind, "study-notes")
    if (preset := CARD_PRESETS.get(preset_id)) is not None:
        _, preset_violations = verify_preset_contrast(preset)
        v.extend(preset_violations)
    for s in card.subjects:
        if not _has(html, s.display_name, norm) and s.subject_id not in html:
            v.append(f"missing_subject:{s.subject_id}")
        if s.availability.value not in html and s.availability.value not in norm:
            v.append(f"missing_subject_availability:{s.subject_id}:{s.availability.value}")
        if s.claim_class.value not in html and s.claim_class.value not in norm:
            v.append(f"missing_subject_claim_class:{s.subject_id}:{s.claim_class.value}")
        if s.causal_claim == "scenario_only" and "scenario_only" not in html:
            v.append(f"missing_scenario_only:{s.subject_id}")
        for m in s.metrics:
            zh_label = _DATA["metric_labels_zh"].get(m.label)
            label_ok = _has(html, m.label, norm) or (zh_label is not None and _has(html, zh_label, norm))
            if not label_ok:
                v.append(f"missing_metric_label:{s.subject_id}:{m.label}")
            zh_val = (
                _DATA["tier_labels_zh"].get(m.value, m.value)
                if m.label == "tier" and m.value is not None
                else (
                    _DATA["outcome_labels_zh"].get(m.value, m.value)
                    if m.label == "outcome" and m.value is not None
                    else None
                )
            )
            if m.value is not None and not _has(html, m.value, norm) and not (zh_val and _has(html, zh_val, norm)):
                v.append(f"missing_metric_value:{s.subject_id}:{m.label}:{m.value}")
            for t, k in (
                (m.currency if m.value is not None else None, "currency"),
                (m.unit if m.value is not None else None, "unit"),
                (m.period, "period"),
                (str(m.confidence) if m.confidence is not None else None, "confidence"),
                (m.trace.reference_id[:16] if m.trace else None, "trace"),
            ):
                if t is not None and not _has(html, t, norm):
                    v.append(f"missing_metric_{k}:{s.subject_id}:{m.label}:{t}")
    return (not v, tuple(v))
