"""Writer for the Core Strategy replay's `mart.strategy_runs`/
`mart.strategy_decisions` tables (`db/migrations/0027_core_strategy_replay_mart.sql`, #26).

The migration's own comment already scoped this precisely: "this only
creates somewhere real for a future writer to land rows... Table shape
mirrors the `Decision` dataclass `run_strategy_smoke.py` already computes, so
a future writer has no reshaping to do." This module is that writer.

`mart.strategy_decisions` has no `confidence` column (the migration's first
cut omitted it) -- `Decision.confidence` is still included in the content
hash (the row's identity should reflect the full reproduced output, the same
way `strategy_run_id` references a definition hash without storing the whole
definition), it just isn't a persisted column. Adding one is a schema change,
not something this writer can retrofit silently.

Content-addressed IDs follow the same convention as
`datahub.production_topt.materialization`'s mart writers: `canonical_sha256`
of a payload dict, prefixed, insert with on-conflict-do-nothing, verify an
existing row matches rather than trusting it blindly -- append-only rows are
immutable evidence, so a genuine identity collision with different content is
a bug to raise on, not paper over.

`CLAIM_CEILING = "preview"` matches `core_strategy_replay.py`'s own framing
(fixture-sourced facts, no BacktestDataGateway yet) -- this writer persists
exactly what the replay claims to be, not more.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import Connection
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.strategy import LargeModelValueV0Definition

from data_engine.core_strategy_replay import CORPUS_SHA256, Decision

CLAIM_CEILING = "preview"


def _run_payload(
    definition: LargeModelValueV0Definition, *, executed_at: datetime, snapshot_id: str | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "strategy_key": definition.strategy_id,
        "strategy_version": definition.definition_version,
        "definition_content_sha256": definition.content_sha256,
        "corpus_sha256": CORPUS_SHA256,
        "claim_ceiling": CLAIM_CEILING,
        "executed_at": executed_at.isoformat(),
    }
    # Included only when set so existing fixture/preview runs keep their identity;
    # a run bound to a PIT snapshot gets a distinct run id (#395).
    if snapshot_id is not None:
        payload["snapshot_id"] = snapshot_id
    return payload


def write_strategy_run(
    connection: Connection[Any],
    definition: LargeModelValueV0Definition,
    *,
    executed_at: datetime,
    snapshot_id: str | None = None,
) -> str:
    payload = _run_payload(definition, executed_at=executed_at, snapshot_id=snapshot_id)
    content_sha256 = canonical_sha256(payload)
    run_id = f"strategy-run:{content_sha256}"
    inserted = connection.execute(
        """
        insert into mart.strategy_runs (
            strategy_run_id, content_sha256, strategy_key, strategy_version,
            definition_content_sha256, corpus_sha256, claim_ceiling, executed_at, snapshot_id
        ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (strategy_run_id) do nothing
        returning strategy_run_id
        """,
        (
            run_id,
            content_sha256,
            definition.strategy_id,
            definition.definition_version,
            definition.content_sha256,
            CORPUS_SHA256,
            CLAIM_CEILING,
            executed_at,
            snapshot_id,
        ),
    ).fetchone()
    if inserted is not None:
        return run_id
    existing = connection.execute(
        "select content_sha256 from mart.strategy_runs where strategy_run_id = %s", (run_id,)
    ).fetchone()
    if existing is None or existing[0] != content_sha256:
        raise ValueError("strategy run identity conflict")
    return run_id


def write_strategy_decision(connection: Connection[Any], decision: Decision, *, strategy_run_id: str) -> str:
    payload = {"strategy_run_id": strategy_run_id, **decision.to_json()}
    content_sha256 = canonical_sha256(payload)
    decision_id = f"strategy-decision:{content_sha256}"
    cutoff_at = datetime.fromisoformat(decision.cutoff_at.replace("Z", "+00:00"))
    inserted = connection.execute(
        """
        insert into mart.strategy_decisions (
            strategy_decision_id, content_sha256, strategy_run_id, issuer_id, cutoff_at,
            capital_adjusted_labor_efficiency, tier, current_price_to_sales, target_price_to_sales,
            valuation_gap, eligible, outcome, exclusion_reason, rank, target_weight
        ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (strategy_decision_id) do nothing
        returning strategy_decision_id
        """,
        (
            decision_id,
            content_sha256,
            strategy_run_id,
            decision.issuer_id,
            cutoff_at,
            decision.capital_adjusted_labor_efficiency,
            decision.tier,
            decision.current_price_to_sales,
            decision.target_price_to_sales,
            decision.valuation_gap,
            decision.eligible,
            decision.outcome,
            decision.exclusion_reason,
            decision.rank,
            decision.target_weight,
        ),
    ).fetchone()
    if inserted is not None:
        return decision_id
    existing = connection.execute(
        "select content_sha256 from mart.strategy_decisions where strategy_decision_id = %s", (decision_id,)
    ).fetchone()
    if existing is None or existing[0] != content_sha256:
        raise ValueError(f"strategy decision identity conflict: {decision.issuer_id}/{decision.cutoff_at}")
    return decision_id


def write_replay(
    connection: Connection[Any],
    decisions: list[Decision],
    definition: LargeModelValueV0Definition,
    *,
    executed_at: datetime,
) -> tuple[str, tuple[str, ...]]:
    """Persist one full replay: one `strategy_runs` row, one `strategy_decisions`
    row per decision. Idempotent -- replaying identical decisions against the
    same definition and executed_at reproduces the same run/decision IDs."""

    run_id = write_strategy_run(connection, definition, executed_at=executed_at)
    decision_ids = tuple(
        write_strategy_decision(connection, decision, strategy_run_id=run_id) for decision in decisions
    )
    return run_id, decision_ids
