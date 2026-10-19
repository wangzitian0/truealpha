import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from truealpha_contracts.strategy import (
    CoreStrategyEvaluationProtocol,
    EvaluationPartition,
    EvaluationSplitRule,
    ReportedEvaluationMetric,
    WalkForwardWindow,
)

_VALIDATION_START = datetime(2025, 7, 1, tzinfo=UTC)
_RESERVE_START = datetime(2026, 1, 1, tzinfo=UTC)


def _protocol(**overrides) -> CoreStrategyEvaluationProtocol:
    defaults = dict(
        protocol_version="v0",
        split_rule=EvaluationSplitRule.CHRONOLOGICAL_TRAIN_VALIDATION_RESERVE,
        validation_start=_VALIDATION_START,
        out_of_sample_reserve_start=_RESERVE_START,
        walk_forward=WalkForwardWindow(window_length_days=90, step_days=30, minimum_history_days=180),
        reported_metrics=(
            ReportedEvaluationMetric.COVERAGE,
            ReportedEvaluationMetric.RETURN,
            ReportedEvaluationMetric.DRAWDOWN,
            ReportedEvaluationMetric.TURNOVER,
        ),
        not_a_profitability_claim=True,
    )
    defaults.update(overrides)
    return CoreStrategyEvaluationProtocol(**defaults)


def test_valid_protocol_is_content_addressed_and_round_trips() -> None:
    protocol = _protocol()
    assert protocol.protocol_id == f"core-strategy-evaluation-protocol:{protocol.content_sha256}"

    replayed = CoreStrategyEvaluationProtocol.model_validate_json(protocol.model_dump_json())
    assert replayed == protocol


def test_tampered_content_hash_is_rejected() -> None:
    protocol = _protocol()
    tampered = {**json.loads(protocol.model_dump_json()), "content_sha256": "a" * 64}
    with pytest.raises(ValidationError, match="content_sha256 does not match"):
        CoreStrategyEvaluationProtocol.model_validate_json(json.dumps(tampered))


def test_mutable_protocol_version_is_rejected() -> None:
    with pytest.raises(ValidationError, match="mutable reference"):
        _protocol(protocol_version="latest")


def test_boundaries_must_be_timezone_aware() -> None:
    with pytest.raises(ValidationError):
        _protocol(validation_start=datetime(2025, 7, 1))  # noqa: DTZ001
    with pytest.raises(ValidationError):
        _protocol(out_of_sample_reserve_start=datetime(2026, 1, 1))  # noqa: DTZ001


def test_validation_start_must_precede_reserve_start() -> None:
    with pytest.raises(ValidationError, match="validation_start must strictly precede"):
        _protocol(validation_start=_RESERVE_START, out_of_sample_reserve_start=_VALIDATION_START)
    with pytest.raises(ValidationError, match="validation_start must strictly precede"):
        _protocol(validation_start=_RESERVE_START, out_of_sample_reserve_start=_RESERVE_START)


def test_duplicate_reported_metrics_are_rejected_not_silently_deduped() -> None:
    with pytest.raises(ValidationError, match="must not repeat"):
        _protocol(reported_metrics=(ReportedEvaluationMetric.COVERAGE, ReportedEvaluationMetric.COVERAGE))


def test_three_way_partition_is_disjoint_by_construction() -> None:
    protocol = _protocol()
    train_instant = datetime(2025, 1, 1, tzinfo=UTC)
    validation_instant = datetime(2025, 9, 1, tzinfo=UTC)
    reserved_instant = datetime(2026, 6, 1, tzinfo=UTC)

    assert protocol.partition_for(train_instant) is EvaluationPartition.TRAIN
    assert protocol.partition_for(_VALIDATION_START) is EvaluationPartition.VALIDATION
    assert protocol.partition_for(validation_instant) is EvaluationPartition.VALIDATION
    assert protocol.partition_for(_RESERVE_START) is EvaluationPartition.RESERVED_OUT_OF_SAMPLE
    assert protocol.partition_for(reserved_instant) is EvaluationPartition.RESERVED_OUT_OF_SAMPLE

    # Every instant lands on exactly one of the three partitions -- never
    # both, never neither.
    for instant in (train_instant, _VALIDATION_START, validation_instant, _RESERVE_START, reserved_instant):
        partition = protocol.partition_for(instant)
        assert sum(partition is candidate for candidate in EvaluationPartition) == 1


def test_is_reserved_out_of_sample_matches_the_partition() -> None:
    protocol = _protocol()
    assert protocol.is_reserved_out_of_sample(datetime(2025, 1, 1, tzinfo=UTC)) is False
    assert protocol.is_reserved_out_of_sample(datetime(2025, 9, 1, tzinfo=UTC)) is False
    assert protocol.is_reserved_out_of_sample(_RESERVE_START) is True


def test_partition_for_fails_closed_on_naive_datetime_not_a_raw_typeerror() -> None:
    protocol = _protocol()
    with pytest.raises(ValueError, match="must be timezone-aware"):
        protocol.partition_for(datetime(2025, 9, 1))  # noqa: DTZ001


class TestWalkForwardWindow:
    def test_valid_window(self) -> None:
        window = WalkForwardWindow(window_length_days=90, step_days=30, minimum_history_days=180)
        assert window.window_length_days == 90

    def test_minimum_history_shorter_than_window_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="at least one full walk-forward window"):
            WalkForwardWindow(window_length_days=90, step_days=30, minimum_history_days=60)

    def test_step_larger_than_window_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="leave gaps"):
            WalkForwardWindow(window_length_days=90, step_days=120, minimum_history_days=180)

    def test_zero_or_negative_fields_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            WalkForwardWindow(window_length_days=0, step_days=30, minimum_history_days=180)

    def test_window_fields_reject_bool_despite_bool_being_an_int_subclass(self) -> None:
        # Classic Python footgun: bool is an int subclass, so an unstrict
        # schema would silently accept `window_length_days=True` as 1.
        # Strict mode (_StrictFrozenModel) must reject it.
        with pytest.raises(ValidationError):
            WalkForwardWindow(window_length_days=True, step_days=30, minimum_history_days=180)


def test_not_a_profitability_claim_must_be_true() -> None:
    with pytest.raises(ValidationError):
        _protocol(not_a_profitability_claim=False)


def test_empty_reported_metrics_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _protocol(reported_metrics=())
