"""10-K supply-chain relationship extraction and materialization (#772, init.md §0 q3)."""

from __future__ import annotations

import contextlib
import json
import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from factors.base.supply_chain_exposure import (
    SupplyChainExposure,
    SupplyChainPartner,
    supply_chain_exposure,
)
from factors.confidence import evaluate_cell_confidence
from factors.shared.entity_resolution import add_edge, ensure_entity
from psycopg import Connection
from truealpha_contracts.confidence import (
    ContinuousConfidencePolicy,
    SourceConfidenceEvidence,
)

from data_engine.datahub.canonical_issuer import CanonicalIssuer, require_canonical_issuer_id
from data_engine.datahub.production_topt.source_registrations import source_reliability_prior
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
    run_id, issuer_id, cutoff, exposure_score, direct_partners, suppliers_count,
    customers_count, max_partner_share, confidence, reason_codes, extractor,
    availability_status, source_evidence_status, factor_validation_status
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
    """Materialized row count with attached LLM extraction and replay telemetry."""

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
    relation_type: str
    evidence_sentence: str
    confidence: Decimal
    target_ticker: str | None = None
    revenue_share_pct: Decimal | None = None
    extractor: str = "graph:kg-edges:v1"
    replayed: bool = False


_CASE_ID_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._:/@+-]+")
_WS_RE = re.compile(r"\s+")


def _safe_case_id_part(value: str, fallback: str) -> str:
    return _CASE_ID_SANITIZE_RE.sub("-", value).strip("-") or fallback


def _quote_is_anchored(quote: str, filing_text: str) -> bool:
    norm_quote = _WS_RE.sub(" ", quote).strip().lower()
    return bool(norm_quote) and norm_quote in _WS_RE.sub(" ", filing_text).strip().lower()


def _filing_freshness(filing_date: date, cutoff: date | datetime | None) -> Decimal:
    ref_date = cutoff.date() if isinstance(cutoff, datetime) else cutoff
    if ref_date is None:
        return Decimal("1")
    age_days = max(0, (ref_date - filing_date).days)
    if age_days <= 365:
        return Decimal("1")
    return max(Decimal("0.20"), Decimal("1") - (Decimal(age_days - 365) * Decimal("0.001")).quantize(Decimal("0.0001")))


def _edge_confidence(
    issuer_id: str,
    accession: str,
    target_name: str,
    rel: str,
    filing_date: date,
    *,
    cutoff: date | datetime | None = None,
) -> Decimal:
    safe_issuer = _safe_case_id_part(issuer_id, "issuer")
    safe_acc = _safe_case_id_part(accession, "acc")
    safe_target = _safe_case_id_part(target_name, "partner")
    prior = source_reliability_prior("origin:llm-extractor:v1") or source_reliability_prior("origin:sec-10k:v1")
    return evaluate_cell_confidence(
        case_id=f"supply-chain:{safe_issuer}:{safe_acc}:{safe_target}",
        sources=(
            SourceConfidenceEvidence(
                provider_id="origin:llm-extractor:v1",
                origin_group_id="lineage:sec-10k",
                independence_weight=Decimal("1"),
                reliability_prior=prior,
                successful_outcome_mass=Decimal("0"),
                failed_outcome_mass=Decimal("0"),
                freshness=_filing_freshness(filing_date, cutoff),
                sample_conformance=Decimal("1"),
                transport_integrity=Decimal("1"),
                evidence_ids=(f"accession:{safe_acc}",),
                reason_codes=(f"relation:{rel}",),
            ),
        ),
        agreement=Decimal("1"),
        semantic_mapping_quality=Decimal("0.95"),
        lineage_completeness=Decimal("1"),
        required_component_completeness=Decimal("1"),
        policy=_CONFIDENCE_POLICY,
    ).confidence


def _parse_cik_from_identifier(raw: str) -> int:
    digits = re.sub(r"\D+", "", raw)
    return int(digits) if digits else 0


def _filing_doc_cik_digits(source_record_id: object) -> str | None:
    if isinstance(source_record_id, str) and source_record_id.startswith("filing-document:CIK"):
        parts = source_record_id.split(":")
        if len(parts) >= 2:
            return parts[1].removeprefix("CIK").lstrip("0") or "0"
    return None


def extract_supply_chain_relationships(
    filing_text: str,
    *,
    issuer_id: str,
    filing_date: date,
    accession: str,
    cik: int | None = None,
    issuer_label: str | None = None,
    cutoff: date | datetime | None = None,
    connection: Connection[Any] | None = None,
    llm_gateway: Any | None = None,
    persist: bool = True,
) -> tuple[SupplyChainEdgeCandidate, ...]:
    """Extract anchored supplier/customer edge candidates from 10-K text via the gated LLM task."""
    if not filing_text or len(filing_text.strip()) < 40:
        return ()
    resolved_cik = cik if cik is not None else _parse_cik_from_identifier(issuer_id or accession)
    gateway_mod = llm_gateway or llm
    extraction = gateway_mod.identify_supply_chain_partners(
        connection,
        cik=resolved_cik,
        accession=accession,
        issuer_label=issuer_label or issuer_id,
        filing_text=filing_text,
        persist=persist and connection is not None,
    )
    results: list[SupplyChainEdgeCandidate] = []
    seen_pairs: set[tuple[str, str]] = set()
    for partner in extraction.partners:
        if not _quote_is_anchored(partner.evidence_quote, filing_text):
            continue
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
                    filing_date,
                    cutoff=cutoff,
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
    return str(uuid.uuid5(ontology_kind_namespace("issuer"), f"legacy-id:{legacy_partner_id}"))


@contextlib.contextmanager
def _optional_savepoint(connection: Connection[Any]) -> Any:
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
    """Resolve an extracted partner against `staging.entity_aliases` or mint a canonical UUID."""
    legacy_partner_id = _slug_entity_id(partner_name)
    clean_name = partner_name.strip()
    try:
        with _optional_savepoint(connection):
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

            birth_uuid = lookup_minted_entity(connection, "legacy-id", legacy_partner_id, known_at=tx_time)
            if birth_uuid is not None:
                return str(birth_uuid)

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
                "select entity_id::text, value from staging.entity_aliases where entity_id = any(%s::uuid[]) and scheme = 'cik'",
                (sorted(uuid_to_issuer),),
            ).fetchall()
            for entity_id, cik_val in alias_rows:
                matched_iss = uuid_to_issuer.get(str(entity_id).lower())
                if matched_iss is not None and cik_val:
                    by_cik.setdefault(str(cik_val).lstrip("0") or "0", matched_iss)
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
                by_cik[iss.legacy_id.removeprefix(prefix).lstrip("0") or "0"] = iss
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
    digits = _filing_doc_cik_digits(source_record_id)
    return by_cik.get(digits) if digits is not None else None


def _extract_cik_int(source_record_id: object, meta: dict[str, Any], matched: CanonicalIssuer) -> int:
    raw_cik = meta.get("cik")
    if isinstance(raw_cik, int):
        return raw_cik
    if isinstance(raw_cik, str) and raw_cik.strip().isdigit():
        return int(raw_cik.strip())
    digits = _filing_doc_cik_digits(source_record_id)
    if digits is not None and digits.isdigit():
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
    exceeded_issuers: dict[str, str] = field(default_factory=dict)
    issuer_extractors: dict[str, str] = field(default_factory=dict)


def _issuer_display_name(
    connection: Connection[Any],
    canonical_issuer_id: str,
    ticker: str,
    tx_time: datetime,
) -> str:
    clean_ticker = ticker.strip().upper()
    if is_uuid(canonical_issuer_id):
        row = connection.execute(
            """
            select btrim(value)
            from staging.entity_aliases
            where entity_id = %s::uuid
              and scheme = 'name'
              and nullif(btrim(value), '') is not null
              and upper(btrim(value)) <> %s
              and transaction_time <= %s
            order by confidence desc, transaction_time desc, alias_id desc
            limit 1
            """,
            (canonical_issuer_id, clean_ticker, tx_time),
        ).fetchone()
        if row and row[0]:
            return str(row[0])
    if clean_ticker:
        connection.execute(
            "update staging.kg_entities set display_name = '' where id = %s and upper(btrim(display_name)) = %s",
            (canonical_issuer_id, clean_ticker),
        )
    return ""


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
            issuer_name = _issuer_display_name(connection, canonical_issuer_id, matched.ticker, tx_time)
            ensure_entity(connection, canonical_issuer_id, "company", issuer_name)
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
    cutoff: datetime,
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
            cutoff=cutoff,
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
    """Extract and persist supply-chain edges from captured SEC 10-K filings at or before `cutoff`."""
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
            cutoff=cutoff,
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


def _exposure_params(
    item: dict[str, Any] | SupplyChainExposure,
    *,
    as_of: datetime,
    default_extractor: str = "graph:kg-edges:v1",
) -> tuple[Any, ...]:
    extractor = default_extractor
    if isinstance(item, dict):
        extractor = str(item.get("extractor", default_extractor))
        if "partners" in item:
            item = supply_chain_exposure(
                item["partners"],
                entity_id=str(item["issuer_id"]),
                as_of=as_of,
                supplies_to_edges_exist=bool(item["supplies_to_edges_exist"]),
            )
    if isinstance(item, SupplyChainExposure):
        return (
            require_canonical_issuer_id(item.entity_id),
            item.exposure_score,
            item.direct_partners,
            item.suppliers_count,
            item.customers_count,
            item.max_partner_share,
            item.result.confidence,
            list(item.result.flags),
            extractor,
            "available" if item.exposure_score is not None else "unavailable",
        )
    raw_exp = item.get("exposure_score")
    exposure_score = Decimal(str(raw_exp)) if raw_exp is not None else None
    direct_partners = int(item.get("direct_partners", 0))
    raw_share = item.get("max_partner_share", exposure_score or Decimal("0"))
    return (
        require_canonical_issuer_id(str(item["issuer_id"])),
        exposure_score,
        direct_partners,
        int(item.get("suppliers_count", direct_partners)),
        int(item.get("customers_count", 0)),
        Decimal(str(raw_share)) if raw_share is not None else Decimal("0"),
        Decimal(str(item.get("confidence", "0.8" if exposure_score is not None else "0"))),
        list(item.get("reason_codes", [])),
        extractor,
        str(item.get("availability_status", "available" if exposure_score is not None else "unavailable")),
    )


def materialize_supply_chain_exposure(
    connection: Connection[Any],
    *,
    run_id: str,
    cutoff: datetime | None = None,
    exposure_data: Sequence[dict[str, Any] | SupplyChainExposure],
    default_extractor: str = "graph:kg-edges:v1",
) -> int:
    """Materialize batch supply-chain exposure rows into `mart.issuer_supply_chain_exposure`."""
    as_of = cutoff or datetime.now(tz=UTC)
    source_evidence, factor_validation = statuses_without_raw_pointer()
    source_status, val_status = source_evidence.value, factor_validation.value
    for item in exposure_data:
        issuer_id, *fields = _exposure_params(item, as_of=as_of, default_extractor=default_extractor)
        connection.execute(_INSERT_SQL, (run_id, issuer_id, as_of, *fields, source_status, val_status))
    return len(exposure_data)


def _parse_revenue_share_pct(raw_pct: object) -> Decimal | None:
    if raw_pct is None or isinstance(raw_pct, bool):
        return None
    text = str(raw_pct).strip()
    if not text:
        return None
    try:
        pct = Decimal(text)
    except Exception:  # noqa: BLE001
        return None
    if not pct.is_finite() or pct <= Decimal("0") or pct > Decimal("100"):
        return None
    return pct / Decimal("100")


def _register_partner_index(
    seen_edges: dict[tuple[str, str], int],
    *,
    rel_type: str,
    partner_id: str,
    partner_name: str,
    idx: int,
) -> None:
    seen_edges[(rel_type, partner_id)] = idx
    if not is_uuid(partner_id):
        seen_edges.setdefault((rel_type, _deterministic_partner_uuid(partner_id)), idx)
    if partner_name and partner_name != partner_id:
        slug_id = _slug_entity_id(partner_name)
        seen_edges.setdefault((rel_type, slug_id), idx)
        seen_edges.setdefault((rel_type, _deterministic_partner_uuid(slug_id)), idx)


def _issuer_relation_aliases(issuer: CanonicalIssuer) -> list[str]:
    det_uuid = (
        _deterministic_partner_uuid(issuer.legacy_id) if issuer.legacy_id and not is_uuid(issuer.legacy_id) else ""
    )
    return [item for item in dict.fromkeys((issuer.issuer_id, issuer.legacy_id, det_uuid)) if item]


def _resolve_relation_endpoint(row: Sequence[Any], alias_set: set[str]) -> tuple[str, str, str, object, object] | None:
    if len(row) < 4:
        return None
    from_id, to_id, conf, raw_pct = str(row[0]), str(row[1]), row[2], row[3]
    from_name = str(row[4]) if len(row) >= 5 and row[4] is not None else from_id
    to_name = str(row[5]) if len(row) >= 6 and row[5] is not None else to_id
    if from_id in alias_set and to_id not in alias_set:
        return "customer", to_id, to_name, conf, raw_pct
    if to_id in alias_set and from_id not in alias_set:
        return "supplier", from_id, from_name, conf, raw_pct
    return None


def _lookup_existing_partner_index(
    seen_edges: dict[tuple[str, str], int], *, rel_type: str, p_id: str, p_name: str
) -> int | None:
    idx = seen_edges.get((rel_type, p_id))
    if idx is not None or not p_name or p_name == p_id:
        return idx
    slug_id = _slug_entity_id(p_name)
    return seen_edges.get((rel_type, slug_id)) or seen_edges.get((rel_type, _deterministic_partner_uuid(slug_id)))


def _apply_entity_relation_row(
    row: Sequence[Any],
    *,
    alias_set: set[str],
    partners: list[SupplyChainPartner],
    seen_edges: dict[tuple[str, str], int],
) -> None:
    endpoint = _resolve_relation_endpoint(row, alias_set)
    if endpoint is None:
        return
    rel_type, p_id, p_name, conf, raw_pct = endpoint
    share = _parse_revenue_share_pct(raw_pct)
    idx = _lookup_existing_partner_index(seen_edges, rel_type=rel_type, p_id=p_id, p_name=p_name)
    if idx is not None:
        existing = partners[idx]
        merged_share = existing.revenue_share if existing.revenue_share is not None else share
        merged_name = (
            existing.partner_name if existing.partner_name and existing.partner_name != existing.partner_id else p_name
        )
        partners[idx] = SupplyChainPartner(
            partner_id=existing.partner_id,
            partner_name=merged_name,
            relation_type=existing.relation_type,
            revenue_share=merged_share,
            confidence=existing.confidence,
        )
        _register_partner_index(seen_edges, rel_type=rel_type, partner_id=p_id, partner_name=merged_name, idx=idx)
        return
    new_idx = len(partners)
    partners.append(
        SupplyChainPartner(
            partner_id=p_id,
            partner_name=p_name,
            relation_type=rel_type,
            revenue_share=share,
            confidence=Decimal(str(conf)) if conf is not None else Decimal("0"),
        )
    )
    _register_partner_index(seen_edges, rel_type=rel_type, partner_id=p_id, partner_name=p_name, idx=new_idx)


def _merge_entity_relation_partners(
    connection: Connection[Any],
    *,
    issuer: CanonicalIssuer,
    cutoff: datetime,
    cutoff_date: Any,
    partners: list[SupplyChainPartner],
    seen_edges: dict[tuple[str, str], int],
) -> None:
    aliases = _issuer_relation_aliases(issuer)
    if not aliases:
        return
    sql = """
    select
        r.from_entity_id::text,
        r.to_entity_id::text,
        r.confidence,
        r.attributes->>'revenue_share_pct',
        coalesce(ent_from.display_name, r.from_entity_id::text),
        coalesce(ent_to.display_name, r.to_entity_id::text)
    from staging.entity_relations r
    left join staging.kg_entities ent_from on ent_from.id = r.from_entity_id::text
    left join staging.kg_entities ent_to on ent_to.id = r.to_entity_id::text
    where r.relation_type = 'supplies_to'
      and (r.from_entity_id::text = any(%s) or r.to_entity_id::text = any(%s))
      and r.transaction_time <= %s
      and r.valid_from <= %s::date
      and (r.valid_to is null or r.valid_to > %s::date)
    order by r.transaction_time desc, r.relation_id desc
    """
    try:
        with _optional_savepoint(connection):
            rows = connection.execute(sql, (aliases, aliases, cutoff, cutoff_date, cutoff_date)).fetchall()
    except Exception:  # noqa: BLE001
        return
    alias_set = set(aliases)
    for row in rows:
        if isinstance(row, tuple | list):
            _apply_entity_relation_row(row, alias_set=alias_set, partners=partners, seen_edges=seen_edges)


def _load_issuer_graph_partners(
    connection: Connection[Any],
    *,
    issuer: CanonicalIssuer,
    cutoff: datetime,
    cutoff_date: Any,
) -> list[SupplyChainPartner]:
    partners: list[SupplyChainPartner] = []
    seen_edges: dict[tuple[str, str], int] = {}
    for endpoint_id in dict.fromkeys((issuer.issuer_id, issuer.legacy_id)):
        if not endpoint_id:
            continue
        for rel_type, partner_col, self_col in (("customer", "to_id", "from_id"), ("supplier", "from_id", "to_id")):
            sql = (
                f"select e.{partner_col}, coalesce(ent.display_name, e.{partner_col}), e.confidence "
                f"from staging.kg_edges e left join staging.kg_entities ent on ent.id = e.{partner_col} "
                f"where e.{self_col} = %s and e.relation_type = 'supplies_to' and e.transaction_time <= %s "
                "and (e.valid_time is null or e.valid_time @> %s::date)"
            )
            for p_id, p_name, conf in connection.execute(sql, (endpoint_id, cutoff, cutoff_date)).fetchall():
                pid_str, pname_str = str(p_id), str(p_name)
                if (rel_type, pid_str) not in seen_edges:
                    idx = len(partners)
                    partners.append(
                        SupplyChainPartner(
                            partner_id=pid_str,
                            partner_name=pname_str,
                            relation_type=rel_type,
                            revenue_share=None,
                            confidence=Decimal(str(conf)) if conf is not None else Decimal("0"),
                        )
                    )
                    _register_partner_index(
                        seen_edges, rel_type=rel_type, partner_id=pid_str, partner_name=pname_str, idx=idx
                    )
    _merge_entity_relation_partners(
        connection,
        issuer=issuer,
        cutoff=cutoff,
        cutoff_date=cutoff_date,
        partners=partners,
        seen_edges=seen_edges,
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
    """Extract and materialize supply-chain exposure for all issuers in a universe run."""
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
        count += materialize_supply_chain_exposure(
            connection,
            run_id=run_id,
            cutoff=cutoff,
            exposure_data=[record],
            default_extractor=diag.issuer_extractors.get(issuer.issuer_id, "graph:kg-edges:v1"),
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
    row = connection.execute(
        "select exists (select 1 from staging.kg_edges where relation_type = 'supplies_to' and transaction_time <= %s)",
        (cutoff,),
    ).fetchone()
    if row and row[0]:
        return True
    try:
        with _optional_savepoint(connection):
            rel_row = connection.execute(
                "select exists (select 1 from staging.entity_relations where relation_type = 'supplies_to' and transaction_time <= %s)",
                (cutoff,),
            ).fetchone()
            return bool(rel_row and rel_row[0])
    except Exception:  # noqa: BLE001
        return False
