"""Test support: the stored lineage that the history projector and the backtest resolve issuers from.

#1257: `staging.strategy_backtest_inputs` holds two issuer id generations. The legacy one is
`issuer:lei:<LEI>` with a `listing:<mic>:<ticker>` listing. The canonical one is the entity UUID
that `resolve_entity` mints from that LEI, with a UUID listing whose `mic-ticker` alias spells the
ticker. A financial-fact observation exists for each generation and points to the company-facts
vintage of the issuer's CIK. This module seeds exactly that chain through the production
repositories, so a test resolves issuers through the real path and needs no stub:

    staging.entities / staging.entity_aliases        (resolve_entity: LEI, legacy id, mic-ticker)
    raw.capture_campaigns ... raw.capture_obligations (one run, one obligation per listing)
    raw.fetches + raw.capture_source_vintages         (companyfacts:CIK<cik>, bytes in a store)
    staging.capture_normalized_observations + payloads (parser PARSER_VERSION)

#1265: an issuer can have two listings, and the listing-identity cells name the ticker of each.
`seed_listing_identity` seeds those cells through the same repositories.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
from data_engine import raw_store
from data_engine.datahub.control_plane import expand_obligations, replay_retry_policy
from data_engine.datahub.production_topt.parser_identity import PARSER_VERSION
from data_engine.datahub.repository import PostgresCaptureControlRepository
from data_engine.datahub.resolve_coordinates import resolve_entity
from truealpha_contracts.capture_control import CaptureListVersion
from truealpha_contracts.common import CaptureEnvironment, canonical_sha256
from truealpha_contracts.datahub import (
    CaptureCampaign,
    CaptureRun,
    CaptureSchedulePolicy,
    NormalizedObservation,
    SourceRequest,
    SourceVintage,
)
from truealpha_contracts.models import DataSource, RawCapture, RawIngestionEnvelope, RawObjectRef
from truealpha_contracts.universe import SubjectKind, SubjectRef, UniverseRef


class MemoryStore:
    """An object store in memory: `raw_store` writes and verifies company-facts bytes here."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def store(self, capture: RawCapture) -> RawIngestionEnvelope:
        digest = hashlib.sha256(capture.body).hexdigest()
        ref = RawObjectRef(
            bucket="memory-bucket",
            key=f"{capture.source.value}/{digest}",
            sha256=digest,
            byte_length=len(capture.body),
            content_type=capture.content_type,
        )
        self.objects[ref.key] = capture.body
        return RawIngestionEnvelope(
            source=capture.source,
            source_record_id=capture.source_record_id,
            object=ref,
            fetched_at=capture.fetched_at,
            source_published_at=capture.source_published_at,
            metadata=capture.metadata,
        )

    def get(self, ref: RawObjectRef) -> bytes:
        return self.objects[ref.key]


@dataclass(frozen=True)
class SeededIssuer:
    """One issuer under both id generations."""

    ticker: str
    cik: int
    legacy_issuer_id: str
    legacy_listing_id: str
    canonical_issuer_id: str
    canonical_listing_id: str


def mint_issuer(
    connection: psycopg.Connection[Any], *, lei: str, mic: str, ticker: str, cik: int, known_at: datetime
) -> SeededIssuer:
    """Mint the canonical issuer and listing entities the way the identity unification did."""
    legacy_issuer = f"issuer:lei:{lei}"
    legacy_listing = f"listing:{mic.lower()}:{ticker.lower()}"
    issuer = resolve_entity(connection, legacy_issuer, "issuer", as_of=known_at.date(), known_at=known_at)
    listing = resolve_entity(connection, legacy_listing, "listing", as_of=known_at.date(), known_at=known_at)
    return SeededIssuer(ticker, cik, legacy_issuer, legacy_listing, str(issuer), str(listing))


@dataclass(frozen=True)
class Observed:
    """One financial-fact observation to seed: the payload identity it carries and when it landed."""

    issuer_id: str
    listing_id: str
    cik: int
    operating_branch: str
    knowable_at: datetime


@dataclass(frozen=True)
class Listed:
    """One listing-identity cell to seed (#1265): the issuer id it names, one listing and its ticker."""

    issuer_id: str
    listing_id: str
    ticker: str
    knowable_at: datetime


@dataclass(frozen=True)
class _Cell:
    """One observation of a seeded run: its raw bytes and its normalized payload."""

    listing_id: str
    source: DataSource
    record_id: str
    body: bytes
    payload: dict[str, Any]
    knowable_at: datetime


def _seed_run(
    connection: psycopg.Connection[Any],
    cells: Sequence[_Cell],
    *,
    tag: str,
    semantic_type: str,
    store: MemoryStore,
    cutoff: datetime,
    parser_version: str,
) -> None:
    """Seed one capture run with one observation of `semantic_type` per cell, through the repositories."""
    policy = CaptureSchedulePolicy(
        policy_version=f"{tag}-lineage:v1",
        demanded_cadence=timedelta(days=1),
        provider_availability_cadence="manual-only:v1",
        freshness_max_age=timedelta(days=2),
        retry=replay_retry_policy(3),
    )
    universe = UniverseRef(
        universe_id=f"universe:{tag}-lineage",
        universe_version=f"{tag}-lineage-v1",
        content_sha256=canonical_sha256(sorted(cell.listing_id for cell in cells)),
    )
    campaign = CaptureCampaign(
        campaign_policy_id=f"capture-policy:{tag}-lineage-v1",
        environment=CaptureEnvironment.STAGING,
        cutoff=cutoff,
        universe_refs=(universe,),
    )
    run = CaptureRun(
        campaign_id=campaign.campaign_id,
        run_sequence=1,
        schedule_policy_id=policy.schedule_policy_id,
        capture_scope_id=f"capture-scope:{canonical_sha256({'scope': f'{tag}-lineage:v1'})}",
    )
    list_version = CaptureListVersion(
        universe=universe,
        members=tuple(SubjectRef(kind=SubjectKind.LISTING, id=cell.listing_id) for cell in cells),
        effective_at=cutoff,
    )
    obligations = {
        obligation.subject.id: obligation
        for obligation in expand_obligations(
            run_id=run.run_id, list_version=list_version, semantic_types=(semantic_type,), partition=tag
        )
    }
    repository = PostgresCaptureControlRepository(connection)
    repository.put_schedule_policy(policy)
    repository.put_campaign(campaign)
    repository.put_list_version(list_version)
    repository.bind_campaign_list(campaign.campaign_id, list_version.list_version_id)
    repository.put_run(run)

    for ordinal, cell in enumerate(cells):
        obligation = obligations[cell.listing_id]
        repository.put_obligation(campaign.campaign_id, obligation)
        request = SourceRequest(
            source_registry_entry_id=f"source-registry-entry:{canonical_sha256({'source': f'{tag}:v1'})}",
            source_policy_id=f"source-policy:{tag}-v1",
            request_fingerprint_version=f"{tag}:v1",
            canonical_request_sha256=canonical_sha256({"ordinal": ordinal, "listing": cell.listing_id}),
            subject_refs=(obligation.subject,),
            capture_requirement_ids=(obligation.capture_requirement_id,),
            partition=obligation.partition,
        )
        repository.put_source_request(request)
        landed = cell.knowable_at - timedelta(minutes=5)
        fetch_id = raw_store.insert_fetch(
            connection,
            source=cell.source,
            source_record_id=cell.record_id,
            body=cell.body,
            content_type="application/json",
            fetched_at=landed,
            store=store,
            recorded_at=landed,
        )
        vintage = SourceVintage(
            source_request_id=request.source_request_id,
            source_record_id=cell.record_id,
            source_published_at=None,
            raw_object_id=f"raw-object:{hashlib.sha256(cell.body).hexdigest()}",
        )
        repository.put_source_vintage(vintage, raw_fetch_id=fetch_id)
        observation = NormalizedObservation(
            semantic_type=semantic_type,
            semantic_version=obligation.capture_requirement_id,
            subject=obligation.subject,
            valid_from=cell.knowable_at - timedelta(days=1),
            valid_to=None,
            knowable_at=cell.knowable_at,
            source_vintage_id=vintage.source_vintage_id,
            parser_version=parser_version,
            mapping_version=f"{tag}-map:v1",
            normalized_payload_sha256=canonical_sha256(cell.payload),
        )
        repository.put_observation(
            obligation.obligation_id, observation, normalized_payload=cell.payload, confidence=Decimal("0.9")
        )


def seed_financial_lineage(
    connection: psycopg.Connection[Any],
    observations: Sequence[Observed],
    *,
    company_facts: Mapping[int, dict[str, Any]],
    store: MemoryStore,
    cutoff: datetime,
    parser_version: str = PARSER_VERSION,
) -> None:
    """Seed one capture run with one financial-fact observation per entry, through the repositories.

    Each observation's vintage is the company-facts document of its CIK, so
    `load_stored_company_facts` can read the bytes back from `store`.
    """
    cells = [
        _Cell(
            listing_id=item.listing_id,
            source=DataSource.SEC,
            record_id=f"companyfacts:CIK{item.cik:010d}",
            body=json.dumps(company_facts[item.cik]).encode(),
            payload={
                "issuer_id": item.issuer_id,
                "instrument_id": f"security:issue1257:{item.cik}",
                "listing_id": item.listing_id,
                "operating_branch": item.operating_branch,
                "currency": "USD",
            },
            knowable_at=item.knowable_at,
        )
        for item in observations
    ]
    _seed_run(
        connection,
        cells,
        tag="issue1257",
        semantic_type="financial-fact",
        store=store,
        cutoff=cutoff,
        parser_version=parser_version,
    )


def seed_listing_identity(
    connection: psycopg.Connection[Any],
    listings: Sequence[Listed],
    *,
    store: MemoryStore,
    cutoff: datetime,
    parser_version: str = PARSER_VERSION,
) -> None:
    """Seed one capture run with one listing-identity cell per entry, through the repositories (#1265).

    A deployed capture writes these cells from the release (`release_derived_adapter`). The payload
    holds the four `IdentityPayload` fields, and the raw bytes hold the payload.
    """
    cells = []
    for item in listings:
        payload = {
            "issuer_id": item.issuer_id,
            "instrument_id": f"security:issue1265:{item.ticker.lower()}",
            "listing_id": item.listing_id,
            "ticker": item.ticker,
        }
        body = json.dumps(
            {"semantic_type": "listing-identity", "subject_id": item.listing_id, "payload": payload}, sort_keys=True
        ).encode()
        cells.append(
            _Cell(
                listing_id=item.listing_id,
                source=DataSource.RELEASE,
                record_id=f"listing-identity:{item.listing_id}",
                body=body,
                payload=payload,
                knowable_at=item.knowable_at,
            )
        )
    _seed_run(
        connection,
        cells,
        tag="issue1265",
        semantic_type="listing-identity",
        store=store,
        cutoff=cutoff,
        parser_version=parser_version,
    )


def annual_company_facts(*, base: int, years: range = range(2018, 2026)) -> dict[str, Any]:
    """A company-facts document: fiscal year Y ends on Y-12-31, and its 10-K is filed on (Y+1)-02-20."""
    series: dict[str, list[dict[str, Any]]] = {
        name: [] for name in ("Revenues", "GrossProfit", "NetIncomeLoss", "Assets", "CommonStockSharesOutstanding")
    }
    for year in years:
        end, start, filed = f"{year}-12-31", f"{year}-01-01", f"{year + 1}-02-20"
        common = {"end": end, "filed": filed, "form": "10-K", "fy": year, "fp": "FY", "accn": f"0001-{filed}"}
        series["Revenues"].append({**common, "start": start, "val": base + year})
        series["GrossProfit"].append({**common, "start": start, "val": (base + year) // 2})
        series["NetIncomeLoss"].append({**common, "start": start, "val": (base + year) // 10})
        series["Assets"].append({**common, "val": base * 10 + year})
        series["CommonStockSharesOutstanding"].append({**common, "val": 5_000_000 + year})
    units = {"Assets": "USD", "CommonStockSharesOutstanding": "shares"}
    return {
        "facts": {"us-gaap": {name: {"units": {units.get(name, "USD"): entries}} for name, entries in series.items()}}
    }


def at(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)
