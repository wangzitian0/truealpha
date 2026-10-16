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

`resolve_entity` in an empty store mints a listing from its `mic-ticker` alias. Production minted
its entities with `staging.entity_backfill()` from the legacy ids, so its ids are different (#1347).
`backfill_issuer` mints a multi-class issuer the production way, and `seed_tick` writes one capture
tick of its listings with the observation id order that a test asks for.
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
from data_engine.datahub.resolve_coordinates import resolve_coordinates, resolve_entity
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
    #: The payload's instrument id. None writes `security:issue1257:<cik>`: one instrument per issuer.
    instrument_id: str | None = None


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
            "instrument_id": item.instrument_id or f"security:issue1257:{item.cik}",
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
            parser_version=parser_version,
            mapping_version="issue1257-map:v1",
            normalized_payload_sha256=canonical_sha256(payload),
        )
        repository.put_observation(
            obligation.obligation_id, observation, normalized_payload=payload, confidence=Decimal("0.9")
        )


@dataclass(frozen=True)
class ShareClass:
    """One share class of a multi-class issuer, as the universe corpus keys it."""

    ticker: str
    cusip: str


@dataclass(frozen=True)
class BackfilledListing:
    """One listing under both id generations. The canonical ids are the ids the entity backfill minted."""

    ticker: str
    legacy_instrument_id: str
    legacy_listing_id: str
    canonical_instrument_id: str
    canonical_listing_id: str


@dataclass(frozen=True)
class BackfilledIssuer:
    """A multi-class issuer under both id generations, minted the way production minted it (#1347)."""

    cik: int
    legacy_issuer_id: str
    canonical_issuer_id: str
    listings: tuple[BackfilledListing, ...]

    def listing(self, ticker: str) -> BackfilledListing:
        (found,) = [item for item in self.listings if item.ticker == ticker]
        return found


def backfill_issuer(
    connection: psycopg.Connection[Any],
    *,
    lei: str,
    mic: str,
    classes: Sequence[ShareClass],
    cik: int,
    known_at: datetime,
    store: MemoryStore,
) -> BackfilledIssuer:
    """Mint a multi-class issuer in the three steps that production took (#1347).

    1. A capture stores one financial-fact observation per listing under the legacy ids.
    2. `staging.entity_backfill()` mints each entity from its legacy id (`entity-backfill:v1`). It
       gives each listing a parsed `mic-ticker` alias.
    3. A later capture resolves the legacy ids with `resolve_coordinates` and writes the entity UUIDs.

    So the canonical ids are the ids that a store holds after the backfill. No literal names them.
    """
    legacy_issuer = f"issuer:lei:{lei}"
    trios = [
        (item.ticker, f"security:cusip:{item.cusip}", f"listing:{mic.lower()}:{item.ticker.lower()}")
        for item in classes
    ]
    seed_financial_lineage(
        connection,
        [
            Observed(legacy_issuer, listing, cik, "non_financial", known_at, instrument_id=instrument)
            for _ticker, instrument, listing in trios
        ],
        company_facts={cik: {"facts": {}}},
        store=store,
        cutoff=known_at,
    )
    summary = connection.execute("select staging.entity_backfill()").fetchone()[0]
    legacy_ids = [legacy_issuer, *(instrument for _t, instrument, _l in trios), *(listing for _t, _i, listing in trios)]
    born = dict(
        connection.execute(
            "select birth_value, entity_id::text from staging.entities "
            "where birth_scheme = 'legacy-id' and birth_value = any(%s)",
            (legacy_ids,),
        ).fetchall()
    )
    if set(born) != set(legacy_ids):
        raise AssertionError(f"the entity backfill did not mint each entity from its legacy id: {summary}")
    resolved = resolve_coordinates(
        connection,
        [(legacy_issuer, instrument, listing, ticker) for ticker, instrument, listing in trios],
        as_of=known_at.date(),
        known_at=known_at,
    )
    listings = tuple(
        BackfilledListing(ticker, instrument, listing, resolved[listing][1], resolved[listing][2])
        for ticker, instrument, listing in trios
    )
    for ticker, instrument, listing in trios:
        if resolved[listing][:3] != (born[legacy_issuer], born[instrument], born[listing]):
            raise AssertionError(f"the capture resolver does not return the backfilled entities of {ticker}")
    return BackfilledIssuer(cik, legacy_issuer, born[legacy_issuer], listings)


def seed_tick(
    connection: psycopg.Connection[Any],
    issuer: BackfilledIssuer,
    *,
    legacy: bool,
    knowable_at: datetime,
    larger_id_ticker: str,
    store: MemoryStore,
    max_attempts: int = 64,
) -> datetime:
    """One capture tick of every listing of `issuer` under one id generation, in one transaction (#1347).

    The observations share `recorded_at` (the transaction start) and `knowable_at`, as one production
    tick writes them. An observation id is a content hash, so a test cannot set the order of two ids.
    This function moves `knowable_at` forward one second at a time until the listing with the ticker
    `larger_id_ticker` holds the larger observation id. An attempt with the other order rolls back to
    its savepoint. The function returns the `knowable_at` of the tick that it kept.
    """
    issuer_id = issuer.legacy_issuer_id if legacy else issuer.canonical_issuer_id
    wanted = issuer.listing(larger_id_ticker)
    for offset in range(max_attempts):
        at = knowable_at + timedelta(seconds=offset)
        with connection.transaction() as attempt:
            seed_financial_lineage(
                connection,
                [
                    Observed(
                        issuer_id,
                        item.legacy_listing_id if legacy else item.canonical_listing_id,
                        issuer.cik,
                        "non_financial",
                        at,
                        instrument_id=item.legacy_instrument_id if legacy else item.canonical_instrument_id,
                    )
                    for item in issuer.listings
                ],
                company_facts={issuer.cik: {"facts": {}}},
                store=store,
                cutoff=at,
            )
            ordered = connection.execute(
                """
                select p.normalized_payload ->> 'listing_id'
                from staging.capture_normalized_observations o
                join staging.capture_observation_payloads p on p.observation_id = o.observation_id
                where o.knowable_at = %s and p.normalized_payload ->> 'issuer_id' = %s
                order by o.observation_id desc
                """,
                (at, issuer_id),
            ).fetchall()
            if len(ordered) != len(issuer.listings):
                raise AssertionError(f"the tick wrote {len(ordered)} observations for {len(issuer.listings)} listings")
            if ordered[0][0] == (wanted.legacy_listing_id if legacy else wanted.canonical_listing_id):
                return at
            raise psycopg.Rollback(attempt)
    raise AssertionError(f"no tick in {max_attempts} seconds gives {larger_id_ticker} the larger observation id")


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
