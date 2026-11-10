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

KNOWN_ENTITY_DISPLAY: Final[dict[str, tuple[str, str]]] = {
    "issuer:cik:0000723125": ("MU", "Micron Technology (美光科技)"),
    "issuer:cik:0000104169": ("WMT", "Walmart (沃尔玛)"),
    "issuer:cik:0001326801": ("META", "Meta Platforms"),
    "issuer:cik:0001018724": ("AMZN", "Amazon (亚马逊)"),
    "issuer:cik:0001045810": ("NVDA", "NVIDIA (英伟达)"),
    "issuer:cik:0001065280": ("NFLX", "Netflix (奈飞)"),
    "issuer:cik:0000019617": ("JPM", "JPMorgan Chase (摩根大通)"),
    "issuer:cik:0000059478": ("LLY", "Eli Lilly (礼来)"),
    "issuer:cik:0000200406": ("JNJ", "Johnson & Johnson (强生)"),
    "issuer:cik:0000320193": ("AAPL", "Apple (苹果)"),
    "issuer:cik:0000789019": ("MSFT", "Microsoft (微软)"),
    "issuer:cik:0000909832": ("COST", "Costco (好市多)"),
    "issuer:cik:0001067983": ("BRK.B", "Berkshire Hathaway (伯克希尔)"),
    "issuer:cik:0001141391": ("MA", "Mastercard (万事达)"),
    "issuer:cik:0001318605": ("TSLA", "Tesla (特斯拉)"),
    "issuer:cik:0001403161": ("V", "Visa"),
    "issuer:cik:0001551152": ("ABBV", "AbbVie (艾伯维)"),
    "issuer:cik:0001652044": ("GOOGL", "Alphabet (谷歌)"),
    "issuer:cik:0001730168": ("AVGO", "Broadcom (博通)"),
    "issuer:lei:J3WHBG0MTS7O8ZVMDC91": ("XOM", "Exxon Mobil (埃克森美孚)"),
    "issuer:cik:0000034088": ("XOM", "Exxon Mobil (埃克森美孚)"),
    "issuer:cik:0000354950": ("HD", "Home Depot (家得宝)"),
    "issuer:cik:0001075531": ("BKNG", "Booking Holdings"),
    "issuer:cik:0000937966": ("ASML", "ASML Holding (阿斯麦)"),
    "issuer:cik:0000049196": ("HBAN", "Huntington Bancshares"),
    "issuer:cik:0000020286": ("CINF", "Cincinnati Financial"),
    "issuer:cik:0001535527": ("CRWD", "CrowdStrike"),
    "issuer:cik:0000829224": ("SBUX", "Starbucks (星巴克)"),
    "issuer:cik:0001327567": ("PANW", "Palo Alto Networks"),
    "issuer:cik:0000896878": ("INTU", "Intuit"),
    "issuer:adm": ("ADM", "Archer-Daniels-Midland"),
    "issuer:nice": ("NICE", "NICE Ltd."),
    "issuer:ddog": ("DDOG", "Datadog"),
    "issuer:shop": ("SHOP", "Shopify"),
}

_TICKER_DISPLAY_INDEX: Final[dict[str, tuple[str, str, str]]] = {
    ticker.upper(): (entity_id, ticker, name) for entity_id, (ticker, name) in KNOWN_ENTITY_DISPLAY.items()
}

_ENTITY_DISPLAY_SQL = """
select r.issuer_id,
       coalesce(nullif(btrim(r.ticker), ''), nullif(btrim(ei.current_ticker), '')) as ticker,
       coalesce(nullif(btrim(r.display_name), ''), nullif(btrim(ei.name), '')) as display_name,
       ei.legacy_id
from mart.entity_display_resolution r
left join (
    select distinct on (e.entity_id::text)
           e.entity_id::text as issuer_id,
           e.current_ticker,
           e.name,
           e.legacy_id
    from mart.entity_identity e
    order by e.entity_id::text, e.legacy_id
) ei on ei.issuer_id = r.issuer_id
"""


def resolve_static_entity_display(entity_id: str) -> tuple[str | None, str | None, str]:
    """Return `(issuer_id, ticker, display_name)` from the canonical static map and identifier conventions."""
    clean = entity_id.strip()
    if not clean:
        return None, None, entity_id
    direct = KNOWN_ENTITY_DISPLAY.get(clean) or KNOWN_ENTITY_DISPLAY.get(clean.lower())
    if direct is not None:
        ticker, name = direct
        return clean, ticker, f"{ticker} · {name}"
    if clean.lower().startswith("listing:"):
        parts = clean.split(":")
        if len(parts) >= 3:
            raw_ticker = parts[2].upper()
            matched = _TICKER_DISPLAY_INDEX.get(raw_ticker)
            if matched is not None:
                iss_id, ticker, name = matched
                return iss_id, ticker, f"{ticker} · {name}"
            return None, raw_ticker, raw_ticker
    upper = clean.upper()
    if upper in _TICKER_DISPLAY_INDEX:
        iss_id, ticker, name = _TICKER_DISPLAY_INDEX[upper]
        return iss_id, ticker, f"{ticker} · {name}"
    if clean.lower().startswith(("issuer:cik:", "company:cik:", "cik:")):
        digits = clean.split(":")[-1].strip().zfill(10)
        cand_key = f"issuer:cik:{digits}"
        if cand_key in KNOWN_ENTITY_DISPLAY:
            ticker, name = KNOWN_ENTITY_DISPLAY[cand_key]
            return cand_key, ticker, f"{ticker} · {name}"
    return clean if clean.startswith("issuer:") else None, None, clean


def _format_display_label(ticker: str, raw_name: str) -> str:
    if ticker and raw_name:
        return raw_name if raw_name.startswith(f"{ticker} ·") else f"{ticker} · {raw_name}"
    return ticker or raw_name


def _merge_display_row(merged: dict[str, tuple[str, str]], row: dict[str, Any]) -> None:
    issuer_id = str(row.get("issuer_id") or "").strip()
    legacy_id = str(row.get("legacy_id") or "").strip()
    fallback = KNOWN_ENTITY_DISPLAY.get(legacy_id) or KNOWN_ENTITY_DISPLAY.get(issuer_id)
    ticker = str(row.get("ticker") or "").strip() or (fallback[0] if fallback else "")
    raw_name = str(row.get("display_name") or "").strip() or (fallback[1] if fallback else "")
    label = _format_display_label(ticker, raw_name)
    if not label:
        return
    if issuer_id:
        merged[issuer_id] = (ticker or issuer_id, label)
    if legacy_id and legacy_id not in merged:
        merged[legacy_id] = (ticker or legacy_id, label)


def load_entity_display_map(database_url: str | None) -> dict[str, tuple[str, str]]:
    """Load `(ticker, display_name)` keyed by both UUID and `legacy_id` from `mart.entity_display_resolution`,
    merged with `KNOWN_ENTITY_DISPLAY` so no consumer surface renders a bare CIK or LEI."""
    merged: dict[str, tuple[str, str]] = {}
    for key, (ticker, name) in KNOWN_ENTITY_DISPLAY.items():
        merged[key] = (ticker, f"{ticker} · {name}")
        merged[ticker] = (ticker, f"{ticker} · {name}")
    if not database_url:
        return merged
    try:
        with psycopg.connect(database_url, connect_timeout=5, autocommit=True) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(_ENTITY_DISPLAY_SQL)
                rows = cursor.fetchall()
    except psycopg.Error:
        return merged
    for row in rows:
        _merge_display_row(merged, row)
    return merged


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
