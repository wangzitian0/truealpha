"""Tests for #1413 (Root 4): F2, F3, F4, B1, B2, and D6."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import polars as pl
import pytest
from data_engine.datahub import backtest as backtest_mod
from data_engine.datahub.production_topt.metric_node_values import materialize_metric_node_values
from data_engine.lanes import backtest as backtest_lane
from data_engine.strategy_backtest_gateway import issuer_inputs_from_rows
from factors.backtest.engine import Performance
from factors.composite.strategy_evaluator import (
    IssuerInput,
    LookAheadError,
    evaluate_cutoff,
)
from factors.panel import (
    GPPE_OPERATING_FINANCIAL_SPEC,
    IssuerClass,
    node_id_for,
    variant_sha256,
)
from factors.production_topt import (
    GppeV0Definition,
    MetricAvailability,
    MetricFreshness,
    OperatingBranch,
    ToptCellQualityInput,
    ToptCoreSnapshotInput,
    ToptMarketValueComponent,
    ToptMetricInput,
)
from truealpha_contracts.execution import InputEvidenceStatus
from truealpha_contracts.metric_node_read import (
    MetricNodeValueReport,
    PostgresMetricNodeValueRepository,
)
from truealpha_contracts.strategy import (
    LargeModelValueV0Definition,
    load_live_strategy_corpus,
    load_live_strategy_definition,
)

CUTOFF = datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)
KNOWN = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)


class _RecordingCursor:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self._rows = rows or []

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Any]:
        return list(self._rows)


class _FakeNodeConnection:
    def __init__(self, financial_payloads: dict[str, dict[str, Any]] | None = None) -> None:
        self.financial_payloads = financial_payloads or {}
        self.inserted_rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _RecordingCursor:
        if "from staging.capture_normalized_observations" in sql:
            return _RecordingCursor([(listing_id, payload) for listing_id, payload in self.financial_payloads.items()])
        if "insert into mart.metric_node_values" in sql:
            self.inserted_rows.append(params)
            return _RecordingCursor([(params[2],)])
        return _RecordingCursor([])


def _sample_factor_input() -> ToptCoreSnapshotInput:
    obs_ids = tuple(f"normalized-observation:{i:064x}" for i in range(1, 5))
    cells = tuple(
        ToptCellQualityInput(
            input_id=oid,
            confidence=Decimal("0.92"),
            knowable_at=KNOWN,
            freshness=MetricFreshness.FRESH,
        )
        for oid in obs_ids
    )
    gp = ToptMetricInput(
        input_id=obs_ids[0],
        metric="gross_profit",
        value=Decimal("10000000000"),
        confidence=Decimal("0.92"),
        knowable_at=KNOWN,
        freshness=MetricFreshness.FRESH,
        availability=MetricAvailability.AVAILABLE,
    )
    assets = ToptMetricInput(
        input_id=obs_ids[0],
        metric="total_assets",
        value=Decimal("20000000000"),
        confidence=Decimal("0.92"),
        knowable_at=KNOWN,
        freshness=MetricFreshness.FRESH,
        availability=MetricAvailability.AVAILABLE,
    )
    headcount = ToptMetricInput(
        input_id=obs_ids[0],
        metric="headcount",
        value=Decimal("1000"),
        confidence=Decimal("0.92"),
        knowable_at=KNOWN,
        freshness=MetricFreshness.FRESH,
        availability=MetricAvailability.AVAILABLE,
    )
    revenue = ToptMetricInput(
        input_id=obs_ids[0],
        metric="revenue",
        value=Decimal("25000000000"),
        confidence=Decimal("0.92"),
        knowable_at=KNOWN,
        freshness=MetricFreshness.FRESH,
        availability=MetricAvailability.AVAILABLE,
    )
    price = ToptMetricInput(
        input_id=obs_ids[1],
        metric="market_price",
        value=Decimal("150"),
        confidence=Decimal("0.92"),
        knowable_at=KNOWN,
        freshness=MetricFreshness.FRESH,
        availability=MetricAvailability.AVAILABLE,
    )
    shares = ToptMetricInput(
        input_id=obs_ids[0],
        metric="shares_outstanding",
        value=Decimal("100000000"),
        confidence=Decimal("0.92"),
        knowable_at=KNOWN,
        freshness=MetricFreshness.FRESH,
        availability=MetricAvailability.AVAILABLE,
    )
    return ToptCoreSnapshotInput(
        snapshot_id="topt-core-snapshot:" + "a" * 64,
        run_id="capture-run:" + "b" * 64,
        release_manifest_id="release-manifest:" + "c" * 64,
        universe_id="universe:topt-20",
        universe_version="2026.07.12",
        universe_sha256="d" * 64,
        cutoff=CUTOFF,
        issuer_id="issuer:nvda",
        instrument_id="security:nvda",
        listing_id="listing:xnas:nvda",
        operating_branch=OperatingBranch.NON_FINANCIAL,
        observation_ids=obs_ids,
        cell_inputs=cells,
        gross_profit=gp,
        total_assets=assets,
        headcount=headcount,
        revenue=revenue,
        pre_provision_profit=None,
        market_value_components=(
            ToptMarketValueComponent(
                instrument_id="security:nvda",
                listing_id="listing:xnas:nvda",
                market_price=price,
                shares_outstanding=shares,
            ),
        ),
    )


def test_f2_polars_expr_factor_iteration_materializes_node_values_without_ddl_or_struct_edit() -> None:
    """F2 standing check (#1610): registering a one-line Polars `pl.Expr` (both cross-sectional
    and multi-period `.shift().over("issuer_id")`) materializes into `mart.metric_node_values`
    with automatic `expr.meta.root_names()` input discovery and `pl.min_horizontal` confidence
    propagation, with zero DDL or struct edits."""
    custom_exprs = {
        "gppe_raw": pl.col("gross_profit") / pl.col("employees_total"),
        "net_income_yoy_growth": (pl.col("net_income") / pl.col("net_income").shift(1).over("issuer_id")) - 1.0,
    }

    factor_2024 = _sample_factor_input()
    obs_id = factor_2024.observation_ids[0]
    ni_2024 = ToptMetricInput(
        input_id=obs_id,
        metric="net_income",
        value=Decimal("5000000000"),
        confidence=Decimal("0.70"),
        knowable_at=KNOWN,
        freshness=MetricFreshness.FRESH,
        availability=MetricAvailability.AVAILABLE,
    )
    ni_2025 = ToptMetricInput(
        input_id=obs_id,
        metric="net_income",
        value=Decimal("7500000000"),
        confidence=Decimal("0.90"),
        knowable_at=KNOWN,
        freshness=MetricFreshness.FRESH,
        availability=MetricAvailability.AVAILABLE,
    )
    factor_2024 = factor_2024.model_copy(
        update={
            "listing_id": "listing:xnas:nvda:2024",
            "inputs": {"net_income": ni_2024},
        }
    )
    factor_2025 = _sample_factor_input().model_copy(
        update={
            "inputs": {"net_income": ni_2025},
        }
    )

    conn = _FakeNodeConnection()
    gppe_def = GppeV0Definition(risk_free_rate=Decimal("0.05"))
    periods = {
        "listing:xnas:nvda:2024": (date(2024, 12, 31), date(2024, 12, 31), date(2024, 12, 31)),
        "listing:xnas:nvda": (date(2025, 12, 31), date(2025, 12, 31), date(2025, 12, 31)),
    }
    count = materialize_metric_node_values(
        conn,  # type: ignore[arg-type]
        factor_inputs=(factor_2024, factor_2025),
        gppe_definition=gppe_def,
        tier_definition_id="strategy-definition:" + "e" * 64,
        periods_by_listing=periods,
        evidence_by_listing={
            "listing:xnas:nvda:2024": InputEvidenceStatus.VERIFIED,
            "listing:xnas:nvda": InputEvidenceStatus.VERIFIED,
        },
        extra_expressions=custom_exprs,
    )
    assert count == 20
    by_period_and_key = {(row[4], row[3]): row for row in conn.inserted_rows}
    assert ("2025-12-31", "gppe_uniform_charge_v0") in by_period_and_key
    assert ("2025-12-31", "gppe_operating_financial_v1") in by_period_and_key
    raw_row = by_period_and_key[("2025-12-31", "gppe_raw")]
    assert raw_row[2] == node_id_for("gppe_raw")
    assert raw_row[6] == Decimal("10000000.0")  # 10B / 1000
    assert raw_row[8] == "available"
    assert raw_row[10] == "verified"
    assert raw_row[12] == Decimal("0.92")

    # Multi-period shift(1).over("issuer_id"): 2024 has no prior period -> unavailable;
    # 2025 has 7.5B / 5.0B - 1 = 0.50 with min(0.90, 0.70) = 0.70 confidence!
    growth_2024 = by_period_and_key[("2024-12-31", "net_income_yoy_growth")]
    assert growth_2024[6] is None
    assert growth_2024[8] != "available"
    growth_2025 = by_period_and_key[("2025-12-31", "net_income_yoy_growth")]
    assert growth_2025[6] == Decimal("0.5")
    assert growth_2025[8] == "available"
    assert growth_2025[12] == Decimal("0.7")

    unverified_conn = _FakeNodeConnection()
    materialize_metric_node_values(
        unverified_conn,  # type: ignore[arg-type]
        factor_inputs=(factor_2025,),
        gppe_definition=gppe_def,
        tier_definition_id="strategy-definition:" + "e" * 64,
        periods_by_listing=periods,
        evidence_by_listing={},
        extra_expressions={"gppe_raw": custom_exprs["gppe_raw"]},
    )
    assert all(row[10] == "degraded" and row[11] == "not_evaluated" for row in unverified_conn.inserted_rows)


def test_f3_strategy_definition_evaluates_trees_by_key_and_allows_omitting_universe() -> None:
    """F3 standing check: LargeModelValueV0Definition omits an unused universe requirement when None
    and evaluates labor_efficiency_tree_keys through factors.forest.evaluate (#1518: pinned by hash)."""
    corpus = load_live_strategy_corpus()
    payload = json.loads(json.dumps(corpus["strategy_definition"]))
    payload.pop("universe", None)
    payload["definition_version"] = "1.1.1"
    definition = LargeModelValueV0Definition.model_validate_json(json.dumps(payload))
    assert definition.universe is None
    assert definition.labor_efficiency_tree_keys is not None
    assert {name: (pin.tree_key, pin.tree_version) for name, pin in definition.labor_efficiency_tree_keys.items()} == {
        "financial": ("gppe_banking_tce", "v2"),
        "non_financial": ("gppe", "production-topt-v0.2.0"),
        "insurance": ("gppe", "production-topt-v0.2.0"),
    }

    # The live definition 1.1.0 hash stays untouched
    live = load_live_strategy_definition()
    assert live.content_sha256 == "b8768334a00f88f9f34244a758182454ef220343e161f6c5008dde5c9d4611e3"

    issuer = IssuerInput(
        issuer_id="issuer:nvda",
        records={
            "gross_profit": (Decimal("10000000000"), Decimal("0.95")),
            "total_assets": (Decimal("20000000000"), Decimal("0.95")),
            "headcount": (Decimal("1000"), Decimal("0.95")),
            "revenue": (Decimal("25000000000"), Decimal("0.95")),
            "shares_outstanding": (Decimal("100000000"), Decimal("0.95")),
            "last_close": (Decimal("100"), Decimal("0.95")),
        },
        knowable_at={
            k: KNOWN
            for k in ("gross_profit", "total_assets", "headcount", "revenue", "shares_outstanding", "last_close")
        },
        issuer_class=IssuerClass.NON_FINANCIAL,
    )
    decisions_tree = evaluate_cutoff(
        [issuer],
        definition=definition,
        cutoff_at=CUTOFF,
        risk_free_rate=Decimal("0.05"),
    )
    decisions_live = evaluate_cutoff(
        [issuer],
        definition=live,
        cutoff_at=CUTOFF,
        risk_free_rate=Decimal("0.05"),
    )
    assert decisions_tree[0].capital_adjusted_labor_efficiency == decisions_live[0].capital_adjusted_labor_efficiency
    assert decisions_tree[0].labor_efficiency_metric == "gppe_uniform_charge_v0"


def test_f4_postgres_metric_node_value_repository_reads_any_node_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """F4 standing check: PostgresMetricNodeValueRepository reads any node_key at the governed head."""

    class _FakeReadConn:
        def __enter__(self) -> _FakeReadConn:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _RecordingCursor:
            if "from mart.served_head" in sql:
                return _RecordingCursor(
                    [
                        {
                            "run_id": "capture-run:" + "b" * 64,
                            "freshness": "fresh",
                            "availability": "available",
                            "staleness_reason": None,
                        }
                    ]
                )
            if "from mart.metric_node_values" in sql:
                node_key = params[1]
                return _RecordingCursor(
                    [
                        {
                            "issuer_id": "issuer:nvda",
                            "node_id": "6143cf17-ceba-5f78-a2d3-a7cc423c49a7",
                            "node_key": node_key,
                            "period": "2025-12-31",
                            "period_tag": "FY",
                            "value": "9000000",
                            "unit": "per_employee",
                            "availability_status": "available",
                            "reason_codes": [],
                            "source_evidence_status": "verified",
                            "factor_validation_status": "accepted",
                            "confidence": "0.92",
                            "tree_version": "production-topt-v0.2.0",
                            "tree_sha256": "f" * 64,
                        }
                    ]
                )
            return _RecordingCursor([])

    monkeypatch.setattr("psycopg.connect", lambda *args, **kwargs: _FakeReadConn())
    repo = PostgresMetricNodeValueRepository(database_url="postgresql://unused")
    report = repo.latest(node_key="custom_node_v1")
    assert isinstance(report, MetricNodeValueReport)
    assert report.node_key == "custom_node_v1"
    assert report.available_count == 1
    assert report.cells[0].value == "9000000"
    assert report.cells[0].confidence_band == "high"


def test_b1_b2_backtest_overrides_and_parameter_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """B1 & B2 standing check: apply_definition_overrides and sweep_core_strategy_backtest run N
    parameter combinations and produce distinct definition_content_sha256 values without overwriting."""
    live = load_live_strategy_definition()
    overridden_1 = backtest_mod.apply_definition_overrides(
        live,
        {"selection.selection_count": 1, "eligibility.maximum_input_age_days": 365},
    )
    overridden_2 = backtest_mod.apply_definition_overrides(
        live,
        {"selection.selection_count": 3, "eligibility.maximum_input_age_days": 400},
    )
    assert overridden_1.selection.selection_count == 1
    assert overridden_1.eligibility.maximum_input_age_days == 365
    assert overridden_2.selection.selection_count == 3
    assert len({live.content_sha256, overridden_1.content_sha256, overridden_2.content_sha256}) == 3

    seen_hashes: list[str] = []

    def _fake_run(
        _connection: Any,
        *,
        tick: datetime,
        definition: LargeModelValueV0Definition | None = None,
        overrides: Any = None,
        start_cutoff: datetime | None = None,
        end_cutoff: datetime | None = None,
    ) -> backtest_mod.BacktestOutcome:
        effective = backtest_mod.apply_definition_overrides(definition or live, overrides or {})
        seen_hashes.append(effective.content_sha256)
        stats = Performance(Decimal("0.1"), Decimal("0.1"), Decimal("0.1"), Decimal("0.1"), Decimal("0.1"), None, None)
        return backtest_mod.BacktestOutcome(
            run_id=f"backtest-run:{effective.content_sha256}",
            inserted=True,
            strategy_key=effective.strategy_id,
            cutoffs=4,
            trades=8,
            strategy=stats,
            benchmark=stats,
        )

    monkeypatch.setattr(backtest_mod, "run_core_strategy_backtest", _fake_run)
    outcomes = backtest_mod.sweep_core_strategy_backtest(
        MagicMock(),
        tick=CUTOFF,
        overrides_list=[
            {"selection.selection_count": 1},
            {"selection.selection_count": 3},
            {"eligibility.maximum_input_age_days": 500},
        ],
    )
    assert len(outcomes) == 3
    assert len(set(seen_hashes)) == 3
    assert live.content_sha256 not in seen_hashes

    # Verify NightlyBacktestConfig passes overrides and cutoff window through _backtest_kwargs_from_config
    cfg = backtest_lane.NightlyBacktestConfig(
        executed_at="2026-10-12T21:45:00+00:00",
        overrides_json='{"selection.selection_count": 1}',
        start_cutoff="2025-03-31T23:59:59+00:00",
        end_cutoff="2026-06-30T23:59:59+00:00",
    )
    kwargs = backtest_lane._backtest_kwargs_from_config(cfg)
    assert kwargs["overrides"] == {"selection.selection_count": 1}
    assert kwargs["start_cutoff"] == datetime(2025, 3, 31, 23, 59, 59, tzinfo=UTC)
    assert kwargs["end_cutoff"] == datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)


def test_d6_lookahead_fact_raises_lookahead_error_at_gateway_and_evaluator() -> None:
    """D6 standing check: any input with knowable_at > cutoff_at raises LookAheadError."""
    future_knowable = datetime(2026, 7, 1, 0, 0, 1, tzinfo=UTC)
    rows = [
        ("issuer:nvda", "gross_profit", "10000000000", "0.95", None, future_knowable),
    ]
    with pytest.raises(LookAheadError, match="knowable_at .* > cutoff"):
        issuer_inputs_from_rows(rows, cutoff_at=CUTOFF)

    issuer = IssuerInput(
        issuer_id="issuer:nvda",
        records={
            "gross_profit": (Decimal("10000000000"), Decimal("0.95")),
            "total_assets": (Decimal("20000000000"), Decimal("0.95")),
            "headcount": (Decimal("1000"), Decimal("0.95")),
            "revenue": (Decimal("25000000000"), Decimal("0.95")),
            "shares_outstanding": (Decimal("100000000"), Decimal("0.95")),
            "last_close": (Decimal("100"), Decimal("0.95")),
        },
        knowable_at={
            "gross_profit": future_knowable,
            "total_assets": KNOWN,
            "headcount": KNOWN,
            "revenue": KNOWN,
            "shares_outstanding": KNOWN,
            "last_close": KNOWN,
        },
        issuer_class=IssuerClass.NON_FINANCIAL,
    )
    with pytest.raises(LookAheadError, match="knowable_at .* > cutoff"):
        evaluate_cutoff(
            [issuer],
            definition=load_live_strategy_definition(),
            cutoff_at=CUTOFF,
            risk_free_rate=Decimal("0.05"),
        )


def test_subtree_scoped_confidence_isolates_headcount_low_confidence_to_gppe_roots() -> None:
    """Intermediate nodes that do not consume headcount keep their consumed XBRL confidence (0.75),
    while GPPE roots that divide by headcount inherit min(0.75, 0.50) = 0.50."""
    factor_input = _sample_factor_input()
    factor_input = factor_input.model_copy(
        update={
            "cell_inputs": tuple(
                cell.model_copy(update={"confidence": Decimal("0.75")}) for cell in factor_input.cell_inputs
            ),
            "gross_profit": factor_input.gross_profit.model_copy(update={"confidence": Decimal("0.75")}),
            "total_assets": factor_input.total_assets.model_copy(update={"confidence": Decimal("0.75")}),
            "headcount": factor_input.headcount.model_copy(update={"confidence": Decimal("0.50")}),
        }
    )
    conn = _FakeNodeConnection()
    materialize_metric_node_values(
        conn,  # type: ignore[arg-type]
        factor_inputs=(factor_input,),
        gppe_definition=GppeV0Definition(risk_free_rate=Decimal("0.05")),
        tier_definition_id="strategy-definition:" + "e" * 64,
        periods_by_listing={"listing:xnas:nvda": (date(2025, 12, 31), date(2025, 12, 31), date(2025, 12, 31))},
        evidence_by_listing={"listing:xnas:nvda": InputEvidenceStatus.VERIFIED},
    )
    by_tree_and_key = {(row[14], row[3]): row[12] for row in conn.inserted_rows}
    assert by_tree_and_key[("production-topt-v0.2.0", "operating_gross_profit")] == Decimal("0.75")
    assert by_tree_and_key[("production-topt-v0.2.0", "capital_charge")] == Decimal("0.75")
    assert by_tree_and_key[("production-topt-v0.2.0", "capital_adjusted_gross_profit")] == Decimal("0.75")
    assert by_tree_and_key[("production-topt-v0.2.0", "gppe_uniform_charge_v0")] == Decimal("0.50")
    assert by_tree_and_key[("1.0.0", "total_gross_return")] == Decimal("0.75")
    assert by_tree_and_key[("1.0.0", "capital_adjusted_operating_financial")] == Decimal("0.75")
    assert by_tree_and_key[("1.0.0", "gppe_operating_financial_v1")] == Decimal("0.50")


def test_financial_issuer_under_gppe_operating_financial_tree_only_materializes_reachable_derived_nodes() -> None:
    """Under GPPE_OPERATING_FINANCIAL_SPEC, a FINANCIAL issuer reaches capital_charge_tce rather than
    capital_charge; capital_charge (though globally _EVERY_CLASS) must not be materialized as unavailable."""
    from data_engine.datahub.production_topt.metric_node_values import _materialize_issuer_tree
    from truealpha_contracts.execution import FactorValidationStatus

    base = _sample_factor_input()
    obs_id = base.observation_ids[0]

    def _m(metric: str, val: str) -> ToptMetricInput:
        return ToptMetricInput(
            input_id=obs_id,
            metric=metric,
            value=Decimal(val),
            confidence=Decimal("0.85"),
            knowable_at=KNOWN,
            freshness=MetricFreshness.FRESH,
            availability=MetricAvailability.AVAILABLE,
        )

    bank_input = base.model_copy(
        update={
            "issuer_id": "issuer:jpm",
            "instrument_id": "security:jpm",
            "listing_id": "listing:xnys:jpm",
            "operating_branch": OperatingBranch.FINANCIAL,
            "gross_profit": None,
            "pre_provision_profit": _m("pre_provision_profit", "86807000000"),
            "bank_net_revenue": _m("bank_net_revenue", "150000000000"),
            "stockholders_equity": _m("stockholders_equity", "362438000000"),
            "preferred_stock_value": _m("preferred_stock_value", "20045000000"),
            "goodwill": _m("goodwill", "52731000000"),
            "intangible_assets_net_excluding_goodwill": _m("intangible_assets_net_excluding_goodwill", "2600000000"),
        }
    )
    conn = _FakeNodeConnection()
    _materialize_issuer_tree(
        conn,  # type: ignore[arg-type]
        tree=GPPE_OPERATING_FINANCIAL_SPEC,
        tree_sha256=variant_sha256(GPPE_OPERATING_FINANCIAL_SPEC),
        factor_input=bank_input,
        issuer_class=IssuerClass.FINANCIAL,
        gppe_definition=GppeV0Definition(risk_free_rate=Decimal("0.05")),
        financial_payload={},
        periods=(date(2025, 12, 31), date(2025, 12, 31), date(2025, 12, 31)),
        freshness=MetricFreshness.FRESH,
        base_confidence=Decimal("0.85"),
        source_evidence=InputEvidenceStatus.VERIFIED,
        validation_status=FactorValidationStatus.ACCEPTED,
    )
    materialized_keys = {row[3] for row in conn.inserted_rows}
    assert "capital_charge" not in materialized_keys
    assert "total_gross_return" not in materialized_keys
    assert materialized_keys == {
        "tangible_deductions",
        "tangible_common_equity",
        "charged_tangible_common_equity",
        "capital_charge_tce",
        "capital_adjusted_operating_financial",
        "gppe_operating_financial_v1",
    }
    assert all(row[8] == "available" for row in conn.inserted_rows)
