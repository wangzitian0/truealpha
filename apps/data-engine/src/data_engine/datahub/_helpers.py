"""Shared DataHub confidence, SQL, and connection helpers."""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from factors.confidence import evaluate_cell_confidence, sanitize_stable_id
from truealpha_contracts.confidence import ContinuousConfidencePolicy, SourceConfidenceEvidence
from truealpha_contracts.metrics import is_registered_input_key

from data_engine.datahub.capture_core.source_registrations import source_reliability_prior
from data_engine.datahub.capture_core.status_dimensions import statuses_without_raw_pointer

DEFAULT_CONFIDENCE_POLICY = ContinuousConfidencePolicy()

CAPTURE_OBSERVATION_JOIN = (
    "from raw.capture_obligations ob\n"
    "join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id\n"
    "join staging.capture_normalized_observations o on o.observation_id = oo.observation_id\n"
    "join staging.capture_observation_payloads p on p.observation_id = o.observation_id"
)
OBSERVATION_VINTAGE_JOIN = (
    "from staging.capture_normalized_observations o "
    "join staging.capture_observation_payloads p on p.observation_id = o.observation_id "
    "join raw.capture_source_vintages v on v.source_vintage_id = o.source_vintage_id"
)

_CASE_ID_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._:/@+-]+")


def safe_case_id_part(value: str, fallback: str, *, collapse: bool = False) -> str:
    """Normalize an identifier segment for confidence case IDs."""
    if collapse:
        cleaned = _CASE_ID_SANITIZE_RE.sub("-", value)
    else:
        cleaned = "".join(char if char.isalnum() or char in "._:/@+-" else "-" for char in value)
    return cleaned.strip("-") or fallback


def single_origin_confidence(
    *,
    case_id: str,
    provider_id: str,
    lineage: str,
    evidence_id: str,
    fallback_prior_id: str | None = None,
    freshness: Decimal = Decimal("1"),
    agreement: Decimal = Decimal("1"),
    sample_conformance: Decimal = Decimal("1"),
    semantic_mapping_quality: Decimal = Decimal("1"),
    reason_codes: tuple[str, ...] = ("reason:single_origin",),
    extra_reason_codes: tuple[str, ...] = (),
    policy: ContinuousConfidencePolicy = DEFAULT_CONFIDENCE_POLICY,
) -> Decimal:
    """Evaluate continuous confidence for a single-source observation."""
    origin_group = lineage if lineage.startswith("lineage:") else f"lineage:{lineage}"
    prior = source_reliability_prior(provider_id)
    if prior is None and fallback_prior_id is not None:
        prior = source_reliability_prior(fallback_prior_id)
    return evaluate_cell_confidence(
        case_id=case_id,
        sources=(
            SourceConfidenceEvidence(
                provider_id=sanitize_stable_id(provider_id, fallback="provider"),
                origin_group_id=sanitize_stable_id(origin_group, fallback="lineage"),
                independence_weight=Decimal("1"),
                reliability_prior=prior,
                successful_outcome_mass=Decimal("0"),
                failed_outcome_mass=Decimal("0"),
                freshness=freshness,
                sample_conformance=sample_conformance,
                transport_integrity=Decimal("1"),
                evidence_ids=(sanitize_stable_id(evidence_id, fallback="evidence"),),
                reason_codes=reason_codes,
            ),
        ),
        agreement=agreement,
        semantic_mapping_quality=semantic_mapping_quality,
        lineage_completeness=Decimal("1"),
        required_component_completeness=Decimal("1"),
        extra_reason_codes=extra_reason_codes,
        policy=policy,
    ).confidence


def calibrated_confidence_for(
    policy_id: str,
    extractor: str,
    *,
    case_id: str,
    evidence_id: str,
    provider_id: str = "origin:sec-10k:v1",
    lineage: str = "sec-10k",
    freshness: Decimal = Decimal("1"),
    policy: ContinuousConfidencePolicy = DEFAULT_CONFIDENCE_POLICY,
) -> Decimal:
    """Route a discrete extractor policy weight through continuous single-origin cell calibration."""
    from data_engine.contracts.standards import confidence_for

    weight = confidence_for(policy_id, extractor)
    return single_origin_confidence(
        case_id=case_id,
        provider_id=provider_id,
        lineage=lineage,
        evidence_id=evidence_id,
        freshness=freshness,
        semantic_mapping_quality=weight,
        policy=policy,
    )


@contextlib.contextmanager
def optional_savepoint(connection: Any) -> Iterator[None]:
    tx_fn = getattr(connection, "transaction", None)
    if callable(tx_fn):
        with tx_fn():
            yield
    else:
        yield


def safe_call(connection: Any, fn: Any, default: Any = None) -> Any:
    try:
        with optional_savepoint(connection):
            return fn()
    except Exception:  # noqa: BLE001
        return default


def safe_fetchall(connection: Any, sql: str, params: Sequence[Any] = ()) -> list[Any]:
    return list(safe_call(connection, lambda: connection.execute(sql, tuple(params)).fetchall(), ()))


def safe_fetchone(connection: Any, sql: str, params: Sequence[Any] = ()) -> Any | None:
    return safe_call(connection, lambda: connection.execute(sql, tuple(params)).fetchone(), None)


def has_table(connection: Any, qualified_name: str) -> bool:
    def _probe() -> bool:
        with connection.cursor() as cur:
            cur.execute(f"select to_regclass('{qualified_name}')")
            res = cur.fetchone()
            return bool(res and res[0])

    return bool(safe_call(connection, _probe, False))


def has_column(connection: Any, schema: str, table: str, column: str) -> bool:
    def _probe() -> bool:
        with connection.cursor() as cur:
            cur.execute(
                "select exists ("
                "select 1 from pg_attribute where attrelid = to_regclass(%s) and attname = %s and not attisdropped"
                ")",
                (f"{schema}.{table}", column),
            )
            res = cur.fetchone()
            return bool(res and res[0])

    return bool(safe_call(connection, _probe, False))


def parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    return value.astimezone(UTC)


def parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in ("null", "nan", "none"):
        return None
    try:
        p = Decimal(s)
        return p if p.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def insert_or_verify_sha(
    connection: Any,
    table: str,
    id_column: str,
    row_id: str,
    content_sha256: str,
    sql: str,
    params: Sequence[Any],
    conflict_msg: str,
) -> str:
    if connection.execute(sql, tuple(params)).fetchone() is not None:
        return row_id
    existing = connection.execute(
        f"select content_sha256 from {table} where {id_column} = %s",  # noqa: S608
        (row_id,),
    ).fetchone()
    if existing is None or existing[0] != content_sha256:
        raise ValueError(conflict_msg)
    return row_id


def require_registered_input_key(input_key: str, *, verb: str = "seeding") -> None:
    if not is_registered_input_key(input_key):
        raise ValueError(
            f"{input_key!r} is not a registered metric (truealpha_contracts.metrics.METRICS); "
            f"register it there before {verb} it as a strategy input"
        )


def unbacked_status_values(cutoff: datetime | None) -> tuple[datetime, str, str]:
    source_evidence, factor_validation = statuses_without_raw_pointer()
    return cutoff or datetime.now(tz=UTC), source_evidence.value, factor_validation.value


def fetch_run_cutoff(connection: Any, run_id: str) -> datetime | None:
    row = connection.execute("select cutoff from mart.capture_status where run_id = %s", (run_id,)).fetchone()
    return row[0] if row is not None else None


def execute_many(connection: Any, query: str, rows: Sequence[Any]) -> int:
    """Use psycopg cursor.executemany when supported; fall back to connection.execute for mocks."""
    if not rows:
        return 0
    if hasattr(connection, "pgconn") and callable(getattr(connection, "cursor", None)):
        with connection.cursor() as cur:
            cur.executemany(query, rows)
            rowcount = getattr(cur, "rowcount", -1)
            return int(rowcount) if isinstance(rowcount, int) and rowcount >= 0 else len(rows)
    for row in rows:
        connection.execute(query, row)
    return len(rows)


def read_raw_json(connection: Any, fetch_id: int) -> dict[str, Any] | None:
    """Load and parse one raw fetch payload as a JSON dict, returning None if absent or unreadable."""
    import json

    from truealpha_runtime.storage import StorageError

    from data_engine import raw_store

    try:
        document = json.loads(raw_store.get_payload(connection, fetch_id))
    except (LookupError, StorageError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def gppe_branch_predicate(
    mnv_alias: str = "mv",
    cil_alias: str = "cil",
    *,
    bank_param: str = "%s",
    base_param: str = "%s",
) -> str:
    """SQL predicate matching the operating-branch-aware GPPE metric node."""
    return (
        f"(({cil_alias}.operating_branch = 'financial' and {mnv_alias}.node_key = {bank_param}) "
        f"or {mnv_alias}.node_key = {base_param})"
    )
