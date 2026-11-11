"""Writer for Core Strategy replay `mart.strategy_runs` and `mart.strategy_decisions` (#26)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import Connection
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.execution import InputEvidenceStatus
from truealpha_contracts.strategy import LargeModelValueV0Definition

from data_engine.core_strategy_replay import CORPUS_SHA256, Decision
from data_engine.datahub.production_topt.status_dimensions import (
    decision_availability_status,
    statuses_without_raw_pointer,
)
from data_engine.datahub.risk_free import RiskFreeRate

CLAIM_CEILING = "preview"


def _insert_or_verify_sha(
    connection: Connection[Any],
    table: str,
    id_column: str,
    row_id: str,
    content_sha256: str,
    sql: str,
    params: tuple[Any, ...],
    conflict_msg: str,
) -> str:
    if connection.execute(sql, params).fetchone() is not None:
        return row_id
    existing = connection.execute(
        f"select content_sha256 from {table} where {id_column} = %s",  # noqa: S608
        (row_id,),
    ).fetchone()
    if existing is None or existing[0] != content_sha256:
        raise ValueError(conflict_msg)
    return row_id


def _run_payload(
    definition: LargeModelValueV0Definition,
    *,
    executed_at: datetime,
    snapshot_id: str | None = None,
    claim_ceiling: str = CLAIM_CEILING,
    risk_free_rate: RiskFreeRate | None = None,
) -> dict[str, Any]:
    corpus_sha = snapshot_id.split(":", 1)[1] if snapshot_id and ":" in snapshot_id else CORPUS_SHA256
    payload: dict[str, Any] = {
        "strategy_key": definition.strategy_id,
        "strategy_version": definition.definition_version,
        "definition_content_sha256": definition.content_sha256,
        "corpus_sha256": corpus_sha,
        "claim_ceiling": claim_ceiling,
        "executed_at": executed_at.isoformat(),
    }
    if snapshot_id is not None:
        payload["snapshot_id"] = snapshot_id
    if risk_free_rate is not None:
        payload["risk_free"] = {
            "series_id": risk_free_rate.series_id,
            "value": format(risk_free_rate.value.normalize(), "f"),
            "observation_date": risk_free_rate.observation_date.isoformat(),
            "source": risk_free_rate.source,
        }
    return payload


def _risk_free_columns(rate: RiskFreeRate | None) -> tuple[Any, ...]:
    if rate is None:
        return (None, None, None, None, None)
    return (rate.series_id, rate.value, rate.observation_date, rate.source, rate.fetch_id)


def write_strategy_run(
    connection: Connection[Any],
    definition: LargeModelValueV0Definition,
    *,
    executed_at: datetime,
    snapshot_id: str | None = None,
    claim_ceiling: str = CLAIM_CEILING,
    risk_free_rate: RiskFreeRate | None = None,
) -> str:
    payload = _run_payload(
        definition,
        executed_at=executed_at,
        snapshot_id=snapshot_id,
        claim_ceiling=claim_ceiling,
        risk_free_rate=risk_free_rate,
    )
    content_sha256 = canonical_sha256(payload)
    run_id = f"strategy-run:{content_sha256}"
    return _insert_or_verify_sha(
        connection,
        "mart.strategy_runs",
        "strategy_run_id",
        run_id,
        content_sha256,
        """
        insert into mart.strategy_runs (
            strategy_run_id, content_sha256, strategy_key, strategy_version,
            definition_content_sha256, corpus_sha256, claim_ceiling, executed_at, snapshot_id,
            risk_free_series_id, risk_free_value, risk_free_observation_date, risk_free_source,
            risk_free_fetch_id
        ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (strategy_run_id) do nothing
        returning strategy_run_id
        """,
        (
            run_id,
            content_sha256,
            definition.strategy_id,
            definition.definition_version,
            definition.content_sha256,
            payload["corpus_sha256"],
            payload["claim_ceiling"],
            executed_at,
            snapshot_id,
            *_risk_free_columns(risk_free_rate),
        ),
        "strategy run identity conflict",
    )


def write_strategy_decision(
    connection: Connection[Any],
    decision: Decision,
    *,
    strategy_run_id: str,
    definition: LargeModelValueV0Definition | None = None,
    source_evidence_status: InputEvidenceStatus | None = None,
) -> str:
    payload = {"strategy_run_id": strategy_run_id, **decision.to_json()}
    availability_status = decision_availability_status(
        eligible=decision.eligible,
        exclusion_reason=decision.exclusion_reason,
        value_present=decision.capital_adjusted_labor_efficiency is not None,
    )
    definition_ids = () if definition is None else (f"{definition.strategy_id}:{definition.content_sha256}",)
    default_evidence, factor_validation_status = statuses_without_raw_pointer(definition_ids)
    resolved_evidence_status = default_evidence if source_evidence_status is None else source_evidence_status
    content_sha256 = canonical_sha256(payload)
    decision_id = f"strategy-decision:{content_sha256}"
    cutoff_at = datetime.fromisoformat(decision.cutoff_at.replace("Z", "+00:00"))
    return _insert_or_verify_sha(
        connection,
        "mart.strategy_decisions",
        "strategy_decision_id",
        decision_id,
        content_sha256,
        """
        insert into mart.strategy_decisions (
            strategy_decision_id, content_sha256, strategy_run_id, issuer_id, cutoff_at,
            capital_adjusted_labor_efficiency, tier, current_price_to_sales, target_price_to_sales,
            valuation_gap, eligible, outcome, exclusion_reason, rank, target_weight, peg, peg_rank,
            peg_reason_codes, availability_status, source_evidence_status, factor_validation_status,
            labor_efficiency_metric, current_price_to_gross_profit, target_price_to_gross_profit
        ) values (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
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
            decision.peg,
            decision.peg_rank,
            list(decision.peg_reason_codes),
            availability_status.value,
            resolved_evidence_status.value,
            factor_validation_status.value,
            decision.labor_efficiency_metric,
            decision.current_price_to_gross_profit,
            decision.target_price_to_gross_profit,
        ),
        f"strategy decision identity conflict: {decision.issuer_id}/{decision.cutoff_at}",
    )


def bind_strategy_run_to_capture(connection: Connection[Any], *, strategy_run_id: str, capture_run_id: str) -> None:
    connection.execute(
        """
        insert into mart.strategy_run_capture_bindings (capture_run_id, strategy_run_id, bound_by)
        values (%s, %s, 'tick')
        on conflict (capture_run_id, strategy_run_id) do nothing
        """,
        (capture_run_id, strategy_run_id),
    )


def write_replay(
    connection: Connection[Any],
    decisions: list[Decision],
    definition: LargeModelValueV0Definition,
    *,
    executed_at: datetime,
    snapshot_id: str | None = None,
    claim_ceiling: str = CLAIM_CEILING,
    source_evidence_status: InputEvidenceStatus | None = None,
    risk_free_rate: RiskFreeRate | None = None,
) -> tuple[str, tuple[str, ...]]:
    run_id = write_strategy_run(
        connection,
        definition,
        executed_at=executed_at,
        snapshot_id=snapshot_id,
        claim_ceiling=claim_ceiling,
        risk_free_rate=risk_free_rate,
    )
    decision_ids = tuple(
        write_strategy_decision(
            connection,
            decision,
            strategy_run_id=run_id,
            definition=definition,
            source_evidence_status=source_evidence_status,
        )
        for decision in decisions
    )
    return run_id, decision_ids
