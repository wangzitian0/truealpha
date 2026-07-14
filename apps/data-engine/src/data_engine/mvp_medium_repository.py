"""Registry-driven normalized storage and PIT selection for D2 medium domains."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, cast

from psycopg import Connection
from psycopg.types.json import Jsonb
from pydantic import BaseModel
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.execution import (
    NormalizedRecordRef,
    ProvenanceNeutralInput,
    SemanticDraft,
    SemanticProducerKind,
    SnapshotDemandCell,
)
from truealpha_contracts.market import CorporateAction
from truealpha_contracts.models import FinancialFact
from truealpha_contracts.registries import RegistrySnapshot, SemanticTypeRegistryEntry, SourceRegistryEntry
from truealpha_contracts.universe import IssuerSecurityLink, SecurityListingLink, UniverseMembership

from data_engine.mvp_medium_models import MarketPricePayload, MvpNormalizationDraft
from data_engine.mvp_medium_registry import (
    CORPORATE_ACTION_TYPE_ID,
    FINANCIAL_FACT_TYPE_ID,
    ISSUER_SECURITY_TYPE_ID,
    SECURITY_LISTING_TYPE_ID,
    UNIVERSE_MEMBERSHIP_TYPE_ID,
)
from data_engine.mvp_models import FilingDocumentPayload
from data_engine.mvp_registry import FILING_SEMANTIC_TYPE_ID

PayloadKey = tuple[str, ...]
ProjectionWriter = Callable[[Connection[Any], NormalizedRecordRef, BaseModel, str], bool]
LogicalKey = Callable[[BaseModel], PayloadKey]
PartitionFilter = Callable[[BaseModel, str], bool]
SourceRank = Callable[[BaseModel, str], int | None]


class MediumRepositoryError(RuntimeError):
    """Base class for D2 durable semantic-record failures."""


class MediumIntegrityError(MediumRepositoryError):
    """A record, registry binding, or projection is inconsistent."""


class MediumConflictError(MediumRepositoryError):
    """A content-addressed identity is already bound to different bytes."""


@dataclass(frozen=True)
class MediumRepositoryRegistration:
    semantic_type_id: str
    semantic_type_version: str
    model_type: type[BaseModel]
    repository_key: str
    projector_key: str
    mapping_versions: Mapping[str, str]
    writer: ProjectionWriter
    logical_key: LogicalKey
    partition_filter: PartitionFilter
    source_rank: SourceRank

    @property
    def key(self) -> tuple[str, str]:
        return self.semantic_type_id, self.semantic_type_version


@dataclass(frozen=True)
class StoredMediumRecord:
    record: NormalizedRecordRef
    payload: BaseModel
    source_id: str


def attach_normalized_lineage(
    *,
    draft: MvpNormalizationDraft,
    semantic_type: SemanticTypeRegistryEntry,
    source: SourceRegistryEntry,
    raw_object_sha256: str,
) -> NormalizedRecordRef:
    """Attach exact registry and raw identities to one source-owned typed draft."""

    if draft.semantic_type_id != semantic_type.semantic_type_id:
        raise MediumIntegrityError("normalizer draft does not match its semantic registry entry")
    if semantic_type.semantic_type_id not in source.supported_type_ids:
        raise MediumIntegrityError("source registry entry does not support the normalized type")
    if draft.supersedes_document_id is not None:
        raise MediumIntegrityError("normalization draft predecessor was not resolved")
    semantic_draft = SemanticDraft(
        semantic_type_id=semantic_type.semantic_type_id,
        semantic_type_version=semantic_type.version,
        payload_model_key=semantic_type.normalized_model_key,
        payload_schema_sha256=semantic_type.schema_fingerprint_sha256,
        payload_sha256=canonical_sha256(draft.payload.model_dump(mode="json")),
        subject=draft.subject,
        valid_from=draft.valid_from,
        valid_to=draft.valid_to,
        knowable_at=draft.knowable_at,
        produced_at=draft.produced_at,
        producer_kind=SemanticProducerKind.DETERMINISTIC_NORMALIZER,
        producer_id=source.normalizer_id,
        producer_version=source.normalizer_version,
        producer_implementation_sha256=source.normalizer_implementation_sha256,
    )
    return NormalizedRecordRef(
        draft=semantic_draft,
        document_id=draft.document_id,
        raw_object_id=f"raw-object:{raw_object_sha256}",
        raw_object_sha256=raw_object_sha256,
        source_registry_entry_id=source.source_registry_entry_id,
        source_registry_entry_sha256=source.content_sha256,
        mapping_version=f"{source.normalizer_id}:{source.normalizer_version}",
        mapping_implementation_sha256=source.normalizer_implementation_sha256,
        recorded_at=draft.recorded_at,
        confidence=draft.confidence,
        is_restatement=draft.is_restatement,
        supersedes_record_id=draft.supersedes_record_id,
    )


class PostgresMediumSemanticRepository:
    """Persist and resolve records through exact, static type registrations."""

    def __init__(
        self,
        connection: Connection[Any],
        *,
        registry: RegistrySnapshot,
        registrations: Sequence[MediumRepositoryRegistration],
    ) -> None:
        self.connection = connection
        self.registry = registry
        self._types = {(entry.semantic_type_id, entry.version): entry for entry in registry.semantic_types}
        self._sources = {entry.source_registry_entry_id: entry for entry in registry.sources}
        self._registrations = {registration.key: registration for registration in registrations}
        if len(self._registrations) != len(registrations):
            raise ValueError("duplicate medium repository registration")
        self._validate_registrations()

    def _validate_registrations(self) -> None:
        if set(self._registrations) != set(self._types):
            missing = sorted(set(self._types) - set(self._registrations))
            unexpected = sorted(set(self._registrations) - set(self._types))
            raise ValueError(f"medium repository coverage drifted: missing={missing}, unexpected={unexpected}")
        for key, registration in self._registrations.items():
            semantic_type = self._types[key]
            if registration.repository_key != semantic_type.repository_key:
                raise ValueError(f"repository key drift for {semantic_type.semantic_type_id}")
            if registration.projector_key != semantic_type.projector_key:
                raise ValueError(f"projector key drift for {semantic_type.semantic_type_id}")
            schema_sha256 = canonical_sha256(registration.model_type.model_json_schema(mode="validation"))
            if schema_sha256 != semantic_type.schema_fingerprint_sha256:
                raise ValueError(f"schema fingerprint drift for {semantic_type.semantic_type_id}")
            supported_sources = {
                source.source_id
                for source in self.registry.sources
                if semantic_type.semantic_type_id in source.supported_type_ids
            }
            if set(registration.mapping_versions) != supported_sources:
                raise ValueError(f"mapping-version coverage drift for {semantic_type.semantic_type_id}")

    def put(self, record: NormalizedRecordRef, payload: BaseModel, *, raw_ref: str) -> bool:
        key = (record.draft.semantic_type_id, record.draft.semantic_type_version)
        registration = self._registrations.get(key)
        if registration is None:
            raise MediumIntegrityError(f"unregistered semantic coordinate: {key}")
        source = self._validate_record(record, payload, raw_ref=raw_ref, registration=registration)
        payload_json = payload.model_dump(mode="json")
        record_json = record.model_dump(mode="json")
        with self.connection.transaction():
            self._validate_supersession(record)
            inserted = self.connection.execute(
                """
                insert into staging.normalized_records (
                    normalized_record_id, content_sha256, semantic_type_id,
                    semantic_type_version, subject_kind, subject_id, valid_time,
                    transaction_time, recorded_at, confidence, document_id,
                    raw_object_id, raw_object_sha256, raw_ref,
                    source_registry_entry_id, source_registry_entry_sha256,
                    mapping_version, mapping_implementation_sha256,
                    payload_model_key, payload_schema_sha256, payload_sha256,
                    payload, record_ref, is_restatement, supersedes_record_id
                ) values (
                    %s, %s, %s, %s, %s, %s, daterange(%s, %s, '[]'),
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s
                )
                on conflict (normalized_record_id) do nothing
                returning normalized_record_id
                """,
                (
                    record.normalized_record_id,
                    record.content_sha256,
                    record.draft.semantic_type_id,
                    record.draft.semantic_type_version,
                    record.draft.subject.kind.value,
                    record.draft.subject.id,
                    record.draft.valid_from,
                    record.draft.valid_to,
                    record.draft.knowable_at,
                    record.recorded_at,
                    record.confidence,
                    record.document_id,
                    record.raw_object_id,
                    record.raw_object_sha256,
                    raw_ref,
                    record.source_registry_entry_id,
                    record.source_registry_entry_sha256,
                    record.mapping_version,
                    record.mapping_implementation_sha256,
                    record.draft.payload_model_key,
                    record.draft.payload_schema_sha256,
                    record.draft.payload_sha256,
                    Jsonb(payload_json),
                    Jsonb(record_json),
                    record.is_restatement,
                    record.supersedes_record_id,
                ),
            ).fetchone()
            if inserted is None:
                self._validate_existing(record, payload_json, record_json, raw_ref=raw_ref)
            registration.writer(self.connection, record, payload, source.source_id)
        return inserted is not None

    def _validate_record(
        self,
        record: NormalizedRecordRef,
        payload: BaseModel,
        *,
        raw_ref: str,
        registration: MediumRepositoryRegistration,
    ) -> SourceRegistryEntry:
        if not isinstance(payload, registration.model_type):
            raise MediumIntegrityError(f"{record.draft.semantic_type_id} does not accept {type(payload).__name__}")
        source = self._sources.get(record.source_registry_entry_id)
        if source is None or source.content_sha256 != record.source_registry_entry_sha256:
            raise MediumIntegrityError("normalized record uses an unknown or drifted source entry")
        expected_mapping = registration.mapping_versions.get(source.source_id)
        if expected_mapping is None or record.mapping_version != expected_mapping:
            raise MediumIntegrityError("normalized record mapping version is not active")
        if record.mapping_implementation_sha256 != source.normalizer_implementation_sha256:
            raise MediumIntegrityError("normalized mapping implementation drifted")
        semantic_type = self._types[registration.key]
        if (
            record.draft.payload_model_key != semantic_type.normalized_model_key
            or record.draft.payload_schema_sha256 != semantic_type.schema_fingerprint_sha256
            or canonical_sha256(payload.model_dump(mode="json")) != record.draft.payload_sha256
        ):
            raise MediumIntegrityError("normalized payload drifted from the semantic registry")
        if not raw_ref.startswith("raw.fetches:"):
            raise MediumIntegrityError("normalized raw_ref is not a raw.fetches pointer")
        raw_id = raw_ref.removeprefix("raw.fetches:")
        if not raw_id.isdigit() or int(raw_id) < 1:
            raise MediumIntegrityError("normalized raw_ref is invalid")
        row = self.connection.execute(
            "select payload_sha256, recorded_at from raw.fetches where id = %s",
            (int(raw_id),),
        ).fetchone()
        if row is None or row[0] != record.raw_object_sha256 or record.recorded_at < row[1]:
            raise MediumIntegrityError("normalized record does not match its raw lineage")
        for field_name, expected in (
            ("knowable_at", record.draft.knowable_at),
            ("recorded_at", record.recorded_at),
            ("confidence", record.confidence),
        ):
            actual = getattr(payload, field_name, expected)
            if actual != expected:
                raise MediumIntegrityError(f"typed payload {field_name} does not match its envelope")
        payload_raw_ref = getattr(payload, "raw_ref", record.raw_object_id)
        if payload_raw_ref != record.raw_object_id:
            raise MediumIntegrityError("typed payload raw_ref is not the content-addressed raw object")
        return source

    def _validate_supersession(self, record: NormalizedRecordRef) -> None:
        if record.supersedes_record_id is None:
            return
        row = self.connection.execute(
            """
            select semantic_type_id, semantic_type_version, subject_kind, subject_id,
                   source_registry_entry_id, source_registry_entry_sha256,
                   valid_time = daterange(%s, %s, '[]'), transaction_time
            from staging.normalized_records
            where normalized_record_id = %s
            """,
            (record.draft.valid_from, record.draft.valid_to, record.supersedes_record_id),
        ).fetchone()
        expected = (
            record.draft.semantic_type_id,
            record.draft.semantic_type_version,
            record.draft.subject.kind.value,
            record.draft.subject.id,
            record.source_registry_entry_id,
            record.source_registry_entry_sha256,
        )
        if row is None or row[:6] != expected or row[6] is not True or record.draft.knowable_at <= row[7]:
            raise MediumIntegrityError("supersession must retain its semantic coordinate and advance time")

    def _validate_existing(
        self,
        record: NormalizedRecordRef,
        payload_json: dict[str, Any],
        record_json: dict[str, Any],
        *,
        raw_ref: str,
    ) -> None:
        row = self.connection.execute(
            """
            select content_sha256, payload, record_ref, raw_ref
            from staging.normalized_records
            where normalized_record_id = %s
            """,
            (record.normalized_record_id,),
        ).fetchone()
        expected = (record.content_sha256, payload_json, record_json, raw_ref)
        if row is None or tuple(row) != expected:
            raise MediumConflictError(f"normalized record {record.normalized_record_id} has different content")

    def get(self, normalized_record_id: str) -> StoredMediumRecord | None:
        row = self.connection.execute(
            """
            select record_ref, payload, source_registry_entry_id, raw_ref
            from staging.normalized_records
            where normalized_record_id = %s
            """,
            (normalized_record_id,),
        ).fetchone()
        return None if row is None else self._load_row(row)

    def project(self, normalized_record_id: str, *, as_of: datetime) -> ProvenanceNeutralInput:
        stored = self.get(normalized_record_id)
        if stored is None:
            raise LookupError(normalized_record_id)
        if stored.record.draft.knowable_at > as_of:
            raise MediumIntegrityError("future normalized record cannot be projected")
        return project_provenance_neutral(stored.record, as_of)

    def visible_records(
        self,
        demand: SnapshotDemandCell,
        *,
        as_of: datetime,
        valid_on: date,
    ) -> tuple[StoredMediumRecord, ...]:
        registration = self._registrations.get((demand.semantic_type_id, demand.semantic_type_version))
        if registration is None:
            raise MediumIntegrityError("snapshot demand uses an unregistered semantic coordinate")
        source_entry_ids = self._source_entry_ids(registration)
        rows = self.connection.execute(
            """
            select record_ref, payload, source_registry_entry_id, raw_ref
            from staging.normalized_records
            where semantic_type_id = %s
              and semantic_type_version = %s
              and subject_kind = %s
              and subject_id = %s
              and source_registry_entry_id = any(%s)
              and transaction_time <= %s
              and valid_time @> %s::date
            order by transaction_time, recorded_at, normalized_record_id
            """,
            (
                demand.semantic_type_id,
                demand.semantic_type_version,
                demand.subject.kind.value,
                demand.subject.id,
                list(source_entry_ids),
                as_of,
                valid_on,
            ),
        ).fetchall()
        candidates = tuple(self._load_row(row) for row in rows)
        active = tuple(
            candidate
            for candidate in candidates
            if registration.partition_filter(candidate.payload, demand.partition_key)
            and candidate.record.mapping_version == registration.mapping_versions[candidate.source_id]
        )
        return self._select_logical_winners(active, registration)

    def all_visible_records(
        self,
        *,
        semantic_type_id: str,
        semantic_type_version: str,
        as_of: datetime,
        valid_on: date,
    ) -> tuple[StoredMediumRecord, ...]:
        registration = self._registrations[(semantic_type_id, semantic_type_version)]
        source_entry_ids = self._source_entry_ids(registration)
        rows = self.connection.execute(
            """
            select record_ref, payload, source_registry_entry_id, raw_ref
            from staging.normalized_records
            where semantic_type_id = %s
              and semantic_type_version = %s
              and source_registry_entry_id = any(%s)
              and transaction_time <= %s
              and valid_time @> %s::date
            order by transaction_time, recorded_at, normalized_record_id
            """,
            (
                semantic_type_id,
                semantic_type_version,
                list(source_entry_ids),
                as_of,
                valid_on,
            ),
        ).fetchall()
        candidates = tuple(self._load_row(row) for row in rows)
        active = tuple(
            candidate
            for candidate in candidates
            if candidate.record.mapping_version == registration.mapping_versions[candidate.source_id]
        )
        return self._select_logical_winners(active, registration)

    def _source_entry_ids(
        self,
        registration: MediumRepositoryRegistration,
    ) -> tuple[str, ...]:
        return tuple(
            sorted(
                source.source_registry_entry_id
                for source in self.registry.sources
                if source.source_id in registration.mapping_versions
                and registration.semantic_type_id in source.supported_type_ids
            )
        )

    def _load_row(self, row: Sequence[Any]) -> StoredMediumRecord:
        record_json, payload_json, source_entry_id, raw_ref = row
        record = NormalizedRecordRef.model_validate(record_json)
        registration = self._registrations[(record.draft.semantic_type_id, record.draft.semantic_type_version)]
        payload = registration.model_type.model_validate(payload_json)
        source = self._validate_record(record, payload, raw_ref=raw_ref, registration=registration)
        return StoredMediumRecord(record=record, payload=payload, source_id=source.source_id)

    @staticmethod
    def _select_logical_winners(
        candidates: Sequence[StoredMediumRecord],
        registration: MediumRepositoryRegistration,
    ) -> tuple[StoredMediumRecord, ...]:
        superseded_ids = {
            candidate.record.supersedes_record_id
            for candidate in candidates
            if candidate.record.supersedes_record_id is not None
        }
        grouped: dict[PayloadKey, list[StoredMediumRecord]] = {}
        for candidate in candidates:
            if candidate.record.normalized_record_id in superseded_ids:
                continue
            grouped.setdefault(registration.logical_key(candidate.payload), []).append(candidate)
        winners: list[StoredMediumRecord] = []
        for logical_key in sorted(grouped):
            ranked = [
                (rank, item)
                for item in grouped[logical_key]
                if (rank := registration.source_rank(item.payload, item.source_id)) is not None
            ]
            if not ranked:
                continue
            winning_rank = min(rank for rank, _item in ranked)
            same_rank = [item for rank, item in ranked if rank == winning_rank]
            winners.append(
                max(
                    same_rank,
                    key=lambda item: (
                        item.record.draft.knowable_at,
                        item.record.recorded_at,
                        item.record.normalized_record_id,
                    ),
                )
            )
        return tuple(sorted(winners, key=lambda item: item.record.normalized_record_id))


def project_provenance_neutral(record: NormalizedRecordRef, as_of: datetime) -> ProvenanceNeutralInput:
    return ProvenanceNeutralInput(
        subject=record.draft.subject,
        payload_model_key=record.draft.payload_model_key,
        payload_sha256=record.draft.payload_sha256,
        valid_from=record.draft.valid_from,
        valid_to=record.draft.valid_to,
        confidence=record.confidence,
        as_of=as_of,
    )


def _inserted(connection: Connection[Any], statement: str, parameters: Sequence[Any]) -> bool:
    return connection.execute(statement, parameters).fetchone() is not None


def _common(record: NormalizedRecordRef) -> tuple[Any, ...]:
    return (
        record.normalized_record_id,
        record.draft.subject.kind.value,
        record.draft.subject.id,
    )


def write_filing_document(
    connection: Connection[Any], record: NormalizedRecordRef, payload: BaseModel, _source_id: str
) -> bool:
    filing = cast(FilingDocumentPayload, payload)
    return _inserted(
        connection,
        """
        insert into staging.filing_documents (
            normalized_record_id, document_id, issuer_id, accession, form,
            filing_date, report_period, content_sha256, content_type, valid_time,
            transaction_time, recorded_at, confidence, raw_ref
        ) values (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, daterange(%s, %s, '[]'),
            %s, %s, %s, %s
        ) on conflict (normalized_record_id) do nothing returning normalized_record_id
        """,
        (
            record.normalized_record_id,
            record.document_id,
            record.draft.subject.id,
            filing.accession,
            filing.form,
            filing.filing_date,
            filing.report_period,
            filing.content_sha256,
            filing.content_type,
            record.draft.valid_from,
            record.draft.valid_to,
            record.draft.knowable_at,
            record.recorded_at,
            record.confidence,
            _raw_ref(record, connection),
        ),
    )


def _raw_ref(record: NormalizedRecordRef, connection: Connection[Any]) -> str:
    row = connection.execute(
        "select raw_ref from staging.normalized_records where normalized_record_id = %s",
        (record.normalized_record_id,),
    ).fetchone()
    if row is None:
        raise MediumIntegrityError("normalized record disappeared before projection")
    return cast(str, row[0])


def write_market_price(
    connection: Connection[Any], record: NormalizedRecordRef, payload: BaseModel, _source_id: str
) -> bool:
    price = cast(MarketPricePayload, payload)
    raw_ref = _raw_ref(record, connection)
    return _inserted(
        connection,
        """
        insert into staging.mvp_market_prices (
            normalized_record_id, subject_kind, subject_id, input_id, issuer_id,
            security_id, listing_id, share_class, exchange_mic, ticker, calendar_id,
            calendar_version, trading_date, session_close_at, open, high, low, close,
            volume, currency, price_basis, confidence_policy_id, price_policy_id,
            valid_time, transaction_time, recorded_at, confidence, raw_ref
        ) values (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, daterange(%s, %s, '[]'),
            %s, %s, %s, %s
        ) on conflict (normalized_record_id) do nothing returning normalized_record_id
        """,
        (
            *_common(record),
            price.input_id,
            price.issuer_id,
            price.security_id,
            price.listing_id,
            price.share_class,
            price.exchange_mic,
            price.ticker,
            price.calendar_id,
            price.calendar_version,
            price.trading_date,
            price.session_close_at,
            price.open,
            price.high,
            price.low,
            price.close,
            price.volume,
            price.currency,
            price.price_basis.value,
            price.confidence_policy_id,
            price.price_policy_id,
            record.draft.valid_from,
            record.draft.valid_to,
            record.draft.knowable_at,
            record.recorded_at,
            record.confidence,
            raw_ref,
        ),
    )


def write_financial_fact(
    connection: Connection[Any], record: NormalizedRecordRef, payload: BaseModel, _source_id: str
) -> bool:
    fact = cast(FinancialFact, payload)
    raw_ref = _raw_ref(record, connection)
    return _inserted(
        connection,
        """
        insert into staging.mvp_financial_facts (
            normalized_record_id, subject_kind, subject_id, entity_id, metric, value,
            unit, fiscal_period, source_metric, mapping_version, accession, form,
            is_restatement, valid_time, transaction_time, recorded_at, confidence, raw_ref
        ) values (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            daterange(%s, %s, '[]'), %s, %s, %s, %s
        ) on conflict (normalized_record_id) do nothing returning normalized_record_id
        """,
        (
            *_common(record),
            fact.entity_id,
            fact.metric,
            fact.value,
            fact.unit,
            fact.fiscal_period,
            fact.source_metric,
            fact.mapping_version,
            fact.accession,
            fact.form,
            fact.is_restatement,
            record.draft.valid_from,
            record.draft.valid_to,
            record.draft.knowable_at,
            record.recorded_at,
            record.confidence,
            raw_ref,
        ),
    )


def write_corporate_action(
    connection: Connection[Any], record: NormalizedRecordRef, payload: BaseModel, _source_id: str
) -> bool:
    action = cast(CorporateAction, payload)
    raw_ref = _raw_ref(record, connection)
    return _inserted(
        connection,
        """
        insert into staging.mvp_corporate_actions (
            normalized_record_id, subject_kind, subject_id, action_id, action_type,
            security_id, share_class, source_instrument_ids, resulting_instrument_ids,
            source_listing_id, resulting_listing_id, declared_at, ex_at, effective_at,
            record_at, pay_at, split_ratio_after_per_before, cash_amount_per_share,
            cash_currency, old_symbol, new_symbol, delisting_reason, valid_time,
            transaction_time, recorded_at, confidence, raw_ref
        ) values (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, daterange(%s, %s, '[]'),
            %s, %s, %s, %s
        ) on conflict (normalized_record_id) do nothing returning normalized_record_id
        """,
        (
            *_common(record),
            action.action_id,
            action.action_type.value,
            action.security_id,
            action.share_class,
            list(action.source_instrument_ids),
            list(action.resulting_instrument_ids),
            action.source_listing_id,
            action.resulting_listing_id,
            action.declared_at,
            action.ex_at,
            action.effective_at,
            action.record_at,
            action.pay_at,
            action.split_ratio_after_per_before,
            action.cash_amount_per_share,
            action.cash_currency,
            action.old_symbol,
            action.new_symbol,
            action.delisting_reason,
            record.draft.valid_from,
            record.draft.valid_to,
            record.draft.knowable_at,
            record.recorded_at,
            record.confidence,
            raw_ref,
        ),
    )


def write_universe_membership(
    connection: Connection[Any], record: NormalizedRecordRef, payload: BaseModel, _source_id: str
) -> bool:
    membership = cast(UniverseMembership, payload)
    raw_ref = _raw_ref(record, connection)
    return _inserted(
        connection,
        """
        insert into staging.mvp_universe_memberships (
            normalized_record_id, subject_kind, subject_id, membership_id, universe_id,
            valid_time, transaction_time, recorded_at, confidence, raw_ref
        ) values (
            %s, %s, %s, %s, %s, daterange(%s, %s, '[]'), %s, %s, %s, %s
        ) on conflict (normalized_record_id) do nothing returning normalized_record_id
        """,
        (
            *_common(record),
            membership.membership_id,
            membership.universe_id,
            record.draft.valid_from,
            record.draft.valid_to,
            record.draft.knowable_at,
            record.recorded_at,
            record.confidence,
            raw_ref,
        ),
    )


def write_issuer_security_link(
    connection: Connection[Any], record: NormalizedRecordRef, payload: BaseModel, _source_id: str
) -> bool:
    link = cast(IssuerSecurityLink, payload)
    raw_ref = _raw_ref(record, connection)
    return _inserted(
        connection,
        """
        insert into staging.mvp_issuer_security_links (
            normalized_record_id, subject_kind, subject_id, input_id, issuer_id,
            security_id, security_kind, share_class, underlying_security_id,
            underlying_shares_per_security_unit, valid_time, transaction_time,
            recorded_at, confidence, raw_ref
        ) values (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            daterange(%s, %s, '[]'), %s, %s, %s, %s
        ) on conflict (normalized_record_id) do nothing returning normalized_record_id
        """,
        (
            *_common(record),
            link.input_id,
            link.issuer_id,
            link.security_id,
            link.security_kind.value,
            link.share_class,
            link.underlying_security_id,
            link.underlying_shares_per_security_unit,
            record.draft.valid_from,
            record.draft.valid_to or record.draft.valid_from,
            record.draft.knowable_at,
            record.recorded_at,
            record.confidence,
            raw_ref,
        ),
    )


def write_security_listing_link(
    connection: Connection[Any], record: NormalizedRecordRef, payload: BaseModel, _source_id: str
) -> bool:
    link = cast(SecurityListingLink, payload)
    raw_ref = _raw_ref(record, connection)
    return _inserted(
        connection,
        """
        insert into staging.mvp_security_listing_links (
            normalized_record_id, subject_kind, subject_id, input_id, security_id,
            listing_id, exchange_mic, ticker, listing_role, currency, timezone,
            trading_calendar_id, trading_calendar_version, valid_time,
            transaction_time, recorded_at, confidence, raw_ref
        ) values (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            daterange(%s, %s, '[]'), %s, %s, %s, %s
        ) on conflict (normalized_record_id) do nothing returning normalized_record_id
        """,
        (
            *_common(record),
            link.input_id,
            link.security_id,
            link.listing_id,
            link.exchange_mic,
            link.ticker,
            link.listing_role.value,
            link.currency,
            link.timezone,
            link.trading_calendar_id,
            link.trading_calendar_version,
            record.draft.valid_from,
            record.draft.valid_to or record.draft.valid_from,
            record.draft.knowable_at,
            record.recorded_at,
            record.confidence,
            raw_ref,
        ),
    )


def _all_partitions(_payload: BaseModel, partition_key: str) -> bool:
    return partition_key == "all"


def _financial_partition(payload: BaseModel, partition_key: str) -> bool:
    return partition_key in {"all", cast(FinancialFact, payload).fiscal_period}


def _price_partition(payload: BaseModel, partition_key: str) -> bool:
    price = cast(MarketPricePayload, payload)
    return partition_key == "all" or partition_key == f"date:{price.trading_date.isoformat()}"


def _source_rank(source_ids: Sequence[str]) -> SourceRank:
    ranks = {source_id: rank for rank, source_id in enumerate(source_ids)}

    def rank(_payload: BaseModel, source_id: str) -> int | None:
        return ranks.get(source_id)

    return rank


def build_medium_repository_registrations(
    registry: RegistrySnapshot,
) -> tuple[MediumRepositoryRegistration, ...]:
    behavior: dict[str, tuple[type[BaseModel], ProjectionWriter, LogicalKey, PartitionFilter]] = {
        FILING_SEMANTIC_TYPE_ID: (
            FilingDocumentPayload,
            write_filing_document,
            lambda payload: (cast(FilingDocumentPayload, payload).accession,),
            _all_partitions,
        ),
        "semantic.market-price": (
            MarketPricePayload,
            write_market_price,
            lambda payload: (cast(MarketPricePayload, payload).trading_date.isoformat(),),
            _price_partition,
        ),
        FINANCIAL_FACT_TYPE_ID: (
            FinancialFact,
            write_financial_fact,
            lambda payload: (
                cast(FinancialFact, payload).metric,
                cast(FinancialFact, payload).fiscal_period,
            ),
            _financial_partition,
        ),
        CORPORATE_ACTION_TYPE_ID: (
            CorporateAction,
            write_corporate_action,
            lambda payload: (cast(CorporateAction, payload).action_id,),
            _all_partitions,
        ),
        UNIVERSE_MEMBERSHIP_TYPE_ID: (
            UniverseMembership,
            write_universe_membership,
            lambda payload: (cast(UniverseMembership, payload).membership_id,),
            _all_partitions,
        ),
        ISSUER_SECURITY_TYPE_ID: (
            IssuerSecurityLink,
            write_issuer_security_link,
            lambda payload: (cast(IssuerSecurityLink, payload).input_id,),
            _all_partitions,
        ),
        SECURITY_LISTING_TYPE_ID: (
            SecurityListingLink,
            write_security_listing_link,
            lambda payload: (cast(SecurityListingLink, payload).input_id,),
            _all_partitions,
        ),
    }
    registrations: list[MediumRepositoryRegistration] = []
    for semantic_type in registry.semantic_types:
        try:
            model_type, writer, logical_key, partition_filter = behavior[semantic_type.semantic_type_id]
        except KeyError as error:
            raise ValueError(f"no medium repository behavior for {semantic_type.semantic_type_id}") from error
        sources = tuple(
            source for source in registry.sources if semantic_type.semantic_type_id in source.supported_type_ids
        )
        mapping_versions = {
            source.source_id: (
                "fixture-sec-filing:1.0.0"
                if semantic_type.semantic_type_id == FILING_SEMANTIC_TYPE_ID
                else "fixture-yahoo-csv:1.0.0"
                if semantic_type.semantic_type_id == "semantic.market-price"
                else f"{source.normalizer_id}:{source.normalizer_version}"
            )
            for source in sources
        }
        registrations.append(
            MediumRepositoryRegistration(
                semantic_type_id=semantic_type.semantic_type_id,
                semantic_type_version=semantic_type.version,
                model_type=model_type,
                repository_key=semantic_type.repository_key,
                projector_key=semantic_type.projector_key,
                mapping_versions=mapping_versions,
                writer=writer,
                logical_key=logical_key,
                partition_filter=partition_filter,
                source_rank=_source_rank(tuple(source.source_id for source in sources)),
            )
        )
    return tuple(registrations)


__all__ = [
    "MediumConflictError",
    "MediumIntegrityError",
    "MediumRepositoryRegistration",
    "PostgresMediumSemanticRepository",
    "StoredMediumRecord",
    "attach_normalized_lineage",
    "build_medium_repository_registrations",
    "project_provenance_neutral",
]
