"""Canonical metric registry: the single declaration of what a field IS
(unit family and period kind).

Staging accepts any metric string — it is evidence, and ingestion must never
be blocked on registry lag. Fusion (staging -> mart materialization) serves
ONLY registered metrics. The source order of a fact family is not here: the
`ReconciliationPolicy` of the family holds it (`reconciliation_policies`).

mapping_version convention (staging.financial_facts.mapping_version): the
producing parser stamps "<parser-id>:<schema-version>", e.g. "sec-companyfacts:1".
Bump the schema version whenever the parser's output for identical raw bytes
can change — a reparse is then a new vintage attributable to the mapping, not
mistakable for a source restatement.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, Field


class UnitFamily(StrEnum):
    CURRENCY = "currency"
    COUNT = "count"
    RATIO = "ratio"
    PER_SHARE = "per_share"
    PER_EMPLOYEE = "per_employee"
    PERCENTAGE = "percentage"
    TIME_YEARS = "time_years"


class MetricSpec(BaseModel):
    """One canonical field. Within a source, the latest transaction_time
    (restatement) wins. Confidence rides along as data for the factor — it never
    arbitrates between sources, because static per-source confidence assignments
    must not silently decide truth."""

    model_config = {"frozen": True}

    name: str = Field(min_length=1)
    unit_family: UnitFamily
    #: True when this metric describes a fiscal PERIOD, so one subject yields a series
    #: rather than a scalar. Declared here because init.md rule 22 forbids generic
    #: transport branching on record type -- the bridge asks the registry instead of
    #: carrying its own list. The payload key is `<name>_by_period`.
    periodic: bool = False
    description: str = Field(min_length=1)


_SPECS = (
    MetricSpec(
        name="revenue",
        unit_family=UnitFamily.CURRENCY,
        description="Total revenue for the fiscal period.",
    ),
    MetricSpec(
        name="segment_revenue",
        unit_family=UnitFamily.CURRENCY,
        # companyfacts publishes no segment axis — measured 2026-09-09 on the packaged ADM
        # sample: every fact entry's keys are exactly accn/end/filed/form/fp/frame/fy/start/val,
        # and SegmentReportingInformationRevenueFromExternalCustomers appears as ONE
        # undimensioned row per period whose value equals consolidated Revenues. So this
        # metric's rows come from the filing's segment note through the shared extraction
        # primitive (#772). Its source order is `SEGMENT_REVENUE_RECONCILIATION_POLICY`.
        description=(
            "Revenue attributed to one reportable segment for the fiscal period. One issuer "
            "yields MANY rows — the segment name is part of the row's identity, not of the "
            "metric name — and the rows are admissible only as a set that accounts for the "
            "issuer's consolidated revenue — the set-valued rule in "
            "factors.shared.extraction owns that identifier and this description does not "
            "restate it (#772, #782's ownership guard)."
        ),
    ),
    MetricSpec(
        name="gross_profit",
        unit_family=UnitFamily.CURRENCY,
        description="Gross profit for non-financial issuers.",
    ),
    MetricSpec(
        name="pre_provision_profit",
        unit_family=UnitFamily.CURRENCY,
        description="Bank pre-provision net revenue: net revenue minus noninterest expense (#59, #1519).",
    ),
    MetricSpec(
        name="insurance_pre_claims_profit",
        unit_family=UnitFamily.CURRENCY,
        description="Insurance operating numerator: revenue minus policyholder benefits and claims (#496, #1519).",
    ),
    MetricSpec(
        name="shares_weighted_average_basic",
        unit_family=UnitFamily.COUNT,
        description="Weighted-average basic common shares for the fiscal period (#496, #1519).",
    ),
    MetricSpec(
        name="cost_of_revenue",
        unit_family=UnitFamily.CURRENCY,
        description="Reported cost of revenue; a component input, not a data-engine-derived gross profit.",
    ),
    MetricSpec(
        name="operating_income",
        unit_family=UnitFamily.CURRENCY,
        description="Reported operating income for issuer branches where gross profit is not reported.",
    ),
    MetricSpec(
        name="net_income",
        unit_family=UnitFamily.CURRENCY,
        periodic=True,
        description="Net income attributable to the company for the fiscal period.",
    ),
    MetricSpec(
        name="eps_diluted",
        unit_family=UnitFamily.PER_SHARE,
        description="Diluted earnings per share (PEG numerator input).",
    ),
    MetricSpec(
        name="shares_outstanding",
        unit_family=UnitFamily.COUNT,
        description="Point-in-time common shares outstanding for the reported context.",
    ),
    MetricSpec(
        name="stockholders_equity",
        unit_family=UnitFamily.CURRENCY,
        description=(
            "Total stockholders' equity at the fiscal year end; the first term of the banking "
            "tangible-common-equity base (gppe_banking_tce_v1, #1176). Includes preferred equity."
        ),
    ),
    MetricSpec(
        name="preferred_stock_value",
        unit_family=UnitFamily.CURRENCY,
        description=(
            "Preferred stock carrying value at the fiscal year end (us-gaap PreferredStockValue); "
            "deducted from stockholders' equity for banking tangible common equity (#1176)."
        ),
    ),
    MetricSpec(
        name="goodwill",
        unit_family=UnitFamily.CURRENCY,
        description="Goodwill carrying amount at the fiscal year end; deducted for tangible common equity (#1176).",
    ),
    MetricSpec(
        name="intangible_assets_net_excluding_goodwill",
        unit_family=UnitFamily.CURRENCY,
        description=(
            "Intangible assets net of amortization, excluding goodwill, at the fiscal year end; "
            "deducted for tangible common equity (#1176)."
        ),
    ),
    MetricSpec(
        name="employees_total",
        unit_family=UnitFamily.COUNT,
        description="Total headcount; SEC rows come from filing-text extraction and carry its confidence.",
    ),
    MetricSpec(
        name="total_assets",
        unit_family=UnitFamily.CURRENCY,
        description="Total assets from the balance sheet; the #59 v0 GPPE capital-charge base.",
    ),
    MetricSpec(
        name="price",
        unit_family=UnitFamily.PER_SHARE,
        description="Unadjusted close price (module 6 price-to-sales market-value input).",
    ),
    MetricSpec(
        name="consensus_profit_growth",
        unit_family=UnitFamily.PERCENTAGE,
        description="Annual profit growth rate in percentage points for consensus-convention PEG (#1354).",
    ),
    MetricSpec(
        name="gross_profit_per_employee",
        unit_family=UnitFamily.PER_EMPLOYEE,
        description="Gross profit per employee for non-financial issuers (#1523).",
    ),
    MetricSpec(
        name="financial_assets",
        unit_family=UnitFamily.CURRENCY,
        description="Cash and marketable investment balances on the balance sheet at the fiscal year end (#528).",
    ),
    MetricSpec(
        name="financial_returns",
        unit_family=UnitFamily.CURRENCY,
        description="Investment income, dividends, and investment gains for the fiscal year (#528).",
    ),
    MetricSpec(
        name="bank_net_revenue",
        unit_family=UnitFamily.CURRENCY,
        description="Bank net revenue (revenues net of interest expense, or net interest income plus noninterest income).",
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
INPUT_KEY_ALIASES: dict[str, str] = {
    "headcount": "employees_total",
    "last_close": "price",
    "gppe": "gross_profit_per_employee",
}


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


class IssuerClass(StrEnum):
    """The classes a node or a decomposition may treat differently. The values are the
    `operating_branch` values the TOPT snapshot and `mart.topt_*_results` already carry."""

    NON_FINANCIAL = "non_financial"
    FINANCIAL = "financial"
    INSURANCE = "insurance"


ALL_ISSUER_CLASSES: frozenset[IssuerClass] = frozenset(IssuerClass)


class PeriodSemantics(StrEnum):
    #: A flow over the fiscal year (income statement).
    FISCAL_YEAR_FLOW = "fiscal-year-flow"
    #: A stock at the fiscal year end (balance sheet, headcount).
    FISCAL_YEAR_END_STOCK = "fiscal-year-end-stock"
    #: As of the snapshot cutoff (price, market value).
    CUTOFF_INSTANT = "cutoff-instant"
    #: A definition parameter; describes no period.
    DEFINITIONAL = "definitional"
    #: A concept carries no value, so no period.
    NONE = "none"


class SignPolicy(StrEnum):
    """What a negative value of a node means (init.md rule 17: recorded per component with the
    versioned definition; the output invariants assert exactly this)."""

    #: Arithmetic may make it negative and that says nothing by itself.
    MAY_BE_NEGATIVE = "may-be-negative"
    #: A negative value is a defect: the invariant suite and the tick's gate refuse it.
    MUST_BE_NON_NEGATIVE = "must-be-non-negative"
    #: A negative value is a valid, ranked low signal. It passes, and it is printed by name on
    #: every check so it is never hidden.
    SIGN_IS_SIGNAL = "sign-is-signal"


#: Physical quantities that cannot be negative. Everything economic — profits, income, value added,
#: efficiencies — may be negative, and a negative number is data, not a defect (#528 D1).
PHYSICALLY_NON_NEGATIVE: Mapping[str, str] = MappingProxyType(
    {
        "total_assets": "a balance of assets; a negative total is a sign or unit defect in the filing parse",
        "employees_total": "a count of people",
        "risk_free_rate": "a definition parameter we set, not a captured fact",
        "capital_charge": "total_assets × risk_free_rate, both non-negative by the entries above",
        "goodwill": "a carrying amount of goodwill; a negative balance is a sign or unit defect in the filing parse",
        "intangible_assets_net_excluding_goodwill": (
            "a carrying amount of intangible assets; a negative balance is a sign or unit defect in the filing parse"
        ),
        "preferred_stock_value": (
            "a carrying amount of preferred stock; a negative balance is a sign or unit defect in the filing parse"
        ),
        "tangible_deductions": "the sum of three non-negative balances (preferred stock, goodwill and intangibles)",
        "charged_tangible_common_equity": "tangible common equity when it is not negative; undefined otherwise (#1518)",
        "financial_assets": "a carrying balance of cash and marketable investments; a negative balance is a sign or unit defect",
    }
)
