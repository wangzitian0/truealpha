"""GPPE v0.2.0 as a registered tree changes no number (#528, docs/metric-forest.md §4).

Three checks, from weakest to strongest:

1. The tree, evaluated alone, equals the hand-written v0.2.0 arithmetic (kept below verbatim as
   `_v020_kernel`) over a grid that crosses every issuer class with negative, non-terminating
   and large values.
2. `compute_topt_gppe`, which now evaluates the tree, reproduces the content-addressed result
   identities captured from `main@f5807a7` BEFORE the kernel was replaced. A result id hashes
   every published field, so an equal id means byte-identical rows in `mart.topt_gppe_results`.
3. The tree's identity is frozen with its version: an edit to what the tree computes, with no
   version bump, turns this file red.
"""

from __future__ import annotations

import itertools
import random
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

import pytest
from factors.forest import (
    FOREST,
    GPPE_BANKING_TCE_TREE,
    GPPE_V0_TREE,
    AliasKind,
    IssuerClass,
    NodeKind,
    evaluate,
    required_inputs,
)
from factors.production_topt import (
    TCE_SNAPSHOT_FIELDS,
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
from truealpha_contracts.strategy import load_live_strategy_definition

CUTOFF = datetime(2026, 9, 16, tzinfo=UTC)
IDS = tuple(f"normalized-observation:{character * 64}" for character in "1234")


def _v020_kernel(branch: str, numerator: Decimal, total_assets: Decimal, headcount: Decimal, rate: Decimal):
    """compute_topt_gppe's arithmetic as it stood on main@f5807a7 (core.py L664-672)."""
    del branch  # the numerator was already chosen per branch; the charge was uniform
    with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
        capital_adjusted = numerator - (total_assets * rate)
        gppe = capital_adjusted / headcount
    return capital_adjusted, gppe


def _numerator_key(issuer_class: IssuerClass) -> str:
    return "pre_provision_profit" if issuer_class is IssuerClass.FINANCIAL else "gross_profit"


def test_the_tree_equals_the_v020_kernel_over_a_grid() -> None:
    numerators = ("86807000000", "-1250000", "0", "210000000", "97858000000.5")
    assets = ("4424900000000", "0", "200000001", "90000000")
    headcounts = ("318512", "7", "1", "36000")
    rates = ("0.05", "0.038", "0.0437", "0")
    generator = random.Random(528)
    randomized = [
        (
            str(generator.randint(-(10**13), 10**13)),
            str(generator.randint(0, 10**14)),
            str(generator.randint(1, 10**6)),
            f"0.{generator.randint(0, 10**6):06d}",
        )
        for _ in range(200)
    ]
    cases = [*itertools.product(numerators, assets, headcounts, rates), *randomized]
    for issuer_class in IssuerClass:
        for numerator, total_assets, headcount, rate in cases:
            evaluation = evaluate(
                FOREST,
                GPPE_V0_TREE,
                issuer_class=issuer_class,
                inputs={
                    _numerator_key(issuer_class): Decimal(numerator),
                    "total_assets": Decimal(total_assets),
                    "employees_total": Decimal(headcount),
                    "risk_free_rate": Decimal(rate),
                },
            )
            expected = _v020_kernel(
                issuer_class.value, Decimal(numerator), Decimal(total_assets), Decimal(headcount), Decimal(rate)
            )
            actual = (evaluation.values["capital_adjusted_gross_profit"], evaluation.values["gppe_uniform_charge_v0"])
            # Equal AND identically represented: `Decimal("1.0") == Decimal("1")` would hide an
            # exponent change, and the result identity hashes the text.
            assert actual == expected and tuple(map(str, actual)) == tuple(map(str, expected)), (
                issuer_class,
                numerator,
                total_assets,
                headcount,
                rate,
            )
            assert evaluation.undefined == {}


def test_each_class_needs_exactly_the_inputs_the_kernel_read() -> None:
    needed = {
        issuer_class: {node.key for node in required_inputs(FOREST, GPPE_V0_TREE, issuer_class)}
        for issuer_class in IssuerClass
    }
    assert needed == {
        IssuerClass.NON_FINANCIAL: {"gross_profit", "total_assets", "employees_total", "risk_free_rate"},
        IssuerClass.INSURANCE: {"gross_profit", "total_assets", "employees_total", "risk_free_rate"},
        IssuerClass.FINANCIAL: {"pre_provision_profit", "total_assets", "employees_total", "risk_free_rate"},
    }


def test_a_missing_input_or_a_zero_denominator_is_undefined_never_a_number() -> None:
    missing = evaluate(
        FOREST,
        GPPE_V0_TREE,
        issuer_class=IssuerClass.FINANCIAL,
        inputs={"total_assets": Decimal("1"), "employees_total": Decimal("1"), "risk_free_rate": Decimal("0.05")},
    )
    assert missing.values["gppe_uniform_charge_v0"] is None
    assert missing.undefined["pre_provision_profit"] == "missing_input"
    assert missing.undefined["gppe_uniform_charge_v0"] == "undefined_operand"
    zero = evaluate(
        FOREST,
        GPPE_V0_TREE,
        issuer_class=IssuerClass.NON_FINANCIAL,
        inputs={
            "gross_profit": Decimal("5"),
            "total_assets": Decimal("1"),
            "employees_total": Decimal("0"),
            "risk_free_rate": Decimal("0.05"),
        },
    )
    assert zero.values["gppe_uniform_charge_v0"] is None and zero.undefined == {
        "gppe_uniform_charge_v0": "zero_denominator"
    }


def _metric(name: str, value: str | None, input_id: str = IDS[0]) -> ToptMetricInput:
    return ToptMetricInput(
        input_id=input_id,
        metric=name,
        value=value,
        unit="USD",
        confidence="0.9",
        knowable_at=CUTOFF - timedelta(days=1),
        freshness=MetricFreshness.FRESH,
        availability=MetricAvailability.AVAILABLE if value is not None else MetricAvailability.UNAVAILABLE,
    )


#: JPM FY2025 10-K (accession 0001628280-26-008131): the four captured leaves of the banking tree.
JPM_TCE_LEAVES = {
    "stockholders_equity": "362438000000",
    "preferred_stock_value": "20045000000",
    "goodwill": "52731000000",
    "intangible_assets_net_excluding_goodwill": "2600000000",
}


def _snapshot(
    branch: str,
    gross_profit: str | None,
    ppnr: str | None,
    assets: str | None,
    headcount: str | None,
    tce_leaves: dict[str, str | None] | None = None,
):
    return ToptCoreSnapshotInput.model_validate(
        {
            "snapshot_id": f"topt-core-snapshot:{'a' * 64}",
            "run_id": f"capture-run:{'b' * 64}",
            "release_manifest_id": f"release-manifest:{'c' * 64}",
            "universe_id": "universe:topt-candidate-v1",
            "universe_version": "2026-03-31-v1",
            "universe_sha256": "d" * 64,
            "cutoff": CUTOFF,
            "issuer_id": "issuer:example",
            "instrument_id": "instrument:example",
            "listing_id": "listing:example",
            "operating_branch": OperatingBranch(branch),
            "observation_ids": IDS,
            "cell_inputs": tuple(
                ToptCellQualityInput(
                    input_id=input_id,
                    confidence="0.9",
                    knowable_at=CUTOFF - timedelta(days=1),
                    freshness=MetricFreshness.FRESH,
                )
                for input_id in IDS
            ),
            "gross_profit": _metric("gross_profit", gross_profit),
            "total_assets": _metric("total_assets", assets),
            "headcount": _metric("headcount", headcount),
            "revenue": _metric("revenue", "100000000"),
            "pre_provision_profit": _metric("pre_provision_profit", ppnr),
            **{name: _metric(name, value) for name, value in (tce_leaves or {}).items()},
            "market_value_components": (
                ToptMarketValueComponent(
                    instrument_id="instrument:example",
                    listing_id="listing:example",
                    market_price=_metric("market_price", "40", IDS[1]),
                    shares_outstanding=_metric("shares_outstanding", "10000000"),
                ),
            ),
        }
    )


# (branch, gross_profit, pre_provision_profit, total_assets, headcount, rate) -> result id and
# gppe as main@f5807a7 published them. The cases cover the non-financial classes, a negative
# non-financial value, a non-terminating division and each unavailable path. A bank at the
# uniform total-assets value is not here: since #1518 a bank is evaluated on the banking tree
# (`test_a_bank_is_evaluated_on_measured_tangible_common_equity` and the tests after it).
GOLDEN = {
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
        ("financial", "5", None, "4424900000000", "318512", "0.05"),  # + JPM_TCE_LEAVES, see below
        "topt-gppe-result:b06da6b18be6cc251b20b6f938d0e2923f948600293e2dae2dc5bfe9adcfcff4",
        None,
    ),
    "missing_headcount_and_assets": (
        ("non_financial", "97858000000", None, None, None, "0.05"),
        "topt-gppe-result:8006be9c818eaae6bdf36638cf089cb50a3525e3782f3e88a01ef4f1444ddecd",
        None,
    ),
    "nonpositive_headcount": (
        ("insurance", "31700000000", None, "300000000000", "0", "0.05"),
        "topt-gppe-result:3f04324d23270b91403081045d499226c54661f90fc1580de8e6d2c7b3f80d3c",
        None,
    ),
}


@pytest.mark.parametrize("case", sorted(GOLDEN))
def test_compute_topt_gppe_reproduces_the_pre_forest_result_identity(case: str) -> None:
    (branch, gross_profit, ppnr, assets, headcount, rate), result_id, gppe = GOLDEN[case]
    # A bank carries its four banking leaves, so the one gap of `bank_without_ppnr` is its numerator.
    leaves = JPM_TCE_LEAVES if branch == "financial" else None
    result = compute_topt_gppe(
        _snapshot(branch, gross_profit, ppnr, assets, headcount, leaves),
        invocation_id=f"topt-gppe-invocation:{'f' * 64}",
        gppe_definition=GppeV0Definition(risk_free_rate=rate),
        definition=load_live_strategy_definition(),
    )
    assert (None if result.gppe is None else str(result.gppe)) == gppe
    assert result.result_id == result_id


def test_the_tree_is_the_definition_version_it_claims() -> None:
    assert GPPE_V0_TREE.version == GppeV0Definition.model_fields["factor_version"].default


#: (tree key, version) -> identity. Changing what a registered tree computes, its nodes' units,
#: sign policies, aliases or applicability, changes the identity; a changed identity under an
#: unchanged version is a silent formula edit (#528 acceptance: "a definition change that
#: alters a resolved value must bump definition_version"). Register a new version instead.
#
# #1176 renamed the uniform node `gppe` -> `gppe_uniform_charge_v0` and reworded its definition.
# The tree's arithmetic is unchanged: the GOLDEN result identities above still pass, and a node
# diff against main@e465d3d shows only `key` and `definition` changed on the node itself.
FROZEN_TREES = {
    ("gppe", "production-topt-v0.2.0"): "2fdad23895c026e4e6b14cb808f794e1d8936666097506122ba8dc3c55e0c26d",
    # #1176 follow-up: the banking tree deducts preferred stock, so its pin moved with the edit.
    # #1518: v1 (pin 6b2d2cce...) had no snapshot-field alias on its four captured leaves. v2 names
    # one for each leaf, so the TOPT core snapshot carries the tree. The arithmetic did not change.
    ("gppe_banking_tce", "v2"): "f123bc093e4dcc5e4a2e889276957783bdd0a18f5b92fafd7430cb36d7171f1a",
}


def test_registered_trees_are_frozen_under_their_version() -> None:
    assert {(tree.key, tree.version): FOREST.tree_sha256(tree) for tree in FOREST.trees} == FROZEN_TREES


def test_both_named_gppe_metrics_are_registered_with_their_own_sign_policy() -> None:
    # gppe_uniform_charge_v0 applies to every class; each class declares SIGN_IS_SIGNAL.
    # gppe_banking_tce_v1 applies to FINANCIAL only and declares SIGN_IS_SIGNAL for it.
    from factors.forest import SignPolicy

    uniform = FOREST.node("gppe_uniform_charge_v0")
    banking = FOREST.node("gppe_banking_tce_v1")
    assert uniform.applicability == frozenset(IssuerClass)
    assert set(uniform.sign_policy.values()) == {SignPolicy.SIGN_IS_SIGNAL}
    assert banking.applicability == frozenset({IssuerClass.FINANCIAL})
    assert banking.sign_policy == {IssuerClass.FINANCIAL: SignPolicy.SIGN_IS_SIGNAL}


def test_banking_tree_evaluates_measured_tangible_common_equity_for_a_bank() -> None:
    from factors.forest import GPPE_BANKING_TCE_TREE as BANKING_TCE_TREE

    inputs = {
        "pre_provision_profit": Decimal("86807000000"),
        "stockholders_equity": Decimal("340000000000"),
        "preferred_stock_value": Decimal("10000000000"),
        "goodwill": Decimal("5000000000"),
        "intangible_assets_net_excluding_goodwill": Decimal("1000000000"),
        "employees_total": Decimal("318512"),
        "risk_free_rate": Decimal("0.05"),
    }
    evaluation = evaluate(FOREST, BANKING_TCE_TREE, issuer_class=IssuerClass.FINANCIAL, inputs=inputs)
    tangible_common_equity = (
        Decimal("340000000000") - Decimal("10000000000") - Decimal("5000000000") - Decimal("1000000000")
    )
    with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
        expected = (Decimal("86807000000") - tangible_common_equity * Decimal("0.05")) / Decimal("318512")
    assert evaluation.values["gppe_banking_tce_v1"] == expected
    assert evaluation.undefined == {}


def test_banking_tree_leaves_a_missing_tangible_common_equity_undefined() -> None:
    from factors.forest import GPPE_BANKING_TCE_TREE as BANKING_TCE_TREE

    inputs = {
        "pre_provision_profit": Decimal("86807000000"),
        "stockholders_equity": Decimal("340000000000"),
        "preferred_stock_value": Decimal("10000000000"),
        "employees_total": Decimal("318512"),
        "risk_free_rate": Decimal("0.05"),
    }
    evaluation = evaluate(FOREST, BANKING_TCE_TREE, issuer_class=IssuerClass.FINANCIAL, inputs=inputs)
    assert evaluation.values["gppe_banking_tce_v1"] is None
    assert evaluation.undefined["goodwill"] == "missing_input"


# --- #1518: a bank is evaluated on the banking tree that the definition pins --------------------


def _tce_kernel(rate: str) -> tuple[Decimal, Decimal]:
    """JPM FY2025, written out by hand and not read from the code under test.

    TCE         = 362,438M - 20,045M - (52,731M + 2,600M)  = 287,062M
    real profit = 86,807M - TCE x rate
    value       = real profit / 318,512
    """
    with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
        tce = Decimal("362438000000") - Decimal("20045000000") - (Decimal("52731000000") + Decimal("2600000000"))
        real_profit = Decimal("86807000000") - tce * Decimal(rate)
        return real_profit, real_profit / Decimal("318512")


def _bank_result(rate: str, leaves: dict[str, str | None] | None):
    return compute_topt_gppe(
        _snapshot("financial", None, "86807000000", "4424900000000", "318512", leaves),
        invocation_id=f"topt-gppe-invocation:{'f' * 64}",
        gppe_definition=GppeV0Definition(risk_free_rate=rate),
        definition=load_live_strategy_definition(),
    )


@pytest.mark.parametrize(
    ("rate", "real_profit", "value"),
    [
        # 86,807,000,000 - 287,062,000,000 x 0.05 = 72,453,900,000; / 318,512 = 227,476.2018...
        ("0.05", Decimal("72453900000"), Decimal("227476.20")),
        # 86,807,000,000 - 287,062,000,000 x 0.038 = 75,898,644,000; / 318,512 = 238,291.3171...
        ("0.038", Decimal("75898644000"), Decimal("238291.32")),
    ],
)
def test_a_bank_is_evaluated_on_measured_tangible_common_equity(
    rate: str, real_profit: Decimal, value: Decimal
) -> None:
    result = _bank_result(rate, JPM_TCE_LEAVES)
    assert result.gppe is not None and result.capital_adjusted_gross_profit is not None
    assert result.capital_adjusted_gross_profit == real_profit
    assert result.gppe.quantize(Decimal("0.01"), ROUND_HALF_EVEN) == value
    assert (result.capital_adjusted_gross_profit, result.gppe) == _tce_kernel(rate)
    assert result.operating_efficiency == result.gppe
    # The uniform total-assets value of the same bank is -422,081.43 (rate 0.05). It is not the result.
    assert result.gppe > 0


def test_a_bank_without_its_banking_leaves_is_unavailable_with_each_missing_field_named() -> None:
    result = _bank_result("0.05", None)
    assert result.availability.value == "unavailable"
    assert result.gppe is None
    assert {reason.value for reason in result.reason_codes} == {f"missing_{name}" for name in JPM_TCE_LEAVES}


@pytest.mark.parametrize("leaf", sorted(JPM_TCE_LEAVES))
def test_one_missing_banking_leaf_is_named_and_never_replaced(leaf: str) -> None:
    result = _bank_result("0.05", {**JPM_TCE_LEAVES, leaf: None})
    assert result.gppe is None
    assert [reason.value for reason in result.reason_codes] == [f"missing_{leaf}"]


def test_a_reported_zero_preferred_stock_is_a_value_not_a_gap() -> None:
    # TCE = 362,438M - 0 - 55,331M = 307,107M. Real profit = 86,807M - 307,107M x 0.05 = 71,451,650,000.
    result = _bank_result("0.05", {**JPM_TCE_LEAVES, "preferred_stock_value": "0"})
    assert result.capital_adjusted_gross_profit == Decimal("71451650000")


def test_a_class_that_the_definition_pins_to_the_uniform_tree_ignores_the_banking_leaves() -> None:
    snapshot = _snapshot("non_financial", "97858000000", None, "111601000000", "36000", JPM_TCE_LEAVES)
    result = compute_topt_gppe(
        snapshot,
        invocation_id=f"topt-gppe-invocation:{'f' * 64}",
        gppe_definition=GppeV0Definition(risk_free_rate="0.05"),
        definition=load_live_strategy_definition(),
    )
    assert str(result.gppe) == GOLDEN["nvda_shaped"][2], "the uniform value of the nvda-shaped case"


def test_the_banking_leaves_are_absent_from_the_dump_when_none() -> None:
    non_bank = _snapshot("non_financial", "97858000000", None, "111601000000", "36000")
    bank = _snapshot("financial", None, "86807000000", "4424900000000", "318512", JPM_TCE_LEAVES)
    assert not set(TCE_SNAPSHOT_FIELDS) & set(non_bank.model_dump(mode="json")), "a stored snapshot keeps its hash"
    assert set(TCE_SNAPSHOT_FIELDS) <= set(bank.model_dump(mode="json"))


def test_the_snapshot_fields_of_the_banking_leaves_are_the_aliases_of_the_registered_tree() -> None:
    aliased = {
        node.alias(AliasKind.TOPT_SNAPSHOT_FIELD)
        for node in required_inputs(FOREST, GPPE_BANKING_TCE_TREE, IssuerClass.FINANCIAL)
        if node.kind is NodeKind.INPUT
    }
    assert aliased == set(TCE_SNAPSHOT_FIELDS) | {"pre_provision_profit", "headcount"}
