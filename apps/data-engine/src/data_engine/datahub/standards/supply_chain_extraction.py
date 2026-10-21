"""10-K Supply-chain relationship extraction and materialization (#772, init.md §0 q3).

Extracts customer/supplier relationships from full 10-K filings via the gated LLM provider
(`glm-5.3-flash`, 1M context window), resolves/mints canonical entity UUIDs in the SSOT
entity store (`staging.entities` / `staging.entity_aliases` / `staging.entity_relations`),
and materializes exposure metrics into `mart.issuer_supply_chain_exposure`.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from factors.base.supply_chain_exposure import (
    SupplyChainExposure,
    SupplyChainPartner,
    supply_chain_exposure,
)
from factors.confidence import evaluate_continuous_confidence
from factors.shared.entity_resolution import add_edge, ensure_entity
from psycopg import Connection
from truealpha_contracts.confidence import (
    ContinuousConfidenceInput,
    ContinuousConfidencePolicy,
    SourceConfidenceEvidence,
)

from data_engine.datahub.canonical_issuer import CanonicalIssuer, require_canonical_issuer_id
from data_engine.datahub.production_topt.status_dimensions import statuses_without_raw_pointer
from data_engine.datahub.resolve_coordinates import is_uuid, lookup_minted_entity, ontology_kind_namespace
from data_engine.sources import llm

logger = logging.getLogger(__name__)

_CONFIDENCE_POLICY = ContinuousConfidencePolicy()

__all__ = (
    "SupplyChainEdgeCandidate",
    "SupplyChainExposure",
    "SupplyChainPartner",
    "SupplyChainRunReport",
    "extract_supply_chain_relationships",
    "materialize_supply_chain_exposure",
    "materialize_universe_supply_chain_exposure",
    "persist_extracted_supply_chain_edges",
    "supply_chain_exposure",
)

_INSERT_SQL = """
insert into mart.issuer_supply_chain_exposure (
    run_id,
    issuer_id,
    cutoff,
    exposure_score,
    direct_partners,
    suppliers_count,
    customers_count,
    max_partner_share,
    confidence,
    reason_codes,
    extractor,
    availability_status,
    source_evidence_status,
    factor_validation_status
) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
on conflict (run_id, issuer_id) do update set
    cutoff = excluded.cutoff,
    exposure_score = excluded.exposure_score,
    direct_partners = excluded.direct_partners,
    suppliers_count = excluded.suppliers_count,
    customers_count = excluded.customers_count,
    max_partner_share = excluded.max_partner_share,
    confidence = excluded.confidence,
    reason_codes = excluded.reason_codes,
    extractor = excluded.extractor,
    availability_status = excluded.availability_status,
    source_evidence_status = excluded.source_evidence_status,
    factor_validation_status = excluded.factor_validation_status
"""


class SupplyChainRunReport(int):
    """Row count returned by `materialize_universe_supply_chain_exposure`, carrying LLM telemetry.

    Subclasses `int` so callers expecting an integer count (`joined == 1`, `json.dumps({"rows": joined})`)
    work unchanged while `run_supply_chain_exposure` inspects `model_extracted`, `replayed`, and
    `context_window_exceeded` for alerting and daily report accounting.
    """

    rows: int
    edges_inserted: int
    model_extracted: int
    replayed: int
    context_window_exceeded: int
    model_errors: int
    exceeded_filings: tuple[str, ...]

    def __new__(
        cls,
        rows: int,
        *,
        edges_inserted: int = 0,
        model_extracted: int = 0,
        replayed: int = 0,
        context_window_exceeded: int = 0,
        model_errors: int = 0,
        exceeded_filings: Sequence[str] = (),
    ) -> SupplyChainRunReport:
        obj = super().__new__(cls, rows)
        obj.rows = int(rows)
        obj.edges_inserted = int(edges_inserted)
        obj.model_extracted = int(model_extracted)
        obj.replayed = int(replayed)
        obj.context_window_exceeded = int(context_window_exceeded)
        obj.model_errors = int(model_errors)
        obj.exceeded_filings = tuple(exceeded_filings)
        return obj


@dataclass(frozen=True)
class SupplyChainEdgeCandidate:
    source_entity_id: str
    target_entity_name: str
    relation_type: str  # "supplier" or "customer"
    evidence_sentence: str
    confidence: Decimal
    target_ticker: str | None = None
    revenue_share_pct: Decimal | None = None
    extractor: str = "graph:kg-edges:v1"
    replayed: bool = False


_CASE_ID_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._:/@+-]+")


def _safe_case_id_part(value: str, fallback: str) -> str:
    return _CASE_ID_SANITIZE_RE.sub("-", value).strip("-") or fallback


def _edge_confidence(issuer_id: str, accession: str, target_name: str, rel: str, quote_len: int) -> Decimal:
    sample_conf = Decimal("0.98") if quote_len >= 40 else Decimal("0.92")
    safe_issuer = _safe_case_id_part(issuer_id, "issuer")
    safe_acc = _safe_case_id_part(accession, "acc")
    safe_target = _safe_case_id_part(target_name, "partner")
    return evaluate_continuous_confidence(
        _CONFIDENCE_POLICY,
        ContinuousConfidenceInput(
            case_id=f"supply-chain:{safe_issuer}:{safe_acc}:{safe_target}",
            sources=(
                SourceConfidenceEvidence(
                    provider_id="origin:sec-10k:v1",
                    origin_group_id="lineage:sec-10k",
                    independence_weight=Decimal("1"),
                    successful_outcome_mass=Decimal("70"),
                    failed_outcome_mass=Decimal("0"),
                    freshness=Decimal("1"),
                    sample_conformance=sample_conf,
                    transport_integrity=Decimal("1"),
                    evidence_ids=(f"accession:{safe_acc}",),
                    reason_codes=(f"relation:{rel}",),
                ),
            ),
            agreement=Decimal("1"),
            semantic_mapping_quality=Decimal("0.95"),
            lineage_completeness=Decimal("1"),
            required_component_completeness=Decimal("1"),
        ),
    ).confidence


def _parse_cik_from_identifier(raw: str) -> int:
    digits = re.sub(r"\D+", "", raw)
    return int(digits) if digits else 0


def extract_supply_chain_relationships(
    filing_text: str,
    *,
    issuer_id: str,
    filing_date: date,
    accession: str,
    cik: int | None = None,
    issuer_label: str | None = None,
    connection: Connection[Any] | None = None,
    llm_gateway: Any | None = None,
    persist: bool = True,
) -> tuple[SupplyChainEdgeCandidate, ...]:
    """Extract supply-chain relationships from 10-K text using the gated 1M-context LLM task.

    Raises `llm.ModelContextLimitError` when the filing exceeds the 1,000,000-token ceiling so
    the caller records `context_window_exceeded` and alerts instead of silently returning `()`.
    """
    _ = filing_date
    if not filing_text or len(filing_text.strip()) < 40:
        return ()
    resolved_cik = cik if cik is not None else _parse_cik_from_identifier(issuer_id or accession)
    label = issuer_label or issuer_id
    gateway_mod = llm_gateway or llm
    extraction = gateway_mod.identify_supply_chain_partners(
        connection,
        cik=resolved_cik,
        accession=accession,
        issuer_label=label,
        filing_text=filing_text,
        persist=persist and connection is not None,
    )
    results: list[SupplyChainEdgeCandidate] = []
    seen_pairs: set[tuple[str, str]] = set()
    for partner in extraction.partners:
        pair = (partner.relation_type, partner.partner_name.lower())
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        results.append(
            SupplyChainEdgeCandidate(
                source_entity_id=issuer_id,
                target_entity_name=partner.partner_name,
                relation_type=partner.relation_type,
                evidence_sentence=partner.evidence_quote[:240],
                confidence=_edge_confidence(
                    issuer_id,
                    accession,
                    partner.partner_name,
                    partner.relation_type,
                    len(partner.evidence_quote),
                ),
                target_ticker=partner.ticker,
                revenue_share_pct=partner.revenue_share_pct,
                extractor=extraction.extractor,
                replayed=extraction.invocation.replayed,
            )
        )
    return tuple(results)


def _slug_entity_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"company:partner:{slug or 'unknown'}"


def _deterministic_partner_uuid(legacy_partner_id: str) -> str:
    """Deterministic UUIDv5 under the ontology's `kind:issuer` namespace for environments without entity store."""
    return str(uuid.uuid5(ontology_kind_namespace("issuer"), f"legacy-id:{legacy_partner_id}"))


@contextlib.contextmanager
def _optional_savepoint(connection: Connection[Any]) -> Any:
    """Enter a PostgreSQL savepoint (`with connection.transaction():`) when supported so a
    caught database error never leaves the outer transaction in `InFailedSqlTransaction`."""
    tx_fn = getattr(connection, "transaction", None)
    if callable(tx_fn):
        with tx_fn():
            yield
    else:
        yield


def _resolve_or_mint_partner_entity(
    connection: Connection[Any],
    *,
    partner_name: str,
    partner_ticker: str | None,
    tx_time: datetime,
    raw_ref: str,
) -> str:
    """Resolve an extracted partner organization against `staging.entity_aliases` or mint a canonical UUID."""
    legacy_partner_id = _slug_entity_id(partner_name)
    clean_name = partner_name.strip()
    try:
        with _optional_savepoint(connection):
            # 1. Exact case-insensitive match on existing `name` alias (PIT at tx_time)
            # or unique `legacy-id` alias (valid across all time once minted).
            row = connection.execute(
                """
                select staging.entity_survivor(a.entity_id, %s)::text
                from staging.entity_aliases a
                join staging.entities e on e.entity_id = a.entity_id
                where e.kind in ('issuer', 'fund')
                  and (
                      (a.scheme = 'legacy-id' and a.value = %s)
                      or (a.scheme = 'name' and lower(a.value) = lower(%s) and a.transaction_time <= %s)
                  )
                order by (a.scheme = 'legacy-id') desc, a.confidence desc, a.transaction_time desc
                limit 1
                """,
                (tx_time, legacy_partner_id, clean_name, tx_time),
            ).fetchone()
            if row and row[0]:
                return str(row[0])

            # 1b. Check immutable birth registry in `staging.entities` so out-of-order filing
            # dates never re-mint generation 2 of an already-minted `legacy-id`.
            birth_uuid = lookup_minted_entity(connection, "legacy-id", legacy_partner_id, known_at=tx_time)
            if birth_uuid is not None:
                return str(birth_uuid)

            # 2. Match by ticker via `issues` -> `listed_as` -> `mic-ticker` if ticker was extracted
            if partner_ticker:
                ticker_row = connection.execute(
                    """
                    select staging.entity_survivor(r1.from_entity_id, %s)::text
                    from staging.entity_aliases a
                    join staging.entity_relations r2
                      on r2.to_entity_id = a.entity_id and r2.relation_type = 'listed_as'
                    join staging.entity_relations r1
                      on r1.to_entity_id = r2.from_entity_id and r1.relation_type = 'issues'
                    where a.scheme = 'mic-ticker'
                      and split_part(a.value, ':', 2) = upper(%s)
                      and a.transaction_time <= %s
                    order by a.confidence desc, a.transaction_time desc
                    limit 1
                    """,
                    (tx_time, partner_ticker.strip(), tx_time),
                ).fetchone()
                if ticker_row and ticker_row[0]:
                    return str(ticker_row[0])

            # 3. Mint canonical UUID in `staging.entities` and register `legacy-id` + `name` aliases
            minted_row = connection.execute(
                "select staging.entity_mint('issuer', 'legacy-id', %s, 'datahub.supply_chain')::text",
                (legacy_partner_id,),
            ).fetchone()
            if minted_row and minted_row[0]:
                minted_uuid = str(minted_row[0])
                connection.execute(
                    """
                    insert into staging.entity_aliases (
                        entity_id, scheme, value, valid_from, valid_to, transaction_time, source,
                        raw_ref, method, confidence, mapping_version
                    ) values
                        (%s::uuid, 'legacy-id', %s, '-infinity'::date, null, least(%s::timestamptz, clock_timestamp()), 'datahub.supply_chain', %s, 'asserted', 0.9, 'supply-chain:v1'),
                        (%s::uuid, 'name', %s, '-infinity'::date, null, least(%s::timestamptz, clock_timestamp()), 'datahub.supply_chain', %s, 'asserted', 0.9, 'supply-chain:v1')
                    on conflict do nothing
                    """,
                    (minted_uuid, legacy_partner_id, tx_time, raw_ref, minted_uuid, clean_name, tx_time, raw_ref),
                )
                return minted_uuid
    except Exception:  # noqa: BLE001
        pass
    return _deterministic_partner_uuid(legacy_partner_id)


def _record_entity_relation_supplies_to(
    connection: Connection[Any],
    *,
    from_uuid: str,
    to_uuid: str,
    valid_from: date,
    tx_time: datetime,
    confidence: Decimal,
    raw_ref: str,
    evidence_sentence: str,
    revenue_share_pct: Decimal | None,
) -> None:
    """Write `supplies_to` edge into `staging.entity_relations` when both endpoints are canonical UUIDs."""
    if not (is_uuid(from_uuid) and is_uuid(to_uuid)) or from_uuid == to_uuid:
        return
    try:
        with _optional_savepoint(connection):
            attributes = {"revenue_share_pct": str(revenue_share_pct)} if revenue_share_pct is not None else {}
            connection.execute(
                """
                with t as (
                    select least(%s::timestamptz, clock_timestamp()) as eff_tx
                ),
                r as (
                    select staging.entity_relation_uuid(
                        'supplies_to', %s::uuid, %s::uuid, %s::date, t.eff_tx, 'sec_10k_supply_chain', 'asserted'
                    ) as rel_id,
                    t.eff_tx
                    from t
                )
                insert into staging.entity_relations (
                    relation_id, relation_type, from_entity_id, to_entity_id, valid_from,
                    transaction_time, source, raw_ref, method, confidence, evidence, attributes, mapping_version
                )
                select
                    r.rel_id, 'supplies_to', %s::uuid, %s::uuid, %s::date,
                    r.eff_tx, 'sec_10k_supply_chain', %s, 'asserted', %s, %s::jsonb, %s::jsonb, 'supply-chain:v1'
                from r
                where exists (select 1 from staging.entities where entity_id = %s::uuid)
                  and exists (select 1 from staging.entities where entity_id = %s::uuid)
                on conflict do nothing
                """,
                (
                    tx_time,
                    from_uuid,
                    to_uuid,
                    valid_from,
                    from_uuid,
                    to_uuid,
                    valid_from,
                    raw_ref,
                    confidence,
                    json.dumps({"quote": evidence_sentence}),
                    json.dumps(attributes),
                    from_uuid,
                    to_uuid,
                ),
            )
    except Exception:  # noqa: BLE001
        pass


def _populate_alias_ciks(
    connection: Connection[Any],
    uuid_to_issuer: dict[str, CanonicalIssuer],
    by_cik: dict[str, CanonicalIssuer],
) -> None:
    if not uuid_to_issuer:
        return
    try:
        with _optional_savepoint(connection):
            alias_rows = connection.execute(
                """
                select entity_id::text, value
                from staging.entity_aliases
                where entity_id = any(%s::uuid[])
                  and scheme = 'cik'
                """,
                (sorted(uuid_to_issuer),),
            ).fetchall()
            for entity_id, cik_val in alias_rows:
                matched_iss = uuid_to_issuer.get(str(entity_id).lower())
                if matched_iss is not None and cik_val:
                    cik_digits = str(cik_val).lstrip("0") or "0"
                    by_cik.setdefault(cik_digits, matched_iss)
    except Exception:  # noqa: BLE001
        pass


def _populate_governed_member_ciks(
    connection: Connection[Any],
    run_id: str,
    by_issuer_id: dict[str, CanonicalIssuer],
    uuid_to_issuer: dict[str, CanonicalIssuer],
    by_cik: dict[str, CanonicalIssuer],
) -> None:
    try:
        with _optional_savepoint(connection):
            from data_engine.datahub.production_topt.theme_purity import governed_members

            for cik_int, member_issuer_id in governed_members(connection, run_id=run_id).items():
                matched_iss = by_issuer_id.get(member_issuer_id) or uuid_to_issuer.get(member_issuer_id.lower())
                if matched_iss is not None:
                    by_cik.setdefault(str(cik_int), matched_iss)
    except Exception:  # noqa: BLE001
        pass


def _build_issuer_lookup(
    issuers: Sequence[CanonicalIssuer],
    connection: Connection[Any] | None = None,
    *,
    run_id: str | None = None,
) -> tuple[dict[str, CanonicalIssuer], dict[str, CanonicalIssuer], dict[str, CanonicalIssuer]]:
    by_cik: dict[str, CanonicalIssuer] = {}
    by_legacy: dict[str, CanonicalIssuer] = {}
    by_issuer_id: dict[str, CanonicalIssuer] = {}
    uuid_to_issuer: dict[str, CanonicalIssuer] = {}
    for iss in issuers:
        by_legacy[iss.legacy_id] = iss
        by_issuer_id[iss.issuer_id] = iss
        for prefix in ("company:cik:", "issuer:cik:"):
            if iss.legacy_id.startswith(prefix):
                cik_raw = iss.legacy_id.removeprefix(prefix).lstrip("0") or "0"
                by_cik[cik_raw] = iss
        if is_uuid(iss.issuer_id):
            uuid_to_issuer[iss.issuer_id.lower()] = iss

    if connection is not None:
        _populate_alias_ciks(connection, uuid_to_issuer, by_cik)
        if run_id:
            _populate_governed_member_ciks(connection, run_id, by_issuer_id, uuid_to_issuer, by_cik)
    return by_cik, by_legacy, by_issuer_id


def _match_filing_issuer(
    source_record_id: object,
    meta: dict[str, Any],
    by_cik: dict[str, CanonicalIssuer],
    by_legacy: dict[str, CanonicalIssuer],
    by_issuer_id: dict[str, CanonicalIssuer],
) -> CanonicalIssuer | None:
    issuer_hint = str(meta.get("issuer_id") or meta.get("legacy_id") or "")
    if issuer_hint in by_legacy:
        return by_legacy[issuer_hint]
    if issuer_hint in by_issuer_id:
        return by_issuer_id[issuer_hint]
    if isinstance(source_record_id, str) and source_record_id.startswith("filing-document:CIK"):
        parts = source_record_id.split(":")
        if len(parts) >= 2:
            cik_digits = parts[1].removeprefix("CIK").lstrip("0") or "0"
            return by_cik.get(cik_digits)
    return None


def _extract_cik_int(source_record_id: object, meta: dict[str, Any], matched: CanonicalIssuer) -> int:
    raw_cik = meta.get("cik")
    if isinstance(raw_cik, int):
        return raw_cik
    if isinstance(raw_cik, str) and raw_cik.strip().isdigit():
        return int(raw_cik.strip())
    if isinstance(source_record_id, str) and source_record_id.startswith("filing-document:CIK"):
        parts = source_record_id.split(":")
        if len(parts) >= 2:
            digits = parts[1].removeprefix("CIK").lstrip("0") or "0"
            if digits.isdigit():
                return int(digits)
    return _parse_cik_from_identifier(matched.legacy_id)


def _resolve_filing_text(
    connection: Connection[Any],
    fetch_id: object,
    meta: dict[str, Any],
    object_uri: object,
    object_store: Any | None,
) -> str | None:
    plain = meta.get("plain_text")
    if isinstance(plain, str) and plain:
        return plain
    if isinstance(object_uri, str) and object_uri:
        try:
            with _optional_savepoint(connection):
                from data_engine.datahub.standards.filing_extraction import filing_plain_text
                from data_engine.raw_store import get_payload

                if object_store is not None and hasattr(object_store, "get_bytes"):
                    raw_bytes = object_store.get_bytes(object_uri)
                elif isinstance(fetch_id, int):
                    raw_bytes = get_payload(connection, fetch_id, store=object_store)
                else:
                    return None
                return str(filing_plain_text(raw_bytes))
        except Exception:  # noqa: BLE001
            return None
    return None


def _has_table(connection: Connection[Any], qualified_name: str) -> bool:
    try:
        with _optional_savepoint(connection), connection.cursor() as cur:
            cur.execute(f"select to_regclass('{qualified_name}')")
            res = cur.fetchone()
            return bool(res and res[0])
    except Exception:  # noqa: BLE001
        return False


@dataclass
class _ExtractionDiagnostics:
    edges_inserted: int = 0
    model_extracted: int = 0
    replayed: int = 0
    context_window_exceeded: int = 0
    model_errors: int = 0
    exceeded_issuers: dict[str, str] = None  # type: ignore[assignment]
    issuer_extractors: dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.exceeded_issuers is None:
            self.exceeded_issuers = {}
        if self.issuer_extractors is None:
            self.issuer_extractors = {}


def _persist_single_candidate_edge(
    connection: Connection[Any],
    *,
    matched: CanonicalIssuer,
    cand: SupplyChainEdgeCandidate,
    filing_dt: date,
    tx_time: datetime,
    raw_ref: str,
) -> bool:
    canonical_issuer_id = matched.issuer_id
    partner_uuid = _resolve_or_mint_partner_entity(
        connection,
        partner_name=cand.target_entity_name,
        partner_ticker=cand.target_ticker,
        tx_time=tx_time,
        raw_ref=raw_ref,
    )
    if partner_uuid == canonical_issuer_id:
        return False
    from_id = partner_uuid if cand.relation_type == "supplier" else canonical_issuer_id
    to_id = canonical_issuer_id if cand.relation_type == "supplier" else partner_uuid
    _record_entity_relation_supplies_to(
        connection,
        from_uuid=from_id,
        to_uuid=to_id,
        valid_from=filing_dt,
        tx_time=tx_time,
        confidence=cand.confidence,
        raw_ref=raw_ref,
        evidence_sentence=cand.evidence_sentence,
        revenue_share_pct=cand.revenue_share_pct,
    )
    try:
        with _optional_savepoint(connection):
            ensure_entity(connection, canonical_issuer_id, "company", matched.ticker)
            ensure_entity(connection, partner_uuid, "company", cand.target_entity_name)
            return bool(
                add_edge(
                    connection,
                    from_id=from_id,
                    to_id=to_id,
                    relation_type="supplies_to",
                    confidence=float(cand.confidence),
                    source="sec_10k_supply_chain",
                    transaction_time=tx_time,
                    valid_from=filing_dt.isoformat(),
                    raw_ref=raw_ref,
                )
            )
    except Exception:  # noqa: BLE001
        return False


def _extract_filing_for_issuer(
    connection: Connection[Any],
    *,
    matched: CanonicalIssuer,
    filing_text: str,
    filing_dt: date,
    accession: str,
    cik_int: int,
    llm_gateway: Any | None,
    diag: _ExtractionDiagnostics,
) -> tuple[SupplyChainEdgeCandidate, ...] | None:
    canonical_issuer_id = matched.issuer_id
    try:
        candidates = extract_supply_chain_relationships(
            filing_text,
            issuer_id=canonical_issuer_id,
            filing_date=filing_dt,
            accession=accession,
            cik=cik_int,
            issuer_label=matched.ticker or canonical_issuer_id,
            connection=connection,
            llm_gateway=llm_gateway,
            persist=True,
        )
    except llm.ModelContextLimitError as exc:
        diag.context_window_exceeded += 1
        diag.exceeded_issuers[canonical_issuer_id] = f"{matched.ticker or canonical_issuer_id}:{accession}"
        logger.error(
            "Supply chain 10-K exceeded 1M context window for %s (CIK %s, accession %s): %s",
            matched.ticker,
            cik_int,
            accession,
            exc,
        )
        return None
    except (llm.ModelNotConfigured, llm.ModelHTTPError) as exc:
        diag.model_errors += 1
        logger.warning(
            "Supply chain LLM extraction unavailable for %s (CIK %s, accession %s): %s",
            matched.ticker,
            cik_int,
            accession,
            exc,
        )
        return None
    if candidates:
        diag.issuer_extractors[canonical_issuer_id] = candidates[0].extractor
        if candidates[0].replayed:
            diag.replayed += 1
        else:
            diag.model_extracted += 1
    else:
        diag.model_extracted += 1
    return candidates


def persist_extracted_supply_chain_edges(
    connection: Connection[Any],
    *,
    issuers: Sequence[CanonicalIssuer],
    cutoff: datetime,
    run_id: str | None = None,
    object_store: Any | None = None,
    llm_gateway: Any | None = None,
    _diagnostics: _ExtractionDiagnostics | None = None,
) -> int:
    """Extract supply-chain edges from captured SEC 10-K filings at or before `cutoff` via
    the 1M-context LLM task and persist them into `staging.entity_relations` and `staging.kg_edges`
    keyed by Canonical UUID (`issuer.issuer_id`).
    """
    if not _has_table(connection, "raw.fetches"):
        return 0
    diag = _diagnostics if _diagnostics is not None else _ExtractionDiagnostics()
    by_cik, by_legacy, by_issuer_id = _build_issuer_lookup(issuers, connection, run_id=run_id)
    try:
        with _optional_savepoint(connection):
            rows = connection.execute(
                """
                select distinct on (split_part(source_record_id, ':', 2))
                    id, source_record_id, object_uri, source_published_at, metadata
                from raw.fetches
                where source = 'sec'
                  and source_record_id like 'filing-document:CIK%%'
                  and source_published_at <= %s
                order by split_part(source_record_id, ':', 2), source_published_at desc, id desc
                """,
                (cutoff,),
            ).fetchall()
    except Exception:  # noqa: BLE001
        return 0

    inserted = 0
    for row in rows:
        if not isinstance(row, tuple | list) or len(row) < 5:
            continue
        fetch_id, source_record_id, object_uri, published_at, metadata = row[:5]
        meta = metadata if isinstance(metadata, dict) else {}
        matched = _match_filing_issuer(source_record_id, meta, by_cik, by_legacy, by_issuer_id)
        filing_text = (
            _resolve_filing_text(connection, fetch_id, meta, object_uri, object_store) if matched is not None else None
        )
        if matched is None or not filing_text:
            continue
        tx_time = published_at if isinstance(published_at, datetime) else cutoff
        filing_dt = tx_time.date() if hasattr(tx_time, "date") else cutoff.date()
        candidates = _extract_filing_for_issuer(
            connection,
            matched=matched,
            filing_text=filing_text,
            filing_dt=filing_dt,
            accession=str(meta.get("accession") or source_record_id),
            cik_int=_extract_cik_int(source_record_id, meta, matched),
            llm_gateway=llm_gateway,
            diag=diag,
        )
        if candidates is None:
            continue
        raw_ref = f"raw.fetches:{fetch_id}"
        for cand in candidates:
            if _persist_single_candidate_edge(
                connection,
                matched=matched,
                cand=cand,
                filing_dt=filing_dt,
                tx_time=tx_time,
                raw_ref=raw_ref,
            ):
                inserted += 1
    diag.edges_inserted = inserted
    return inserted


def materialize_supply_chain_exposure(
    connection: Connection[Any],
    *,
    run_id: str,
    cutoff: datetime | None = None,
    exposure_data: Sequence[dict[str, Any] | SupplyChainExposure],
) -> int:
    """Materialize batch supply chain exposure rows into mart.issuer_supply_chain_exposure.

    No Q3 row names a raw pointer yet (#1116). Every row gets its evidence and validation statuses
    from the status source. A status in an input item has no effect.
    """
    as_of = cutoff or datetime.now(tz=UTC)
    source_evidence, factor_validation = statuses_without_raw_pointer()
    source_status, val_status = source_evidence.value, factor_validation.value
    count = 0
    for item in exposure_data:
        if isinstance(item, SupplyChainExposure):
            issuer_id = item.entity_id
            exposure_score = item.exposure_score
            direct_partners = item.direct_partners
            suppliers_count = item.suppliers_count
            customers_count = item.customers_count
            max_partner_share = item.max_partner_share
            confidence = item.result.confidence
            reason_codes = list(item.result.flags)
            extractor = "graph:kg-edges:v1"
            avail = (
                "available"
                if item.result.data_availability == "verified" and exposure_score is not None
                else "unavailable"
            )
        else:
            issuer_id = str(item["issuer_id"])
            if "partners" in item:
                rec = supply_chain_exposure(
                    item["partners"],
                    entity_id=issuer_id,
                    as_of=as_of,
                    supplies_to_edges_exist=bool(item["supplies_to_edges_exist"]),
                )
                exposure_score = rec.exposure_score
                direct_partners = rec.direct_partners
                suppliers_count = rec.suppliers_count
                customers_count = rec.customers_count
                max_partner_share = rec.max_partner_share
                confidence = rec.result.confidence
                reason_codes = list(rec.result.flags)
                avail = (
                    "available"
                    if rec.result.data_availability == "verified" and exposure_score is not None
                    else "unavailable"
                )
            else:
                raw_exp = item.get("exposure_score")
                exposure_score = Decimal(str(raw_exp)) if raw_exp is not None else None
                direct_partners = int(item.get("direct_partners", 0))
                suppliers_count = int(item.get("suppliers_count", direct_partners))
                customers_count = int(item.get("customers_count", 0))
                raw_share = item.get("max_partner_share", exposure_score or Decimal("0"))
                max_partner_share = Decimal(str(raw_share)) if raw_share is not None else Decimal("0")
                confidence = Decimal(str(item.get("confidence", "0.8" if exposure_score is not None else "0")))
                reason_codes = list(item.get("reason_codes", []))
                avail = str(
                    item.get("availability_status", "available" if exposure_score is not None else "unavailable")
                )
            extractor = str(item.get("extractor", "graph:kg-edges:v1"))

        connection.execute(
            _INSERT_SQL,
            (
                run_id,
                require_canonical_issuer_id(issuer_id),
                as_of,
                exposure_score,
                direct_partners,
                suppliers_count,
                customers_count,
                max_partner_share,
                confidence,
                reason_codes,
                extractor,
                avail,
                source_status,
                val_status,
            ),
        )
        count += 1
    return count


def _load_issuer_graph_partners(
    connection: Connection[Any],
    *,
    issuer: CanonicalIssuer,
    cutoff: datetime,
    cutoff_date: Any,
) -> list[SupplyChainPartner]:
    partners: list[SupplyChainPartner] = []
    seen_edges: set[tuple[str, str]] = set()
    for endpoint_id in dict.fromkeys((issuer.issuer_id, issuer.legacy_id)):
        if not endpoint_id:
            continue
        rows = connection.execute(
            """
            select e.to_id, coalesce(ent.display_name, e.to_id), e.confidence
            from staging.kg_edges e
            left join staging.kg_entities ent on ent.id = e.to_id
            where e.from_id = %s and e.relation_type = 'supplies_to' and e.transaction_time <= %s
              and (e.valid_time is null or e.valid_time @> %s::date)
            """,
            (endpoint_id, cutoff, cutoff_date),
        ).fetchall()
        for p_id, p_name, conf in rows:
            key = ("customer", str(p_id))
            if key not in seen_edges:
                seen_edges.add(key)
                partners.append(
                    SupplyChainPartner(
                        partner_id=str(p_id),
                        partner_name=str(p_name),
                        relation_type="customer",
                        revenue_share=None,
                        confidence=Decimal(str(conf)) if conf is not None else Decimal("0"),
                    )
                )
        supplier_rows = connection.execute(
            """
            select e.from_id, coalesce(ent.display_name, e.from_id), e.confidence
            from staging.kg_edges e
            left join staging.kg_entities ent on ent.id = e.from_id
            where e.to_id = %s and e.relation_type = 'supplies_to' and e.transaction_time <= %s
              and (e.valid_time is null or e.valid_time @> %s::date)
            """,
            (endpoint_id, cutoff, cutoff_date),
        ).fetchall()
        for p_id, p_name, conf in supplier_rows:
            key = ("supplier", str(p_id))
            if key not in seen_edges:
                seen_edges.add(key)
                partners.append(
                    SupplyChainPartner(
                        partner_id=str(p_id),
                        partner_name=str(p_name),
                        relation_type="supplier",
                        revenue_share=None,
                        confidence=Decimal(str(conf)) if conf is not None else Decimal("0"),
                    )
                )
    return partners


def materialize_universe_supply_chain_exposure(
    connection: Connection[Any],
    *,
    run_id: str,
    cutoff: datetime,
    issuers: Sequence[CanonicalIssuer],
    object_store: Any | None = None,
    llm_gateway: Any | None = None,
) -> SupplyChainRunReport:
    """Extract and materialize supply chain exposure for all issuers in a universe run.

    Keys graph reads on both `issuer.issuer_id` (Canonical UUID) and `issuer.legacy_id` so
    canonical edges and pre-migration edges both resolve. When an issuer's 10-K filing exceeds
    the 1M-token LLM context ceiling, writes `availability_status = 'unavailable'` with
    `reason_codes = ['context_window_exceeded']` instead of `no_disclosed_suppliers`.
    """
    count = 0
    has_edges_table = _has_table(connection, "staging.kg_edges")
    diag = _ExtractionDiagnostics()
    if has_edges_table:
        persist_extracted_supply_chain_edges(
            connection,
            issuers=issuers,
            cutoff=cutoff,
            run_id=run_id,
            object_store=object_store,
            llm_gateway=llm_gateway,
            _diagnostics=diag,
        )

    cutoff_date = cutoff.date() if hasattr(cutoff, "date") else cutoff
    supplies_to_edges_exist = has_edges_table and _supplies_to_edges_exist(connection, cutoff)
    for issuer in issuers:
        if issuer.issuer_id in diag.exceeded_issuers:
            count += materialize_supply_chain_exposure(
                connection,
                run_id=run_id,
                cutoff=cutoff,
                exposure_data=[
                    {
                        "issuer_id": issuer.issuer_id,
                        "exposure_score": None,
                        "direct_partners": 0,
                        "suppliers_count": 0,
                        "customers_count": 0,
                        "max_partner_share": None,
                        "confidence": Decimal("0"),
                        "reason_codes": ["context_window_exceeded"],
                        "extractor": f"model:{llm.SUPPLY_CHAIN_PROMPT_VERSION}:context_exceeded",
                        "availability_status": "unavailable",
                    }
                ],
            )
            continue

        partners = (
            _load_issuer_graph_partners(connection, issuer=issuer, cutoff=cutoff, cutoff_date=cutoff_date)
            if has_edges_table
            else []
        )
        record = supply_chain_exposure(
            partners, entity_id=issuer.issuer_id, as_of=cutoff, supplies_to_edges_exist=supplies_to_edges_exist
        )
        extractor = diag.issuer_extractors.get(issuer.issuer_id, "graph:kg-edges:v1")
        if extractor != "graph:kg-edges:v1":
            avail = (
                "available"
                if record.result.data_availability == "verified" and record.exposure_score is not None
                else "unavailable"
            )
            count += materialize_supply_chain_exposure(
                connection,
                run_id=run_id,
                cutoff=cutoff,
                exposure_data=[
                    {
                        "issuer_id": record.entity_id,
                        "exposure_score": record.exposure_score,
                        "direct_partners": record.direct_partners,
                        "suppliers_count": record.suppliers_count,
                        "customers_count": record.customers_count,
                        "max_partner_share": record.max_partner_share,
                        "confidence": record.result.confidence,
                        "reason_codes": list(record.result.flags),
                        "extractor": extractor,
                        "availability_status": avail,
                    }
                ],
            )
        else:
            count += materialize_supply_chain_exposure(
                connection,
                run_id=run_id,
                cutoff=cutoff,
                exposure_data=[record],
            )
    return SupplyChainRunReport(
        count,
        edges_inserted=diag.edges_inserted,
        model_extracted=diag.model_extracted,
        replayed=diag.replayed,
        context_window_exceeded=diag.context_window_exceeded,
        model_errors=diag.model_errors,
        exceeded_filings=tuple(diag.exceeded_issuers.values()),
    )


def _supplies_to_edges_exist(connection: Connection[Any], cutoff: datetime) -> bool:
    """True when the graph holds a `supplies_to` edge for ANY issuer that was knowable at the cutoff."""
    row = connection.execute(
        "select exists (select 1 from staging.kg_edges where relation_type = 'supplies_to' and transaction_time <= %s)",
        (cutoff,),
    ).fetchone()
    return bool(row and row[0])
