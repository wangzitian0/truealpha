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
    ThreeTierV0Definition,
    ToptCoreResult,
    ToptCoreSnapshotInput,
    ToptMetricInput,
    compute_topt_core,
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
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    gross_profit: Decimal | None
    total_assets: Decimal | None
    headcount: Decimal | None
    revenue: Decimal | None
    shares_outstanding: Decimal | None

    @field_validator(
        "gross_profit",
        "total_assets",
        "headcount",
        "revenue",
        "shares_outstanding",
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
    observation_ids: tuple[str, str, str, str]
    gross_profit: ToptMetricInput | None
    total_assets: ToptMetricInput | None
    headcount: ToptMetricInput | None
    revenue: ToptMetricInput | None
    shares_outstanding: ToptMetricInput | None
    market_price: ToptMetricInput | None

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
        return tuple(
            ToptCoreSnapshotInput(
                snapshot_id=self.snapshot_id,
                run_id=self.run_id,
                release_manifest_id=self.release_manifest_id,
                universe_id=self.universe_id,
                universe_version=self.universe_version,
                universe_sha256=self.universe_sha256,
                cutoff=self.cutoff,
                issuer_id=member.issuer_id,
                instrument_id=member.instrument_id,
                listing_id=member.listing_id,
                observation_ids=member.observation_ids,
                gross_profit=member.gross_profit,
                total_assets=member.total_assets,
                headcount=member.headcount,
                revenue=member.revenue,
                shares_outstanding=member.shares_outstanding,
                market_price=member.market_price,
            )
            for member in self.members
        )


@dataclass(frozen=True)
class ToptCoreIdentity:
    run_id: str
    release_manifest_id: str
    universe_id: str
    universe_version: str
    universe_sha256: str
    snapshot_id: str


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
    availability: str
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


class PostgresToptCoreRepository:
    def __init__(self, connection: Connection[Any]) -> None:
        self._connection = connection

    def freeze_snapshot(self, *, run_id: str, release_manifest_id: str) -> ToptCoreSnapshot:
        status = self._connection.execute(
            """
            select environment, cutoff, universe_id, universe_version, universe_sha256,
                   obligation_count, terminal_count, complete
            from mart.topt_capture_status where run_id = %s
            """,
            (run_id,),
        ).fetchone()
        if status is None:
            raise LookupError(f"capture run not found: {run_id}")
        environment, cutoff, universe_id, universe_version, universe_sha256, obligations, terminal, complete = status
        if environment != "production" or (obligations, terminal, complete) != (84, 84, True):
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
                    observation.freshness_state,
                    observation.knowable_at,
                    payload.normalized_payload,
                    row_number() over (
                        partition by obligation.obligation_id
                        order by observation.knowable_at desc,
                                 observation.recorded_at desc,
                                 observation.observation_id desc
                    ) as selection_rank
                from raw.capture_obligations obligation
                join raw.capture_obligation_results terminal
                  on terminal.capture_obligation_id = obligation.obligation_id
                 and terminal.terminal_state in ('success', 'unchanged')
                join staging.capture_observation_obligations usage
                  on usage.capture_obligation_id = obligation.obligation_id
                join staging.capture_normalized_observations observation using (observation_id)
                join staging.capture_observation_payloads payload using (observation_id)
                where obligation.run_id = %s and observation.knowable_at <= %s
            )
            select obligation_id, subject_id,
                   replace(capture_requirement_id, ':v1', ''),
                   observation_id, confidence, freshness_state, knowable_at,
                   normalized_payload
            from selected where selection_rank = 1
            order by subject_id, capture_requirement_id
            """,
            (run_id, cutoff),
        ).fetchall()
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
            observation_ids=(
                observation_ids[0],
                observation_ids[1],
                observation_ids[2],
                observation_ids[3],
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
                    on conflict (snapshot_id, instrument_id) do nothing
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
                        where snapshot_id = %s and instrument_id = %s
                        """,
                        (snapshot.snapshot_id, factor_input.instrument_id),
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
        invocation_payload = {
            "snapshot_id": snapshot.snapshot_id,
            "gppe_definition": gppe_definition.model_dump(mode="json"),
            "tier_definition": tier_definition.model_dump(mode="json"),
        }
        invocation_sha256 = canonical_sha256(invocation_payload)
        invocation_id = f"topt-core-invocation:{invocation_sha256}"
        results = tuple(
            compute_topt_core(
                factor_input,
                invocation_id=invocation_id,
                gppe_definition=gppe_definition,
                tier_definition=tier_definition,
            )
            for factor_input in snapshot.factor_inputs()
        )
        if len(results) != _EXPECTED_INSTRUMENTS or len({item.issuer_id for item in results}) != _EXPECTED_ISSUERS:
            raise ValueError("TOPT core materialization denominator drifted")
        with self._connection.transaction():
            inserted = self._connection.execute(
                """
                insert into mart.topt_core_invocations (
                    invocation_id, content_sha256, snapshot_id,
                    gppe_definition_id, gppe_definition_sha256,
                    tier_definition_id, tier_definition_sha256, payload
                ) values (%s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (invocation_id) do nothing returning invocation_id
                """,
                (
                    invocation_id,
                    invocation_sha256,
                    snapshot.snapshot_id,
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

    def _put_result(self, result: ToptCoreResult) -> None:
        payload = result.model_dump(mode="json", exclude={"result_id", "content_sha256"})
        inserted = self._connection.execute(
            """
            insert into mart.topt_core_results (
                result_id, content_sha256, invocation_id, snapshot_id, run_id,
                release_manifest_id, universe_id, universe_version, universe_sha256,
                cutoff, issuer_id, instrument_id, listing_id, availability,
                capital_adjusted_gross_profit, gppe, tier, target_ps_lower,
                target_ps_upper, target_ps_midpoint, current_ps, valuation_gap,
                confidence, freshness, reason_codes, input_observation_ids,
                gppe_definition_id, gppe_definition_sha256,
                tier_definition_id, tier_definition_sha256, payload
            ) values (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s
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
                result.availability.value,
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
                   instrument_id, listing_id, availability, capital_adjusted_gross_profit,
                   gppe, tier, target_ps_lower, target_ps_upper, target_ps_midpoint,
                   current_ps, valuation_gap, confidence, freshness, reason_codes,
                   gppe_definition_id, gppe_definition_sha256,
                   tier_definition_id, tier_definition_sha256, created_at
            from mart.topt_core_result_read
            where run_id = %s and release_manifest_id = %s and universe_id = %s
              and universe_version = %s and universe_sha256 = %s and snapshot_id = %s
            order by instrument_id limit %s offset %s
            """,
            (
                identity.run_id,
                identity.release_manifest_id,
                identity.universe_id,
                identity.universe_version,
                identity.universe_sha256,
                identity.snapshot_id,
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
                availability=row[12],
                capital_adjusted_gross_profit=row[13],
                gppe=row[14],
                tier=row[15],
                target_ps_lower=row[16],
                target_ps_upper=row[17],
                target_ps_midpoint=row[18],
                current_ps=row[19],
                valuation_gap=row[20],
                confidence=row[21],
                freshness=row[22],
                reason_codes=tuple(row[23]),
                gppe_definition_id=row[24],
                gppe_definition_sha256=row[25],
                tier_definition_id=row[26],
                tier_definition_sha256=row[27],
                created_at=row[28],
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
                   gppe_definition_id, gppe_definition_sha256,
                   tier_definition_id, tier_definition_sha256,
                   confidence, freshness, created_at, lineage
            from mart.topt_core_meta_info
            where run_id = %s and release_manifest_id = %s and universe_id = %s
              and universe_version = %s and universe_sha256 = %s and snapshot_id = %s
            order by instrument_id limit %s offset %s
            """,
            (
                identity.run_id,
                identity.release_manifest_id,
                identity.universe_id,
                identity.universe_version,
                identity.universe_sha256,
                identity.snapshot_id,
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
                gppe_definition_id=row[13],
                gppe_definition_sha256=row[14],
                tier_definition_id=row[15],
                tier_definition_sha256=row[16],
                confidence=row[17],
                freshness=row[18],
                created_at=row[19],
                lineage=tuple(row[20]),
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
