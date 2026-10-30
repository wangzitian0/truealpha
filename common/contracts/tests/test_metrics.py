from truealpha_contracts.metrics import (
    METRICS,
    MetricSpec,
    UnitFamily,
    input_key_for_metric,
    is_registered_input_key,
)


def test_every_module_core_metric_is_registered():
    # The seven modules' base inputs (init.md Section 7) must be registered
    # before any parser lands them.
    for metric in (
        "revenue",
        "gross_profit",
        "cost_of_revenue",
        "operating_income",
        "net_income",
        "eps_diluted",
        "shares_outstanding",
        "employees_total",
    ):
        assert metric in METRICS


def test_separate_operating_and_share_quantities_are_registered_under_their_own_names():
    for metric, expected_unit in (
        ("pre_provision_profit", UnitFamily.CURRENCY),
        ("insurance_pre_claims_profit", UnitFamily.CURRENCY),
        ("shares_weighted_average_basic", UnitFamily.COUNT),
    ):
        assert metric in METRICS
        assert METRICS[metric].unit_family is expected_unit
    assert not hasattr(METRICS["gross_profit"], "financial_issuer_split")


def test_input_key_aliases_round_trip_through_the_registry():
    # `headcount` / `last_close` are the strategy vocabulary's spelling of registered
    # metrics; every other input key IS the registered name (init.md rule 22).
    assert is_registered_input_key("headcount")
    assert is_registered_input_key("last_close")
    assert is_registered_input_key("revenue")
    assert is_registered_input_key("gppe")
    assert not is_registered_input_key("not_a_real_metric")
    assert input_key_for_metric("employees_total") == "headcount"
    assert input_key_for_metric("price") == "last_close"
    assert input_key_for_metric("revenue") == "revenue"
    assert input_key_for_metric("gross_profit_per_employee") == "gppe"


def test_gross_profit_per_employee_is_registered_with_per_employee_unit():
    assert "gross_profit_per_employee" in METRICS
    assert METRICS["gross_profit_per_employee"].unit_family == UnitFamily.PER_EMPLOYEE


def test_registering_a_synthetic_metric_needs_no_second_enumeration():
    """#770 acceptance: adding metric N+1 to METRICS alone is enough to admit its input
    key -- no migration (0032's enumerated CHECK is dropped) and no edit to
    `is_registered_input_key`/`input_key_for_metric`, which read the registry rather than
    carry their own list of names."""
    synthetic_name = "synthetic_test_metric_770"
    assert synthetic_name not in METRICS, "test fixture collided with a real registration"
    assert not is_registered_input_key(synthetic_name)

    METRICS[synthetic_name] = MetricSpec(
        name=synthetic_name,
        unit_family=UnitFamily.RATIO,
        description="A metric registered only for this test.",
    )
    try:
        # No code in this module changed between the assertion above and this one --
        # the registration alone is what flips the answer.
        assert is_registered_input_key(synthetic_name)
        assert input_key_for_metric(synthetic_name) == synthetic_name
    finally:
        del METRICS[synthetic_name]
