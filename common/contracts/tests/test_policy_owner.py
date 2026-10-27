"""One owner for the source order of each fact family (#1518 step 1).

`truealpha_contracts.reconciliation_policies` holds one `ReconciliationPolicy` per family.
Each test below reorders one policy of that module. It then calls a deployed reader and
asserts that the reader follows the new order. The readers are the market-price failover,
`lookup_headcount`, the missing-cell planner and the invariant tool. A reader that keeps
its own literal order fails its test. No database is needed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from data_engine.datahub import quality_report
from data_engine.datahub.production_topt import headcount, twelve_data_origin
from data_engine.datahub.production_topt.market_price_adapter import CorroboratingOrigin, failover_order
from data_engine.datahub.production_topt.source_registrations import (
    MOOMOO_KLINE_MAPPING_VERSION,
    MOOMOO_KLINE_ORIGIN,
    MOOMOO_KLINE_PARSER_VERSION,
    MOOMOO_KLINE_VALUE_KEY,
)
from data_engine.datahub.standards.planner import UniverseIssuer, open_cells
from truealpha_contracts import metrics, reconciliation_policies
from truealpha_contracts.models import DataSource
from truealpha_contracts.reconciliation import ReconciliationPolicy
from truealpha_contracts.standards import STANDARDS, FactPlane
from truealpha_runtime.testing import load_tool

CUTOFF = datetime(2026, 9, 10, tzinfo=UTC)
ISSUER = UniverseIssuer(issuer_id="issuer:cik:320193", ticker="AAPL", listing_id="listing:xnas:aapl", cik=320193)


def _with_last_two_swapped(policy: ReconciliationPolicy) -> ReconciliationPolicy:
    """The same policy with its last two sources in the other order."""
    *head, second_last, last = policy.source_priority
    return ReconciliationPolicy(
        policy_version=policy.policy_version,
        source_priority=(*head, last, second_last),
        absolute_tolerance=policy.absolute_tolerance,
        relative_tolerance=policy.relative_tolerance,
        minimum_independent_origin_groups=policy.minimum_independent_origin_groups,
        conflict_behavior=policy.conflict_behavior,
    )


class _RecordingConnection:
    """Records each statement and its parameters. Finds no row."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> Any:
        self.statements.append((" ".join(sql.split()), params))
        return type("_Cursor", (), {"fetchone": lambda _self: None, "fetchall": lambda _self: []})()


def _twelve_data() -> CorroboratingOrigin:
    return CorroboratingOrigin(
        origin=twelve_data_origin.ORIGIN,
        parser_version=twelve_data_origin.PARSER_VERSION,
        mapping_version=twelve_data_origin.MAPPING_VERSION,
        value_key=twelve_data_origin.VALUE_KEY,
        confidence=Decimal("0.85"),
        fetch=lambda symbol, cutoff: None,
    )


def _moomoo_kline() -> CorroboratingOrigin:
    return CorroboratingOrigin(
        origin=MOOMOO_KLINE_ORIGIN,
        parser_version=MOOMOO_KLINE_PARSER_VERSION,
        mapping_version=MOOMOO_KLINE_MAPPING_VERSION,
        value_key=MOOMOO_KLINE_VALUE_KEY,
        confidence=Decimal("0.80"),
        fetch=lambda symbol, cutoff: None,
        raw_source=DataSource.MOOMOO,
    )


def test_the_price_failover_follows_the_price_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    twelve, moomoo = _twelve_data(), _moomoo_kline()
    policy = reconciliation_policies.RECONCILIATION_POLICY
    assert policy.source_priority.index("twelve-data:v1") < policy.source_priority.index("moomoo-kline:v1")
    assert failover_order([moomoo, twelve]) == (twelve, moomoo)

    monkeypatch.setattr(reconciliation_policies, "RECONCILIATION_POLICY", _with_last_two_swapped(policy))

    assert failover_order([twelve, moomoo]) == (moomoo, twelve)


def test_lookup_headcount_follows_the_headcount_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = reconciliation_policies.HEADCOUNT_RECONCILIATION_POLICY
    before = _RecordingConnection()
    headcount.lookup_headcount(before, 320193, date(2026, 9, 10))
    assert before.statements[0][1][2] == list(policy.source_priority)

    reordered = _with_last_two_swapped(policy)
    assert reordered.source_priority != policy.source_priority
    monkeypatch.setattr(reconciliation_policies, "HEADCOUNT_RECONCILIATION_POLICY", reordered)
    after = _RecordingConnection()
    headcount.lookup_headcount(after, 320193, date(2026, 9, 10))

    sql, params = after.statements[0]
    assert "order by array_position(%s::text[], source) nulls last" in sql
    assert params[2] == list(reordered.source_priority)


def test_the_planner_sql_follows_the_policy_that_the_plane_names() -> None:
    standard = STANDARDS["employees_total"]
    before = _RecordingConnection()
    open_cells(before, [ISSUER], standard=standard, cutoff=CUTOFF)
    sql, params = before.statements[0]
    assert "order by array_position(%s::text[], source) nulls last" in sql
    assert params[-1] == list(reconciliation_policies.HEADCOUNT_RECONCILIATION_POLICY.source_priority)

    reordered = _with_last_two_swapped(standard.plane.reconciliation_policy)
    plane = standard.plane.model_copy(update={"reconciliation_policy": reordered})
    after = _RecordingConnection()
    open_cells(after, [ISSUER], standard=standard.model_copy(update={"plane": plane}), cutoff=CUTOFF)

    assert after.statements[0][1][-1] == list(reordered.source_priority)
    assert after.statements[0][1][-1] != params[-1]


def test_each_registered_plane_names_the_policy_of_the_contract_module() -> None:
    assert (
        STANDARDS["employees_total"].plane.reconciliation_policy
        is reconciliation_policies.HEADCOUNT_RECONCILIATION_POLICY
    )
    assert (
        STANDARDS["segment_revenue"].plane.reconciliation_policy
        is reconciliation_policies.SEGMENT_REVENUE_RECONCILIATION_POLICY
    )


def test_the_invariant_tool_ranks_the_price_origins_by_the_price_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = load_tool("output_invariants")
    before = dict(tool._market_price_parser_ranks())
    assert before["twelve-data-parser"] < before["moomoo-kline-parser"]

    monkeypatch.setattr(
        reconciliation_policies,
        "RECONCILIATION_POLICY",
        _with_last_two_swapped(reconciliation_policies.RECONCILIATION_POLICY),
    )
    after = dict(tool._market_price_parser_ranks())

    assert after["moomoo-kline-parser"] < after["twelve-data-parser"]


def test_the_volume_policy_takes_the_order_of_the_price_policy() -> None:
    assert (
        reconciliation_policies.VOLUME_RECONCILIATION_POLICY.source_priority
        == reconciliation_policies.RECONCILIATION_POLICY.source_priority
    )


def test_the_report_module_declares_no_policy_of_its_own() -> None:
    for name in ("RECONCILIATION_POLICY", "FINANCIAL_FACT_RECONCILIATION_POLICY", "FIELD_RECONCILIATION_POLICIES"):
        assert getattr(quality_report, name) is getattr(reconciliation_policies, name), name


def test_the_deleted_owners_stay_deleted() -> None:
    assert not hasattr(metrics, "fusion_rank")
    assert not hasattr(metrics, "source_priority")
    assert not hasattr(metrics, "FUSION_RULESET_VERSION")
    assert "source_priority" not in metrics.MetricSpec.model_fields
    assert "source_priority" not in FactPlane.model_fields
    assert not hasattr(headcount, "HEADCOUNT_SOURCE_PRIORITY")
    assert not hasattr(headcount, "_ACTIVE_HEADCOUNT_SOURCES")
    assert not hasattr(quality_report, "_PRICE_SOURCE_PRIORITY")
