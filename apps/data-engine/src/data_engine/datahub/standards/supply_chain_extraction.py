"""10-K supply-chain extraction and materialization (#772)."""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from factors.base.supply_chain_exposure import SupplyChainExposure, SupplyChainPartner, supply_chain_exposure
from psycopg import Connection

from data_engine.datahub._helpers import (
    DEFAULT_CONFIDENCE_POLICY,
    execute_many,
    has_table,
    parse_decimal,
    safe_call,
    safe_case_id_part,
    safe_fetchall,
    safe_fetchone,
    single_origin_confidence,
    unbacked_status_values,
)
from data_engine.datahub.canonical_issuer import CanonicalIssuer, require_canonical_issuer_id
from data_engine.datahub.resolve_coordinates import is_uuid, lookup_minted_entity, ontology_kind_namespace
from data_engine.sources import llm

logger = logging.getLogger(__name__)

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

_UPDATE_COLS = (
    "cutoff",
    "exposure_score",
    "direct_partners",
    "suppliers_count",
    "customers_count",
    "max_partner_share",
    "confidence",
    "reason_codes",
    "extractor",
    "availability_status",
    "source_evidence_status",
    "factor_validation_status",
)
_INSERT_SQL = (
    f"insert into mart.issuer_supply_chain_exposure (run_id, issuer_id, {', '.join(_UPDATE_COLS)}) "
    f"values ({', '.join(('%s',) * (len(_UPDATE_COLS) + 2))}) on conflict (run_id, issuer_id) do update set "
    + ", ".join(f"{col} = excluded.{col}" for col in _UPDATE_COLS)
)


class SupplyChainRunReport(int):
    """Materialized row count with attached LLM telemetry."""

    rows: int
    edges_inserted: int
    model_extracted: int
    replayed: int
    context_window_exceeded: int
    model_errors: int
    #: Filings for which the provider gave no answer (status 0) or a server error (5xx). The next
    #: run can get an answer. A refusal or a cut-off answer of one filing repeats on each run.
    no_answer: int
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
        no_answer: int = 0,
        exceeded_filings: Sequence[str] = (),
    ) -> SupplyChainRunReport:
        obj = super().__new__(cls, rows)
        obj.rows, obj.edges_inserted, obj.model_extracted = int(rows), int(edges_inserted), int(model_extracted)
        obj.replayed, obj.context_window_exceeded, obj.model_errors = (
            int(replayed),
            int(context_window_exceeded),
            int(model_errors),
        )
        obj.no_answer = int(no_answer)
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
    extractor: str = "graph:entity-relations:v1"
    replayed: bool = False


@dataclass(frozen=True)
class _SupplyChainExtractionResult:
    candidates: tuple[SupplyChainEdgeCandidate, ...]
    extractor: str
    replayed: bool


_WS_RE = re.compile(r"\s+")


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
    safe_issuer = safe_case_id_part(issuer_id, "issuer", collapse=True)
    safe_acc = safe_case_id_part(accession, "acc", collapse=True)
    safe_target = safe_case_id_part(target_name, "partner", collapse=True)
    return single_origin_confidence(
        case_id=f"supply-chain:{safe_issuer}:{safe_acc}:{safe_target}",
        provider_id="origin:llm-extractor:v1",
        lineage="sec-10k",
        evidence_id=f"accession:{safe_acc}",
        fallback_prior_id="origin:sec-10k:v1",
        freshness=_filing_freshness(filing_date, cutoff),
        semantic_mapping_quality=Decimal("0.95"),
        reason_codes=(f"relation:{rel}",),
        policy=DEFAULT_CONFIDENCE_POLICY,
    )


def _parse_cik_from_identifier(raw: str) -> int:
    return int(digits) if (digits := re.sub(r"\D+", "", raw)) else 0


def _filing_doc_cik_digits(source_record_id: object) -> str | None:
    if isinstance(source_record_id, str) and source_record_id.startswith("filing-document:CIK"):
        parts = source_record_id.split(":")
        if len(parts) >= 2:
            return parts[1].removeprefix("CIK").lstrip("0") or "0"
    return None


def _extract_supply_chain_result(
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
) -> _SupplyChainExtractionResult:
    if not filing_text or len(filing_text.strip()) < 40:
        return _SupplyChainExtractionResult(candidates=(), extractor="graph:entity-relations:v1", replayed=False)
    resolved_cik = cik if cik is not None else _parse_cik_from_identifier(issuer_id or accession)
    extraction = (llm_gateway or llm).identify_supply_chain_partners(
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
                    issuer_id, accession, partner.partner_name, partner.relation_type, filing_date, cutoff=cutoff
                ),
                target_ticker=partner.ticker,
                revenue_share_pct=partner.revenue_share_pct,
                extractor=extraction.extractor,
                replayed=extraction.invocation.replayed,
            )
        )
    return _SupplyChainExtractionResult(
        candidates=tuple(results),
        extractor=extraction.extractor,
        replayed=extraction.invocation.replayed,
    )


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
    """Extract supplier/customer edge candidates from 10-K text."""
    return _extract_supply_chain_result(
        filing_text,
        issuer_id=issuer_id,
        filing_date=filing_date,
        accession=accession,
        cik=cik,
        issuer_label=issuer_label,
        cutoff=cutoff,
        connection=connection,
        llm_gateway=llm_gateway,
        persist=persist,
    ).candidates


def _slug_entity_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"company:partner:{slug or 'unknown'}"


def _deterministic_partner_uuid(legacy_partner_id: str) -> str:
    return str(uuid.uuid5(ontology_kind_namespace("issuer"), f"legacy-id:{legacy_partner_id}"))


def _resolve_or_mint_partner_entity(
    connection: Connection[Any], *, partner_name: str, partner_ticker: str | None, tx_time: datetime, raw_ref: str
) -> str:
    legacy_id = _slug_entity_id(partner_name)
    clean_name = partner_name.strip()

    def _resolve() -> str:
        row = connection.execute(
            "select staging.entity_survivor(a.entity_id, %s)::text from staging.entity_aliases a "
            "join staging.entities e on e.entity_id = a.entity_id where "
            "(a.scheme = 'legacy-id' and a.value = %s) or "
            "(e.kind in ('issuer', 'fund') and a.scheme = 'name' and lower(a.value) = lower(%s) and a.transaction_time <= %s) "
            "order by (a.scheme = 'legacy-id') desc, a.confidence desc, a.transaction_time desc limit 1",
            (tx_time, legacy_id, clean_name, tx_time),
        ).fetchone()
        if row and row[0]:
            return str(row[0])
        if (birth := lookup_minted_entity(connection, "legacy-id", legacy_id, known_at=tx_time)) is not None:
            return str(birth)
        if partner_ticker:
            ticker_row = connection.execute(
                "select staging.entity_survivor(r1.from_entity_id, %s)::text from staging.entity_aliases a "
                "join staging.entity_relations r2 on r2.to_entity_id = a.entity_id and r2.relation_type = 'listed_as' "
                "join staging.entity_relations r1 on r1.to_entity_id = r2.from_entity_id and r1.relation_type = 'issues' "
                "where a.scheme = 'mic-ticker' and split_part(a.value, ':', 2) = upper(%s) "
                "and a.transaction_time <= %s order by a.confidence desc, a.transaction_time desc limit 1",
                (tx_time, partner_ticker.strip(), tx_time),
            ).fetchone()
            if ticker_row and ticker_row[0]:
                return str(ticker_row[0])
        minted = connection.execute(
            "select staging.entity_mint('issuer', 'legacy-id', %s, 'datahub.supply_chain')::text", (legacy_id,)
        ).fetchone()
        if minted and minted[0]:
            minted_uuid = str(minted[0])
            connection.execute(
                "insert into staging.entity_aliases (entity_id, scheme, value, valid_from, valid_to, "
                "transaction_time, source, raw_ref, method, confidence, mapping_version) values "
                "(%s::uuid, 'legacy-id', %s, '-infinity'::date, null, least(%s::timestamptz, clock_timestamp()), "
                "'datahub.supply_chain', %s, 'asserted', 0.9, 'supply-chain:v1') on conflict do nothing",
                (minted_uuid, legacy_id, tx_time, raw_ref),
            )
            if clean_name:
                connection.execute(
                    "insert into staging.entity_aliases (entity_id, scheme, value, valid_from, valid_to, "
                    "transaction_time, source, raw_ref, method, confidence, mapping_version) values "
                    "(%s::uuid, 'name', %s, '-infinity'::date, null, least(%s::timestamptz, clock_timestamp()), "
                    "'datahub.supply_chain', %s, 'asserted', 0.9, 'supply-chain:v1') on conflict do nothing",
                    (minted_uuid, clean_name, tx_time, raw_ref),
                )
            return minted_uuid
        return _deterministic_partner_uuid(legacy_id)

    return str(safe_call(connection, _resolve) or _deterministic_partner_uuid(legacy_id))


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
        by_legacy[iss.legacy_id] = by_issuer_id[iss.issuer_id] = iss
        for prefix in ("company:cik:", "issuer:cik:"):
            if iss.legacy_id.startswith(prefix):
                by_cik[iss.legacy_id.removeprefix(prefix).lstrip("0") or "0"] = iss
        if is_uuid(iss.issuer_id):
            uuid_to_issuer[iss.issuer_id.lower()] = iss
    if connection is not None:
        if uuid_to_issuer:
            for entity_id, cik_val in safe_fetchall(
                connection,
                "select entity_id::text, value from staging.entity_aliases where entity_id = any(%s::uuid[]) and scheme = 'cik'",
                (sorted(uuid_to_issuer),),
            ):
                if cik_val and (matched := uuid_to_issuer.get(str(entity_id).lower())) is not None:
                    by_cik.setdefault(str(cik_val).lstrip("0") or "0", matched)
        if run_id:

            def _load_governed() -> None:
                from data_engine.datahub.capture_core.theme_purity import governed_members

                for cik_int, member_id in governed_members(connection, run_id=run_id).items():
                    if (matched := by_issuer_id.get(member_id) or uuid_to_issuer.get(member_id.lower())) is not None:
                        by_cik.setdefault(str(cik_int), matched)

            safe_call(connection, _load_governed)
    return by_cik, by_legacy, by_issuer_id


def _match_filing_issuer(
    source_record_id: object,
    meta: dict[str, Any],
    by_cik: dict[str, CanonicalIssuer],
    by_legacy: dict[str, CanonicalIssuer],
    by_issuer_id: dict[str, CanonicalIssuer],
) -> CanonicalIssuer | None:
    hint = str(meta.get("issuer_id") or meta.get("legacy_id") or "")
    if hint in by_legacy:
        return by_legacy[hint]
    if hint in by_issuer_id:
        return by_issuer_id[hint]
    digits = _filing_doc_cik_digits(source_record_id)
    return by_cik.get(digits) if digits is not None else None


def _extract_cik_int(source_record_id: object, meta: dict[str, Any], matched: CanonicalIssuer) -> int:
    raw_cik = meta.get("cik")
    if isinstance(raw_cik, int):
        return raw_cik
    if isinstance(raw_cik, str) and raw_cik.strip().isdigit():
        return int(raw_cik.strip())
    digits = _filing_doc_cik_digits(source_record_id)
    return int(digits) if digits is not None and digits.isdigit() else _parse_cik_from_identifier(matched.legacy_id)


def _resolve_filing_text(
    connection: Connection[Any],
    fetch_id: object,
    meta: dict[str, Any],
    object_uri: object,
    object_store: Any | None,
) -> str | None:
    if isinstance(plain := meta.get("plain_text"), str) and plain:
        return plain
    if not (isinstance(object_uri, str) and object_uri):
        return None

    def _load() -> str | None:
        from data_engine.datahub.standards.filing_extraction import filing_plain_text
        from data_engine.raw_store import get_payload

        if object_store is not None and hasattr(object_store, "get_bytes"):
            raw_bytes = object_store.get_bytes(object_uri)
        elif isinstance(fetch_id, int):
            raw_bytes = get_payload(connection, fetch_id, store=object_store)
        else:
            return None
        return str(filing_plain_text(raw_bytes))

    return safe_call(connection, _load)


@dataclass
class _ExtractionDiagnostics:
    edges_inserted: int = 0
    model_extracted: int = 0
    replayed: int = 0
    context_window_exceeded: int = 0
    model_errors: int = 0
    no_answer: int = 0
    exceeded_issuers: dict[str, str] = field(default_factory=dict)
    issuer_extractors: dict[str, str] = field(default_factory=dict)
    issuer_raw_refs: dict[str, str] = field(default_factory=dict)


def persist_extracted_supply_chain_edges(
    connection: Connection[Any],
    *,
    issuers: Sequence[CanonicalIssuer],
    cutoff: datetime,
    run_id: str | None = None,
    object_store: Any | None = None,
    llm_gateway: Any | None = None,
    _diagnostics: _ExtractionDiagnostics | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> int:
    """Extract and persist supply-chain edges from 10-K filings.

    `checkpoint` runs after each filing that has an extraction. The lane passes the commit of its
    connection: a run that the runtime bound stops keeps the model answers of the filings it
    finished, and the next run replays them without a model call (#1816).
    """
    if not has_table(connection, "raw.fetches"):
        return 0
    diag = _diagnostics if _diagnostics is not None else _ExtractionDiagnostics()
    by_cik, by_legacy, by_issuer_id = _build_issuer_lookup(issuers, connection, run_id=run_id)
    rows = safe_fetchall(
        connection,
        "select distinct on (split_part(source_record_id, ':', 2)) "
        "id, source_record_id, object_uri, source_published_at, metadata "
        "from raw.fetches where source = 'sec' and source_record_id like 'filing-document:CIK%%' "
        "and source_published_at <= %s "
        "order by split_part(source_record_id, ':', 2), source_published_at desc, id desc",
        (cutoff,),
    )
    if not rows:
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
        accession = str(meta.get("accession") or source_record_id)
        cik_int = _extract_cik_int(source_record_id, meta, matched)
        issuer_id = matched.issuer_id
        canonical_label = matched.ticker.strip().upper().replace("-", ".") if matched.ticker else f"CIK {cik_int:010d}"
        try:
            extraction = _extract_supply_chain_result(
                filing_text,
                issuer_id=issuer_id,
                filing_date=filing_dt,
                accession=accession,
                cik=cik_int,
                issuer_label=canonical_label,
                cutoff=cutoff,
                connection=connection,
                llm_gateway=llm_gateway,
                persist=True,
            )
        except llm.ModelContextLimitError as exc:
            diag.context_window_exceeded += 1
            diag.exceeded_issuers[issuer_id] = f"{matched.ticker or issuer_id}:{accession}"
            logger.error(
                "Supply chain 10-K exceeded 1M context window for %s (CIK %s, accession %s): %s",
                matched.ticker,
                cik_int,
                accession,
                exc,
            )
            continue
        except (llm.ModelNotConfigured, llm.ModelHTTPError) as exc:
            diag.model_errors += 1
            status = getattr(exc, "status_code", None)
            if status is not None and (status == 0 or status >= 500):
                diag.no_answer += 1
            logger.warning(
                "Supply chain LLM extraction unavailable for %s (CIK %s, %s): %s",
                matched.ticker,
                cik_int,
                accession,
                exc,
            )
            continue
        raw_ref = f"raw.fetches:{fetch_id}"
        diag.issuer_extractors[issuer_id] = extraction.extractor
        diag.issuer_raw_refs[issuer_id] = raw_ref
        if extraction.replayed:
            diag.replayed += 1
        else:
            diag.model_extracted += 1
        candidates = extraction.candidates
        for cand in candidates:
            partner_uuid = _resolve_or_mint_partner_entity(
                connection,
                partner_name=cand.target_entity_name,
                partner_ticker=cand.target_ticker,
                tx_time=tx_time,
                raw_ref=raw_ref,
            )
            if partner_uuid == issuer_id:
                continue
            from_id, to_id = (
                (partner_uuid, issuer_id) if cand.relation_type == "supplier" else (issuer_id, partner_uuid)
            )
            if is_uuid(from_id) and is_uuid(to_id) and from_id != to_id:
                attrs = {"revenue_share_pct": str(cand.revenue_share_pct)} if cand.revenue_share_pct is not None else {}
                row = safe_fetchone(
                    connection,
                    "with t as (select least(%s::timestamptz, clock_timestamp()) as eff_tx), "
                    "r as (select staging.entity_relation_uuid('supplies_to', %s::uuid, %s::uuid, %s::date, t.eff_tx, "
                    "'sec_10k_supply_chain', 'asserted') as rel_id, t.eff_tx from t) "
                    "insert into staging.entity_relations (relation_id, relation_type, from_entity_id, to_entity_id, "
                    "valid_from, transaction_time, source, raw_ref, method, confidence, evidence, attributes, mapping_version) "
                    "select r.rel_id, 'supplies_to', %s::uuid, %s::uuid, %s::date, r.eff_tx, 'sec_10k_supply_chain', %s, "
                    "'asserted', %s, %s::jsonb, %s::jsonb, 'supply-chain:v1' from r "
                    "where exists (select 1 from staging.entities where entity_id = %s::uuid) "
                    "and exists (select 1 from staging.entities where entity_id = %s::uuid) "
                    "on conflict do nothing returning relation_id",
                    (
                        tx_time,
                        from_id,
                        to_id,
                        filing_dt,
                        from_id,
                        to_id,
                        filing_dt,
                        raw_ref,
                        cand.confidence,
                        json.dumps({"quote": cand.evidence_sentence}),
                        json.dumps(attrs),
                        from_id,
                        to_id,
                    ),
                )
                if row:
                    inserted += 1
        diag.edges_inserted = inserted
        if checkpoint is not None:
            checkpoint()
    diag.edges_inserted = inserted
    return inserted


def _exposure_params(
    item: dict[str, Any] | SupplyChainExposure, *, as_of: datetime, default_extractor: str = "graph:entity-relations:v1"
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
    score = Decimal(str(raw_exp)) if raw_exp is not None else None
    direct = int(item.get("direct_partners", 0))
    raw_share = item.get("max_partner_share", score or Decimal("0"))
    return (
        require_canonical_issuer_id(str(item["issuer_id"])),
        score,
        direct,
        int(item.get("suppliers_count", direct)),
        int(item.get("customers_count", 0)),
        Decimal(str(raw_share)) if raw_share is not None else Decimal("0"),
        Decimal(str(item.get("confidence", "0.8" if score is not None else "0"))),
        list(item.get("reason_codes", [])),
        extractor,
        str(item.get("availability_status", "available" if score is not None else "unavailable")),
    )


def materialize_supply_chain_exposure(
    connection: Connection[Any],
    *,
    run_id: str,
    cutoff: datetime | None = None,
    exposure_data: Sequence[dict[str, Any] | SupplyChainExposure],
    default_extractor: str = "graph:entity-relations:v1",
) -> int:
    """Materialize exposure rows into `mart.issuer_supply_chain_exposure`."""
    as_of, source_status, val_status = unbacked_status_values(cutoff)
    rows = [
        (run_id, issuer_id, as_of, *fields, source_status, val_status)
        for item in exposure_data
        for issuer_id, *fields in (_exposure_params(item, as_of=as_of, default_extractor=default_extractor),)
    ]
    execute_many(connection, _INSERT_SQL, rows)
    return len(exposure_data)


def _parse_revenue_share_pct(raw_pct: object) -> Decimal | None:
    return (
        pct / Decimal("100")
        if (pct := parse_decimal(raw_pct)) is not None
        and not isinstance(raw_pct, bool)
        and Decimal("0") < pct <= Decimal("100")
        else None
    )


def _partner_index_keys(rel_type: str, partner_id: str, partner_name: str) -> tuple[tuple[str, str], ...]:
    keys = [(rel_type, partner_id)]
    if not is_uuid(partner_id):
        keys.append((rel_type, _deterministic_partner_uuid(partner_id)))
    if partner_name and partner_name != partner_id:
        slug_id = _slug_entity_id(partner_name)
        keys.extend(((rel_type, slug_id), (rel_type, _deterministic_partner_uuid(slug_id))))
    return tuple(keys)


def _register_partner_index(
    seen_edges: dict[tuple[str, str], int], rel_type: str, partner_id: str, partner_name: str, idx: int
) -> None:
    keys = _partner_index_keys(rel_type, partner_id, partner_name)
    seen_edges[keys[0]] = idx
    for key in keys[1:]:
        seen_edges.setdefault(key, idx)


def _append_partner(
    partners: list[SupplyChainPartner],
    seen_edges: dict[tuple[str, str], int],
    partner_id: str,
    partner_name: str,
    rel_type: str,
    share: Decimal | None,
    conf: object,
) -> None:
    idx = len(partners)
    partners.append(
        SupplyChainPartner(
            partner_id=partner_id,
            partner_name=partner_name,
            relation_type=rel_type,
            revenue_share=share,
            confidence=Decimal(str(conf)) if conf is not None else Decimal("0"),
        )
    )
    _register_partner_index(seen_edges, rel_type, partner_id, partner_name, idx)


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


def _load_issuer_graph_partners(
    connection: Connection[Any],
    *,
    issuer: CanonicalIssuer,
    cutoff: datetime,
    cutoff_date: Any,
    current_raw_ref: str | None = None,
) -> list[SupplyChainPartner]:
    partners: list[SupplyChainPartner] = []
    seen_edges: dict[tuple[str, str], int] = {}
    det_uuid = (
        _deterministic_partner_uuid(issuer.legacy_id) if issuer.legacy_id and not is_uuid(issuer.legacy_id) else ""
    )
    aliases = [item for item in dict.fromkeys((issuer.issuer_id, issuer.legacy_id, det_uuid)) if item]
    if aliases:
        rows = safe_fetchall(
            connection,
            "select r.from_entity_id::text, r.to_entity_id::text, r.confidence, r.attributes->>'revenue_share_pct', "
            "coalesce(nf.value, r.from_entity_id::text), coalesce(nt.value, r.to_entity_id::text) "
            "from staging.entity_relations r "
            "left join lateral ("
            "  select ea.value from staging.entity_aliases ea "
            "  where ea.entity_id = r.from_entity_id and ea.scheme = 'name' "
            "  order by ea.confidence desc, ea.transaction_time desc, ea.alias_id desc limit 1"
            ") nf on true "
            "left join lateral ("
            "  select ea.value from staging.entity_aliases ea "
            "  where ea.entity_id = r.to_entity_id and ea.scheme = 'name' "
            "  order by ea.confidence desc, ea.transaction_time desc, ea.alias_id desc limit 1"
            ") nt on true "
            "where r.relation_type = 'supplies_to' and (r.from_entity_id::text = any(%s) or r.to_entity_id::text = any(%s)) "
            "and (%s::text is null or r.raw_ref = %s) "
            "and r.transaction_time <= %s and r.valid_from <= %s::date and (r.valid_to is null or r.valid_to > %s::date) "
            "order by r.transaction_time desc, r.relation_id desc",
            (aliases, aliases, current_raw_ref, current_raw_ref, cutoff, cutoff_date, cutoff_date),
        )
        alias_set = set(aliases)
        for row in rows or ():
            if not isinstance(row, tuple | list) or (ep := _resolve_relation_endpoint(row, alias_set)) is None:
                continue
            rel_type, p_id, p_name, conf, raw_pct = ep
            share = _parse_revenue_share_pct(raw_pct)
            idx = next((seen_edges[k] for k in _partner_index_keys(rel_type, p_id, p_name) if k in seen_edges), None)
            if idx is None:
                _append_partner(partners, seen_edges, p_id, p_name, rel_type, share, conf)
                continue
            existing = partners[idx]
            merged_name = (
                existing.partner_name
                if existing.partner_name and existing.partner_name != existing.partner_id
                else p_name
            )
            partners[idx] = replace(
                existing,
                partner_name=merged_name,
                revenue_share=existing.revenue_share if existing.revenue_share is not None else share,
            )
            _register_partner_index(seen_edges, rel_type, p_id, merged_name, idx)
    return partners


def materialize_universe_supply_chain_exposure(
    connection: Connection[Any],
    *,
    run_id: str,
    cutoff: datetime,
    issuers: Sequence[CanonicalIssuer],
    object_store: Any | None = None,
    llm_gateway: Any | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> SupplyChainRunReport:
    """Materialize supply-chain exposure for a universe run."""
    count = 0
    has_edges_table = has_table(connection, "staging.entity_relations")
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
            checkpoint=checkpoint,
        )
    cutoff_date = cutoff.date() if hasattr(cutoff, "date") else cutoff
    supplies_to_edges_exist = has_edges_table and _supplies_to_edges_exist(connection, cutoff)
    for issuer in issuers:
        if issuer.issuer_id in diag.exceeded_issuers:
            item: dict[str, Any] | SupplyChainExposure = {
                "issuer_id": issuer.issuer_id,
                "exposure_score": None,
                "max_partner_share": None,
                "confidence": Decimal("0"),
                "reason_codes": ["context_window_exceeded"],
                "extractor": f"model:{llm.SUPPLY_CHAIN_PROMPT_VERSION}:context_exceeded",
                "availability_status": "unavailable",
            }
        else:
            partners = (
                _load_issuer_graph_partners(
                    connection,
                    issuer=issuer,
                    cutoff=cutoff,
                    cutoff_date=cutoff_date,
                    current_raw_ref=diag.issuer_raw_refs.get(issuer.issuer_id),
                )
                if has_edges_table
                else []
            )
            item = supply_chain_exposure(
                partners, entity_id=issuer.issuer_id, as_of=cutoff, supplies_to_edges_exist=supplies_to_edges_exist
            )
        count += materialize_supply_chain_exposure(
            connection,
            run_id=run_id,
            cutoff=cutoff,
            exposure_data=[item],
            default_extractor=diag.issuer_extractors.get(issuer.issuer_id, "graph:entity-relations:v1"),
        )
    return SupplyChainRunReport(
        count,
        edges_inserted=diag.edges_inserted,
        model_extracted=diag.model_extracted,
        replayed=diag.replayed,
        context_window_exceeded=diag.context_window_exceeded,
        model_errors=diag.model_errors,
        no_answer=diag.no_answer,
        exceeded_filings=tuple(diag.exceeded_issuers.values()),
    )


def _supplies_to_edges_exist(connection: Connection[Any], cutoff: datetime) -> bool:
    row = safe_fetchone(
        connection,
        "select exists (select 1 from staging.entity_relations where relation_type = 'supplies_to' and transaction_time <= %s)",
        (cutoff,),
    )
    return bool(row and row[0])
