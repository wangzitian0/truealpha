"""Versioned card schemas and deterministic renderer from `ResearchReport` (#372, #44)."""

from __future__ import annotations

import html as html_lib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Final, Literal

from pydantic import Field, model_validator

from truealpha_contracts.common import OmitNoneFields, StrictFrozenModel, canonical_sha256, require_aware
from truealpha_contracts.execution import AvailabilityStatus, FactorValidationStatus
from truealpha_contracts.research_report import ReportSection, ReportSectionKind, ResearchReport, ResultValue

CARD_SCHEMA_VERSION: Literal["research_card.v1"] = "research_card.v1"
_CARD_ID_PREFIX = "card:"
MAX_SUBJECTS_PER_CARD: Final[int] = 4
XHS_CARD_WIDTH_PX = 1080
XHS_CARD_HEIGHT_PX = 1440
TEMPLATE_VERSION: Literal["card_template.v1"] = "card_template.v1"


class CardPreset(StrictFrozenModel):
    preset_id: str = Field(min_length=1)
    style: str = Field(min_length=1)
    layout: str = Field(min_length=1)
    palette: str = Field(min_length=1)
    bg_css: str = Field(min_length=1)
    surface_bg: str = Field(min_length=1)
    fg: str = Field(min_length=1)
    mute: str = Field(min_length=1)
    accent: str = Field(min_length=1)
    border: str = Field(min_length=1)
    chip_bg: str = Field(min_length=1)


_BOLD_BG = "radial-gradient(circle at 85% 12%, #311042 0%, #140C1C 55%, #0B0711 100%)"
_FRESH_BG = "linear-gradient(160deg, #E8F8F5 0%, #F4FBF7 55%, #FFFFFF 100%)"
_PRESET_KEYS = ("style", "layout", "palette", "bg_css", "surface_bg", "fg", "mute", "accent", "border", "chip_bg")
_PRESET_ROWS = (
    ("bold", "sparse", "neon", _BOLD_BG, "#191126", "#F5F5F7", "#B8B8D4", "#39FF14", "#00F5FF", "#120B1D"),
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
    ("fresh", "mindmap", "pastel", _FRESH_BG, "#FFFFFF", "#1B3B2B", "#4A6B5D", "#148F77", "#48C9B0", "#E8F6F3"),
)

CARD_PRESETS: Final[dict[str, CardPreset]] = {
    row[0]: CardPreset.model_validate({"preset_id": row[0], **dict(zip(_PRESET_KEYS, row, strict=True))})
    for row in _PRESET_ROWS
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


_SOURCE_ATTRIBUTION = "TrueAlpha research — traceable to a materialized factor output; see the compact trace id."

_CARD_META: dict[CardKind, tuple[tuple[ReportSectionKind, ...], str, str, str]] = {
    CardKind.COMPANY: (
        (ReportSectionKind.OPERATING_EFFICIENCY, ReportSectionKind.VALUATION),
        "Research hypothesis, not investment advice. Confidence reflects source evidence, not independent formula validation unless marked empirically validated.",
        "单票深度拆解 · COMPANY",
        "study-notes",
    ),
    CardKind.COMPARISON: (
        (ReportSectionKind.VALUATION,),
        "Side-by-side comparison of independently materialized results at one cutoff; no cross-issuer metric is computed for this card.",
        "多票横向对比 · COMPARISON",
        "minimal",
    ),
    CardKind.RANKING: (
        (ReportSectionKind.VALUATION, ReportSectionKind.STRATEGY_SUMMARY, ReportSectionKind.RANKING),
        "Ranking reflects one versioned strategy run at one cutoff; historical rank is not a forward-looking guarantee.",
        "策略估值折价榜 · RANKING",
        "screen-print",
    ),
    CardKind.ETF: (
        (ReportSectionKind.ETF_VIRTUAL_COMPANY,),
        "ETF virtual-company view is delayed by public holdings-disclosure lag and is a research hypothesis, not investment advice.",
        "ETF 穿透虚拟公司 · ETF",
        "retro",
    ),
    CardKind.SUPPLY_CHAIN: (
        (ReportSectionKind.SUPPLY_CHAIN,),
        "Scenario propagation over a disclosed relationship graph. High edge confidence does not by itself establish a causal effect; this is not causal evidence.",
        "供应链情景推演 · SUPPLY CHAIN",
        "sketch-notes",
    ),
    CardKind.STRATEGY_SUMMARY: (
        (ReportSectionKind.STRATEGY_SUMMARY,),
        "Reflects one versioned strategy's decision at one cutoff, not a performance guarantee.",
        "策略过筛漏斗 · STRATEGY",
        "bold",
    ),
}


class CardSubject(OmitNoneFields, StrictFrozenModel):
    omit_none_fields = ("ticker", "outcome", "exclusion_reason")
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


class ResearchCard(OmitNoneFields, StrictFrozenModel):
    omit_none_fields = ("strategy_context", "style_preset", "series_index", "series_total", "rendered_html")
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
    AvailabilityStatus.NOT_APPLICABLE: 6,
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
    wanted, risk_note, _, _ = _CARD_META[card_kind]
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
        research_risk_note=risk_note,
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
    wanted = {"outcome", "tier", "target_price_to_gross_profit", "target_price_to_sales"}
    for sub in report.subjects:
        fields = {r.label: r.value for sec in sub.sections for r in sec.results if r.value and r.label in wanted}
        t_pgp, t_ps = fields.get("target_price_to_gross_profit"), fields.get("target_price_to_sales")
        if t_ps is not None and t_pgp is None:
            basis = "price_to_sales"
        if (tier := fields.get("tier")) and (t_pgp or t_ps):
            ceilings[tier] = t_pgp or t_ps or ""
        outcome = fields.get("outcome")
        if outcome in counts:
            counts[outcome] += 1
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
    rep_map = {s.subject_id: s for s in report.subjects}
    enriched: list[CardSubject] = []
    for sub in card.subjects:
        rep_sub = rep_map.get(sub.subject_id)
        ticker = sub.ticker or (sub.display_name.split(" · ", 1)[0].strip() if " · " in sub.display_name else None)
        outcome, excl = sub.outcome, sub.exclusion_reason
        if rep_sub is not None:
            for sec in rep_sub.sections:
                if sec.section_kind is ReportSectionKind.STRATEGY_SUMMARY:
                    excl = excl or (sec.reason_codes[0] if sec.reason_codes else None)
                    for res in sec.results:
                        if res.label == "outcome" and res.value and outcome is None:
                            outcome = res.value
        if outcome == "excluded" and excl is None:
            excl = "missing_audited_gross_profit_or_coordinate_join"
        enriched.append(sub.model_copy(update={"ticker": ticker, "outcome": outcome, "exclusion_reason": excl}))
    updates = {
        "subjects": tuple(enriched),
        **{
            k: v
            for k, v in (
                ("strategy_context", strategy_context),
                ("style_preset", style_preset),
                ("series_index", series_index),
                ("series_total", series_total),
            )
            if v is not None
        },
    }
    base = ResearchCard(**{**card.model_dump(exclude={"card_id", "rendered_html"}), **updates})
    if not include_rendered_html:
        return base
    return base.model_copy(update={"rendered_html": render_card_html(base, preset=base.style_preset)})


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


def _prompt_subject(sub: CardSubject) -> dict[str, object]:
    return {
        "subject_id": sub.subject_id,
        "display_name": sub.display_name,
        "rank": sub.rank,
        "availability": sub.availability.value,
        "claim_class": sub.claim_class.value,
        "causal_claim": sub.causal_claim,
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
            for m in sub.metrics
        ],
    }


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
        "subjects": [_prompt_subject(sub) for sub in card.subjects],
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


def _normalize_html_text(html_doc: str) -> str:
    unescaped = _HTML_TAG_RE.sub(" ", html_doc)
    for _ in range(3):
        next_val = html_lib.unescape(unescaped)
        if next_val == unescaped:
            break
        unescaped = next_val
    return _WHITESPACE_RE.sub(" ", _HTML_TAG_RE.sub(" ", unescaped))


def _contains_text(html_doc: str, raw: str, norm_doc: str) -> bool:
    if raw in html_doc or html_lib.escape(raw) in html_doc:
        return True
    return _WHITESPACE_RE.sub(" ", html_lib.unescape(raw)).strip() in norm_doc


def verify_card_html(card: ResearchCard, html: str) -> tuple[bool, tuple[str, ...]]:
    norm, lower = _normalize_html_text(html), html.lower()
    cutoff_date, prefix = card.cutoff_at.date().isoformat(), card.card_id[:12]

    def has_tok(val: str) -> bool:
        return val in html or val in norm

    def has_txt(val: str) -> bool:
        return _contains_text(html, val, norm)

    violations = [
        tag
        for bad, tag in (
            (
                ("<!doctype html" not in lower and "<html" not in lower) or "</html>" not in lower,
                "missing_html_envelope",
            ),
            (str(XHS_CARD_WIDTH_PX) not in html or str(XHS_CARD_HEIGHT_PX) not in html, "missing_1080x1440_dimensions"),
            (
                "<script" in lower or "javascript:" in lower or bool(_INLINE_EVENT_HANDLER_RE.search(html)),
                "forbidden_script_or_event_handler",
            ),
            ("http://" in lower or "https://" in lower, "forbidden_external_url"),
            (cutoff_date not in html and card.cutoff_at.isoformat() not in html, f"missing_cutoff:{cutoff_date}"),
            (not has_tok(prefix), f"missing_card_id_prefix:{prefix}"),
            (not has_txt(card.research_risk_note), "missing_research_risk_note"),
            (not has_txt(card.source_attribution), "missing_source_attribution"),
        )
        if bad
    ]
    for sub in card.subjects:
        sid, av, cc = sub.subject_id, sub.availability.value, sub.claim_class.value
        violations.extend(
            tag
            for bad, tag in (
                (not has_txt(sub.display_name) and sid not in html, f"missing_subject:{sid}"),
                (not has_tok(av), f"missing_subject_availability:{sid}:{av}"),
                (not has_tok(cc), f"missing_subject_claim_class:{sid}:{cc}"),
                (sub.causal_claim == "scenario_only" and "scenario_only" not in html, f"missing_scenario_only:{sid}"),
            )
            if bad
        )
        for m in sub.metrics:
            for text, kind in (
                (m.label, "label"),
                (m.value, "value"),
                (m.currency if m.value is not None else None, "currency"),
                (m.unit if m.value is not None else None, "unit"),
                (m.period, "period"),
                (str(m.confidence) if m.confidence is not None else None, "confidence"),
                (m.trace.reference_id[:16] if m.trace is not None else None, "trace"),
            ):
                if text is not None and not has_txt(text):
                    violations.append(f"missing_metric_{kind}:{sid}:{m.label}{f':{text}' if kind != 'label' else ''}")
    return not violations, tuple(violations)


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


_esc = html_lib.escape


def _pill(cls: str, text: str) -> str:
    return f'<span class="pill {cls}">{text}</span>'


def _render_value(r: ResultValue) -> str:
    if r.value is None:
        return "—"
    suffix = (
        f" {r.currency} ({r.unit})"
        if r.currency and r.unit
        else (f" {r.currency or r.unit}" if r.currency or r.unit else "")
    )
    return _esc(f"{r.value}{suffix}")


def _render_metric_card(r: ResultValue, *, compact: bool = False) -> str:
    zh = _esc(_METRIC_LABELS_ZH.get(r.label, r.label))
    val_map = _TIER_LABELS_ZH if r.label == "tier" else (_OUTCOME_LABELS_ZH if r.label == "outcome" else None)
    val = "—" if r.value is None else (_esc(val_map.get(r.value, r.value)) if val_map else _render_value(r))
    if compact:
        return f'<div class="metric-tile compact"><div class="metric-label-zh">{zh}</div><div class="metric-value">{val}</div></div>'
    conf, av = _esc("—" if r.confidence is None else str(r.confidence)), r.availability.value
    return (
        f'<div class="metric-tile"><div class="metric-top"><span class="metric-label-zh">{zh}</span>'
        f'{_pill(f"availability-{av}", av)}</div><div class="metric-value">{val}</div>'
        f'<div class="metric-meta"><span>截面: {_esc(r.period or "—")}</span><span>置信度: {conf}</span></div></div>'
    )


def _render_subject(sub: CardSubject, *, compact: bool = False) -> str:
    rank_html = f'<span class="rank-badge">#{sub.rank}</span>' if sub.rank is not None else ""
    causal = (
        '<div class="causal-banner">scenario_only · 仅做图谱情景推演，不构成因果结论。</div>'
        if sub.causal_claim == "scenario_only"
        else ""
    )
    badge_txt = (
        _OUTCOME_LABELS_ZH.get(sub.outcome, sub.outcome)
        if sub.outcome
        else (f"剔除: {sub.exclusion_reason}" if sub.exclusion_reason else "")
    )
    badge = _pill("outcome-pill", _esc(badge_txt)) if badge_txt else ""
    tiles = "".join(_render_metric_card(r, compact=compact) for r in sub.metrics[: (4 if compact else 6)]) or (
        '<div class="empty-tile">当前治理截面暂无已物化因子数据 (unavailable)</div>'
    )
    cls_sfx = " compact" if compact else ""
    cc, av = sub.claim_class.value, sub.availability.value
    return (
        f'<section class="subject-block{cls_sfx}"><div class="subject-header">'
        f'<div class="subject-title-group">{rank_html}<h2>{_esc(sub.display_name)}</h2>'
        f'<span class="subject-id">{_esc(sub.subject_id)}</span></div>'
        f'<div class="subject-badges">{badge}{_pill(f"claim-class-{cc}", cc)}{_pill(f"availability-{av}", av)}'
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
        conf = str(m.confidence) if m.confidence is not None else "—"
        parts.append(
            f"{_esc(m.label)}={_render_value(m)}[p={_esc(m.period or '—')},v={_esc(m.factor_version or '—')},c={_esc(conf)},t={_esc(tr)}]"
        )
    return " · ".join(parts)


_STYLE_TEMPLATE = (
    "*{box-sizing:border-box;margin:0;padding:0;}"
    "html,body,.card-canvas{width:1080px;height:1440px;overflow:hidden;background:%(bg_css)s;color:%(fg)s;font-family:'PingFang SC','Microsoft YaHei',sans-serif;}"
    ".card-canvas{padding:36px 42px 24px;display:flex;flex-direction:column;justify-content:space-between;border:3px solid %(border)s;}"
    ".card-header{border-bottom:2px solid %(border)s;padding-bottom:14px;margin-bottom:14px;}"
    ".eyebrow,.meta-strip,.subject-id,.pill,.metric-value,.metric-meta,.ev-box{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;}"
    ".eyebrow,.subject-header,.metric-top,.metric-meta{display:flex;justify-content:space-between;}"
    ".eyebrow,.subject-header,.metric-top{align-items:center;}"
    ".eyebrow{font-size:13px;color:%(mute)s;margin-bottom:8px;font-weight:700;}"
    ".preset-tag,.rank-badge{background:%(accent)s;color:#FFF;font-weight:800;}"
    ".preset-tag{padding:3px 10px;border-radius:999px;font-size:11px;}"
    "h1{font-size:32px;line-height:1.18;margin-bottom:8px;font-weight:900;}"
    ".meta-strip,.strategy-bar,.subject-title-group,.subject-badges{display:flex;flex-wrap:wrap;}"
    ".meta-strip{gap:12px;font-size:12px;color:%(mute)s;}"
    ".strategy-bar,.pill,.metric-tile,.ev-box{background:%(chip_bg)s;border:1px solid %(border)s;}"
    ".strategy-bar{gap:10px;border-width:1.5px;border-radius:10px;padding:8px 12px;margin-bottom:12px;font-size:12px;}"
    ".subject-block{background:%(surface_bg)s;border:2px solid %(border)s;border-radius:14px;padding:16px 18px;margin-bottom:12px;}"
    ".subject-block.compact{padding:11px 14px;margin-bottom:9px;}"
    ".subject-header{gap:10px;border-bottom:1px solid %(border)s;padding-bottom:8px;margin-bottom:10px;}"
    ".subject-title-group{align-items:baseline;gap:8px;}"
    "h2{font-size:20px;font-weight:800;}.subject-block.compact h2{font-size:17px;}"
    ".subject-id,.series-note,.card-footer{color:%(mute)s;}"
    ".subject-id{font-size:11px;}.rank-badge{padding:2px 8px;border-radius:6px;font-size:13px;}"
    ".subject-badges{gap:6px;}.pill{font-size:10.5px;padding:2px 7px;border-radius:999px;font-weight:700;}"
    ".causal-banner{background:#FEF3C7;border:1px solid #D97706;color:#92400E;padding:6px 10px;border-radius:8px;font-size:12px;font-weight:700;margin-bottom:10px;}"
    ".metrics-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;}"
    ".metrics-grid.compact{grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;}"
    ".metric-tile{border-radius:9px;padding:10px 12px;}.metric-tile.compact{padding:7px 9px;}"
    ".metric-top{margin-bottom:4px;gap:4px;}.metric-label-zh{font-size:11.5px;font-weight:700;color:%(mute)s;}"
    ".metric-value{font-size:20px;font-weight:900;color:%(fg)s;margin:2px 0 4px;}"
    ".metric-tile.compact .metric-value{font-size:16px;margin:2px 0 0;}.metric-meta{font-size:10.5px;color:%(mute)s;}"
    ".empty-tile{grid-column:1/-1;padding:12px;text-align:center;color:%(mute)s;font-size:13px;background:%(chip_bg)s;border:1px dashed %(border)s;border-radius:8px;}"
    ".series-note{font-size:11.5px;text-align:center;padding:4px 0;font-weight:600;}"
    ".card-footer{border-top:2px solid %(border)s;padding-top:10px;margin-top:auto;display:flex;flex-direction:column;gap:3px;font-size:10.5px;}"
    ".ev-box{border-radius:6px;padding:4px 7px;font-size:7.5px;line-height:1.22;max-height:145px;overflow:hidden;word-break:break-all;}"
)

_HEADLINE_PREFIX = {
    CardKind.RANKING: "估值折价排位",
    CardKind.COMPARISON: "多票同截面比价",
    CardKind.STRATEGY_SUMMARY: "策略过筛结论",
}


def render_card_html(card: ResearchCard, *, preset: str | CardPreset | None = None) -> str:
    _, _, kind_zh, default_preset = _CARD_META[card.card_kind]
    theme = (
        preset
        if isinstance(preset, CardPreset)
        else CARD_PRESETS.get(preset or card.style_preset or "") or CARD_PRESETS[default_preset]
    )
    visible = card.subjects[:MAX_SUBJECTS_PER_CARD]
    subjects_html = "".join(_render_subject(s, compact=len(visible) >= 3) for s in visible)
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
            f"{_HEADLINE_PREFIX.get(card.card_kind, card.card_kind.value.upper())}："
            f"{'、'.join(s.display_name.split(' · ')[0] for s in card.subjects[:3])}"
            f"{f' 等 {len(card.subjects)} 家' if len(card.subjects) > 3 else ''}"
        )
    )
    ctx = card.strategy_context
    ctx_bar = ""
    if ctx is not None:
        items = (
            ("候选池", ctx.total_universe_count, " 家"),
            ("入选", ctx.selected_count, f" 家 (Top {ctx.selection_count})"),
            ("候补", ctx.ranked_beyond_selection_count, " 家"),
            ("超上限淘汰", ctx.rejected_above_band_count, " 家"),
            ("缺数据剔除", ctx.excluded_count, " 家"),
        )
        spans = "".join(f"<span>{lbl} <b>{cnt}</b>{sfx}</span>" for lbl, cnt, sfx in items)
        ctx_bar = f'<div class="strategy-bar">{spans}<span>分档上限: {_esc(" / ".join(f"{k}:{v}x" for k, v in ctx.tier_ceilings))}</span></div>'
    cutoff_date = card.cutoff_at.date().isoformat()
    ev_lines = "".join(f'<div class="ev-line">{_subject_evidence_line(s)}</div>' for s in card.subjects)
    return (
        f'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>{_esc(card.title)}</title>'
        f'<style>{_STYLE_TEMPLATE % theme.model_dump()}</style></head><body><div class="card-canvas"><div>'
        '<header class="card-header"><div class="eyebrow">'
        f"<span>TRUEALPHA 买方研报卡 · {_esc(kind_zh)}{series_badge}</span>"
        f'<span class="preset-tag">{_esc(theme.style.upper())} × {_esc(theme.layout.upper())} · {XHS_CARD_WIDTH_PX}x{XHS_CARD_HEIGHT_PX}</span>'
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
