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

_IDENTITY_MATCH_SQL = (
    "ei.entity_id::text = lower(inp.q)"
    " or ei.legacy_id = inp.q"
    " or upper(ei.legacy_id) = upper(inp.q)"
    " or upper(ei.current_ticker) = upper(inp.q)"
    " or (inp.q ~* '^listing:[0-9a-z]{4}:[0-9a-z.\\-]+$' and upper(ei.current_ticker) = upper(split_part(inp.q, ':', 3)))"
    " or (regexp_replace(inp.q, '^(?:issuer:cik:|company:cik:|cik:)', '', 'i') ~ '^[0-9]{1,10}$'"
    "     and ei.cik = lpad(regexp_replace(inp.q, '^(?:issuer:cik:|company:cik:|cik:)', '', 'i'), 10, '0'))"
    " or upper(ei.lei) = upper(regexp_replace(inp.q, '^(?:issuer:lei:|company:lei:|lei:)', '', 'i'))"
)


def issuer_identity_sql(column: str, placeholder: str = "%s") -> str:
    """Return the unified SQL predicate resolving any issuer alias (UUID, ticker, CIK, LEI, legacy_id)."""
    return (
        f"({column} = {placeholder}"
        f" or {column} in ("
        f"select ei.entity_id::text from mart.entity_identity ei"
        f" cross join (select btrim({placeholder}::text) as q) inp"
        f" where {_IDENTITY_MATCH_SQL})"
        f" or {column} in ("
        f"select ei.legacy_id from mart.entity_identity ei"
        f" cross join (select btrim({placeholder}::text) as q) inp"
        f" where ei.legacy_id is not null and ({_IDENTITY_MATCH_SQL})))"
    )


_RESOLVE_CANDIDATES_SQL = (
    "select ei.entity_id::text as entity_id, ei.legacy_id, ei.current_ticker, ei.cik, ei.lei"
    " from mart.entity_identity ei"
    " cross join (select btrim(%s::text) as q) inp"
    f" where {_IDENTITY_MATCH_SQL}"
)

_RESOLVE_BATCH_CANDIDATES_SQL = (
    "select u.raw_q,"
    "       ei.entity_id::text as entity_id,"
    "       ei.legacy_id,"
    "       ei.current_ticker,"
    "       ei.cik,"
    "       ei.lei"
    " from unnest(%s::text[]) as u(raw_q)"
    " cross join lateral (select btrim(u.raw_q) as q) inp"
    " join mart.entity_identity ei"
    f"   on ({_IDENTITY_MATCH_SQL})"
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
where {issuer_identity_sql("s.issuer_id", "%s")}
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
where {issuer_identity_sql("r.issuer_id", "%s")}
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


def _append_row_aliases(candidates: list[str], row: dict[str, Any]) -> None:
    for key in ("entity_id", "legacy_id", "current_ticker", "cik", "lei"):
        val = row.get(key)
        if isinstance(val, str) and val and val not in candidates:
            candidates.append(val)
    cik_val = row.get("cik")
    if isinstance(cik_val, str) and cik_val:
        for prefix in ("issuer:cik:", "company:cik:"):
            prefixed = f"{prefix}{cik_val}"
            if prefixed not in candidates:
                candidates.append(prefixed)
    lei_val = row.get("lei")
    if isinstance(lei_val, str) and lei_val:
        prefixed_lei = f"issuer:lei:{lei_val}"
        if prefixed_lei not in candidates:
            candidates.append(prefixed_lei)


def resolve_issuer_id_candidates(database_url: str, issuer_id: str) -> tuple[str, ...]:
    """Return every identifier alias (`entity_id`, `legacy_id`, `current_ticker`, `cik`, `lei`)
    in `mart.entity_identity` that resolves to the same issuer as `issuer_id`."""
    clean = issuer_id.strip()
    if not clean:
        return ()
    candidates: list[str] = [clean]
    try:
        with psycopg.connect(database_url, connect_timeout=5, autocommit=True) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(_RESOLVE_CANDIDATES_SQL, (clean,))
                rows = cursor.fetchall()
    except psycopg.Error:
        return tuple(candidates)
    for row in rows:
        _append_row_aliases(candidates, row)
    return tuple(candidates)


def resolve_issuer_id_candidates_batch(
    database_url: str,
    issuer_ids: tuple[str, ...] | list[str],
) -> dict[str, tuple[str, ...]]:
    """Resolve multiple issuer identifiers in a single Postgres round-trip."""
    cleaned = [item.strip() for item in issuer_ids if item and item.strip()]
    if not cleaned:
        return {}
    by_input: dict[str, list[str]] = {raw: [raw] for raw in cleaned}
    try:
        with psycopg.connect(database_url, connect_timeout=5, autocommit=True) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(_RESOLVE_BATCH_CANDIDATES_SQL, (list(by_input.keys()),))
                rows = cursor.fetchall()
    except psycopg.Error:
        return {raw: tuple(vals) for raw, vals in by_input.items()}
    for row in rows:
        raw_q = str(row.get("raw_q") or "")
        target = by_input.get(raw_q)
        if target is not None:
            _append_row_aliases(target, row)
    return {raw: tuple(vals) for raw, vals in by_input.items()}


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
