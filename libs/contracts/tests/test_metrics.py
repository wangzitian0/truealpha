from truealpha_contracts import (
    METRICS,
    DataSource,
    fusion_rank,
    source_priority,
)


def test_every_module_core_metric_is_registered():
    # The seven modules' base inputs (init.md Section 7) must have a declared
    # fusion order before any parser lands them.
    for metric in ("revenue", "gross_profit", "net_income", "eps_diluted", "employees_total"):
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
