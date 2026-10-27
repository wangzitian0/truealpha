"""Materialize evaluated metric-forest node values into `mart.metric_node_values` (#1413)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from factors.forest import (
    FOREST,
    AliasKind,
    Forest,
    IssuerClass,
    MetricNode,
    MetricTree,
    NodeKind,
    PeriodSemantics,
    evaluate,
    required_inputs,
)
from factors.forest.model import leaf_and_derived_keys
from factors.production_topt import (
    GppeV0Definition,
    MetricAvailability,
    MetricFreshness,
    ToptCoreSnapshotInput,
)
from psycopg import Connection
from truealpha_contracts.execution import AvailabilityStatus, FactorValidationStatus, InputEvidenceStatus

from data_engine.datahub.production_topt.status_dimensions import (
    availability_status_for,
    factor_validation_status_for,
)


def _snapshot_freshness(factor_input: ToptCoreSnapshotInput) -> MetricFreshness:
    if any(cell.freshness is MetricFreshness.STALE for cell in factor_input.cell_inputs):
        return MetricFreshness.STALE
    if any(cell.freshness is MetricFreshness.UNKNOWN for cell in factor_input.cell_inputs):
        return MetricFreshness.UNKNOWN
    return MetricFreshness.FRESH


def _parse_decimal(raw: Any) -> Decimal | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def _resolve_input_value(
    node: MetricNode,
    *,
    factor_input: ToptCoreSnapshotInput,
    gppe_definition: GppeV0Definition,
    financial_payload: Mapping[str, Any],
) -> tuple[Decimal | None, str | None]:
    parameter = node.alias(AliasKind.DEFINITION_PARAMETER)
    if parameter is not None:
        val = getattr(gppe_definition, parameter, None)
        return (_parse_decimal(val), None if val is not None else f"missing_{parameter}")

    field = node.alias(AliasKind.TOPT_SNAPSHOT_FIELD)
    if field is not None and hasattr(factor_input, field):
        metric = getattr(factor_input, field)
        if metric is None or metric.availability is MetricAvailability.UNAVAILABLE or metric.value is None:
            return None, f"missing_{field}"
        if field == "headcount" and metric.value <= 0:
            return None, "nonpositive_headcount"
        return metric.value, None

    for candidate_key in (
        node.alias(AliasKind.METRIC),
        node.alias(AliasKind.INPUT_KEY),
        node.key,
    ):
        if candidate_key and candidate_key in financial_payload:
            parsed = _parse_decimal(financial_payload[candidate_key])
            if parsed is not None:
                if node.key == "employees_total" and parsed <= 0:
                    return None, "nonpositive_headcount"
                return parsed, None
    missing_label = field or node.alias(AliasKind.INPUT_KEY) or node.alias(AliasKind.METRIC) or node.key
    return None, f"missing_{missing_label}"


def _node_period(
    node: MetricNode,
    *,
    factor_input: ToptCoreSnapshotInput,
    periods: tuple[date | None, date | None, date | None],
    financial_payload: Mapping[str, Any],
) -> tuple[str, str | None]:
    cutoff_iso = factor_input.cutoff.date().isoformat()
    if node.period is PeriodSemantics.DEFINITIONAL:
        return "definitional", "definitional"
    if node.period is PeriodSemantics.CUTOFF_INSTANT:
        return cutoff_iso, "cutoff"
    operating_end, revenue_end, _shares_end = periods
    tce_end_raw = financial_payload.get("tangible_equity_period_end")
    if isinstance(tce_end_raw, str) and tce_end_raw and "tce" in node.key:
        return tce_end_raw, "FY"
    period_date = operating_end or revenue_end
    if period_date is not None:
        return period_date.isoformat(), "FY"
    return cutoff_iso, "FY"


def _load_financial_payloads_by_listing(
    connection: Connection[Any],
    observation_ids: list[str],
) -> dict[str, dict[str, Any]]:
    if not observation_ids:
        return {}
    rows = connection.execute(
        """
        select coalesce(p.normalized_payload->>'listing_id', o.subject_id), p.normalized_payload
        from staging.capture_normalized_observations o
        join staging.capture_observation_payloads p using (observation_id)
        where o.observation_id = any(%s) and o.semantic_type = 'financial-fact'
        """,
        (observation_ids,),
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for subject_id, payload in rows:
        if isinstance(payload, dict):
            result[str(subject_id)] = payload
    return result


def materialize_metric_node_values(
    connection: Connection[Any],
    *,
    factor_inputs: tuple[ToptCoreSnapshotInput, ...],
    gppe_definition: GppeV0Definition,
    tier_definition_id: str,
    periods_by_listing: Mapping[str, tuple[date | None, date | None, date | None]],
    evidence_by_listing: Mapping[str, InputEvidenceStatus],
    forest: Forest = FOREST,
) -> int:
    """Evaluate every applicable tree in `forest` for each issuer and persist derived node rows.

    A bank gets the nodes of both trees under their own node keys: the pinned banking tree, which the published
    column `gppe` holds, and the uniform tree, which is a separate diversity metric (#1518)."""
    if not factor_inputs:
        return 0
    all_obs_ids = sorted({oid for item in factor_inputs for oid in item.observation_ids})
    financial_by_listing = _load_financial_payloads_by_listing(connection, all_obs_ids)
    validation_status: FactorValidationStatus = factor_validation_status_for(
        (gppe_definition.definition_id, tier_definition_id)
    )
    tree_hashes: dict[tuple[str, str], str] = {
        (tree.key, tree.version): forest.tree_sha256(tree) for tree in forest.trees
    }
    inserted_count = 0
    for factor_input in factor_inputs:
        # #1518: an issuer with no declared primary listing is unavailable. Its identity is a placeholder,
        # so no node value rests on it.
        if factor_input.primary_listing_undeclared:
            continue
        issuer_class = IssuerClass(factor_input.operating_branch.value)
        freshness = _snapshot_freshness(factor_input)
        base_confidence = min(cell.confidence for cell in factor_input.cell_inputs)
        financial_payload = financial_by_listing.get(factor_input.listing_id, {})
        periods = periods_by_listing.get(factor_input.listing_id, (None, None, None))
        source_evidence = evidence_by_listing.get(factor_input.listing_id, InputEvidenceStatus.VERIFIED)

        for tree in forest.trees:
            if issuer_class not in tree.applicability:
                continue
            inserted_count += _materialize_issuer_tree(
                connection,
                forest=forest,
                tree=tree,
                tree_sha256=tree_hashes[(tree.key, tree.version)],
                factor_input=factor_input,
                issuer_class=issuer_class,
                gppe_definition=gppe_definition,
                financial_payload=financial_payload,
                periods=periods,
                freshness=freshness,
                base_confidence=base_confidence,
                source_evidence=source_evidence,
                validation_status=validation_status,
            )
    return inserted_count


def _materialize_issuer_tree(
    connection: Connection[Any],
    *,
    forest: Forest,
    tree: MetricTree,
    tree_sha256: str,
    factor_input: ToptCoreSnapshotInput,
    issuer_class: IssuerClass,
    gppe_definition: GppeV0Definition,
    financial_payload: Mapping[str, Any],
    periods: tuple[date | None, date | None, date | None],
    freshness: MetricFreshness,
    base_confidence: Decimal,
    source_evidence: InputEvidenceStatus,
    validation_status: FactorValidationStatus,
) -> int:
    inputs: dict[str, Decimal | None] = {}
    input_reasons: dict[str, str] = {}
    for node in required_inputs(forest, tree, issuer_class):
        value, reason = _resolve_input_value(
            node,
            factor_input=factor_input,
            gppe_definition=gppe_definition,
            financial_payload=financial_payload,
        )
        inputs[node.key] = value
        if reason is not None:
            input_reasons[node.key] = reason

    freshness_reasons: list[str] = []
    if freshness is MetricFreshness.STALE:
        freshness_reasons.append("stale_input")
    elif freshness is MetricFreshness.UNKNOWN:
        freshness_reasons.append("unknown_freshness")

    evaluation = evaluate(forest, tree, issuer_class=issuer_class, inputs=inputs)
    derived_keys = sorted(key for key in leaf_and_derived_keys(tree) if forest.node(key).kind is NodeKind.DERIVED)
    count = 0
    for node_key in derived_keys:
        node = forest.node(node_key)
        raw_val = evaluation.values.get(node_key)
        node_reasons = list(dict.fromkeys([*input_reasons.values(), *freshness_reasons]))
        if raw_val is None and not node_reasons:
            undef_reason = evaluation.undefined.get(node_key, "undefined_operand")
            node_reasons.append(undef_reason)
        if freshness_reasons or raw_val is None or input_reasons:
            persisted_value: Decimal | None = None
            availability_str = "unavailable"
            node_confidence = Decimal("0")
        else:
            persisted_value = raw_val
            availability_str = "available"
            node_confidence = base_confidence
        avail_status: AvailabilityStatus = availability_status_for(
            availability=availability_str,
            freshness=freshness.value,
            confidence=node_confidence,
            reason_codes=node_reasons,
        )
        period_str, period_tag = _node_period(
            node,
            factor_input=factor_input,
            periods=periods,
            financial_payload=financial_payload,
        )
        unit_str = node.unit.value if node.unit is not None else None
        row = connection.execute(
            """
            insert into mart.metric_node_values (
                run_id, issuer_id, node_id, node_key, period, period_tag,
                value, unit, availability_status, reason_codes,
                source_evidence_status, factor_validation_status,
                confidence, tree_id, tree_version, tree_sha256,
                input_observation_ids
            ) values (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s,
                %s, %s, %s, %s,
                %s
            )
            on conflict (run_id, issuer_id, node_id, period) do nothing
            returning node_id
            """,
            (
                factor_input.run_id,
                factor_input.issuer_id,
                node.node_id,
                node.key,
                period_str,
                period_tag,
                persisted_value,
                unit_str,
                avail_status.value,
                node_reasons,
                source_evidence.value,
                validation_status.value,
                node_confidence,
                tree.tree_id,
                tree.version,
                tree_sha256,
                list(factor_input.observation_ids),
            ),
        ).fetchone()
        if row is not None:
            count += 1
    return count
