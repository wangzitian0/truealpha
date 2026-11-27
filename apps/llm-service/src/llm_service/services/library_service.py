"""Library domain services: deterministic research reports, 1080x1440 XHS cards, and owner-scoped RLS documents."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from datetime import datetime
from typing import Any, Literal

from infra2_sdk.storage import create_s3_client, is_not_found, put_object_bytes, read_object_bytes
from pydantic import BaseModel, Field
from truealpha_contracts.common import require_stable_and_immutable
from truealpha_runtime.access import AccessContext
from truealpha_runtime.research_cards import (
    CardKind,
    build_card,
    build_strategy_context_from_report,
    enrich_card_metadata,
    paginate_card_series,
    render_card_json,
    verify_card_html,
)
from truealpha_runtime.research_report import (
    ReportSectionKind,
    ResearchReadPort,
    ResearchReportKind,
    ResearchReportRequest,
    build_research_report,
    render_report_markdown,
)
from truealpha_runtime.research_report_mart import MartResearchReadRepository
from truealpha_runtime.strategy_run_postgres import PostgresStrategyRunRepository

from llm_service.config import settings
from llm_service.services._db import with_mart_readonly, with_owner_scoped_runtime
from llm_service.services.strategy_service import (
    DASHBOARD_STRATEGY_ID,
    normalize_peg_convention,
    normalize_ranking_universe,
)

CARD_EXPORT_CACHE_CONTROL = "public, max-age=300"
_ISO_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


def assert_non_empty_text(value: str, label: str) -> str:
    trimmed = value.strip()
    if not trimmed:
        raise ValueError(f"{label} must be a non-empty string")
    return trimmed


def assert_stable_id(value: str, label: str) -> str:
    trimmed = assert_non_empty_text(value, label)
    return require_stable_and_immutable(trimmed, label)


def clamp_list_limit(limit: int | None) -> int:
    if limit is None:
        return 50
    return max(1, min(limit, 200))


def parse_before_cursor(created_at: str | None, document_id: str | None) -> tuple[str, str] | None:
    if created_at is None and document_id is None:
        return None
    if not created_at or not document_id or not _ISO_TIMESTAMP_RE.match(created_at):
        raise ValueError("before cursor must supply valid UTC created_at and document_id")
    datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    return created_at, assert_stable_id(document_id, "before.document_id")


def _new_id(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4()}"


class DocumentDto(BaseModel):
    document_id: str
    tenant_id: str
    owner_principal_id: str
    created_at: str


class DocumentRevisionDto(BaseModel):
    revision_id: str
    document_id: str
    tenant_id: str
    owner_principal_id: str
    source_artifact_id: str
    artifact_sha256: str
    artifact_byte_length: int
    artifact_content_type: str
    created_at: str


class DocumentCursorDto(BaseModel):
    created_at: str
    document_id: str


class DocumentListResponse(BaseModel):
    documents: list[DocumentDto] = Field(default_factory=list)
    next_before: DocumentCursorDto | None = None


class CreateDocumentRequest(BaseModel):
    source_artifact_id: str
    content_type: str = "text/plain; charset=utf-8"
    content: str


class DocumentDetailResponse(BaseModel):
    document: DocumentDto
    revisions: list[DocumentRevisionDto] = Field(default_factory=list)
    latest_content: str | None = None


class CreateDocumentResponse(BaseModel):
    document: DocumentDto
    revision: DocumentRevisionDto


class DeleteDocumentResponse(BaseModel):
    tombstone_id: str
    document_id: str
    created_at: str


class CardExportRequest(BaseModel):
    card_kind: CardKind = CardKind.RANKING
    universe: str | None = "all"
    issuer_id: str | None = None
    cutoff: str | None = None
    convention: str = "normalized"
    preset: str | None = "study-notes"
    page: int | None = None
    title: str | None = None
    format: Literal["html", "json"] = "html"
    persist: bool = True


class CardExportResponse(BaseModel):
    card_id: str
    card_kind: str
    render_mode: str
    document_id: str | None = None
    rendered_html: str | None = None
    card: dict[str, Any]


class LibraryReportResponse(BaseModel):
    report_id: str
    schema_version: str
    format: Literal["json", "markdown"] = "json"
    report: dict[str, Any] | None = None
    markdown: str | None = None


_s3_client_lock = threading.Lock()
_s3_client_cache: tuple[tuple[Any, Any], Any] | None = None


def _s3_client() -> Any:
    global _s3_client_cache
    key = (create_s3_client, settings.to_s3_settings())
    with _s3_client_lock:
        if _s3_client_cache is not None and _s3_client_cache[0] == key:
            return _s3_client_cache[1]
        client = create_s3_client(key[1])
        _s3_client_cache = (key, client)
        return client


def _put_s3_bytes(principal_id: str, data: bytes, content_type: str) -> tuple[str, str, int]:
    digest = hashlib.sha256(data).hexdigest()
    prefix = settings.s3_documents_prefix.strip("/") or "documents"
    key = f"{prefix}/{principal_id}/{digest[:2]}/{digest}"
    put_object_bytes(_s3_client(), bucket=settings.s3_bucket, key=key, data=data, content_type=content_type)
    return key, digest, len(data)


def _get_s3_bytes(key: str) -> bytes | None:
    try:
        return read_object_bytes(_s3_client(), bucket=settings.s3_bucket, key=key)
    except Exception as exc:  # noqa: BLE001
        if is_not_found(exc):
            return None
        raise


def list_documents(
    context: AccessContext,
    *,
    limit: int | None = None,
    before_created_at: str | None = None,
    before_document_id: str | None = None,
    database_url: str | None = None,
) -> DocumentListResponse:
    clamped = clamp_list_limit(limit)
    cursor = parse_before_cursor(before_created_at, before_document_id)

    def _q(cur: Any) -> DocumentListResponse:
        cur.execute(
            """
            select d.document_id, d.tenant_id, d.owner_principal_id, d.created_at,
                   to_char(d.created_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') as created_at_cursor
            from app.research_documents d
            where not exists (
              select 1 from app.research_document_tombstones t where t.document_id = d.document_id
            )
              and (%s::timestamptz is null or (d.created_at, d.document_id) < (%s::timestamptz, %s))
            order by d.created_at desc, d.document_id desc
            limit %s
            """,
            (cursor[0] if cursor else None, cursor[0] if cursor else None, cursor[1] if cursor else None, clamped),
        )
        rows = cur.fetchall()
        docs = [
            DocumentDto(
                document_id=str(r["document_id"]),
                tenant_id=str(r["tenant_id"]),
                owner_principal_id=str(r["owner_principal_id"]),
                created_at=r["created_at"].isoformat(),
            )
            for r in rows
        ]
        next_cursor = (
            DocumentCursorDto(created_at=str(rows[-1]["created_at_cursor"]), document_id=str(rows[-1]["document_id"]))
            if len(docs) == clamped and rows
            else None
        )
        return DocumentListResponse(documents=docs, next_before=next_cursor)

    return with_owner_scoped_runtime(context, _q, database_url=database_url)


def create_document(
    context: AccessContext,
    *,
    source_artifact_id: str,
    content_bytes: bytes,
    content_type: str = "text/plain; charset=utf-8",
    database_url: str | None = None,
) -> CreateDocumentResponse:
    clean_source = assert_stable_id(source_artifact_id, "source_artifact_id")
    clean_ct = assert_non_empty_text(content_type, "content_type")
    if not content_bytes:
        raise ValueError("document content must not be empty")
    object_key, sha256, byte_length = _put_s3_bytes(context.principal_id, content_bytes, clean_ct)

    def _q(cur: Any) -> CreateDocumentResponse:
        doc_id = _new_id("document")
        rev_id = _new_id("revision")
        cur.execute(
            """
            insert into app.research_documents (document_id, tenant_id, owner_principal_id)
            values (%s, %s, %s)
            returning document_id, tenant_id, owner_principal_id, created_at
            """,
            (doc_id, context.tenant_id, context.principal_id),
        )
        d = cur.fetchone()
        cur.execute(
            """
            insert into app.research_document_revisions
              (revision_id, document_id, tenant_id, owner_principal_id, source_artifact_id,
               artifact_sha256, artifact_byte_length, artifact_content_type, object_key)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            returning revision_id, document_id, tenant_id, owner_principal_id, source_artifact_id,
                      artifact_sha256, artifact_byte_length, artifact_content_type, created_at
            """,
            (
                rev_id,
                doc_id,
                context.tenant_id,
                context.principal_id,
                clean_source,
                sha256,
                byte_length,
                clean_ct,
                object_key,
            ),
        )
        rv = cur.fetchone()
        return CreateDocumentResponse(
            document=DocumentDto(
                document_id=str(d["document_id"]),
                tenant_id=str(d["tenant_id"]),
                owner_principal_id=str(d["owner_principal_id"]),
                created_at=d["created_at"].isoformat(),
            ),
            revision=DocumentRevisionDto(
                revision_id=str(rv["revision_id"]),
                document_id=str(rv["document_id"]),
                tenant_id=str(rv["tenant_id"]),
                owner_principal_id=str(rv["owner_principal_id"]),
                source_artifact_id=str(rv["source_artifact_id"]),
                artifact_sha256=str(rv["artifact_sha256"]),
                artifact_byte_length=int(rv["artifact_byte_length"]),
                artifact_content_type=str(rv["artifact_content_type"]),
                created_at=rv["created_at"].isoformat(),
            ),
        )

    return with_owner_scoped_runtime(context, _q, database_url=database_url)


def get_document_detail(
    context: AccessContext, *, document_id: str, database_url: str | None = None
) -> DocumentDetailResponse | None:
    def _q(cur: Any) -> tuple[DocumentDto, list[DocumentRevisionDto], str | None] | None:
        cur.execute(
            """
            select d.document_id, d.tenant_id, d.owner_principal_id, d.created_at
            from app.research_documents d
            where d.document_id = %s
              and not exists (select 1 from app.research_document_tombstones t where t.document_id = d.document_id)
            """,
            (document_id,),
        )
        d = cur.fetchone()
        if d is None:
            return None
        cur.execute(
            """
            select r.revision_id, r.document_id, r.tenant_id, r.owner_principal_id, r.source_artifact_id,
                   r.artifact_sha256, r.artifact_byte_length, r.artifact_content_type, r.object_key, r.created_at
            from app.research_document_revisions r
            where r.document_id = %s
              and not exists (select 1 from app.research_document_tombstones t where t.document_id = r.document_id)
            order by r.created_at asc, r.revision_id asc
            """,
            (document_id,),
        )
        rev_rows = cur.fetchall()
        revs = [
            DocumentRevisionDto(
                revision_id=str(rv["revision_id"]),
                document_id=str(rv["document_id"]),
                tenant_id=str(rv["tenant_id"]),
                owner_principal_id=str(rv["owner_principal_id"]),
                source_artifact_id=str(rv["source_artifact_id"]),
                artifact_sha256=str(rv["artifact_sha256"]),
                artifact_byte_length=int(rv["artifact_byte_length"]),
                artifact_content_type=str(rv["artifact_content_type"]),
                created_at=rv["created_at"].isoformat(),
            )
            for rv in rev_rows
        ]
        latest_key = str(rev_rows[-1]["object_key"]) if rev_rows else None
        return (
            DocumentDto(
                document_id=str(d["document_id"]),
                tenant_id=str(d["tenant_id"]),
                owner_principal_id=str(d["owner_principal_id"]),
                created_at=d["created_at"].isoformat(),
            ),
            revs,
            latest_key,
        )

    found = with_owner_scoped_runtime(context, _q, database_url=database_url)
    if found is None:
        return None
    doc, revs, latest_key = found
    raw = _get_s3_bytes(latest_key) if latest_key else None
    return DocumentDetailResponse(
        document=doc,
        revisions=revs,
        latest_content=raw.decode("utf-8", errors="replace") if raw is not None else None,
    )


def delete_document(
    context: AccessContext, *, document_id: str, database_url: str | None = None
) -> DeleteDocumentResponse | None:
    def _q(cur: Any) -> DeleteDocumentResponse | None:
        cur.execute(
            """
            insert into app.research_document_tombstones (tombstone_id, document_id, tenant_id, owner_principal_id)
            select %s, d.document_id, %s, %s
            from app.research_documents d
            where d.document_id = %s
            on conflict (document_id) do nothing
            returning tombstone_id, document_id, created_at
            """,
            (_new_id("tombstone"), context.tenant_id, context.principal_id, document_id),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "select tombstone_id, document_id, created_at from app.research_document_tombstones where document_id = %s",
                (document_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return DeleteDocumentResponse(
            tombstone_id=str(row["tombstone_id"]),
            document_id=str(row["document_id"]),
            created_at=row["created_at"].isoformat(),
        )

    return with_owner_scoped_runtime(context, _q, database_url=database_url)


def _lookup_governed_card_artifact(
    *,
    card_kind: str,
    subject_key: str,
    convention: str,
    universe: str,
    cutoff_at: datetime | None = None,
    database_url: str | None = None,
) -> tuple[str, str] | None:
    universe_candidates = [universe, f"universe-list:{universe}"] if universe and universe != "all" else [universe]
    sql = """
      select a.html, a.render_mode
      from mart.research_card_artifacts a
      join mart.served_head h on h.run_id = a.run_id
      where h.run_id is not null
        and a.card_kind = %s
        and a.subject_key = %s
        and a.convention = %s
        and a.universe = any(%s::text[])
        and (%s::timestamptz is null or a.cutoff_at = %s::timestamptz)
      order by h.advanced_at desc, h.sequence desc, a.run_id desc
      limit 1
    """

    def _q(cur: Any) -> tuple[str, str] | None:
        cur.execute(sql, (card_kind, subject_key, convention, universe_candidates, cutoff_at, cutoff_at))
        row = cur.fetchone()
        return (str(row["html"]), str(row["render_mode"])) if row else None

    return with_mart_readonly(_q, database_url=database_url)


def _resolve_governed_cutoff(
    context: AccessContext,
    *,
    research_repo: ResearchReadPort,
    strategy_id: str | None = None,
    database_url: str | None = None,
) -> datetime:
    sid = strategy_id or DASHBOARD_STRATEGY_ID
    strategy_repo = getattr(research_repo, "_strategy_repository", None)
    if strategy_repo is None and database_url is not None:
        strategy_repo = PostgresStrategyRunRepository(database_url=database_url)
    if strategy_repo is not None:
        latest = strategy_repo.get_latest(strategy_id=sid, context=context)
        decisions = getattr(latest, "decisions", None)
        if decisions:
            return max(d.cutoff_at for d in decisions)
    raise ValueError("governed cutoff_at is unavailable and no explicit cutoff was supplied")


def build_library_report(
    context: AccessContext,
    *,
    report_kind: ResearchReportKind = ResearchReportKind.THEME_RANKING,
    target_entity_ids: tuple[str, ...] = ("theme:large_model_value",),
    cutoff_at: datetime | None = None,
    section_kinds: tuple[ReportSectionKind, ...] = (),
    strategy_id: str | None = None,
    title: str | None = None,
    output_format: Literal["json", "markdown"] = "json",
    research_repo: ResearchReadPort | None = None,
    database_url: str | None = None,
) -> LibraryReportResponse:
    db_url = database_url or settings.database_url
    postgres_db_url = (
        database_url
        if database_url is not None
        else (getattr(research_repo, "_database_url", None) if research_repo is not None else settings.database_url)
    )
    active_repo = research_repo or MartResearchReadRepository(database_url=db_url)
    effective_cutoff = cutoff_at or _resolve_governed_cutoff(
        context,
        research_repo=active_repo,
        strategy_id=strategy_id,
        database_url=postgres_db_url,
    )
    report = build_research_report(
        ResearchReportRequest(
            report_kind=report_kind,
            target_entity_ids=target_entity_ids,
            cutoff_at=effective_cutoff,
            section_kinds=section_kinds,
            strategy_id=strategy_id,
            title=title,
        ),
        active_repo,
        context=context,
    )
    if output_format == "markdown":
        return LibraryReportResponse(
            report_id=report.report_id,
            schema_version=report.schema_version,
            format="markdown",
            markdown=render_report_markdown(report),
        )
    return LibraryReportResponse(
        report_id=report.report_id,
        schema_version=report.schema_version,
        format="json",
        report=report.model_dump(mode="json"),
    )


def _resolve_etf_cutoff(database_url: str | None) -> datetime | None:
    if not database_url:
        return None

    def _q(cur: Any) -> datetime | None:
        cur.execute(
            """
            select max(f.cutoff) as cutoff
            from mart.fund_virtual_company f
            join mart.served_head h on h.run_id = f.run_id
            where h.run_id is not null and not starts_with(h.universe_id, 'universe:canary-')
            """
        )
        row = cur.fetchone()
        return row.get("cutoff") if row else None

    return with_mart_readonly(_q, database_url=database_url)


def _route_card_report_spec(
    req: CardExportRequest,
) -> tuple[ResearchReportKind, tuple[str, ...], tuple[ReportSectionKind, ...], str]:
    if req.card_kind == CardKind.COMPANY:
        target = (req.issuer_id or "").strip() or "NVDA"
        return (
            ResearchReportKind.COMPANY,
            (target,),
            (ReportSectionKind.OPERATING_EFFICIENCY, ReportSectionKind.VALUATION, ReportSectionKind.STRATEGY_SUMMARY),
            target,
        )
    if req.card_kind == CardKind.SUPPLY_CHAIN:
        target = (req.issuer_id or "").strip() or "NVDA"
        return (
            ResearchReportKind.COMPANY,
            (target,),
            (ReportSectionKind.SUPPLY_CHAIN, ReportSectionKind.STRATEGY_SUMMARY),
            target,
        )
    if req.card_kind == CardKind.ETF:
        target = (req.issuer_id or "").strip() or "etf:qqq"
        return (
            ResearchReportKind.ETF,
            (target,),
            (ReportSectionKind.ETF_VIRTUAL_COMPANY, ReportSectionKind.VALUATION),
            target,
        )
    if req.card_kind == CardKind.COMPARISON:
        raw_targets = tuple(t.strip() for t in (req.issuer_id or "").split(",") if t.strip())
        if len(raw_targets) >= 2:
            return (
                ResearchReportKind.THEME_RANKING,
                raw_targets,
                (ReportSectionKind.VALUATION, ReportSectionKind.STRATEGY_SUMMARY),
                ",".join(raw_targets),
            )
    return (
        ResearchReportKind.THEME_RANKING,
        ("theme:large_model_value",),
        (ReportSectionKind.VALUATION, ReportSectionKind.STRATEGY_SUMMARY, ReportSectionKind.RANKING),
        "theme:large_model_value",
    )


def export_or_render_card(
    context: AccessContext,
    req: CardExportRequest,
    *,
    research_repo: ResearchReadPort | None = None,
    report_request: ResearchReportRequest | None = None,
    database_url: str | None = None,
) -> CardExportResponse:
    db_url = database_url or settings.database_url
    postgres_db_url = (
        database_url
        if database_url is not None
        else (getattr(research_repo, "_database_url", None) if research_repo is not None else settings.database_url)
    )
    active_repo = research_repo or MartResearchReadRepository(database_url=db_url)
    norm_convention = normalize_peg_convention(req.convention)
    norm_universe = normalize_ranking_universe(req.universe) or "all"
    if report_request is not None:
        effective_req = report_request
        target_cutoff_dt = report_request.cutoff_at
        subject_key = (
            report_request.target_entity_ids[0]
            if req.card_kind in {CardKind.COMPANY, CardKind.ETF, CardKind.SUPPLY_CHAIN}
            and report_request.target_entity_ids
            else "theme:large_model_value"
        )
    else:
        report_kind, targets, sections, subject_key = _route_card_report_spec(req)
        if req.cutoff:
            target_cutoff_dt = datetime.fromisoformat(req.cutoff.replace("Z", "+00:00"))
        elif report_kind is ResearchReportKind.ETF:
            target_cutoff_dt = _resolve_etf_cutoff(postgres_db_url) or _resolve_governed_cutoff(
                context,
                research_repo=active_repo,
                database_url=postgres_db_url,
            )
        else:
            target_cutoff_dt = _resolve_governed_cutoff(
                context,
                research_repo=active_repo,
                database_url=postgres_db_url,
            )

        effective_req = ResearchReportRequest(
            report_kind=report_kind,
            target_entity_ids=targets,
            cutoff_at=target_cutoff_dt,
            section_kinds=sections,
            strategy_id=DASHBOARD_STRATEGY_ID,
        )

    report = build_research_report(
        effective_req,
        active_repo,
        context=context,
    )
    card = build_card(report, req.card_kind, title=req.title)
    strategy_ctx = (
        build_strategy_context_from_report(report, strategy_id=DASHBOARD_STRATEGY_ID)
        if req.card_kind in (CardKind.RANKING, CardKind.COMPARISON, CardKind.STRATEGY_SUMMARY)
        else None
    )
    if req.format == "html" and req.page is not None:
        raise ValueError("Paginated HTML card export is not supported; use format='json' or omit page.")
    card = enrich_card_metadata(
        card,
        report,
        strategy_context=strategy_ctx,
        style_preset=req.preset,
    )
    if req.page is not None:
        pages = paginate_card_series(card, preset=req.preset)
        card = pages[max(0, min(req.page - 1, len(pages) - 1))]

    if req.format == "json":
        return CardExportResponse(
            card_id=card.card_id,
            card_kind=card.card_kind.value,
            render_mode="cold_verdict_json",
            card=json.loads(render_card_json(card)),
        )

    pre = (
        _lookup_governed_card_artifact(
            card_kind=req.card_kind.value,
            subject_key=subject_key,
            convention=norm_convention,
            universe=norm_universe,
            cutoff_at=target_cutoff_dt,
            database_url=postgres_db_url,
        )
        if req.page is None and postgres_db_url is not None
        else None
    )
    if pre is None:
        raise LookupError(
            "No verified LLM HTML card artifact exists for this governed run; export JSON or wait for card materialization."
        )
    html, render_mode = pre
    ok, violations = verify_card_html(card, html)
    if not ok:
        raise ValueError(f"governed card HTML artifact failed verification: {violations}")

    doc_id: str | None = None
    if req.persist:
        saved = create_document(
            context,
            source_artifact_id=card.card_id,
            content_bytes=html.encode("utf-8"),
            content_type="text/html; charset=utf-8",
            database_url=db_url,
        )
        doc_id = saved.document.document_id

    return CardExportResponse(
        card_id=card.card_id,
        card_kind=card.card_kind.value,
        render_mode=render_mode,
        document_id=doc_id,
        rendered_html=html,
        card=json.loads(render_card_json(card)),
    )


__all__ = [
    "CARD_EXPORT_CACHE_CONTROL",
    "CardExportRequest",
    "CardExportResponse",
    "CardKind",
    "CreateDocumentRequest",
    "CreateDocumentResponse",
    "DeleteDocumentResponse",
    "DocumentCursorDto",
    "DocumentDetailResponse",
    "DocumentDto",
    "DocumentListResponse",
    "DocumentRevisionDto",
    "LibraryReportResponse",
    "assert_non_empty_text",
    "assert_stable_id",
    "build_library_report",
    "clamp_list_limit",
    "create_document",
    "create_s3_client",
    "delete_document",
    "export_or_render_card",
    "get_document_detail",
    "list_documents",
    "parse_before_cursor",
    "put_object_bytes",
    "read_object_bytes",
]
