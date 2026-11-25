import pytest
from truealpha_contracts.metrics import METRICS, MetricSpec, UnitFamily, input_key_for_metric, is_registered_input_key

pytestmark = pytest.mark.contract


def test_metrics_registry_and_aliases():
    for m in (
        "revenue",
        "gross_profit",
        "cost_of_revenue",
        "operating_income",
        "net_income",
        "eps_diluted",
        "shares_outstanding",
        "employees_total",
    ):
        assert m in METRICS
    for m, u in (
        ("pre_provision_profit", UnitFamily.CURRENCY),
        ("insurance_pre_claims_profit", UnitFamily.CURRENCY),
        ("shares_weighted_average_basic", UnitFamily.COUNT),
    ):
        assert m in METRICS and METRICS[m].unit_family is u
    assert not hasattr(METRICS["gross_profit"], "financial_issuer_split")
    assert all(
        is_registered_input_key(k) for k in ("headcount", "last_close", "revenue", "gppe")
    ) and not is_registered_input_key("not_a_real_metric")
    assert (
        input_key_for_metric("employees_total"),
        input_key_for_metric("price"),
        input_key_for_metric("revenue"),
        input_key_for_metric("gross_profit_per_employee"),
    ) == ("headcount", "last_close", "revenue", "gppe")
    assert (
        "gross_profit_per_employee" in METRICS
        and METRICS["gross_profit_per_employee"].unit_family == UnitFamily.PER_EMPLOYEE
    )
    syn = "synthetic_test_metric_770"
    assert syn not in METRICS and not is_registered_input_key(syn)
    METRICS[syn] = MetricSpec(name=syn, unit_family=UnitFamily.RATIO, description="test metric")
    try:
        assert is_registered_input_key(syn) and input_key_for_metric(syn) == syn
    finally:
        del METRICS[syn]
