"""Normalize SEC filing documents and non-semantic document envelopes."""

from __future__ import annotations

from datetime import UTC, date, datetime

from truealpha_contracts import canonical_sha256

from data_engine import raw_store
from data_engine.normalizers import lineage

MAPPING_VERSION = "sec-filing-document:1"
EXTRACTOR_VERSION = "document-envelope:1"


def normalize_document(
    conn,
    *,
    raw_fetch_id: int,
    issuer_id: str,
    accession: str,
    form: str,
    filing_period: date | None,
    document_name: str,
    source_url: str,
    knowable_at: datetime,
) -> tuple[int, int]:
    raw_row = conn.execute(
        "select payload_sha256 from raw.fetches where id = %s",
        (raw_fetch_id,),
    ).fetchone()
    if raw_row is None:
        raise LookupError(f"raw.fetches:{raw_fetch_id} does not exist")
    raw_ref = raw_store.raw_ref(raw_fetch_id)
    row = conn.execute(
        """
        insert into staging.filing_documents
            (issuer_id, accession, form, filing_period, document_name,
             document_sha256, source_url, transaction_time, recorded_at,
             confidence, source, raw_ref, mapping_version)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1, 'sec', %s, %s)
        on conflict do nothing returning id
        """,
        (
            issuer_id,
            accession,
            form,
            filing_period,
            document_name,
            raw_row[0],
            source_url,
            knowable_at,
            datetime.now(UTC),
            raw_ref,
            MAPPING_VERSION,
        ),
    ).fetchone()
    if row is None:
        row = conn.execute(
            """
            select id from staging.filing_documents
            where issuer_id = %s and accession = %s and document_name = %s
              and raw_ref = %s and mapping_version = %s
            """,
            (issuer_id, accession, document_name, raw_ref, MAPPING_VERSION),
        ).fetchone()
    if row is None:
        raise RuntimeError(f"could not persist filing document {accession}/{document_name}")
    document_id = row[0]
    lineage.link(
        conn,
        table="filing_documents",
        record_id=document_id,
        raw_ref=raw_ref,
        mapping_version=MAPPING_VERSION,
    )

    semantic_record_id = "extraction:" + canonical_sha256(
        {
            "issuer_id": issuer_id,
            "accession": accession,
            "document_name": document_name,
            "document_sha256": raw_row[0],
            "extractor_version": EXTRACTOR_VERSION,
        }
    )
    valid_on = filing_period or knowable_at.date()
    extraction = conn.execute(
        """
        insert into staging.filing_extractions
            (semantic_record_id, issuer_id, filing_document_id, extraction_type,
             payload, evidence_span, extractor_version, review_state, valid_time,
             transaction_time, recorded_at, confidence, source, raw_ref, mapping_version)
        values (%s, %s, %s, 'document_envelope', %s::jsonb, %s, %s,
                'rule_verified', daterange(%s::date, (%s::date + 1), '[)'),
                %s, %s, 1, 'sec', %s, %s)
        on conflict do nothing returning id
        """,
        (
            semantic_record_id,
            issuer_id,
            document_id,
            '{"semantic_claims":0}',
            f"document:{document_name};sha256:{raw_row[0]}",
            EXTRACTOR_VERSION,
            valid_on,
            valid_on,
            knowable_at,
            datetime.now(UTC),
            raw_ref,
            MAPPING_VERSION,
        ),
    ).fetchone()
    if extraction is None:
        extraction = conn.execute(
            """
            select id from staging.filing_extractions
            where semantic_record_id = %s and transaction_time = %s
              and raw_ref = %s and mapping_version = %s
            """,
            (semantic_record_id, knowable_at, raw_ref, MAPPING_VERSION),
        ).fetchone()
    if extraction is None:
        raise RuntimeError(f"could not persist filing envelope {semantic_record_id}")
    extraction_id = extraction[0]
    lineage.link(
        conn,
        table="filing_extractions",
        record_id=extraction_id,
        raw_ref=raw_ref,
        mapping_version=MAPPING_VERSION,
    )
    return document_id, extraction_id
