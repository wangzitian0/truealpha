"""Exact-snapshot orchestration and bounded reads for Production TOPT core results."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import cache, partial
from typing import Any, Self

from factors.composite.strategy_evaluator import deciding_input_keys
from factors.panel import IssuerClass, definition_pins
from factors.production_topt import (
    OPTIONAL_SNAPSHOT_FIELDS,
    GppeV0Definition,
    MetricAvailability,
    MetricFreshness,
    OperatingBranch,
    ToptCellQualityInput,
    ToptCoreReasonCode,
    ToptCoreResult,
    ToptCoreSnapshotInput,
    ToptGppeResult,
    ToptMarketValueComponent,
    ToptMetricInput,
    compute_topt_core,
    compute_topt_gppe,
    drop_absent_banking_leaves,
)
from psycopg import Connection
from psycopg.types.json import Jsonb
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.concept_mapping import ConceptMappingRuleset
from truealpha_contracts.environment_contract import UnknownUniverse, load_environment_contract
from truealpha_contracts.execution import InputEvidenceStatus
from truealpha_contracts.strategy import LargeModelValueV0Definition, load_live_strategy_definition
from truealpha_runtime.storage import StorageError

from data_engine import raw_store
from data_engine.datahub.input_cells import InputCell, financial_input_rows, last_close_cell
from data_engine.datahub.market_prices import SPLIT_ADJUSTED, UNADJUSTED
from data_engine.datahub.price_series import load_daily_series
from data_engine.datahub.primary_listing import choose_primary_listings
from data_engine.datahub.production_topt.concept_mapping import DEFAULT_RULESET, resolve_ruleset
from data_engine.datahub.production_topt.metric_node_values import (
    load_financial_payloads_by_listing,
    materialize_metric_node_values,
)
from data_engine.datahub.production_topt.source_registrations import SEMANTIC_TYPES
from data_engine.datahub.production_topt.status_dimensions import (
    StatusDimensions,
    availability_status_for,
    factor_validation_status_for,
    resolve_raw_pointers,
    source_evidence_status_for,
)
from data_engine.datahub.share_basis import (
    MISSING_CLASS_RATIO,
    NO_SPLIT_ADJUSTED_SERIES,
    SHARES_KEY,
    LiveShareCount,
    OnCloseBasis,
    SplitHistory,
    Unavailable,
    live_on_close_basis,
    split_history,
)
from data_engine.datahub.share_count_entry import share_count_entry

log = logging.getLogger(__name__)

_REQUIRED_TYPES = frozenset(SEMANTIC_TYPES)
_SEMANTICS_PER_LISTING = len(_REQUIRED_TYPES)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _reject_float(value: Any) -> Any:
    if isinstance(value, float):
        raise ValueError("binary float is forbidden; normalized numerics must use base-10 strings")
    return value


def _insert_or_verify(
    connection: Connection[Any],
    *,
    insert_sql: str,
    insert_params: Sequence[Any],
    check_sql: str,
    check_params: Sequence[Any],
    expected: tuple[Any, ...],
    conflict_message: str,
) -> None:
    if connection.execute(insert_sql, insert_params).fetchone() is None:
        existing = connection.execute(check_sql, check_params).fetchone()
        if existing != expected:
            raise ValueError(conflict_message)


class FinancialFactPayload(_FrozenModel):
    issuer_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    listing_id: str = Field(min_length=1)
    operating_branch: OperatingBranch
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    gross_profit: Decimal | None
    total_assets: Decimal | None
    headcount: Decimal | None
    revenue: Decimal | None
    shares_outstanding: Decimal | None
    shares_basis: str | None = None
    pre_provision_profit: Decimal | None
    operating_period_end: date | None = None
    revenue_period_end: date | None = None
    operating_period_stale: bool | None = None
    stale_operating_period_end: date | None = None
    registry_unavailable: bool | None = None
    shares_period_end: date | None = None
    bank_net_revenue: Decimal | None = None
    stockholders_equity: Decimal | None = None
    preferred_stock_value: Decimal | None = None
    goodwill: Decimal | None = None
    intangible_assets_net_excluding_goodwill: Decimal | None = None
    intangible_basis: str | None = None
    tangible_equity_period_end: date | None = None
    net_income: Decimal | None = None
    net_income_by_period: dict[str, Decimal] | None = None
    vintage: dict[str, Any] | None = None
    financial_assets: Decimal | None = None
    financial_returns: Decimal | None = None
    financial_basis: dict[str, Any] | None = None
    earnings_cagr_3y: Decimal | None = None
    earnings_cagr_base_period_end: date | None = None
    earnings_cagr_latest_period_end: date | None = None
    listing_units_per_share: Decimal | None = None
    ratio_pattern: str | None = None
    ratio_title: str | None = None
    ratio_reason: str | None = None
    ratio_accession: str | None = None
    ratio_form: str | None = None
    ratio_filed: date | None = None

    @field_validator(
        "gross_profit",
        "total_assets",
        "headcount",
        "revenue",
        "shares_outstanding",
        "pre_provision_profit",
        "net_income",
        "earnings_cagr_3y",
        "listing_units_per_share",
        "bank_net_revenue",
        "financial_assets",
        "financial_returns",
        "stockholders_equity",
        "preferred_stock_value",
        "goodwill",
        "intangible_assets_net_excluding_goodwill",
        mode="before",
    )
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        return _reject_float(value)

    @field_validator("net_income_by_period", mode="before")
    @classmethod
    def reject_binary_float_in_series(cls, value: Any) -> Any:
        if isinstance(value, dict):
            for entry in value.values():
                _reject_float(entry)
        return value


class MarketPricePayload(_FrozenModel):
    issuer_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    listing_id: str = Field(min_length=1)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    close: Decimal | None
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    volume: Decimal | None = None
    is_provisional: bool = False
    served_by_failover: str | None = Field(default=None, min_length=1)

    @field_validator("close", "open", "high", "low", "volume", mode="before")
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        return _reject_float(value)


class IdentityPayload(_FrozenModel):
    issuer_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    listing_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1)


class SnapshotMember(_FrozenModel):
    issuer_id: str
    instrument_id: str
    listing_id: str
    operating_branch: OperatingBranch
    observation_ids: tuple[str, str, str, str]
    cell_inputs: tuple[ToptCellQualityInput, ToptCellQualityInput, ToptCellQualityInput, ToptCellQualityInput]
    gross_profit: ToptMetricInput | None
    total_assets: ToptMetricInput | None
    headcount: ToptMetricInput | None
    revenue: ToptMetricInput | None
    pre_provision_profit: ToptMetricInput | None
    shares_outstanding: ToptMetricInput
    market_price: ToptMetricInput
    is_primary: bool = False
    stockholders_equity: ToptMetricInput | None = None
    preferred_stock_value: ToptMetricInput | None = None
    goodwill: ToptMetricInput | None = None
    intangible_assets_net_excluding_goodwill: ToptMetricInput | None = None
    financial_assets: ToptMetricInput | None = None
    financial_returns: ToptMetricInput | None = None
    bank_net_revenue: ToptMetricInput | None = None

    @model_serializer(mode="wrap")
    def omit_absent_optional_fields(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if not self.is_primary:
            data.pop("is_primary", None)
        return drop_absent_banking_leaves(data)

    @field_validator("observation_ids")
    @classmethod
    def canonical_observations(cls, values: tuple[str, str, str, str]) -> tuple[str, str, str, str]:
        if tuple(sorted(values)) != values:
            raise ValueError("snapshot member requires sorted observations")
        return values


class ToptCoreSnapshot(_FrozenModel):
    snapshot_id: str = Field(default="", pattern=r"^(?:|topt-core-snapshot:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    run_id: str = Field(pattern=r"^capture-run:[0-9a-f]{64}$")
    release_manifest_id: str = Field(pattern=r"^release-manifest:[0-9a-f]{64}$")
    universe_id: str
    universe_version: str
    universe_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cutoff: datetime
    members: tuple[SnapshotMember, ...]

    @field_validator("cutoff")
    @classmethod
    def normalize_cutoff(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("snapshot cutoff must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        members = tuple(sorted(self.members, key=lambda member: member.instrument_id))
        if not members:
            raise ValueError("a core snapshot must contain at least one member")
        if len({member.instrument_id for member in members}) != len(members):
            raise ValueError("core snapshot instrument identities are not unique")
        if len({member.listing_id for member in members}) != len(members):
            raise ValueError("core snapshot listing identities are not unique")
        observations = {item for member in members for item in member.observation_ids}
        if len(observations) != _SEMANTICS_PER_LISTING * len(members):
            raise ValueError("core snapshot must bind exactly four distinct observations per member")
        object.__setattr__(self, "members", members)
        payload = self.model_dump(mode="json", exclude={"snapshot_id", "content_sha256"})
        digest = canonical_sha256(payload)
        expected_id = f"topt-core-snapshot:{digest}"
        if self.snapshot_id not in {"", expected_id} or self.content_sha256 not in {"", digest}:
            raise ValueError("TOPT snapshot identity does not match its canonical content")
        object.__setattr__(self, "snapshot_id", expected_id)
        object.__setattr__(self, "content_sha256", digest)
        return self

    def factor_inputs(self) -> tuple[ToptCoreSnapshotInput, ...]:
        grouped: dict[str, list[SnapshotMember]] = {}
        for member in self.members:
            grouped.setdefault(member.issuer_id, []).append(member)
        return tuple(self._issuer_factor_input(grouped[issuer_id]) for issuer_id in sorted(grouped))

    def _issuer_factor_input(self, members: list[SnapshotMember]) -> ToptCoreSnapshotInput:
        ordered = sorted(members, key=lambda member: (not member.is_primary, member.instrument_id))
        execution = ordered[0]
        if sum(member.is_primary for member in ordered) > 1:
            raise ValueError(f"issuer {execution.issuer_id} has more than one primary listing")
        undeclared = len(ordered) > 1 and not execution.is_primary
        if len({member.operating_branch for member in ordered}) != 1:
            raise ValueError(f"issuer {execution.issuer_id} has inconsistent operating branches")

        def common_metric(field_name: str) -> ToptMetricInput | None:
            metrics = [getattr(member, field_name) for member in ordered]
            signatures = {None if metric is None else (metric.value, metric.availability) for metric in metrics}
            if len(signatures) != 1:
                raise ValueError(f"issuer {execution.issuer_id} has inconsistent {field_name} facts")
            return metrics[0]

        observations = tuple(sorted(item for member in ordered for item in member.observation_ids))
        cells = tuple(
            sorted((item for member in ordered for item in member.cell_inputs), key=lambda item: item.input_id)
        )
        components = tuple(
            ToptMarketValueComponent(
                instrument_id=member.instrument_id,
                listing_id=member.listing_id,
                market_price=member.market_price,
                shares_outstanding=member.shares_outstanding,
            )
            for member in ordered
        )
        return ToptCoreSnapshotInput(
            snapshot_id=self.snapshot_id,
            run_id=self.run_id,
            release_manifest_id=self.release_manifest_id,
            universe_id=self.universe_id,
            universe_version=self.universe_version,
            universe_sha256=self.universe_sha256,
            cutoff=self.cutoff,
            issuer_id=execution.issuer_id,
            instrument_id=execution.instrument_id,
            listing_id=execution.listing_id,
            operating_branch=execution.operating_branch,
            observation_ids=observations,
            cell_inputs=cells,
            gross_profit=common_metric("gross_profit"),
            total_assets=common_metric("total_assets"),
            headcount=common_metric("headcount"),
            revenue=common_metric("revenue"),
            pre_provision_profit=common_metric("pre_provision_profit"),
            market_value_components=components,
            primary_listing_undeclared=undeclared,
            inputs={name: val for name in OPTIONAL_SNAPSHOT_FIELDS if (val := common_metric(name)) is not None},
        )


@dataclass(frozen=True)
class ToptCoreIdentity:
    run_id: str
    release_manifest_id: str
    universe_id: str
    universe_version: str
    universe_sha256: str
    snapshot_id: str
    invocation_id: str


@dataclass(frozen=True)
class ToptCoreReadResult:
    """One `mart.topt_core_results` row read through `results()`."""

    result_id: str
    invocation_id: str
    snapshot_id: str
    run_id: str
    release_manifest_id: str
    universe_id: str
    universe_version: str
    universe_sha256: str
    cutoff: datetime
    issuer_id: str
    instrument_id: str
    listing_id: str
    operating_branch: str
    operating_metric: str
    availability: str
    operating_efficiency: Decimal | None
    capital_adjusted_gross_profit: Decimal | None
    gppe: Decimal | None
    tier: str | None
    target_ps_lower: Decimal | None
    target_ps_upper: Decimal | None
    target_ps_midpoint: Decimal | None
    current_ps: Decimal | None
    current_pgp: Decimal | None
    target_pgp_lower: Decimal | None
    target_pgp_upper: Decimal | None
    target_pgp_midpoint: Decimal | None
    valuation_gap: Decimal | None
    confidence: Decimal
    freshness: str
    reason_codes: tuple[str, ...]
    gppe_invocation_id: str
    gppe_result_id: str
    gppe_definition_id: str
    gppe_definition_sha256: str
    tier_definition_id: str
    tier_definition_sha256: str
    created_at: datetime

    @property
    def target_ps_is_implied(self) -> bool:
        return self.target_pgp_midpoint is not None


@dataclass(frozen=True)
class ToptCoreMetaInfo:
    result_id: str
    invocation_id: str
    snapshot_id: str
    run_id: str
    release_manifest_id: str
    universe_id: str
    universe_version: str
    universe_sha256: str
    cutoff: datetime
    issuer_id: str
    instrument_id: str
    listing_id: str
    input_observation_ids: tuple[str, ...]
    gppe_invocation_id: str
    gppe_result_id: str
    gppe_definition_id: str
    gppe_definition_sha256: str
    tier_definition_id: str
    tier_definition_sha256: str
    confidence: Decimal
    freshness: str
    created_at: datetime
    lineage: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class _ObservationRow:
    obligation_id: str
    listing_id: str
    semantic_type: str
    observation_id: str
    freshness: MetricFreshness
    knowable_at: datetime
    payload: dict[str, Any]
    confidence: Decimal | None = None


def _metric(
    row: _ObservationRow,
    *,
    name: str,
    value: Decimal | None,
    cell: InputCell | None = None,
    confidence: Decimal | None = None,
    knowable_at: datetime | None = None,
    unavailable_reason: ToptCoreReasonCode | None = None,
) -> ToptMetricInput:
    resolved_knowable = (
        knowable_at if knowable_at is not None else (row.knowable_at if cell is None else cell.knowable_at)
    )
    resolved_confidence = (
        cell.confidence
        if cell is not None
        else (
            confidence if confidence is not None else (row.confidence if row.confidence is not None else Decimal("0"))
        )
    )
    return ToptMetricInput(
        input_id=row.observation_id,
        metric=name,
        value=value,
        confidence=resolved_confidence,
        knowable_at=resolved_knowable,
        freshness=row.freshness,
        availability=MetricAvailability.AVAILABLE if value is not None else MetricAvailability.UNAVAILABLE,
        unavailable_reason=unavailable_reason,
    )


def _price_in_financial_currency(
    listing_id: str, financial_currency: str, price: Any
) -> tuple[Decimal | None, ToptCoreReasonCode | None]:
    if financial_currency == price.currency:
        return price.close, None
    log.warning(
        "currency mismatch: %s financial currency %s does not match market price currency %s",
        listing_id,
        financial_currency,
        price.currency,
    )
    return None, ToptCoreReasonCode.CURRENCY_MISMATCH


@dataclass(frozen=True)
class InputAgeLimit:
    """Maximum input age limit for a core snapshot at one cutoff (#1518)."""

    cutoff: datetime
    maximum_age: timedelta
    deciding: Mapping[str, frozenset[str]]

    def is_stale(self, issuer_class: str, cells: Mapping[str, InputCell]) -> bool:
        keys = self.deciding.get(issuer_class, frozenset())
        return any(self.cutoff - cell.knowable_at > self.maximum_age for key, cell in cells.items() if key in keys)

    def is_period_stale(self, period_end: date | None) -> bool:
        return period_end is not None and (self.cutoff.astimezone(UTC).date() - period_end) > self.maximum_age


def _period_checked_value(
    value: Decimal | None,
    period_end: date | None,
    input_age: InputAgeLimit | None,
) -> tuple[Decimal | None, ToptCoreReasonCode | None]:
    if value is not None and input_age is not None and input_age.is_period_stale(period_end):
        return None, ToptCoreReasonCode.STALE_PERIOD
    return value, None


def _override_confidence(
    listing_confidences: Mapping[str, Decimal] | None,
    key: str,
    fallback: Decimal | None,
) -> Decimal | None:
    return listing_confidences[key] if listing_confidences is not None and key in listing_confidences else fallback


def _validate_member_rows(
    listing_id: str,
    by_type: dict[str, _ObservationRow],
) -> tuple[IdentityPayload, FinancialFactPayload, MarketPricePayload]:
    if set(by_type) != _REQUIRED_TYPES:
        raise ValueError(f"listing {listing_id} does not have the exact four TOPT semantic cells")
    listing = IdentityPayload.model_validate(by_type["listing-identity"].payload)
    membership = IdentityPayload.model_validate(by_type["universe-membership"].payload)
    financial = FinancialFactPayload.model_validate(by_type["financial-fact"].payload)
    price = MarketPricePayload.model_validate(by_type["market-price"].payload)
    coordinates = {(item.issuer_id, item.instrument_id, item.listing_id) for item in (listing, membership, financial)}
    if (
        coordinates != {(listing.issuer_id, listing.instrument_id, listing.listing_id)}
        or price.listing_id != listing.listing_id
    ):
        raise ValueError(f"listing {listing_id} normalized payload identities disagree")
    return listing, financial, price


def _shares_cell_at(cell: InputCell | None, knowable_at: datetime) -> dict[str, InputCell]:
    return {} if cell is None else {SHARES_KEY: cell._replace(knowable_at=knowable_at)}


def _cell(row: _ObservationRow, confidence: Decimal | None = None) -> ToptCellQualityInput:
    return ToptCellQualityInput(
        input_id=row.observation_id,
        confidence=confidence
        if confidence is not None
        else (row.confidence if row.confidence is not None else Decimal("1")),
        knowable_at=row.knowable_at,
        freshness=row.freshness,
    )


def _observation_documents(connection: Connection[Any], observation_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        select v.raw_fetch_id
        from staging.capture_normalized_observations o
        join raw.capture_source_vintages v on v.source_vintage_id = o.source_vintage_id
        where o.observation_id = %s and v.raw_fetch_id is not null
        """,
        (observation_id,),
    ).fetchall()
    documents: list[dict[str, Any]] = []
    for (raw_fetch_id,) in rows:
        try:
            documents.append(json.loads(raw_store.get_payload(connection, raw_fetch_id)))
        except (LookupError, StorageError, ValueError):
            continue
    return documents


def _universe_stores_price_series(universe_id: str) -> bool:
    contract = load_environment_contract()
    try:
        return contract.record(contract.universe_of_id(universe_id)).price_series
    except UnknownUniverse:
        return True


class PostgresToptCoreRepository:
    def __init__(self, connection: Connection[Any]) -> None:
        self._connection = connection

    def freeze_snapshot(self, *, run_id: str, release_manifest_id: str) -> ToptCoreSnapshot:
        existing = self._connection.execute(
            """
            select snapshot_id, content_sha256, release_manifest_id, payload
            from staging.topt_core_snapshots where run_id = %s
            """,
            (run_id,),
        ).fetchone()
        if existing is not None:
            if existing[2] != release_manifest_id:
                raise ValueError("frozen TOPT snapshot is bound to a different release manifest")
            return ToptCoreSnapshot.model_validate(
                {**existing[3], "snapshot_id": existing[0], "content_sha256": existing[1]}
            )
        status = self._connection.execute(
            """
            select environment, cutoff, universe_id, universe_version, universe_sha256,
                   obligation_count, success_count, unchanged_count
            from mart.topt_capture_status where run_id = %s
            """,
            (run_id,),
        ).fetchone()
        if status is None:
            raise LookupError(f"capture run not found: {run_id}")
        environment, cutoff, universe_id, universe_version, universe_sha256, obligations, success, unchanged = status
        identity_row = self._connection.execute("select environment from mart.environment_identity").fetchone()
        governed_env = identity_row[0] if identity_row is not None else "production"
        if environment != governed_env:
            raise ValueError(f"core snapshot requires a {governed_env} run, found a {environment} run")
        if success + unchanged != obligations:
            raise ValueError("core snapshot requires a completely successful run")
        rows = self._load_observations(run_id, cutoff=cutoff)
        if len(rows) != obligations:
            raise ValueError("complete run does not expose one normalized payload per obligation")
        grouped: dict[str, dict[str, _ObservationRow]] = {}
        for row in rows:
            by_type = grouped.setdefault(row.listing_id, {})
            if row.semantic_type in by_type:
                raise ValueError("Production run selected more than one observation for a listing semantic cell")
            by_type[row.semantic_type] = row

        histories_by_issuer = self._issuer_split_histories(grouped)
        ruleset = cache(partial(resolve_ruleset, self._connection))
        primary_by_listing = self._primary_listings(grouped, cutoff=cutoff)
        definition = load_live_strategy_definition()
        input_age = InputAgeLimit(
            cutoff=cutoff,
            maximum_age=timedelta(days=definition.eligibility.maximum_input_age_days),
            deciding={name: deciding_input_keys(definition, IssuerClass(name)) for name in definition_pins(definition)},
        )
        from data_engine.datahub.confidence_report import evaluate_and_persist_run_cell_confidence

        cell_confidences = evaluate_and_persist_run_cell_confidence(self._connection, run_id, cutoff=cutoff)
        stores_price_series = _universe_stores_price_series(universe_id)
        members = tuple(
            self._snapshot_member(
                listing_id,
                by_type,
                histories=histories_by_issuer.get(
                    str((by_type.get("listing-identity") or next(iter(by_type.values()))).payload.get("issuer_id", "")),
                    (),
                ),
                documents=(
                    partial(_observation_documents, self._connection, by_type["financial-fact"].observation_id)
                    if "financial-fact" in by_type
                    else None
                ),
                ruleset=ruleset,
                is_primary=primary_by_listing.get(listing_id, False),
                input_age=input_age,
                listing_confidences={
                    metric: conf for (subj, metric), conf in cell_confidences.items() if subj == listing_id
                },
                stores_price_series=stores_price_series,
            )
            for listing_id, by_type in grouped.items()
        )
        snapshot = ToptCoreSnapshot(
            run_id=run_id,
            release_manifest_id=release_manifest_id,
            universe_id=universe_id,
            universe_version=universe_version,
            universe_sha256=universe_sha256,
            cutoff=cutoff,
            members=members,
        )
        self._put_snapshot(snapshot)
        return snapshot

    def _primary_listings(
        self, grouped: Mapping[str, Mapping[str, _ObservationRow]], *, cutoff: datetime
    ) -> dict[str, bool]:
        issuer_of: dict[str, str] = {}
        listing_of: dict[str, str] = {}
        for key, by_type in grouped.items():
            payload = (by_type.get("listing-identity") or next(iter(by_type.values()))).payload
            issuer_of[key], listing_of[key] = str(payload.get("issuer_id", "")), str(payload.get("listing_id", ""))
        listings_by_issuer: dict[str, list[str]] = {}
        for key, issuer_id in issuer_of.items():
            listings_by_issuer.setdefault(issuer_id, []).append(listing_of[key])
        chosen = choose_primary_listings(self._connection, listings_by_issuer, known_at=cutoff)
        return {
            key: chosen[issuer_of[key]] == listing_of[key]
            for key in grouped
            if len(listings_by_issuer[issuer_of[key]]) > 1
        }

    def _issuer_split_histories(
        self, grouped: Mapping[str, Mapping[str, _ObservationRow]]
    ) -> dict[str, tuple[SplitHistory | Unavailable | None, ...]]:
        tickers_by_issuer: dict[str, list[str]] = {}
        for by_type in grouped.values():
            listing_row = by_type.get("listing-identity")
            if listing_row is None:
                continue
            issuer_id, ticker = listing_row.payload.get("issuer_id"), listing_row.payload.get("ticker")
            if issuer_id and ticker:
                tickers_by_issuer.setdefault(str(issuer_id), []).append(str(ticker))
        symbols = sorted({ticker for names in tickers_by_issuer.values() for ticker in names})
        if not symbols:
            return {}
        unadjusted = load_daily_series(self._connection, symbols, adjust=UNADJUSTED)
        split_adjusted = load_daily_series(self._connection, symbols, adjust=SPLIT_ADJUSTED)
        by_symbol = {symbol: split_history(symbol, unadjusted, split_adjusted) for symbol in symbols}
        return {
            issuer_id: tuple(by_symbol[symbol] for symbol in sorted(set(names)))
            for issuer_id, names in tickers_by_issuer.items()
        }

    def _load_observations(self, run_id: str, *, cutoff: datetime) -> tuple[_ObservationRow, ...]:
        rows = self._connection.execute(
            """
            with selected as (
                select
                    obligation.obligation_id,
                    obligation.subject_id,
                    obligation.capture_requirement_id,
                    observation.observation_id,
                    case
                        when %(cutoff)s - observation.knowable_at <= coalesce(nullif(policy.semantic_freshness_max_age->>observation.semantic_type, '')::interval, policy.freshness_max_age) then 'fresh'
                        else 'stale'
                    end as cutoff_freshness_state,
                    observation.knowable_at,
                    payload.normalized_payload,
                    count(*) over (partition by obligation.obligation_id) as selection_count,
                    row_number() over (
                        partition by obligation.obligation_id
                        order by observation.knowable_at desc, observation.observation_id desc
                    ) as selection_rank
                from raw.capture_obligations obligation
                join raw.capture_obligation_results terminal
                  on terminal.capture_obligation_id = obligation.obligation_id
                 and terminal.terminal_state in ('success', 'unchanged')
                join raw.capture_attempt_results terminal_attempt
                  on terminal_attempt.attempt_id = terminal.final_attempt_id
                join staging.capture_observation_obligations usage
                  on usage.capture_obligation_id = obligation.obligation_id
                join staging.capture_normalized_observations observation using (observation_id)
                join staging.capture_observation_payloads payload using (observation_id)
                join raw.capture_obligation_work_bindings binding
                  on binding.obligation_id = obligation.obligation_id
                join raw.capture_work_items work using (work_item_id)
                join raw.capture_schedule_policies policy using (schedule_policy_id)
                join raw.capture_source_vintages vintage
                  on vintage.source_vintage_id = observation.source_vintage_id
                 and (
                     vintage.source_request_id = work.source_request_id
                     or terminal_attempt.reused_source_vintage_id = observation.source_vintage_id
                 )
                where obligation.run_id = %(run_id)s
                  and observation.source_vintage_id = coalesce(
                      terminal_attempt.source_vintage_id,
                      terminal_attempt.reused_source_vintage_id
                  )
                  and observation.subject_kind = obligation.subject_kind
                  and observation.subject_id = obligation.subject_id
                  and observation.semantic_type = regexp_replace(obligation.capture_requirement_id, ':v1$', '')
                  and (observation.valid_from at time zone 'UTC')::date
                      <= (%(cutoff)s::timestamptz at time zone 'UTC')::date
                  and (
                      observation.valid_to is null
                      or (observation.valid_to at time zone 'UTC')::date
                          >= (%(cutoff)s::timestamptz at time zone 'UTC')::date
                  )
                  and observation.knowable_at <= %(cutoff)s
            )
            select obligation_id, subject_id,
                   regexp_replace(capture_requirement_id, ':v1$', ''),
                   observation_id, cutoff_freshness_state, knowable_at,
                   normalized_payload, selection_count
            from selected where selection_rank = 1
            order by subject_id, capture_requirement_id
            """,
            {"cutoff": cutoff, "run_id": run_id},
        ).fetchall()
        ambiguous = [row[0] for row in rows if row[7] != 1]
        if ambiguous:
            raise ValueError(
                "Production terminal source vintage does not resolve exactly one "
                f"normalized observation for obligations: {', '.join(ambiguous)}"
            )
        return tuple(
            _ObservationRow(
                obligation_id=row[0],
                listing_id=row[1],
                semantic_type=row[2],
                observation_id=row[3],
                freshness=MetricFreshness(row[4]),
                knowable_at=row[5],
                payload=row[6],
            )
            for row in rows
        )

    @staticmethod
    def _resolve_shares_on_close_basis(
        listing_id: str,
        *,
        financial: FinancialFactPayload,
        financial_row: _ObservationRow,
        market_row: _ObservationRow,
        price: MarketPricePayload,
        price_value: Decimal | None,
        shares_cell: Any,
        histories: Sequence[SplitHistory | Unavailable | None],
        documents: Callable[[], Sequence[Mapping[str, Any]]] | None,
        ruleset: Callable[[], ConceptMappingRuleset] | None,
        stores_price_series: bool = True,
    ) -> tuple[Decimal | None, datetime, ToptCoreReasonCode | None]:
        shares_value = financial.shares_outstanding
        shares_knowable_at = financial_row.knowable_at if shares_cell is None else shares_cell.knowable_at
        if shares_value is None:
            reason = ToptCoreReasonCode.MISSING_CLASS_RATIO if financial.shares_basis == MISSING_CLASS_RATIO else None
            return None, shares_knowable_at, reason
        outcome = live_on_close_basis(
            LiveShareCount(
                shares=shares_value,
                knowable_at=shares_knowable_at,
                entry=share_count_entry(financial.vintage, financial.shares_period_end, financial_row.knowable_at),
                close_session=market_row.knowable_at.astimezone(UTC).date(),
                close_knowable_at=market_row.knowable_at,
                close=price_value if price_value is not None else (price.close or Decimal("1")),
            ),
            histories,
            listing_units_per_share=financial.listing_units_per_share,
            shares_basis=financial.shares_basis,
            documents=documents if documents is not None else (lambda: ()),
            ruleset=ruleset if ruleset is not None else (lambda: DEFAULT_RULESET),
        )
        if isinstance(outcome, OnCloseBasis):
            return outcome.value, outcome.knowable_at, None
        if price_value is None and financial.listing_units_per_share and financial.listing_units_per_share > 0:
            return shares_value / financial.listing_units_per_share, shares_knowable_at, None
        no_bar_stored = len(histories) > 0 and all(history is None for history in histories)
        if not stores_price_series and outcome.reason == NO_SPLIT_ADJUSTED_SERIES and no_bar_stored:
            log.info(
                "share basis: %s has no stored daily bar and its universe declares price_series=false: %s; "
                "the filed share count stays unproven and market_cap stays NULL",
                listing_id,
                outcome.reason,
            )
        else:
            log.warning(
                "share basis: %s gets no share count beside the close %s: %s: %s",
                listing_id,
                price.close,
                outcome.reason,
                outcome.detail,
            )
        return None, shares_knowable_at, ToptCoreReasonCode(outcome.reason)

    @staticmethod
    def _snapshot_member(
        listing_id: str,
        by_type: dict[str, _ObservationRow],
        *,
        histories: Sequence[SplitHistory | Unavailable | None],
        documents: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
        ruleset: Callable[[], ConceptMappingRuleset] | None = None,
        is_primary: bool = False,
        input_age: InputAgeLimit | None = None,
        listing_confidences: Mapping[str, Decimal] | None = None,
        stores_price_series: bool = True,
    ) -> SnapshotMember:
        listing, financial, price = _validate_member_rows(listing_id, by_type)
        financial_row, market_row = by_type["financial-fact"], by_type["market-price"]
        observation_ids = sorted(row.observation_id for row in by_type.values())
        price_value, price_reason = _price_in_financial_currency(listing_id, financial.currency, price)

        cells = {
            cell.input_key: cell
            for cell in financial_input_rows(
                financial_row.payload,
                financial_row.confidence,
                financial_row.knowable_at,
                confidence_by_key=listing_confidences,
            )
            if cell.fiscal_period is None
        }
        close_conf = _override_confidence(listing_confidences, "close", market_row.confidence)
        price_cell = last_close_cell(market_row.payload, close_conf, market_row.knowable_at)

        shares_cell = cells.get(SHARES_KEY)
        shares_value, shares_knowable_at, shares_reason = PostgresToptCoreRepository._resolve_shares_on_close_basis(
            listing_id,
            financial=financial,
            financial_row=financial_row,
            market_row=market_row,
            price=price,
            price_value=price_value,
            shares_cell=shares_cell,
            histories=histories,
            documents=documents,
            ruleset=ruleset,
            stores_price_series=stores_price_series,
        )

        if input_age is not None and input_age.is_stale(
            financial.operating_branch.value, {**cells, **_shares_cell_at(shares_cell, shares_knowable_at)}
        ):
            financial_row = replace(financial_row, freshness=MetricFreshness.STALE)
        deciding_keys = input_age.deciding.get(financial.operating_branch.value) if input_age is not None else None
        xbrl_confs = [
            cell.confidence
            for key, cell in cells.items()
            if key != "headcount"
            and not (financial.operating_branch is OperatingBranch.FINANCIAL and key == "revenue")
            and (deciding_keys is None or key in deciding_keys)
        ]
        cell_confs = (xbrl_confs or [cell.confidence for cell in cells.values()]) + (
            [financial_row.confidence] if financial_row.confidence is not None else []
        )
        financial_cell_confidence = min(cell_confs, default=Decimal("0"))
        profit_cell = cells.get("gross_profit")
        ppnr_cell = cells.get("pre_provision_profit") or profit_cell
        gp_value, gp_reason = _period_checked_value(financial.gross_profit, financial.operating_period_end, input_age)
        ppnr_value, ppnr_reason = _period_checked_value(
            financial.pre_provision_profit, financial.operating_period_end, input_age
        )
        rev_value, rev_reason = _period_checked_value(financial.revenue, financial.revenue_period_end, input_age)
        listing_id_conf = _override_confidence(
            listing_confidences, "listing-identity", by_type["listing-identity"].confidence
        )
        market_cell_conf = price_cell.confidence if price_cell is not None else (close_conf or Decimal("0"))
        membership_conf = _override_confidence(
            listing_confidences, "universe-membership", by_type["universe-membership"].confidence
        )

        return SnapshotMember(
            issuer_id=listing.issuer_id,
            instrument_id=listing.instrument_id,
            listing_id=listing.listing_id,
            operating_branch=financial.operating_branch,
            observation_ids=(observation_ids[0], observation_ids[1], observation_ids[2], observation_ids[3]),
            cell_inputs=(
                ToptCellQualityInput(
                    input_id=financial_row.observation_id,
                    confidence=financial_cell_confidence,
                    knowable_at=financial_row.knowable_at,
                    freshness=financial_row.freshness,
                ),
                _cell(by_type["listing-identity"], listing_id_conf),
                _cell(by_type["market-price"], market_cell_conf),
                _cell(by_type["universe-membership"], membership_conf),
            ),
            gross_profit=_metric(
                financial_row,
                name="gross_profit",
                value=gp_value,
                cell=profit_cell,
                confidence=financial_cell_confidence,
                unavailable_reason=gp_reason,
            ),
            total_assets=_metric(
                financial_row,
                name="total_assets",
                value=financial.total_assets,
                cell=cells.get("total_assets"),
                confidence=financial_cell_confidence,
            ),
            headcount=_metric(
                financial_row,
                name="headcount",
                value=financial.headcount,
                cell=cells.get("headcount"),
                confidence=financial_cell_confidence,
            ),
            revenue=_metric(
                financial_row,
                name="revenue",
                value=rev_value,
                cell=cells.get("revenue"),
                confidence=financial_cell_confidence,
                unavailable_reason=rev_reason,
            ),
            pre_provision_profit=_metric(
                financial_row,
                name="pre_provision_profit",
                value=ppnr_value,
                cell=None if financial.pre_provision_profit is None else ppnr_cell,
                confidence=_override_confidence(listing_confidences, "pre_provision_profit", financial_cell_confidence)
                or financial_cell_confidence,
                unavailable_reason=ppnr_reason,
            ),
            shares_outstanding=_metric(
                financial_row,
                name="shares_outstanding",
                value=shares_value,
                cell=shares_cell,
                confidence=financial_cell_confidence,
                knowable_at=shares_knowable_at,
                unavailable_reason=shares_reason,
            ),
            market_price=_metric(
                market_row,
                name="market_price",
                value=price_value,
                cell=price_cell,
                confidence=market_cell_conf,
                unavailable_reason=price_reason,
            ),
            is_primary=is_primary,
            **{
                name: _metric(
                    financial_row,
                    name=name,
                    value=getattr(financial, name),
                    cell=cells.get(name),
                    confidence=_override_confidence(listing_confidences, name, financial_cell_confidence)
                    or financial_cell_confidence,
                )
                for name in OPTIONAL_SNAPSHOT_FIELDS
                if getattr(financial, name) is not None
            },
        )

    def _put_snapshot(self, snapshot: ToptCoreSnapshot) -> None:
        payload = snapshot.model_dump(mode="json", exclude={"snapshot_id", "content_sha256"})
        with self._connection.transaction():
            _insert_or_verify(
                self._connection,
                insert_sql="""
                    insert into staging.topt_core_snapshots (
                        snapshot_id, content_sha256, run_id, release_manifest_id,
                        universe_id, universe_version, universe_sha256, cutoff,
                        issuer_count, instrument_count, observation_count, payload
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (snapshot_id) do nothing returning snapshot_id
                """,
                insert_params=(
                    snapshot.snapshot_id,
                    snapshot.content_sha256,
                    snapshot.run_id,
                    snapshot.release_manifest_id,
                    snapshot.universe_id,
                    snapshot.universe_version,
                    snapshot.universe_sha256,
                    snapshot.cutoff,
                    len({member.issuer_id for member in snapshot.members}),
                    len(snapshot.members),
                    _SEMANTICS_PER_LISTING * len(snapshot.members),
                    Jsonb(payload),
                ),
                check_sql="select content_sha256, payload from staging.topt_core_snapshots where snapshot_id = %s",
                check_params=(snapshot.snapshot_id,),
                expected=(snapshot.content_sha256, payload),
                conflict_message="TOPT core snapshot identity conflict",
            )
            for factor_input in snapshot.factor_inputs():
                factor_payload = factor_input.model_dump(mode="json")
                member_sha256 = canonical_sha256(factor_payload)
                _insert_or_verify(
                    self._connection,
                    insert_sql="""
                        insert into staging.topt_core_snapshot_members (
                            snapshot_id, instrument_id, issuer_id, listing_id,
                            observation_ids, member_sha256, factor_input
                        ) values (%s, %s, %s, %s, %s, %s, %s)
                        on conflict (snapshot_id, issuer_id) do nothing
                        returning instrument_id
                    """,
                    insert_params=(
                        snapshot.snapshot_id,
                        factor_input.instrument_id,
                        factor_input.issuer_id,
                        factor_input.listing_id,
                        list(factor_input.observation_ids),
                        member_sha256,
                        Jsonb(factor_payload),
                    ),
                    check_sql="""
                        select issuer_id, listing_id, observation_ids, member_sha256, factor_input
                        from staging.topt_core_snapshot_members
                        where snapshot_id = %s and issuer_id = %s
                    """,
                    check_params=(snapshot.snapshot_id, factor_input.issuer_id),
                    expected=(
                        factor_input.issuer_id,
                        factor_input.listing_id,
                        list(factor_input.observation_ids),
                        member_sha256,
                        factor_payload,
                    ),
                    conflict_message="TOPT core snapshot member identity conflict",
                )

    def materialize(
        self,
        snapshot: ToptCoreSnapshot,
        *,
        gppe_definition: GppeV0Definition,
        definition: LargeModelValueV0Definition | None = None,
    ) -> tuple[ToptCoreResult, ...]:
        definition = definition or load_live_strategy_definition()
        factor_inputs = snapshot.factor_inputs()
        gppe_definition = gppe_definition.pinned_by(definition)
        gppe_invocation_payload = {
            "snapshot_id": snapshot.snapshot_id,
            "gppe_definition": gppe_definition.model_dump(mode="json"),
        }
        gppe_invocation_sha256 = canonical_sha256(gppe_invocation_payload)
        gppe_invocation_id = f"topt-gppe-invocation:{gppe_invocation_sha256}"
        gppe_results = tuple(
            compute_topt_gppe(
                factor_input,
                invocation_id=gppe_invocation_id,
                gppe_definition=gppe_definition,
                definition=definition,
            )
            for factor_input in factor_inputs
        )
        invocation_payload = {
            "snapshot_id": snapshot.snapshot_id,
            "gppe_invocation_id": gppe_invocation_id,
            "tier_definition": definition.model_dump(mode="json"),
        }
        invocation_sha256 = canonical_sha256(invocation_payload)
        invocation_id = f"topt-core-invocation:{invocation_sha256}"
        expected_issuers = len({member.issuer_id for member in snapshot.members})
        if len(gppe_results) != expected_issuers:
            raise ValueError("GPPE materialization denominator drifted from the snapshot's issuers")

        with self._connection.transaction():
            _insert_or_verify(
                self._connection,
                insert_sql="""
                    insert into mart.topt_gppe_invocations (
                        invocation_id, content_sha256, snapshot_id,
                        gppe_definition_id, gppe_definition_sha256, payload
                    ) values (%s, %s, %s, %s, %s, %s)
                    on conflict (invocation_id) do nothing returning invocation_id
                """,
                insert_params=(
                    gppe_invocation_id,
                    gppe_invocation_sha256,
                    snapshot.snapshot_id,
                    gppe_definition.definition_id,
                    gppe_definition.content_sha256,
                    Jsonb(gppe_invocation_payload),
                ),
                check_sql="select content_sha256, payload from mart.topt_gppe_invocations where invocation_id = %s",
                check_params=(gppe_invocation_id,),
                expected=(gppe_invocation_sha256, gppe_invocation_payload),
                conflict_message="TOPT GPPE invocation identity conflict",
            )
            evidence_by_listing = self._load_source_evidence(snapshot)
            gppe_validation = factor_validation_status_for((gppe_definition.definition_id,))
            for gppe_result in gppe_results:
                self._put_gppe_result(
                    gppe_result,
                    StatusDimensions(
                        availability_status=availability_status_for(
                            availability=gppe_result.availability.value,
                            freshness=gppe_result.freshness.value,
                            confidence=gppe_result.confidence,
                            reason_codes=(item.value for item in gppe_result.reason_codes),
                        ),
                        source_evidence_status=evidence_by_listing[gppe_result.listing_id],
                        factor_validation_status=gppe_validation,
                    ),
                )
            materialized_gppe_results = self._load_gppe_results(gppe_invocation_id)
            if len(materialized_gppe_results) != expected_issuers:
                raise ValueError("GPPE materialization did not persist the complete denominator")

            results = tuple(
                compute_topt_core(
                    factor_input,
                    gppe_result,
                    invocation_id=invocation_id,
                    definition=definition,
                )
                for factor_input, gppe_result in zip(factor_inputs, materialized_gppe_results, strict=True)
            )
            if len(results) != expected_issuers or len({item.issuer_id for item in results}) != expected_issuers:
                raise ValueError("core materialization denominator drifted from the snapshot's issuers")

            _insert_or_verify(
                self._connection,
                insert_sql="""
                    insert into mart.topt_core_invocations (
                        invocation_id, content_sha256, snapshot_id,
                        gppe_invocation_id,
                        gppe_definition_id, gppe_definition_sha256,
                        tier_definition_id, tier_definition_sha256, payload
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (invocation_id) do nothing returning invocation_id
                """,
                insert_params=(
                    invocation_id,
                    invocation_sha256,
                    snapshot.snapshot_id,
                    gppe_invocation_id,
                    gppe_definition.definition_id,
                    gppe_definition.content_sha256,
                    definition.strategy_definition_id,
                    definition.content_sha256,
                    Jsonb(invocation_payload),
                ),
                check_sql="select content_sha256, payload from mart.topt_core_invocations where invocation_id = %s",
                check_params=(invocation_id,),
                expected=(invocation_sha256, invocation_payload),
                conflict_message="TOPT core invocation identity conflict",
            )
            periods_by_listing = self._load_result_periods(snapshot)
            core_validation = factor_validation_status_for(
                (gppe_definition.definition_id, definition.strategy_definition_id)
            )
            for result in results:
                if result.listing_id not in periods_by_listing:
                    raise ValueError(f"no financial-fact observation for {result.listing_id} in its own snapshot")
                self._put_result(
                    result,
                    periods_by_listing[result.listing_id],
                    StatusDimensions(
                        availability_status=availability_status_for(
                            availability=result.availability.value,
                            freshness=result.freshness.value,
                            confidence=result.confidence,
                            reason_codes=(item.value for item in result.reason_codes),
                        ),
                        source_evidence_status=evidence_by_listing[result.listing_id],
                        factor_validation_status=core_validation,
                    ),
                )
            materialize_metric_node_values(
                self._connection,
                factor_inputs=factor_inputs,
                gppe_definition=gppe_definition,
                tier_definition_id=definition.strategy_definition_id,
                periods_by_listing=periods_by_listing,
                evidence_by_listing=evidence_by_listing,
            )
        return results

    def _load_source_evidence(self, snapshot: ToptCoreSnapshot) -> dict[str, InputEvidenceStatus]:
        all_ids = sorted({oid for member in snapshot.members for oid in member.observation_ids})
        resolved = resolve_raw_pointers(self._connection, all_ids)
        payloads_by_listing = load_financial_payloads_by_listing(self._connection, all_ids)
        return {
            member.listing_id: source_evidence_status_for(
                self._connection,
                member.observation_ids,
                financial_payloads=payloads_by_listing.get(member.listing_id, ()),
                resolved=resolved,
            )
            for member in snapshot.members
        }

    def _load_result_periods(
        self, snapshot: ToptCoreSnapshot
    ) -> dict[str, tuple[date | None, date | None, date | None]]:
        observation_ids = sorted({oid for member in snapshot.members for oid in member.observation_ids})
        rows = self._connection.execute(
            """
            select coalesce(p.normalized_payload->>'listing_id', o.subject_id),
                   p.normalized_payload->>'operating_period_end',
                   p.normalized_payload->>'revenue_period_end',
                   p.normalized_payload->>'shares_period_end'
            from staging.capture_normalized_observations o
            join staging.capture_observation_payloads p using (observation_id)
            where o.observation_id = any(%s) and o.semantic_type = 'financial-fact'
            """,
            (observation_ids,),
        ).fetchall()
        return {
            str(subject_id): (
                None if operating is None else date.fromisoformat(operating),
                None if revenue is None else date.fromisoformat(revenue),
                None if shares is None else date.fromisoformat(shares),
            )
            for subject_id, operating, revenue, shares in rows
        }

    def _load_gppe_results(self, invocation_id: str) -> tuple[ToptGppeResult, ...]:
        rows = self._connection.execute(
            """
            select result_id, content_sha256, payload
            from mart.topt_gppe_results
            where invocation_id = %s
            order by issuer_id
            """,
            (invocation_id,),
        ).fetchall()
        return tuple(
            ToptGppeResult.model_validate({**row[2], "result_id": row[0], "content_sha256": row[1]}) for row in rows
        )

    def _put_gppe_result(self, result: ToptGppeResult, statuses: StatusDimensions) -> None:
        payload = result.model_dump(mode="json", exclude={"result_id", "content_sha256"})
        _insert_or_verify(
            self._connection,
            insert_sql="""
                insert into mart.topt_gppe_results (
                    result_id, content_sha256, invocation_id, snapshot_id, run_id,
                    release_manifest_id, universe_id, universe_version, universe_sha256,
                    cutoff, issuer_id, instrument_id, listing_id, operating_branch,
                    operating_metric, availability, operating_efficiency,
                    capital_adjusted_gross_profit, gppe, confidence, freshness,
                    reason_codes, input_observation_ids,
                    gppe_definition_id, gppe_definition_sha256, payload,
                    availability_status, source_evidence_status, factor_validation_status
                ) values (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s
                ) on conflict (result_id) do nothing returning result_id
            """,
            insert_params=(
                result.result_id,
                result.content_sha256,
                result.invocation_id,
                result.snapshot_id,
                result.run_id,
                result.release_manifest_id,
                result.universe_id,
                result.universe_version,
                result.universe_sha256,
                result.cutoff,
                result.issuer_id,
                result.instrument_id,
                result.listing_id,
                result.operating_branch.value,
                result.operating_metric.value,
                result.availability.value,
                result.operating_efficiency,
                result.capital_adjusted_gross_profit,
                result.gppe,
                result.confidence,
                result.freshness.value,
                [item.value for item in result.reason_codes],
                list(result.input_observation_ids),
                result.gppe_definition_id,
                result.gppe_definition_sha256,
                Jsonb(payload),
                *statuses.as_columns(),
            ),
            check_sql="select content_sha256, payload from mart.topt_gppe_results where result_id = %s",
            check_params=(result.result_id,),
            expected=(result.content_sha256, payload),
            conflict_message="TOPT GPPE result identity conflict",
        )

    def _put_result(
        self,
        result: ToptCoreResult,
        periods: tuple[date | None, date | None, date | None],
        statuses: StatusDimensions,
    ) -> None:
        payload = result.model_dump(mode="json", exclude={"result_id", "content_sha256"})
        _insert_or_verify(
            self._connection,
            insert_sql="""
                insert into mart.topt_core_results (
                    result_id, content_sha256, invocation_id, snapshot_id, run_id,
                    release_manifest_id, universe_id, universe_version, universe_sha256,
                    cutoff, issuer_id, instrument_id, listing_id, operating_branch,
                    operating_metric, availability, operating_efficiency,
                    capital_adjusted_gross_profit, gppe, tier, target_ps_lower,
                    target_ps_upper, target_ps_midpoint, current_ps, valuation_gap,
                    current_pgp, target_pgp_lower, target_pgp_upper, target_pgp_midpoint,
                    operating_period_end, revenue_period_end, shares_period_end,
                    confidence, freshness, reason_codes, input_observation_ids,
                    gppe_invocation_id, gppe_result_id,
                    gppe_definition_id, gppe_definition_sha256,
                    tier_definition_id, tier_definition_sha256, payload,
                    availability_status, source_evidence_status, factor_validation_status
                ) values (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s
                ) on conflict (result_id) do nothing returning result_id
            """,
            insert_params=(
                result.result_id,
                result.content_sha256,
                result.invocation_id,
                result.snapshot_id,
                result.run_id,
                result.release_manifest_id,
                result.universe_id,
                result.universe_version,
                result.universe_sha256,
                result.cutoff,
                result.issuer_id,
                result.instrument_id,
                result.listing_id,
                result.operating_branch.value,
                result.operating_metric.value,
                result.availability.value,
                result.operating_efficiency,
                result.capital_adjusted_gross_profit,
                result.gppe,
                None if result.tier is None else result.tier.value,
                result.target_ps_lower,
                result.target_ps_upper,
                result.target_ps_midpoint,
                result.current_ps,
                result.valuation_gap,
                result.current_pgp,
                result.target_pgp_lower,
                result.target_pgp_upper,
                result.target_pgp_midpoint,
                periods[0],
                periods[1],
                periods[2],
                result.confidence,
                result.freshness.value,
                [item.value for item in result.reason_codes],
                list(result.input_observation_ids),
                result.gppe_invocation_id,
                result.gppe_result_id,
                result.gppe_definition_id,
                result.gppe_definition_sha256,
                result.tier_definition_id,
                result.tier_definition_sha256,
                Jsonb(payload),
                *statuses.as_columns(),
            ),
            check_sql="select content_sha256, payload from mart.topt_core_results where result_id = %s",
            check_params=(result.result_id,),
            expected=(result.content_sha256, payload),
            conflict_message="TOPT core result identity conflict",
        )

    @staticmethod
    def _validate_page(limit: int, offset: int) -> None:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("TOPT core query pagination is outside the bounded range")

    @staticmethod
    def _identity_query_params(identity: ToptCoreIdentity, limit: int, offset: int) -> tuple[Any, ...]:
        return (
            identity.run_id,
            identity.release_manifest_id,
            identity.universe_id,
            identity.universe_version,
            identity.universe_sha256,
            identity.snapshot_id,
            identity.invocation_id,
            limit,
            offset,
        )

    def results(
        self,
        identity: ToptCoreIdentity,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[ToptCoreReadResult, ...]:
        self._validate_page(limit, offset)
        rows = self._connection.execute(
            """
            select r.result_id, r.invocation_id, r.snapshot_id, r.run_id, r.release_manifest_id,
                   r.universe_id, r.universe_version, r.universe_sha256, r.cutoff, r.issuer_id,
                   r.instrument_id, r.listing_id, r.operating_branch, r.operating_metric,
                   r.availability, r.operating_efficiency, r.capital_adjusted_gross_profit,
                   r.gppe, r.tier, r.target_ps_lower, r.target_ps_upper, r.target_ps_midpoint,
                   r.current_ps, r.valuation_gap, r.confidence, r.freshness, r.reason_codes,
                   r.gppe_invocation_id, r.gppe_result_id,
                   r.gppe_definition_id, r.gppe_definition_sha256,
                   r.tier_definition_id, r.tier_definition_sha256, r.created_at,
                   t.current_pgp, t.target_pgp_lower, t.target_pgp_upper, t.target_pgp_midpoint
            from mart.topt_core_result_read r
            join mart.topt_core_results t on t.result_id = r.result_id
            where r.run_id = %s and r.release_manifest_id = %s and r.universe_id = %s
              and r.universe_version = %s and r.universe_sha256 = %s and r.snapshot_id = %s
              and r.invocation_id = %s
            order by r.issuer_id limit %s offset %s
            """,
            self._identity_query_params(identity, limit, offset),
        ).fetchall()
        return tuple(
            ToptCoreReadResult(
                result_id=row[0],
                invocation_id=row[1],
                snapshot_id=row[2],
                run_id=row[3],
                release_manifest_id=row[4],
                universe_id=row[5],
                universe_version=row[6],
                universe_sha256=row[7],
                cutoff=row[8],
                issuer_id=row[9],
                instrument_id=row[10],
                listing_id=row[11],
                operating_branch=row[12],
                operating_metric=row[13],
                availability=row[14],
                operating_efficiency=row[15],
                capital_adjusted_gross_profit=row[16],
                gppe=row[17],
                tier=row[18],
                target_ps_lower=row[19],
                target_ps_upper=row[20],
                target_ps_midpoint=row[21],
                current_ps=row[22],
                current_pgp=row[34],
                target_pgp_lower=row[35],
                target_pgp_upper=row[36],
                target_pgp_midpoint=row[37],
                valuation_gap=row[23],
                confidence=row[24],
                freshness=row[25],
                reason_codes=tuple(row[26]),
                gppe_invocation_id=row[27],
                gppe_result_id=row[28],
                gppe_definition_id=row[29],
                gppe_definition_sha256=row[30],
                tier_definition_id=row[31],
                tier_definition_sha256=row[32],
                created_at=row[33],
            )
            for row in rows
        )

    def meta_info(
        self,
        identity: ToptCoreIdentity,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[ToptCoreMetaInfo, ...]:
        self._validate_page(limit, offset)
        rows = self._connection.execute(
            """
            select result_id, invocation_id, snapshot_id, run_id, release_manifest_id,
                   universe_id, universe_version, universe_sha256, cutoff, issuer_id,
                   instrument_id, listing_id, input_observation_ids,
                   gppe_invocation_id, gppe_result_id,
                   gppe_definition_id, gppe_definition_sha256,
                   tier_definition_id, tier_definition_sha256,
                   confidence, freshness, created_at, lineage
            from mart.topt_core_meta_info
            where run_id = %s and release_manifest_id = %s and universe_id = %s
              and universe_version = %s and universe_sha256 = %s and snapshot_id = %s
              and invocation_id = %s
            order by issuer_id limit %s offset %s
            """,
            self._identity_query_params(identity, limit, offset),
        ).fetchall()
        return tuple(
            ToptCoreMetaInfo(
                result_id=row[0],
                invocation_id=row[1],
                snapshot_id=row[2],
                run_id=row[3],
                release_manifest_id=row[4],
                universe_id=row[5],
                universe_version=row[6],
                universe_sha256=row[7],
                cutoff=row[8],
                issuer_id=row[9],
                instrument_id=row[10],
                listing_id=row[11],
                input_observation_ids=tuple(row[12]),
                gppe_invocation_id=row[13],
                gppe_result_id=row[14],
                gppe_definition_id=row[15],
                gppe_definition_sha256=row[16],
                tier_definition_id=row[17],
                tier_definition_sha256=row[18],
                confidence=row[19],
                freshness=row[20],
                created_at=row[21],
                lineage=tuple(row[22]),
            )
            for row in rows
        )


__all__ = [
    "FinancialFactPayload",
    "IdentityPayload",
    "MarketPricePayload",
    "PostgresToptCoreRepository",
    "SnapshotMember",
    "ToptCoreIdentity",
    "ToptCoreMetaInfo",
    "ToptCoreReadResult",
    "ToptCoreSnapshot",
]
