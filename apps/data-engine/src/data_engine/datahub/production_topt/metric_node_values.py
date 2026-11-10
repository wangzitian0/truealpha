"""Materialize evaluated Polars wide-panel metric node values into `mart.metric_node_values` (#1413, #1610)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import polars as pl
from factors.panel import (
    ALL_ISSUER_CLASSES,
    FACTOR_VARIANTS,
    PANEL_EXPRESSIONS,
    PANEL_METRICS,
    AliasKind,
    FactorVariantSpec,
    IssuerClass,
    PanelMetricSpec,
    PeriodSemantics,
    SignPolicy,
    UnitFamily,
    build_wide_panel,
    evaluate_panel_variant,
    evaluate_wide_panel,
    leaf_consumed_columns,
    node_id_for,
    reachable_derived_columns,
    required_inputs,
    tree_id_for,
    variant_sha256,
)
from factors.production_topt import (
    GppeV0Definition,
    MetricAvailability,
    MetricFreshness,
    ToptCoreSnapshotInput,
)
from psycopg import Connection
from truealpha_contracts.common import canonical_sha256
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


def _spec_for_column(
    col: str,
    metrics: Mapping[str, PanelMetricSpec],
    expressions: Mapping[str, pl.Expr] = PANEL_EXPRESSIONS,
) -> PanelMetricSpec:
    if col in metrics:
        return metrics[col]
    is_derived = col in expressions
    return PanelMetricSpec(
        key=col,
        unit=UnitFamily.RATIO,
        period=PeriodSemantics.FISCAL_YEAR_FLOW,
        applicability=ALL_ISSUER_CLASSES,
        sign_policy=dict.fromkeys(ALL_ISSUER_CLASSES, SignPolicy.SIGN_IS_SIGNAL),
        expr=expressions.get(col),
        metric=col if not is_derived else None,
        input_key=col if not is_derived else None,
        snapshot_field=col if not is_derived else None,
    )


def _resolve_from_payload(
    spec: PanelMetricSpec,
    financial_payload: Mapping[str, Any],
) -> tuple[Decimal | None, str | None, Decimal | None] | None:
    for candidate_key in (spec.alias(AliasKind.METRIC), spec.alias(AliasKind.INPUT_KEY), spec.key):
        if candidate_key and candidate_key in financial_payload:
            parsed = _parse_decimal(financial_payload[candidate_key])
            if parsed is not None:
                if spec.key == "employees_total" and parsed <= 0:
                    return None, "nonpositive_headcount", None
                return parsed, None, None
    return None


def _resolve_market_component(
    spec: PanelMetricSpec,
    factor_input: ToptCoreSnapshotInput,
) -> tuple[Decimal | None, str | None, Decimal | None] | None:
    if spec.key not in ("price", "market_price", "shares_outstanding") or not factor_input.market_value_components:
        return None
    comp = next(
        (c for c in factor_input.market_value_components if c.listing_id == factor_input.listing_id),
        factor_input.market_value_components[0],
    )
    comp_metric = comp.market_price if spec.key in ("price", "market_price") else comp.shares_outstanding
    if comp_metric.availability is MetricAvailability.AVAILABLE and comp_metric.value is not None:
        return comp_metric.value, None, comp_metric.confidence
    return None


def _resolve_snapshot_metric(
    spec: PanelMetricSpec,
    factor_input: ToptCoreSnapshotInput,
) -> tuple[Decimal | None, str | None, Decimal | None] | None:
    field_name = spec.alias(AliasKind.TOPT_SNAPSHOT_FIELD) or spec.key
    metric = factor_input.metric_input(field_name) or factor_input.metric_input(spec.key)
    if metric is not None and metric.availability is MetricAvailability.AVAILABLE and metric.value is not None:
        if field_name in ("headcount", "employees_total") and metric.value <= 0:
            return None, "nonpositive_headcount", None
        return metric.value, None, metric.confidence
    return _resolve_market_component(spec, factor_input)


def _resolve_input_value(
    spec: PanelMetricSpec,
    *,
    factor_input: ToptCoreSnapshotInput,
    gppe_definition: GppeV0Definition,
    financial_payload: Mapping[str, Any],
) -> tuple[Decimal | None, str | None, Decimal | None]:
    parameter = spec.alias(AliasKind.DEFINITION_PARAMETER)
    if parameter is not None:
        val = getattr(gppe_definition, parameter, None)
        return (_parse_decimal(val), None if val is not None else f"missing_{parameter}", None)

    from_snapshot = _resolve_snapshot_metric(spec, factor_input)
    if from_snapshot is not None:
        return from_snapshot

    field_name = spec.alias(AliasKind.TOPT_SNAPSHOT_FIELD) or spec.key
    if hasattr(factor_input, field_name) and not financial_payload.get(field_name):
        if spec.default_when_missing is not None:
            return spec.default_when_missing, None, None
        return None, f"missing_{field_name}", None

    from_payload = _resolve_from_payload(spec, financial_payload)
    if from_payload is not None:
        return from_payload
    if spec.default_when_missing is not None:
        return spec.default_when_missing, None, None
    missing_label = field_name or spec.alias(AliasKind.INPUT_KEY) or spec.alias(AliasKind.METRIC) or spec.key
    return None, f"missing_{missing_label}", None


def _node_period(
    spec: PanelMetricSpec,
    *,
    factor_input: ToptCoreSnapshotInput,
    periods: tuple[date | None, date | None, date | None],
    financial_payload: Mapping[str, Any],
) -> tuple[str, str | None]:
    cutoff_iso = factor_input.cutoff.date().isoformat()
    if spec.period is PeriodSemantics.DEFINITIONAL:
        return "definitional", "definitional"
    if spec.period is PeriodSemantics.CUTOFF_INSTANT:
        return cutoff_iso, "cutoff"
    operating_end, revenue_end, _shares_end = periods
    tce_end_raw = financial_payload.get("tangible_equity_period_end")
    if isinstance(tce_end_raw, str) and tce_end_raw and "tce" in spec.key:
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


def _snapshot_leaf_confidence(factor_input: ToptCoreSnapshotInput) -> Decimal:
    leaf_confs = [
        m.confidence
        for m in (
            factor_input.gross_profit,
            factor_input.total_assets,
            factor_input.headcount,
            factor_input.revenue,
            factor_input.pre_provision_profit,
            *factor_input.inputs.values(),
        )
        if m is not None and m.availability is MetricAvailability.AVAILABLE and m.value is not None
    ]
    return (
        min(leaf_confs)
        if leaf_confs
        else min((cell.confidence for cell in factor_input.cell_inputs), default=Decimal("0"))
    )


def materialize_metric_node_values(
    connection: Connection[Any],
    *,
    factor_inputs: tuple[ToptCoreSnapshotInput, ...],
    gppe_definition: GppeV0Definition,
    tier_definition_id: str,
    periods_by_listing: Mapping[str, tuple[date | None, date | None, date | None]],
    evidence_by_listing: Mapping[str, InputEvidenceStatus],
    variants: Sequence[FactorVariantSpec] = FACTOR_VARIANTS,
    metrics: Mapping[str, PanelMetricSpec] = PANEL_METRICS,
    extra_expressions: Mapping[str, pl.Expr] | None = None,
) -> int:
    """Evaluate every applicable factor variant and any extra Polars `pl.Expr` columns over the wide panel."""
    if not factor_inputs:
        return 0
    all_obs_ids = sorted({oid for item in factor_inputs for oid in item.observation_ids})
    financial_by_listing = _load_financial_payloads_by_listing(connection, all_obs_ids)
    validation_status: FactorValidationStatus = factor_validation_status_for(
        (gppe_definition.definition_id, tier_definition_id)
    )
    variant_hashes: dict[tuple[str, str], str] = {
        (v.key, v.version): variant_sha256(v, metrics=metrics) for v in variants
    }
    inserted_count = 0
    for factor_input in factor_inputs:
        if factor_input.primary_listing_undeclared:
            continue
        issuer_class = IssuerClass(factor_input.operating_branch.value)
        freshness = _snapshot_freshness(factor_input)
        base_confidence = _snapshot_leaf_confidence(factor_input)
        financial_payload = financial_by_listing.get(factor_input.listing_id, {})
        periods = periods_by_listing.get(factor_input.listing_id, (None, None, None))
        source_evidence = evidence_by_listing.get(factor_input.listing_id, InputEvidenceStatus.DEGRADED)

        for variant in variants:
            if issuer_class not in variant.applicability:
                continue
            inserted_count += _materialize_issuer_tree(
                connection,
                metrics=metrics,
                tree=variant,
                tree_sha256=variant_hashes[(variant.key, variant.version)],
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

    if extra_expressions:
        inserted_count += _materialize_extra_expressions(
            connection,
            factor_inputs=factor_inputs,
            gppe_definition=gppe_definition,
            periods_by_listing=periods_by_listing,
            evidence_by_listing=evidence_by_listing,
            financial_by_listing=financial_by_listing,
            validation_status=validation_status,
            metrics=metrics,
            extra_expressions=extra_expressions,
        )
    return inserted_count


def _materialize_extra_expressions(
    connection: Connection[Any],
    *,
    factor_inputs: tuple[ToptCoreSnapshotInput, ...],
    gppe_definition: GppeV0Definition,
    periods_by_listing: Mapping[str, tuple[date | None, date | None, date | None]],
    evidence_by_listing: Mapping[str, InputEvidenceStatus],
    financial_by_listing: Mapping[str, Mapping[str, Any]],
    validation_status: FactorValidationStatus,
    metrics: Mapping[str, PanelMetricSpec],
    extra_expressions: Mapping[str, pl.Expr],
) -> int:
    all_expressions = {**PANEL_EXPRESSIONS, **extra_expressions}
    consumed_leaves: set[str] = set()
    for expr in extra_expressions.values():
        consumed_leaves.update(leaf_consumed_columns(expr, metrics=metrics, expressions=all_expressions))

    panel_rows: list[dict[str, Any]] = []
    meta_by_row: dict[tuple[str, str], tuple[ToptCoreSnapshotInput, dict[str, str], MetricFreshness]] = {}
    for factor_input in factor_inputs:
        if factor_input.primary_listing_undeclared:
            continue
        issuer_class = IssuerClass(factor_input.operating_branch.value)
        freshness = _snapshot_freshness(factor_input)
        base_conf = float(_snapshot_leaf_confidence(factor_input))
        financial_payload = financial_by_listing.get(factor_input.listing_id, {})
        periods = periods_by_listing.get(factor_input.listing_id, (None, None, None))
        period_str = (periods[0] or periods[1] or factor_input.cutoff.date()).isoformat()
        row: dict[str, Any] = {
            "issuer_id": factor_input.issuer_id,
            "period": period_str,
            "issuer_class": issuer_class.value,
        }
        reasons: dict[str, str] = {}
        for col in sorted(consumed_leaves):
            spec = _spec_for_column(col, metrics, all_expressions)
            val, reason, conf = _resolve_input_value(
                spec,
                factor_input=factor_input,
                gppe_definition=gppe_definition,
                financial_payload=financial_payload,
            )
            row[col] = float(val) if val is not None else None
            row[f"conf__{col}"] = float(conf) if conf is not None else base_conf
            if reason is not None:
                reasons[col] = reason
        panel_rows.append(row)
        meta_by_row[(factor_input.issuer_id, period_str)] = (factor_input, reasons, freshness)

    if not panel_rows:
        return 0
    wide = build_wide_panel(panel_rows)
    evaluated = evaluate_wide_panel(
        wide,
        targets=tuple(extra_expressions),
        expressions=all_expressions,
        include_confidence=True,
    )
    count = 0
    for eval_row in evaluated.iter_rows(named=True):
        issuer_id = str(eval_row["issuer_id"])
        period_str = str(eval_row["period"])
        factor_input, input_reasons, freshness = meta_by_row[(issuer_id, period_str)]
        source_evidence = evidence_by_listing.get(factor_input.listing_id, InputEvidenceStatus.DEGRADED)
        for name, expr in extra_expressions.items():
            spec = _spec_for_column(name, metrics, all_expressions)
            raw_val = eval_row.get(name)
            conf_val = eval_row.get(f"conf__{name}")
            leaves = leaf_consumed_columns(expr, metrics=metrics, expressions=all_expressions)
            node_reasons = [input_reasons[k] for k in leaves if k in input_reasons]
            if raw_val is None or node_reasons:
                persisted_val: Decimal | None = None
                avail_str = "unavailable"
                node_conf = Decimal("0")
                if not node_reasons:
                    node_reasons.append("undefined_operand")
            else:
                persisted_val = Decimal(str(raw_val))
                avail_str = "available"
                node_conf = Decimal(str(conf_val if conf_val is not None else 0.0))
            avail_status = availability_status_for(
                availability=avail_str,
                freshness=freshness.value,
                confidence=node_conf,
                reason_codes=node_reasons,
            )
            expr_sha = canonical_sha256({"key": name, "expr": expr.meta.serialize(format="json")})
            inserted = connection.execute(
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
                    issuer_id,
                    node_id_for(name),
                    name,
                    period_str,
                    "FY",
                    persisted_val,
                    spec.unit.value if spec.unit is not None else "ratio",
                    avail_status.value,
                    node_reasons,
                    source_evidence.value,
                    validation_status.value,
                    node_conf,
                    tree_id_for(name, "v1"),
                    "v1",
                    expr_sha,
                    list(factor_input.observation_ids),
                ),
            ).fetchone()
            if inserted is not None:
                count += 1
    return count


def _materialize_issuer_tree(
    connection: Connection[Any],
    *,
    tree: FactorVariantSpec,
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
    metrics: Mapping[str, PanelMetricSpec] = PANEL_METRICS,
) -> int:
    inputs: dict[str, Decimal | None] = {}
    input_reasons: dict[str, str] = {}
    input_confidences: dict[str, Decimal] = {}
    for spec in required_inputs(tree, issuer_class, metrics=metrics):
        value, reason, conf = _resolve_input_value(
            spec,
            factor_input=factor_input,
            gppe_definition=gppe_definition,
            financial_payload=financial_payload,
        )
        inputs[spec.key] = value
        if reason is not None:
            input_reasons[spec.key] = reason
        if conf is not None:
            input_confidences[spec.key] = conf

    freshness_reasons: list[str] = []
    if freshness is MetricFreshness.STALE:
        freshness_reasons.append("stale_input")
    elif freshness is MetricFreshness.UNKNOWN:
        freshness_reasons.append("unknown_freshness")

    evaluation = evaluate_panel_variant(tree, issuer_class=issuer_class, inputs=inputs, metrics=metrics)
    derived_keys = sorted(reachable_derived_columns(tree.root, issuer_class=issuer_class, metrics=metrics))
    count = 0
    for node_key in derived_keys:
        node = metrics[node_key]
        raw_val = evaluation.values.get(node_key)
        consumed_keys = leaf_consumed_columns(node_key, issuer_class=issuer_class, metrics=metrics)
        subtree_reasons = [input_reasons[k] for k in consumed_keys if k in input_reasons]
        node_reasons = list(dict.fromkeys([*subtree_reasons, *freshness_reasons]))
        if raw_val is None and not node_reasons:
            undef_reason = evaluation.undefined.get(node_key, "undefined_operand")
            node_reasons.append(undef_reason)
        if freshness_reasons or raw_val is None or subtree_reasons:
            persisted_value: Decimal | None = None
            availability_str = "unavailable"
            node_confidence = Decimal("0")
        else:
            persisted_value = raw_val
            availability_str = "available"
            consumed_confs = [input_confidences[k] for k in consumed_keys if k in input_confidences]
            node_confidence = min(consumed_confs) if consumed_confs else base_confidence
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
