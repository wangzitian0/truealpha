"""Source-neutral factor and replay kernel for the S9 direct-P/E smoke."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Annotated, Any, Literal, Protocol, Self, cast

from factors.batches.issuer_strategy_selection_tiny import expected_qlib_runtime_artifact_sha256
from factors.batches.qlib_expression_contract_tiny import (
    QLIB_DISTRIBUTION,
    QLIB_LOCK_SHA256,
    QLIB_RELEASE_COMMIT,
    QLIB_VERSION,
    bind_qlib_expression_execution,
    compile_and_parse_qlib_expression,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.qlib_expression import (
    CompiledQlibExpression,
    QlibCallNode,
    QlibExpressionExecutionBinding,
    QlibFactorExpressionDefinition,
    QlibFeatureBinding,
    QlibFeatureNode,
    QlibNumericNode,
    QlibOperatorRegistry,
)

CORPUS_SHA256 = "9aa64d2d5c4e72b4087b8a501fdd2fc1cbc37b332a3a3278a2ac8d4f123f53b7"
PREPARED_MANIFEST_SHA256 = "43bf58d45738d0c70ce2c8413dff9927166afd27b82c208b5adf7da2f8210cc4"
S7_MANIFEST_SHA256 = "730cf2419dc7795d920998c6d57846c6878bf44c650ec16dadf2e85dc3921bf1"
S8_MANIFEST_SHA256 = "4d2ff50a09e3e13a71c82392f450d4ad76774583e2c1e13c0acd0528d2b34007"
EXPECTED_UNIVERSE = ("DDOG", "DUOL", "NICE", "SHOP")
EXPECTED_PRICE_SESSIONS = 754
EXPECTED_DECISIONS = 36
MINIMUM_DECISIONS = 30
INITIAL_CASH = Decimal("100000")
QLIB_ADAPTER_ID = "truealpha.direct_pe_smoke_adapter.v1"
DECIMAL_CONTEXT = Context(prec=34, rounding=ROUND_HALF_EVEN)
REPORT_CAVEATS = (
    "provisional E1",
    "fixed cohort",
    "vendor history may be backfilled or restated",
    "unadjusted price return only",
    "no corporate-action total return",
    "not survivorship safe",
    "no validated strategy",
    "no alpha claim",
)
_QLIB_PROVIDER_LOCK = RLock()
_DECIMAL_INPUT = Annotated[Decimal, Field(allow_inf_nan=False)]


class _QlibExpression(Protocol):
    def load(self, instrument: str, start_index: int, end_index: int, freq: str) -> Any: ...


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _reject_float(value: object) -> object:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("decimal_must_not_be_binary_float")
    return value


def _canonical_decimal(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("decimal_must_be_finite")
    return Decimal(format(value, "f"))


class DirectPeSmokeActivation(_StrictFrozenModel):
    batch_id: Literal["S9-direct-pe-qlib-smoke"] = "S9-direct-pe-qlib-smoke"
    environment: Literal["local", "ci"]
    corpus_sha256: str = Field(default=CORPUS_SHA256, pattern=r"^[0-9a-f]{64}$")
    prepared_manifest_sha256: str = Field(default=PREPARED_MANIFEST_SHA256, pattern=r"^[0-9a-f]{64}$")
    s7_manifest_sha256: str = Field(default=S7_MANIFEST_SHA256, pattern=r"^[0-9a-f]{64}$")
    s8_manifest_sha256: str = Field(default=S8_MANIFEST_SHA256, pattern=r"^[0-9a-f]{64}$")
    qlib_distribution: Literal["pyqlib"] = "pyqlib"
    qlib_version: Literal["0.9.7"] = "0.9.7"
    qlib_release_commit: str = Field(default=QLIB_RELEASE_COMMIT, pattern=r"^[0-9a-f]{40}$")
    qlib_lock_sha256: str = Field(default=QLIB_LOCK_SHA256, pattern=r"^[0-9a-f]{64}$")
    universe: tuple[str, ...] = EXPECTED_UNIVERSE
    expected_price_sessions: int = EXPECTED_PRICE_SESSIONS
    expected_decisions: int = EXPECTED_DECISIONS
    live_source_allowed: Literal[False] = False
    adjusted_price_allowed: Literal[False] = False
    historical_peg_allowed: Literal[False] = False
    release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def bind_frozen_evidence(self) -> Self:
        actual = (
            self.corpus_sha256,
            self.prepared_manifest_sha256,
            self.s7_manifest_sha256,
            self.s8_manifest_sha256,
            self.qlib_distribution,
            self.qlib_version,
            self.qlib_release_commit,
            self.qlib_lock_sha256,
            self.universe,
            self.expected_price_sessions,
            self.expected_decisions,
        )
        expected = (
            CORPUS_SHA256,
            PREPARED_MANIFEST_SHA256,
            S7_MANIFEST_SHA256,
            S8_MANIFEST_SHA256,
            QLIB_DISTRIBUTION,
            QLIB_VERSION,
            QLIB_RELEASE_COMMIT,
            QLIB_LOCK_SHA256,
            EXPECTED_UNIVERSE,
            EXPECTED_PRICE_SESSIONS,
            EXPECTED_DECISIONS,
        )
        if actual != expected:
            raise ValueError("S9 activation artifact identity drifted")
        return self


class DirectPeFeature(_StrictFrozenModel):
    instrument_id: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,15}$")
    observation_date: date
    as_of: date
    direct_pe: _DECIMAL_INPUT
    confidence: _DECIMAL_INPUT = Field(ge=Decimal("0"), le=Decimal("1"))
    input_id: str = Field(pattern=r"^sample-input:[0-9a-f]{64}$")

    @model_validator(mode="before")
    @classmethod
    def reject_historical_growth(cls, value: object) -> object:
        if isinstance(value, dict) and any(
            field in value for field in ("financial_ttm_multiple", "growth", "historical_growth")
        ):
            raise ValueError("historical_peg_lookahead")
        return value

    @field_validator("direct_pe", "confidence", mode="before")
    @classmethod
    def reject_binary_float(cls, value: object) -> object:
        return _reject_float(value)

    @field_validator("direct_pe", "confidence")
    @classmethod
    def normalize_decimal(cls, value: Decimal) -> Decimal:
        return _canonical_decimal(value)

    @model_validator(mode="after")
    def validate_availability(self) -> Self:
        if self.as_of < self.observation_date:
            raise ValueError("availability_precedes_observation")
        return self


class PriceBar(_StrictFrozenModel):
    instrument_id: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,15}$")
    session_date: date
    unadjusted_open: _DECIMAL_INPUT = Field(gt=Decimal("0"))
    unadjusted_close: _DECIMAL_INPUT = Field(gt=Decimal("0"))
    input_id: str = Field(pattern=r"^sample-input:[0-9a-f]{64}$")

    @field_validator("unadjusted_open", "unadjusted_close", mode="before")
    @classmethod
    def reject_binary_float(cls, value: object) -> object:
        return _reject_float(value)

    @field_validator("unadjusted_open", "unadjusted_close")
    @classmethod
    def normalize_decimal(cls, value: Decimal) -> Decimal:
        return _canonical_decimal(value)


class CurrentPegInput(_StrictFrozenModel):
    instrument_id: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,15}$")
    as_captured_date: date
    current_pe: _DECIMAL_INPUT | None
    financial_ttm_multiple: _DECIMAL_INPUT | None
    confidence: _DECIMAL_INPUT = Field(ge=Decimal("0"), le=Decimal("1"))
    pe_input_id: str = Field(pattern=r"^sample-input:[0-9a-f]{64}$")
    growth_input_id: str = Field(pattern=r"^sample-input:[0-9a-f]{64}$")

    @field_validator("current_pe", "financial_ttm_multiple", "confidence", mode="before")
    @classmethod
    def reject_binary_float(cls, value: object) -> object:
        return _reject_float(value)

    @field_validator("current_pe", "financial_ttm_multiple", "confidence")
    @classmethod
    def normalize_decimal(cls, value: Decimal | None) -> Decimal | None:
        return None if value is None else _canonical_decimal(value)


class DirectPeSmokeRequest(_StrictFrozenModel):
    activation: DirectPeSmokeActivation
    expression_definition: QlibFactorExpressionDefinition
    operator_registry: QlibOperatorRegistry
    pe_features: tuple[DirectPeFeature, ...] = Field(min_length=1)
    price_bars: tuple[PriceBar, ...] = Field(min_length=1)
    current_peg_inputs: tuple[CurrentPegInput, ...] = Field(min_length=len(EXPECTED_UNIVERSE))
    initial_cash: _DECIMAL_INPUT = Field(default=INITIAL_CASH, gt=Decimal("0"))

    @field_validator("initial_cash", mode="before")
    @classmethod
    def reject_binary_float(cls, value: object) -> object:
        return _reject_float(value)

    @field_validator("initial_cash")
    @classmethod
    def normalize_decimal(cls, value: Decimal) -> Decimal:
        return _canonical_decimal(value)

    @model_validator(mode="after")
    def validate_partition(self) -> Self:
        if self.expression_definition.operator_registry_id != self.operator_registry.operator_registry_id:
            raise ValueError("operator_registry_identity_mismatch")
        feature_coordinates = [(row.instrument_id, row.observation_date) for row in self.pe_features]
        if len(feature_coordinates) != len(set(feature_coordinates)):
            raise ValueError("duplicate_feature_coordinate")
        price_coordinates = [(row.instrument_id, row.session_date) for row in self.price_bars]
        if len(price_coordinates) != len(set(price_coordinates)):
            raise ValueError("duplicate_price_coordinate")
        peg_instruments = [row.instrument_id for row in self.current_peg_inputs]
        if tuple(sorted(peg_instruments)) != EXPECTED_UNIVERSE or len(set(peg_instruments)) != len(peg_instruments):
            raise ValueError("current_peg_denominator_mismatch")
        all_instruments = {row.instrument_id for row in self.pe_features}
        all_instruments.update(row.instrument_id for row in self.price_bars)
        if not all_instruments.issubset(set(EXPECTED_UNIVERSE)):
            raise ValueError("unexpected_instrument")
        return self


class ScoreAvailability(StrEnum):
    AVAILABLE = "available"
    NONPOSITIVE_PE = "nonpositive_pe"
    MISSING_PE = "missing_pe"


class EarningsYieldScore(_StrictFrozenModel):
    instrument_id: str
    decision_date: date
    feature_date: date | None
    direct_pe: Decimal | None
    earnings_yield: Decimal | None
    qlib_score: Decimal | None
    confidence: Decimal | None
    input_id: str | None
    availability: ScoreAvailability


class MonthlyDecision(_StrictFrozenModel):
    decision_date: date
    execution_date: date
    selected_instrument_id: str
    scores: tuple[EarningsYieldScore, ...]


class CurrentPegResult(_StrictFrozenModel):
    instrument_id: str
    as_captured_date: date
    current_pe: Decimal | None
    financial_ttm_multiple: Decimal | None
    peg: Decimal | None
    confidence: Decimal
    available: bool
    reason: Literal["available", "missing_input", "nonpositive_pe", "nonpositive_growth"]
    input_ids: tuple[str, str]
    historical_decision_input: Literal[False] = False


class TradeSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class SmokeTrade(_StrictFrozenModel):
    execution_date: date
    instrument_id: str
    side: TradeSide
    quantity: Decimal
    unadjusted_open: Decimal
    notional: Decimal
    price_input_id: str


class DailyPortfolioValue(_StrictFrozenModel):
    session_date: date
    cash: Decimal
    instrument_id: str | None
    quantity: Decimal
    unadjusted_close: Decimal | None
    total_value: Decimal
    cumulative_price_return: Decimal
    drawdown: Decimal


class SmokeMetrics(_StrictFrozenModel):
    common_session_count: int
    executable_decision_count: int
    available_score_cells: int
    expected_score_cells: int
    coverage: Decimal
    trade_count: int
    cumulative_turnover: Decimal
    cumulative_price_return: Decimal
    maximum_drawdown: Decimal
    equal_weight_sanity_price_return: Decimal


class RuntimeEvidence(_StrictFrozenModel):
    distribution: Literal["pyqlib"] = "pyqlib"
    version: Literal["0.9.7"] = "0.9.7"
    release_commit: str
    runtime_artifact_sha256: str
    runtime_lock_sha256: str
    adapter_id: str
    adapter_implementation_sha256: str
    expression_id: str
    compiled_expression_id: str
    compiled_qlib_field: str
    operator_registry_id: str
    expression_execution_evidence_id: str


class DirectPeQlibSmokeReport(_StrictFrozenModel):
    report_id: str = Field(default="", pattern=r"^(?:|direct-pe-qlib-smoke-report:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    batch_id: Literal["S9-direct-pe-qlib-smoke"] = "S9-direct-pe-qlib-smoke"
    evidence_state: Literal["provisional_e1"] = "provisional_e1"
    corpus_sha256: str
    universe: tuple[str, ...]
    runtime: RuntimeEvidence
    current_peg_snapshot: tuple[CurrentPegResult, ...]
    decisions: tuple[MonthlyDecision, ...]
    exclusions: tuple[EarningsYieldScore, ...]
    trades: tuple[SmokeTrade, ...]
    daily_portfolio: tuple[DailyPortfolioValue, ...]
    metrics: SmokeMetrics
    consumed_input_ids: tuple[str, ...]
    caveats: tuple[str, ...] = REPORT_CAVEATS
    historical_peg_used: Literal[False] = False
    adjusted_price_used: Literal[False] = False
    corporate_actions_applied: Literal[False] = False
    stable_handoff: Literal[False] = False
    release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        if self.corpus_sha256 != CORPUS_SHA256 or self.universe != EXPECTED_UNIVERSE:
            raise ValueError("report_scope_identity_mismatch")
        if self.caveats != REPORT_CAVEATS:
            raise ValueError("evidence_ceiling_violation")
        if len(self.decisions) != EXPECTED_DECISIONS:
            raise ValueError("decision_denominator_mismatch")
        if tuple(sorted(set(self.consumed_input_ids))) != self.consumed_input_ids:
            raise ValueError("consumed_input_ids_not_canonical")
        payload = self.model_dump(mode="json", exclude={"report_id", "content_sha256"})
        digest = canonical_sha256(payload)
        expected_id = f"direct-pe-qlib-smoke-report:{digest}"
        if self.content_sha256 and self.content_sha256 != digest:
            raise ValueError("report_content_hash_mismatch")
        if self.report_id and self.report_id != expected_id:
            raise ValueError("report_id_mismatch")
        object.__setattr__(self, "content_sha256", digest)
        object.__setattr__(self, "report_id", expected_id)
        return self


def build_earnings_yield_definition(registry: QlibOperatorRegistry) -> QlibFactorExpressionDefinition:
    """Build the frozen typed expression; raw Qlib strings are never accepted."""

    return QlibFactorExpressionDefinition(
        factor_id="factor.sample.earnings_yield",
        factor_version="1.0.0",
        operator_registry_id=registry.operator_registry_id,
        feature_bindings=(QlibFeatureBinding(feature_binding_id="feature.direct_pe.v1", qlib_field_name="pe"),),
        root=QlibCallNode(
            operator_id="truealpha.qlib.div.v1",
            arguments=(
                QlibNumericNode(value=Decimal("1")),
                QlibFeatureNode(feature_binding_id="feature.direct_pe.v1"),
            ),
        ),
        maximum_lookback_sessions=0,
    )


def _adapter_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _expression_execution_binding() -> QlibExpressionExecutionBinding:
    return QlibExpressionExecutionBinding(
        version=QLIB_VERSION,
        release_commit=QLIB_RELEASE_COMMIT,
        runtime_artifact_sha256=expected_qlib_runtime_artifact_sha256(),
        runtime_lock_sha256=QLIB_LOCK_SHA256,
        adapter_id=QLIB_ADAPTER_ID,
        adapter_implementation_sha256=_adapter_sha256(),
    )


@contextmanager
def _truealpha_feature_provider(panel: dict[str, tuple[float, ...]]) -> Iterator[None]:
    import pandas as pd
    from qlib.data.cache import H  # type: ignore[import-not-found]
    from qlib.data.data import FeatureD, FeatureProvider  # type: ignore[import-not-found]

    class _PanelFeatureProvider(FeatureProvider):
        def feature(
            self,
            instrument: str,
            field: str,
            start_time: int,
            end_time: int,
            freq: str,
        ) -> pd.Series:
            if field != "$pe" or freq != "day" or instrument not in panel:
                raise ValueError("qlib_provider_request_outside_projected_panel")
            values = panel[instrument]
            return pd.Series(values, dtype="float64").iloc[start_time : end_time + 1]

    with _QLIB_PROVIDER_LOCK:
        original = FeatureD._provider
        H["f"].clear()
        FeatureD.register(_PanelFeatureProvider())
        try:
            yield
        finally:
            H["f"].clear()
            FeatureD.register(original)


def _common_price_panel(
    request: DirectPeSmokeRequest,
) -> tuple[tuple[date, ...], dict[str, dict[date, PriceBar]]]:
    by_instrument: dict[str, dict[date, PriceBar]] = {instrument: {} for instrument in EXPECTED_UNIVERSE}
    for row in request.price_bars:
        by_instrument[row.instrument_id][row.session_date] = row
    session_sets = [set(by_instrument[instrument]) for instrument in EXPECTED_UNIVERSE]
    if any(not sessions for sessions in session_sets):
        raise ValueError("universe_denominator_mismatch")
    common = set.intersection(*session_sets)
    if any(sessions != common for sessions in session_sets):
        raise ValueError("price_session_denominator_mismatch")
    sessions = tuple(sorted(common))
    if len(sessions) != request.activation.expected_price_sessions:
        raise ValueError("price_session_denominator_mismatch")
    return sessions, by_instrument


def _feature_panel(
    request: DirectPeSmokeRequest,
    sessions: tuple[date, ...],
) -> tuple[
    dict[str, tuple[float, ...]],
    dict[tuple[str, date], DirectPeFeature | None],
    dict[tuple[str, date], Decimal | None],
]:
    features: dict[str, list[DirectPeFeature]] = {instrument: [] for instrument in EXPECTED_UNIVERSE}
    for row in request.pe_features:
        features[row.instrument_id].append(row)
    for rows in features.values():
        rows.sort(key=lambda item: item.observation_date)

    float_panel: dict[str, tuple[float, ...]] = {}
    selected_features: dict[tuple[str, date], DirectPeFeature | None] = {}
    decimal_oracle: dict[tuple[str, date], Decimal | None] = {}
    for instrument in EXPECTED_UNIVERSE:
        rows = features[instrument]
        index = -1
        values: list[float] = []
        for session in sessions:
            while index + 1 < len(rows) and rows[index + 1].observation_date <= session:
                index += 1
            selected = rows[index] if index >= 0 else None
            if selected is not None and selected.as_of > session:
                raise ValueError("future_feature")
            selected_features[(instrument, session)] = selected
            if selected is None or selected.direct_pe <= 0:
                decimal_oracle[(instrument, session)] = None
                values.append(float("nan"))
                continue
            with localcontext(DECIMAL_CONTEXT):
                oracle = Decimal(1) / selected.direct_pe
            decimal_oracle[(instrument, session)] = oracle
            values.append(float(selected.direct_pe))
        float_panel[instrument] = tuple(values)
    return float_panel, selected_features, decimal_oracle


def _evaluate_qlib(
    request: DirectPeSmokeRequest,
    panel: dict[str, tuple[float, ...]],
    sessions: tuple[date, ...],
) -> tuple[CompiledQlibExpression, dict[tuple[str, date], float], str]:
    compiled, expression = compile_and_parse_qlib_expression(
        request.expression_definition,
        request.operator_registry,
    )
    if compiled.qlib_field != "Div(1,$pe)":
        raise ValueError("unexpected_compiled_expression")
    execution = bind_qlib_expression_execution(compiled, _expression_execution_binding())
    runtime_expression = cast(_QlibExpression, expression)
    outputs: dict[tuple[str, date], float] = {}
    with _truealpha_feature_provider(panel):
        from qlib.data.cache import H  # type: ignore[import-not-found]

        for instrument in EXPECTED_UNIVERSE:
            H["f"].clear()
            values = runtime_expression.load(instrument, 0, len(sessions) - 1, "day").tolist()
            if len(values) != len(sessions):
                raise ValueError("qlib_output_denominator_mismatch")
            outputs.update(zip(((instrument, session) for session in sessions), values, strict=True))
    return compiled, outputs, execution.execution_evidence_id


def _month_end_schedule(sessions: tuple[date, ...]) -> tuple[tuple[date, date], ...]:
    month_ends: dict[tuple[int, int], date] = {}
    for session in sessions:
        month_ends[(session.year, session.month)] = session
    session_index = {session: index for index, session in enumerate(sessions)}
    schedule = []
    for decision_date in month_ends.values():
        index = session_index[decision_date]
        if index + 1 < len(sessions):
            schedule.append((decision_date, sessions[index + 1]))
    return tuple(schedule)


def _score_rows(
    decision_date: date,
    selected_features: dict[tuple[str, date], DirectPeFeature | None],
    decimal_oracle: dict[tuple[str, date], Decimal | None],
    qlib_outputs: dict[tuple[str, date], float],
) -> tuple[EarningsYieldScore, ...]:
    scores = []
    for instrument in EXPECTED_UNIVERSE:
        feature = selected_features[(instrument, decision_date)]
        oracle = decimal_oracle[(instrument, decision_date)]
        qlib_value = qlib_outputs[(instrument, decision_date)]
        if feature is None:
            scores.append(
                EarningsYieldScore(
                    instrument_id=instrument,
                    decision_date=decision_date,
                    feature_date=None,
                    direct_pe=None,
                    earnings_yield=None,
                    qlib_score=None,
                    confidence=None,
                    input_id=None,
                    availability=ScoreAvailability.MISSING_PE,
                )
            )
            continue
        if oracle is None:
            if not math.isnan(qlib_value):
                raise ValueError("nonpositive_pe_ranked")
            scores.append(
                EarningsYieldScore(
                    instrument_id=instrument,
                    decision_date=decision_date,
                    feature_date=feature.observation_date,
                    direct_pe=feature.direct_pe,
                    earnings_yield=None,
                    qlib_score=None,
                    confidence=feature.confidence,
                    input_id=feature.input_id,
                    availability=ScoreAvailability.NONPOSITIVE_PE,
                )
            )
            continue
        if math.isnan(qlib_value) or not math.isfinite(qlib_value):
            raise ValueError("nonfinite_qlib_output")
        if not math.isclose(float(oracle), qlib_value, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("independent_oracle_qlib_output_mismatch")
        scores.append(
            EarningsYieldScore(
                instrument_id=instrument,
                decision_date=decision_date,
                feature_date=feature.observation_date,
                direct_pe=feature.direct_pe,
                earnings_yield=oracle,
                qlib_score=Decimal(str(qlib_value)),
                confidence=feature.confidence,
                input_id=feature.input_id,
                availability=ScoreAvailability.AVAILABLE,
            )
        )
    return tuple(scores)


def _monthly_decisions(
    sessions: tuple[date, ...],
    selected_features: dict[tuple[str, date], DirectPeFeature | None],
    decimal_oracle: dict[tuple[str, date], Decimal | None],
    qlib_outputs: dict[tuple[str, date], float],
) -> tuple[MonthlyDecision, ...]:
    decisions = []
    for decision_date, execution_date in _month_end_schedule(sessions):
        scores = _score_rows(decision_date, selected_features, decimal_oracle, qlib_outputs)
        available = [row for row in scores if row.availability is ScoreAvailability.AVAILABLE]
        if not available:
            raise ValueError("no_available_candidate")
        decimal_order = sorted(available, key=lambda row: (row.earnings_yield, row.instrument_id), reverse=True)
        if len(decimal_order) > 1 and decimal_order[0].earnings_yield == decimal_order[1].earnings_yield:
            raise ValueError("unapproved_score_tie")
        qlib_order = sorted(available, key=lambda row: (row.qlib_score, row.instrument_id), reverse=True)
        if [row.instrument_id for row in decimal_order] != [row.instrument_id for row in qlib_order]:
            raise ValueError("qlib_score_order_not_preserved")
        decisions.append(
            MonthlyDecision(
                decision_date=decision_date,
                execution_date=execution_date,
                selected_instrument_id=qlib_order[0].instrument_id,
                scores=scores,
            )
        )
    if len(decisions) < MINIMUM_DECISIONS or len(decisions) != EXPECTED_DECISIONS:
        raise ValueError("decision_denominator_mismatch")
    return tuple(decisions)


def _current_peg(inputs: tuple[CurrentPegInput, ...]) -> tuple[CurrentPegResult, ...]:
    results = []
    for row in sorted(inputs, key=lambda item: item.instrument_id):
        reason: Literal["available", "missing_input", "nonpositive_pe", "nonpositive_growth"]
        if row.current_pe is None or row.financial_ttm_multiple is None:
            peg = None
            reason = "missing_input"
        elif row.current_pe <= 0:
            peg = None
            reason = "nonpositive_pe"
        elif row.financial_ttm_multiple <= 0:
            peg = None
            reason = "nonpositive_growth"
        else:
            with localcontext(DECIMAL_CONTEXT):
                peg = row.current_pe / row.financial_ttm_multiple
            reason = "available"
        results.append(
            CurrentPegResult(
                instrument_id=row.instrument_id,
                as_captured_date=row.as_captured_date,
                current_pe=row.current_pe,
                financial_ttm_multiple=row.financial_ttm_multiple,
                peg=peg,
                confidence=row.confidence,
                available=reason == "available",
                reason=reason,
                input_ids=(row.pe_input_id, row.growth_input_id),
            )
        )
    return tuple(results)


def _replay(
    request: DirectPeSmokeRequest,
    sessions: tuple[date, ...],
    prices: dict[str, dict[date, PriceBar]],
    decisions: tuple[MonthlyDecision, ...],
) -> tuple[tuple[SmokeTrade, ...], tuple[DailyPortfolioValue, ...], SmokeMetrics]:
    decisions_by_execution = {decision.execution_date: decision for decision in decisions}
    cash = request.initial_cash
    held_instrument: str | None = None
    quantity = Decimal(0)
    trades: list[SmokeTrade] = []
    daily: list[DailyPortfolioValue] = []
    peak = request.initial_cash
    gross_notional = Decimal(0)

    for session in sessions:
        decision = decisions_by_execution.get(session)
        if decision is not None and decision.selected_instrument_id != held_instrument:
            if held_instrument is not None and quantity > 0:
                sell_bar = prices[held_instrument][session]
                with localcontext(DECIMAL_CONTEXT):
                    sell_notional = quantity * sell_bar.unadjusted_open
                    cash += sell_notional
                gross_notional += sell_notional
                trades.append(
                    SmokeTrade(
                        execution_date=session,
                        instrument_id=held_instrument,
                        side=TradeSide.SELL,
                        quantity=quantity,
                        unadjusted_open=sell_bar.unadjusted_open,
                        notional=sell_notional,
                        price_input_id=sell_bar.input_id,
                    )
                )
                quantity = Decimal(0)
            buy_bar = prices[decision.selected_instrument_id][session]
            with localcontext(DECIMAL_CONTEXT):
                quantity = (cash / buy_bar.unadjusted_open).to_integral_value(rounding=ROUND_FLOOR)
                buy_notional = quantity * buy_bar.unadjusted_open
                cash -= buy_notional
            if quantity <= 0:
                raise ValueError("insufficient_cash_for_target")
            gross_notional += buy_notional
            trades.append(
                SmokeTrade(
                    execution_date=session,
                    instrument_id=decision.selected_instrument_id,
                    side=TradeSide.BUY,
                    quantity=quantity,
                    unadjusted_open=buy_bar.unadjusted_open,
                    notional=buy_notional,
                    price_input_id=buy_bar.input_id,
                )
            )
            held_instrument = decision.selected_instrument_id

        close = prices[held_instrument][session].unadjusted_close if held_instrument is not None else None
        with localcontext(DECIMAL_CONTEXT):
            total = cash + (quantity * close if close is not None else Decimal(0))
            cumulative_return = total / request.initial_cash - Decimal(1)
            peak = max(peak, total)
            drawdown = total / peak - Decimal(1)
        daily.append(
            DailyPortfolioValue(
                session_date=session,
                cash=cash,
                instrument_id=held_instrument,
                quantity=quantity,
                unadjusted_close=close,
                total_value=total,
                cumulative_price_return=cumulative_return,
                drawdown=drawdown,
            )
        )

    available_cells = sum(
        score.availability is ScoreAvailability.AVAILABLE for decision in decisions for score in decision.scores
    )
    expected_cells = len(decisions) * len(EXPECTED_UNIVERSE)
    with localcontext(DECIMAL_CONTEXT):
        coverage = Decimal(available_cells) / Decimal(expected_cells)
        turnover = gross_notional / request.initial_cash
    metrics = SmokeMetrics(
        common_session_count=len(sessions),
        executable_decision_count=len(decisions),
        available_score_cells=available_cells,
        expected_score_cells=expected_cells,
        coverage=coverage,
        trade_count=len(trades),
        cumulative_turnover=turnover,
        cumulative_price_return=daily[-1].cumulative_price_return,
        maximum_drawdown=min(row.drawdown for row in daily),
        equal_weight_sanity_price_return=_equal_weight_sanity(request, sessions, prices, decisions[0].execution_date),
    )
    return tuple(trades), tuple(daily), metrics


def _equal_weight_sanity(
    request: DirectPeSmokeRequest,
    sessions: tuple[date, ...],
    prices: dict[str, dict[date, PriceBar]],
    start_date: date,
) -> Decimal:
    allocation = request.initial_cash / Decimal(len(EXPECTED_UNIVERSE))
    with localcontext(DECIMAL_CONTEXT):
        shares = {
            instrument: allocation / prices[instrument][start_date].unadjusted_open for instrument in EXPECTED_UNIVERSE
        }
        final_date = sessions[-1]
        final_value = sum(
            shares[instrument] * prices[instrument][final_date].unadjusted_close for instrument in EXPECTED_UNIVERSE
        )
        return final_value / request.initial_cash - Decimal(1)


def run_direct_pe_qlib_smoke(request: DirectPeSmokeRequest) -> DirectPeQlibSmokeReport:
    """Run the frozen fixed-cohort expression and price-return replay."""

    sessions, prices = _common_price_panel(request)
    panel, selected_features, decimal_oracle = _feature_panel(request, sessions)
    compiled, qlib_outputs, execution_evidence_id = _evaluate_qlib(request, panel, sessions)
    decisions = _monthly_decisions(sessions, selected_features, decimal_oracle, qlib_outputs)
    peg = _current_peg(request.current_peg_inputs)
    trades, daily, metrics = _replay(request, sessions, prices, decisions)
    exclusions = tuple(
        score
        for decision in decisions
        for score in decision.scores
        if score.availability is not ScoreAvailability.AVAILABLE
    )
    binding = _expression_execution_binding()
    consumed = {row.input_id for row in selected_features.values() if row is not None}
    consumed.update(row.input_id for row in request.price_bars)
    for row in request.current_peg_inputs:
        consumed.update((row.pe_input_id, row.growth_input_id))
    return DirectPeQlibSmokeReport(
        corpus_sha256=request.activation.corpus_sha256,
        universe=request.activation.universe,
        runtime=RuntimeEvidence(
            release_commit=QLIB_RELEASE_COMMIT,
            runtime_artifact_sha256=binding.runtime_artifact_sha256,
            runtime_lock_sha256=binding.runtime_lock_sha256,
            adapter_id=binding.adapter_id,
            adapter_implementation_sha256=binding.adapter_implementation_sha256,
            expression_id=request.expression_definition.expression_id,
            compiled_expression_id=compiled.compiled_expression_id,
            compiled_qlib_field=compiled.qlib_field,
            operator_registry_id=request.operator_registry.operator_registry_id,
            expression_execution_evidence_id=execution_evidence_id,
        ),
        current_peg_snapshot=peg,
        decisions=decisions,
        exclusions=exclusions,
        trades=trades,
        daily_portfolio=daily,
        metrics=metrics,
        consumed_input_ids=tuple(sorted(consumed)),
    )


def canonical_report_json(report: DirectPeQlibSmokeReport) -> str:
    """Serialize the report reproducibly for hashing and regression."""

    return (
        json.dumps(
            report.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    )


def render_markdown(report: DirectPeQlibSmokeReport) -> str:
    """Render a concise deterministic view of the canonical report."""

    lines = [
        "# Direct P/E Qlib Smoke",
        "",
        f"- Report ID: `{report.report_id}`",
        f"- Evidence: `{report.evidence_state}`",
        f"- Universe: {', '.join(report.universe)}",
        f"- Sessions: {report.metrics.common_session_count}",
        f"- Decisions: {report.metrics.executable_decision_count}",
        f"- Trades: {report.metrics.trade_count}",
        f"- Coverage: {report.metrics.coverage}",
        f"- Price return: {report.metrics.cumulative_price_return}",
        f"- Maximum drawdown: {report.metrics.maximum_drawdown}",
        f"- Equal-weight sanity return: {report.metrics.equal_weight_sanity_price_return}",
        "",
        "## Expression",
        "",
        f"- Qlib: {report.runtime.distribution} {report.runtime.version}",
        f"- Compiled: `{report.runtime.compiled_qlib_field}`",
        "",
        "## Current PEG Snapshot",
        "",
        "| Symbol | P/E | Growth multiple | PEG | Status |",
        "|---|---:|---:|---:|---|",
    ]
    for row in report.current_peg_snapshot:
        lines.append(
            f"| {row.instrument_id} | {row.current_pe} | {row.financial_ttm_multiple} | {row.peg} | {row.reason} |"
        )
    lines.extend(["", "## Monthly Decisions", "", "| Decision | Execution | Selected |", "|---|---|---|"])
    lines.extend(
        f"| {row.decision_date} | {row.execution_date} | {row.selected_instrument_id} |" for row in report.decisions
    )
    lines.extend(["", "## Evidence Ceiling", ""])
    lines.extend(f"- {caveat}" for caveat in report.caveats)
    return "\n".join(lines) + "\n"
