"""Governed concept-mapping ruleset resolution, default ruleset, and publication (#496)."""

from __future__ import annotations

import json
from typing import Any

from psycopg import Connection
from truealpha_contracts.concept_mapping import ConceptMappingRuleset

_KIND = "concept-mapping"

DEFAULT_RULESET = ConceptMappingRuleset.model_validate(
    {
        "version": "production-topt-concepts:v6",
        "mappings": (
            {
                "field": "revenue",
                "unit": "USD",
                "kind": "synonym",
                "concepts": (
                    {"taxonomy": "us-gaap", "concept": "Revenues"},
                    {"taxonomy": "us-gaap", "concept": "RevenueFromContractWithCustomerExcludingAssessedTax"},
                    {"taxonomy": "us-gaap", "concept": "RevenueFromContractWithCustomerIncludingAssessedTax"},
                    {"taxonomy": "us-gaap", "concept": "RegulatedAndUnregulatedOperatingRevenue"},
                ),
            },
            {
                "field": "cost_of_revenue",
                "unit": "USD",
                "kind": "synonym",
                "concepts": (
                    {"taxonomy": "us-gaap", "concept": "CostOfRevenue"},
                    {"taxonomy": "us-gaap", "concept": "CostOfGoodsAndServicesSold"},
                    {"taxonomy": "us-gaap", "concept": "CostOfGoodsSold"},
                    {"taxonomy": "us-gaap", "concept": "CostOfServices"},
                    {"taxonomy": "us-gaap", "concept": "CostOfSales"},
                    {"taxonomy": "us-gaap", "concept": "DirectOperatingCosts"},
                    {"taxonomy": "us-gaap", "concept": "ProgrammingAndProductionCosts"},
                    {
                        "taxonomy": "us-gaap",
                        "concept": "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
                    },
                    {"taxonomy": "us-gaap", "concept": "ProductionAndDistributionCosts"},
                ),
            },
            {
                "field": "gross_profit",
                "unit": "USD",
                "kind": "synonym",
                "concepts": ({"taxonomy": "us-gaap", "concept": "GrossProfit"},),
            },
            {
                "field": "net_income",
                "unit": "USD",
                "kind": "synonym",
                "concepts": (
                    {"taxonomy": "us-gaap", "concept": "NetIncomeLoss"},
                    {"taxonomy": "us-gaap", "concept": "ProfitLoss"},
                ),
            },
            {
                "field": "total_assets",
                "unit": "USD",
                "kind": "synonym",
                "concepts": ({"taxonomy": "us-gaap", "concept": "Assets"},),
            },
            {
                "field": "stockholders_equity",
                "unit": "USD",
                "kind": "synonym",
                "concepts": ({"taxonomy": "us-gaap", "concept": "StockholdersEquity"},),
            },
            {
                "field": "goodwill",
                "unit": "USD",
                "kind": "synonym",
                "concepts": ({"taxonomy": "us-gaap", "concept": "Goodwill"},),
            },
            {
                "field": "preferred_stock_value",
                "unit": "USD",
                "kind": "synonym",
                "concepts": (
                    {"taxonomy": "us-gaap", "concept": "PreferredStockValue"},
                    {
                        "taxonomy": "us-gaap",
                        "concept": "PreferredStockIncludingAdditionalPaidInCapitalNetOfDiscount",
                    },
                ),
            },
            {
                "field": "intangible_assets_net_excluding_goodwill",
                "unit": "USD",
                "kind": "synonym",
                "concepts": ({"taxonomy": "us-gaap", "concept": "IntangibleAssetsNetExcludingGoodwill"},),
            },
            {
                "field": "finite_lived_intangibles_net",
                "unit": "USD",
                "kind": "synonym",
                "concepts": ({"taxonomy": "us-gaap", "concept": "FiniteLivedIntangibleAssetsNet"},),
            },
            {
                "field": "indefinite_lived_intangibles",
                "unit": "USD",
                "kind": "synonym",
                "concepts": ({"taxonomy": "us-gaap", "concept": "IndefiniteLivedIntangibleAssetsExcludingGoodwill"},),
            },
            {
                "field": "shares_outstanding",
                "unit": "shares",
                "kind": "synonym",
                "concepts": (
                    {"taxonomy": "dei", "concept": "EntityCommonStockSharesOutstanding"},
                    {"taxonomy": "us-gaap", "concept": "CommonStockSharesOutstanding"},
                ),
            },
            {
                "field": "shares_outstanding_last_resort",
                "unit": "shares",
                "kind": "fallback",
                "concepts": ({"taxonomy": "us-gaap", "concept": "WeightedAverageNumberOfSharesOutstandingBasic"},),
            },
            {
                "field": "bank_revenue",
                "unit": "USD",
                "kind": "fallback",
                "concepts": (
                    {"taxonomy": "us-gaap", "concept": "RevenuesNetOfInterestExpense"},
                    {"taxonomy": "us-gaap", "concept": "Revenues"},
                ),
            },
            {
                "field": "bank_net_revenue",
                "unit": "USD",
                "kind": "synonym",
                "concepts": ({"taxonomy": "us-gaap", "concept": "RevenuesNetOfInterestExpense"},),
            },
            {
                "field": "interest_income_expense_net",
                "unit": "USD",
                "kind": "synonym",
                "concepts": ({"taxonomy": "us-gaap", "concept": "InterestIncomeExpenseNet"},),
            },
            {
                "field": "noninterest_income",
                "unit": "USD",
                "kind": "synonym",
                "concepts": ({"taxonomy": "us-gaap", "concept": "NoninterestIncome"},),
            },
            {
                "field": "insurance_claims",
                "unit": "USD",
                "kind": "fallback",
                "concepts": (
                    {"taxonomy": "us-gaap", "concept": "PolicyholderBenefitsAndClaimsIncurredNet"},
                    {"taxonomy": "us-gaap", "concept": "BenefitsLossesAndExpenses"},
                    {"taxonomy": "us-gaap", "concept": "IncurredClaimsPropertyCasualtyAndLiability"},
                ),
            },
            {
                "field": "noninterest_expense",
                "unit": "USD",
                "kind": "synonym",
                "concepts": ({"taxonomy": "us-gaap", "concept": "NoninterestExpense"},),
            },
            {
                "field": "cash_and_equivalents",
                "unit": "USD",
                "kind": "synonym",
                "concepts": (
                    {"taxonomy": "us-gaap", "concept": "CashAndCashEquivalentsAtCarryingValue"},
                    {"taxonomy": "us-gaap", "concept": "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"},
                ),
            },
            {
                "field": "cash_and_short_term_investments",
                "unit": "USD",
                "kind": "fallback",
                "concepts": ({"taxonomy": "us-gaap", "concept": "CashCashEquivalentsAndShortTermInvestments"},),
            },
            {
                "field": "short_term_investments",
                "unit": "USD",
                "kind": "synonym",
                "concepts": ({"taxonomy": "us-gaap", "concept": "ShortTermInvestments"},),
            },
            {
                "field": "long_term_investments",
                "unit": "USD",
                "kind": "synonym",
                "concepts": ({"taxonomy": "us-gaap", "concept": "LongTermInvestments"},),
            },
            {
                "field": "marketable_securities_current",
                "unit": "USD",
                "kind": "synonym",
                "concepts": ({"taxonomy": "us-gaap", "concept": "MarketableSecuritiesCurrent"},),
            },
            {
                "field": "marketable_securities_noncurrent",
                "unit": "USD",
                "kind": "synonym",
                "concepts": ({"taxonomy": "us-gaap", "concept": "MarketableSecuritiesNoncurrent"},),
            },
            {
                "field": "afs_debt_securities",
                "unit": "USD",
                "kind": "synonym",
                "concepts": ({"taxonomy": "us-gaap", "concept": "AvailableForSaleSecuritiesDebtSecurities"},),
            },
            {
                "field": "htm_securities",
                "unit": "USD",
                "kind": "synonym",
                "concepts": ({"taxonomy": "us-gaap", "concept": "HeldToMaturitySecurities"},),
            },
            {
                "field": "equity_securities_fvni",
                "unit": "USD",
                "kind": "synonym",
                "concepts": ({"taxonomy": "us-gaap", "concept": "EquitySecuritiesFvNi"},),
            },
            {
                "field": "investments_total",
                "unit": "USD",
                "kind": "synonym",
                "concepts": ({"taxonomy": "us-gaap", "concept": "Investments"},),
            },
            {
                "field": "fed_funds_sold",
                "unit": "USD",
                "kind": "synonym",
                "concepts": (
                    {"taxonomy": "us-gaap", "concept": "FederalFundsSoldAndSecuritiesPurchasedUnderAgreementsToResell"},
                ),
            },
            {
                "field": "investment_income",
                "unit": "USD",
                "kind": "fallback",
                "concepts": (
                    {"taxonomy": "us-gaap", "concept": "InvestmentIncomeInterestAndDividend"},
                    {"taxonomy": "us-gaap", "concept": "InvestmentIncomeNet"},
                    {"taxonomy": "us-gaap", "concept": "InvestmentIncomeInterest"},
                ),
            },
            {
                "field": "investment_income_dividend",
                "unit": "USD",
                "kind": "synonym",
                "concepts": ({"taxonomy": "us-gaap", "concept": "InvestmentIncomeDividend"},),
            },
            {
                "field": "investment_gains",
                "unit": "USD",
                "kind": "synonym",
                "concepts": ({"taxonomy": "us-gaap", "concept": "GainLossOnInvestments"},),
            },
            {
                "field": "nonoperating_income",
                "unit": "USD",
                "kind": "fallback",
                "concepts": (
                    {"taxonomy": "us-gaap", "concept": "NonoperatingIncomeExpense"},
                    {"taxonomy": "us-gaap", "concept": "OtherNonoperatingIncomeExpense"},
                ),
            },
        ),
    }
)


def publish_governed_object(
    connection: Connection[Any],
    *,
    kind: str,
    contract_id: str,
    content_sha256: str,
    payload: Any,
    note: str,
) -> tuple[str, int]:
    """Store a content-addressed contract object and advance `staging.accepted_rulesets`."""
    connection.execute(
        "insert into staging.contract_objects (contract_id, contract_kind, content_sha256, payload) "
        "values (%s, %s, %s, %s) on conflict (contract_id) do nothing",
        (contract_id, kind, content_sha256, payload),
    )
    row = connection.execute(
        "select coalesce(max(sequence), 0) + 1 from staging.accepted_rulesets where kind = %s",
        (kind,),
    ).fetchone()
    assert row is not None
    sequence = int(row[0])
    connection.execute(
        "insert into staging.accepted_rulesets (kind, contract_id, sequence, note) values (%s, %s, %s, %s)",
        (kind, contract_id, sequence, note),
    )
    return contract_id, sequence


def resolve_ruleset(connection: Connection[Any] | None) -> ConceptMappingRuleset:
    """Resolve the governed `staging.accepted_ruleset_head` ruleset, or `DEFAULT_RULESET` when absent."""
    if connection is None:
        return DEFAULT_RULESET
    row = connection.execute(
        """
        select object.payload
        from staging.accepted_ruleset_head head
        join staging.contract_objects object on object.contract_id = head.contract_id
        where head.kind = %s
        """,
        (_KIND,),
    ).fetchone()
    return DEFAULT_RULESET if row is None else ConceptMappingRuleset.model_validate(row[0])


def publish_ruleset(connection: Connection[Any], ruleset: ConceptMappingRuleset, *, note: str) -> tuple[str, int]:
    """Store `ruleset` in `staging.contract_objects` and advance the governed pointer."""
    return publish_governed_object(
        connection,
        kind=_KIND,
        contract_id=ruleset.ruleset_id,
        content_sha256=ruleset.content_sha256,
        payload=json.dumps(ruleset.model_dump(mode="json")),
        note=note,
    )
