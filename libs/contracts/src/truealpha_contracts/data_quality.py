"""Machine-readable data requirements and quality results for strategy research."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator

from truealpha_contracts.models import _require_aware


class ReadinessLevel(StrEnum):
    TOOLCHAIN = "toolchain"
    LOCAL_BACKTEST = "local_backtest"
    STRATEGY_EVALUATION = "strategy_evaluation"


class Strategy(StrEnum):
    BACKTEST_CORE = "backtest_core"
    PEG = "peg"
    GROSS_PROFIT_PER_EMPLOYEE = "gross_profit_per_employee"
    SUPPLY_CHAIN = "supply_chain"
    ANALYST_RATING = "analyst_rating"
    ETF_VIRTUAL_COMPANY = "etf_virtual_company"
    PURE_BLOOD = "pure_blood"
    THREE_TIER = "three_tier"


class DataDomain(StrEnum):
    ENTITY_IDENTITY = "entity_identity"
    FINANCIAL_FACTS = "financial_facts"
    FILINGS = "filings"
    MARKET_PRICES = "market_prices"
    CORPORATE_ACTIONS = "corporate_actions"
    UNIVERSE = "universe"
    KNOWLEDGE_GRAPH = "knowledge_graph"
    ANALYST_RATINGS = "analyst_ratings"
    FUND_HOLDINGS = "fund_holdings"
    SEGMENTS = "segments"
    FACTOR_OUTPUTS = "factor_outputs"


class QualityStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"


class StrategyDataRequirement(BaseModel):
    """A stable requirement shared by ingestion, quality gates, and backtests."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_.]+$")
    domain: DataDomain
    description: str = Field(min_length=1)
    acceptance: str = Field(min_length=1)
    strategies: frozenset[Strategy] = Field(min_length=1)
    required_for: frozenset[ReadinessLevel] = Field(min_length=1)


class QualityCheckResult(BaseModel):
    requirement_id: str
    level: ReadinessLevel
    status: QualityStatus
    observed: str = Field(min_length=1)
    expected: str = Field(min_length=1)


class ReadinessAssessment(BaseModel):
    level: ReadinessLevel
    ready: bool
    blockers: tuple[str, ...] = ()


class DataQualityReport(BaseModel):
    generated_at: datetime
    sample_root: str
    checks: tuple[QualityCheckResult, ...]
    assessments: tuple[ReadinessAssessment, ...]

    @field_validator("generated_at")
    @classmethod
    def validate_generated_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "generated_at")

    @model_validator(mode="after")
    def validate_unique_results(self) -> DataQualityReport:
        check_keys = [(check.requirement_id, check.level) for check in self.checks]
        if len(check_keys) != len(set(check_keys)):
            raise ValueError("checks must contain one result per requirement and readiness level")
        levels = [assessment.level for assessment in self.assessments]
        if len(levels) != len(set(levels)):
            raise ValueError("assessments must contain one result per readiness level")
        return self

    def assessment(self, level: ReadinessLevel) -> ReadinessAssessment:
        return next(item for item in self.assessments if item.level == level)


ALL_STRATEGIES = frozenset(Strategy)
BACKTEST_STRATEGIES = frozenset(strategy for strategy in Strategy if strategy is not Strategy.BACKTEST_CORE)
ALL_LEVELS = frozenset(ReadinessLevel)
BACKTEST_LEVELS = frozenset({ReadinessLevel.LOCAL_BACKTEST, ReadinessLevel.STRATEGY_EVALUATION})


STRATEGY_DATA_REQUIREMENTS: tuple[StrategyDataRequirement, ...] = (
    StrategyDataRequirement(
        id="identity.point_in_time",
        domain=DataDomain.ENTITY_IDENTITY,
        description="Stable entity IDs with time-bounded ticker, CIK, CUSIP, ISIN, and fund mappings.",
        acceptance="Every observed source identifier resolves to one entity as of the query time.",
        strategies=ALL_STRATEGIES,
        required_for=ALL_LEVELS,
    ),
    StrategyDataRequirement(
        id="financial.lineage",
        domain=DataDomain.FINANCIAL_FACTS,
        description="Financial values retain period, filing/accession, publication time, confidence, and raw lineage.",
        acceptance="SEC facts expose period, form, filed date, accession, unit, and value without float coercion.",
        strategies=frozenset(
            {
                Strategy.PEG,
                Strategy.GROSS_PROFIT_PER_EMPLOYEE,
                Strategy.ETF_VIRTUAL_COMPANY,
                Strategy.PURE_BLOOD,
                Strategy.THREE_TIER,
            }
        ),
        required_for=ALL_LEVELS,
    ),
    StrategyDataRequirement(
        id="filings.edge_cases",
        domain=DataDomain.FILINGS,
        description="Annual filing text covers domestic and foreign issuers plus ambiguous disclosures.",
        acceptance="Samples include both 10-K and 20-F documents and at least one ambiguity case.",
        strategies=frozenset({Strategy.GROSS_PROFIT_PER_EMPLOYEE, Strategy.PURE_BLOOD}),
        required_for=frozenset({ReadinessLevel.TOOLCHAIN}),
    ),
    StrategyDataRequirement(
        id="prices.adjusted_ohlcv",
        domain=DataDomain.MARKET_PRICES,
        description="Daily OHLCV and adjusted close use exchange trading dates and Decimal-compatible values.",
        acceptance="All sampled companies have ordered, valid OHLCV rows with adjusted close.",
        strategies=ALL_STRATEGIES,
        required_for=ALL_LEVELS,
    ),
    StrategyDataRequirement(
        id="prices.history",
        domain=DataDomain.MARKET_PRICES,
        description="Price history is long enough to include multiple market regimes.",
        acceptance="Local backtests have at least 3 years; strategy evaluation has at least 5 years.",
        strategies=ALL_STRATEGIES,
        required_for=BACKTEST_LEVELS,
    ),
    StrategyDataRequirement(
        id="corporate_actions.total_return",
        domain=DataDomain.CORPORATE_ACTIONS,
        description="Splits and cash dividends can be replayed without forward knowledge.",
        acceptance="Golden fixtures cover at least one split and one dividend with ex/pay dates.",
        strategies=ALL_STRATEGIES,
        required_for=BACKTEST_LEVELS,
    ),
    StrategyDataRequirement(
        id="universe.membership_history",
        domain=DataDomain.UNIVERSE,
        description="Historical membership, symbol changes, and delistings prevent survivorship bias.",
        acceptance="Membership intervals exist and fixtures include one symbol change or delisting.",
        strategies=ALL_STRATEGIES,
        required_for=BACKTEST_LEVELS,
    ),
    StrategyDataRequirement(
        id="financial.restatement_vintages",
        domain=DataDomain.FINANCIAL_FACTS,
        description="Original and restated financial vintages remain independently replayable.",
        acceptance="A golden pair proves pre-restatement and post-restatement as-of queries differ.",
        strategies=frozenset(
            {
                Strategy.PEG,
                Strategy.GROSS_PROFIT_PER_EMPLOYEE,
                Strategy.ETF_VIRTUAL_COMPANY,
                Strategy.PURE_BLOOD,
                Strategy.THREE_TIER,
            }
        ),
        required_for=BACKTEST_LEVELS,
    ),
    StrategyDataRequirement(
        id="graph.supply_chain_evidence",
        domain=DataDomain.KNOWLEDGE_GRAPH,
        description="Supplier/customer edges retain validity intervals, confidence, and filing evidence.",
        acceptance="Filing fixtures contain named relationship candidates for extraction and confidence tests.",
        strategies=frozenset({Strategy.SUPPLY_CHAIN, Strategy.THREE_TIER}),
        required_for=frozenset({ReadinessLevel.TOOLCHAIN}),
    ),
    StrategyDataRequirement(
        id="graph.supply_chain_history",
        domain=DataDomain.KNOWLEDGE_GRAPH,
        description="Supply-chain edges are replayable by the date their supporting evidence became public.",
        acceptance="Golden edges include valid intervals, knowable_at, confidence, and raw evidence references.",
        strategies=frozenset({Strategy.SUPPLY_CHAIN, Strategy.THREE_TIER}),
        required_for=BACKTEST_LEVELS,
    ),
    StrategyDataRequirement(
        id="analyst.event_history",
        domain=DataDomain.ANALYST_RATINGS,
        description="Analyst identity, rating, target, recommendation time, vendor update, and source URL are retained.",
        acceptance="Samples contain multiple analysts and historical recommendation events.",
        strategies=frozenset({Strategy.ANALYST_RATING, Strategy.THREE_TIER}),
        required_for=frozenset({ReadinessLevel.TOOLCHAIN}),
    ),
    StrategyDataRequirement(
        id="analyst.knowability",
        domain=DataDomain.ANALYST_RATINGS,
        description="Public availability is corroborated separately from recommendation and vendor backfill times.",
        acceptance="At least one rating fixture has externally corroborated knowable_at evidence.",
        strategies=frozenset({Strategy.ANALYST_RATING, Strategy.THREE_TIER}),
        required_for=BACKTEST_LEVELS,
    ),
    StrategyDataRequirement(
        id="holdings.point_in_time",
        domain=DataDomain.FUND_HOLDINGS,
        description="Fund holdings retain report period, filing availability, weight, value, and identifier fallbacks.",
        acceptance="N-PORT samples parse weights and include a non-CUSIP identifier fallback case.",
        strategies=frozenset({Strategy.ETF_VIRTUAL_COMPANY, Strategy.THREE_TIER}),
        required_for=ALL_LEVELS,
    ),
    StrategyDataRequirement(
        id="segments.revenue_taxonomy",
        domain=DataDomain.SEGMENTS,
        description="Product and geography revenue segments retain period, taxonomy, confidence, and raw text evidence.",
        acceptance="Samples expose structured revenue breakdowns and filing text for fallback extraction.",
        strategies=frozenset({Strategy.PURE_BLOOD, Strategy.ETF_VIRTUAL_COMPANY, Strategy.THREE_TIER}),
        required_for=ALL_LEVELS,
    ),
    StrategyDataRequirement(
        id="universe.strategy_diversity",
        domain=DataDomain.UNIVERSE,
        description="The validation universe includes software, financial, traditional, and loss-making companies.",
        acceptance="At least 7 companies cover every required strategy trait.",
        strategies=BACKTEST_STRATEGIES,
        required_for=BACKTEST_LEVELS,
    ),
    StrategyDataRequirement(
        id="prices.source_reconciliation",
        domain=DataDomain.MARKET_PRICES,
        description="A primary price source is reconciled against an independent fallback.",
        acceptance="Overlapping bars and corporate actions meet documented discrepancy tolerances.",
        strategies=ALL_STRATEGIES,
        required_for=frozenset({ReadinessLevel.STRATEGY_EVALUATION}),
    ),
    StrategyDataRequirement(
        id="factors.point_in_time_outputs",
        domain=DataDomain.FACTOR_OUTPUTS,
        description="Composite strategies consume versioned base-factor outputs at the same as-of boundary.",
        acceptance="A golden replay proves composite inputs have no future timestamps and confidence uses the minimum input.",
        strategies=frozenset({Strategy.THREE_TIER}),
        required_for=BACKTEST_LEVELS,
    ),
)
