"""Prompt-driven Xiaohongshu 1080x1440 HTML card generation via `glm-5.3-flash` and deterministic
fact-preservation verifier (#44).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from data_engine.config import settings
from data_engine.sources import gateway
from data_engine.sources.llm import XHS_CARD_TASK, generate_xhs_card_html
from truealpha_contracts.execution import AvailabilityStatus, FactorValidationStatus
from truealpha_contracts.research_cards import CardKind, build_card, render_card_html, verify_card_html
from truealpha_contracts.research_report import (
    EvidenceTrace,
    ReportSection,
    ReportSectionKind,
    ReportSubject,
    ResearchReport,
    ResearchReportKind,
    ResultValue,
)

CUTOFF = datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)


class _Conn:
    def __init__(self, replay_row=None):
        self.replay_row = replay_row
        self.inserts: list[tuple] = []

    def execute(self, sql, params=()):
        text = " ".join(sql.split())
        if text.startswith("select pg_advisory_xact_lock("):
            return _R([(None,)])
        if text.startswith("select invocation_id, decision"):
            return _R([self.replay_row] if self.replay_row else [])
        if text.startswith("insert into staging.model_invocations"):
            self.inserts.append(params)
            return _R([])
        raise AssertionError(text[:60])


class _R:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


def _sample_card():
    report = ResearchReport.assemble(
        report_kind=ResearchReportKind.COMPANY,
        title="Apple Inc. Research Report",
        cutoff_at=CUTOFF,
        generated_from="mart:strategy_run:test",
        subjects=(
            ReportSubject(
                subject_id="issuer:aapl",
                display_name="AAPL · Apple Inc.",
                sections=(
                    ReportSection(
                        section_kind=ReportSectionKind.VALUATION,
                        title="Valuation",
                        availability=AvailabilityStatus.AVAILABLE,
                        validation_status=FactorValidationStatus.ACCEPTED,
                        results=(
                            ResultValue(
                                label="valuation_gap",
                                value="-0.2415",
                                period="2026-06-30",
                                cutoff_at=CUTOFF,
                                availability=AvailabilityStatus.AVAILABLE,
                                confidence=Decimal("0.92"),
                                factor_version="large_model_value_v0",
                                trace=EvidenceTrace(reference_id="mart:aapl:2026-06-30"),
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )
    return build_card(report, CardKind.COMPANY)


def _provider_payload(html: str, served: str = "glm-5.3-flash") -> bytes:
    content = json.dumps({"html": html})
    body = {
        "model": served,
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 420, "completion_tokens": 680, "total_tokens": 1100},
    }
    return json.dumps(body).encode()


@pytest.fixture
def seated(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_model", "glm-5.3-flash")
    ledger = gateway.MemoryLedger()
    previous = gateway.set_writer(ledger)
    yield ledger
    gateway.set_writer(previous)


def test_generate_xhs_card_html_accepts_verified_glm_output(seated) -> None:
    card = _sample_card()
    valid_html = render_card_html(card)
    conn = _Conn()
    outcome = generate_xhs_card_html(
        conn,
        card=card,
        transport=lambda url, headers, body: (200, _provider_payload(valid_html)),
    )
    assert outcome.render_mode == "model_glm"
    assert outcome.model == "glm-5.3-flash"
    assert outcome.prompt_sha256 == XHS_CARD_TASK.prompt_sha256
    ok, violations = verify_card_html(card, outcome.html)
    assert ok and violations == ()
    assert len(conn.inserts) == 1
    assert len(seated) == 1


def test_generate_xhs_card_html_falls_back_when_model_mutates_fact(seated) -> None:
    card = _sample_card()
    valid_html = render_card_html(card)
    corrupted_html = valid_html.replace("-0.2415", "+9.9999")
    conn = _Conn()
    outcome = generate_xhs_card_html(
        conn,
        card=card,
        transport=lambda url, headers, body: (200, _provider_payload(corrupted_html)),
    )
    assert outcome.render_mode == "deterministic_fallback"
    ok, violations = verify_card_html(card, outcome.html)
    assert ok and violations == ()
    assert "-0.2415" in outcome.html


def test_generate_xhs_card_html_falls_back_when_model_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_api_key", "")
    card = _sample_card()
    conn = _Conn()
    outcome = generate_xhs_card_html(conn, card=card)
    assert outcome.render_mode == "deterministic_fallback"
    ok, violations = verify_card_html(card, outcome.html)
    assert ok and violations == ()
