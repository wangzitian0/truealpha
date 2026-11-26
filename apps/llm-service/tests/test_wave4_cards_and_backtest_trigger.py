"""Falsifiable Wave 4 tests for XHS cards, universe normalization, geometric CAGR Entity 360, and backtest trigger."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from llm_service.api.v1 import _deps
from llm_service.main import app
from llm_service.services import auth_service, entities_service, library_service, strategy_service
from truealpha_contracts.execution import AvailabilityStatus
from truealpha_runtime.research_cards import CardKind
from truealpha_runtime.research_report import (
    ReportSection,
    ReportSectionKind,
    ReportSubject,
    ResearchReportKind,
    ResearchReportRequest,
    ResultValue,
)

pytestmark = [pytest.mark.contract, pytest.mark.deps]


def test_normalize_ranking_universe_accepts_universe_list_and_prefix_ids() -> None:
    assert strategy_service.normalize_ranking_universe("universe-list:qqq") == "qqq"
    assert strategy_service.normalize_ranking_universe("universe-list:topt") == "topt"
    assert strategy_service.normalize_ranking_universe("universe:qqq-us-2026") == "qqq"
    assert strategy_service.normalize_ranking_universe("all") == "all"
    assert strategy_service.normalize_ranking_universe("nonexistent") is None


def test_export_or_render_card_routes_etf_and_supply_chain_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    cutoff = datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)
    seen_requests: list[ResearchReportRequest] = []

    class _FakeRepo:
        provenance_label = "test"
        _database_url = None

        def load_subjects(self, *, request: ResearchReportRequest, context: Any) -> tuple[ReportSubject, ...]:
            del context
            seen_requests.append(request)
            if request.report_kind is ResearchReportKind.ETF:
                return (
                    ReportSubject(
                        subject_id="etf:qqq",
                        display_name="Invesco QQQ Trust",
                        sections=(
                            ReportSection(
                                section_kind=ReportSectionKind.ETF_VIRTUAL_COMPANY,
                                title="ETF virtual company",
                                availability=AvailabilityStatus.AVAILABLE,
                                results=(
                                    ResultValue(
                                        label="weighted_valuation_gap",
                                        value="0.14",
                                        cutoff_at=cutoff,
                                        availability=AvailabilityStatus.AVAILABLE,
                                    ),
                                    ResultValue(
                                        label="virtual_revenue",
                                        value="1000000",
                                        cutoff_at=cutoff,
                                        availability=AvailabilityStatus.AVAILABLE,
                                    ),
                                ),
                            ),
                        ),
                    ),
                )
            return (
                ReportSubject(
                    subject_id="NVDA",
                    display_name="NVDA · NVIDIA",
                    sections=(
                        ReportSection(
                            section_kind=ReportSectionKind.SUPPLY_CHAIN,
                            title="Supply-chain exposure",
                            availability=AvailabilityStatus.AVAILABLE,
                            results=(
                                ResultValue(
                                    label="exposure_score",
                                    value="0.25",
                                    cutoff_at=cutoff,
                                    availability=AvailabilityStatus.AVAILABLE,
                                ),
                            ),
                        ),
                    ),
                ),
            )

    monkeypatch.setattr(library_service, "_lookup_governed_card_artifact", lambda **_k: None)
    principal = auth_service.PrincipalRecord(principal_id="p:1", tenant_id="t:1", principal_kind="member")
    ctx = auth_service.access_context_for(principal)

    etf_card = library_service.export_or_render_card(
        ctx,
        library_service.CardExportRequest(
            card_kind=CardKind.ETF,
            issuer_id="etf:qqq",
            cutoff=cutoff.isoformat(),
            persist=False,
        ),
        research_repo=_FakeRepo(),
    )
    assert etf_card.card_kind == "etf"
    assert etf_card.rendered_html and "Invesco QQQ Trust" in etf_card.rendered_html
    assert seen_requests[-1].report_kind is ResearchReportKind.ETF

    sc_card = library_service.export_or_render_card(
        ctx,
        library_service.CardExportRequest(
            card_kind=CardKind.SUPPLY_CHAIN,
            issuer_id="NVDA",
            cutoff=cutoff.isoformat(),
            persist=False,
        ),
        research_repo=_FakeRepo(),
    )
    assert sc_card.card_kind == "supply_chain"
    assert sc_card.rendered_html and "scenario_only" in sc_card.rendered_html
    assert seen_requests[-1].report_kind is ResearchReportKind.COMPANY


def test_entity_detail_computes_geometric_cagr_peg_across_loss_year() -> None:
    decision = strategy_service.StrategyDecisionItemDto(
        issuer_id="issuer:cik:0001",
        label="TEST",
        cutoff_at="2026-04-01T00:00:00Z",
        outcome="eligible",
        eligible=True,
        peg=None,
        peg_reason_codes=["loss_year_in_growth_window"],
    )
    nodes = [
        entities_service.TraceNodeValueDto(node_key="last_close", value="100", confidence="0.90"),
        entities_service.TraceNodeValueDto(node_key="shares_outstanding", value="10", confidence="0.90"),
        entities_service.TraceNodeValueDto(node_key="net_income", period="2022-12-31", value="100", confidence="0.80"),
        entities_service.TraceNodeValueDto(node_key="net_income", period="2023-12-31", value="-50", confidence="0.70"),
        entities_service.TraceNodeValueDto(node_key="net_income", period="2024-12-31", value="120", confidence="0.85"),
        entities_service.TraceNodeValueDto(node_key="net_income", period="2025-12-31", value="150", confidence="0.90"),
    ]
    out = entities_service._apply_entity_peg_convention([decision], nodes, "geometric_cagr")
    assert out[0].peg is not None
    assert "growth_convention:geometric_cagr" in out[0].peg_reason_codes


def test_backtest_trigger_endpoint_requires_admin_and_inserts_into_pipeline_trigger_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[tuple[str, Any]] = []

    class _FakeCur:
        def execute(self, sql: str, params: Any = None) -> None:
            recorded.append((sql, params))

        def fetchone(self) -> dict[str, int]:
            return {"request_id": 77}

    monkeypatch.setattr(strategy_service, "with_app_runtime", lambda fn, **_k: fn(_FakeCur()))
    member = auth_service.PrincipalRecord(principal_id="p:mem", tenant_id="t:1", principal_kind="member")
    admin = auth_service.PrincipalRecord(principal_id="p:adm", tenant_id="t:1", principal_kind="administrator")
    app.dependency_overrides[_deps.require_principal] = lambda: member
    try:
        denied = TestClient(app).post(
            "/api/v1/strategy/backtests/trigger",
            json={"definition_resource": "large_model_value_v0_definition_1_1_0.json"},
        )
    finally:
        app.dependency_overrides.clear()
    assert denied.status_code == 403

    app.dependency_overrides[_deps.require_admin] = lambda: admin
    try:
        resp = TestClient(app).post(
            "/api/v1/strategy/backtests/trigger",
            json={"definition_resource": "large_model_value_v0_definition_1_1_0.json"},
        )
        bad_range = TestClient(app).post(
            "/api/v1/strategy/backtests/trigger",
            json={"start_cutoff": "2026-06-01T00:00:00Z", "end_cutoff": "2025-01-01T00:00:00Z"},
        )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 202
    assert bad_range.status_code == 400
    body = resp.json()
    assert body["request_id"] == 77
    assert body["job_name"] == "nightly_backtest_pipeline"
    assert body["run_key"] == f"manual:{body['dedupe_key']}"
    assert any("staging.pipeline_trigger_requests" in sql for sql, _ in recorded)
