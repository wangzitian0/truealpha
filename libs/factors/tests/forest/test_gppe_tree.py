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
from factors.forest import FOREST, GPPE_V0_TREE, IssuerClass, evaluate, required_inputs
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
            actual = (evaluation.values["capital_adjusted_gross_profit"], evaluation.values["gppe"])
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
    assert missing.values["gppe"] is None
    assert missing.undefined["pre_provision_profit"] == "missing_input"
    assert missing.undefined["gppe"] == "undefined_operand"
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
    assert zero.values["gppe"] is None and zero.undefined == {"gppe": "zero_denominator"}


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


def _snapshot(branch: str, gross_profit: str | None, ppnr: str | None, assets: str | None, headcount: str | None):
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
# gppe as main@f5807a7 published them. JPM's inputs are the strategy corpus's
# (`large_model_value_v0_strategy.v1.json`, jpm-2026-06-30); the rest cover every class, a
# negative non-financial value, a non-terminating division and each unavailable path.
GOLDEN = {
    "jpm_production_rate": (
        ("financial", None, "86807000000", "4424900000000", "318512", "0.05"),
        "topt-gppe-result:45a5ec8438ea001361777cb66b28d21a8b457af75c9ff53c1f6892c7459e5293",
        "-422081.4286431908373938815492037977",
    ),
    "jpm_corpus_rate": (
        ("financial", None, "86807000000", "4424900000000", "318512", "0.038"),
        "topt-gppe-result:fc0a466e1cb021fb03d6992fb42a6b781e3c1b9bb4db41e3e2f8b99e59de4468",
        "-255372.4820414929421811423117496358",
    ),
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
    result = compute_topt_gppe(
        _snapshot(branch, gross_profit, ppnr, assets, headcount),
        invocation_id=f"topt-gppe-invocation:{'f' * 64}",
        gppe_definition=GppeV0Definition(risk_free_rate=rate),
    )
    assert (None if result.gppe is None else str(result.gppe)) == gppe
    assert result.result_id == result_id


def test_the_tree_is_the_definition_version_it_claims() -> None:
    assert GPPE_V0_TREE.version == GppeV0Definition.model_fields["factor_version"].default


#: (tree key, version) -> identity. Changing what a registered tree computes, its nodes' units,
#: sign policies, aliases or applicability, changes the identity; a changed identity under an
#: unchanged version is a silent formula edit (#528 acceptance: "a definition change that
#: alters a resolved value must bump definition_version"). Register a new version instead.
FROZEN_TREES = {
    ("gppe", "production-topt-v0.2.0"): "5f562c8a4272a69baf5c14eaf8959d1dbf7c21c8eb837b1bc172f776f299446f",
}


def test_registered_trees_are_frozen_under_their_version() -> None:
    assert {(tree.key, tree.version): FOREST.tree_sha256(tree) for tree in FOREST.trees} == FROZEN_TREES
