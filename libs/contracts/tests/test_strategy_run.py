from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from truealpha_contracts.access import AccessContext, AuthenticationMethod, PrincipalKind
from truealpha_contracts.research import ValuationTier
from truealpha_contracts.strategy_run import (
    StrategyRunDecision,
    StrategyRunOutcome,
    StrategyRunReport,
    StrategyRunUnavailable,
)
from truealpha_contracts.strategy_run_fixture import FixtureStrategyRunRepository

FIXTURE_PATH = Path(__file__).parents[1] / "src" / "truealpha_contracts" / "data" / "strategy_run_preview.v1.json"


def _context(*, expired: bool = False) -> AccessContext:
    now = datetime.now(UTC)
    return AccessContext(
        context_id="ctx:test",
        principal_id="principal:test",
        tenant_id="tenant:test",
        session_id="session:test",
        authentication_method=AuthenticationMethod.SERVICE_IDENTITY,
        principal_kind=PrincipalKind.SERVICE,
        issued_at=now - timedelta(hours=2) if expired else now,
        expires_at=now - timedelta(hours=1) if expired else now + timedelta(hours=1),
    )


def test_decision_rejects_naive_cutoff() -> None:
    with pytest.raises(ValidationError):
        StrategyRunDecision(
            issuer_id="issuer:adm",
            cutoff_at=datetime(2026, 3, 31, 23, 59, 59),  # noqa: DTZ001 - intentionally naive
            outcome=StrategyRunOutcome.SELECTED,
            eligible=True,
        )


def test_decision_parses_zulu_string_cutoff() -> None:
    decision = StrategyRunDecision(
        issuer_id="issuer:adm",
        cutoff_at="2026-03-31T23:59:59Z",
        outcome=StrategyRunOutcome.SELECTED,
        eligible=True,
        confidence=Decimal("0.9"),
    )
    assert decision.cutoff_at == datetime(2026, 3, 31, 23, 59, 59, tzinfo=UTC)


def test_report_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        StrategyRunReport(
            strategy_id="large_model_value_v0",
            corpus_sha256="0" * 64,
            decisions=(),
            unknown_field="nope",
        )


def test_confidence_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        StrategyRunDecision(
            issuer_id="issuer:adm",
            cutoff_at="2026-03-31T23:59:59Z",
            outcome=StrategyRunOutcome.EXCLUDED,
            eligible=False,
            confidence=Decimal("1.5"),
        )


def test_canonical_fixture_is_committed_and_deterministic() -> None:
    payload = json.loads(FIXTURE_PATH.read_bytes())
    assert payload["strategy_id"] == "large_model_value_v0"
    assert payload["source"] == "strategy_smoke_fixture"
    assert payload["golden_mismatches"] == []
    assert "generated_at" not in payload
    assert len(payload["decisions"]) == 10


def test_fixture_repository_returns_report_matching_committed_bytes() -> None:
    payload = json.loads(FIXTURE_PATH.read_bytes())
    repository = FixtureStrategyRunRepository()
    report = repository.get_latest(strategy_id="large_model_value_v0", context=_context())

    assert isinstance(report, StrategyRunReport)
    assert report.corpus_sha256 == payload["corpus_sha256"]
    assert len(report.decisions) == len(payload["decisions"])

    selected = next(d for d in report.decisions if d.issuer_id == "issuer:adm" and d.cutoff_at.month == 3)
    expected = next(
        d for d in payload["decisions"] if d["issuer_id"] == "issuer:adm" and d["cutoff_at"].startswith("2026-03")
    )
    assert selected.outcome.value == expected["outcome"]
    assert selected.tier == ValuationTier(expected["tier"])
    assert str(selected.valuation_gap) == expected["valuation_gap"]
    assert str(selected.confidence) == expected["confidence"]
    assert selected.rank == expected["rank"]
    assert str(selected.target_weight) == expected["target_weight"]

    excluded = next(d for d in report.decisions if d.exclusion_reason == "below_confidence_floor")
    assert excluded.eligible is False
    assert excluded.confidence is not None


def test_fixture_repository_returns_unavailable_for_unknown_strategy() -> None:
    repository = FixtureStrategyRunRepository()
    result = repository.get_latest(strategy_id="does_not_exist", context=_context())

    assert isinstance(result, StrategyRunUnavailable)
    assert result.reason == "unknown_strategy_id"
    assert result.strategy_id == "does_not_exist"


def test_fixture_repository_does_not_evaluate_context() -> None:
    """The provisional adapter makes no authorization decision (see #347)."""
    repository = FixtureStrategyRunRepository()
    expired = repository.get_latest(strategy_id="large_model_value_v0", context=_context(expired=True))
    assert isinstance(expired, StrategyRunReport)
