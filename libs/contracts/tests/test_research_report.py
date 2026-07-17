from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from truealpha_contracts import research_report
from truealpha_contracts.access import AccessContext, AuthenticationMethod, PrincipalKind
from truealpha_contracts.execution import AvailabilityStatus
from truealpha_contracts.research_report import (
    ReportSection,
    ReportSectionKind,
    ReportSubject,
    ResearchReport,
    ResearchReportKind,
    ResearchReportRequest,
    ResultValue,
    build_research_report,
    render_report_json,
    render_report_markdown,
)
from truealpha_contracts.research_report_fixture import FixtureResearchReadRepository
from truealpha_contracts.strategy_run_fixture import FixtureStrategyRunRepository

GOLDEN = Path(__file__).parent / "golden"
CUTOFF = datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)


def _context(*, expired: bool = False) -> AccessContext:
    now = datetime.now(UTC)
    return AccessContext(
        context_id="ctx:test",
        principal_id="principal:test",
        tenant_id="tenant:test",
        session_id="session:test",
        authentication_method=AuthenticationMethod.SERVICE_IDENTITY,
        principal_kind=PrincipalKind.SERVICE,
        issued_at=now.replace(year=now.year - 1) if expired else now,
        expires_at=now.replace(year=now.year - 1) if expired else now.replace(year=now.year + 1),
    )


def _request(kind: ResearchReportKind, targets: tuple[str, ...], title: str) -> ResearchReportRequest:
    return ResearchReportRequest(report_kind=kind, target_entity_ids=targets, cutoff_at=CUTOFF, title=title)


GOLDEN_CASES = {
    "company_adm": _request(ResearchReportKind.COMPANY, ("issuer:adm",), "ADM — company research report"),
    "etf_qqq": _request(ResearchReportKind.ETF, ("etf:qqq",), "QQQ — ETF research report"),
    "theme_ranking": _request(
        ResearchReportKind.THEME_RANKING, ("theme:large_model_value",), "Large-model-value theme ranking"
    ),
}


def _build(name: str) -> ResearchReport:
    return build_research_report(GOLDEN_CASES[name], FixtureResearchReadRepository(), context=_context())


@pytest.mark.parametrize("name", sorted(GOLDEN_CASES))
def test_golden_json_matches(name: str) -> None:
    report = _build(name)
    expected = (GOLDEN / f"{name}.json").read_text()
    assert render_report_json(report) == expected


@pytest.mark.parametrize("name", sorted(GOLDEN_CASES))
def test_golden_markdown_matches(name: str) -> None:
    report = _build(name)
    expected = (GOLDEN / f"{name}.md").read_text()
    assert render_report_markdown(report) == expected


@pytest.mark.parametrize("name", sorted(GOLDEN_CASES))
def test_identical_read_models_yield_identical_report_id(name: str) -> None:
    first = _build(name)
    second = _build(name)
    assert first.report_id == second.report_id
    assert first.report_id.startswith("report:")


def test_report_id_is_a_pure_function_of_content() -> None:
    report = _build("company_adm")
    # A hand-constructed report with a wrong id must fail closed.
    with pytest.raises(ValidationError):
        ResearchReport(
            report_id="report:" + "0" * 64,
            report_kind=report.report_kind,
            title=report.title,
            cutoff_at=report.cutoff_at,
            generated_from=report.generated_from,
            subjects=report.subjects,
        )
    # Changing any value changes the id.
    mutated_subjects = report.subjects[:0] + (
        ReportSubject(
            subject_id="issuer:adm",
            display_name="issuer:adm-RENAMED",
            sections=report.subjects[0].sections,
        ),
    )
    mutated = ResearchReport.assemble(
        report_kind=report.report_kind,
        title=report.title,
        cutoff_at=report.cutoff_at,
        generated_from=report.generated_from,
        subjects=mutated_subjects,
    )
    assert mutated.report_id != report.report_id


def test_company_values_reproduce_strategy_fixture_exactly() -> None:
    """The builder copies materialized values through; it computes nothing (#369)."""
    strategy = FixtureStrategyRunRepository().get_latest(strategy_id="large_model_value_v0", context=_context())
    decision = next(d for d in strategy.decisions if d.issuer_id == "issuer:adm" and d.cutoff_at == CUTOFF)
    report = _build("company_adm")
    subject = report.subjects[0]

    values: dict[str, str | None] = {}
    for section in subject.sections:
        for result in section.results:
            values[result.label] = result.value

    assert values["capital_adjusted_labor_efficiency"] == str(decision.capital_adjusted_labor_efficiency)
    assert values["current_price_to_sales"] == str(decision.current_price_to_sales)
    assert values["target_price_to_sales"] == str(decision.target_price_to_sales)
    assert values["valuation_gap"] == str(decision.valuation_gap)
    assert values["target_weight"] == str(decision.target_weight)
    assert values["tier"] == decision.tier.value


def test_excluded_and_low_confidence_paths_are_explicit() -> None:
    report = _build("theme_ranking")
    by_id = {subject.subject_id: subject for subject in report.subjects}

    ddog = by_id["issuer:ddog"].sections[0]
    assert ddog.availability is AvailabilityStatus.LOW_CONFIDENCE
    assert ddog.reason_codes == ("below_confidence_floor",)

    jpm = by_id["issuer:jpm"].sections[0]
    assert jpm.availability is AvailabilityStatus.EXCLUDED
    assert jpm.reason_codes == ("financial_valuation_not_comparable",)


def test_ranking_is_ordered_and_reproduces_ranks() -> None:
    report = _build("theme_ranking")
    ranked = [(s.subject_id, s.rank) for s in report.subjects]
    assert ranked[0] == ("issuer:adm", 1)
    assert ranked[1] == ("issuer:nice", 2)
    # Unranked members follow, sorted by issuer id.
    assert [s for s, _ in ranked[2:]] == ["issuer:ddog", "issuer:jpm", "issuer:shop"]


def test_section_filter_selects_only_requested_sections() -> None:
    request = ResearchReportRequest(
        report_kind=ResearchReportKind.COMPANY,
        target_entity_ids=("issuer:adm",),
        cutoff_at=CUTOFF,
        section_kinds=(ReportSectionKind.VALUATION,),
    )
    report = build_research_report(request, FixtureResearchReadRepository(), context=_context())
    kinds = {section.section_kind for section in report.subjects[0].sections}
    assert kinds == {ReportSectionKind.VALUATION}


def test_missing_subject_is_reported_not_dropped() -> None:
    request = _request(ResearchReportKind.COMPANY, ("issuer:not_in_universe",), "Missing")
    report = build_research_report(request, FixtureResearchReadRepository(), context=_context())
    subject = report.subjects[0]
    assert subject.sections[0].availability is AvailabilityStatus.UNAVAILABLE
    assert subject.sections[0].reason_codes == ("subject_not_in_strategy_run",)


def test_request_rejects_naive_cutoff() -> None:
    with pytest.raises(ValidationError):
        ResearchReportRequest(
            report_kind=ResearchReportKind.COMPANY,
            target_entity_ids=("issuer:adm",),
            cutoff_at=datetime(2026, 6, 30, 23, 59, 59),  # noqa: DTZ001 - intentionally naive
        )


def test_result_value_confidence_bounds() -> None:
    with pytest.raises(ValidationError):
        ResultValue(
            label="x",
            cutoff_at=CUTOFF,
            availability=AvailabilityStatus.AVAILABLE,
            confidence=Decimal("1.5"),
        )


def test_section_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        ReportSection(
            section_kind=ReportSectionKind.VALUATION,
            title="x",
            availability=AvailabilityStatus.AVAILABLE,
            unknown_field="nope",
        )


# --- Boundary test: no factor computation leaks into the builder (#369 acceptance) ---

_BUILDER_FUNCTIONS = {
    "build_research_report",
    "_select_subject",
    "_select_sections",
    "_default_title",
}
_FORBIDDEN_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.MatMult)
_FORBIDDEN_CALLS = {"sum", "round", "abs", "pow", "min", "max", "Decimal", "float", "int", "quantize"}


def _forbidden_call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name) and func.id in _FORBIDDEN_CALLS:
        return func.id
    if isinstance(func, ast.Attribute) and func.attr in _FORBIDDEN_CALLS:
        return func.attr
    return None


def test_builder_contains_no_arithmetic_or_computation() -> None:
    """Statically proves `build_research_report` and its selection helpers copy values
    through — they never do arithmetic or call a numeric/aggregation primitive."""
    source = inspect.getsource(research_report)
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
