"""Exact-snapshot orchestration and bounded reads for Production TOPT core results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Self

from factors.production_topt import (
    GppeV0Definition,
    MetricAvailability,
    MetricFreshness,
    OperatingBranch,
    ThreeTierV0Definition,
    ToptCellQualityInput,
    ToptCoreResult,
    ToptCoreSnapshotInput,
    ToptGppeResult,
    ToptMarketValueComponent,
    ToptMetricInput,
    compute_topt_core,
    compute_topt_gppe,
)
from psycopg import Connection
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from truealpha_contracts.common import canonical_sha256

_EXPECTED_ISSUERS = 20
_EXPECTED_INSTRUMENTS = 21
_EXPECTED_OBSERVATIONS = 84
_REQUIRED_TYPES = frozenset(
    {
        "financial-fact",
        "listing-identity",
        "market-price",
        "universe-membership",
    }
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _reject_float(value: Any) -> Any:
    if isinstance(value, float):
        raise ValueError("binary float is forbidden; normalized numerics must use base-10 strings")
    return value


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
    pre_provision_profit: Decimal | None

    @field_validator(
        "gross_profit",
        "total_assets",
        "headcount",
        "revenue",
        "shares_outstanding",
        "pre_provision_profit",
        mode="before",
    )
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        return _reject_float(value)


class MarketPricePayload(_FrozenModel):
    issuer_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    listing_id: str = Field(min_length=1)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    close: Decimal | None

    @field_validator("close", mode="before")
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

    @field_validator("observation_ids")
    @classmethod
    def canonical_observations(cls, values: tuple[str, str, str, str]) -> tuple[str, str, str, str]:
        if len(set(values)) != 4 or tuple(sorted(values)) != values:
            raise ValueError("snapshot member requires four sorted unique observations")
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
        if len(members) != _EXPECTED_INSTRUMENTS:
            raise ValueError("TOPT snapshot must contain exactly 21 instruments")
        if len({member.instrument_id for member in members}) != _EXPECTED_INSTRUMENTS:
            raise ValueError("TOPT snapshot instrument identities are not unique")
        if len({member.listing_id for member in members}) != _EXPECTED_INSTRUMENTS:
            raise ValueError("TOPT snapshot listing identities are not unique")
        if len({member.issuer_id for member in members}) != _EXPECTED_ISSUERS:
            raise ValueError("TOPT snapshot must contain exactly 20 issuers")
        observations = {item for member in members for item in member.observation_ids}
        if len(observations) != _EXPECTED_OBSERVATIONS:
            raise ValueError("TOPT snapshot must bind exactly 84 observations")
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
        ordered = sorted(members, key=lambda member: member.instrument_id)
        execution = ordered[0]
        if len({member.operating_branch for member in ordered}) != 1:
            raise ValueError(f"issuer {execution.issuer_id} has inconsistent operating branches")

        def common_metric(field_name: str) -> ToptMetricInput | None:
            metrics = [getattr(member, field_name) for member in ordered]
            signatures = {
                None if metric is None else (metric.value, metric.unit, metric.availability) for metric in metrics
            }
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
    confidence: Decimal
    freshness: MetricFreshness
    knowable_at: datetime
    payload: dict[str, Any]


def _metric(
    row: _ObservationRow,
    *,
    name: str,
    value: Decimal | None,
    unit: str,
) -> ToptMetricInput:
    return ToptMetricInput(
        input_id=row.observation_id,
        metric=name,
        value=value,
        unit=unit,
        confidence=row.confidence,
        knowable_at=row.knowable_at,
        freshness=row.freshness,
        availability=MetricAvailability.AVAILABLE if value is not None else MetricAvailability.UNAVAILABLE,
    )


def _cell(row: _ObservationRow) -> ToptCellQualityInput:
    return ToptCellQualityInput(
        input_id=row.observation_id,
        confidence=row.confidence,
        knowable_at=row.knowable_at,
        freshness=row.freshness,
    )


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
                {
                    **existing[3],
                    "snapshot_id": existing[0],
                    "content_sha256": existing[1],
                }
            )
        status = self._connection.execute(
            """
            select environment, cutoff, universe_id, universe_version, universe_sha256,
                   obligation_count, terminal_count, success_count, unchanged_count,
                   unavailable_count, skipped_count, failed_count, complete
            from mart.topt_capture_status where run_id = %s
            """,
            (run_id,),
        ).fetchone()
        if status is None:
            raise LookupError(f"capture run not found: {run_id}")
        (
            environment,
            cutoff,
            universe_id,
            universe_version,
            universe_sha256,
            obligations,
            terminal,
            success,
            unchanged,
            unavailable,
            skipped,
            failed,
            complete,
        ) = status
        if environment != "production" or (
            obligations,
            terminal,
            success + unchanged,
            unavailable,
            skipped,
            failed,
            complete,
        ) != (84, 84, 84, 0, 0, 0, True):
            raise ValueError("TOPT core snapshot requires a complete 84-cell Production run")
        rows = self._load_observations(run_id, cutoff=cutoff)
        if len(rows) != _EXPECTED_OBSERVATIONS:
            raise ValueError("complete Production run does not expose 84 normalized payloads")
        grouped: dict[str, dict[str, _ObservationRow]] = {}
        for row in rows:
            by_type = grouped.setdefault(row.listing_id, {})
            if row.semantic_type in by_type:
                raise ValueError("Production run selected more than one observation for a listing semantic cell")
            by_type[row.semantic_type] = row
        if len(grouped) != _EXPECTED_INSTRUMENTS:
            raise ValueError("Production normalized payloads do not cover 21 listings")
        members = tuple(self._snapshot_member(listing_id, by_type) for listing_id, by_type in grouped.items())
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

    def _load_observations(self, run_id: str, *, cutoff: datetime) -> tuple[_ObservationRow, ...]:
        rows = self._connection.execute(
            """
            with selected as (
                select
                    obligation.obligation_id,
                    obligation.subject_id,
                    obligation.capture_requirement_id,
                    observation.observation_id,
                    observation.confidence,
                    case
                        when %s - observation.knowable_at <= policy.freshness_max_age then 'fresh'
                        else 'stale'
                    end as cutoff_freshness_state,
                    observation.knowable_at,
                    payload.normalized_payload,
                    count(*) over (
                        partition by obligation.obligation_id
                    ) as selection_count,
                    row_number() over (
                        partition by obligation.obligation_id
                        order by observation.knowable_at desc,
                                 observation.observation_id desc
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
                 and vintage.source_request_id = work.source_request_id
                where obligation.run_id = %s
                  and observation.source_vintage_id = coalesce(
                      terminal_attempt.source_vintage_id,
                      terminal_attempt.reused_source_vintage_id
                  )
                  and observation.subject_kind = obligation.subject_kind
                  and observation.subject_id = obligation.subject_id
                  and observation.semantic_type = regexp_replace(
                      obligation.capture_requirement_id, ':v1$', ''
                  )
                  and obligation.partition_key ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
                  and (observation.valid_from at time zone 'UTC')::date <= obligation.partition_key::date
                  and (
                      observation.valid_to is null
                      or (observation.valid_to at time zone 'UTC')::date >= obligation.partition_key::date
                  )
                  and observation.knowable_at <= %s
            )
            select obligation_id, subject_id,
                   regexp_replace(capture_requirement_id, ':v1$', ''),
                   observation_id, confidence, cutoff_freshness_state, knowable_at,
                   normalized_payload, selection_count
            from selected where selection_rank = 1
            order by subject_id, capture_requirement_id
            """,
            (cutoff, run_id, cutoff),
        ).fetchall()
        ambiguous = [row[0] for row in rows if row[8] != 1]
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
                confidence=row[4],
                freshness=MetricFreshness(row[5]),
                knowable_at=row[6],
                payload=row[7],
            )
            for row in rows
        )

    @staticmethod
    def _snapshot_member(listing_id: str, by_type: dict[str, _ObservationRow]) -> SnapshotMember:
        if set(by_type) != _REQUIRED_TYPES:
            raise ValueError(f"listing {listing_id} does not have the exact four TOPT semantic cells")
        listing = IdentityPayload.model_validate(by_type["listing-identity"].payload)
        membership = IdentityPayload.model_validate(by_type["universe-membership"].payload)
        financial = FinancialFactPayload.model_validate(by_type["financial-fact"].payload)
        price = MarketPricePayload.model_validate(by_type["market-price"].payload)
        coordinates = {
            (item.issuer_id, item.instrument_id, item.listing_id) for item in (listing, membership, financial, price)
        }
        if coordinates != {(listing.issuer_id, listing.instrument_id, listing_id)}:
            raise ValueError(f"listing {listing_id} normalized payload identities disagree")
        if financial.currency != price.currency:
            raise ValueError(f"listing {listing_id} financial and market currencies disagree")
        financial_row = by_type["financial-fact"]
        market_row = by_type["market-price"]
        observation_ids = sorted(row.observation_id for row in by_type.values())
        if len(observation_ids) != 4:
            raise ValueError(f"listing {listing_id} does not bind exactly four observations")
        return SnapshotMember(
            issuer_id=listing.issuer_id,
            instrument_id=listing.instrument_id,
            listing_id=listing.listing_id,
            operating_branch=financial.operating_branch,
            observation_ids=(
                observation_ids[0],
                observation_ids[1],
                observation_ids[2],
                observation_ids[3],
            ),
            cell_inputs=(
                _cell(by_type["financial-fact"]),
                _cell(by_type["listing-identity"]),
                _cell(by_type["market-price"]),
                _cell(by_type["universe-membership"]),
            ),
            gross_profit=_metric(
                financial_row,
                name="gross_profit",
                value=financial.gross_profit,
                unit=financial.currency,
            ),
            total_assets=_metric(
                financial_row,
                name="total_assets",
                value=financial.total_assets,
                unit=financial.currency,
            ),
            headcount=_metric(financial_row, name="headcount", value=financial.headcount, unit="employees"),
            revenue=_metric(financial_row, name="revenue", value=financial.revenue, unit=financial.currency),
            pre_provision_profit=_metric(
                financial_row,
                name="pre_provision_profit",
                value=financial.pre_provision_profit,
                unit=financial.currency,
            ),
            shares_outstanding=_metric(
                financial_row,
                name="shares_outstanding",
                value=financial.shares_outstanding,
                unit="shares",
            ),
            market_price=_metric(
                market_row,
                name="market_price",
                value=price.close,
                unit=f"{price.currency}_per_share",
            ),
        )

    def _put_snapshot(self, snapshot: ToptCoreSnapshot) -> None:
        payload = snapshot.model_dump(mode="json", exclude={"snapshot_id", "content_sha256"})
        with self._connection.transaction():
            inserted = self._connection.execute(
                """
                insert into staging.topt_core_snapshots (
                    snapshot_id, content_sha256, run_id, release_manifest_id,
                    universe_id, universe_version, universe_sha256, cutoff,
                    issuer_count, instrument_count, observation_count, payload
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, 20, 21, 84, %s)
                on conflict (snapshot_id) do nothing returning snapshot_id
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.content_sha256,
                    snapshot.run_id,
                    snapshot.release_manifest_id,
                    snapshot.universe_id,
                    snapshot.universe_version,
                    snapshot.universe_sha256,
                    snapshot.cutoff,
                    Jsonb(payload),
                ),
            ).fetchone()
            if inserted is None:
                existing = self._connection.execute(
                    "select content_sha256, payload from staging.topt_core_snapshots where snapshot_id = %s",
                    (snapshot.snapshot_id,),
                ).fetchone()
                if existing is None or existing[0] != snapshot.content_sha256 or existing[1] != payload:
                    raise ValueError("TOPT core snapshot identity conflict")
            for factor_input in snapshot.factor_inputs():
                factor_payload = factor_input.model_dump(mode="json")
                member_sha256 = canonical_sha256(factor_payload)
                inserted_member = self._connection.execute(
                    """
                    insert into staging.topt_core_snapshot_members (
                        snapshot_id, instrument_id, issuer_id, listing_id,
                        observation_ids, member_sha256, factor_input
                    ) values (%s, %s, %s, %s, %s, %s, %s)
                    on conflict (snapshot_id, issuer_id) do nothing
                    returning instrument_id
                    """,
                    (
                        snapshot.snapshot_id,
                        factor_input.instrument_id,
                        factor_input.issuer_id,
                        factor_input.listing_id,
                        list(factor_input.observation_ids),
                        member_sha256,
                        Jsonb(factor_payload),
                    ),
                ).fetchone()
                if inserted_member is None:
                    existing_member = self._connection.execute(
                        """
                        select issuer_id, listing_id, observation_ids, member_sha256, factor_input
                        from staging.topt_core_snapshot_members
                        where snapshot_id = %s and issuer_id = %s
                        """,
                        (snapshot.snapshot_id, factor_input.issuer_id),
                    ).fetchone()
                    expected_member = (
                        factor_input.issuer_id,
                        factor_input.listing_id,
                        list(factor_input.observation_ids),
                        member_sha256,
                        factor_payload,
                    )
                    if existing_member != expected_member:
                        raise ValueError("TOPT core snapshot member identity conflict")

    def materialize(
        self,
        snapshot: ToptCoreSnapshot,
        *,
        gppe_definition: GppeV0Definition,
        tier_definition: ThreeTierV0Definition | None = None,
    ) -> tuple[ToptCoreResult, ...]:
        tier_definition = tier_definition or ThreeTierV0Definition()
        factor_inputs = snapshot.factor_inputs()
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
            )
            for factor_input in factor_inputs
        )
        invocation_payload = {
            "snapshot_id": snapshot.snapshot_id,
            "gppe_invocation_id": gppe_invocation_id,
            "tier_definition": tier_definition.model_dump(mode="json"),
        }
        invocation_sha256 = canonical_sha256(invocation_payload)
        invocation_id = f"topt-core-invocation:{invocation_sha256}"
        if len(gppe_results) != _EXPECTED_ISSUERS:
            raise ValueError("TOPT GPPE materialization denominator drifted")
        with self._connection.transaction():
            gppe_inserted = self._connection.execute(
                """
                insert into mart.topt_gppe_invocations (
                    invocation_id, content_sha256, snapshot_id,
                    gppe_definition_id, gppe_definition_sha256, payload
                ) values (%s, %s, %s, %s, %s, %s)
                on conflict (invocation_id) do nothing returning invocation_id
                """,
                (
                    gppe_invocation_id,
                    gppe_invocation_sha256,
                    snapshot.snapshot_id,
                    gppe_definition.definition_id,
                    gppe_definition.content_sha256,
                    Jsonb(gppe_invocation_payload),
                ),
            ).fetchone()
            if gppe_inserted is None:
                existing_gppe = self._connection.execute(
                    "select content_sha256, payload from mart.topt_gppe_invocations where invocation_id = %s",
                    (gppe_invocation_id,),
                ).fetchone()
                if (
                    existing_gppe is None
                    or existing_gppe[0] != gppe_invocation_sha256
                    or existing_gppe[1] != gppe_invocation_payload
                ):
                    raise ValueError("TOPT GPPE invocation identity conflict")
            for gppe_result in gppe_results:
                self._put_gppe_result(gppe_result)
            materialized_gppe_results = self._load_gppe_results(gppe_invocation_id)
            if len(materialized_gppe_results) != _EXPECTED_ISSUERS:
                raise ValueError("TOPT GPPE materialization did not persist the complete denominator")

            results = tuple(
                compute_topt_core(
                    factor_input,
                    gppe_result,
                    invocation_id=invocation_id,
                    tier_definition=tier_definition,
                )
                for factor_input, gppe_result in zip(factor_inputs, materialized_gppe_results, strict=True)
            )
            if len(results) != _EXPECTED_ISSUERS or len({item.issuer_id for item in results}) != _EXPECTED_ISSUERS:
                raise ValueError("TOPT core materialization denominator drifted")
            inserted = self._connection.execute(
                """
                insert into mart.topt_core_invocations (
                    invocation_id, content_sha256, snapshot_id,
                    gppe_invocation_id,
                    gppe_definition_id, gppe_definition_sha256,
                    tier_definition_id, tier_definition_sha256, payload
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (invocation_id) do nothing returning invocation_id
                """,
                (
                    invocation_id,
                    invocation_sha256,
                    snapshot.snapshot_id,
                    gppe_invocation_id,
                    gppe_definition.definition_id,
                    gppe_definition.content_sha256,
                    tier_definition.definition_id,
                    tier_definition.content_sha256,
                    Jsonb(invocation_payload),
                ),
            ).fetchone()
            if inserted is None:
                existing = self._connection.execute(
                    "select content_sha256, payload from mart.topt_core_invocations where invocation_id = %s",
                    (invocation_id,),
                ).fetchone()
                if existing is None or existing[0] != invocation_sha256 or existing[1] != invocation_payload:
                    raise ValueError("TOPT core invocation identity conflict")
            for result in results:
                self._put_result(result)
        return results

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
            ToptGppeResult.model_validate(
                {
                    **row[2],
                    "result_id": row[0],
                    "content_sha256": row[1],
                }
            )
            for row in rows
        )

    def _put_gppe_result(self, result: ToptGppeResult) -> None:
        payload = result.model_dump(mode="json", exclude={"result_id", "content_sha256"})
        inserted = self._connection.execute(
            """
            insert into mart.topt_gppe_results (
                result_id, content_sha256, invocation_id, snapshot_id, run_id,
                release_manifest_id, universe_id, universe_version, universe_sha256,
                cutoff, issuer_id, instrument_id, listing_id, operating_branch,
                operating_metric, availability, operating_efficiency,
                capital_adjusted_gross_profit, gppe, confidence, freshness,
                reason_codes, input_observation_ids,
                gppe_definition_id, gppe_definition_sha256, payload
            ) values (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            ) on conflict (result_id) do nothing returning result_id
            """,
            (
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
            ),
        ).fetchone()
        if inserted is None:
            existing = self._connection.execute(
                "select content_sha256, payload from mart.topt_gppe_results where result_id = %s",
                (result.result_id,),
            ).fetchone()
            if existing is None or existing[0] != result.content_sha256 or existing[1] != payload:
                raise ValueError("TOPT GPPE result identity conflict")

    def _put_result(self, result: ToptCoreResult) -> None:
        payload = result.model_dump(mode="json", exclude={"result_id", "content_sha256"})
        inserted = self._connection.execute(
            """
            insert into mart.topt_core_results (
                result_id, content_sha256, invocation_id, snapshot_id, run_id,
                release_manifest_id, universe_id, universe_version, universe_sha256,
                cutoff, issuer_id, instrument_id, listing_id, operating_branch,
                operating_metric, availability, operating_efficiency,
                capital_adjusted_gross_profit, gppe, tier, target_ps_lower,
                target_ps_upper, target_ps_midpoint, current_ps, valuation_gap,
                confidence, freshness, reason_codes, input_observation_ids,
                gppe_invocation_id, gppe_result_id,
                gppe_definition_id, gppe_definition_sha256,
                tier_definition_id, tier_definition_sha256, payload
            ) values (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s
            ) on conflict (result_id) do nothing returning result_id
            """,
            (
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
            ),
        ).fetchone()
        if inserted is None:
            existing = self._connection.execute(
                "select content_sha256, payload from mart.topt_core_results where result_id = %s",
                (result.result_id,),
            ).fetchone()
            if existing is None or existing[0] != result.content_sha256 or existing[1] != payload:
                raise ValueError("TOPT core result identity conflict")

    @staticmethod
    def _validate_page(limit: int, offset: int) -> None:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("TOPT core query pagination is outside the bounded range")

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
            select result_id, invocation_id, snapshot_id, run_id, release_manifest_id,
                   universe_id, universe_version, universe_sha256, cutoff, issuer_id,
                   instrument_id, listing_id, operating_branch, operating_metric,
                   availability, operating_efficiency, capital_adjusted_gross_profit,
                   gppe, tier, target_ps_lower, target_ps_upper, target_ps_midpoint,
                   current_ps, valuation_gap, confidence, freshness, reason_codes,
                   gppe_invocation_id, gppe_result_id,
                   gppe_definition_id, gppe_definition_sha256,
                   tier_definition_id, tier_definition_sha256, created_at
            from mart.topt_core_result_read
            where run_id = %s and release_manifest_id = %s and universe_id = %s
              and universe_version = %s and universe_sha256 = %s and snapshot_id = %s
              and invocation_id = %s
            order by issuer_id limit %s offset %s
            """,
            (
                identity.run_id,
                identity.release_manifest_id,
                identity.universe_id,
                identity.universe_version,
                identity.universe_sha256,
                identity.snapshot_id,
                identity.invocation_id,
                limit,
                offset,
            ),
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
            (
                identity.run_id,
                identity.release_manifest_id,
                identity.universe_id,
                identity.universe_version,
                identity.universe_sha256,
                identity.snapshot_id,
                identity.invocation_id,
                limit,
                offset,
            ),
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
