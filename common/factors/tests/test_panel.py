"""Tests for the standardized wide panel and native Polars pl.Expr evaluation (`factors.panel`, #1610)."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import polars as pl
import pytest
from factors.panel import (
    EXPR_CAPITAL_ADJUSTED_GROSS_PROFIT,
    EXPR_CAPITAL_ADJUSTED_OPERATING_FINANCIAL,
    EXPR_CAPITAL_ADJUSTED_TCE,
    EXPR_CAPITAL_CHARGE,
    EXPR_CAPITAL_CHARGE_TCE,
    EXPR_CHARGED_TANGIBLE_COMMON_EQUITY,
    EXPR_GPPE_BANKING_TCE_V1,
    EXPR_GPPE_OPERATING_FINANCIAL_V1,
    EXPR_GPPE_UNIFORM_CHARGE_V0,
    EXPR_OPERATING_GROSS_PROFIT,
    EXPR_PEG,
    EXPR_POSITIVE_TANGIBLE_COMMON_EQUITY,
    EXPR_PRICE_TO_GROSS_PROFIT,
    EXPR_PRICE_TO_SALES,
    EXPR_TANGIBLE_COMMON_EQUITY,
    EXPR_TANGIBLE_DEDUCTIONS,
    EXPR_TANGIBLE_DEDUCTIONS_FILL_NULL,
    EXPR_TOTAL_GROSS_RETURN,
    FACTOR_EXPRESSIONS,
    GPPE_V0_SPEC,
    INTERMEDIATE_EXPRESSIONS,
    MINTED_NODE_IDS,
    MINTED_TREE_IDS,
    PANEL_EXPRESSIONS,
    PANEL_METRICS,
    PANEL_VARIANTS,
    PHYSICALLY_NON_NEGATIVE,
    IssuerClass,
    SignPolicy,
    build_wide_panel,
    consumed_columns,
    evaluate_panel_variant,
    evaluate_wide_panel,
    judge_sign,
    leaf_consumed_columns,
    node_id_for,
    published_node,
    tree_id_for,
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
    compute_topt_gppe,
)

D = Decimal

#: Frozen tree content hashes (#1176, #1518). Any change to a reached node, decomposition,
#: or arithmetic precision changes the hash and fails here.
FROZEN_TREES: dict[tuple[str, str], tuple[UUID, str]] = {
    ("gppe", "production-topt-v0.2.0"): (
        UUID("ef13b940-b695-5f5f-846a-a36face5cc3e"),
        "9015a3713fd159cf5ba9014eb6ecbb14551da177c14bbd5d7acc17b1e2177906",
    ),
    ("gppe_banking_tce", "v2"): (
        UUID("96fd6d4a-8983-5caf-b647-360122569d00"),
        "a6d6ca21a0a92144e46a95054b111ad4e5cd0455dcb7741991e6660e7fc8153b",
    ),
    ("gppe_operating_financial", "1.0.0"): (
        UUID("7183c828-7252-5591-81a8-99fd05737995"),
        "39f0b445af54adcb0ca9c3a028c45adc353523657b336f61e387e6720f7b1a6d",
    ),
}

#: Exact 34-digit Decimal golden values from the TOPT v0.2.0 fixtures.
GOLDEN_GPPE: list[tuple[IssuerClass, dict[str, D | None], D | None, D | None]] = [
    (
        IssuerClass.NON_FINANCIAL,
        {"gross_profit": D("210000000"), "total_assets": D("200000000"), "employees_total": D("100")},
        D("200000000.00"),
        D("2000000"),
    ),
    (
        IssuerClass.FINANCIAL,
        {"pre_provision_profit": D("80000000"), "total_assets": D("200000000"), "employees_total": D("100")},
        D("70000000.00"),
        D("700000"),
    ),
    (
        IssuerClass.INSURANCE,
        {"gross_profit": D("50000000"), "total_assets": D("200000000"), "employees_total": D("100")},
        D("40000000.00"),
        D("400000"),
    ),
    (
        IssuerClass.FINANCIAL,
        {"pre_provision_profit": D("86807000000"), "total_assets": D("4424900000000"), "employees_total": D("318512")},
        D("-134438000000.00"),
        D("-422081.4286431908373938815492037977"),
    ),
]

JPM_TCE_LEAVES = {
    "stockholders_equity": "362438000000",
    "preferred_stock_value": "20045000000",
    "goodwill": "52731000000",
    "intangible_assets_net_excluding_goodwill": "2600000000",
}

GOLDEN_TOPT_GPPE = {
    "insurer": (
        ("insurance", "31700000000", None, "300000000000", "48000", "0.05"),
        "topt-gppe-result:774f50a7825428bb7e9b307ca6a9428e89a7fbe58b60830fddd5d81954a1b9e7",
        "347916.6666666666666666666666666667",
    ),
    "nvda_shaped": (
        ("non_financial", "97858000000", None, "111601000000", "36000", "0.05"),
        "topt-gppe-result:71b56deff8a6c2430782aeea681d03d376a3bab058b422a6bcbafb60066a4182",
        "2563276.388888888888888888888888889",
    ),
    "thin_margin_negative": (
        ("non_financial", "1000000", None, "90000000", "7", "0.05"),
        "topt-gppe-result:dab2e610a10cfeb88fa7ba5fd11aa198218ee6687675a865e4b90160000e5092",
        "-5E+5",
    ),
    "non_terminating": (
        ("non_financial", "210000000", None, "200000001", "7", "0.0437"),
        "topt-gppe-result:f563de9c3b3434955d7e83d39cd3d64e8b110eb8f74330ba323b67078345ad29",
        "28751428.56518571428571428571428571",
    ),
    "bank_without_ppnr": (
        ("financial", "5", None, "4424900000000", "318512", "0.05"),
        "topt-gppe-result:b06da6b18be6cc251b20b6f938d0e2923f948600293e2dae2dc5bfe9adcfcff4",
        None,
    ),
}


def test_wide_panel_evaluates_all_gppe_variants_intermediates_and_valuation_factors() -> None:
    """Standardized wide panel `((issuer_id, period) × METRICS)` evaluates all GPPE variants,
    intermediates, and valuation/growth factors via native Polars `pl.Expr`."""
    rows = [
        # Non-financial issuer across 4 annual periods (enables 3-year PEG window)
        {
            "issuer_id": "issuer:nvda",
            "period": "2022-12-31",
            "issuer_class": IssuerClass.NON_FINANCIAL.value,
            "gross_profit": 100_000_000.0,
            "financial_returns": None,
            "total_assets": 200_000_000.0,
            "risk_free_rate": 0.05,
            "employees_total": 100.0,
            "price": 50.0,
            "shares_outstanding": 1_000_000.0,
            "revenue": 20_000_000.0,
            "net_income": 100.0,
            "conf__gross_profit": 0.90,
            "conf__financial_returns": 0.85,
            "conf__total_assets": 0.80,
            "conf__employees_total": 0.95,
            "conf__price": 0.92,
            "conf__shares_outstanding": 0.88,
            "conf__revenue": 0.91,
            "conf__net_income": 0.90,
        },
        {
            "issuer_id": "issuer:nvda",
            "period": "2023-12-31",
            "issuer_class": IssuerClass.NON_FINANCIAL.value,
            "gross_profit": 120_000_000.0,
            "financial_returns": 2_000_000.0,
            "total_assets": 200_000_000.0,
            "risk_free_rate": 0.05,
            "employees_total": 100.0,
            "price": 50.0,
            "shares_outstanding": 1_000_000.0,
            "revenue": 20_000_000.0,
            "net_income": 120.0,
            "conf__gross_profit": 0.90,
            "conf__financial_returns": 0.85,
            "conf__total_assets": 0.80,
            "conf__employees_total": 0.95,
            "conf__price": 0.92,
            "conf__shares_outstanding": 0.88,
            "conf__revenue": 0.91,
            "conf__net_income": 0.70,
        },
        {
            "issuer_id": "issuer:nvda",
            "period": "2024-12-31",
            "issuer_class": IssuerClass.NON_FINANCIAL.value,
            "gross_profit": 150_000_000.0,
            "financial_returns": 3_000_000.0,
            "total_assets": 200_000_000.0,
            "risk_free_rate": 0.05,
            "employees_total": 100.0,
            "price": 50.0,
            "shares_outstanding": 1_000_000.0,
            "revenue": 20_000_000.0,
            "net_income": 150.0,
            "conf__gross_profit": 0.90,
            "conf__financial_returns": 0.85,
            "conf__total_assets": 0.80,
            "conf__employees_total": 0.95,
            "conf__price": 0.92,
            "conf__shares_outstanding": 0.88,
            "conf__revenue": 0.91,
            "conf__net_income": 0.90,
        },
        {
            "issuer_id": "issuer:nvda",
            "period": "2025-12-31",
            "issuer_class": IssuerClass.NON_FINANCIAL.value,
            "gross_profit": 210_000_000.0,
            "financial_returns": 5_000_000.0,
            "total_assets": 200_000_000.0,
            "risk_free_rate": 0.05,
            "employees_total": 100.0,
            "price": 50.0,
            "shares_outstanding": 1_000_000.0,
            "revenue": 20_000_000.0,
            "net_income": 200.0,
            "conf__gross_profit": 0.90,
            "conf__financial_returns": 0.85,
            "conf__total_assets": 0.80,
            "conf__employees_total": 0.95,
            "conf__price": 0.92,
            "conf__shares_outstanding": 0.88,
            "conf__revenue": 0.91,
            "conf__net_income": 0.90,
        },
        # Financial issuer (JPM FY2025)
        {
            "issuer_id": "issuer:jpm",
            "period": "2025-12-31",
            "issuer_class": IssuerClass.FINANCIAL.value,
            "pre_provision_profit": 86_807_000_000.0,
            "bank_net_revenue": 177_560_000_000.0,
            "total_assets": 4_424_900_000_000.0,
            "stockholders_equity": 362_438_000_000.0,
            "preferred_stock_value": 20_045_000_000.0,
            "goodwill": 52_731_000_000.0,
            "intangible_assets_net_excluding_goodwill": 2_600_000_000.0,
            "risk_free_rate": 0.05,
            "employees_total": 318_512.0,
            "conf__pre_provision_profit": 0.92,
            "conf__bank_net_revenue": 0.91,
            "conf__total_assets": 0.95,
            "conf__stockholders_equity": 0.90,
            "conf__preferred_stock_value": 0.89,
            "conf__goodwill": 0.88,
            "conf__intangible_assets_net_excluding_goodwill": 0.87,
            "conf__employees_total": 0.75,
        },
    ]

    panel = build_wide_panel(rows)
    evaluated = evaluate_wide_panel(panel)

    nvda_2025 = evaluated.filter((pl.col("issuer_id") == "issuer:nvda") & (pl.col("period") == "2025-12-31")).row(
        0, named=True
    )
    assert nvda_2025["operating_gross_profit"] == pytest.approx(210_000_000.0)
    assert nvda_2025["capital_charge"] == pytest.approx(10_000_000.0)
    assert nvda_2025["capital_adjusted_gross_profit"] == pytest.approx(200_000_000.0)
    assert nvda_2025["gppe_uniform_charge_v0"] == pytest.approx(2_000_000.0)
    assert nvda_2025["total_gross_return"] == pytest.approx(215_000_000.0)
    assert nvda_2025["capital_adjusted_operating_financial"] == pytest.approx(205_000_000.0)
    assert nvda_2025["gppe_operating_financial_v1"] == pytest.approx(2_050_000.0)
    assert nvda_2025["price_to_sales"] == pytest.approx(2.5)
    assert nvda_2025["price_to_gross_profit"] == pytest.approx(50_000_000.0 / 210_000_000.0)
    assert nvda_2025["peg"] == pytest.approx(250_000.0 / (1700.0 / 60.0), rel=1e-9)

    # Check null-filling on NVDA 2022 where financial_returns is None -> coalesces to 0
    nvda_2022 = evaluated.filter((pl.col("issuer_id") == "issuer:nvda") & (pl.col("period") == "2022-12-31")).row(
        0, named=True
    )
    assert nvda_2022["total_gross_return"] == pytest.approx(100_000_000.0)
    assert nvda_2022["peg"] is None  # first year in 4-period window has no 3-year lookback

    # Check confidence propagation via pl.min_horizontal(...)
    # PEG confidence at 2025-12-31 includes 2023 net_income confidence (0.70) via .shift(2).over("issuer_id")
    assert nvda_2025["conf__peg"] == pytest.approx(0.70)
    assert nvda_2025["conf__price_to_sales"] == pytest.approx(0.88)

    # Check bank TCE intermediates and roots on JPM 2025
    jpm_2025 = evaluated.filter(pl.col("issuer_id") == "issuer:jpm").row(0, named=True)
    assert jpm_2025["tangible_deductions"] == pytest.approx(75_376_000_000.0)
    assert jpm_2025["tangible_common_equity"] == pytest.approx(287_062_000_000.0)
    assert jpm_2025["charged_tangible_common_equity"] == pytest.approx(287_062_000_000.0)
    assert jpm_2025["capital_charge_tce"] == pytest.approx(14_353_100_000.0)
    assert jpm_2025["capital_adjusted_tce"] == pytest.approx(72_453_900_000.0)
    assert jpm_2025["gppe_banking_tce_v1"] == pytest.approx(72_453_900_000.0 / 318_512.0, rel=1e-12)
    assert jpm_2025["gppe_uniform_charge_v0"] == pytest.approx(-422081.42864319084, rel=1e-12)
    assert jpm_2025["gppe_operating_financial_v1"] == pytest.approx(
        (177_560_000_000.0 - 14_353_100_000.0) / 318_512.0, rel=1e-12
    )


def test_consumed_columns_uses_expr_meta_root_names() -> None:
    """Consumed input columns are discovered automatically via `expr.meta.root_names()`."""
    assert set(consumed_columns(EXPR_PRICE_TO_SALES)) == {"price", "shares_outstanding", "revenue"}
    assert set(consumed_columns(EXPR_PRICE_TO_GROSS_PROFIT)) == {"price", "shares_outstanding", "gross_profit"}
    assert set(consumed_columns(EXPR_PEG)) == {"price", "shares_outstanding", "net_income"}
    assert set(consumed_columns(EXPR_TANGIBLE_DEDUCTIONS)) == {
        "preferred_stock_value",
        "goodwill",
        "intangible_assets_net_excluding_goodwill",
    }
    assert set(consumed_columns(EXPR_TOTAL_GROSS_RETURN)) == {"gross_profit", "financial_returns"}

    # Recursive leaf expansion for composite expressions
    assert set(leaf_consumed_columns("gppe_uniform_charge_v0", issuer_class=IssuerClass.NON_FINANCIAL)) == {
        "gross_profit",
        "total_assets",
        "risk_free_rate",
        "employees_total",
    }
    assert set(leaf_consumed_columns("gppe_banking_tce_v1", issuer_class=IssuerClass.FINANCIAL)) == {
        "pre_provision_profit",
        "stockholders_equity",
        "preferred_stock_value",
        "goodwill",
        "intangible_assets_net_excluding_goodwill",
        "risk_free_rate",
        "employees_total",
    }
    assert set(PANEL_EXPRESSIONS) == set(INTERMEDIATE_EXPRESSIONS) | set(FACTOR_EXPRESSIONS)
    panel_meta = {expr.meta for expr in PANEL_EXPRESSIONS.values()}
    assert {
        EXPR_OPERATING_GROSS_PROFIT.meta,
        EXPR_CAPITAL_CHARGE.meta,
        EXPR_CAPITAL_ADJUSTED_GROSS_PROFIT.meta,
        EXPR_TANGIBLE_COMMON_EQUITY.meta,
        EXPR_CHARGED_TANGIBLE_COMMON_EQUITY.meta,
        EXPR_CAPITAL_CHARGE_TCE.meta,
        EXPR_CAPITAL_ADJUSTED_TCE.meta,
        EXPR_CAPITAL_ADJUSTED_OPERATING_FINANCIAL.meta,
        EXPR_GPPE_UNIFORM_CHARGE_V0.meta,
        EXPR_GPPE_BANKING_TCE_V1.meta,
        EXPR_GPPE_OPERATING_FINANCIAL_V1.meta,
    } <= panel_meta


def test_null_filling_and_positivity_conditions_in_polars_expressions() -> None:
    """Null-filling (`fill_null(0)`) and positivity conditions (`> 0` / `>= 0`) behave as declared."""
    df = pl.DataFrame(
        {
            "issuer_id": ["bank:pos", "bank:zero", "bank:neg"],
            "period": ["2025-12-31"] * 3,
            "stockholders_equity": [100.0, 50.0, 30.0],
            "preferred_stock_value": [10.0, 20.0, 20.0],
            "goodwill": [None, 30.0, 20.0],
            "intangible_assets_net_excluding_goodwill": [None, None, 10.0],
        }
    )
    res = (
        df.with_columns(tangible_deductions=EXPR_TANGIBLE_DEDUCTIONS_FILL_NULL)
        .with_columns(tangible_common_equity=EXPR_TANGIBLE_COMMON_EQUITY)
        .with_columns(
            charged_tce=EXPR_CHARGED_TANGIBLE_COMMON_EQUITY,
            positive_tce=EXPR_POSITIVE_TANGIBLE_COMMON_EQUITY,
        )
    )
    assert res["tangible_deductions"].to_list() == [10.0, 50.0, 50.0]
    assert res["tangible_common_equity"].to_list() == [90.0, 0.0, -20.0]
    assert res["charged_tce"].to_list() == [90.0, 0.0, None]
    assert res["positive_tce"].to_list() == [90.0, None, None]


def test_deterministic_node_and_tree_uuids_preserve_minted_identities_and_use_uuid5_for_new_keys() -> None:
    """Existing 26 nodes and 3 variants keep their minted UUIDs; new factor keys derive deterministic uuid5."""
    for key, expected_uuid in MINTED_NODE_IDS.items():
        assert node_id_for(key) == expected_uuid
        assert PANEL_METRICS[key].node_id == expected_uuid
    for (t_key, t_ver), expected_uuid in MINTED_TREE_IDS.items():
        assert tree_id_for(t_key, t_ver) == expected_uuid
        assert PANEL_VARIANTS[(t_key, t_ver)].tree_id == expected_uuid

    new_id_1 = node_id_for("price_to_sales")
    new_id_2 = node_id_for("price_to_sales")
    assert new_id_1 == new_id_2
    assert new_id_1 not in set(MINTED_NODE_IDS.values())


@pytest.mark.parametrize(("coord", "expected"), sorted(FROZEN_TREES.items()))
def test_registered_trees_match_frozen_uuids_and_content_hashes(
    coord: tuple[str, str], expected: tuple[UUID, str]
) -> None:
    variant = PANEL_VARIANTS[coord]
    expected_id, expected_sha = expected
    assert variant.tree_id == expected_id
    assert variant_sha256(variant) == expected_sha


@pytest.mark.parametrize(
    ("issuer_class", "inputs", "expected_cap_adj", "expected_gppe"),
    GOLDEN_GPPE,
)
def test_gppe_v0_tree_reproduces_v020_golden_decimals(
    issuer_class: IssuerClass,
    inputs: dict[str, D | None],
    expected_cap_adj: D | None,
    expected_gppe: D | None,
) -> None:
    ev = evaluate_panel_variant(
        GPPE_V0_SPEC,
        issuer_class=issuer_class,
        inputs={**inputs, "risk_free_rate": D("0.05")},
    )
    assert ev.values["gppe_uniform_charge_v0"] == expected_gppe
    assert ev.values["capital_adjusted_gross_profit"] == (
        None if expected_cap_adj is None else expected_cap_adj.normalize()
    )


@pytest.mark.parametrize("case", sorted(GOLDEN_TOPT_GPPE))
def test_compute_topt_gppe_reproduces_the_pre_forest_result_identity(case: str) -> None:
    from datetime import UTC, datetime, timedelta

    from truealpha_contracts.strategy import load_live_strategy_definition

    (branch, gross_profit, ppnr, assets, headcount, rate), result_id, gppe = GOLDEN_TOPT_GPPE[case]
    cutoff = datetime(2026, 9, 16, tzinfo=UTC)
    known = cutoff - timedelta(days=1)
    obs_ids = tuple(f"normalized-observation:{ch * 64}" for ch in "1234")

    def _m(metric: str, val: str | None, *, input_id: str = obs_ids[0]) -> ToptMetricInput:
        return ToptMetricInput(
            input_id=input_id,
            metric=metric,
            value=val,
            confidence="0.9",
            knowable_at=known,
            freshness=MetricFreshness.FRESH,
            availability=MetricAvailability.AVAILABLE if val is not None else MetricAvailability.UNAVAILABLE,
        )

    leaves = JPM_TCE_LEAVES if branch == "financial" else {}
    snap = ToptCoreSnapshotInput.model_validate(
        {
            "snapshot_id": f"topt-core-snapshot:{'a' * 64}",
            "run_id": f"capture-run:{'b' * 64}",
            "release_manifest_id": f"release-manifest:{'c' * 64}",
            "universe_id": "universe:topt-candidate-v1",
            "universe_version": "2026-03-31-v1",
            "universe_sha256": "d" * 64,
            "cutoff": cutoff,
            "issuer_id": "issuer:example",
            "instrument_id": "instrument:example",
            "listing_id": "listing:example",
            "operating_branch": OperatingBranch(branch),
            "observation_ids": obs_ids,
            "cell_inputs": tuple(
                ToptCellQualityInput(
                    input_id=oid,
                    confidence="0.9",
                    knowable_at=known,
                    freshness=MetricFreshness.FRESH,
                )
                for oid in obs_ids
            ),
            "gross_profit": _m("gross_profit", gross_profit),
            "pre_provision_profit": _m("pre_provision_profit", ppnr),
            "total_assets": _m("total_assets", assets),
            "headcount": _m("headcount", headcount),
            "revenue": _m("revenue", "100000000"),
            **{name: _m(name, value) for name, value in leaves.items()},
            "market_value_components": (
                ToptMarketValueComponent(
                    instrument_id="instrument:example",
                    listing_id="listing:example",
                    market_price=_m("market_price", "40", input_id=obs_ids[1]),
                    shares_outstanding=_m("shares_outstanding", "10000000", input_id=obs_ids[0]),
                ),
            ),
        }
    )
    res = compute_topt_gppe(
        snap,
        invocation_id=f"topt-gppe-invocation:{'f' * 64}",
        gppe_definition=GppeV0Definition(risk_free_rate=rate),
        definition=load_live_strategy_definition(),
    )
    assert (None if res.gppe is None else str(res.gppe)) == gppe
    assert res.result_id == result_id


def test_must_be_non_negative_is_reserved_for_physical_quantities_and_judge_sign_enforces_it() -> None:
    """Every node with `must-be-non-negative` is in `PHYSICALLY_NON_NEGATIVE`, and `judge_sign` distinguishes
    violations from low signals."""
    non_neg_nodes = {
        node.key for node in PANEL_METRICS.values() if SignPolicy.MUST_BE_NON_NEGATIVE in node.sign_policy.values()
    }
    assert non_neg_nodes == set(PHYSICALLY_NON_NEGATIVE)

    gppe_node = published_node("mart.topt_gppe_results", "gppe")
    finding_signal = judge_sign(
        gppe_node,
        issuer_class="financial",
        value=D("-422081.43"),
        subject="listing:xnys:jpm",
    )
    assert finding_signal is not None
    assert finding_signal.signal is True
    assert finding_signal.violation is False

    assets_node = PANEL_METRICS["total_assets"]
    finding_violation = judge_sign(
        assets_node,
        issuer_class="non_financial",
        value=D("-1"),
        subject="listing:xnas:nvda",
    )
    assert finding_violation is not None
    assert finding_violation.violation is True
    assert finding_violation.signal is False
