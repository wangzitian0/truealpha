"""10-K Supply-chain relationship extraction and materialization (#772, init.md §0 q3).

Extracts customer/supplier relationships from 10-K filings and materializes
exposure metrics into mart.issuer_supply_chain_exposure.
"""

from __future__ import annotations

import re
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

_CONFIDENCE_POLICY = ContinuousConfidencePolicy()

__all__ = (
    "SupplyChainEdgeCandidate",
    "SupplyChainExposure",
    "SupplyChainPartner",
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


@dataclass(frozen=True)
class SupplyChainEdgeCandidate:
    source_entity_id: str
    target_entity_name: str
    relation_type: str  # e.g. "supplier" or "customer"
    evidence_sentence: str
    confidence: Decimal


_NEGATIVE_SUBSTRINGS = (
    "customer service",
    "customer support",
    "customer deposit",
    "customer care",
    "customer satisfaction",
    "customer relations",
    "customer experience",
    "customer base",
    "vendor management",
)

_SUPPLIES_TO_PATTERN = re.compile(
    r"supplies\s+to\s+([A-Z][A-Za-z0-9\s,\.&]{1,40}?)(?:\s+(?:as|for|under|with|\.|\,)|$)",
    re.IGNORECASE,
)
_PURCHASES_FROM_PATTERN = re.compile(
    r"purchase(?:s)?\s+(?:raw\s+materials\s+|components\s+)?from\s+([A-Z][A-Za-z0-9\s,\.&]{1,40}?)(?:\s+(?:substantially|as|for|under|with|\.|\,)|$)",
    re.IGNORECASE,
)


def extract_supply_chain_relationships(
    filing_text: str,
    *,
    issuer_id: str,
    filing_date: date,
    accession: str,
    llm_gateway: Any | None = None,
) -> tuple[SupplyChainEdgeCandidate, ...]:
    """Extract supply chain relationships from 10-K text.

    Args:
        filing_text: Raw or extracted text from Item 1 / 1A.
        issuer_id: Source company canonical ID.
        filing_date: As-of filing date.
        accession: SEC accession number.
        llm_gateway: Optional LLM classification gateway.

    Returns:
        Tuple of detected relationship edge candidates.
    """
    if not filing_text or len(filing_text.strip()) < 100:
        return ()

    results: list[SupplyChainEdgeCandidate] = []
    # Extract candidate sentences referencing relationships
    for line in filing_text.splitlines():
        line_clean = line.strip()
        lower = line_clean.lower()
        if any(neg in lower for neg in _NEGATIVE_SUBSTRINGS):
            continue

        if any(term in lower for term in ("supplier", "customer", "vendor", "supplies", "purchase", "purchasing")):
            # Direction rules:
            #   "supplies to X"   → X is a customer (issuer sells to X)
            #   "customer"        → partner is a customer
            #   "supplier/vendor" → partner is a supplier
            #   "supplies" alone  → issuer is a supplier; partner is a customer
            #   "purchase(s) from X"→ issuer buys from X; X is a supplier
            is_customer_context = "supplies to" in lower or "customer" in lower
            is_supplier_context = any(s in lower for s in ("supplier", "vendor", "purchase", "purchasing")) or (
                "supplies" in lower and "supplies to" not in lower
            )
            if not is_customer_context and not is_supplier_context:
                continue
            rel = "customer" if is_customer_context and not is_supplier_context else "supplier"

            target_name = None
            if rel == "customer":
                m_sup = _SUPPLIES_TO_PATTERN.search(line_clean)
                if m_sup:
                    target_name = m_sup.group(1).strip()
            else:
                m_pur = _PURCHASES_FROM_PATTERN.search(line_clean)
                if m_pur:
                    target_name = m_pur.group(1).strip()

            if not target_name:
                continue

            sample_conf = Decimal("0.98") if len(line_clean) >= 40 else Decimal("0.92")
            safe_issuer = re.sub(r"[^A-Za-z0-9._:/@+-]+", "-", issuer_id).strip("-") or "issuer"
            safe_acc = re.sub(r"[^A-Za-z0-9._:/@+-]+", "-", accession).strip("-") or "acc"
            safe_target = re.sub(r"[^A-Za-z0-9._:/@+-]+", "-", target_name).strip("-") or "partner"
            edge_conf = evaluate_continuous_confidence(
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

            results.append(
                SupplyChainEdgeCandidate(
                    source_entity_id=issuer_id,
                    target_entity_name=target_name,
                    relation_type=rel,
                    evidence_sentence=line_clean[:200],
                    confidence=edge_conf,
                )
            )
    return tuple(results)


def _slug_entity_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"company:partner:{slug or 'unknown'}"


def persist_extracted_supply_chain_edges(
    connection: Connection[Any],
    *,
    issuers: Sequence[CanonicalIssuer],
    cutoff: datetime,
    object_store: Any | None = None,
) -> int:
    """Extract supply-chain edges from captured SEC 10-K filings at or before `cutoff` and
    persist them into `staging.kg_entities` and `staging.kg_edges`.
    """
    by_cik: dict[str, CanonicalIssuer] = {}
    by_legacy: dict[str, CanonicalIssuer] = {}
    by_issuer_id: dict[str, CanonicalIssuer] = {}
    for iss in issuers:
        by_legacy[iss.legacy_id] = iss
        by_issuer_id[iss.issuer_id] = iss
        if iss.legacy_id.startswith("company:cik:"):
            cik_raw = iss.legacy_id.removeprefix("company:cik:").lstrip("0") or "0"
            by_cik[cik_raw] = iss

    try:
        rows = connection.execute(
            """
            select id, source_record_id, object_key, source_published_at, metadata
            from raw.fetches
            where source = 'sec'
              and source_record_id like 'filing-document:CIK%%'
              and source_published_at <= %s
            order by source_published_at asc, id asc
            """,
            (cutoff,),
        ).fetchall()
    except Exception:  # noqa: BLE001
        return 0

    inserted = 0
    for row in rows:
        if not isinstance(row, tuple | list) or len(row) < 5:
            continue
        fetch_id, source_record_id, object_key, published_at, metadata = row[:5]
        meta = metadata if isinstance(metadata, dict) else {}
        matched: CanonicalIssuer | None = None
        issuer_hint = str(meta.get("issuer_id") or meta.get("legacy_id") or "")
        if issuer_hint in by_legacy:
            matched = by_legacy[issuer_hint]
        elif issuer_hint in by_issuer_id:
            matched = by_issuer_id[issuer_hint]
        elif isinstance(source_record_id, str) and source_record_id.startswith("filing-document:CIK"):
            parts = source_record_id.split(":")
            if len(parts) >= 2:
                cik_digits = parts[1].removeprefix("CIK").lstrip("0") or "0"
                matched = by_cik.get(cik_digits)
        if matched is None:
            continue

        filing_text = meta.get("plain_text") if isinstance(meta.get("plain_text"), str) else None
        if not filing_text and object_store is not None and isinstance(object_key, str) and object_key:
            try:
                from data_engine.datahub.sec_filing_documents import (  # type: ignore[import-untyped]
                    filing_plain_text,
                )

                raw_bytes = object_store.get_bytes(object_key)
                filing_text = filing_plain_text(raw_bytes, document_name=str(meta.get("primary_document") or "10k.txt"))
            except Exception:  # noqa: BLE001
                filing_text = None
        if not filing_text:
            continue

        tx_time = published_at if isinstance(published_at, datetime) else cutoff
        filing_dt = tx_time.date() if hasattr(tx_time, "date") else cutoff.date()
        accession = str(meta.get("accession") or source_record_id)
        candidates = extract_supply_chain_relationships(
            filing_text,
            issuer_id=matched.legacy_id,
            filing_date=filing_dt,
            accession=accession,
        )
        for cand in candidates:
            partner_id = _slug_entity_id(cand.target_entity_name)
            ensure_entity(connection, matched.legacy_id, "company", matched.ticker)
            ensure_entity(connection, partner_id, "company", cand.target_entity_name)
            if add_edge(
                connection,
                from_id=matched.legacy_id,
                to_id=partner_id,
                relation_type="supplies_to",
                confidence=float(cand.confidence),
                source="sec_10k_supply_chain",
                transaction_time=tx_time,
                valid_from=filing_dt.isoformat(),
                raw_ref=f"raw.fetches:{fetch_id}",
            ):
                inserted += 1
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


def materialize_universe_supply_chain_exposure(
    connection: Connection[Any],
    *,
    run_id: str,
    cutoff: datetime,
    issuers: Sequence[CanonicalIssuer],
    object_store: Any | None = None,
) -> int:
    """Extract and materialize supply chain exposure for all issuers in a universe run.

    A row stores the issuer's canonical id, the wide row's (#1079). The graph read takes the
    legacy id, which is what the knowledge graph keys its edges by.
    """
    count = 0
    has_edges_table = False
    try:
        with connection.cursor() as cur:
            cur.execute("select to_regclass('staging.kg_edges')")
            res = cur.fetchone()
            has_edges_table = bool(res and res[0])
    except Exception:
        try:
            connection.rollback()
        except Exception:
            pass
        has_edges_table = False

    if has_edges_table:
        persist_extracted_supply_chain_edges(
            connection,
            issuers=issuers,
            cutoff=cutoff,
            object_store=object_store,
        )

    cutoff_date = cutoff.date() if hasattr(cutoff, "date") else cutoff
    # One measurement per run, taken before any issuer: does the graph hold a supplier edge at all?
    # A false value means no extraction has run. A row then says so, instead of claiming that the
    # company disclosed no supplier (#772).
    supplies_to_edges_exist = has_edges_table and _supplies_to_edges_exist(connection, cutoff)
    for issuer in issuers:
        partners: list[SupplyChainPartner] = []
        if has_edges_table:
            rows = connection.execute(
                """
                select e.to_id, coalesce(ent.display_name, e.to_id), e.confidence
                from staging.kg_edges e
                left join staging.kg_entities ent on ent.id = e.to_id
                where e.from_id = %s and e.relation_type = 'supplies_to' and e.transaction_time <= %s
                  and (e.valid_time is null or e.valid_time @> %s::date)
                """,
                (issuer.legacy_id, cutoff, cutoff_date),
            ).fetchall()
            for p_id, p_name, conf in rows:
                # The edge runs from the issuer to the partner, so the partner buys from the issuer.
                partners.append(
                    SupplyChainPartner(
                        partner_id=str(p_id),
                        partner_name=str(p_name),
                        relation_type="customer",
                        revenue_share=None,
                        confidence=Decimal(str(conf)) if conf is not None else Decimal("0"),
                    )
                )

        record = supply_chain_exposure(
            partners, entity_id=issuer.issuer_id, as_of=cutoff, supplies_to_edges_exist=supplies_to_edges_exist
        )
        count += materialize_supply_chain_exposure(
            connection,
            run_id=run_id,
            cutoff=cutoff,
            exposure_data=[record],
        )
    return count


def _supplies_to_edges_exist(connection: Connection[Any], cutoff: datetime) -> bool:
    """True when the graph holds a `supplies_to` edge for ANY issuer that was knowable at the cutoff.

    The test uses transaction time only. An edge whose validity ended before the cutoff still
    proves that an extraction ran. An edge recorded after the cutoff was not knowable then.
    """
    row = connection.execute(
        "select exists (select 1 from staging.kg_edges where relation_type = 'supplies_to' and transaction_time <= %s)",
        (cutoff,),
    ).fetchone()
    return bool(row and row[0])
