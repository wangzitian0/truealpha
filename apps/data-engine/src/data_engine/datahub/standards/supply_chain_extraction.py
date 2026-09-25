"""10-K Supply-chain relationship extraction and materialization (#772, init.md §0 q3).

Extracts customer/supplier relationships from 10-K filings and materializes
exposure metrics into mart.issuer_supply_chain_exposure.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from factors.base.supply_chain_exposure import (
    SupplyChainExposure,
    SupplyChainPartner,
    supply_chain_exposure,
)
from psycopg import Connection

__all__ = (
    "SupplyChainEdgeCandidate",
    "SupplyChainExposure",
    "SupplyChainPartner",
    "extract_supply_chain_relationships",
    "materialize_supply_chain_exposure",
    "materialize_universe_supply_chain_exposure",
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

            target_name = "Major Customer" if rel == "customer" else "Key Supplier"
            if rel == "customer":
                m_sup = _SUPPLIES_TO_PATTERN.search(line_clean)
                if m_sup:
                    target_name = m_sup.group(1).strip()
            else:
                m_pur = _PURCHASES_FROM_PATTERN.search(line_clean)
                if m_pur:
                    target_name = m_pur.group(1).strip()

            results.append(
                SupplyChainEdgeCandidate(
                    source_entity_id=issuer_id,
                    target_entity_name=target_name,
                    relation_type=rel,
                    evidence_sentence=line_clean[:200],
                    confidence=Decimal("0.85"),
                )
            )
    return tuple(results)


def materialize_supply_chain_exposure(
    connection: Connection[Any],
    *,
    run_id: str,
    cutoff: datetime | None = None,
    exposure_data: Sequence[dict[str, Any] | SupplyChainExposure],
) -> int:
    """Materialize batch supply chain exposure rows into mart.issuer_supply_chain_exposure."""
    as_of = cutoff or datetime.now(tz=UTC)
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
            source_status = "verified" if avail == "available" else "degraded"
            val_status = "accepted" if exposure_score is not None else "not_evaluated"
        else:
            issuer_id = str(item["issuer_id"])
            if "partners" in item:
                rec = supply_chain_exposure(item["partners"], entity_id=issuer_id, as_of=as_of)
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
                source_status = "verified" if avail == "available" else "degraded"
                val_status = "accepted" if exposure_score is not None else "not_evaluated"
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
                source_status = str(
                    item.get("source_evidence_status", "verified" if avail == "available" else "degraded")
                )
                val_status = str(
                    item.get("factor_validation_status", "accepted" if exposure_score is not None else "not_evaluated")
                )
            extractor = str(item.get("extractor", "graph:kg-edges:v1"))

        connection.execute(
            _INSERT_SQL,
            (
                run_id,
                issuer_id,
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
    tickers: Mapping[str, str],
) -> int:
    """Extract and materialize supply chain exposure for all issuers in a universe run."""
    count = 0
    has_edges_table = False
    try:
        with connection.cursor() as cur:
            cur.execute("select to_regclass('staging.kg_edges')")
            res = cur.fetchone()
            has_edges_table = bool(res and res[0])
    except Exception:
        has_edges_table = False

    for issuer_id, ticker in tickers.items():
        partners: list[SupplyChainPartner] = []
        if has_edges_table:
            rows = connection.execute(
                """
                select target_entity_id, target_entity_name, relation_type, revenue_share, confidence
                from staging.kg_edges
                where source_entity_id = %s and valid_from <= %s and (valid_to is null or valid_to > %s)
                """,
                (issuer_id, cutoff, cutoff),
            ).fetchall()
            for r in rows:
                p_id, p_name, r_type, rev_share, conf = r
                partners.append(
                    SupplyChainPartner(
                        partner_id=str(p_id or p_name),
                        partner_name=str(p_name),
                        relation_type=str(r_type),
                        revenue_share=Decimal(str(rev_share)) if rev_share is not None else None,
                        confidence=Decimal(str(conf)) if conf is not None else Decimal("0.8"),
                    )
                )

        record = supply_chain_exposure(partners, entity_id=issuer_id, as_of=cutoff)
        count += materialize_supply_chain_exposure(
            connection,
            run_id=run_id,
            cutoff=cutoff,
            exposure_data=[record],
        )
    return count
