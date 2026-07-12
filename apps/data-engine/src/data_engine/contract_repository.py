"""Immutable Postgres storage for Gate 0 content-addressed contracts.

The repository deliberately stores the complete validated JSON contract.  It
never updates a row: a duplicate put succeeds only when kind, content hash, and
payload are all identical to the existing object.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from psycopg import Connection, sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ValidationError
from truealpha_contracts.capture_contracts import (
    CaptureEvaluationReport,
    CaptureManifest,
    CaptureScope,
)
from truealpha_contracts.catalog import ResearchCatalogManifest
from truealpha_contracts.execution import SnapshotManifest, TraceBundle
from truealpha_contracts.gates import GraduationAttestation
from truealpha_contracts.registries import RegistrySnapshot
from truealpha_contracts.release import ReleaseManifest
from truealpha_contracts.usage import (
    StrategyDataQualityReview,
    StrategyUsageAudit,
    UsageFrequencySlice,
)

_STABLE_STRATEGY_RUN_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/@+-]*$")


def _require_stable_strategy_run_id(strategy_run_id: str) -> None:
    if _STABLE_STRATEGY_RUN_ID.fullmatch(strategy_run_id) is None:
        raise ValueError("strategy_run_id must be a stable identifier")


class ContractKind(StrEnum):
    REGISTRY_SNAPSHOT = "registry_snapshot"
    RESEARCH_CATALOG = "research_catalog_manifest"
    SNAPSHOT_MANIFEST = "snapshot_manifest"
    RELEASE_MANIFEST = "release_manifest"
    CAPTURE_SCOPE = "capture_scope"
    CAPTURE_MANIFEST = "capture_manifest"
    CAPTURE_EVALUATION_REPORT = "capture_evaluation_report"
    TRACE_BUNDLE = "trace_bundle"
    STRATEGY_USAGE_AUDIT = "strategy_usage_audit"
    USAGE_FREQUENCY_SLICE = "usage_frequency_slice"
    STRATEGY_DATA_QUALITY_REVIEW = "strategy_data_quality_review"
    GRADUATION_ATTESTATION = "graduation_attestation"


class ContractRepositoryError(RuntimeError):
    """Base class for immutable contract repository failures."""


class ContractKindMismatchError(ContractRepositoryError):
    """The model, ID, or stored row belongs to a different contract kind."""


class ContractIntegrityError(ContractRepositoryError):
    """A contract payload, ID, or content hash failed validation."""


class ContractConflictError(ContractRepositoryError):
    """An existing ID is bound to different immutable content."""


@dataclass(frozen=True)
class _ContractSpec[ContractT: BaseModel]:
    kind: ContractKind
    model_type: type[ContractT]
    id_field: str
    hash_field: str
    id_prefix: str


_REGISTRY_SPEC = _ContractSpec(
    kind=ContractKind.REGISTRY_SNAPSHOT,
    model_type=RegistrySnapshot,
    id_field="registry_snapshot_id",
    hash_field="content_sha256",
    id_prefix="registry-snapshot",
)
_CATALOG_SPEC = _ContractSpec(
    kind=ContractKind.RESEARCH_CATALOG,
    model_type=ResearchCatalogManifest,
    id_field="research_catalog_id",
    hash_field="content_sha256",
    id_prefix="research-catalog",
)
_SNAPSHOT_SPEC = _ContractSpec(
    kind=ContractKind.SNAPSHOT_MANIFEST,
    model_type=SnapshotManifest,
    id_field="snapshot_id",
    hash_field="content_sha256",
    id_prefix="snapshot",
)
_RELEASE_SPEC = _ContractSpec(
    kind=ContractKind.RELEASE_MANIFEST,
    model_type=ReleaseManifest,
    id_field="release_manifest_id",
    hash_field="manifest_sha256",
    id_prefix="release-manifest",
)
_CAPTURE_SCOPE_SPEC = _ContractSpec(
    kind=ContractKind.CAPTURE_SCOPE,
    model_type=CaptureScope,
    id_field="capture_scope_id",
    hash_field="content_sha256",
    id_prefix="capture-scope",
)
_CAPTURE_MANIFEST_SPEC = _ContractSpec(
    kind=ContractKind.CAPTURE_MANIFEST,
    model_type=CaptureManifest,
    id_field="capture_manifest_id",
    hash_field="content_sha256",
    id_prefix="capture-manifest",
)
_CAPTURE_EVALUATION_SPEC = _ContractSpec(
    kind=ContractKind.CAPTURE_EVALUATION_REPORT,
    model_type=CaptureEvaluationReport,
    id_field="capture_evaluation_report_id",
    hash_field="content_sha256",
    id_prefix="capture-evaluation",
)
_TRACE_BUNDLE_SPEC = _ContractSpec(
    kind=ContractKind.TRACE_BUNDLE,
    model_type=TraceBundle,
    id_field="trace_bundle_id",
    hash_field="content_sha256",
    id_prefix="trace-bundle",
)
_STRATEGY_USAGE_AUDIT_SPEC = _ContractSpec(
    kind=ContractKind.STRATEGY_USAGE_AUDIT,
    model_type=StrategyUsageAudit,
    id_field="strategy_usage_audit_id",
    hash_field="content_sha256",
    id_prefix="strategy-usage-audit",
)
_USAGE_FREQUENCY_SLICE_SPEC = _ContractSpec(
    kind=ContractKind.USAGE_FREQUENCY_SLICE,
    model_type=UsageFrequencySlice,
    id_field="usage_frequency_slice_id",
    hash_field="content_sha256",
    id_prefix="usage-frequency",
)
_STRATEGY_DATA_QUALITY_REVIEW_SPEC = _ContractSpec(
    kind=ContractKind.STRATEGY_DATA_QUALITY_REVIEW,
    model_type=StrategyDataQualityReview,
    id_field="review_id",
    hash_field="content_sha256",
    id_prefix="strategy-data-quality-review",
)
_GRADUATION_ATTESTATION_SPEC = _ContractSpec(
    kind=ContractKind.GRADUATION_ATTESTATION,
    model_type=GraduationAttestation,
    id_field="graduation_attestation_id",
    hash_field="content_sha256",
    id_prefix="graduation-attestation",
)


class PostgresContractRepository[ContractT: BaseModel]:
    """Typed adapter over one append-only content-addressed JSON table.

    The expected table shape is::

        contract_id text primary key,
        contract_kind text not null,
        content_sha256 text not null,
        payload jsonb not null

    Migrations own the durable table. Tests may point this adapter at a
    transaction-local temporary table with the same shape.
    """

    def __init__(
        self,
        connection: Connection[Any],
        spec: _ContractSpec[ContractT],
        *,
        schema: str | None = "staging",
        table: str = "contract_objects",
    ) -> None:
        self._connection = connection
        self._spec = spec
        self._table = sql.Identifier(table) if schema is None else sql.Identifier(schema, table)
        self._id_pattern = re.compile(rf"^{re.escape(spec.id_prefix)}:[0-9a-f]{{64}}$")

    def put(self, contract: ContractT) -> bool:
        """Insert a new object, returning ``False`` for an identical duplicate."""

        if not isinstance(contract, self._spec.model_type):
            raise ContractKindMismatchError(
                f"{self._spec.kind.value} repository does not accept {type(contract).__name__}"
            )
        validated, payload = self._validate_payload(contract.model_dump(mode="json", exclude_computed_fields=True))
        contract_id, content_sha256 = self._identity(validated)

        inserted = self._connection.execute(
            sql.SQL(
                """
                insert into {} (contract_id, contract_kind, content_sha256, payload)
                values (%s, %s, %s, %s)
                on conflict (contract_id) do nothing
                returning contract_id
                """
            ).format(self._table),
            (contract_id, self._spec.kind.value, content_sha256, Jsonb(payload)),
        ).fetchone()
        if inserted is not None:
            return True

        existing = self._select(contract_id)
        if existing is None:
            raise ContractRepositoryError(f"contract {contract_id} disappeared while resolving a conflict")
        stored_kind, stored_hash, stored_payload = existing
        self._require_expected_kind(contract_id, stored_kind)
        if stored_hash != content_sha256 or stored_payload != payload:
            raise ContractConflictError(f"contract ID {contract_id} is already bound to different content")
        # Revalidate even an identical-looking JSONB row so pre-existing tamper
        # cannot be blessed by a duplicate write.
        self._validate_stored_identity(contract_id, stored_hash, stored_payload)
        return False

    def get(self, contract_id: str) -> ContractT | None:
        """Return one validated object, or ``None`` for an unknown valid ID."""

        self._require_expected_id(contract_id)
        row = self._select(contract_id)
        if row is None:
            return None
        stored_kind, stored_hash, stored_payload = row
        self._require_expected_kind(contract_id, stored_kind)
        return self._validate_stored_identity(contract_id, stored_hash, stored_payload)

    def _list_by_payload_text(self, field_name: str, value: str) -> tuple[ContractT, ...]:
        """Return a deterministic, fully revalidated reverse lookup."""

        rows = self._connection.execute(
            sql.SQL(
                """
                select contract_id, contract_kind, content_sha256, payload
                from {}
                where contract_kind = %s and payload ->> %s = %s
                order by contract_id
                """
            ).format(self._table),
            (self._spec.kind.value, field_name, value),
        ).fetchall()
        contracts: list[ContractT] = []
        for row in rows:
            contract_id = str(row[0])
            stored_kind = str(row[1])
            stored_hash = str(row[2])
            stored_payload = row[3]
            if not isinstance(stored_payload, dict):
                raise ContractIntegrityError(f"stored payload for {contract_id} is not a JSON object")
            self._require_expected_id(contract_id)
            self._require_expected_kind(contract_id, stored_kind)
            contracts.append(self._validate_stored_identity(contract_id, stored_hash, stored_payload))
        return tuple(contracts)

    def _select(self, contract_id: str) -> tuple[str, str, dict[str, Any]] | None:
        row = self._connection.execute(
            sql.SQL("select contract_kind, content_sha256, payload from {} where contract_id = %s").format(self._table),
            (contract_id,),
        ).fetchone()
        if row is None:
            return None
        payload = row[2]
        if not isinstance(payload, dict):
            raise ContractIntegrityError(f"stored payload for {contract_id} is not a JSON object")
        return str(row[0]), str(row[1]), payload

    def _validate_stored_identity(
        self,
        contract_id: str,
        stored_hash: str,
        stored_payload: dict[str, Any],
    ) -> ContractT:
        validated, canonical_payload = self._validate_payload(stored_payload)
        validated_id, validated_hash = self._identity(validated)
        if canonical_payload != stored_payload:
            raise ContractIntegrityError(f"stored payload for {contract_id} is not canonical")
        if validated_id != contract_id or validated_hash != stored_hash:
            raise ContractIntegrityError(f"stored identity for {contract_id} does not match its payload")
        return validated

    def _validate_payload(self, payload: dict[str, Any]) -> tuple[ContractT, dict[str, Any]]:
        try:
            encoded = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            validated = self._spec.model_type.model_validate_json(encoded)
        except (TypeError, ValueError, ValidationError) as error:
            raise ContractIntegrityError(f"invalid {self._spec.kind.value} contract payload") from error
        canonical_payload = validated.model_dump(mode="json", exclude_computed_fields=True)
        self._identity(validated)
        return validated, canonical_payload

    def _identity(self, contract: ContractT) -> tuple[str, str]:
        contract_id = getattr(contract, self._spec.id_field, None)
        content_sha256 = getattr(contract, self._spec.hash_field, None)
        if not isinstance(contract_id, str) or not isinstance(content_sha256, str):
            raise ContractIntegrityError(f"{self._spec.kind.value} contract has no content identity")
        self._require_expected_id(contract_id)
        if contract_id != f"{self._spec.id_prefix}:{content_sha256}":
            raise ContractIntegrityError(f"contract ID {contract_id} does not match its content hash")
        return contract_id, content_sha256

    def _require_expected_id(self, contract_id: str) -> None:
        if not self._id_pattern.fullmatch(contract_id):
            raise ContractKindMismatchError(f"contract ID {contract_id!r} does not belong to {self._spec.kind.value}")

    def _require_expected_kind(self, contract_id: str, stored_kind: str) -> None:
        if stored_kind != self._spec.kind.value:
            raise ContractKindMismatchError(
                f"contract ID {contract_id} is stored as {stored_kind}, not {self._spec.kind.value}"
            )


class PostgresRegistrySnapshotRepository(PostgresContractRepository[RegistrySnapshot]):
    def __init__(
        self,
        connection: Connection[Any],
        *,
        schema: str | None = "staging",
        table: str = "contract_objects",
    ) -> None:
        super().__init__(connection, _REGISTRY_SPEC, schema=schema, table=table)


class PostgresResearchCatalogRepository(PostgresContractRepository[ResearchCatalogManifest]):
    def __init__(
        self,
        connection: Connection[Any],
        *,
        schema: str | None = "staging",
        table: str = "contract_objects",
    ) -> None:
        super().__init__(connection, _CATALOG_SPEC, schema=schema, table=table)


class PostgresSnapshotRepository(PostgresContractRepository[SnapshotManifest]):
    def __init__(
        self,
        connection: Connection[Any],
        *,
        schema: str | None = "staging",
        table: str = "contract_objects",
    ) -> None:
        super().__init__(connection, _SNAPSHOT_SPEC, schema=schema, table=table)


class PostgresReleaseManifestRepository(PostgresContractRepository[ReleaseManifest]):
    def __init__(
        self,
        connection: Connection[Any],
        *,
        schema: str | None = "staging",
        table: str = "contract_objects",
    ) -> None:
        super().__init__(connection, _RELEASE_SPEC, schema=schema, table=table)


class PostgresCaptureScopeRepository(PostgresContractRepository[CaptureScope]):
    def __init__(
        self,
        connection: Connection[Any],
        *,
        schema: str | None = "staging",
        table: str = "contract_objects",
    ) -> None:
        super().__init__(connection, _CAPTURE_SCOPE_SPEC, schema=schema, table=table)


class PostgresCaptureManifestRepository(PostgresContractRepository[CaptureManifest]):
    def __init__(
        self,
        connection: Connection[Any],
        *,
        schema: str | None = "staging",
        table: str = "contract_objects",
    ) -> None:
        super().__init__(connection, _CAPTURE_MANIFEST_SPEC, schema=schema, table=table)


class PostgresCaptureEvaluationRepository(PostgresContractRepository[CaptureEvaluationReport]):
    def __init__(
        self,
        connection: Connection[Any],
        *,
        schema: str | None = "staging",
        table: str = "contract_objects",
    ) -> None:
        super().__init__(connection, _CAPTURE_EVALUATION_SPEC, schema=schema, table=table)


class PostgresTraceBundleRepository(PostgresContractRepository[TraceBundle]):
    def __init__(
        self,
        connection: Connection[Any],
        *,
        schema: str | None = "staging",
        table: str = "contract_objects",
    ) -> None:
        super().__init__(connection, _TRACE_BUNDLE_SPEC, schema=schema, table=table)


class PostgresStrategyUsageAuditRepository(PostgresContractRepository[StrategyUsageAudit]):
    def __init__(
        self,
        connection: Connection[Any],
        *,
        schema: str | None = "staging",
        table: str = "contract_objects",
    ) -> None:
        super().__init__(connection, _STRATEGY_USAGE_AUDIT_SPEC, schema=schema, table=table)

    def list_for_run(self, strategy_run_id: str) -> tuple[StrategyUsageAudit, ...]:
        _require_stable_strategy_run_id(strategy_run_id)
        return self._list_by_payload_text("strategy_run_id", strategy_run_id)


class PostgresUsageFrequencySliceRepository(PostgresContractRepository[UsageFrequencySlice]):
    def __init__(
        self,
        connection: Connection[Any],
        *,
        schema: str | None = "staging",
        table: str = "contract_objects",
    ) -> None:
        super().__init__(connection, _USAGE_FREQUENCY_SLICE_SPEC, schema=schema, table=table)


class PostgresStrategyDataQualityReviewRepository(PostgresContractRepository[StrategyDataQualityReview]):
    def __init__(
        self,
        connection: Connection[Any],
        *,
        schema: str | None = "staging",
        table: str = "contract_objects",
    ) -> None:
        super().__init__(connection, _STRATEGY_DATA_QUALITY_REVIEW_SPEC, schema=schema, table=table)

    def list_for_run(self, strategy_run_id: str) -> tuple[StrategyDataQualityReview, ...]:
        _require_stable_strategy_run_id(strategy_run_id)
        return self._list_by_payload_text("strategy_run_id", strategy_run_id)


class PostgresGraduationAttestationRepository(PostgresContractRepository[GraduationAttestation]):
    def __init__(
        self,
        connection: Connection[Any],
        *,
        schema: str | None = "staging",
        table: str = "contract_objects",
    ) -> None:
        super().__init__(connection, _GRADUATION_ATTESTATION_SPEC, schema=schema, table=table)
