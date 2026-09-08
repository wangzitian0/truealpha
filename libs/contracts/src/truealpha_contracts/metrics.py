"""Canonical metric registry: the single declaration of what a field IS and
which sources may assert it, in what order (init.md Section 6, "Source fusion").

Staging accepts any metric string — it is evidence, and ingestion must never
be blocked on registry lag. Fusion (staging -> mart materialization) serves
ONLY registered metrics, and resolves multi-source disagreement by
source_priority — never by which row happened to be ingested last. Swapping a
field's source is therefore a registry edit (reviewed, versioned via
FUSION_RULESET_VERSION), not parser archaeology.

mapping_version convention (staging.financial_facts.mapping_version): the
producing parser stamps "<parser-id>:<schema-version>", e.g. "sec-companyfacts:1".
Bump the schema version whenever the parser's output for identical raw bytes
can change — a reparse is then a new vintage attributable to the mapping, not
mistakable for a source restatement.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from truealpha_contracts.models import DataSource

# Bump on ANY semantic registry change (priority reorder, metric add/remove,
# unit redefinition). Mart rows carry this so a number stays explainable after
# the rules move on: mart lineage is (staging_ref, fusion_version).
FUSION_RULESET_VERSION = 1


class UnitFamily(StrEnum):
    CURRENCY = "currency"
    COUNT = "count"
    RATIO = "ratio"
    PER_SHARE = "per_share"
    PER_EMPLOYEE = "per_employee"
    PERCENTAGE = "percentage"
    TIME_YEARS = "time_years"


class MetricSpec(BaseModel):
    """One canonical field. `source_priority` is the fusion order: the
    highest-priority source with a row visible at the as-of cutoff wins;
    within a source, the latest transaction_time (restatement) wins.
    Confidence rides along as data for the factor — it never arbitrates
    between sources, because static per-source confidence assignments must not
    silently decide truth."""

    model_config = {"frozen": True}

    name: str = Field(min_length=1)
    unit_family: UnitFamily
    #: True when this metric describes a fiscal PERIOD, so one subject yields a series
    #: rather than a scalar. Declared here because init.md rule 22 forbids generic
    #: transport branching on record type -- the bridge asks the registry instead of
    #: carrying its own list. The payload key is `<name>_by_period`.
    periodic: bool = False
    source_priority: tuple[DataSource, ...] = Field(min_length=1)
    description: str = Field(min_length=1)
    # Gross profit is defined differently for financial vs non-financial
    # issuers (vision.md) — the parser branches, the metric name does not.
    financial_issuer_split: bool = False

    @model_validator(mode="after")
    def validate_priority_unique(self) -> MetricSpec:
        if len(set(self.source_priority)) != len(self.source_priority):
            raise ValueError(f"metric {self.name}: source_priority repeats a source")
        return self


_SPECS = (
    MetricSpec(
        name="revenue",
        unit_family=UnitFamily.CURRENCY,
        source_priority=(DataSource.SEC, DataSource.MOOMOO, DataSource.TWELVE_DATA, DataSource.YAHOO),
        description="Total revenue for the fiscal period.",
    ),
    MetricSpec(
        name="gross_profit",
        unit_family=UnitFamily.CURRENCY,
        source_priority=(DataSource.SEC, DataSource.MOOMOO),
        description="Gross profit; financial issuers use the industry-branch definition.",
        financial_issuer_split=True,
    ),
    MetricSpec(
        name="cost_of_revenue",
        unit_family=UnitFamily.CURRENCY,
        source_priority=(DataSource.SEC, DataSource.MOOMOO),
        description="Reported cost of revenue; a component input, not a data-engine-derived gross profit.",
    ),
    MetricSpec(
        name="operating_income",
        unit_family=UnitFamily.CURRENCY,
        source_priority=(DataSource.SEC, DataSource.MOOMOO),
        description="Reported operating income for issuer branches where gross profit is not reported.",
    ),
    MetricSpec(
        name="net_income",
        unit_family=UnitFamily.CURRENCY,
        periodic=True,
        source_priority=(DataSource.SEC, DataSource.MOOMOO, DataSource.TWELVE_DATA),
        description="Net income attributable to the company for the fiscal period.",
    ),
    MetricSpec(
        name="eps_diluted",
        unit_family=UnitFamily.PER_SHARE,
        source_priority=(DataSource.SEC, DataSource.MOOMOO),
        description="Diluted earnings per share (PEG numerator input).",
    ),
    MetricSpec(
        name="shares_outstanding",
        unit_family=UnitFamily.COUNT,
        source_priority=(DataSource.SEC, DataSource.MOOMOO),
        description="Point-in-time common shares outstanding for the reported context.",
    ),
    MetricSpec(
        name="employees_total",
        unit_family=UnitFamily.COUNT,
        source_priority=(DataSource.SEC, DataSource.MOOMOO),
        description="Total headcount; SEC rows come from filing-text extraction and carry its confidence.",
    ),
    MetricSpec(
        name="total_assets",
        unit_family=UnitFamily.CURRENCY,
        source_priority=(DataSource.SEC,),
        description="Total assets from the balance sheet; the #59 v0 GPPE capital-charge base.",
    ),
    MetricSpec(
        name="price",
        unit_family=UnitFamily.PER_SHARE,
        source_priority=(DataSource.YAHOO, DataSource.TWELVE_DATA, DataSource.MOOMOO),
        description="Unadjusted close price (module 6 price-to-sales market-value input).",
    ),
)


def _build_registry(specs: tuple[MetricSpec, ...]) -> dict[str, MetricSpec]:
    registry: dict[str, MetricSpec] = {}
    for spec in specs:
        if spec.name in registry:
            raise ValueError(f"duplicate metric registration: {spec.name}")
        registry[spec.name] = spec
    return registry


METRICS: dict[str, MetricSpec] = _build_registry(_SPECS)

# The strategy-lane input-key vocabulary (`staging.strategy_backtest_inputs.input_key`,
# `factors.composite.strategy_evaluator`) reads more naturally than the registry's
# canonical names in that context -- "headcount", "last_close" -- and predates this
# registry. This is the one place the two vocabularies are reconciled; a key not listed
# here IS the registered metric name (init.md rule 22 -- no second enumeration of metric
# identity, just the narrow spelling difference this alias carries).
INPUT_KEY_ALIASES: dict[str, str] = {"headcount": "employees_total", "last_close": "price"}


def is_registered_input_key(input_key: str) -> bool:
    """True when `input_key` names a registered metric, directly or through the
    strategy vocabulary alias above.

    `staging.strategy_backtest_inputs.input_key` used to enforce this with a
    `CHECK (input_key in (...))` (migration 0032); a metric added to `METRICS` alone
    now admits its input key here too, so registering metric N+1 needs no migration
    (init.md rule 22, #770).
    """
    return INPUT_KEY_ALIASES.get(input_key, input_key) in METRICS


def input_key_for_metric(metric: str) -> str:
    """The strategy input-key spelling for a registered metric name -- the inverse of
    `INPUT_KEY_ALIASES`, defaulting to the metric's own name when no alias exists.
    Lets a transport derive its input keys from `METRICS` instead of re-declaring the
    same metric list under a second name (init.md rule 22, #770)."""
    for input_key, name in INPUT_KEY_ALIASES.items():
        if name == metric:
            return input_key
    return metric


def source_priority(metric: str) -> tuple[DataSource, ...]:
    """The declared fusion order for a metric. KeyError on unregistered
    metrics is deliberate: fusion must not silently invent an order."""
    return METRICS[metric].source_priority


def fusion_rank(metric: str, source: DataSource) -> int | None:
    """Position of a source in the metric's fusion order (0 = wins), or None
    when the source is not registered for the metric — such rows stay in
    staging as evidence but are excluded from mart materialization."""
    priority = source_priority(metric)
    try:
        return priority.index(source)
    except ValueError:
        return None
