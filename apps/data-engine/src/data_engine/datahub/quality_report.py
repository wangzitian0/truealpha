"""Row-complete DataHub quality report for a capture run (#61 / #404)."""

from __future__ import annotations

import functools
import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol

import psycopg
from factors.panel import AliasKind, IssuerClass, bound_tree, operating_numerator_node, required_inputs
from factors.production_topt import OperatingBranch
from pydantic import ValidationError
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.models import RawObjectRef
from truealpha_contracts.reconciliation import (
    ReconciliationCell,
    ReconciliationOutcome,
    ReconciliationResult,
    SourceAssertion,
    reconcile_source_assertions,
)
from truealpha_contracts.reconciliation_policies import (
    FIELD_RECONCILIATION_POLICIES,
    FINANCIAL_FACT_RECONCILIATION_POLICY,
    RECONCILIATION_POLICY,
)
from truealpha_contracts.strategy import load_live_strategy_definition
from truealpha_contracts.universe import SubjectKind, SubjectRef
from truealpha_runtime import S3RawObjectStore

from data_engine.datahub.production_topt.executor import SERVED_BY_FAILOVER
from data_engine.datahub.production_topt.materialization import (
    FinancialFactPayload,
    IdentityPayload,
    MarketPricePayload,
)
from data_engine.datahub.production_topt.parser_identity import PARSER_VERSION_HISTORY
from data_engine.datahub.production_topt.source_registrations import (
    RELEASE_SEMANTICS,
    SOURCE_BY_PARSER,
    registration_for,
)
from data_engine.datahub.repository import persist_run_report
from data_engine.datahub.standards.listing_unit_ratio import OUTCOME_NOT_MEASURED, OUTCOMES, outcome_of_payload

_HEADLINE_FIELD = "close"
PRICE_BAR_FIELDS: tuple[str, ...] = ("open", "high", "low", "close", "volume")
FIELD_UNITS: dict[str, str] = {"open": "USD", "high": "USD", "low": "USD", "close": "USD", "volume": "shares"}
_SOURCE_BY_PARSER = SOURCE_BY_PARSER
_IDENTITY_SEMANTICS = RELEASE_SEMANTICS
_FACTOR_REQUIRED_SEMANTICS: dict[str, tuple[str, ...]] = {"gross_profit_per_employee": ("financial-fact",)}

_PER_EMPLOYEE_FLOOR = Decimal("1000")
_PER_EMPLOYEE_CEILING = Decimal("20000000")
_PRIMARY_PRICE_SOURCE = RECONCILIATION_POLICY.source_priority[0]

_FINANCIAL_FACT_REGISTRATION = registration_for("financial-fact")
_PRIMARY_FINANCIAL_SOURCE = f"{_FINANCIAL_FACT_REGISTRATION.source_id}:{_FINANCIAL_FACT_REGISTRATION.version}"
_PRIMARY_FINANCIAL_ORIGIN_GROUP = (
    f"origin:{_FINANCIAL_FACT_REGISTRATION.source_id}:{_FINANCIAL_FACT_REGISTRATION.version}"
)
_PRIMARY_FINANCIAL_VINTAGES = frozenset(PARSER_VERSION_HISTORY)

FINANCIAL_FACT_FUSION_FIELDS: dict[str, tuple[str, str]] = {
    "revenue": ("revenue", "revenue_period_end"),
    "gross_profit": ("gross_profit", "operating_period_end"),
    "net_income": ("net_income", "vintage.net_income.period_end"),
    "total_assets": ("total_assets", "vintage.total_assets.period_end"),
}
_ISO_CURRENCY = re.compile(r"[A-Z]{3}")

_FINANCIAL_OBLIGATION_METRICS: frozenset[str] = frozenset(
    {
        "gross_profit",
        "total_assets",
        "shares_outstanding",
        "revenue",
        "net_income",
        "headcount",
        "pre_provision_profit",
    }
)

_CORE_COMPLETE_LISTINGS_SQL = """
    select distinct coalesce(identity.listing_id, member->>'listing_id')
    from staging.topt_core_snapshots s
    cross join lateral jsonb_array_elements(s.payload->'members') member
    left join mart.entity_identity identity on identity.entity_id::text = member->>'listing_id'
    where s.run_id = %s
      and exists (
          select 1 from mart.topt_core_results r
          where r.run_id = s.run_id and r.issuer_id = member->>'issuer_id' and r.availability = 'available'
      )
    order by 1
"""


def _operating_numerator_field(branch: OperatingBranch) -> str:
    issuer_class = IssuerClass(branch.value)
    node = operating_numerator_node(bound_tree(load_live_strategy_definition(), issuer_class), issuer_class)
    field = node.alias(AliasKind.TOPT_SNAPSHOT_FIELD)
    assert field is not None, f"the numerator {node.key} names no snapshot field"
    return field


_FINANCIAL_FACT_OPERATING_NUMERATOR = {branch: _operating_numerator_field(branch) for branch in OperatingBranch}


def _quantized_ratio(numerator: int, denominator: int) -> str:
    return str((Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.0001"))) if denominator else "0"


def _factor_availability(
    conn: psycopg.Connection[Any], run_id: str, usable_by_subject: Mapping[str, Mapping[str, bool]]
) -> dict[str, dict[str, Any]]:
    universe = sorted(usable_by_subject)
    core_count_row = conn.execute("select count(*) from mart.topt_core_results where run_id = %s", (run_id,)).fetchone()
    core_count = core_count_row[0] if core_count_row is not None else 0
    complete_ids = (
        sorted(
            str(row[0]) for row in conn.execute(_CORE_COMPLETE_LISTINGS_SQL, (run_id,)).fetchall() if row[0] is not None
        )
        if core_count > 0
        else []
    )
    ratio = _quantized_ratio(len(complete_ids), len(universe)) if universe else "0"
    return {
        "gross_profit_per_employee": {
            "status": "computed" if core_count > 0 else "not_computed",
            "required_semantics": list(_FACTOR_REQUIRED_SEMANTICS["gross_profit_per_employee"]),
            "complete_subjects": len(complete_ids),
            "complete_subject_ids": complete_ids,
            "universe_subjects": len(universe),
            "ratio": ratio,
        }
    }


@functools.cache
def _core_payload_fields(branch: OperatingBranch) -> tuple[str, ...]:
    issuer_class = IssuerClass(branch.value)
    tree = bound_tree(load_live_strategy_definition(), issuer_class)
    fields = (node.alias(AliasKind.TOPT_SNAPSHOT_FIELD) for node in required_inputs(tree, issuer_class))
    return tuple(field for field in fields if field is not None)


def _has_usable_value(semantic_type: str, payload: dict[str, Any] | None) -> bool:
    if payload is None:
        return False
    try:
        if semantic_type in _IDENTITY_SEMANTICS:
            IdentityPayload.model_validate(payload)
            return True
        if semantic_type == "market-price":
            return MarketPricePayload.model_validate(payload).close is not None
        if semantic_type == "financial-fact":
            fact = FinancialFactPayload.model_validate(payload)
            return all(
                getattr(fact, name) is not None
                for name in (*_core_payload_fields(fact.operating_branch), "revenue", "shares_outstanding")
            )
    except ValidationError:
        return False
    return False


def _observation_usable(semantic_type: str, payload: dict[str, Any] | None) -> bool:
    return _has_usable_value(semantic_type, payload)


def _plausibility_violations(fact: FinancialFactPayload) -> list[str]:
    violated: list[str] = []
    if fact.gross_profit is not None and fact.revenue is not None and fact.gross_profit > fact.revenue:
        violated.append("gross_profit_exceeds_revenue")
    if fact.pre_provision_profit is not None and fact.revenue is not None and fact.pre_provision_profit > fact.revenue:
        violated.append("pre_provision_profit_exceeds_revenue")
    for name in ("headcount", "total_assets", "shares_outstanding"):
        value = getattr(fact, name)
        if value is not None and value <= 0:
            violated.append(f"nonpositive_{name}")
    numerator = getattr(fact, _FINANCIAL_FACT_OPERATING_NUMERATOR[fact.operating_branch])
    if numerator is not None and fact.headcount is not None and fact.headcount > 0:
        if not (_PER_EMPLOYEE_FLOOR <= numerator / fact.headcount <= _PER_EMPLOYEE_CEILING):
            violated.append("per_employee_outside_domain")
    return violated


class _ObjectReader(Protocol):
    def get(self, ref: RawObjectRef) -> bytes: ...


class _PointerDereferencer:
    def __init__(self, store: _ObjectReader | None = None) -> None:
        self._store = store
        self._resolved = store is not None
        self._cache: dict[tuple[str, str], bool] = {}

    def _reader(self) -> _ObjectReader | None:
        if not self._resolved:
            self._resolved = True
            try:
                store = S3RawObjectStore()
                store.ensure_bucket(create=False)
                self._store = store
            except Exception:
                self._store = None
        return self._store

    def dereferences(self, *, object_uri: str, sha256: str, byte_length: int, content_type: str) -> bool:
        cache_key = (object_uri, sha256)
        if cache_key in self._cache:
            return self._cache[cache_key]
        reader = self._reader()
        result = False
        bucket, _, key = object_uri.removeprefix("s3://").partition("/")
        if reader is not None and bucket and key:
            try:
                body = reader.get(
                    RawObjectRef(
                        bucket=bucket, key=key, sha256=sha256, byte_length=byte_length, content_type=content_type
                    )
                )
                result = hashlib.sha256(body).hexdigest() == sha256 and len(body) == byte_length
            except Exception:
                result = False
        self._cache[cache_key] = result
        return result


def listing_ratio_cell(payload: Mapping[str, Any], *, ticker: str | None = None) -> dict[str, Any]:
    return {
        "issuer_id": payload.get("issuer_id"),
        "listing_id": payload.get("listing_id"),
        "ticker": payload.get("ticker") if payload.get("ticker") is not None else ticker,
        "outcome": outcome_of_payload(payload),
        "listing_units_per_share": payload.get("listing_units_per_share"),
        "pattern": payload.get("ratio_pattern"),
        "title": payload.get("ratio_title"),
        "reason": payload.get("ratio_reason"),
        "accession": payload.get("ratio_accession"),
        "form": payload.get("ratio_form"),
        "filed": payload.get("ratio_filed"),
    }


def record_listing_ratio(
    cells: dict[str, dict[str, Any]], subject_id: str, payload: Mapping[str, Any], *, ticker: str | None = None
) -> None:
    existing_ticker = cells[subject_id].get("ticker") if subject_id in cells else None
    cell = listing_ratio_cell(payload, ticker=ticker if ticker is not None else existing_ticker)
    if cell["outcome"] != OUTCOME_NOT_MEASURED or subject_id not in cells:
        cells[subject_id] = cell
    elif cell["ticker"] is not None and cells[subject_id].get("ticker") is None:
        cells[subject_id]["ticker"] = cell["ticker"]


def listing_ratio_report(cells: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    counts = dict.fromkeys(OUTCOMES, 0)
    for cell in cells.values():
        counts[cell["outcome"]] += 1
    return {"counts": counts, "cells": dict(sorted(cells.items()))}


@dataclass
class _Cell:
    available: bool = False
    lineage_complete: bool = False
    fresh: bool = False
    confidence: Decimal | None = None


def latest_run(conn: psycopg.Connection[Any]) -> str:
    row = conn.execute(
        "select run_id from mart.topt_capture_status order by cutoff desc, run_id desc limit 1"
    ).fetchone()
    if row is None:
        raise ValueError("no capture run found")
    return row[0]


def _obligation_cell_confidence(
    cell_confidences: Mapping[tuple[str, str], Decimal], obligation_subject: str, semantic_type: str
) -> Decimal | None:
    if semantic_type == "market-price":
        return cell_confidences.get((obligation_subject, "close"))
    if semantic_type in ("listing-identity", "universe-membership"):
        return cell_confidences.get((obligation_subject, semantic_type))
    if semantic_type == "financial-fact":
        fin_confs = [
            conf
            for (subj, metric), conf in cell_confidences.items()
            if subj == obligation_subject and metric in _FINANCIAL_OBLIGATION_METRICS and conf > Decimal("0")
        ]
        return min(fin_confs) if fin_confs else Decimal("0")
    return None


def _tickers_by_subject(rows: Sequence[Sequence[Any]]) -> dict[str, str]:
    tickers: dict[str, str] = {}
    for row in rows:
        sem_type, ob_subject, subj_id, row_payload = row[1], row[2], row[3], row[5]
        if sem_type in _IDENTITY_SEMANTICS and isinstance(row_payload, Mapping):
            raw_ticker = row_payload.get("ticker")
            if isinstance(raw_ticker, str) and raw_ticker.strip():
                t = raw_ticker.strip()
                tickers[str(ob_subject)] = t
                if subj_id is not None:
                    tickers[str(subj_id)] = t
    return tickers


def _record_plausibility(plausibility: dict[str, dict[str, Any]], subject_id: str, payload: Mapping[str, Any]) -> None:
    try:
        violations = _plausibility_violations(FinancialFactPayload.model_validate(payload))
    except ValidationError:
        return
    cell_grades = plausibility.setdefault(subject_id, {"outcome": "plausible", "violated": []})
    merged = sorted(set(cell_grades["violated"]) | set(violations))
    cell_grades["violated"] = merged
    cell_grades["outcome"] = "implausible" if merged else "plausible"


def build_report(
    conn: psycopg.Connection[Any], run_id: str, *, object_store: _ObjectReader | None = None
) -> dict[str, Any]:
    status = conn.execute(
        """
        select obligation_count, terminal_count, success_count, unchanged_count,
               unavailable_count, skipped_count, failed_count, complete
        from mart.topt_capture_status where run_id = %s
        """,
        (run_id,),
    ).fetchone()
    if status is None:
        raise ValueError(f"no capture status for run {run_id}")
    requested = status[0]

    rows = conn.execute(
        """
        select ob.obligation_id,
               regexp_replace(ob.capture_requirement_id, ':v1$', '') as semantic_type,
               ob.subject_id as obligation_subject,
               o.subject_id, o.observation_id, p.normalized_payload, meta.freshness_state,
               f.object_uri, f.payload_sha256, f.byte_length, f.content_type
        from raw.capture_obligations ob
        left join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
        left join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
        left join staging.capture_observation_payloads p on p.observation_id = o.observation_id
        left join raw.capture_source_vintages v on v.source_vintage_id = o.source_vintage_id
        left join raw.fetches f on f.id = v.raw_fetch_id
        left join mart.topt_capture_meta_info meta on meta.obligation_id = ob.obligation_id
        where ob.run_id = %s
        order by ob.obligation_id, o.observation_id
        """,
        (run_id,),
    ).fetchall()

    from data_engine.datahub.confidence_report import evaluate_and_persist_run_cell_confidence

    cell_confidences = evaluate_and_persist_run_cell_confidence(conn, run_id)
    pointers = _PointerDereferencer(object_store)
    cells: dict[str, _Cell] = {}
    plausibility: dict[str, dict[str, Any]] = {}
    listing_ratio: dict[str, dict[str, Any]] = {}
    tickers_by_subject = _tickers_by_subject(rows)
    usable_by_subject: dict[str, dict[str, bool]] = {}
    for (
        obligation_id,
        semantic_type,
        obligation_subject,
        subject_id,
        observation_id,
        payload,
        freshness_state,
        object_uri,
        payload_sha256,
        byte_length,
        content_type,
    ) in rows:
        cell = cells.setdefault(obligation_id, _Cell())
        subject_semantics = usable_by_subject.setdefault(str(obligation_subject), {})
        subject_semantics.setdefault(semantic_type, False)
        if observation_id is None:
            continue
        cell.fresh = cell.fresh or freshness_state == "fresh"
        obs_conf = _obligation_cell_confidence(cell_confidences, str(obligation_subject), semantic_type)
        if obs_conf is not None:
            cell.confidence = obs_conf if cell.confidence is None else max(cell.confidence, obs_conf)
        if not cell.available:
            cell.available = _observation_usable(semantic_type, payload)
        if cell.available:
            subject_semantics[semantic_type] = True
        if semantic_type == "financial-fact" and payload is not None and subject_id is not None:
            record_listing_ratio(
                listing_ratio,
                str(subject_id),
                payload,
                ticker=tickers_by_subject.get(str(subject_id), tickers_by_subject.get(str(obligation_subject))),
            )
            _record_plausibility(plausibility, str(subject_id), payload)
        if not cell.lineage_complete and payload is not None and object_uri is not None:
            cell.lineage_complete = pointers.dereferences(
                object_uri=object_uri, sha256=payload_sha256, byte_length=byte_length, content_type=content_type
            )

    available = sum(1 for cell in cells.values() if cell.available)
    lineage_complete = sum(1 for cell in cells.values() if cell.lineage_complete)
    fresh = sum(1 for cell in cells.values() if cell.fresh)
    reconciliation = _reconcile_market_price_cells(conn, run_id)
    independent = sum(1 for cell in reconciliation.values() if cell["outcome"] == ReconciliationOutcome.AGREED.value)
    served_by_failover = sum(1 for cell in reconciliation.values() if SERVED_BY_FAILOVER in cell)
    field_reconciliation = _field_reconciliation(reconciliation)
    financial_reconciliation = _reconcile_financial_fact_cells(conn, run_id)
    financial_independent = sum(
        1 for cell in financial_reconciliation.values() if cell["outcome"] == ReconciliationOutcome.AGREED.value
    )
    financial_graded = sum(
        1 for cell in financial_reconciliation.values() if cell["outcome"] != ReconciliationOutcome.UNAVAILABLE.value
    )
    confidences = [cell.confidence for cell in cells.values() if cell.confidence is not None]
    mean_conf = (sum(confidences) / requested) if requested else Decimal(0)
    plan = conn.execute(
        "select coalesce((payload->>'forced_fetch')::boolean, false) from raw.production_topt_run_plans where run_id = %s",
        (run_id,),
    ).fetchone()

    return {
        "reconciliation_policy_id": RECONCILIATION_POLICY.policy_id,
        "reconciliation_cells": reconciliation,
        "field_reconciliation": field_reconciliation,
        "financial_fact_reconciliation_policy_id": FINANCIAL_FACT_RECONCILIATION_POLICY.policy_id,
        "financial_fact_reconciliation_cells": financial_reconciliation,
        "financial_fact_independently_reconciled_count": financial_independent,
        "financial_fact_independent_reconciliation": _quantized_ratio(financial_independent, financial_graded),
        "plausibility_cells": plausibility,
        "listing_ratio": listing_ratio_report(listing_ratio),
        "implausible_count": sum(1 for cell in plausibility.values() if cell["outcome"] == "implausible"),
        "run_id": run_id,
        "requested_count": requested,
        "terminal_count": status[1],
        "available_count": available,
        "fresh_count": fresh,
        "independently_reconciled_count": independent,
        "served_by_failover_count": served_by_failover,
        "lineage_complete_count": lineage_complete,
        "terminal_coverage": _quantized_ratio(status[1], requested),
        "availability": _quantized_ratio(available, requested),
        "freshness": _quantized_ratio(fresh, requested),
        "independent_reconciliation": _quantized_ratio(independent, requested),
        "lineage_completeness": _quantized_ratio(lineage_complete, requested),
        "denominator_mean_confidence": str(Decimal(mean_conf).quantize(Decimal("0.0001"))),
        "factor_availability": _factor_availability(conn, run_id, usable_by_subject),
        "complete": bool(status[7]),
        "forced_fetch": bool(plan[0]) if plan is not None else False,
    }


def utc_day(stamp: datetime) -> date:
    return stamp.astimezone(UTC).date()


def _served_day_assertions(
    entries: list[tuple[str, Any, str, str, str, str, dict]],
) -> list[tuple[str, Any, str, str, str, str, dict]]:
    primary_days = [knowable_at for source_id, knowable_at, *_ in entries if source_id == _PRIMARY_PRICE_SOURCE]
    anchor = max(primary_days, default=None) or max(knowable_at for _, knowable_at, *_ in entries)
    return [entry for entry in entries if utc_day(entry[1]) == utc_day(anchor)]


def _field_reconciliation(cells: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    graded = len(cells)
    return {
        field: {
            "agreed": (
                agreed := sum(
                    1
                    for cell in cells.values()
                    if cell["fields"][field]["outcome"] == ReconciliationOutcome.AGREED.value
                )
            ),
            "cells": graded,
            "share": _quantized_ratio(agreed, graded) if graded else "0",
            "policy_id": FIELD_RECONCILIATION_POLICIES[field].policy_id,
        }
        for field in PRICE_BAR_FIELDS
    }


def _reconciliation_cell(
    requirement: str,
    listing_id: str,
    field_name: str,
    semantics_prefix: str,
    unit: str,
    valid_from: date,
    valid_to: date,
) -> ReconciliationCell:
    return ReconciliationCell(
        requirement_id=f"data-requirement:{canonical_sha256({'requirement': requirement})}",
        subject=SubjectRef(kind=SubjectKind.LISTING, id=listing_id),
        field_name=field_name,
        field_semantics_id=f"field-semantics:{canonical_sha256({'field': f'{semantics_prefix}-{field_name}:v1'})}",
        unit=unit,
        valid_from=valid_from,
        valid_to=valid_to,
    )


def _source_assertion(
    cell_id: str,
    obs_id: str,
    source_id: str,
    origin_group: str,
    knowable_at: datetime,
    payload_sha: str,
    value: Decimal,
    vintage_id: str,
    raw_object: str,
) -> SourceAssertion:
    return SourceAssertion(
        cell_id=cell_id,
        observation_id=obs_id,
        source_id=source_id,
        origin_group_id=origin_group,
        knowable_at=knowable_at,
        normalized_value_sha256=payload_sha,
        numeric_value=value,
        confidence_assessment_id=f"confidence-assessment:{payload_sha}",
        lineage_node_ids=(vintage_id, raw_object),
        lineage_complete=True,
    )


def reconcile_price_bar(
    listing_id: str,
    entries: list[tuple[str, Any, str, str, str, str, dict]],
    *,
    partition: date,
    cutoff: datetime,
) -> dict[str, Any]:
    fields: dict[str, dict[str, Any]] = {}
    for field in PRICE_BAR_FIELDS:
        policy = FIELD_RECONCILIATION_POLICIES[field]
        cell = _reconciliation_cell(
            "market-price:v1", listing_id, field, "market-price", FIELD_UNITS[field], partition, utc_day(cutoff)
        )
        assertions = tuple(
            _source_assertion(
                cell.cell_id,
                obs_id,
                source_id,
                extra["origin_group"],
                knowable_at,
                payload_sha,
                Decimal(str(extra["values"][field])),
                vintage_id,
                raw_object,
            )
            for source_id, knowable_at, payload_sha, obs_id, vintage_id, raw_object, extra in entries
            if extra["values"].get(field) is not None
        )
        result = reconcile_source_assertions(cell=cell, assertions=assertions, policy=policy, cutoff=cutoff)
        fields[field] = {**graded_cell(result, assertions), "policy_id": policy.policy_id}
    headline = {key: value for key, value in fields[_HEADLINE_FIELD].items() if key != "policy_id"}
    return {**headline, "fields": fields}


def graded_cell(result: ReconciliationResult, assertions: Sequence[SourceAssertion]) -> dict[str, Any]:
    source_by_id = {item.assertion_id: item.source_id for item in assertions}
    return {
        "outcome": result.outcome.value,
        "origin_groups": len(result.origin_group_ids),
        "agreeing_origin_groups": len(result.agreeing_assertion_ids),
        "selected_source": source_by_id.get(result.selected_assertion_id or ""),
        "selected_value": None if result.selected_numeric_value is None else str(result.selected_numeric_value),
        "conflicting": len(result.conflicting_assertion_ids),
        "outliers": sorted(source_by_id[item] for item in result.conflicting_assertion_ids),
    }


def _query_semantic_observations(
    conn: psycopg.Connection[Any], run_id: str, semantic_type: str
) -> tuple[datetime | None, list[Any]]:
    status = conn.execute("select cutoff from mart.topt_capture_status where run_id = %s", (run_id,)).fetchone()
    if status is None:
        return None, []
    rows = conn.execute(
        """
        select o.subject_id, o.parser_version, o.knowable_at,
               o.normalized_payload_sha256, o.observation_id,
               v.source_vintage_id, v.raw_object_id, p.normalized_payload,
               (ob.partition_key)::date as partition_date
        from raw.capture_obligations ob
        join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
        join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
        join staging.capture_observation_payloads p on p.observation_id = o.observation_id
        join raw.capture_source_vintages v on v.source_vintage_id = o.source_vintage_id
        where ob.run_id = %s and o.semantic_type = %s
        order by o.subject_id
        """,
        (run_id, semantic_type),
    ).fetchall()
    return status[0], rows


def _reconcile_market_price_cells(conn: psycopg.Connection[Any], run_id: str) -> dict[str, dict[str, Any]]:
    cutoff, rows = _query_semantic_observations(conn, run_id, "market-price")
    if cutoff is None:
        return {}
    by_listing: dict[str, list[tuple[str, Any, str, str, str, str, dict]]] = {}
    served_by_failover: dict[str, str] = {}
    partition: date | None = None
    for subject_id, parser, knowable_at, payload_sha, obs_id, vintage_id, raw_object, payload, part in rows:
        if parser not in _SOURCE_BY_PARSER:
            continue
        partition = partition or part
        source_id, origin_group, value_key = _SOURCE_BY_PARSER[parser]
        value = payload.get(value_key)
        if value is None:
            continue
        if payload.get(SERVED_BY_FAILOVER):
            served_by_failover[subject_id] = payload[SERVED_BY_FAILOVER]
        values = {field: (value if field == _HEADLINE_FIELD else payload.get(field)) for field in PRICE_BAR_FIELDS}
        by_listing.setdefault(subject_id, []).append(
            (
                source_id,
                knowable_at,
                payload_sha,
                obs_id,
                vintage_id,
                raw_object,
                {"origin_group": origin_group, "values": values},
            )
        )

    outcomes: dict[str, dict[str, Any]] = {}
    for listing_id, entries in sorted(by_listing.items()):
        outcomes[listing_id] = reconcile_price_bar(
            listing_id, _served_day_assertions(entries), partition=partition or utc_day(cutoff), cutoff=cutoff
        )
        if listing_id in served_by_failover:
            outcomes[listing_id][SERVED_BY_FAILOVER] = served_by_failover[listing_id]
    return outcomes


def financial_fact_unit(payload: Mapping[str, Any]) -> str | None:
    currency = payload.get("currency")
    return currency if isinstance(currency, str) and _ISO_CURRENCY.fullmatch(currency) else None


def _payload_path(payload: Mapping[str, Any], path: str) -> Any:
    node: Any = payload
    for key in path.split("."):
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def primary_financial_fields(payload: Mapping[str, Any]) -> dict[str, tuple[Decimal, date]]:
    out: dict[str, tuple[Decimal, date]] = {}
    for field_name, (value_key, period_path) in FINANCIAL_FACT_FUSION_FIELDS.items():
        value = payload.get(value_key)
        period = _payload_path(payload, period_path)
        if value is None or not isinstance(period, str):
            continue
        try:
            out[field_name] = (Decimal(str(value)), date.fromisoformat(period))
        except (ValueError, ArithmeticError):
            continue
    return out


def corroborating_financial_value(payload: Mapping[str, Any], field_name: str, period_end: date) -> Decimal | None:
    periods = payload.get("by_period_end")
    if not isinstance(periods, Mapping):
        return None
    values = periods.get(period_end.isoformat())
    if not isinstance(values, Mapping) or values.get(field_name) is None:
        return None
    try:
        return Decimal(str(values[field_name]))
    except ArithmeticError:
        return None


@dataclass(frozen=True)
class FinancialFactEntry:
    source_id: str
    origin_group: str
    knowable_at: datetime
    payload_sha: str
    observation_id: str
    vintage_id: str
    raw_object: str
    payload: Mapping[str, Any]

    @property
    def is_primary(self) -> bool:
        return self.source_id == _PRIMARY_FINANCIAL_SOURCE


def classify_financial_fact_entry(
    parser_version: str,
    knowable_at: datetime,
    payload_sha: str,
    observation_id: str,
    vintage_id: str,
    raw_object: str,
    payload: Mapping[str, Any],
) -> FinancialFactEntry | None:
    if parser_version in _PRIMARY_FINANCIAL_VINTAGES:
        source_id, origin_group = _PRIMARY_FINANCIAL_SOURCE, _PRIMARY_FINANCIAL_ORIGIN_GROUP
    elif parser_version in _SOURCE_BY_PARSER:
        source_id, origin_group, _value_key = _SOURCE_BY_PARSER[parser_version]
    else:
        return None
    return FinancialFactEntry(
        source_id, origin_group, knowable_at, payload_sha, observation_id, vintage_id, raw_object, payload
    )


def reconcile_financial_fact_entries(
    listing_id: str, entries: Sequence[FinancialFactEntry], *, cutoff: datetime
) -> dict[str, Any]:
    primaries = [entry for entry in entries if entry.is_primary]
    if not primaries:
        return {"outcome": ReconciliationOutcome.UNAVAILABLE.value, "fields": {}, "origin_groups": 0}
    primary = max(primaries, key=lambda entry: (entry.knowable_at, entry.observation_id))
    unit = financial_fact_unit(primary.payload)
    if unit is None:
        return {"outcome": ReconciliationOutcome.UNAVAILABLE.value, "fields": {}, "origin_groups": 0}
    fields: dict[str, dict[str, Any]] = {}
    outcomes: list[str] = []
    participating = {primary.origin_group}
    for field_name, (value, period_end) in sorted(primary_financial_fields(primary.payload).items()):
        cell = _reconciliation_cell(
            "financial-fact:v1",
            listing_id,
            field_name,
            "financial-fact",
            unit,
            period_end,
            max(period_end, utc_day(cutoff)),
        )
        assertions = [
            _source_assertion(
                cell.cell_id,
                primary.observation_id,
                primary.source_id,
                primary.origin_group,
                primary.knowable_at,
                primary.payload_sha,
                value,
                primary.vintage_id,
                primary.raw_object,
            )
        ]
        for entry in entries:
            if entry.is_primary or financial_fact_unit(entry.payload) != unit:
                continue
            other = corroborating_financial_value(entry.payload, field_name, period_end)
            if other is not None:
                assertions.append(
                    _source_assertion(
                        cell.cell_id,
                        entry.observation_id,
                        entry.source_id,
                        entry.origin_group,
                        entry.knowable_at,
                        entry.payload_sha,
                        other,
                        entry.vintage_id,
                        entry.raw_object,
                    )
                )
                participating.add(entry.origin_group)
        result = reconcile_source_assertions(
            cell=cell, assertions=tuple(assertions), policy=FINANCIAL_FACT_RECONCILIATION_POLICY, cutoff=cutoff
        )
        fields[field_name] = {**graded_cell(result, assertions), "period_end": period_end.isoformat(), "unit": unit}
        outcomes.append(result.outcome.value)
    if any(outcome == ReconciliationOutcome.CONFLICT_PRIORITY_SERVED.value for outcome in outcomes):
        overall = ReconciliationOutcome.CONFLICT_PRIORITY_SERVED.value
    elif outcomes and all(outcome == ReconciliationOutcome.AGREED.value for outcome in outcomes):
        overall = ReconciliationOutcome.AGREED.value
    elif outcomes:
        overall = ReconciliationOutcome.INSUFFICIENT_INDEPENDENT_ORIGINS.value
    else:
        overall = ReconciliationOutcome.UNAVAILABLE.value
    return {"outcome": overall, "origin_groups": len(participating), "fields": fields}


def _reconcile_financial_fact_cells(conn: psycopg.Connection[Any], run_id: str) -> dict[str, dict[str, Any]]:
    cutoff, rows = _query_semantic_observations(conn, run_id, "financial-fact")
    if cutoff is None:
        return {}
    by_listing: dict[str, list[FinancialFactEntry]] = {}
    for row in rows:
        entry = classify_financial_fact_entry(*row[1:8])
        if entry is not None:
            by_listing.setdefault(row[0], []).append(entry)
    return {
        listing_id: reconcile_financial_fact_entries(listing_id, entries, cutoff=cutoff)
        for listing_id, entries in sorted(by_listing.items())
    }


def persist(conn: psycopg.Connection[Any], report: dict[str, Any]) -> str:
    return persist_run_report(
        conn,
        report,
        table="mart.datahub_quality_report",
        report_id_prefix="datahub-quality-report",
        columns=("run_id", "requested_count"),
    )
