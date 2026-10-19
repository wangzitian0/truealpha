"""Unit tests for tools/vision_audit.py (Issue #28 and Issue #54).

Asserts that vision-audit accurately evaluates gate statuses against
nightly verdicts and health endpoints.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from truealpha_runtime.testing import load_tool

_module = load_tool("vision_audit")
audit_vision = _module.audit_vision
GateAuditResult = _module.GateAuditResult
VisionAuditReport = _module.VisionAuditReport


class _FakeVerdict:
    def __init__(self, check: str, ok: bool | None, summary: str) -> None:
        self.check = check
        self.ok = ok
        self.summary = summary
        self.ran_at = datetime(2026, 10, 9, 21, 30, tzinfo=UTC)


class _FakeHealthReport:
    def __init__(
        self,
        verdicts: list[Any],
        release: str | None = "v0.1.2",
        governed_pointers: list[dict[str, Any]] | str | None = None,
    ) -> None:
        self.verdicts = verdicts
        self.release = release
        self.governed_pointers = (
            [
                {"universe_id": "universe:topt-us-2026-03-31", "freshness": "fresh", "availability": "available"},
                {"universe_id": "universe:qqq-us-2026-03-31", "freshness": "fresh", "availability": "available"},
            ]
            if governed_pointers is None
            else governed_pointers
        )


def test_vision_audit_reports_all_gates_verified_when_all_verdicts_are_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_verdicts = [
        _FakeVerdict("release_fetch_proof", True, "capture ok"),
        _FakeVerdict("report_surface_proof", True, "5/5 surfaces ok"),
        _FakeVerdict("output_invariants", True, "6 held, 0 empty"),
        _FakeVerdict("question_coverage@universe-list:qqq", True, "coverage ok"),
        _FakeVerdict("question_coverage@topt", True, "coverage ok"),
        _FakeVerdict("nightly_backtest", True, "backtest executed ok"),
        _FakeVerdict("market_data_freshness", True, "freshness within 72h"),
    ]

    monkeypatch.setattr(
        _module,
        "read_report",
        lambda _url, _http_get: _FakeHealthReport(fake_verdicts, "v0.1.2"),
    )

    report: VisionAuditReport = audit_vision(env="production")
    assert report.all_passed is True
    assert report.status == "pass"
    assert all(gate.verified is True for gate in report.gates)
    assert {gate.gate_id for gate in report.gates} == {"#56", "#29", "#30", "#31", "#54"}


def test_vision_audit_refuses_pass_when_served_head_freshness_is_stale_or_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_verdicts = [
        _FakeVerdict("release_fetch_proof", True, "capture ok"),
        _FakeVerdict("report_surface_proof", True, "5/5 surfaces ok"),
        _FakeVerdict("output_invariants", True, "6 held, 0 empty"),
        _FakeVerdict("question_coverage@universe-list:qqq", True, "coverage ok"),
        _FakeVerdict("question_coverage@topt", True, "coverage ok"),
        _FakeVerdict("nightly_backtest", True, "backtest executed ok"),
        _FakeVerdict("market_data_freshness", True, "freshness within 72h"),
    ]
    stale_pointers = [
        {"universe_id": "universe:topt-us-2026-03-31", "freshness": "stale", "availability": "available"},
    ]

    monkeypatch.setattr(
        _module,
        "read_report",
        lambda _url, _http_get: _FakeHealthReport(fake_verdicts, "v0.1.2", stale_pointers),
    )

    report: VisionAuditReport = audit_vision(env="production")
    assert report.all_passed is False
    assert report.status == "fail"
    g4 = next(g for g in report.gates if g.gate_id == "#54")
    assert g4.verified is False
    assert g4.status == "OPEN"
    assert "mart.served_head.freshness!=fresh" in g4.evidence

    # Fail-closed when governed_pointers is absent (None)
    missing_report = _FakeHealthReport(fake_verdicts, "v0.1.2")
    missing_report.governed_pointers = None
    monkeypatch.setattr(
        _module,
        "read_report",
        lambda _url, _http_get: missing_report,
    )
    report_missing: VisionAuditReport = audit_vision(env="production")
    assert report_missing.all_passed is False
    assert report_missing.status == "fail"

    # Manual-only unscheduled QQQ pointer in staging does not fail when TOPT is fresh (#1256)
    staging_pointers = [
        {"universe_id": "universe:topt-us-2026-03-31", "freshness": "fresh", "availability": "available"},
        {"universe_id": "universe:qqq-us-2026-09-30", "freshness": "stale", "availability": "available"},
    ]
    monkeypatch.setattr(
        _module,
        "read_report",
        lambda _url, _http_get: _FakeHealthReport(fake_verdicts, "v0.1.2", staging_pointers),
    )
    report_staging: VisionAuditReport = audit_vision(env="staging")
    assert report_staging.all_passed is True
    assert report_staging.status == "pass"


def test_vision_audit_fails_gate_3_when_nightly_backtest_verdict_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_verdicts = [
        _FakeVerdict("release_fetch_proof", True, "capture ok"),
        _FakeVerdict("report_surface_proof", True, "5/5 surfaces ok"),
        _FakeVerdict("output_invariants", True, "6 held, 0 empty"),
        _FakeVerdict("question_coverage@universe-list:qqq", True, "coverage ok"),
        _FakeVerdict("question_coverage@topt", True, "coverage ok"),
        _FakeVerdict("market_data_freshness", True, "freshness within 72h"),
    ]

    monkeypatch.setattr(
        _module,
        "read_report",
        lambda _url, _http_get: _FakeHealthReport(fake_verdicts, "v0.1.2"),
    )

    report: VisionAuditReport = audit_vision(env="production")
    assert report.all_passed is False
    assert report.status == "fail"
    g3 = next(g for g in report.gates if g.gate_id == "#31")
    assert g3.verified is False
    assert g3.status == "OPEN"
    assert "nightly_backtest: no verdict recorded at all" in g3.evidence


def test_vision_audit_fails_gate_1_when_report_surface_proof_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_verdicts = [
        _FakeVerdict("release_fetch_proof", True, "capture ok"),
        _FakeVerdict("report_surface_proof", False, "4/5 surfaces serve governed head"),
        _FakeVerdict("output_invariants", True, "6 held, 0 empty"),
        _FakeVerdict("question_coverage@universe-list:qqq", True, "coverage ok"),
        _FakeVerdict("question_coverage@topt", True, "coverage ok"),
        _FakeVerdict("nightly_backtest", True, "backtest executed ok"),
        _FakeVerdict("market_data_freshness", True, "freshness within 72h"),
    ]

    monkeypatch.setattr(
        _module,
        "read_report",
        lambda _url, _http_get: _FakeHealthReport(fake_verdicts, "v0.1.2"),
    )

    report: VisionAuditReport = audit_vision(env="staging")
    assert report.all_passed is False
    assert report.status == "fail"
    g1 = next(g for g in report.gates if g.gate_id == "#29")
    assert g1.verified is False
    assert g1.status == "OPEN"
