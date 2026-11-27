"""Postgres readers for served issuer modules and entity display maps (#42)."""

from __future__ import annotations

from typing import Any, Final, Literal, Protocol

import psycopg
from psycopg.rows import dict_row
from pydantic import SerializerFunctionWrapHandler, model_serializer
from truealpha_contracts.common import FrozenModel, drop_none_fields, load_data_json
from truealpha_contracts.execution import AvailabilityStatus, InputEvidenceStatus

NO_SERVED_ROW: Final = "no_row_in_served_head"
KNOWN_ENTITY_DISPLAY: Final[dict[str, tuple[str, str]]] = {
    k: (v[0], v[1]) for k, v in load_data_json("registries.v1.json")["known_entity_display"].items()
}
_TICKER_DISPLAY_INDEX: Final[dict[str, tuple[str, str, str]]] = {
    t.upper(): (eid, t, n) for eid, (t, n) in KNOWN_ENTITY_DISPLAY.items()
}


def resolve_static_entity_display(entity_id: str) -> tuple[str | None, str | None, str]:
    clean = entity_id.strip()
    if not clean:
        return None, None, entity_id
    if direct := KNOWN_ENTITY_DISPLAY.get(clean) or KNOWN_ENTITY_DISPLAY.get(clean.lower()):
        return clean, direct[0], f"{direct[0]} · {direct[1]}"
    if clean.lower().startswith("listing:") and len(parts := clean.split(":")) >= 3:
        raw_t = parts[2].upper()
        return (m[0], m[1], f"{m[1]} · {m[2]}") if (m := _TICKER_DISPLAY_INDEX.get(raw_t)) else (None, raw_t, raw_t)
    if m := _TICKER_DISPLAY_INDEX.get(clean.upper()):
        return m[0], m[1], f"{m[1]} · {m[2]}"
    if clean.lower().startswith(("issuer:cik:", "company:cik:", "cik:")):
        cand = f"issuer:cik:{clean.split(':')[-1].strip().zfill(10)}"
        if entry := KNOWN_ENTITY_DISPLAY.get(cand):
            return cand, entry[0], f"{entry[0]} · {entry[1]}"
    return (clean if clean.startswith("issuer:") else None), None, clean


class _ModuleRow(FrozenModel):
    run_id: str
    cutoff: str
    confidence: str
    reason_codes: tuple[str, ...]
    extractor: str
    availability_status: AvailabilityStatus
    source_evidence_status: InputEvidenceStatus


class SupplyChainExposure(_ModuleRow):
    exposure_score: str | None
    direct_partners: int
    suppliers_count: int
    customers_count: int
    max_partner_share: str


class AnalystRatings(_ModuleRow):
    consensus_rating: str | None
    target_price: str | None
    track_record_score: str | None = None
    analysts_count: int
    buy_count: int
    hold_count: int
    sell_count: int

    @model_serializer(mode="wrap")
    def _omit_unset(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return drop_none_fields(handler(self), "track_record_score")


class IssuerModuleUnavailable(FrozenModel):
    issuer_id: str
    module: Literal["supply_chain_exposure", "analyst_ratings"]
    reason: Literal["no_row_in_served_head"] = NO_SERVED_ROW


class IssuerModuleReader(Protocol):
    def supply_chain_exposure(self, issuer_id: str) -> SupplyChainExposure | None: ...
    def analyst_ratings(self, issuer_id: str) -> AnalystRatings | None: ...


__all__ = [
    "ANALYST_RATINGS_SQL",
    "AnalystRatings",
    "IssuerModuleReader",
    "IssuerModuleUnavailable",
    "KNOWN_ENTITY_DISPLAY",
    "NO_SERVED_ROW",
    "PostgresIssuerModuleReader",
    "SUPPLY_CHAIN_EXPOSURE_SQL",
    "SupplyChainExposure",
    "issuer_identity_sql",
    "load_entity_display_map",
    "resolve_issuer_candidates_from_cursor",
    "resolve_issuer_id_candidates_batch",
    "resolve_issuer_identity_from_cursor",
    "resolve_static_entity_display",
]

_IDENTITY_MATCH_SQL = (
    "ei.entity_id::text = lower(inp.q) or ei.legacy_id = inp.q or upper(ei.legacy_id) = upper(inp.q)"
    " or upper(ei.current_ticker) = upper(inp.q)"
    " or (inp.q ~* '^listing:[0-9a-z]{4}:[0-9a-z.\\-]+$' and upper(ei.current_ticker) = upper(split_part(inp.q, ':', 3)))"
    " or (regexp_replace(inp.q, '^(?:issuer:cik:|company:cik:|cik:)', '', 'i') ~ '^[0-9]{1,10}$'"
    "     and ei.cik = lpad(regexp_replace(inp.q, '^(?:issuer:cik:|company:cik:|cik:)', '', 'i'), 10, '0'))"
    " or upper(ei.lei) = upper(regexp_replace(inp.q, '^(?:issuer:lei:|company:lei:|lei:)', '', 'i'))"
)


def issuer_identity_sql(column: str, placeholder: str = "%s") -> str:
    return (
        f"({column} = {placeholder} or {column} in (select ei.entity_id::text from mart.entity_identity ei cross join (select btrim({placeholder}::text) as q) inp where {_IDENTITY_MATCH_SQL})"
        f" or {column} in (select ei.legacy_id from mart.entity_identity ei cross join (select btrim({placeholder}::text) as q) inp where ei.legacy_id is not null and ({_IDENTITY_MATCH_SQL})))"
    )


_RESOLVE_BATCH_CANDIDATES_SQL = (
    f"select u.raw_q, ei.entity_id::text as entity_id, ei.legacy_id, ei.current_ticker, ei.cik, ei.lei "
    f"from unnest(%s::text[]) as u(raw_q) cross join lateral (select btrim(u.raw_q) as q) inp join mart.entity_identity ei on ({_IDENTITY_MATCH_SQL})"
)
ENTITY_DISPLAY_SQL = (
    "select r.issuer_id, coalesce(nullif(btrim(r.ticker), ''), nullif(btrim(ei.current_ticker), '')) as ticker, "
    "coalesce(nullif(btrim(r.display_name), ''), nullif(btrim(ei.name), '')) as display_name, ei.legacy_id "
    "from mart.entity_display_resolution r left join (select distinct on (e.entity_id::text) e.entity_id::text as issuer_id, "
    "e.current_ticker, e.name, e.legacy_id from mart.entity_identity e order by e.entity_id::text, e.legacy_id) ei on ei.issuer_id = r.issuer_id"
)


def _query_rows(database_url: str, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with (
        psycopg.connect(database_url, connect_timeout=5) as conn,
        conn.cursor(row_factory=dict_row) as cur,
    ):
        cur.execute("set local role mart_readonly")
        cur.execute("select set_config('statement_timeout', '5s', true)")
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.execute("reset role")
        return rows


def load_entity_display_map(database_url: str | None) -> dict[str, tuple[str, str]]:
    merged = {k: (t, f"{t} · {n}") for k, (t, n) in KNOWN_ENTITY_DISPLAY.items()}
    merged.update({t: (t, f"{t} · {n}") for t, n in KNOWN_ENTITY_DISPLAY.values()})
    if not database_url:
        return merged
    try:
        rows = _query_rows(database_url, ENTITY_DISPLAY_SQL)
    except psycopg.Error:
        return merged
    for row in rows:
        iid, leg = str(row.get("issuer_id") or "").strip(), str(row.get("legacy_id") or "").strip()
        fb = KNOWN_ENTITY_DISPLAY.get(leg) or KNOWN_ENTITY_DISPLAY.get(iid)
        t = str(row.get("ticker") or "").strip() or (fb[0] if fb else "")
        n = str(row.get("display_name") or "").strip() or (fb[1] if fb else "")
        label = (n if n.startswith(f"{t} ·") else f"{t} · {n}") if (t and n) else (t or n)
        if label:
            if iid:
                merged[iid] = (t or iid, label)
            if leg and leg not in merged:
                merged[leg] = (t or leg, label)
    return merged


def _module_sql(table: str, alias: str, cols: str, *, where_issuer: str | None = None) -> str:
    issuer_pred = where_issuer or issuer_identity_sql(f"{alias}.issuer_id", "%s")
    return (
        f"select {alias}.run_id, to_char({alias}.cutoff at time zone 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') as cutoff, {cols}, "
        f"{alias}.confidence::text as confidence, {alias}.reason_codes, {alias}.extractor, coalesce({alias}.availability_status, 'unavailable') as availability_status, "
        f"coalesce({alias}.source_evidence_status, 'degraded') as source_evidence_status "
        f"from {table} {alias} where {issuer_pred} and {alias}.run_id in ("
        "select h.run_id from mart.served_head h where h.run_id is not null and not starts_with(h.universe_id, 'universe:canary-')"
        ") "
        f"order by ({alias}.availability_status = 'available') desc, {alias}.cutoff desc, {alias}.created_at desc limit 1"
    )


_SUPPLY_CHAIN_COLS = "s.exposure_score::text as exposure_score, s.direct_partners, s.suppliers_count, s.customers_count, s.max_partner_share::text as max_partner_share"
_ANALYST_RATINGS_COLS = (
    "r.consensus_rating::text as consensus_rating, "
    "coalesce(to_jsonb(r) ->> 'target_price', "
    "(select substring(rc from '^target_price:([0-9.]+)$') from unnest(r.reason_codes) as rc where rc like 'target_price:%%' limit 1)) as target_price, "
    "to_jsonb(r) ->> 'track_record_score' as track_record_score, "
    "r.analysts_count, r.buy_count, r.hold_count, r.sell_count"
)
SUPPLY_CHAIN_EXPOSURE_SQL = _module_sql("mart.issuer_supply_chain_exposure", "s", _SUPPLY_CHAIN_COLS)
ANALYST_RATINGS_SQL = _module_sql("mart.issuer_analyst_ratings", "r", _ANALYST_RATINGS_COLS)
_SUPPLY_CHAIN_BY_IDS_SQL = _module_sql(
    "mart.issuer_supply_chain_exposure", "s", _SUPPLY_CHAIN_COLS, where_issuer="s.issuer_id = any(%s::text[])"
)
_ANALYST_RATINGS_BY_IDS_SQL = _module_sql(
    "mart.issuer_analyst_ratings", "r", _ANALYST_RATINGS_COLS, where_issuer="r.issuer_id = any(%s::text[])"
)


def _append_resolved_candidates(target: list[str], row: dict[str, Any]) -> None:
    for k in ("entity_id", "legacy_id", "current_ticker", "cik", "lei"):
        if isinstance(v := row.get(k), str) and v and v not in target:
            target.append(v)
    for pre, k in (("issuer:cik:", "cik"), ("company:cik:", "cik"), ("issuer:lei:", "lei")):
        if (v := row.get(k)) and f"{pre}{v}" not in target:
            target.append(f"{pre}{v}")


def resolve_issuer_identity_from_cursor(cur: Any, issuer_id: str) -> tuple[list[str], dict[str, str]]:
    """Resolve candidate identifiers and ``entity_id -> legacy_id`` mapping on an open cursor."""
    clean = issuer_id.strip()
    if not clean:
        return [issuer_id], {}
    out = [clean]
    legacy_by_entity: dict[str, str] = {}
    try:
        cur.execute("savepoint _issuer_cand_sp")
        cur.execute(_RESOLVE_BATCH_CANDIDATES_SQL, ([clean],))
        for row in cur.fetchall():
            _append_resolved_candidates(out, row)
            eid = row.get("entity_id")
            leg = row.get("legacy_id")
            if isinstance(eid, str) and eid and isinstance(leg, str) and leg:
                legacy_by_entity.setdefault(eid, leg)
        cur.execute("release savepoint _issuer_cand_sp")
    except psycopg.Error:
        try:
            cur.execute("rollback to savepoint _issuer_cand_sp")
            cur.execute("release savepoint _issuer_cand_sp")
        except psycopg.Error:
            pass
    return out, legacy_by_entity


def resolve_issuer_candidates_from_cursor(cur: Any, issuer_id: str) -> list[str]:
    candidates, _legacy_map = resolve_issuer_identity_from_cursor(cur, issuer_id)
    return candidates


def resolve_issuer_id_candidates_batch(
    database_url: str, issuer_ids: tuple[str, ...] | list[str]
) -> dict[str, tuple[str, ...]]:
    by_input: dict[str, list[str]] = {c: [c] for item in issuer_ids if item and (c := item.strip())}
    if not by_input:
        return {}
    for row in _query_rows(database_url, _RESOLVE_BATCH_CANDIDATES_SQL, (list(by_input),)):
        if (target := by_input.get(str(row.get("raw_q") or ""))) is not None:
            _append_resolved_candidates(target, row)
    return {raw: tuple(vals) for raw, vals in by_input.items()}


class PostgresIssuerModuleReader:
    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url
        self._candidates_cache: dict[str, list[str]] = {}

    def seed_candidates(self, issuer_id: str, candidates: list[str] | tuple[str, ...]) -> None:
        clean = issuer_id.strip() or issuer_id
        self._candidates_cache[clean] = list(candidates)

    def _candidates(self, issuer_id: str) -> list[str]:
        clean = issuer_id.strip() or issuer_id
        if clean not in self._candidates_cache:
            resolved = resolve_issuer_id_candidates_batch(self._database_url, [clean]).get(clean)
            self._candidates_cache[clean] = list(resolved) if resolved else [clean]
        return self._candidates_cache[clean]

    def _one(self, sql: str, issuer_id: str) -> dict[str, Any] | None:
        rows = _query_rows(self._database_url, sql, (self._candidates(issuer_id),))
        return None if not rows else {**rows[0], "reason_codes": tuple(str(c) for c in rows[0]["reason_codes"] or ())}

    def supply_chain_exposure(self, issuer_id: str) -> SupplyChainExposure | None:
        return (
            None
            if (r := self._one(_SUPPLY_CHAIN_BY_IDS_SQL, issuer_id)) is None
            else SupplyChainExposure.model_validate(r)
        )

    def analyst_ratings(self, issuer_id: str) -> AnalystRatings | None:
        return (
            None
            if (r := self._one(_ANALYST_RATINGS_BY_IDS_SQL, issuer_id)) is None
            else AnalystRatings.model_validate(r)
        )
