"""Tests for #1413 (Root 4): F2, F3, F4, B1, B2, and D6."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

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
from factors.forest import (
    FOREST,
    Forest,
    IssuerClass,
    MetricNode,
    MetricTree,
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


def test_f2_json_loaded_tree_materializes_node_values_without_ddl_or_python_edit() -> None:
    """F2 standing check: adding a new tree loaded from JSON into a Forest materializes
    mart.metric_node_values rows with zero migration or Python code change."""
    custom_node = MetricNode.model_validate(
        {
            "node_id": "11111111-2222-5333-8444-555555555555",
            "key": "gppe_raw",
            "kind": "derived",
            "definition": "Uncharged gross profit per employee loaded from JSON.",
            "unit": "per_employee",
            "period": "fiscal-year-flow",
            "applicability": ["non_financial"],
            "sign_policy": {"non_financial": "sign-is-signal"},
            "provenance": {"kind": "derived", "reference": "gppe_raw_json", "note": ""},
            "aliases": [],
        }
    )
    custom_tree = MetricTree.model_validate(
        {
            "tree_id": "66666666-7777-5888-9999-aaaaaaaaaaaa",
            "key": "gppe_raw_json",
            "version": "v1",
            "realizes": "labor_efficiency",
            "root": "gppe_raw",
            "applicability": ["non_financial"],
            "decompositions": [
                {
                    "output": "gppe_raw",
                    "formula_id": "ratio",
                    "formula_version": 1,
                    "operands": {"non_financial": ["gross_profit", "employees_total"]},
                }
            ],
            "decimal_precision": 34,
            "rounding": "ROUND_HALF_EVEN",
        }
    )
    extended_forest = Forest(nodes=(*FOREST.nodes, custom_node), trees=(*FOREST.trees, custom_tree))

    conn = _FakeNodeConnection()
    factor_input = _sample_factor_input()
    gppe_def = GppeV0Definition(risk_free_rate=Decimal("0.05"))
    count = materialize_metric_node_values(
        conn,  # type: ignore[arg-type]
        factor_inputs=(factor_input,),
        gppe_definition=gppe_def,
        tier_definition_id="strategy-definition:" + "e" * 64,
        periods_by_listing={"listing:xnas:nvda": (date(2025, 12, 31), date(2025, 12, 31), date(2025, 12, 31))},
        evidence_by_listing={"listing:xnas:nvda": InputEvidenceStatus.VERIFIED},
        forest=extended_forest,
    )
    # 4 derived nodes from GPPE_V0_TREE + 1 derived node from custom_tree = 5 rows
    assert count == 5
    by_key = {row[3]: row for row in conn.inserted_rows}
    assert "gppe_uniform_charge_v0" in by_key
    assert "gppe_raw" in by_key
    raw_row = by_key["gppe_raw"]
    assert raw_row[2] == UUID("11111111-2222-5333-8444-555555555555")
    assert raw_row[6] == Decimal("10000000")  # 10B / 1000
    assert raw_row[8] == "available"
    assert raw_row[10] == "verified"

    unverified_conn = _FakeNodeConnection()
    materialize_metric_node_values(
        unverified_conn,  # type: ignore[arg-type]
        factor_inputs=(factor_input,),
        gppe_definition=gppe_def,
        tier_definition_id="strategy-definition:" + "e" * 64,
        periods_by_listing={"listing:xnas:nvda": (date(2025, 12, 31), date(2025, 12, 31), date(2025, 12, 31))},
        evidence_by_listing={},
        forest=extended_forest,
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
    assert live.content_sha256 == "e6a49d6cee179d393bb71c89cb5f20b3a17039e4123532c8cbf92c54b30e5604"

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
