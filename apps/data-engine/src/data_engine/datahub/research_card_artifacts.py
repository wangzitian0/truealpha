"""Materialize verified Xiaohongshu HTML cards into `mart.research_card_artifacts`."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb
from truealpha_contracts.access import AccessContext, AuthenticationMethod, PrincipalKind
from truealpha_contracts.research_cards import CardKind, ResearchCard, build_card, render_card_json
from truealpha_contracts.research_report import ResearchReportKind, ResearchReportRequest, build_research_report
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
    connection.execute(
        "insert into mart.research_card_artifacts (run_id, card_id, card_kind, subject_key, convention, universe, "
        "cutoff_at, card_json, html, render_mode, model, model_invocation_ref, prompt_sha256) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "on conflict (run_id, card_kind, subject_key, convention, universe) do nothing",
        (
            run_id,
            card.card_id,
            card.card_kind.value,
            subject_key,
            convention,
            universe,
            card.cutoff_at,
            Jsonb(json.loads(render_card_json(card))),
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
    repository = MartResearchReadRepository(database_url=database_url or settings.database_url)
    now = datetime.now(UTC)
    context = _service_access_context(now)

    def _card(kind: ResearchReportKind, card_kind: CardKind, subject: str) -> tuple[Any, ResearchCard]:
        report = build_research_report(
            ResearchReportRequest(report_kind=kind, target_entity_ids=(subject,), cutoff_at=cutoff),
            repository,
            context=context,
        )
        return report, build_card(report, card_kind)

    ranking_report, ranking_card = _card(ResearchReportKind.THEME_RANKING, CardKind.RANKING, "theme:large_model_value")
    cards_to_generate = [("theme:large_model_value", ranking_card)]
    for subject_id in [sub.subject_id for sub in ranking_report.subjects[:max_entities] if sub.subject_id]:
        cards_to_generate.append((subject_id, _card(ResearchReportKind.COMPANY, CardKind.COMPANY, subject_id)[1]))

    counts = {"written": 0, "model_glm": 0, "deterministic_fallback": 0}
    for subject_key, card in cards_to_generate:
        existing = connection.execute(
            "select 1 from mart.research_card_artifacts "
            "where run_id = %s and card_kind = %s and subject_key = %s and convention = %s and universe = %s",
            (run_id, card.card_kind.value, subject_key, convention, universe),
        ).fetchone()
        if existing is not None:
            continue
        outcome = generate_xhs_card_html(connection, card=card, transport=transport, now=lambda: now)
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
        counts["written"] += 1
        counts["model_glm" if outcome.render_mode == "model_glm" else "deterministic_fallback"] += 1
    return counts
