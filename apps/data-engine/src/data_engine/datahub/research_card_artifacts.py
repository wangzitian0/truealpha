"""Materialize prompt-driven Xiaohongshu 1080x1440 HTML cards into `mart.research_card_artifacts` (#44)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb
from truealpha_contracts.access import AccessContext, AuthenticationMethod, PrincipalKind
from truealpha_contracts.research_cards import CardKind, ResearchCard, build_card, render_card_json
from truealpha_contracts.research_report import (
    ResearchReportKind,
    ResearchReportRequest,
    build_research_report,
)
from truealpha_contracts.research_report_mart import MartResearchReadRepository

from data_engine.config import settings
from data_engine.sources.llm import Transport, generate_xhs_card_html

logger = logging.getLogger(__name__)


def _service_access_context(now: datetime) -> AccessContext:
    return AccessContext(
        context_id="ctx:data-engine:xhs-cards",
        principal_id="principal:data-engine",
        tenant_id="tenant:system",
        session_id="session:data-engine",
        authentication_method=AuthenticationMethod.SERVICE_IDENTITY,
        principal_kind=PrincipalKind.SERVICE,
        issued_at=now,
        expires_at=now + timedelta(days=1),
    )


def persist_card_artifact(
    connection: Connection[Any],
    *,
    run_id: str,
    card: ResearchCard,
    subject_key: str,
    convention: str,
    universe: str,
    html: str,
    render_mode: str,
    model: str | None,
    model_invocation_ref: str | None,
    prompt_sha256: str,
) -> None:
    """Persist a verified Xiaohongshu HTML card row into `mart.research_card_artifacts`."""
    card_payload = json.loads(render_card_json(card))
    connection.execute(
        """
        insert into mart.research_card_artifacts (
            run_id,
            card_id,
            card_kind,
            subject_key,
            convention,
            universe,
            cutoff_at,
            card_json,
            html,
            render_mode,
            model,
            model_invocation_ref,
            prompt_sha256
        )
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (run_id, card_kind, subject_key, convention, universe) do nothing
        """,
        (
            run_id,
            card.card_id,
            card.card_kind.value,
            subject_key,
            convention,
            universe,
            card.cutoff_at,
            Jsonb(card_payload),
            html,
            render_mode,
            model,
            model_invocation_ref,
            prompt_sha256,
        ),
    )


def materialize_head_research_cards(
    connection: Connection[Any],
    *,
    run_id: str,
    cutoff: datetime,
    universe: str,
    convention: str = "historical_cagr",
    max_entities: int = 3,
    database_url: str | None = None,
    transport: Transport | None = None,
) -> dict[str, int]:
    """Build and materialize governed Xiaohongshu 1080x1440 HTML cards (`ranking` + top `company` cards)
    for `run_id` using `glm-5.3-flash` through `sources/llm.py` and `verify_card_html`.
    """
    db_url = database_url or settings.database_url
    repository = MartResearchReadRepository(database_url=db_url)
    now = datetime.now(UTC)
    context = _service_access_context(now)

    cards_to_generate: list[tuple[str, ResearchCard]] = []

    ranking_req = ResearchReportRequest(
        report_kind=ResearchReportKind.THEME_RANKING,
        target_entity_ids=("theme:large_model_value",),
        cutoff_at=cutoff,
    )
    ranking_report = build_research_report(ranking_req, repository, context=context)
    ranking_card = build_card(ranking_report, CardKind.RANKING)
    cards_to_generate.append(("theme:large_model_value", ranking_card))

    top_subject_ids = [s.subject_id for s in ranking_report.subjects[:max_entities] if s.subject_id]
    for subject_id in top_subject_ids:
        company_req = ResearchReportRequest(
            report_kind=ResearchReportKind.COMPANY,
            target_entity_ids=(subject_id,),
            cutoff_at=cutoff,
        )
        company_report = build_research_report(company_req, repository, context=context)
        company_card = build_card(company_report, CardKind.COMPANY)
        cards_to_generate.append((subject_id, company_card))

    written = 0
    model_rendered = 0
    fallback_rendered = 0

    for subject_key, card in cards_to_generate:
        existing = connection.execute(
            """
            select 1 from mart.research_card_artifacts
            where run_id = %s and card_kind = %s and subject_key = %s and convention = %s and universe = %s
            """,
            (run_id, card.card_kind.value, subject_key, convention, universe),
        ).fetchone()
        if existing is not None:
            continue
        outcome = generate_xhs_card_html(
            connection,
            card=card,
            transport=transport,
            now=lambda: now,
        )
        persist_card_artifact(
            connection,
            run_id=run_id,
            card=card,
            subject_key=subject_key,
            convention=convention,
            universe=universe,
            html=outcome.html,
            render_mode=outcome.render_mode,
            model=outcome.model,
            model_invocation_ref=outcome.invocation_id,
            prompt_sha256=outcome.prompt_sha256,
        )
        written += 1
        if outcome.render_mode == "model_glm":
            model_rendered += 1
        else:
            fallback_rendered += 1

    return {
        "written": written,
        "model_glm": model_rendered,
        "deterministic_fallback": fallback_rendered,
    }
