from __future__ import annotations

import importlib.util
import json
import math
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

pytest.importorskip("qlib")

from factors.batches.direct_pe_qlib_smoke import (  # noqa: E402
    DirectPeFeature,
    DirectPeSmokeActivation,
    DirectPeSmokeRequest,
    canonical_report_json,
    render_markdown,
    run_direct_pe_qlib_smoke,
)
from factors.batches.direct_pe_qlib_smoke.kernel import ScoreAvailability  # noqa: E402

REPOSITORY_ROOT = Path(__file__).parents[5]
SCRIPT_PATH = REPOSITORY_ROOT / "apps/data-engine/scripts/run_direct_pe_qlib_smoke.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("direct_pe_qlib_smoke_runner_for_factors", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RUNNER = _load_runner()


@pytest.fixture(scope="module")
def corpus():
    return RUNNER.load_frozen_corpus()


@pytest.fixture(scope="module")
def smoke_request(corpus):
    return RUNNER.build_request(corpus, environment="ci")


@pytest.fixture(scope="module")
def report(corpus, smoke_request):
    result = run_direct_pe_qlib_smoke(smoke_request)
    RUNNER.verify_frozen_oracles(corpus, result)
    return result


def test_real_qlib_expression_matches_decimal_oracle_and_frozen_decisions(corpus, report) -> None:
    assert report.runtime.compiled_qlib_field == "Div(1,$pe)"
    assert report.runtime.distribution == "pyqlib"
    assert report.runtime.version == "0.9.7"
    assert len(report.decisions) == corpus["replay_contract"]["expected_executable_decisions"] == 36
    assert [
        (row.decision_date.isoformat(), row.execution_date.isoformat(), row.selected_instrument_id)
        for row in report.decisions
    ] == [
        (row["decision_date"], row["execution_date"], row["selected_symbol"])
        for row in corpus["expected_monthly_decisions"]
    ]
    for decision in report.decisions:
        for score in decision.scores:
            if score.availability is ScoreAvailability.AVAILABLE:
                assert math.isclose(
                    float(score.earnings_yield),
                    float(score.qlib_score),
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                )


def test_current_peg_is_snapshot_only_and_matches_frozen_decimal_oracle(corpus, report) -> None:
    expected = {row["symbol"]: row for row in corpus["current_peg_snapshot"]["rows"]}

    assert {row.instrument_id: str(row.peg) for row in report.current_peg_snapshot} == {
        symbol: row["expected_current_peg"] for symbol, row in expected.items()
    }
    assert all(row.historical_decision_input is False for row in report.current_peg_snapshot)
    assert all(
        score.input_id not in row.input_ids
        for row in report.current_peg_snapshot
        for decision in report.decisions
        for score in decision.scores
    )


def test_price_return_report_is_deterministic_and_keeps_the_evidence_ceiling(corpus, report) -> None:
    serialized = canonical_report_json(report)
    markdown = render_markdown(report)
    envelope_a = RUNNER.build_report_envelope(corpus, report)
    envelope_b = RUNNER.build_report_envelope(corpus, report)

    assert json.loads(serialized)["report_id"] == report.report_id
    assert envelope_a == envelope_b
    assert len(envelope_a["input_artifacts"]) == 8
    assert markdown.startswith("# Direct P/E Qlib Smoke\n")
    assert report.metrics.common_session_count == 754
    assert report.metrics.executable_decision_count == 36
    assert report.metrics.trade_count > 0
    assert report.adjusted_price_used is False
    assert report.corporate_actions_applied is False
    assert report.stable_handoff is False
    assert report.release_allowed is False
    assert "no alpha claim" in report.caveats


def test_future_feature_fails_closed(smoke_request) -> None:
    target = next(
        row
        for row in smoke_request.pe_features
        if row.instrument_id == "NICE" and row.observation_date == date(2023, 7, 31)
    )
    mutated = target.model_copy(update={"as_of": target.observation_date + timedelta(days=1)})
    features = tuple(mutated if row is target else row for row in smoke_request.pe_features)

    with pytest.raises(ValueError, match="future_feature"):
        run_direct_pe_qlib_smoke(smoke_request.model_copy(update={"pe_features": features}))


def test_duplicate_feature_and_price_coordinates_fail_closed(smoke_request) -> None:
    payload = smoke_request.model_dump()
    payload["pe_features"] = [*payload["pe_features"], payload["pe_features"][0]]
    with pytest.raises(ValidationError, match="duplicate_feature_coordinate"):
        DirectPeSmokeRequest.model_validate(payload)

    payload = smoke_request.model_dump()
    payload["price_bars"] = [*payload["price_bars"], payload["price_bars"][0]]
    with pytest.raises(ValidationError, match="duplicate_price_coordinate"):
        DirectPeSmokeRequest.model_validate(payload)


@pytest.mark.parametrize("direct_pe", ["0", "-1"])
def test_nonpositive_pe_is_unavailable_and_cannot_win(smoke_request, direct_pe: str) -> None:
    target = next(
        row
        for row in smoke_request.pe_features
        if row.instrument_id == "NICE" and row.observation_date == date(2024, 2, 29)
    )
    mutated = target.model_copy(update={"direct_pe": Decimal(direct_pe)})
    features = tuple(mutated if row is target else row for row in smoke_request.pe_features)

    result = run_direct_pe_qlib_smoke(smoke_request.model_copy(update={"pe_features": features}))
    decision = next(row for row in result.decisions if row.decision_date == date(2024, 2, 29))
    score = next(row for row in decision.scores if row.instrument_id == "NICE")

    assert score.availability is ScoreAvailability.NONPOSITIVE_PE
    assert score.earnings_yield is None
    assert score.qlib_score is None
    assert decision.selected_instrument_id != "NICE"


def test_historical_growth_and_raw_expression_bypasses_are_rejected(smoke_request) -> None:
    payload = smoke_request.pe_features[0].model_dump(mode="json")
    payload["financial_ttm_multiple"] = "2"
    with pytest.raises(ValidationError, match="historical_peg_lookahead"):
        DirectPeFeature.model_validate(payload)

    request_payload = smoke_request.model_dump(mode="json")
    request_payload["expression_definition"] = "Div(1,$pe)"
    with pytest.raises(ValidationError, match="typed_expression_required"):
        DirectPeSmokeRequest.model_validate(request_payload)


def test_runtime_identity_drift_and_universe_shrink_fail_closed(smoke_request) -> None:
    activation = smoke_request.activation.model_dump(mode="json")
    activation["qlib_lock_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="qlib_execution_identity_drift"):
        DirectPeSmokeActivation.model_validate(activation)

    prices = tuple(row for row in smoke_request.price_bars if row.instrument_id != "SHOP")
    features = tuple(row for row in smoke_request.pe_features if row.instrument_id != "SHOP")
    shrunk = smoke_request.model_copy(update={"price_bars": prices, "pe_features": features})
    with pytest.raises(ValueError, match="universe_denominator_mismatch"):
        run_direct_pe_qlib_smoke(shrunk)


def test_missing_common_session_and_misleading_claim_fail_closed(smoke_request, report) -> None:
    prices = tuple(row for row in smoke_request.price_bars if row.session_date != date(2023, 8, 1))
    with pytest.raises(ValueError, match="missing_next_session"):
        run_direct_pe_qlib_smoke(smoke_request.model_copy(update={"price_bars": prices}))

    payload = report.model_dump(mode="json")
    payload["caveats"] = [*payload["caveats"][:-1], "validated alpha"]
    with pytest.raises(ValidationError, match="evidence_ceiling_violation"):
        type(report).model_validate(payload)
