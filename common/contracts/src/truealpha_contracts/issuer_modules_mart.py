"""Module 3 and module 4 rows of the served governed head (#42).

`mart.issuer_supply_chain_exposure` (module 3, #772) and `mart.issuer_analyst_ratings`
(module 4, #771) hold one row per issuer per governed run. A consumer reads only the rows
of a run that `mart.served_head` serves (AGENTS.md: a bare latest is never a read path).
When more than one served run holds the issuer, an available row comes first, then the
newest cutoff.

The research report (`research_report_mart`) and the MCP tools read through this module.
The App reads the same rows through its twin in `apps/app-web/src/server/mart/research-read.ts`
(`entitySupplyChainExposure`, `entityAnalystRatings`). Both twins seed
`conformance/issuer_modules_parity.json` on a real database and must return its expected
rows, with a newer unserved decoy row present. Each value is the exact text of the stored
numeric, never a float.
"""

from __future__ import annotations

from typing import Any, Final, Literal, Protocol

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict

from truealpha_contracts.execution import AvailabilityStatus, FactorValidationStatus, InputEvidenceStatus

#: The issuer predicate of both twins (the App's `bidirectionalIssuerPredicate`): the stored
#: id, or its entity id, or its legacy id. psycopg takes the same value three times.
_ISSUER_PREDICATE = (
    "({alias}.issuer_id = %s"
    " or {alias}.issuer_id in (select entity_id::text from mart.entity_identity where legacy_id = %s)"
    " or {alias}.issuer_id in (select legacy_id from mart.entity_identity"
    " where entity_id::text = %s and legacy_id is not null))"
)
#: Only a run that the served head names (the App's `servedRunPredicate`). A withheld head
#: has a null `run_id`.
_SERVED_RUN = "{alias}.run_id in (select h.run_id from mart.served_head h where h.run_id is not null)"

SUPPLY_CHAIN_EXPOSURE_SQL = f"""
select s.run_id,
       to_char(s.cutoff at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as cutoff,
       s.exposure_score::text as exposure_score,
       s.direct_partners,
       s.suppliers_count,
       s.customers_count,
       s.max_partner_share::text as max_partner_share,
       s.confidence::text as confidence,
       s.reason_codes,
       s.extractor,
       coalesce(s.availability_status, 'unavailable') as availability_status,
       coalesce(s.source_evidence_status, 'degraded') as source_evidence_status,
       coalesce(s.factor_validation_status, 'not_evaluated') as factor_validation_status
from mart.issuer_supply_chain_exposure s
where {_ISSUER_PREDICATE.format(alias="s")}
  and {_SERVED_RUN.format(alias="s")}
order by (s.availability_status = 'available') desc, s.cutoff desc, s.created_at desc
limit 1
"""

ANALYST_RATINGS_SQL = f"""
select r.run_id,
       to_char(r.cutoff at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as cutoff,
       r.consensus_rating::text as consensus_rating,
       coalesce(
           to_jsonb(r) ->> 'target_price',
           (select substring(rc from '^target_price:([0-9.]+)$')
            from unnest(r.reason_codes) as rc
            where rc like 'target_price:%%'
            limit 1)
       ) as target_price,
       r.analysts_count,
       r.buy_count,
       r.hold_count,
       r.sell_count,
       r.confidence::text as confidence,
       r.reason_codes,
       r.extractor,
       coalesce(r.availability_status, 'unavailable') as availability_status,
       coalesce(r.source_evidence_status, 'degraded') as source_evidence_status,
       coalesce(r.factor_validation_status, 'not_evaluated') as factor_validation_status
from mart.issuer_analyst_ratings r
where {_ISSUER_PREDICATE.format(alias="r")}
  and {_SERVED_RUN.format(alias="r")}
order by (r.availability_status = 'available') desc, r.cutoff desc, r.created_at desc
limit 1
"""


class _ModuleRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    cutoff: str
    confidence: str
    reason_codes: tuple[str, ...]
    extractor: str
    availability_status: AvailabilityStatus
    source_evidence_status: InputEvidenceStatus
    factor_validation_status: FactorValidationStatus


class SupplyChainExposure(_ModuleRow):
    """One `mart.issuer_supply_chain_exposure` row (module 3)."""

    exposure_score: str | None
    direct_partners: int
    suppliers_count: int
    customers_count: int
    max_partner_share: str


class AnalystRatings(_ModuleRow):
    """One `mart.issuer_analyst_ratings` row (module 4)."""

    consensus_rating: str | None
    #: No producer writes a target price today (no column, no `target_price:` reason code);
    #: the read keeps the App's expression, so both surfaces show the same null.
    target_price: str | None
    analysts_count: int
    buy_count: int
    hold_count: int
    sell_count: int


#: The served head holds no row of the module for the issuer.
NO_SERVED_ROW: Final = "no_row_in_served_head"


class IssuerModuleUnavailable(BaseModel):
    """The typed answer when the served head holds no row of a module for the issuer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    issuer_id: str
    module: Literal["supply_chain_exposure", "analyst_ratings"]
    reason: Literal["no_row_in_served_head"] = NO_SERVED_ROW


class IssuerModuleReader(Protocol):
    """The module 3 and module 4 read. None means the served head holds no row for the issuer."""

    def supply_chain_exposure(self, issuer_id: str) -> SupplyChainExposure | None: ...

    def analyst_ratings(self, issuer_id: str) -> AnalystRatings | None: ...


def _row(sql: str, database_url: str, issuer_id: str) -> dict[str, Any] | None:
    with psycopg.connect(database_url, connect_timeout=5, autocommit=True) as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(sql, (issuer_id, issuer_id, issuer_id))
            return cursor.fetchone()


class PostgresIssuerModuleReader:
    """Reads one served row per module. A database error propagates: the caller decides
    whether it is a failed section or a failed tool call, and never reads it as "no row"."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def supply_chain_exposure(self, issuer_id: str) -> SupplyChainExposure | None:
        row = _row(SUPPLY_CHAIN_EXPOSURE_SQL, self._database_url, issuer_id)
        return None if row is None else SupplyChainExposure.model_validate(_normalized(row))

    def analyst_ratings(self, issuer_id: str) -> AnalystRatings | None:
        row = _row(ANALYST_RATINGS_SQL, self._database_url, issuer_id)
        return None if row is None else AnalystRatings.model_validate(_normalized(row))


def _normalized(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "reason_codes": tuple(str(code) for code in row["reason_codes"] or ())}
