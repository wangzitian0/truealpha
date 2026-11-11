"""Module 3 and module 4 rows of the served governed head (#42)."""

from __future__ import annotations

from typing import Any, Final, Literal, Protocol

import psycopg
from psycopg.rows import dict_row

from truealpha_contracts.common import FrozenModel
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
    return (
        f"({column} = {placeholder}"
        f" or {column} in (select ei.entity_id::text from mart.entity_identity ei"
        f" cross join (select btrim({placeholder}::text) as q) inp where {_IDENTITY_MATCH_SQL})"
        f" or {column} in (select ei.legacy_id from mart.entity_identity ei"
        f" cross join (select btrim({placeholder}::text) as q) inp where ei.legacy_id is not null and ({_IDENTITY_MATCH_SQL})))"
    )


_RESOLVE_CANDIDATES_SQL = (
    "select ei.entity_id::text as entity_id, ei.legacy_id, ei.current_ticker, ei.cik, ei.lei"
    f" from mart.entity_identity ei cross join (select btrim(%s::text) as q) inp where {_IDENTITY_MATCH_SQL}"
)
_RESOLVE_BATCH_CANDIDATES_SQL = (
    "select u.raw_q, ei.entity_id::text as entity_id, ei.legacy_id, ei.current_ticker, ei.cik, ei.lei"
    " from unnest(%s::text[]) as u(raw_q) cross join lateral (select btrim(u.raw_q) as q) inp"
    f" join mart.entity_identity ei on ({_IDENTITY_MATCH_SQL})"
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
    ticker.upper(): (eid, ticker, name) for eid, (ticker, name) in KNOWN_ENTITY_DISPLAY.items()
}

_ENTITY_DISPLAY_SQL = """
select r.issuer_id,
       coalesce(nullif(btrim(r.ticker), ''), nullif(btrim(ei.current_ticker), '')) as ticker,
       coalesce(nullif(btrim(r.display_name), ''), nullif(btrim(ei.name), '')) as display_name,
       ei.legacy_id
from mart.entity_display_resolution r
left join (
    select distinct on (e.entity_id::text)
           e.entity_id::text as issuer_id, e.current_ticker, e.name, e.legacy_id
    from mart.entity_identity e
    order by e.entity_id::text, e.legacy_id
) ei on ei.issuer_id = r.issuer_id
"""


def _query_rows(database_url: str, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with (
        psycopg.connect(database_url, connect_timeout=5, autocommit=True) as conn,
        conn.cursor(row_factory=dict_row) as cur,
    ):
        cur.execute(sql, params)
        return cur.fetchall()


def resolve_static_entity_display(entity_id: str) -> tuple[str | None, str | None, str]:
    clean = entity_id.strip()
    if not clean:
        return None, None, entity_id
    if direct := KNOWN_ENTITY_DISPLAY.get(clean) or KNOWN_ENTITY_DISPLAY.get(clean.lower()):
        return clean, direct[0], f"{direct[0]} · {direct[1]}"
    if clean.lower().startswith("listing:") and len(parts := clean.split(":")) >= 3:
        raw_ticker = parts[2].upper()
        if matched := _TICKER_DISPLAY_INDEX.get(raw_ticker):
            return matched[0], matched[1], f"{matched[1]} · {matched[2]}"
        return None, raw_ticker, raw_ticker
    if matched := _TICKER_DISPLAY_INDEX.get(clean.upper()):
        return matched[0], matched[1], f"{matched[1]} · {matched[2]}"
    if clean.lower().startswith(("issuer:cik:", "company:cik:", "cik:")):
        cand_key = f"issuer:cik:{clean.split(':')[-1].strip().zfill(10)}"
        if entry := KNOWN_ENTITY_DISPLAY.get(cand_key):
            return cand_key, entry[0], f"{entry[0]} · {entry[1]}"
    return (clean if clean.startswith("issuer:") else None), None, clean


def load_entity_display_map(database_url: str | None) -> dict[str, tuple[str, str]]:
    merged: dict[str, tuple[str, str]] = {}
    for key, (ticker, name) in KNOWN_ENTITY_DISPLAY.items():
        merged[key] = merged[ticker] = (ticker, f"{ticker} · {name}")
    if not database_url:
        return merged
    try:
        rows = _query_rows(database_url, _ENTITY_DISPLAY_SQL)
    except psycopg.Error:
        return merged
    for row in rows:
        issuer_id = str(row.get("issuer_id") or "").strip()
        legacy_id = str(row.get("legacy_id") or "").strip()
        fallback = KNOWN_ENTITY_DISPLAY.get(legacy_id) or KNOWN_ENTITY_DISPLAY.get(issuer_id)
        ticker = str(row.get("ticker") or "").strip() or (fallback[0] if fallback else "")
        raw_name = str(row.get("display_name") or "").strip() or (fallback[1] if fallback else "")
        label = (
            (raw_name if raw_name.startswith(f"{ticker} ·") else f"{ticker} · {raw_name}")
            if (ticker and raw_name)
            else (ticker or raw_name)
        )
        if label:
            if issuer_id:
                merged[issuer_id] = (ticker or issuer_id, label)
            if legacy_id and legacy_id not in merged:
                merged[legacy_id] = (ticker or legacy_id, label)
    return merged


def _module_sql(table: str, alias: str, cols: str) -> str:
    return f"""
select {alias}.run_id,
       to_char({alias}.cutoff at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as cutoff,
       {cols},
       {alias}.confidence::text as confidence, {alias}.reason_codes, {alias}.extractor,
       coalesce({alias}.availability_status, 'unavailable') as availability_status,
       coalesce({alias}.source_evidence_status, 'degraded') as source_evidence_status,
       coalesce({alias}.factor_validation_status, 'not_evaluated') as factor_validation_status
from {table} {alias}
where {issuer_identity_sql(f"{alias}.issuer_id", "%s")}
  and {alias}.run_id in (select h.run_id from mart.served_head h where h.run_id is not null)
order by ({alias}.availability_status = 'available') desc, {alias}.cutoff desc, {alias}.created_at desc
limit 1
"""


SUPPLY_CHAIN_EXPOSURE_SQL = _module_sql(
    "mart.issuer_supply_chain_exposure",
    "s",
    "s.exposure_score::text as exposure_score, s.direct_partners, s.suppliers_count, s.customers_count, s.max_partner_share::text as max_partner_share",
)

ANALYST_RATINGS_SQL = _module_sql(
    "mart.issuer_analyst_ratings",
    "r",
    """r.consensus_rating::text as consensus_rating,
       coalesce(
           to_jsonb(r) ->> 'target_price',
           (select substring(rc from '^target_price:([0-9.]+)$')
            from unnest(r.reason_codes) as rc where rc like 'target_price:%%' limit 1)
       ) as target_price,
       r.analysts_count, r.buy_count, r.hold_count, r.sell_count""",
)


class _ModuleRow(FrozenModel):
    run_id: str
    cutoff: str
    confidence: str
    reason_codes: tuple[str, ...]
    extractor: str
    availability_status: AvailabilityStatus
    source_evidence_status: InputEvidenceStatus
    factor_validation_status: FactorValidationStatus


class SupplyChainExposure(_ModuleRow):
    exposure_score: str | None
    direct_partners: int
    suppliers_count: int
    customers_count: int
    max_partner_share: str


class AnalystRatings(_ModuleRow):
    consensus_rating: str | None
    target_price: str | None
    analysts_count: int
    buy_count: int
    hold_count: int
    sell_count: int


NO_SERVED_ROW: Final = "no_row_in_served_head"


class IssuerModuleUnavailable(FrozenModel):
    issuer_id: str
    module: Literal["supply_chain_exposure", "analyst_ratings"]
    reason: Literal["no_row_in_served_head"] = NO_SERVED_ROW


class IssuerModuleReader(Protocol):
    def supply_chain_exposure(self, issuer_id: str) -> SupplyChainExposure | None: ...
    def analyst_ratings(self, issuer_id: str) -> AnalystRatings | None: ...


def _append_row_aliases(candidates: list[str], row: dict[str, Any]) -> None:
    def _add(v: Any) -> None:
        if isinstance(v, str) and v and v not in candidates:
            candidates.append(v)

    for k in ("entity_id", "legacy_id", "current_ticker", "cik", "lei"):
        _add(row.get(k))
    if cik := row.get("cik"):
        _add(f"issuer:cik:{cik}")
        _add(f"company:cik:{cik}")
    if lei := row.get("lei"):
        _add(f"issuer:lei:{lei}")


def resolve_issuer_id_candidates(database_url: str, issuer_id: str) -> tuple[str, ...]:
    clean = issuer_id.strip()
    if not clean:
        return ()
    candidates = [clean]
    try:
        for row in _query_rows(database_url, _RESOLVE_CANDIDATES_SQL, (clean,)):
            _append_row_aliases(candidates, row)
    except psycopg.Error:
        pass
    return tuple(candidates)


def resolve_issuer_id_candidates_batch(
    database_url: str,
    issuer_ids: tuple[str, ...] | list[str],
) -> dict[str, tuple[str, ...]]:
    by_input: dict[str, list[str]] = {c: [c] for item in issuer_ids if item and (c := item.strip())}
    if not by_input:
        return {}
    try:
        for row in _query_rows(database_url, _RESOLVE_BATCH_CANDIDATES_SQL, (list(by_input),)):
            if (target := by_input.get(str(row.get("raw_q") or ""))) is not None:
                _append_row_aliases(target, row)
    except psycopg.Error:
        pass
    return {raw: tuple(vals) for raw, vals in by_input.items()}


class PostgresIssuerModuleReader:
    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def _one(self, sql: str, issuer_id: str) -> dict[str, Any] | None:
        rows = _query_rows(self._database_url, sql, (issuer_id, issuer_id, issuer_id))
        return None if not rows else {**rows[0], "reason_codes": tuple(str(c) for c in rows[0]["reason_codes"] or ())}

    def supply_chain_exposure(self, issuer_id: str) -> SupplyChainExposure | None:
        row = self._one(SUPPLY_CHAIN_EXPOSURE_SQL, issuer_id)
        return None if row is None else SupplyChainExposure.model_validate(row)

    def analyst_ratings(self, issuer_id: str) -> AnalystRatings | None:
        row = self._one(ANALYST_RATINGS_SQL, issuer_id)
        return None if row is None else AnalystRatings.model_validate(row)
