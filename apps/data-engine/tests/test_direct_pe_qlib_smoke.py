from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

REPOSITORY_ROOT = Path(__file__).parents[3]
SCRIPT_PATH = REPOSITORY_ROOT / "apps/data-engine/scripts/run_direct_pe_qlib_smoke.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("direct_pe_qlib_smoke_runner", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RUNNER = _load_runner()


def test_frozen_samples_project_to_source_neutral_contracts() -> None:
    corpus = RUNNER.load_frozen_corpus()
    RUNNER.verify_frozen_artifacts(corpus)

    features, prices, current_peg = RUNNER.project_frozen_inputs(corpus)

    assert len(features) == sum(row["pe_points"] for row in corpus["observed_profiles"].values()) == 7_977
    assert len(prices) == 4 * corpus["replay_contract"]["common_sessions"] == 3_016
    assert tuple(row.instrument_id for row in current_peg) == ("DDOG", "DUOL", "NICE", "SHOP")
    assert {row.instrument_id: row.financial_ttm_multiple for row in current_peg} == {
        "DDOG": Decimal("65.828"),
        "DUOL": Decimal("26.289"),
        "NICE": Decimal("2.613"),
        "SHOP": Decimal("0.39"),
    }
    assert set(features[0].model_dump()) == {
        "instrument_id",
        "observation_date",
        "as_of",
        "direct_pe",
        "confidence",
        "input_id",
    }
    assert not {"source", "source_path", "vendor", "raw_ref", "financial_ttm_multiple"} & set(features[0].model_dump())


def test_request_uses_only_the_typed_direct_pe_expression() -> None:
    request = RUNNER.build_request(RUNNER.load_frozen_corpus())

    assert request.expression_definition.root.kind == "call"
    assert request.expression_definition.root.operator_id == "truealpha.qlib.div.v1"
    assert request.expression_definition.maximum_lookback_sessions == 0
    assert request.activation.adjusted_price_allowed is False
    assert request.activation.historical_peg_allowed is False
    assert request.activation.release_allowed is False


def test_adjusted_close_projection_is_rejected() -> None:
    corpus = RUNNER.load_frozen_corpus()

    with pytest.raises(ValueError, match="adjusted_price_forbidden"):
        RUNNER.project_frozen_inputs(corpus, close_field="Adj Close")


def test_nonpositive_price_projection_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("Date,Open,Close\n2026-01-02,0,1\n")

    with pytest.raises(ValueError, match="nonpositive_execution_price"):
        RUNNER._project_prices("DDOG", path, "0" * 64)


def test_duplicate_coordinates_and_binary_floats_fail_closed() -> None:
    request = RUNNER.build_request(RUNNER.load_frozen_corpus())
    payload = request.model_dump()
    payload["pe_features"] = [*payload["pe_features"], payload["pe_features"][0]]

    with pytest.raises(ValidationError, match="duplicate_feature_coordinate"):
        type(request).model_validate(payload)
    with pytest.raises(ValidationError, match="decimal_must_not_be_binary_float"):
        type(request.price_bars[0])(
            instrument_id="DDOG",
            session_date="2026-01-02",
            unadjusted_open=1.0,
            unadjusted_close="1",
            input_id=f"sample-input:{'0' * 64}",
        )
