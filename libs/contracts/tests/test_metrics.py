from truealpha_contracts.metrics import (
    METRICS,
    MetricSpec,
    UnitFamily,
    fusion_rank,
    input_key_for_metric,
    is_registered_input_key,
    source_priority,
)
from truealpha_contracts.models import DataSource


def test_every_module_core_metric_is_registered():
    # The seven modules' base inputs (init.md Section 7) must have a declared
    # fusion order before any parser lands them.
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


def test_priorities_are_nonempty_and_deduplicated():
    for name, spec in METRICS.items():
        assert spec.source_priority, name
        assert len(set(spec.source_priority)) == len(spec.source_priority), name


def test_fusion_rank_orders_declared_sources_and_excludes_others():
    assert fusion_rank("revenue", DataSource.SEC) == 0
    assert fusion_rank("revenue", DataSource.YAHOO) > fusion_rank("revenue", DataSource.TWELVE_DATA)
    # Unregistered source: evidence stays in staging, never reaches mart.
    assert fusion_rank("gross_profit", DataSource.YAHOO) is None


def test_unregistered_metric_fails_loud():
    # Fusion must not invent an order for a metric nobody declared.
    try:
        source_priority("share_of_wallet")
    except KeyError:
        return
    raise AssertionError("expected KeyError for unregistered metric")


def test_gross_profit_declares_the_financial_issuer_branch():
    assert METRICS["gross_profit"].financial_issuer_split is True


def test_input_key_aliases_round_trip_through_the_registry():
    # `headcount` / `last_close` are the strategy vocabulary's spelling of registered
    # metrics; every other input key IS the registered name (init.md rule 22).
    assert is_registered_input_key("headcount")
    assert is_registered_input_key("last_close")
    assert is_registered_input_key("revenue")
    assert not is_registered_input_key("not_a_real_metric")
    assert input_key_for_metric("employees_total") == "headcount"
    assert input_key_for_metric("price") == "last_close"
    assert input_key_for_metric("revenue") == "revenue"


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
        source_priority=(DataSource.SEC,),
        description="A metric registered only for this test.",
    )
    try:
        # No code in this module changed between the assertion above and this one --
        # the registration alone is what flips the answer.
        assert is_registered_input_key(synthetic_name)
        assert input_key_for_metric(synthetic_name) == synthetic_name
    finally:
        del METRICS[synthetic_name]
