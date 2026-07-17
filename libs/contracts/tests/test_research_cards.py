from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from truealpha_contracts import research_cards
from truealpha_contracts.access import AccessContext, AuthenticationMethod, PrincipalKind
from truealpha_contracts.execution import AvailabilityStatus, FactorValidationStatus
from truealpha_contracts.research_cards import (
    CardKind,
    CardSubject,
    ClaimClass,
    ResearchCard,
    build_card,
    render_card_html,
    render_card_json,
)
from truealpha_contracts.research_report import (
    EvidenceTrace,
    ReportSection,
    ReportSectionKind,
    ReportSubject,
    ResearchReport,
    ResearchReportKind,
    ResearchReportRequest,
    ResultValue,
    build_research_report,
)
from truealpha_contracts.research_report_fixture import FixtureResearchReadRepository

GOLDEN = Path(__file__).parent / "golden_cards"
CUTOFF = datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)
CUTOFF_MARCH = datetime(2026, 3, 31, 23, 59, 59, tzinfo=UTC)


def _context() -> AccessContext:
    now = datetime.now(UTC)
    return AccessContext(
        context_id="ctx:test",
        principal_id="principal:test",
        tenant_id="tenant:test",
        session_id="session:test",
        authentication_method=AuthenticationMethod.SERVICE_IDENTITY,
        principal_kind=PrincipalKind.SERVICE,
        issued_at=now,
        # timedelta, not .replace(year=...): replacing into a non-leap year raises
        # ValueError when `now` falls on Feb 29 (Copilot review on #390).
        expires_at=now + timedelta(days=400),
    )


def _report(kind: ResearchReportKind, targets: tuple[str, ...], cutoff: datetime = CUTOFF) -> ResearchReport:
    request = ResearchReportRequest(report_kind=kind, target_entity_ids=targets, cutoff_at=cutoff)
    return build_research_report(request, FixtureResearchReadRepository(), context=_context())


def _long_name_report() -> ResearchReport:
    return ResearchReport.assemble(
        report_kind=ResearchReportKind.COMPANY,
        title="Long-name multi-currency research report",
        cutoff_at=CUTOFF,
        generated_from="fixture:research_report.v1",
        subjects=(
            ReportSubject(
                subject_id="issuer:long-name-example-conglomerate-holdings-plc",
                display_name="A Very Long Illustrative Multinational Conglomerate Holdings Public Limited Company",
                sections=(
                    ReportSection(
                        section_kind=ReportSectionKind.VALUATION,
                        title="Valuation (three-tier P/S)",
                        availability=AvailabilityStatus.AVAILABLE,
                        validation_status=FactorValidationStatus.NOT_EVALUATED,
                        results=(
                            ResultValue(
                                label="current_price_to_sales",
                                value="12.3456",
                                currency="EUR",
                                period="2026-06-30",
                                cutoff_at=CUTOFF,
                                availability=AvailabilityStatus.AVAILABLE,
                                confidence=Decimal("0.75"),
                                factor_version="large_model_value_v0",
                                trace=EvidenceTrace(reference_id="fixture:long-name:2026-06-30"),
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )


GOLDEN_CARDS = {
    "company": lambda: build_card(_report(ResearchReportKind.COMPANY, ("issuer:adm",)), CardKind.COMPANY),
    "comparison": lambda: build_card(
        _report(ResearchReportKind.COMPANY, ("issuer:adm", "issuer:nice")), CardKind.COMPARISON
    ),
    "ranking": lambda: build_card(
        _report(ResearchReportKind.THEME_RANKING, ("theme:large_model_value",)), CardKind.RANKING
    ),
    "etf": lambda: build_card(_report(ResearchReportKind.ETF, ("etf:qqq",)), CardKind.ETF),
    "supply_chain": lambda: build_card(_report(ResearchReportKind.COMPANY, ("issuer:adm",)), CardKind.SUPPLY_CHAIN),
    "strategy": lambda: build_card(_report(ResearchReportKind.COMPANY, ("issuer:adm",)), CardKind.STRATEGY_SUMMARY),
    "negative": lambda: build_card(
        _report(ResearchReportKind.COMPANY, ("issuer:shop",), CUTOFF_MARCH), CardKind.COMPANY
    ),
    "unavailable": lambda: build_card(
        _report(ResearchReportKind.COMPANY, ("issuer:not_in_universe",)), CardKind.COMPANY
    ),
    "low_confidence": lambda: build_card(_report(ResearchReportKind.COMPANY, ("issuer:ddog",)), CardKind.COMPANY),
    "long_name_multi_currency": lambda: build_card(_long_name_report(), CardKind.COMPANY),
}


@pytest.mark.parametrize("name", sorted(GOLDEN_CARDS))
def test_golden_json_matches(name: str) -> None:
    card = GOLDEN_CARDS[name]()
    expected = (GOLDEN / f"{name}.json").read_text(encoding="utf-8")
    assert render_card_json(card) == expected


@pytest.mark.parametrize("name", sorted(GOLDEN_CARDS))
def test_golden_html_matches(name: str) -> None:
    card = GOLDEN_CARDS[name]()
    expected = (GOLDEN / f"{name}.html").read_text(encoding="utf-8")
    assert render_card_html(card) == expected


@pytest.mark.parametrize("name", sorted(GOLDEN_CARDS))
def test_card_id_is_stable_across_rebuilds(name: str) -> None:
    first = GOLDEN_CARDS[name]()
    second = GOLDEN_CARDS[name]()
    assert first.card_id == second.card_id
    assert first.card_id.startswith("card:")


def test_a_template_or_report_change_produces_a_new_revision() -> None:
    card = GOLDEN_CARDS["company"]()
    retitled = build_card(
        _report(ResearchReportKind.COMPANY, ("issuer:adm",)), CardKind.COMPANY, title="Different title"
    )
    assert retitled.card_id != card.card_id

    other_report = _report(ResearchReportKind.COMPANY, ("issuer:nice",))
    other_card = build_card(other_report, CardKind.COMPANY)
    assert other_card.card_id != card.card_id


def test_card_id_fails_closed_on_mismatch() -> None:
    card = GOLDEN_CARDS["company"]()
    with pytest.raises(ValidationError):
        ResearchCard(
            card_id="card:" + "0" * 64,
            card_kind=card.card_kind,
            title=card.title,
            cutoff_at=card.cutoff_at,
            generated_from_report_id=card.generated_from_report_id,
            research_risk_note=card.research_risk_note,
            source_attribution=card.source_attribution,
            subjects=card.subjects,
        )


def test_negative_value_flows_through_unmodified() -> None:
    card = GOLDEN_CARDS["negative"]()
    gap = next(m for m in card.subjects[0].metrics if m.label == "valuation_gap")
    assert gap.value == "-0.3250"


def test_multi_currency_value_is_not_clipped_or_converted() -> None:
    card = GOLDEN_CARDS["long_name_multi_currency"]()
    metric = card.subjects[0].metrics[0]
    assert metric.value == "12.3456"
    assert metric.currency == "EUR"


def test_low_confidence_subject_is_explicit_and_untouched() -> None:
    card = GOLDEN_CARDS["low_confidence"]()
    subject = card.subjects[0]
    assert subject.availability is AvailabilityStatus.LOW_CONFIDENCE
    confidence_values = {m.confidence for m in subject.metrics if m.confidence is not None}
    assert confidence_values == {Decimal("0.65")}


def test_unavailable_subject_has_no_metrics_but_is_present() -> None:
    card = GOLDEN_CARDS["unavailable"]()
    subject = card.subjects[0]
    assert subject.availability is AvailabilityStatus.UNAVAILABLE
    assert subject.metrics == ()
    assert subject.claim_class is ClaimClass.NOT_APPLICABLE


def test_supply_chain_card_is_pinned_to_scenario_only() -> None:
    card = GOLDEN_CARDS["supply_chain"]()
    for subject in card.subjects:
        assert subject.causal_claim == "scenario_only"
    assert "not causal evidence" in card.research_risk_note


def test_non_supply_chain_card_cannot_carry_a_causal_claim() -> None:
    with pytest.raises(ValidationError):
        ResearchCard(
            card_kind=CardKind.COMPANY,
            title="x",
            cutoff_at=CUTOFF,
            generated_from_report_id="report:" + "0" * 64,
            research_risk_note="note",
            source_attribution="attribution",
            subjects=(
                CardSubject(
                    subject_id="issuer:x",
                    display_name="issuer:x",
                    availability=AvailabilityStatus.AVAILABLE,
                    claim_class=ClaimClass.RESEARCH_HYPOTHESIS,
                    metrics=(),
                    causal_claim="scenario_only",
                ),
            ),
        )


def test_supply_chain_card_cannot_omit_the_causal_claim() -> None:
    with pytest.raises(ValidationError):
        ResearchCard(
            card_kind=CardKind.SUPPLY_CHAIN,
            title="x",
            cutoff_at=CUTOFF,
            generated_from_report_id="report:" + "0" * 64,
            research_risk_note="note",
            source_attribution="attribution",
            subjects=(
                CardSubject(
                    subject_id="issuer:x",
                    display_name="issuer:x",
                    availability=AvailabilityStatus.UNAVAILABLE,
                    claim_class=ClaimClass.NOT_APPLICABLE,
                    metrics=(),
                    causal_claim=None,
                ),
            ),
        )


def test_claim_class_reflects_validation_status_not_invented() -> None:
    accepted_report = ResearchReport.assemble(
        report_kind=ResearchReportKind.COMPANY,
        title="Accepted-validation report",
        cutoff_at=CUTOFF,
        generated_from="fixture:test",
        subjects=(
            ReportSubject(
                subject_id="issuer:validated",
                display_name="issuer:validated",
                sections=(
                    ReportSection(
                        section_kind=ReportSectionKind.VALUATION,
                        title="Valuation",
                        availability=AvailabilityStatus.AVAILABLE,
                        validation_status=FactorValidationStatus.ACCEPTED,
                        results=(),
                    ),
                ),
            ),
        ),
    )
    card = build_card(accepted_report, CardKind.COMPANY)
    assert card.subjects[0].claim_class is ClaimClass.EMPIRICALLY_VALIDATED


def test_claim_class_is_most_conservative_across_multiple_sections() -> None:
    """A COMPANY card pulls both OPERATING_EFFICIENCY and VALUATION (#372's _CARD_SECTIONS).
    If they disagree on validation_status, the card must take the most conservative one
    (REJECTED > NOT_EVALUATED > ACCEPTED), never the status of whichever section happens to
    tie-break first by availability (Copilot review on #390). Both sections share the same
    AVAILABLE status here specifically to defeat the old first-occurrence tie-break and
    isolate the validation-status aggregation from the availability aggregation."""
    mixed_report = ResearchReport.assemble(
        report_kind=ResearchReportKind.COMPANY,
        title="Mixed-validation report",
        cutoff_at=CUTOFF,
        generated_from="fixture:test",
        subjects=(
            ReportSubject(
                subject_id="issuer:mixed",
                display_name="issuer:mixed",
                sections=(
                    ReportSection(
                        section_kind=ReportSectionKind.OPERATING_EFFICIENCY,
                        title="Operating efficiency",
                        availability=AvailabilityStatus.AVAILABLE,
                        validation_status=FactorValidationStatus.ACCEPTED,
                        results=(),
                    ),
                    ReportSection(
                        section_kind=ReportSectionKind.VALUATION,
                        title="Valuation",
                        availability=AvailabilityStatus.AVAILABLE,
                        validation_status=FactorValidationStatus.REJECTED,
                        results=(),
                    ),
                ),
            ),
        ),
    )
    card = build_card(mixed_report, CardKind.COMPANY)
    assert card.subjects[0].claim_class is ClaimClass.REJECTED_DO_NOT_USE


def test_ranking_card_preserves_rank_order() -> None:
    card = GOLDEN_CARDS["ranking"]()
    ranks = [(s.subject_id, s.rank) for s in card.subjects]
    assert ranks[0] == ("issuer:adm", 1)
    assert ranks[1] == ("issuer:nice", 2)


def test_card_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        CardSubject(
            subject_id="issuer:x",
            display_name="issuer:x",
            availability=AvailabilityStatus.AVAILABLE,
            claim_class=ClaimClass.RESEARCH_HYPOTHESIS,
            metrics=(),
            unknown_field="nope",
        )


def test_card_requires_a_naive_free_cutoff() -> None:
    card = GOLDEN_CARDS["company"]()
    with pytest.raises(ValidationError):
        ResearchCard(
            card_kind=card.card_kind,
            title=card.title,
            cutoff_at=datetime(2026, 6, 30, 23, 59, 59),  # noqa: DTZ001 - intentionally naive
            generated_from_report_id=card.generated_from_report_id,
            research_risk_note=card.research_risk_note,
            source_attribution=card.source_attribution,
            subjects=card.subjects,
        )


def test_html_escapes_special_characters() -> None:
    hostile_report = ResearchReport.assemble(
        report_kind=ResearchReportKind.COMPANY,
        title='Hostile <script>alert(1)</script> & "quoted" title',
        cutoff_at=CUTOFF,
        generated_from="fixture:test",
        subjects=(
            ReportSubject(
                subject_id="issuer:hostile",
                display_name='<img src=x onerror=alert(1)> & "quoted"',
                sections=(),
            ),
        ),
    )
    html = render_card_html(build_card(hostile_report, CardKind.COMPANY))
    assert "<script>" not in html
    assert "<img" not in html
    assert "&lt;img" in html
    assert "&amp;" in html
    assert "&quot;quoted&quot;" in html
    assert html.count("<html") == 1


# --- Boundary test: no mart query, no factor/cross-row computation (#372 acceptance) ---

_BUILDER_FUNCTIONS = {
    "build_card",
    "_card_subject",
    "_select_metrics",
    "_section_status",
    "_default_card_title",
    "_claim_class",
}
# `min`/`max` are deliberately NOT forbidden: `_section_status` uses `min(sections, key=...)`
# to select the worst already-materialized availability label by a fixed enum priority
# order — a categorical selection over existing labels, not numeric aggregation over a
# metric value. The forbidden set below targets numeric/decimal computation only
# (Copilot review on #390: the PR description's "no ... aggregation primitive" wording
# was imprecise about this distinction).
_FORBIDDEN_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.MatMult)
_FORBIDDEN_CALLS = {"sum", "round", "abs", "pow", "Decimal", "float", "int", "quantize"}
_FORBIDDEN_IMPORT_MODULES = {"psycopg", "sqlalchemy"}


def _forbidden_call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name) and func.id in _FORBIDDEN_CALLS:
        return func.id
    if isinstance(func, ast.Attribute) and func.attr in _FORBIDDEN_CALLS:
        return func.attr
    return None


def test_card_builder_contains_no_arithmetic_or_computation() -> None:
    source = inspect.getsource(research_cards)
    tree = ast.parse(source)
    checked: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in _BUILDER_FUNCTIONS:
            continue
        checked.add(node.name)
        for inner in ast.walk(node):
            assert not isinstance(inner, _FORBIDDEN_BINOPS), f"{node.name} contains arithmetic {type(inner).__name__}"
            assert not isinstance(inner, ast.AugAssign), f"{node.name} contains an augmented assignment"
            if isinstance(inner, ast.Call):
                name = _forbidden_call_name(inner)
                assert name is None, f"{node.name} calls forbidden computation primitive {name!r}"
    assert checked == _BUILDER_FUNCTIONS, f"boundary test did not scan every builder function: {checked}"


def test_card_module_performs_no_mart_query() -> None:
    """The card module must never import a read port/repository — a card is built from an
    already-assembled ResearchReport in memory, never from a fresh mart query."""
    tree = ast.parse(inspect.getsource(research_cards))
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert not any(forbidden in module for forbidden in _FORBIDDEN_IMPORT_MODULES), (
                f"research_cards.py must not import a database driver ({module})"
            )
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            # Plain `import x` / `import x.y as z`, not just `from x import y` — a bare
            # `import psycopg` would otherwise bypass this scan (Copilot review on #390).
            for alias in node.names:
                assert not any(forbidden in alias.name for forbidden in _FORBIDDEN_IMPORT_MODULES), (
                    f"research_cards.py must not import a database driver ({alias.name})"
                )
                imported_names.add(alias.asname or alias.name)
    assert "ResearchReadPort" not in imported_names, "research_cards.py must not depend on a read port/repository"
    assert "FixtureResearchReadRepository" not in imported_names
