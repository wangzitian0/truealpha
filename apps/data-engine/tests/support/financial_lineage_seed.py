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


def seed_financial_lineage(
    connection: psycopg.Connection[Any],
    observations: Sequence[Observed],
    *,
    company_facts: Mapping[int, dict[str, Any]],
    store: MemoryStore,
    cutoff: datetime,
) -> None:
    """Seed one capture run with one financial-fact observation per entry, through the repositories.

    Each observation's vintage is the company-facts document of its CIK, so
    `load_stored_company_facts` can read the bytes back from `store`.
    """
    policy = CaptureSchedulePolicy(
        policy_version="issue1257-lineage:v1",
        demanded_cadence=timedelta(days=1),
        provider_availability_cadence="manual-only:v1",
        freshness_max_age=timedelta(days=2),
        retry=replay_retry_policy(3),
    )
    universe = UniverseRef(
        universe_id="universe:issue1257-lineage",
        universe_version="issue1257-lineage-v1",
        content_sha256=canonical_sha256(sorted(item.listing_id for item in observations)),
    )
    campaign = CaptureCampaign(
        campaign_policy_id="capture-policy:issue1257-lineage-v1",
        environment=CaptureEnvironment.STAGING,
        cutoff=cutoff,
        universe_refs=(universe,),
    )
    run = CaptureRun(
        campaign_id=campaign.campaign_id,
        run_sequence=1,
        schedule_policy_id=policy.schedule_policy_id,
        capture_scope_id=f"capture-scope:{canonical_sha256({'scope': 'issue1257-lineage:v1'})}",
    )
    list_version = CaptureListVersion(
        universe=universe,
        members=tuple(SubjectRef(kind=SubjectKind.LISTING, id=item.listing_id) for item in observations),
        effective_at=cutoff,
    )
    obligations = {
        obligation.subject.id: obligation
        for obligation in expand_obligations(
            run_id=run.run_id, list_version=list_version, semantic_types=("financial-fact",), partition="issue1257"
        )
    }
    repository = PostgresCaptureControlRepository(connection)
    repository.put_schedule_policy(policy)
    repository.put_campaign(campaign)
    repository.put_list_version(list_version)
    repository.bind_campaign_list(campaign.campaign_id, list_version.list_version_id)
    repository.put_run(run)

    for ordinal, item in enumerate(observations):
        obligation = obligations[item.listing_id]
        repository.put_obligation(campaign.campaign_id, obligation)
        request = SourceRequest(
            source_registry_entry_id=f"source-registry-entry:{canonical_sha256({'source': 'issue1257:v1'})}",
            source_policy_id="source-policy:issue1257-v1",
            request_fingerprint_version="issue1257:v1",
            canonical_request_sha256=canonical_sha256({"ordinal": ordinal, "listing": item.listing_id}),
            subject_refs=(obligation.subject,),
            capture_requirement_ids=(obligation.capture_requirement_id,),
            partition=obligation.partition,
        )
        repository.put_source_request(request)
        record_id = f"companyfacts:CIK{item.cik:010d}"
        body = json.dumps(company_facts[item.cik]).encode()
        landed = item.knowable_at - timedelta(minutes=5)
        fetch_id = raw_store.insert_fetch(
            connection,
            source=DataSource.SEC,
            source_record_id=record_id,
            body=body,
            content_type="application/json",
            fetched_at=landed,
            store=store,
            recorded_at=landed,
        )
        vintage = SourceVintage(
            source_request_id=request.source_request_id,
            source_record_id=record_id,
            source_published_at=None,
            raw_object_id=f"raw-object:{hashlib.sha256(body).hexdigest()}",
        )
        repository.put_source_vintage(vintage, raw_fetch_id=fetch_id)
        payload = {
            "issuer_id": item.issuer_id,
            "instrument_id": f"security:issue1257:{item.cik}",
            "listing_id": item.listing_id,
            "operating_branch": item.operating_branch,
            "currency": "USD",
        }
        observation = NormalizedObservation(
            semantic_type="financial-fact",
            semantic_version=obligation.capture_requirement_id,
            subject=obligation.subject,
            valid_from=item.knowable_at - timedelta(days=1),
            valid_to=None,
            knowable_at=item.knowable_at,
            source_vintage_id=vintage.source_vintage_id,
            parser_version=PARSER_VERSION,
            mapping_version="issue1257-map:v1",
            normalized_payload_sha256=canonical_sha256(payload),
        )
        repository.put_observation(
            obligation.obligation_id, observation, normalized_payload=payload, confidence=Decimal("0.9")
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
