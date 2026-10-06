from __future__ import annotations

import dataclasses
import json
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub import quality_report
from data_engine.datahub.a1_evidence import (
    ACCEPTED_SERVICE_OBJECTIVES,
    ServiceObjectives,
    register_run_evidence,
)
from data_engine.datahub.control_plane import AttemptLedger, expand_obligations, replay_retry_policy
from data_engine.datahub.evidence_graph_repository import PostgresEvidenceGraphRepository
from data_engine.datahub.medium_replay import frozen_topt_list_version
from data_engine.datahub.production_topt import PostgresToptCoreRepository, ToptCoreIdentity
from data_engine.datahub.production_topt.materialization import _ObservationRow
from data_engine.datahub.production_topt.universe_corpus import corpus_list_version
from data_engine.datahub.repository import PostgresCaptureControlRepository
from data_engine.datahub.strategy_bridge import run_strategy_replay_for_cutoff, seed_strategy_inputs_from_capture
from factors.production_topt import GppeV0Definition, MetricFreshness, ToptCoreAvailability
from psycopg.types.json import Jsonb
from truealpha_contracts.access import AccessContext, AuthenticationMethod, PrincipalKind
from truealpha_contracts.capture_control import CaptureObligationWorkBinding
from truealpha_contracts.common import CaptureEnvironment, canonical_sha256
from truealpha_contracts.datahub import (
    CaptureCampaign,
    CaptureRun,
    CaptureSchedulePolicy,
    CaptureWorkItem,
    FetchAttemptOutcome,
    ListObligationResult,
    NormalizedObservation,
    ObligationTerminalState,
    SourceRequest,
    SourceVintage,
)
from truealpha_contracts.evidence_graph import BitemporalStamp, EvidenceNode, EvidenceNodeKind, EvidenceNodeRef
from truealpha_contracts.strategy_run import StrategyRunReport
from truealpha_contracts.strategy_run_postgres import PostgresStrategyRunRepository
from truealpha_contracts.topt_read import PostgresToptGppeRepository, ToptGppeReport, ToptGppeUnavailable

CORPUS = Path(__file__).parents[1] / "fixtures" / "capture_control" / "corpus.v1.json"
CUTOFF = datetime(2026, 4, 2, tzinfo=UTC)
SEMANTIC_TYPES = ("market-price", "listing-identity", "universe-membership", "financial-fact")
# The corpus is a single-origin fixture: its market-price parser vintage is not in the
# fusion source map, so a report over a corpus run grades zero reconciled cells. Every
# other accepted objective is kept exactly as declared, so a corpus run that regressed on
# coverage, availability or confidence would still be refused the pointer here (#536).
_CORPUS_OBJECTIVES = ServiceObjectives(
    minimum_coverage=ACCEPTED_SERVICE_OBJECTIVES.minimum_coverage,
    minimum_availability=ACCEPTED_SERVICE_OBJECTIVES.minimum_availability,
    minimum_confidence_score=ACCEPTED_SERVICE_OBJECTIVES.minimum_confidence_score,
    minimum_independent_origin_groups=0,
    minimum_corroborated_share=Decimal("0"),
)


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        active.execute("select 1")
        yield active
    finally:
        active.rollback()
        active.close()


def _normalized_payload(
    coordinates: tuple[str, str, str, str],
    semantic_type: str,
    *,
    financial_vintage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    issuer_id, instrument_id, listing_id, ticker = coordinates
    identity = {
        "issuer_id": issuer_id,
        "instrument_id": instrument_id,
        "listing_id": listing_id,
    }
    if semantic_type in {"listing-identity", "universe-membership"}:
        return {**identity, "ticker": ticker}
    if semantic_type == "market-price":
        return {**identity, "currency": "USD", "close": "40"}
    if semantic_type == "financial-fact":
        financial = ticker == "JPM"
        # #394 convergence: total_assets + revenue are captured for every issuer
        # (the SEC financial-fact adapter provides them), so a financial issuer now
        # takes the uniform capital-adjusted path -- gross_profit stays None for a
        # bank (it reports pre-provision profit as its industry-branch numerator).
        payload: dict[str, Any] = {
            **identity,
            "operating_branch": "financial" if financial else "non_financial",
            "currency": "USD",
            "gross_profit": None if financial else "210000000",
            "total_assets": "200000000",
            "headcount": "100",
            "revenue": "100000000",
            "shares_outstanding": "10000000",
            "pre_provision_profit": "80000000" if financial else None,
        }
        if financial_vintage is not None:
            payload["vintage"] = financial_vintage
        return payload
    raise AssertionError(f"unexpected semantic type: {semantic_type}")


def _source_request(obligation, *, ordinal: int) -> SourceRequest:
    coordinate = {
        "ordinal": ordinal,
        "subject": obligation.subject.model_dump(mode="json"),
        "requirement": obligation.capture_requirement_id,
        "partition": obligation.partition,
    }
    return SourceRequest(
        source_registry_entry_id=(
            f"source-registry-entry:{canonical_sha256({'source': 'production-topt-integration:v1'})}"
        ),
        source_policy_id="source-policy:production-topt-integration-v1",
        request_fingerprint_version="production-topt-integration:v1",
        canonical_request_sha256=canonical_sha256(coordinate),
        subject_refs=(obligation.subject,),
        capture_requirement_ids=(obligation.capture_requirement_id,),
        partition=obligation.partition,
    )


def _seed_complete_production_run(
    connection,
    *,
    stale_unchanged_first_observation: bool = False,
    corpus: dict | None = None,
    valid_from_by_semantic: dict[str, datetime] | None = None,
    valid_to_by_semantic: dict[str, datetime] | None = None,
    cutoff: datetime = CUTOFF,
    financial_vintage: dict[str, Any] | None = None,
    environment: CaptureEnvironment = CaptureEnvironment.PRODUCTION,
):
    """Seed one complete run in the given capture environment.

    A semantic type that `valid_from_by_semantic` omits gets valid_from = cutoff - 2 days.
    A semantic type that `valid_to_by_semantic` omits gets an open valid_to.
    The capture sink writes an open valid_to only.
    A `financial_vintage` is written into every financial-fact payload as its `vintage`.
    """
    valid_from_by_semantic = valid_from_by_semantic or {}
    valid_to_by_semantic = valid_to_by_semantic or {}
    corpus = corpus if corpus is not None else json.loads(CORPUS.read_text())
    denominator = corpus["topt_denominator"]
    coordinates = {row[2]: tuple(row) for row in denominator["instruments"]}
    # The same dispatch composition.plan_and_persist ships: a self-pinned corpus
    # (universe-as-data, #539) resolves through corpus_list_version; only the
    # checked-in TOPT fixture takes the frozen legacy path.
    if "instrument_mapping_sha256" in denominator:
        list_version = corpus_list_version(corpus)
    else:
        list_version = frozen_topt_list_version(corpus)
    policy = CaptureSchedulePolicy(
        policy_version="production-topt-integration:v1",
        demanded_cadence=timedelta(days=1),
        provider_availability_cadence="manual-only:v1",
        freshness_max_age=timedelta(days=2),
        retry=replay_retry_policy(3),
    )
    campaign = CaptureCampaign(
        campaign_policy_id="capture-policy:production-topt-integration-v1",
        environment=environment,
        cutoff=cutoff,
        universe_refs=(list_version.universe,),
    )
    run = CaptureRun(
        campaign_id=campaign.campaign_id,
        run_sequence=1,
        schedule_policy_id=policy.schedule_policy_id,
        capture_scope_id=f"capture-scope:{canonical_sha256({'scope': 'production-topt-integration:v1'})}",
    )
    obligations = expand_obligations(
        run_id=run.run_id,
        list_version=list_version,
        semantic_types=SEMANTIC_TYPES,
        partition=str(denominator["report_date"]),
    )
    repository = PostgresCaptureControlRepository(connection)
    repository.put_schedule_policy(policy)
    repository.put_campaign(campaign)
    repository.put_list_version(list_version)
    repository.bind_campaign_list(campaign.campaign_id, list_version.list_version_id)
    repository.put_run(run)
    # The capture executor appends the run's evidence node as part of the capture it
    # performed (#171 A1/A4); this seeder stands in for that executor, so it owes the
    # same node. `register_run_evidence` only binds the release manifest to it.
    PostgresEvidenceGraphRepository(connection).append(
        [
            EvidenceNode(
                ref=EvidenceNodeRef(kind=EvidenceNodeKind.CAPTURE_RUN, node_id=run.run_id),
                content_sha256=run.run_id.split(":", 1)[1],
                stamp=BitemporalStamp(valid_from=cutoff.date(), transaction_time=cutoff, recorded_at=cutoff),
            )
        ],
        [],
    )
    terminal_observation_id = None
    unselected_same_request_observation_ids = None
    foreign_observation_id = None

    release_payload = {"kind": "production-topt-integration-release"}
    release_sha256 = canonical_sha256(release_payload)
    release_manifest_id = f"release-manifest:{release_sha256}"
    run_plan_payload = {
        "run_id": run.run_id,
        "release_manifest_id": release_manifest_id,
    }
    connection.execute(
        """
        insert into raw.production_topt_run_plans (
            run_id, release_manifest_id, content_sha256, payload
        ) values (%s, %s, %s, %s)
        """,
        (
            run.run_id,
            release_manifest_id,
            canonical_sha256(run_plan_payload),
            psycopg.types.json.Jsonb(run_plan_payload),
        ),
    )

    for ordinal, obligation in enumerate(obligations):
        request = _source_request(obligation, ordinal=ordinal)
        work_item = CaptureWorkItem(
            campaign_id=campaign.campaign_id,
            source_request_id=request.source_request_id,
            schedule_policy_id=policy.schedule_policy_id,
        )
        binding = CaptureObligationWorkBinding(
            obligation_id=obligation.obligation_id,
            work_item_id=work_item.work_item_id,
        )
        repository.put_obligation(campaign.campaign_id, obligation)
        repository.put_source_request(request)
        repository.put_work_item(work_item, policy.retry)
        repository.put_binding(binding)

        semantic_type = obligation.capture_requirement_id.removesuffix(":v1")
        normalized_payload = _normalized_payload(
            coordinates[obligation.subject.id], semantic_type, financial_vintage=financial_vintage
        )
        raw_sha256 = canonical_sha256({"ordinal": ordinal, "payload": normalized_payload})
        source_record_id = f"production-topt-integration:{ordinal}"
        raw_fetch_id = connection.execute(
            """
            insert into raw.fetches (
                source, source_record_id, payload_sha256, object_uri, content_type,
                byte_length, fetched_at, recorded_at, metadata
            ) values (%s, %s, %s, %s, 'application/json', 1, %s, %s, '{}'::jsonb)
            returning id
            """,
            (
                "production-topt-integration",
                source_record_id,
                raw_sha256,
                f"s3://production-topt-integration/{raw_sha256}",
                cutoff - timedelta(hours=2),
                cutoff - timedelta(hours=2),
            ),
        ).fetchone()[0]
        vintage = SourceVintage(
            source_request_id=request.source_request_id,
            source_record_id=source_record_id,
            source_published_at=cutoff - timedelta(hours=2),
            raw_object_id=f"raw-object:{raw_sha256}",
        )
        ledger = AttemptLedger(work_item_id=work_item.work_item_id, retry_policy=policy.retry)
        attempt = ledger.start(started_at=cutoff - timedelta(hours=1))
        unchanged = stale_unchanged_first_observation and ordinal == 0
        attempt_result = ledger.finish(
            attempt=attempt,
            completed_at=cutoff - timedelta(minutes=59),
            outcome=FetchAttemptOutcome.UNCHANGED if unchanged else FetchAttemptOutcome.SUCCESS,
            status_code=200,
            source_vintage_id=None if unchanged else vintage.source_vintage_id,
            reused_source_vintage_id=vintage.source_vintage_id if unchanged else None,
        )
        observation = NormalizedObservation(
            semantic_type=semantic_type,
            semantic_version=obligation.capture_requirement_id,
            subject=obligation.subject,
            valid_from=valid_from_by_semantic.get(semantic_type, cutoff - timedelta(days=2)),
            valid_to=valid_to_by_semantic.get(semantic_type),
            knowable_at=cutoff - (timedelta(days=3) if unchanged else timedelta(minutes=58)),
            source_vintage_id=vintage.source_vintage_id,
            parser_version="production-topt-integration-parser:v1",
            mapping_version="production-topt-integration-map:v1",
            normalized_payload_sha256=canonical_sha256(normalized_payload),
        )
        terminal = ListObligationResult(
            obligation_id=obligation.obligation.obligation_id,
            terminal_state=(ObligationTerminalState.UNCHANGED if unchanged else ObligationTerminalState.SUCCESS),
            completed_at=cutoff - timedelta(minutes=57),
            final_attempt_id=attempt.attempt_id,
            reason_codes=("unchanged" if unchanged else "success",),
        )
        repository.put_attempt(attempt)
        repository.put_source_vintage(vintage, raw_fetch_id=raw_fetch_id)
        repository.put_attempt_result(attempt_result)
        repository.put_observation(
            obligation.obligation_id,
            observation,
            normalized_payload=normalized_payload,
            confidence=Decimal("0.9"),
            freshness_state="fresh",
        )
        repository.put_obligation_result(obligation.obligation_id, terminal)

        if ordinal == 0:
            terminal_observation_id = observation.observation_id
            future_valid = NormalizedObservation(
                semantic_type=semantic_type,
                semantic_version=obligation.capture_requirement_id,
                subject=obligation.subject,
                valid_from=cutoff + timedelta(days=1),
                knowable_at=cutoff - timedelta(minutes=30),
                source_vintage_id=vintage.source_vintage_id,
                parser_version="production-topt-integration-parser:v1",
                mapping_version="production-topt-integration-map:v1",
                normalized_payload_sha256=canonical_sha256(normalized_payload),
            )
            repository.put_observation(
                obligation.obligation_id,
                future_valid,
                normalized_payload=normalized_payload,
                confidence=Decimal("1"),
                freshness_state="fresh",
            )

            tied_observations = []
            for suffix in ("a", "b"):
                tied_vintage = SourceVintage(
                    source_request_id=request.source_request_id,
                    source_record_id=f"{source_record_id}-tie-{suffix}",
                    source_published_at=cutoff - timedelta(hours=2),
                    raw_object_id=f"raw-object:{raw_sha256}",
                )
                repository.put_source_vintage(tied_vintage, raw_fetch_id=raw_fetch_id)
                tied_observations.append(
                    NormalizedObservation(
                        semantic_type=semantic_type,
                        semantic_version=obligation.capture_requirement_id,
                        subject=obligation.subject,
                        valid_from=cutoff - timedelta(days=2),
                        knowable_at=cutoff - timedelta(minutes=30),
                        source_vintage_id=tied_vintage.source_vintage_id,
                        parser_version="production-topt-integration-parser:v1",
                        mapping_version="production-topt-integration-map:v1",
                        normalized_payload_sha256=canonical_sha256(normalized_payload),
                    )
                )
            for tied_observation in tied_observations:
                repository.put_observation(
                    obligation.obligation_id,
                    tied_observation,
                    normalized_payload=normalized_payload,
                    confidence=Decimal("0.9"),
                    freshness_state="fresh",
                )
            unselected_same_request_observation_ids = tuple(item.observation_id for item in tied_observations)

            foreign_request = _source_request(obligation, ordinal=1000)
            repository.put_source_request(foreign_request)
            foreign_vintage = SourceVintage(
                source_request_id=foreign_request.source_request_id,
                source_record_id=f"{source_record_id}-foreign",
                source_published_at=cutoff - timedelta(hours=2),
                raw_object_id=f"raw-object:{raw_sha256}",
            )
            repository.put_source_vintage(foreign_vintage, raw_fetch_id=raw_fetch_id)
            foreign_observation = NormalizedObservation(
                semantic_type=semantic_type,
                semantic_version=obligation.capture_requirement_id,
                subject=obligation.subject,
                valid_from=cutoff - timedelta(days=2),
                knowable_at=cutoff - timedelta(minutes=10),
                source_vintage_id=foreign_vintage.source_vintage_id,
                parser_version="production-topt-integration-parser:v1",
                mapping_version="production-topt-integration-map:v1",
                normalized_payload_sha256=canonical_sha256(normalized_payload),
            )
            foreign_observation_id = foreign_observation.observation_id
            repository.put_observation(
                obligation.obligation_id,
                foreign_observation,
                normalized_payload=normalized_payload,
                confidence=Decimal("1"),
                freshness_state="fresh",
            )

    connection.execute(
        """
        insert into staging.contract_objects (
            contract_id, contract_kind, content_sha256, payload
        ) values (%s, 'release_manifest', %s, %s)
        on conflict (contract_id) do nothing
        """,
        (release_manifest_id, release_sha256, psycopg.types.json.Jsonb(release_payload)),
    )
    assert terminal_observation_id is not None
    assert unselected_same_request_observation_ids is not None
    assert foreign_observation_id is not None
    return (
        repository,
        run,
        list_version,
        release_manifest_id,
        terminal_observation_id,
        unselected_same_request_observation_ids,
        foreign_observation_id,
    )


def test_exact_production_snapshot_materializes_queryable_core_and_meta_info(connection) -> None:
    (
        capture_repository,
        run,
        list_version,
        release_manifest_id,
        terminal_observation_id,
        unselected_same_request_observation_ids,
        foreign_observation_id,
    ) = _seed_complete_production_run(connection)
    assert capture_repository.status(run.run_id).complete

    repository = PostgresToptCoreRepository(connection)
    snapshot = repository.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
    selected_observation_ids = {
        observation_id for member in snapshot.members for observation_id in member.observation_ids
    }
    assert terminal_observation_id in selected_observation_ids
    assert selected_observation_ids.isdisjoint(unselected_same_request_observation_ids)
    assert foreign_observation_id not in selected_observation_ids
    assert connection.execute(
        """
        select observation_id from mart.topt_capture_meta_info
        where observation_id in (%s, %s, %s, %s)
        """,
        (
            terminal_observation_id,
            *unselected_same_request_observation_ids,
            foreign_observation_id,
        ),
    ).fetchall() == [(terminal_observation_id,)]

    base_payload, obligation_id, normalized_payload = connection.execute(
        """
        select observation.payload, observation.capture_obligation_id, payload.normalized_payload
        from staging.capture_normalized_observations observation
        join staging.capture_observation_payloads payload using (observation_id)
        join raw.capture_obligations obligation
          on obligation.obligation_id = observation.capture_obligation_id
        where obligation.run_id = %s
          and (observation.valid_from at time zone 'UTC')::date = date '2026-03-31'
        order by observation.knowable_at limit 1
        """,
        (run.run_id,),
    ).fetchone()
    later_payload = {
        **base_payload,
        "observation_id": "",
        "content_sha256": "",
        "knowable_at": CUTOFF - timedelta(minutes=10),
    }
    later_observation = NormalizedObservation.model_validate(later_payload)
    capture_repository.put_observation(
        obligation_id,
        later_observation,
        normalized_payload=normalized_payload,
        confidence=Decimal("1"),
        freshness_state="fresh",
    )
    assert repository.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id) == snapshot
    with pytest.raises(ValueError, match="different release manifest"):
        repository.freeze_snapshot(
            run_id=run.run_id,
            release_manifest_id=f"release-manifest:{'0' * 64}",
        )
    results = repository.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))
    repeated = repository.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.050"))

    assert repeated == results
    assert len(results) == 20
    assert len({item.issuer_id for item in results}) == 20
    assert sum(item.availability is ToptCoreAvailability.AVAILABLE for item in results) == 20
    assert {item.gppe for item in results if item.gppe is not None} == {Decimal("2000000"), Decimal("700000")}
    alphabet = next(item for item in results if item.issuer_id == "issuer:lei:5493006MHB84DD0ZWV18")
    # #705: the previous pin was 8 — the dual-listed issuer summed price×shares
    # per listing while every listing carries the COMPANY-total dei share count,
    # so Alphabet valued the company twice. Identical share counts value once at
    # the execution listing's price: 40 × 10M / 100M = 4, same as every
    # single-class issuer built from the same fixture numbers.
    assert alphabet.current_ps == Decimal("4")
    single_class = next(
        item for item in results if item.issuer_id != alphabet.issuer_id and item.current_ps is not None
    )
    assert alphabet.current_ps == single_class.current_ps, "dual listing must not change the company's P/S"
    financial = next(item for item in results if item.issuer_id == "issuer:lei:8I5DZWZKVSZI1NUHU748")
    # #394: the financial issuer now takes the uniform capital-adjusted path and flows
    # through the tier / P-S valuation like every other issuer -- no not-comparable
    # short-circuit. (80_000_000 - 200_000_000*0.05)/100 = 700_000, not 80M/100.
    assert financial.availability is ToptCoreAvailability.AVAILABLE
    assert financial.gppe == Decimal("700000")
    assert financial.operating_efficiency == Decimal("700000")
    assert financial.tier is not None
    assert financial.current_ps is not None
    assert financial.reason_codes == ()
    identity = ToptCoreIdentity(
        run_id=run.run_id,
        release_manifest_id=release_manifest_id,
        universe_id=list_version.universe.universe_id,
        universe_version=list_version.universe.universe_version,
        universe_sha256=list_version.universe.content_sha256,
        snapshot_id=snapshot.snapshot_id,
        invocation_id=results[0].invocation_id,
    )
    reads = repository.results(identity)
    meta_info = repository.meta_info(identity)
    assert len(reads) == len(meta_info) == 20
    assert all(item.gppe_invocation_id == results[0].gppe_invocation_id for item in reads)
    assert {item.gppe_result_id for item in reads} == {item.gppe_result_id for item in results}
    assert sorted(len(item.lineage) for item in meta_info) == [4] * 19 + [8]
    assert connection.execute("select count(*) from mart.topt_gppe_invocations").fetchone() == (1,)
    assert connection.execute("select count(*) from mart.topt_gppe_results").fetchone() == (20,)
    assert connection.execute(
        """
        select count(*)
        from mart.topt_core_results core
        join mart.topt_gppe_results gppe on gppe.result_id = core.gppe_result_id
        where gppe.invocation_id = core.gppe_invocation_id
          and gppe.snapshot_id = core.snapshot_id
          and gppe.issuer_id = core.issuer_id
        """
    ).fetchone() == (20,)
    assert (
        repository.results(ToptCoreIdentity(**{**identity.__dict__, "snapshot_id": f"topt-core-snapshot:{'0' * 64}"}))
        == ()
    )
    assert (
        repository.results(
            ToptCoreIdentity(**{**identity.__dict__, "invocation_id": f"topt-core-invocation:{'0' * 64}"})
        )
        == ()
    )
    different = repository.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.04"))
    assert len(different) == 20
    assert different[0].invocation_id != identity.invocation_id
    assert repository.results(identity) == reads
    assert connection.execute("select count(*) from staging.capture_observation_payloads").fetchone() == (89,)
    assert connection.execute("select count(*) from staging.topt_core_snapshot_members").fetchone() == (20,)

    corrupt_payload = results[0].model_dump(mode="json", exclude={"result_id", "content_sha256"})
    corrupt_payload["gppe_result_id"] = results[1].gppe_result_id
    corrupt_sha256 = canonical_sha256(corrupt_payload)
    with pytest.raises(psycopg.errors.CheckViolation, match="does not match its invocation"), connection.transaction():
        connection.execute(
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
            )
            select
                %s, %s, invocation_id, snapshot_id, run_id,
                release_manifest_id, universe_id, universe_version, universe_sha256,
                cutoff, issuer_id, instrument_id, listing_id, operating_branch,
                operating_metric, availability, operating_efficiency,
                capital_adjusted_gross_profit, gppe, tier, target_ps_lower,
                target_ps_upper, target_ps_midpoint, current_ps, valuation_gap,
                confidence, freshness, reason_codes, input_observation_ids,
                gppe_invocation_id, %s,
                gppe_definition_id, gppe_definition_sha256,
                tier_definition_id, tier_definition_sha256, %s
            from mart.topt_core_results where result_id = %s
            on conflict (result_id) do nothing
            """,
            (
                f"topt-core-result:{corrupt_sha256}",
                corrupt_sha256,
                results[1].gppe_result_id,
                psycopg.types.json.Jsonb(corrupt_payload),
                results[0].result_id,
            ),
        )

    with pytest.raises(psycopg.errors.RaiseException, match="append-only"), connection.transaction():
        connection.execute(
            "update mart.topt_core_results set confidence = 0 where result_id = %s",
            (results[0].result_id,),
        )


# The obligations' partition_key is the universe anchor (corpus report_date, 2026-03-31).
# CUTOFF is 2026-04-02. Each adapter writes valid_from as the date of the fact itself, so a
# fact can lie after the anchor and still be knowable at the cutoff (#1060).
_NO_PAYLOAD_PER_OBLIGATION = "does not expose one normalized payload per obligation"
_FACT_OWN_DATES = {
    "listing-identity": datetime(2026, 3, 31, tzinfo=UTC),
    "universe-membership": datetime(2026, 3, 31, tzinfo=UTC),
    "financial-fact": datetime(2026, 2, 14, tzinfo=UTC),
    "market-price": datetime(2026, 4, 1, tzinfo=UTC),
}


def test_freeze_selects_a_fact_dated_after_the_anchor_and_before_the_cutoff(connection) -> None:
    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(
        connection, valid_from_by_semantic=_FACT_OWN_DATES
    )

    snapshot = PostgresToptCoreRepository(connection).freeze_snapshot(
        run_id=run.run_id, release_manifest_id=release_manifest_id
    )

    assert len(snapshot.members) == 21
    selected = sorted({observation_id for member in snapshot.members for observation_id in member.observation_ids})
    selected_dates = connection.execute(
        """
        select semantic_type, (valid_from at time zone 'UTC')::date
        from staging.capture_normalized_observations
        where observation_id = any(%s)
        group by 1, 2 order by 1
        """,
        (selected,),
    ).fetchall()
    assert selected_dates == [
        ("financial-fact", date(2026, 2, 14)),
        ("listing-identity", date(2026, 3, 31)),
        ("market-price", date(2026, 4, 1)),
        ("universe-membership", date(2026, 3, 31)),
    ]
    # The meta info view judges valid time with the same rule: it must expose the 84 selected rows.
    exposed = connection.execute(
        """
        select observation_id, freshness_state from mart.topt_capture_meta_info
        where run_id = %s and observation_id is not null
        """,
        (run.run_id,),
    ).fetchall()
    assert sorted(row[0] for row in exposed) == selected
    assert {row[1] for row in exposed} == {"fresh"}


def test_freeze_selects_a_fact_whose_window_starts_and_ends_on_the_cutoff_date(connection) -> None:
    window = {semantic_type: CUTOFF for semantic_type in SEMANTIC_TYPES}
    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(
        connection, valid_from_by_semantic=window, valid_to_by_semantic=window
    )

    snapshot = PostgresToptCoreRepository(connection).freeze_snapshot(
        run_id=run.run_id, release_manifest_id=release_manifest_id
    )

    assert len(snapshot.members) == 21


@pytest.mark.parametrize("semantic_type", SEMANTIC_TYPES)
def test_freeze_refuses_a_fact_that_starts_after_the_cutoff_date(connection, semantic_type: str) -> None:
    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(
        connection, valid_from_by_semantic={semantic_type: CUTOFF + timedelta(days=1)}
    )

    with pytest.raises(ValueError, match=_NO_PAYLOAD_PER_OBLIGATION):
        PostgresToptCoreRepository(connection).freeze_snapshot(
            run_id=run.run_id, release_manifest_id=release_manifest_id
        )


@pytest.mark.parametrize("semantic_type", SEMANTIC_TYPES)
def test_freeze_refuses_a_fact_that_ended_before_the_cutoff_date(connection, semantic_type: str) -> None:
    # valid_to is after the anchor (2026-03-31) and before the cutoff date (2026-04-02).
    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(
        connection, valid_to_by_semantic={semantic_type: CUTOFF - timedelta(days=1)}
    )

    with pytest.raises(ValueError, match=_NO_PAYLOAD_PER_OBLIGATION):
        PostgresToptCoreRepository(connection).freeze_snapshot(
            run_id=run.run_id, release_manifest_id=release_manifest_id
        )


def _exposed_by_semantic(connection, run_id: str) -> dict[str, int]:
    """The number of obligations per semantic type for which the view exposes an observation."""
    rows = connection.execute(
        """
        select regexp_replace(capture_requirement_id, ':v1$', ''), count(observation_id)
        from mart.topt_capture_meta_info
        where run_id = %s
        group by 1 order by 1
        """,
        (run_id,),
    ).fetchall()
    return dict(rows)


def test_view_exposes_no_observation_for_a_candidate_that_starts_after_the_cutoff_day(connection) -> None:
    (_, run, *_rest) = _seed_complete_production_run(
        connection, valid_from_by_semantic={"market-price": CUTOFF + timedelta(days=1)}
    )

    assert _exposed_by_semantic(connection, run.run_id) == {
        "financial-fact": 21,
        "listing-identity": 21,
        "market-price": 0,
        "universe-membership": 21,
    }


def test_view_exposes_no_observation_for_a_candidate_that_ended_before_the_cutoff_day(connection) -> None:
    (_, run, *_rest) = _seed_complete_production_run(
        connection, valid_to_by_semantic={"market-price": CUTOFF - timedelta(days=1)}
    )

    assert _exposed_by_semantic(connection, run.run_id) == {
        "financial-fact": 21,
        "listing-identity": 21,
        "market-price": 0,
        "universe-membership": 21,
    }


def test_view_exposes_the_in_window_candidate_when_out_of_window_candidates_exist(connection) -> None:
    (capture_repository, _, _, _, terminal_observation_id, *_rest) = _seed_complete_production_run(connection)
    base_payload, obligation_id, normalized_payload = connection.execute(
        """
        select observation.payload, observation.capture_obligation_id, payload.normalized_payload
        from staging.capture_normalized_observations observation
        join staging.capture_observation_payloads payload using (observation_id)
        where observation.observation_id = %s
        """,
        (terminal_observation_id,),
    ).fetchone()
    # The seeder already holds one candidate that starts after the cutoff day for this
    # obligation. This one ended before the cutoff day. Both share the terminal vintage.
    expired = NormalizedObservation.model_validate(
        {
            **base_payload,
            "observation_id": "",
            "content_sha256": "",
            "valid_to": CUTOFF - timedelta(days=1),
            "knowable_at": CUTOFF - timedelta(minutes=30),
        }
    )
    capture_repository.put_observation(
        obligation_id,
        expired,
        normalized_payload=normalized_payload,
        confidence=Decimal("1"),
        freshness_state="fresh",
    )
    not_started = connection.execute(
        """
        select observation_id
        from staging.capture_normalized_observations
        where observation_id in (
            select observation_id from staging.capture_observation_obligations where capture_obligation_id = %s
        ) and valid_from = %s
        """,
        (obligation_id, CUTOFF + timedelta(days=1)),
    ).fetchall()
    assert len(not_started) == 1, "the seeder holds one candidate that starts after the cutoff day"

    exposed = connection.execute(
        "select observation_id from mart.topt_capture_meta_info where obligation_id = %s", (obligation_id,)
    ).fetchall()

    assert expired.observation_id != terminal_observation_id
    assert exposed == [(terminal_observation_id,)]


# A session TimeZone must not move a UTC day. psycopg renders a timestamptz in the session
# TimeZone, and a cast to date follows it (#885). Each case sits on a UTC day boundary.
_NON_UTC_ZONES = ("America/Los_Angeles", "Asia/Shanghai")


@pytest.mark.parametrize("zone", _NON_UTC_ZONES)
def test_valid_to_at_midnight_of_the_cutoff_day_stays_valid_in_any_session_time_zone(connection, zone: str) -> None:
    cutoff = datetime(2026, 4, 2, 22, 15, tzinfo=UTC)
    midnight = datetime(2026, 4, 2, tzinfo=UTC)
    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(
        connection, cutoff=cutoff, valid_to_by_semantic=dict.fromkeys(SEMANTIC_TYPES, midnight)
    )
    connection.execute("select set_config('TimeZone', %s, false)", (zone,))

    snapshot = PostgresToptCoreRepository(connection).freeze_snapshot(
        run_id=run.run_id, release_manifest_id=release_manifest_id
    )

    assert len(snapshot.members) == 21
    assert _exposed_by_semantic(connection, run.run_id) == dict.fromkeys(SEMANTIC_TYPES, 21)


@pytest.mark.parametrize("zone", _NON_UTC_ZONES)
def test_valid_from_on_the_next_utc_day_is_refused_in_any_session_time_zone(connection, zone: str) -> None:
    cutoff = datetime(2026, 4, 2, 23, 59, 59, tzinfo=UTC)
    next_midnight = datetime(2026, 4, 3, tzinfo=UTC)
    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(
        connection, cutoff=cutoff, valid_from_by_semantic=dict.fromkeys(SEMANTIC_TYPES, next_midnight)
    )
    connection.execute("select set_config('TimeZone', %s, false)", (zone,))

    with pytest.raises(ValueError, match=_NO_PAYLOAD_PER_OBLIGATION):
        PostgresToptCoreRepository(connection).freeze_snapshot(
            run_id=run.run_id, release_manifest_id=release_manifest_id
        )
    assert _exposed_by_semantic(connection, run.run_id) == dict.fromkeys(SEMANTIC_TYPES, 0)


def test_snapshot_recomputes_freshness_for_unchanged_observation_at_cutoff(connection) -> None:
    (
        _,
        run,
        _,
        release_manifest_id,
        terminal_observation_id,
        _,
        _,
    ) = _seed_complete_production_run(connection, stale_unchanged_first_observation=True)

    stored, projected = connection.execute(
        """
        select observation.freshness_state, meta.freshness_state
        from staging.capture_normalized_observations observation
        join mart.topt_capture_meta_info meta using (observation_id)
        where observation.observation_id = %s
        """,
        (terminal_observation_id,),
    ).fetchone()
    assert stored == "fresh"
    assert projected == "stale"

    repository = PostgresToptCoreRepository(connection)
    snapshot = repository.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
    selected_cells = [
        cell for member in snapshot.members for cell in member.cell_inputs if cell.input_id == terminal_observation_id
    ]
    assert len(selected_cells) == 1
    assert selected_cells[0].freshness.value == "stale"
    results = repository.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))
    affected = next(result for result in results if terminal_observation_id in result.input_observation_ids)
    assert affected.availability is ToptCoreAvailability.UNAVAILABLE
    assert tuple(reason.value for reason in affected.reason_codes) == ("stale_input",)

    # #530: the quality report grades the same run from the same table this test just
    # proved is stale (mart.topt_capture_meta_info); it must not fall back to the
    # frozen capture_normalized_observations.freshness_state and call the cell fresh.
    graded = quality_report.build_report(connection, run.run_id)
    live_fresh_count = connection.execute(
        "select count(*) from mart.topt_capture_meta_info where run_id = %s and freshness_state = 'fresh'",
        (run.run_id,),
    ).fetchone()[0]
    assert graded["fresh_count"] == live_fresh_count


def test_snapshot_rejects_ambiguous_mapping_for_terminal_source_vintage(connection) -> None:
    (
        capture_repository,
        run,
        _,
        release_manifest_id,
        terminal_observation_id,
        _,
        _,
    ) = _seed_complete_production_run(connection)
    observation_payload, obligation_id, normalized_payload = connection.execute(
        """
        select observation.payload, observation.capture_obligation_id, payload.normalized_payload
        from staging.capture_normalized_observations observation
        join staging.capture_observation_payloads payload using (observation_id)
        where observation.observation_id = %s
        """,
        (terminal_observation_id,),
    ).fetchone()
    ambiguous_observation = NormalizedObservation.model_validate(
        {
            **observation_payload,
            "observation_id": "",
            "content_sha256": "",
            "mapping_version": "production-topt-integration-map:v2",
        }
    )
    capture_repository.put_observation(
        obligation_id,
        ambiguous_observation,
        normalized_payload=normalized_payload,
        confidence=Decimal("0.9"),
        freshness_state="fresh",
    )

    with pytest.raises(ValueError, match="does not resolve exactly one normalized observation"):
        PostgresToptCoreRepository(connection).freeze_snapshot(
            run_id=run.run_id,
            release_manifest_id=release_manifest_id,
        )
    assert connection.execute(
        """
        select observation_id from mart.topt_capture_meta_info
        where run_id = %s and obligation_id = %s
        """,
        (run.run_id, obligation_id),
    ).fetchall() == [(None,)]


def test_snapshot_rejects_unknown_run(connection) -> None:
    repository = PostgresToptCoreRepository(connection)
    with pytest.raises(LookupError, match="capture run not found"):
        repository.freeze_snapshot(
            run_id=f"capture-run:{'0' * 64}",
            release_manifest_id=f"release-manifest:{'1' * 64}",
        )


def _cell_row(listing_id: str, semantic_type: str, ordinal: int) -> _ObservationRow:
    return _ObservationRow(
        obligation_id=f"capture-list-obligation:{ordinal:064x}",
        listing_id=listing_id,
        semantic_type=semantic_type,
        observation_id=f"normalized-observation:{ordinal:064x}",
        confidence=Decimal("0.9"),
        freshness=MetricFreshness.FRESH,
        knowable_at=CUTOFF,
        payload={},
    )


@pytest.mark.parametrize(
    "cells",
    [
        pytest.param(SEMANTIC_TYPES[:3], id="one-cell-missing"),
        pytest.param((*SEMANTIC_TYPES, "extra-cell"), id="one-cell-too-many"),
        pytest.param((*SEMANTIC_TYPES[:3], "extra-cell"), id="one-cell-replaced"),
    ],
)
def test_a_listing_without_the_exact_four_semantic_cells_is_refused_by_the_member_builder(
    cells: tuple[str, ...],
) -> None:
    """#1061: this is the only per-listing count guard. The snapshot model and the run-wide
    equality it replaced both run later or are weaker: neither names the missing cell."""
    by_type = {semantic: _cell_row("listing:probe", semantic, ordinal) for ordinal, semantic in enumerate(cells)}

    with pytest.raises(ValueError, match="listing:probe does not have the exact four TOPT semantic cells"):
        PostgresToptCoreRepository._snapshot_member("listing:probe", by_type)


def test_a_run_whose_listing_cells_are_unbalanced_but_sum_to_the_obligations_is_refused(
    connection, monkeypatch
) -> None:
    """#1061: one listing holds three cells and another holds five. The row count equals the
    obligation count. The sum `listings * 4 == obligations` holds. The removed run-wide
    equality passed this input. The member builder refuses it, and nothing is stored."""
    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(connection)
    repository = PostgresToptCoreRepository(connection)
    rows = list(repository._load_observations(run.run_id, cutoff=CUTOFF))
    assert len(rows) == 84
    donor, receiver = rows[0], rows[-1]
    assert donor.listing_id != receiver.listing_id
    moved = dataclasses.replace(donor, listing_id=receiver.listing_id, semantic_type="extra-cell")
    unbalanced = tuple([moved, *rows[1:]])
    assert len(unbalanced) == 84
    monkeypatch.setattr(PostgresToptCoreRepository, "_load_observations", lambda self, run_id, *, cutoff: unbalanced)

    with pytest.raises(ValueError, match="does not have the exact four TOPT semantic cells"):
        repository.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
    assert connection.execute(
        "select count(*) from staging.topt_core_snapshots where run_id = %s", (run.run_id,)
    ).fetchone() == (0,)


def _insert_snapshot_row(connection, run_id: str, release_manifest_id: str, *, instruments: int, observations: int):
    """A hand-written snapshot row. Every column except the counts satisfies the snapshot
    trigger. So a refusal names the count rule, not an unrelated column."""
    universe_id, universe_version, universe_sha256, cutoff = connection.execute(
        "select universe_id, universe_version, universe_sha256, cutoff from mart.topt_capture_status where run_id = %s",
        (run_id,),
    ).fetchone()
    payload = {"probe": run_id, "instruments": instruments}
    digest = connection.execute("select raw.canonical_sha256(%s::jsonb)", (Jsonb(payload),)).fetchone()[0]
    return connection.execute(
        """
        insert into staging.topt_core_snapshots (
            snapshot_id, content_sha256, run_id, release_manifest_id, universe_id, universe_version,
            universe_sha256, cutoff, issuer_count, instrument_count, observation_count, payload
        ) values (%s, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s, %s)
        """,
        (
            f"topt-core-snapshot:{digest}",
            digest,
            run_id,
            release_manifest_id,
            universe_id,
            universe_version,
            universe_sha256,
            cutoff,
            instruments,
            observations,
            Jsonb(payload),
        ),
    )


def test_the_database_refuses_a_snapshot_that_does_not_bind_four_observations_per_instrument(connection) -> None:
    """#1061: the kept database check. The run has 84 obligations, so the snapshot trigger
    accepts observation_count 84 and the CHECK alone refuses 20 instruments."""
    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(connection)

    with pytest.raises(psycopg.errors.CheckViolation) as refused, connection.transaction():
        _insert_snapshot_row(connection, run.run_id, release_manifest_id, instruments=20, observations=84)
    assert refused.value.diag.constraint_name == "topt_core_snapshots_observation_count_check"

    with connection.transaction():
        _insert_snapshot_row(connection, run.run_id, release_manifest_id, instruments=21, observations=84)


def test_the_database_refuses_a_snapshot_with_fewer_members_than_the_run_has_obligations(connection) -> None:
    """#1061: the kept database check for the same input the removed run-wide equality
    refused. 20 instruments with 80 observations satisfy the CHECK and miss the run's 84."""
    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(connection)

    with (
        pytest.raises(psycopg.errors.CheckViolation, match="matching its own obligation count"),
        connection.transaction(),
    ):
        _insert_snapshot_row(connection, run.run_id, release_manifest_id, instruments=20, observations=80)

    with connection.transaction():
        _insert_snapshot_row(connection, run.run_id, release_manifest_id, instruments=21, observations=84)


def test_the_database_refuses_a_snapshot_member_that_repeats_an_observation(connection) -> None:
    """#1061: the kept database check for the repeated-observation input. The control row
    differs only in distinct ids, so the member trigger's other clauses all hold."""
    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(connection)
    snapshot = PostgresToptCoreRepository(connection).freeze_snapshot(
        run_id=run.run_id, release_manifest_id=release_manifest_id
    )
    ids = list(snapshot.members[0].observation_ids)

    def insert_member(label: str, observation_ids: list[str]) -> None:
        factor_input = {
            "snapshot_id": snapshot.snapshot_id,
            "instrument_id": f"security:probe:{label}",
            "issuer_id": f"issuer:probe:{label}",
            "listing_id": f"listing:probe:{label}",
        }
        digest = connection.execute("select raw.canonical_sha256(%s::jsonb)", (Jsonb(factor_input),)).fetchone()[0]
        connection.execute(
            """
            insert into staging.topt_core_snapshot_members (
                snapshot_id, instrument_id, issuer_id, listing_id, observation_ids, member_sha256, factor_input
            ) values (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                snapshot.snapshot_id,
                factor_input["instrument_id"],
                factor_input["issuer_id"],
                factor_input["listing_id"],
                observation_ids,
                digest,
                Jsonb(factor_input),
            ),
        )

    with connection.transaction():
        insert_member("distinct", ids)
    with (
        pytest.raises(psycopg.errors.CheckViolation, match="member identity or payload drifted"),
        connection.transaction(),
    ):
        insert_member("repeated", [ids[0], ids[0], ids[1], ids[2]])


_NOT_SUCCESSFUL = ("unavailable", "skipped_by_policy", "failed", "missing")


def _spoil_one_result(connection, run_id: str, how: str) -> None:
    """Leave one obligation of a complete run without a successful terminal result.

    Results are append-only by trigger, so the change bypasses the trigger for this
    transaction only. The test's rollback restores everything."""
    connection.execute("set local session_replication_role = replica")
    result_id = connection.execute(
        """
        select result.result_id
        from raw.capture_obligation_results result
        join raw.capture_obligations obligation on obligation.obligation_id = result.capture_obligation_id
        where obligation.run_id = %s
        order by result.result_id limit 1
        """,
        (run_id,),
    ).fetchone()[0]
    if how == "missing":
        connection.execute("delete from raw.capture_obligation_results where result_id = %s", (result_id,))
    else:
        connection.execute(
            """
            update raw.capture_obligation_results
               set terminal_state = %s,
                   final_attempt_id = case when %s = 'skipped_by_policy' then null else final_attempt_id end
             where result_id = %s
            """,
            (how, how, result_id),
        )
    connection.execute("set local session_replication_role = origin")
    assert connection.execute(
        "select success_count + unchanged_count, obligation_count from mart.topt_capture_status where run_id = %s",
        (run_id,),
    ).fetchone() == (83, 84)


@pytest.mark.parametrize("how", _NOT_SUCCESSFUL)
def test_freeze_refuses_a_run_with_one_obligation_that_did_not_succeed(connection, how: str) -> None:
    """#1061: `success + unchanged == obligations` is the one status condition freeze keeps.
    It leaves no room for an unavailable, skipped or failed obligation, or for a gap."""
    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(connection)
    _spoil_one_result(connection, run.run_id, how)

    with pytest.raises(ValueError, match="core snapshot requires a completely successful run"):
        PostgresToptCoreRepository(connection).freeze_snapshot(
            run_id=run.run_id, release_manifest_id=release_manifest_id
        )
    assert connection.execute(
        "select count(*) from staging.topt_core_snapshots where run_id = %s", (run.run_id,)
    ).fetchone() == (0,)


@pytest.mark.parametrize("how", _NOT_SUCCESSFUL)
def test_the_database_refuses_a_snapshot_for_a_run_with_one_obligation_that_did_not_succeed(
    connection, how: str
) -> None:
    """#1061: the kept database check for the same input. The row binds 21 instruments and
    84 observations, so only the run's own status can make the snapshot trigger refuse."""
    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(connection)
    _spoil_one_result(connection, run.run_id, how)

    with (
        pytest.raises(psycopg.errors.CheckViolation, match="matching its own obligation count"),
        connection.transaction(),
    ):
        _insert_snapshot_row(connection, run.run_id, release_manifest_id, instruments=21, observations=84)


@pytest.mark.parametrize("how", (None, *_NOT_SUCCESSFUL))
def test_the_capture_status_counts_partition_the_results_of_a_run(connection, how: str | None) -> None:
    """#1061: freeze drops its `unavailable == 0`, `skipped == 0`, `failed == 0` and
    `terminal == obligations` terms because the status view makes them follow from
    `success + unchanged == obligations`. This pins the view facts that argument uses."""
    (_, run, _, _, *_rest) = _seed_complete_production_run(connection)
    if how is not None:
        _spoil_one_result(connection, run.run_id, how)

    (obligations, terminal, success, unchanged, unavailable, skipped, failed, complete) = connection.execute(
        """
        select obligation_count, terminal_count, success_count, unchanged_count,
               unavailable_count, skipped_count, failed_count, complete
        from mart.topt_capture_status where run_id = %s
        """,
        (run.run_id,),
    ).fetchone()

    assert success + unchanged + unavailable + skipped + failed == terminal
    assert terminal <= obligations
    assert complete is (terminal == obligations)


def _forbid_loading_observations(monkeypatch) -> list[str]:
    """Make any observation load fail the test. The list records the run ids that were loaded."""
    loaded: list[str] = []

    def load(self, run_id, *, cutoff):
        loaded.append(run_id)
        raise AssertionError("freeze loaded observations for a run that it must refuse first")

    monkeypatch.setattr(PostgresToptCoreRepository, "_load_observations", load)
    return loaded


def _snapshot_count(connection, run_id: str) -> tuple[int]:
    return connection.execute(
        "select count(*) from staging.topt_core_snapshots where run_id = %s", (run_id,)
    ).fetchone()


def test_freeze_refuses_a_staging_run_when_the_environment_identity_is_empty(connection, monkeypatch) -> None:
    """#1061: with an empty identity table the snapshot trigger compares with NULL and does
    not fire. The control row at the end shows that gap. Only the freeze check refuses."""
    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(
        connection, environment=CaptureEnvironment.STAGING
    )
    connection.execute("delete from mart.environment_identity")
    with monkeypatch.context() as patch:
        loaded = _forbid_loading_observations(patch)
        with pytest.raises(ValueError, match="requires a production run, found a staging run"):
            PostgresToptCoreRepository(connection).freeze_snapshot(
                run_id=run.run_id, release_manifest_id=release_manifest_id
            )
    assert loaded == []
    assert _snapshot_count(connection, run.run_id) == (0,)

    with connection.transaction():
        _insert_snapshot_row(connection, run.run_id, release_manifest_id, instruments=21, observations=84)


def test_freeze_refuses_a_run_of_another_environment_when_the_identity_is_populated(connection, monkeypatch) -> None:
    """#1061: the freeze check refuses before it loads any observation. The same run freezes
    once the identity matches again."""
    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(connection)
    repository = PostgresToptCoreRepository(connection)
    connection.execute("update mart.environment_identity set environment = 'staging'")
    with monkeypatch.context() as patch:
        loaded = _forbid_loading_observations(patch)
        with pytest.raises(ValueError, match="requires a staging run, found a production run"):
            repository.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
    assert loaded == []
    assert _snapshot_count(connection, run.run_id) == (0,)

    connection.execute("update mart.environment_identity set environment = 'production'")
    assert len(repository.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id).members) == 21


def test_the_database_refuses_a_snapshot_for_a_run_of_another_environment(connection) -> None:
    """#1061: the snapshot trigger is the second guard. It refuses a hand-written row when the
    identity is populated. The same row passes once the identity matches."""
    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(connection)
    connection.execute("update mart.environment_identity set environment = 'staging'")

    with (
        pytest.raises(psycopg.errors.CheckViolation, match="matching its own obligation count"),
        connection.transaction(),
    ):
        _insert_snapshot_row(connection, run.run_id, release_manifest_id, instruments=21, observations=84)

    connection.execute("update mart.environment_identity set environment = 'production'")
    with connection.transaction():
        _insert_snapshot_row(connection, run.run_id, release_manifest_id, instruments=21, observations=84)


class _BorrowedConnection:
    """Lends the test's own transaction to code that would open a fresh psycopg
    connection: `PostgresToptGppeRepository` connects per call, but the governed
    read must see the same uncommitted rows the real writers just produced, and
    committing the deterministic corpus identity would break the first-insert
    assertions other tests make against a shared database. Every SQL statement
    still executes against the real schema; only connection acquisition is
    redirected, rows are shaped per cursor (dict_row, the repository's own row
    factory), and close/commit are withheld so the test's rollback owns the
    data's lifetime. This is the same seam test_topt_read.py fakes — here the
    backend is the real database."""

    def __init__(self, real: psycopg.Connection) -> None:
        self._real = real

    def __enter__(self) -> _BorrowedConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, query, params=None):
        return self._real.cursor(row_factory=psycopg.rows.dict_row).execute(query, params)

    def cursor(self, **kwargs):
        # PostgresStrategyRunRepository opens its own dict_row cursor.
        return self._real.cursor(**kwargs)


def test_governed_read_serves_the_mcp_repository_only_after_pointer_advance(connection, monkeypatch) -> None:
    """#462: the exact repository class MCP ships (`PostgresToptGppeRepository`,
    asserted as `_default_repository()`'s type by test_mcp_server) reads real
    materialized rows through the governed pointer, against the real schema —
    seeded by the real writers, never skipped for lack of data.

    Trigger states covered (#461 postmortem — that bug fired only once
    `mart.current_pointer_head` had rows, so every head-resolution branch is
    driven with data, in the deployed order):
      1. nothing seeded                -> unavailable
      2. materialized, no quality      -> still unavailable (the acceptance gate)
      3. quality persisted, no pointer -> served via the acceptance-gated FALLBACK
      4. pointer advanced              -> served via the governed POINTER path
    """
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: _BorrowedConnection(connection))
    reader = PostgresToptGppeRepository(database_url=settings.database_url)

    # State 4 must be deterministic on ANY database (Copilot on #478): the
    # reader picks the head by `advanced_at desc`, and register_run_evidence
    # stamps this run's pointer with the fixed corpus cutoff — a developer
    # database with a NEWER real pointer would win and fail the served-state
    # asserts. Clear this factor's pointers inside the rolled-back transaction;
    # the append-only trigger is bypassed via replica mode, and rollback
    # restores everything.
    connection.execute("set session_replication_role = replica")
    connection.execute(
        "delete from mart.current_pointer where environment = 'production' and factor_id = %s",
        ("gross_profit_per_employee",),
    )
    connection.execute("set session_replication_role = origin")

    before = reader.latest()
    # The transaction now sees no governed pointer; `before` is a report only
    # when the committed database carries a quality-accepted complete run the
    # FALLBACK serves (a developer database). That makes the withheld-state
    # assertions vacuous while every served-state assertion still binds; CI's
    # ephemeral Postgres always takes the fresh path.
    fresh_database = isinstance(before, ToptGppeUnavailable)
    if fresh_database:
        assert before.reason == "no accepted (quality-reported) production TOPT run"

    _repository, run, _list_version, release_manifest_id, *_ = _seed_complete_production_run(connection)
    core = PostgresToptCoreRepository(connection)
    snapshot = core.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
    results = core.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))
    assert len(results) == 20

    if fresh_database:
        # State 2: captured and materialized — but not quality-accepted and not
        # pointer-advanced. The governed read must still withhold the run: the
        # head is acceptance-gated, not an ORDER BY over mart rows.
        withheld = reader.latest()
        assert isinstance(withheld, ToptGppeUnavailable)

    # Deployed order (a1_evidence docstring): the quality report persists first,
    # then the pointer advances.
    graded = quality_report.build_report(connection, run.run_id)
    quality_report.persist(connection, graded)

    if fresh_database:
        # State 3: quality-accepted but no pointer advanced yet — the reader's
        # documented interim fallback must serve exactly this run.
        via_fallback = reader.latest()
        assert isinstance(via_fallback, ToptGppeReport), f"expected the fallback to serve, got {via_fallback!r}"
        assert via_fallback.run_id == run.run_id
        assert via_fallback.quality is not None

    # #536: the advance is gated on the report. This test drives the READER's
    # governed-pointer branch, not the gate — and the checked-in corpus carries one
    # price origin whose parser vintage the fusion source map does not list, so it
    # grades zero reconciled cells and can never reach the demand's `high` band.
    # Register it against an explicitly relaxed objective set so every reader assertion
    # below still binds; the gate itself is proven in tests/datahub/test_a1_evidence.py.
    registration = register_run_evidence(
        connection,
        run_id=run.run_id,
        release_manifest_id=release_manifest_id,
        quality_report=graded,
        objectives=_CORPUS_OBJECTIVES,
    )
    assert registration.accepted, registration.summary
    assert registration.sequence is not None and registration.sequence >= 0

    # State 4: the governed pointer path — the state in which #461 crashed.
    served = reader.latest()
    assert isinstance(served, ToptGppeReport), f"expected a report after the pointer advance, got {served!r}"
    assert served.run_id == run.run_id
    assert len(served.cells) == 20
    assert served.available_count == 20
    assert all(cell.availability == "available" for cell in served.cells)
    listing_ids = [cell.listing_id for cell in served.cells]
    assert listing_ids == sorted(listing_ids)
    assert served.quality is not None and served.quality["run_id"] == run.run_id
    # #462 AC3: the denominator is DB-derived (capture plane obligation_count),
    # asserted against the corpus's real 84 requested cells on the real schema.
    assert served.requested_count == 84


def test_capture_feeds_strategy_mart_read_back_by_the_shipping_consumer(connection, monkeypatch) -> None:
    """#395's missing weld, driven end to end in one transaction: the captured
    corpus run's observation payloads cross the capture→strategy bridge
    (`seed_strategy_inputs_from_capture`, #429), the gateway + frozen evaluator
    run the strategy, `write_replay` persists to mart, and the result returns
    through the exact consumer MCP ships (`PostgresStrategyRunRepository`) —
    islands 1→2→3 with no fixture on the data path.

    Also pins a capture-shape drift so it stays visible until #407 converges the
    corpus: the checked-in corpus payload keeps `gross_profit` NULL for the
    financial issuer (raw SEC reality), while #451 fixed LIVE capture to place
    the PPNR proxy into `gross_profit`. On corpus shape the bridge therefore
    seeds no gross_profit for JPM and the evaluator excludes it — asserted
    explicitly below.
    """
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: _BorrowedConnection(connection))
    _repository, run, _list_version, _release_manifest_id, *_ = _seed_complete_production_run(connection)

    written = seed_strategy_inputs_from_capture(
        connection, run.run_id, cutoff=CUTOFF, parser_version="production-topt-integration-parser:v1"
    )
    # 19 non-financial issuers × 5 financial keys + JPM's 4 (no gross_profit on
    # corpus shape) + 20 last_close rows.
    assert written == 19 * 5 + 4 + 20

    executed_at = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)
    run_id, decision_count, snapshot_id = run_strategy_replay_for_cutoff(
        connection, cutoff=CUTOFF, executed_at=executed_at, risk_free_rate=Decimal("0.05"), capture_run_id=run.run_id
    )
    assert decision_count == 20
    assert snapshot_id.startswith("strategy-snapshot:")

    context = AccessContext(
        context_id="ctx:test-e2e",
        principal_id="principal:test-e2e",
        tenant_id="tenant:test-e2e",
        session_id="session:test-e2e",
        authentication_method=AuthenticationMethod.SERVICE_IDENTITY,
        principal_kind=PrincipalKind.SERVICE,
        issued_at=executed_at,
        expires_at=executed_at + timedelta(hours=1),
    )
    reader = PostgresStrategyRunRepository(database_url=settings.database_url)
    report = reader.get_latest(strategy_id="large_model_value_v0", context=context)
    assert isinstance(report, StrategyRunReport), f"expected a mart-backed report, got {report!r}"
    assert report.source == "mart"
    assert len(report.decisions) == decision_count

    issuer_ids = [decision.issuer_id for decision in report.decisions]
    assert issuer_ids == sorted(issuer_ids)  # single cutoff, so (cutoff_at, issuer_id) order is issuer order
    by_issuer = {decision.issuer_id: decision for decision in report.decisions}
    assert len(by_issuer) == 20

    jpm = by_issuer["issuer:lei:8I5DZWZKVSZI1NUHU748"]
    assert jpm.outcome == "excluded" and jpm.eligible is False
    assert jpm.exclusion_reason == "missing_gross_profit_fact"
    assert any(decision.outcome == "selected" for decision in report.decisions), (
        "the corpus capture must select at least one issuer"
    )

    # The persisted run binds the exact content-addressed snapshot of its inputs.
    lineage = connection.execute(
        "select snapshot_id from mart.strategy_runs where strategy_run_id = %s", (run_id,)
    ).fetchone()
    assert lineage == (snapshot_id,)


def test_superseded_input_wins_and_lookahead_is_rejected(connection, monkeypatch) -> None:
    """#395 trigger states for the strategy-input lane (0032's PIT semantics):

    - supersede-by-recorded_at: a corrected input row (same issuer/cutoff/key,
      later recorded_at) must win in the gateway's distinct-on read and flow
      through a re-run into the consumer-visible report — history is never
      overwritten, the later vintage simply supersedes (this lane's
      restatement mechanism);
    - no look-ahead: an input with knowable_at > cutoff_at must be impossible
      to land at all (the 0032 CHECK, asserted red).
    """
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: _BorrowedConnection(connection))
    _repository, run, _list_version, _release_manifest_id, *_ = _seed_complete_production_run(connection)
    seed_strategy_inputs_from_capture(
        connection, run.run_id, cutoff=CUTOFF, parser_version="production-topt-integration-parser:v1"
    )
    first_executed = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)
    run_strategy_replay_for_cutoff(
        connection, cutoff=CUTOFF, executed_at=first_executed, risk_free_rate=Decimal("0.05"), capture_run_id=run.run_id
    )

    context = AccessContext(
        context_id="ctx:test-supersede",
        principal_id="principal:test-supersede",
        tenant_id="tenant:test-supersede",
        session_id="session:test-supersede",
        authentication_method=AuthenticationMethod.SERVICE_IDENTITY,
        principal_kind=PrincipalKind.SERVICE,
        issued_at=first_executed,
        expires_at=first_executed + timedelta(hours=1),
    )
    reader = PostgresStrategyRunRepository(database_url=settings.database_url)
    first = reader.get_latest(strategy_id="large_model_value_v0", context=context)
    assert isinstance(first, StrategyRunReport)
    target = next(d for d in first.decisions if d.current_price_to_sales is not None)
    assert target.current_price_to_sales is not None

    # Correct the issuer's last_close (40 -> 80): a NEW row with a later
    # recorded_at, same (issuer, cutoff, key) — never an UPDATE.
    connection.execute(
        """
        insert into staging.strategy_backtest_inputs
            (issuer_id, cutoff_at, input_key, value, confidence, knowable_at, recorded_at)
        select issuer_id, cutoff_at, input_key, value * 2, confidence, knowable_at,
               recorded_at + interval '1 second'
        from staging.strategy_backtest_inputs
        where issuer_id = %s and cutoff_at = %s and input_key = 'last_close'
        order by recorded_at desc limit 1
        """,
        (target.issuer_id, CUTOFF),
    )
    run_strategy_replay_for_cutoff(
        connection,
        cutoff=CUTOFF,
        executed_at=first_executed + timedelta(minutes=5),
        risk_free_rate=Decimal("0.05"),
        capture_run_id=run.run_id,
    )
    second = reader.get_latest(strategy_id="large_model_value_v0", context=context)
    assert isinstance(second, StrategyRunReport)
    corrected = next(d for d in second.decisions if d.issuer_id == target.issuer_id)
    assert corrected.current_price_to_sales is not None
    assert corrected.current_price_to_sales == target.current_price_to_sales * 2, (
        f"the superseding last_close must double P/S: {target.current_price_to_sales} -> "
        f"{corrected.current_price_to_sales}"
    )

    # No look-ahead can even land: knowable_at > cutoff_at violates 0032's CHECK.
    with pytest.raises(psycopg.errors.CheckViolation):
        with connection.transaction():
            connection.execute(
                """
                insert into staging.strategy_backtest_inputs
                    (issuer_id, cutoff_at, input_key, value, confidence, knowable_at)
                values (%s, %s, 'last_close', 999, 0.9, %s)
                """,
                (target.issuer_id, CUTOFF, CUTOFF + timedelta(seconds=1)),
            )


# The corpus report_date becomes the obligations' partition_key. The seeder dates every
# observation CUTOFF - 2 days, and the selection judges validity at the cutoff day, so
# any report_date works here. This date keeps the universe id readable.
_SYNTHETIC_REPORT_DATE = (CUTOFF - timedelta(days=2)).date().isoformat()


def _synthetic_corpus() -> dict:
    """A 3-listing / 2-issuer universe that shares no dimension with TOPT (21/20)
    or QQQ (102/101). One issuer is dual-class so issuer_count < instrument_count."""
    fields = ["issuer_id", "instrument_id", "listing_id", "ticker"]
    instruments = [
        ["issuer:cik:0000000101", "security:figi:SYNTH001", "listing:xsyn:alfa", "ALFA"],
        ["issuer:cik:0000000101", "security:figi:SYNTH002", "listing:xsyn:alfb", "ALF.B"],
        ["issuer:cik:0000000202", "security:figi:SYNTH003", "listing:xsyn:brav", "BRAV"],
    ]
    return {
        "topt_denominator": {
            "universe_id": f"universe:synthetic-{_SYNTHETIC_REPORT_DATE}",
            "report_date": _SYNTHETIC_REPORT_DATE,
            "instrument_count": 3,
            "issuer_count": 2,
            "instrument_tuple_fields": fields,
            "instruments": instruments,
            "instrument_mapping_sha256": canonical_sha256({"fields": fields, "instruments": instruments}),
        }
    }


def test_a_synthetic_three_listing_universe_flows_end_to_end(connection) -> None:
    """#627: size-generality is proven by RUNNING a differently-shaped universe
    through the deployed path, not by deleting literals one hunt at a time. Five
    TOPT constants (20/21/84) survived every model test because model and test
    shared the same single-consumer premise (#609/#606/#612/0042/#621); this
    universe shares no dimension with TOPT or QQQ, so any layer that re-acquires
    a shape assumption — Python, SQL string, CHECK constraint, trigger — breaks
    here first, in CI, against the real schema."""
    (capture_repository, run, list_version, release_manifest_id, *_rest) = _seed_complete_production_run(
        connection, corpus=_synthetic_corpus()
    )
    assert capture_repository.status(run.run_id).complete
    assert list_version.universe.universe_id == f"universe:synthetic-{_SYNTHETIC_REPORT_DATE}"

    repository = PostgresToptCoreRepository(connection)
    snapshot = repository.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
    assert len(snapshot.members) == 3
    assert len({member.issuer_id for member in snapshot.members}) == 2
    # The persisted row passed 0042's self-consistency trigger (4 observations per
    # listing, issuer_count <= instrument_count) with counts that are neither 84 nor 408.
    persisted = connection.execute(
        "select issuer_count, instrument_count, observation_count from staging.topt_core_snapshots where run_id = %s",
        (run.run_id,),
    ).fetchone()
    assert persisted == (2, 3, 12)

    results = repository.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))
    assert len(results) == 2  # one row per issuer, derived from the snapshot's own membership

    graded = quality_report.build_report(connection, run.run_id)
    quality_report.persist(connection, graded)
    assert graded["complete"] is True
    assert graded["requested_count"] == 12

    # The synthetic corpus is single-origin (its parser vintage is not in the fusion
    # source map), so it registers under the corpus objectives — same relaxation the
    # governed-read test documents. The pointer key is this universe's own, so the
    # first advance is sequence 0 regardless of TOPT/QQQ state.
    registration = register_run_evidence(
        connection,
        run_id=run.run_id,
        release_manifest_id=release_manifest_id,
        quality_report=graded,
        objectives=_CORPUS_OBJECTIVES,
    )
    assert registration.accepted, registration.summary
    head = connection.execute(
        "select universe_id, sequence from mart.current_pointer_head where universe_id = %s",
        (f"universe:synthetic-{_SYNTHETIC_REPORT_DATE}",),
    ).fetchone()
    assert head == (f"universe:synthetic-{_SYNTHETIC_REPORT_DATE}", registration.sequence)


def test_every_materialized_row_carries_the_three_status_dimensions(connection) -> None:
    """#747 / init.md §8: the producer writes availability, source-evidence and validation
    status on every core and GPPE row, derived from what it consumed. On the seeded run
    every observation dereferences to a landed raw fetch, no sealed holdout exists (#65),
    and the headcount travels without its evidence (`vintage.headcount` is a follow-up), so
    a row with a headcount is `degraded` and a row without one is `verified`."""
    (
        _capture_repository,
        run,
        _list_version,
        release_manifest_id,
        *_rest,
    ) = _seed_complete_production_run(connection)
    repository = PostgresToptCoreRepository(connection)
    snapshot = repository.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
    results = repository.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))
    assert results
    core_rows = connection.execute(
        """
        select r.availability_status, r.source_evidence_status, r.factor_validation_status,
               r.availability, r.freshness, r.confidence, r.payload->>'headcount'
        from mart.topt_core_results r
        where r.result_id = any(%s)
        """,
        ([result.result_id for result in results],),
    ).fetchall()
    assert len(core_rows) == len(results)
    for availability_status, evidence, validation, availability, freshness, confidence, headcount in core_rows:
        assert availability_status in {"available", "unavailable", "stale", "excluded", "low_confidence", "error"}
        if availability == "unavailable":
            assert availability_status == "unavailable"
        elif freshness == "stale":
            assert availability_status == "stale"
        assert validation == "not_evaluated"
        assert evidence in {"verified", "degraded"}, "a seeded run never carries a dangling pointer"
    gppe_rows = connection.execute(
        """
        select availability_status, source_evidence_status, factor_validation_status
        from mart.topt_gppe_results where invocation_id = %s
        """,
        (results[0].gppe_invocation_id,),
    ).fetchall()
    assert len(gppe_rows) == len(results)
    assert {row[2] for row in gppe_rows} == {"not_evaluated"}
    assert all(row[0] and row[1] for row in gppe_rows)
    # The dimensions are not part of the identity: a repeated materialization of the same
    # snapshot returns the same ids (the existing idempotence test) and re-derives the same columns.
    repeated = repository.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))
    assert [item.result_id for item in repeated] == [item.result_id for item in results]
