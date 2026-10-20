from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from truealpha_contracts import research_report
from truealpha_contracts.access import AccessContext, AuthenticationMethod, PrincipalKind
from truealpha_contracts.execution import AvailabilityStatus
from truealpha_contracts.research import ValuationTier
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
from truealpha_contracts.research_report_mart import (
    MartResearchReadRepository,
    _operating_efficiency_section,
    _strategy_summary_section,
    _valuation_section,
)
from truealpha_contracts.strategy_run import StrategyRunDecision, StrategyRunOutcome
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
        # timedelta, not .replace(year=...): replacing into a non-leap year raises
        # ValueError when `now` falls on Feb 29 (Copilot review on #383).
        issued_at=now - timedelta(days=400) if expired else now,
        expires_at=now - timedelta(days=400) if expired else now + timedelta(days=400),
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


class _NoServedModuleRows:
    """An issuer-module read with no served row for any issuer."""

    def supply_chain_exposure(self, issuer_id: str) -> None:
        return None

    def analyst_ratings(self, issuer_id: str) -> None:
        return None


def _repository() -> MartResearchReadRepository:
    """The deployed reader class over the checked-in strategy run and no module rows."""
    return MartResearchReadRepository(
        strategy_repository=FixtureStrategyRunRepository(), issuer_modules=_NoServedModuleRows()
    )


def _build(name: str) -> ResearchReport:
    return build_research_report(GOLDEN_CASES[name], _repository(), context=_context())


@pytest.mark.parametrize("name", sorted(GOLDEN_CASES))
def test_golden_json_matches(name: str) -> None:
    report = _build(name)
    expected = (GOLDEN / f"{name}.json").read_text(encoding="utf-8")
    assert render_report_json(report) == expected


@pytest.mark.parametrize("name", sorted(GOLDEN_CASES))
def test_golden_markdown_matches(name: str) -> None:
    report = _build(name)
    expected = (GOLDEN / f"{name}.md").read_text(encoding="utf-8")
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
    if decision.valuation_basis == "price_to_gross_profit":
        assert values["current_price_to_gross_profit"] == str(decision.current_price_to_gross_profit)
        assert values["target_price_to_gross_profit"] == str(decision.target_price_to_gross_profit)
    else:
        assert values["current_price_to_sales"] == str(decision.current_price_to_sales)
        assert values["target_price_to_sales"] == str(decision.target_price_to_sales)
    assert values["valuation_gap"] == str(decision.valuation_gap)
    assert values["target_weight"] == str(decision.target_weight)
    assert values["tier"] == decision.tier.value


def test_low_confidence_path_is_explicit() -> None:
    report = _build("theme_ranking")
    by_id = {subject.subject_id: subject for subject in report.subjects}

    ddog = by_id["issuer:ddog"].sections[0]
    assert ddog.availability is AvailabilityStatus.LOW_CONFIDENCE
    assert ddog.reason_codes == ("below_confidence_floor",)


def test_hard_excluded_path_is_explicit() -> None:
    """A non-confidence-floor exclusion must render as EXCLUDED with its reason code.

    #381 removed financial-issuer special-casing, so the shared strategy-run fixture no
    longer contains a naturally-occurring hard-excluded issuer (only below_confidence_floor,
    which maps to LOW_CONFIDENCE). Constructing the decision directly keeps this mapping
    branch covered without depending on another lane's fixture content.
    """
    decision = StrategyRunDecision(
        issuer_id="issuer:test-excluded",
        cutoff_at=CUTOFF,
        outcome=StrategyRunOutcome.EXCLUDED,
        eligible=False,
        exclusion_reason="valuation_inputs_unavailable",
    )
    section = _strategy_summary_section(decision, source="strategy_smoke_fixture", corpus_sha256="0" * 64)
    assert section.availability is AvailabilityStatus.EXCLUDED
    assert section.reason_codes == ("valuation_inputs_unavailable",)


def test_operating_efficiency_section_preserves_low_confidence_when_value_is_null() -> None:
    """The section's own availability must mirror the decision, not collapse to
    UNAVAILABLE just because this one field happens to be null (Copilot review on #383):
    a below-confidence-floor issuer with an unmaterialized labor-efficiency value must
    still surface as LOW_CONFIDENCE, not the more generic/less informative UNAVAILABLE."""
    decision = StrategyRunDecision(
        issuer_id="issuer:test-low-confidence-null-value",
        cutoff_at=CUTOFF,
        outcome=StrategyRunOutcome.EXCLUDED,
        eligible=False,
        exclusion_reason="below_confidence_floor",
        confidence=Decimal("0.5"),
        capital_adjusted_labor_efficiency=None,
    )
    section = _operating_efficiency_section(decision, source="strategy_smoke_fixture", corpus_sha256="0" * 64)
    assert section.availability is AvailabilityStatus.LOW_CONFIDENCE
    # The individual result's own value-availability still correctly reflects the null field.
    assert section.results[0].availability is AvailabilityStatus.UNAVAILABLE


def test_valuation_section_reports_the_basis_of_the_decision() -> None:
    """#1235: a definition 1.0.0 decision divides P/GP. The section names P/GP, reports the
    P/GP pair, still records P/S, and shows no P/S target, because none exists."""
    decision = StrategyRunDecision(
        issuer_id="issuer:wmt",
        cutoff_at=CUTOFF,
        outcome=StrategyRunOutcome.SELECTED,
        eligible=True,
        tier=ValuationTier.TRADITIONAL,
        current_price_to_sales=Decimal("1.2420"),
        current_price_to_gross_profit=Decimal("5.1302"),
        target_price_to_gross_profit=Decimal("8.7500"),
        valuation_gap=Decimal("0.7056"),
        rank=1,
        target_weight=Decimal("0.5"),
    )
    section = _valuation_section(decision, source="mart", corpus_sha256="0" * 64)
    assert section.title == "Valuation (three-tier P/GP)"
    assert [(item.label, item.value) for item in section.results] == [
        ("tier", "traditional"),
        ("current_price_to_gross_profit", "5.1302"),
        ("target_price_to_gross_profit", "8.7500"),
        ("current_price_to_sales", "1.2420"),
        ("valuation_gap", "0.7056"),
    ]
    with pytest.raises(ValidationError, match="one valuation basis"):
        StrategyRunDecision(
            issuer_id="issuer:wmt",
            cutoff_at=CUTOFF,
            outcome=StrategyRunOutcome.SELECTED,
            eligible=True,
            target_price_to_sales=Decimal("1.15"),
            target_price_to_gross_profit=Decimal("8.75"),
        )


def test_ranking_is_ordered_and_reproduces_ranks() -> None:
    report = _build("theme_ranking")
    ranked = [(s.subject_id, s.rank) for s in report.subjects]
    assert ranked[0] == ("issuer:nice", 1)
    assert ranked[1] == ("issuer:adm", 2)
    # Unranked members follow, sorted by issuer id.
    assert [s for s, _ in ranked[2:]] == ["issuer:ddog", "issuer:jpm", "issuer:shop"]


def test_section_filter_selects_only_requested_sections() -> None:
    request = ResearchReportRequest(
        report_kind=ResearchReportKind.COMPANY,
        target_entity_ids=("issuer:adm",),
        cutoff_at=CUTOFF,
        section_kinds=(ReportSectionKind.VALUATION,),
    )
    report = build_research_report(request, _repository(), context=_context())
    kinds = {section.section_kind for section in report.subjects[0].sections}
    assert kinds == {ReportSectionKind.VALUATION}


def test_missing_subject_is_reported_not_dropped() -> None:
    request = _request(ResearchReportKind.COMPANY, ("issuer:not_in_universe",), "Missing")
    report = build_research_report(request, _repository(), context=_context())
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

_BUILDER_ENTRY_POINT = "build_research_report"
# Documents the expected call graph; test_builder_contains_no_arithmetic_or_computation
# discovers the *actual* transitively-called module-level functions from
# _BUILDER_ENTRY_POINT and scans exactly that set, so a new helper invoked by the builder
# is always covered even if this constant is never updated (Copilot review on #383: a
# hard-coded scan list could be silently bypassed by a new unlisted helper).
_EXPECTED_BUILDER_FUNCTIONS = {
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


def _discover_transitively_called_functions(tree: ast.Module, entry_point: str) -> set[str]:
    """Every module-level function transitively reachable from `entry_point` by name-based
    calls (`foo(...)`), not `self.foo(...)`/`module.foo(...)` — those are ports/external
    APIs, not builder helpers to scan."""
    by_name = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    visited: set[str] = set()
    pending = [entry_point]
    while pending:
        name = pending.pop()
        if name in visited or name not in by_name:
            continue
        visited.add(name)
        for inner in ast.walk(by_name[name]):
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) and inner.func.id in by_name:
                pending.append(inner.func.id)
    return visited


def test_builder_contains_no_arithmetic_or_computation() -> None:
    """Statically proves `build_research_report` and every helper it transitively calls
    copy values through — they never do arithmetic or call a numeric/aggregation
    primitive."""
    source = inspect.getsource(research_report)
    tree = ast.parse(source)
    checked = _discover_transitively_called_functions(tree, _BUILDER_ENTRY_POINT)
    assert checked == _EXPECTED_BUILDER_FUNCTIONS, (
        f"builder call graph changed ({checked}); update _EXPECTED_BUILDER_FUNCTIONS after "
        "confirming the new/removed helper does not need boundary coverage"
    )
    by_name = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    for name in checked:
        node = by_name[name]
        for inner in ast.walk(node):
            assert not isinstance(inner, _FORBIDDEN_BINOPS), f"{name} contains arithmetic {type(inner).__name__}"
            assert not isinstance(inner, ast.AugAssign), f"{name} contains an augmented assignment"
            if isinstance(inner, ast.UnaryOp) and isinstance(inner.op, (ast.USub, ast.UAdd)):
                raise AssertionError(f"{name} contains unary arithmetic {type(inner.op).__name__}")
            if isinstance(inner, ast.Call):
                forbidden = _forbidden_call_name(inner)
                assert forbidden is None, f"{name} calls forbidden computation primitive {forbidden!r}"


# --- #42: PEG, supply chain and analyst sections come from mart, never a constant -----------


class _ServedModuleRows:
    """An issuer-module read that serves one row per module for `issuer:adm` only."""

    def __init__(self) -> None:
        from truealpha_contracts.issuer_modules_mart import AnalystRatings, SupplyChainExposure

        common = {
            "run_id": "capture-run:" + "a1" * 32,
            "cutoff": "2026-06-30T23:59:59Z",
            "confidence": "0.7700",
            "availability_status": "available",
            "source_evidence_status": "verified",
            "factor_validation_status": "not_evaluated",
        }
        self.supply = SupplyChainExposure(
            **common,
            reason_codes=("edge_confidence_floor",),
            extractor="graph:kg-edges:v1",
            exposure_score="0.4200",
            direct_partners=3,
            suppliers_count=2,
            customers_count=1,
            max_partner_share="0.3100",
        )
        self.analyst = AnalystRatings(
            **common,
            reason_codes=(),
            extractor="origin:moomoo:v1",
            consensus_rating="4.3500",
            target_price=None,
            analysts_count=20,
            buy_count=14,
            hold_count=5,
            sell_count=1,
        )

    def supply_chain_exposure(self, issuer_id: str):  # type: ignore[no-untyped-def]
        return self.supply if issuer_id == "issuer:adm" else None

    def analyst_ratings(self, issuer_id: str):  # type: ignore[no-untyped-def]
        return self.analyst if issuer_id == "issuer:adm" else None


class _FailingModuleRows:
    def supply_chain_exposure(self, issuer_id: str) -> None:
        import psycopg

        raise psycopg.OperationalError("connection refused")

    def analyst_ratings(self, issuer_id: str) -> None:
        import psycopg

        raise psycopg.OperationalError("connection refused")


def _company_sections(modules: object) -> dict[ReportSectionKind, ReportSection]:
    repository = MartResearchReadRepository(
        strategy_repository=FixtureStrategyRunRepository(),
        issuer_modules=modules,  # type: ignore[arg-type]
    )
    report = build_research_report(GOLDEN_CASES["company_adm"], repository, context=_context())
    return {section.section_kind: section for section in report.subjects[0].sections}


def test_no_company_section_reads_not_materialized() -> None:
    """The defect of #42: three sections were constants. No reason code may claim a module
    is not materialized when the reader has its mart output."""
    for section in _company_sections(_ServedModuleRows()).values():
        assert not any(code.endswith("_not_materialized") for code in section.reason_codes), section


def test_module_sections_carry_the_served_row_byte_exact() -> None:
    modules = _ServedModuleRows()
    sections = _company_sections(modules)

    supply = sections[ReportSectionKind.SUPPLY_CHAIN]
    assert supply.availability is AvailabilityStatus.AVAILABLE
    assert supply.reason_codes == ("edge_confidence_floor",)
    assert [(r.label, r.value) for r in supply.results] == [
        ("exposure_score", "0.4200"),
        ("direct_partners", "3"),
        ("suppliers_count", "2"),
        ("customers_count", "1"),
        ("max_partner_share", "0.3100"),
    ]
    for result in supply.results:
        assert result.cutoff_at == datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)
        assert result.confidence == Decimal("0.7700")
        assert result.factor_version == "graph:kg-edges:v1"
        assert result.trace is not None
        assert result.trace.reference_id == f"mart.issuer_supply_chain_exposure:{modules.supply.run_id}:issuer:adm"

    analyst = sections[ReportSectionKind.ANALYST_HISTORY]
    values = {r.label: r for r in analyst.results}
    assert values["consensus_rating"].value == "4.3500"
    assert values["buy_count"].value == "14"
    # A null value is unavailable on its own; the section keeps the row's status.
    assert values["target_price"].value is None
    assert values["target_price"].availability is AvailabilityStatus.UNAVAILABLE
    assert analyst.availability is AvailabilityStatus.AVAILABLE


def test_a_missing_served_row_and_a_failed_read_are_different_answers() -> None:
    absent = _company_sections(_NoServedModuleRows())
    failed = _company_sections(_FailingModuleRows())
    for kind in (ReportSectionKind.SUPPLY_CHAIN, ReportSectionKind.ANALYST_HISTORY):
        assert absent[kind].availability is AvailabilityStatus.UNAVAILABLE
        assert absent[kind].reason_codes == ("no_row_in_served_head",)
        assert failed[kind].availability is AvailabilityStatus.ERROR
        assert failed[kind].reason_codes == ("mart_read_failed",)


def _decision(**overrides: object) -> StrategyRunDecision:
    fields: dict[str, object] = {
        "issuer_id": "issuer:test",
        "cutoff_at": CUTOFF,
        "outcome": StrategyRunOutcome.SELECTED,
        "eligible": True,
        "rank": 1,
        "confidence": Decimal("0.90"),
    }
    fields.update(overrides)
    return StrategyRunDecision(**fields)  # type: ignore[arg-type]


def test_peg_section_reads_the_decision() -> None:
    from truealpha_contracts.research_report_mart import _peg_section

    recorded = _peg_section(_decision(peg=Decimal("1.2300"), peg_rank=4), "mart", "0" * 64)
    assert recorded.availability is AvailabilityStatus.AVAILABLE
    assert [(r.label, r.value) for r in recorded.results] == [("peg", "1.2300"), ("peg_rank", "4")]
    assert recorded.reason_codes == ()

    missing = _peg_section(_decision(), "mart", "0" * 64)
    assert missing.availability is AvailabilityStatus.UNAVAILABLE
    assert missing.reason_codes == ("peg_not_recorded",)

    excluded = _peg_section(
        _decision(outcome=StrategyRunOutcome.EXCLUDED, eligible=False, rank=None, exclusion_reason="no_price"),
        "mart",
        "0" * 64,
    )
    assert excluded.availability is AvailabilityStatus.EXCLUDED, "an excluded decision stays visible"


def test_the_mart_reader_has_no_default_source() -> None:
    """No hidden fixture: without a database URL the reader needs both sources injected."""
    with pytest.raises(TypeError):
        MartResearchReadRepository(strategy_repository=FixtureStrategyRunRepository())
    with pytest.raises(TypeError):
        MartResearchReadRepository(issuer_modules=_NoServedModuleRows())


def test_a_served_row_later_than_the_report_cutoff_is_not_shown() -> None:
    """Point in time: a report anchored at 2026-06-30 never shows a module row of 2026-09-30."""
    modules = _ServedModuleRows()
    later = {"cutoff": "2026-09-30T23:59:59Z"}
    modules.supply = modules.supply.model_copy(update=later)
    modules.analyst = modules.analyst.model_copy(update=later)
    sections = _company_sections(modules)
    for kind in (ReportSectionKind.SUPPLY_CHAIN, ReportSectionKind.ANALYST_HISTORY):
        assert sections[kind].availability is AvailabilityStatus.UNAVAILABLE
        assert sections[kind].reason_codes == ("served_row_after_report_cutoff",)
        assert sections[kind].results == ()
