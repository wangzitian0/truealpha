"""Resolve the concept mapping a run must use, and the default it falls back to (#496).

Two sources, in order:

1. The governed pointer, `staging.accepted_ruleset_head`. Publishing a corrected mapping is
   an insert plus a pointer advance — no image rebuild, no deploy. This is the whole point:
   the plane that changes most often should not be the plane that requires a release.
2. `DEFAULT_RULESET`, shipped with the image. Not a silent fallback so much as the declared
   starting state, the same way the frozen universe corpus is versioned configuration
   rather than input data. A fresh database resolves it and behaves identically to the
   image it came from.

Either way the resolved ruleset is content-addressed, and the hash it resolves to is what
`mapping_version` records — so an observation always names the exact rules behind it, and a
pointer advance is visible in the data rather than only in a changelog.
"""

from __future__ import annotations

import json
from typing import Any

from psycopg import Connection
from truealpha_contracts.concept_mapping import ConceptMappingRuleset

_KIND = "concept-mapping"

# The mapping the image ships with — the state #501/#507 arrived at, now expressed as data.
# Synonym vs fallback is declared per field and is the load-bearing distinction; see the
# contract's docstring for why merging the two produces a silently wrong number.
DEFAULT_RULESET = ConceptMappingRuleset.model_validate(
    {
        "version": "production-topt-concepts:v1",
        "mappings": (
            {
                "field": "revenue",
                "unit": "USD",
                "kind": "synonym",
                "concepts": (
                    {"taxonomy": "us-gaap", "concept": "Revenues"},
                    {"taxonomy": "us-gaap", "concept": "RevenueFromContractWithCustomerExcludingAssessedTax"},
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
                ),
            },
            {
                "field": "gross_profit",
                "unit": "USD",
                "kind": "synonym",
                "concepts": ({"taxonomy": "us-gaap", "concept": "GrossProfit"},),
            },
            {
                "field": "total_assets",
                "unit": "USD",
                "kind": "synonym",
                "concepts": ({"taxonomy": "us-gaap", "concept": "Assets"},),
            },
            {
                # True synonyms: both are point-in-time shares OUTSTANDING, one on the cover
                # page and one in the statements. `CommonStockSharesIssued` is deliberately
                # absent — issued includes treasury stock (JNJ: 3.12bn issued against 2.41bn
                # outstanding) and is a different quantity, not a later-dated synonym.
                "field": "shares_outstanding",
                "unit": "shares",
                "kind": "synonym",
                "concepts": (
                    {"taxonomy": "dei", "concept": "EntityCommonStockSharesOutstanding"},
                    {"taxonomy": "us-gaap", "concept": "CommonStockSharesOutstanding"},
                ),
            },
            {
                # Consulted only when the field above yields NOTHING (#496, owner-approved
                # 2026-07-28 for dual-class filers whose per-class point-in-time tags the
                # company-facts API drops entirely). A period average is a different quantity
                # from a point-in-time count, so it is a separate field rather than a trailing
                # entry in the synonym list: inside that list it could win on recency whenever
                # the cover-page figure predates the latest fiscal year end, which is exactly
                # the substitution this ruleset exists to make impossible.
                "field": "shares_outstanding_last_resort",
                "unit": "shares",
                "kind": "fallback",
                "concepts": ({"taxonomy": "us-gaap", "concept": "WeightedAverageNumberOfSharesOutstandingBasic"},),
            },
            {
                # FALLBACK: for a bank, plain `Revenues` is gross of interest expense, so it is
                # a stand-in reached only when no net-of-interest total is published.
                "field": "bank_revenue",
                "unit": "USD",
                "kind": "fallback",
                "concepts": (
                    {"taxonomy": "us-gaap", "concept": "RevenuesNetOfInterestExpense"},
                    {"taxonomy": "us-gaap", "concept": "Revenues"},
                ),
            },
            {
                # Stand-ins: the entries measure different nettings, so recency must not
                # promote one over another (#496).
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
        ),
    }
)


def resolve_ruleset(connection: Connection[Any] | None) -> ConceptMappingRuleset:
    """The in-force mapping: the governed head when one is published, else the default.

    Resolution goes through `staging.accepted_ruleset_head`, never through the newest row
    of `contract_objects` — a bare mutable latest is not a read path, and it would make a
    half-published ruleset take effect the moment its object landed.
    """
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
    if row is None:
        return DEFAULT_RULESET
    return ConceptMappingRuleset.model_validate(row[0])


def publish_ruleset(connection: Connection[Any], ruleset: ConceptMappingRuleset, *, note: str) -> tuple[str, int]:
    """Land a ruleset and advance the pointer to it; returns (contract_id, sequence).

    Both halves in one call because a published object nothing points at is inert, and a
    pointer to an object that is not stored resolves to nothing — the pair is the unit of
    change.
    """
    connection.execute(
        "insert into staging.contract_objects (contract_id, contract_kind, content_sha256, payload) "
        "values (%s, %s, %s, %s) on conflict (contract_id) do nothing",
        (
            ruleset.ruleset_id,
            _KIND,
            ruleset.content_sha256,
            json.dumps(ruleset.model_dump(mode="json")),
        ),
    )
    row = connection.execute(
        "select coalesce(max(sequence), 0) + 1 from staging.accepted_rulesets where kind = %s",
        (_KIND,),
    ).fetchone()
    assert row is not None
    sequence = int(row[0])
    connection.execute(
        "insert into staging.accepted_rulesets (kind, contract_id, sequence, note) values (%s, %s, %s, %s)",
        (_KIND, ruleset.ruleset_id, sequence, note),
    )
    return ruleset.ruleset_id, sequence
