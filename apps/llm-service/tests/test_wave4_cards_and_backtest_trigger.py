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
from truealpha_runtime.research_cards import (
    CardKind,
    CardSubject,
    ClaimClass,
    ResearchCard,
    verify_card_html,
)
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

    etf_html = "<!DOCTYPE html><html><body>Invesco QQQ Trust 0.14 1000000 Research output only.</body></html>"
    sc_html = "<!DOCTYPE html><html><body>NVDA scenario_only 0.25 Research output only.</body></html>"
    monkeypatch.setattr(
        library_service,
        "_lookup_governed_card_artifact",
        lambda **k: (etf_html if k.get("card_kind") == "etf" else sc_html, "model_glm"),
    )
    monkeypatch.setattr(library_service, "verify_card_html", lambda *_a, **_k: (True, ()))
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
        database_url="postgresql://test",
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
        database_url="postgresql://test",
    )
    assert sc_card.card_kind == "supply_chain"
    assert sc_card.rendered_html and "scenario_only" in sc_card.rendered_html
    assert seen_requests[-1].report_kind is ResearchReportKind.COMPANY

    monkeypatch.setattr(library_service, "_lookup_governed_card_artifact", lambda **_k: None)
    with pytest.raises(LookupError):
        library_service.export_or_render_card(
            ctx,
            library_service.CardExportRequest(
                card_kind=CardKind.ETF,
                issuer_id="etf:qqq",
                cutoff=cutoff.isoformat(),
                persist=False,
            ),
            research_repo=_FakeRepo(),
        )


def test_entity_detail_reads_materialized_geometric_cagr_peg_node() -> None:
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
        entities_service.TraceNodeValueDto(node_key="peg_geometric_cagr", value="0.6842", confidence="0.80"),
        entities_service.TraceNodeValueDto(node_key="peg_geometric_cagr_rank", value="3", confidence="0.80"),
    ]
    out = entities_service._apply_entity_peg_convention([decision], nodes, "geometric_cagr")
    assert out[0].peg == "0.6842"
    assert out[0].peg_rank == 3
    assert out[0].peg_reason_codes == []


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
            json={
                "definition_resource": "large_model_value_v0_definition_1_1_0.json",
                "idempotency_key": "bt-key-1",
                "release_manifest_id": "release-manifest:test-1",
            },
        )
        from truealpha_contracts.strategy import load_live_strategy_definition

        custom_def_dict = load_live_strategy_definition().model_dump(mode="json")
        custom_resp = TestClient(app).post(
            "/api/v1/strategy/backtests/trigger",
            json={
                "custom_definition": custom_def_dict,
                "idempotency_key": "bt-custom-1",
            },
        )
        bad_range = TestClient(app).post(
            "/api/v1/strategy/backtests/trigger",
            json={"start_cutoff": "2026-06-01T00:00:00Z", "end_cutoff": "2025-01-01T00:00:00Z"},
        )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 202
    assert custom_resp.status_code == 202
    assert bad_range.status_code == 400
    body = resp.json()
    assert body["request_id"] == 77
    assert body["dedupe_key"] == "bt-key-1"
    assert body["job_name"] == "nightly_backtest_pipeline"
    assert body["run_key"] == f"manual:{body['dedupe_key']}"
    assert body["release_manifest_id"] == "release-manifest:test-1"
    assert body["exploratory"] is False
    custom_body = custom_resp.json()
    assert custom_body["custom_definition_sha256"] and len(custom_body["custom_definition_sha256"]) == 64
    assert custom_body["exploratory"] is True
    assert any("staging.pipeline_trigger_requests" in sql for sql, _ in recorded)


def test_verify_card_html_accepts_localized_labels_and_rejects_altered_numbers() -> None:
    cutoff = datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)
    card = ResearchCard(
        card_kind=CardKind.RANKING,
        title="Ranking Card",
        cutoff_at=cutoff,
        generated_from_report_id="report:test:1",
        research_risk_note="Research output only.",
        source_attribution="TrueAlpha Test Source",
        subjects=(
            CardSubject(
                subject_id="AAPL",
                display_name="AAPL · Apple Inc.",
                availability=AvailabilityStatus.AVAILABLE,
                claim_class=ClaimClass.EMPIRICALLY_VALIDATED,
                metrics=(
                    ResultValue(
                        label="p_gp",
                        value="18.25",
                        cutoff_at=cutoff,
                        availability=AvailabilityStatus.AVAILABLE,
                    ),
                    ResultValue(
                        label="tier",
                        value="tier_1",
                        cutoff_at=cutoff,
                        availability=AvailabilityStatus.AVAILABLE,
                    ),
                ),
            ),
        ),
    )
    localized_html = (
        "<!DOCTYPE html><html><body style='width:1080px;height:1440px'>"
        "<article><h1>Ranking Card</h1>"
        "<p>AAPL · Apple Inc. · available · empirically_validated</p>"
        "<p>当前市值/毛利 (p_gp): 18.25</p>"
        f"<p>tier: tier_1 · 2026-06-30 · {card.card_id[:12]}</p>"
        "<p>TrueAlpha Test Source · Research output only.</p>"
        "</article></body></html>"
    )
    ok, violations = verify_card_html(card, localized_html)
    assert ok, violations
    tampered_html = localized_html.replace("18.25", "19.99")
    ok_bad, bad_violations = verify_card_html(card, tampered_html)
    assert not ok_bad
    assert "missing_metric_value:AAPL:p_gp:18.25" in bad_violations


def test_backtest_trigger_cli_uses_shared_contract(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from truealpha_tools import backtest_trigger as cli

    seen: list[str] = []

    class _Cur:
        def execute(self, sql: str, params: Any = None) -> None:
            seen.append(sql)

        def fetchone(self) -> tuple[int]:
            return (99,)

        def __enter__(self) -> _Cur:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

    class _Conn:
        def cursor(self) -> _Cur:
            return _Cur()

        def commit(self) -> None:
            return None

        def __enter__(self) -> _Conn:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

    monkeypatch.setattr(cli.psycopg, "connect", lambda *_a, **_k: _Conn())
    rc = cli.main(
        [
            "--definition-resource",
            "large_model_value_v0_definition_1_1_0.json",
            "--release-manifest-id",
            "release-manifest:cli-1",
            "--idempotency-key",
            "cli-bt-1",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["request_id"] == 99 and payload["dedupe_key"] == "cli-bt-1"
    assert payload["release_manifest_id"] == "release-manifest:cli-1" and payload["exploratory"] is False
    assert seen and "staging.pipeline_trigger_requests" in seen[0]


def test_fetch_issuer_basic_info_falls_back_to_cik_when_duplicate_uuid_is_deduplicated() -> None:
    seen: list[tuple[str, Any]] = []

    class _FakeCur:
        def execute(self, sql: str, params: Any = None) -> None:
            seen.append((sql, params))

        def fetchone(self) -> dict[str, Any]:
            return {
                "canonical_name": "Exxon Mobil Corp.",
                "primary_ticker": "XOM",
                "primary_exchange": "NYSE",
                "cik": "0002115436",
                "sic": "2911",
                "sic_description": "Petroleum Refining",
                "sector_gics_or_sic": "Petroleum Refining",
                "fiscal_year_end": "1231",
                "state_of_incorporation": "NJ",
                "entity_type": "operating",
                "market_cap": "644843483947.52",
                "market_cap_confidence": "0.80",
                "shares_outstanding": "4079222444",
                "close_price": "158.08",
                "as_of_cutoff": "2026-11-26",
            }

    assert entities_service._extract_cik_candidates(
        ["720a8508-ada0-50ba-bfeb-0d563cae8dbb", "issuer:cik:0002115436", "company:cik:2115436"]
    ) == ["0002115436"]
    dto = entities_service._fetch_issuer_basic_info_from_cursor(
        _FakeCur(), ["720a8508-ada0-50ba-bfeb-0d563cae8dbb", "issuer:cik:0002115436"]
    )
    assert dto is not None and dto.primary_ticker == "XOM" and dto.cik == "0002115436"
    assert "cik = any" in seen[0][0] and seen[0][1][1] == ["0002115436"]
