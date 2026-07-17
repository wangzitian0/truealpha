from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.control_plane import AttemptLedger, expand_obligations, replay_retry_policy
from data_engine.datahub.medium_replay import frozen_topt_list_version
from data_engine.datahub.production_topt import PostgresToptCoreRepository, ToptCoreIdentity
from data_engine.datahub.repository import PostgresCaptureControlRepository
from factors.production_topt import GppeV0Definition, ToptCoreAvailability
from truealpha_contracts import CaptureEnvironment, canonical_sha256
from truealpha_contracts.capture_control import CaptureObligationWorkBinding
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

CORPUS = Path(__file__).parents[1] / "fixtures" / "capture_control" / "corpus.v1.json"
CUTOFF = datetime(2026, 4, 2, tzinfo=UTC)
SEMANTIC_TYPES = ("market-price", "listing-identity", "universe-membership", "financial-fact")


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
) -> dict[str, str | None]:
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
        return {
            **identity,
            "operating_branch": "financial" if financial else "non_financial",
            "currency": "USD",
            "gross_profit": None if financial else "210000000",
            "total_assets": None if financial else "200000000",
            "headcount": "100",
            "revenue": None if financial else "100000000",
            "shares_outstanding": "10000000",
            "pre_provision_profit": "80000000" if financial else None,
        }
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
):
    corpus = json.loads(CORPUS.read_text())
    denominator = corpus["topt_denominator"]
    coordinates = {row[2]: tuple(row) for row in denominator["instruments"]}
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
        environment=CaptureEnvironment.PRODUCTION,
        cutoff=CUTOFF,
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
        normalized_payload = _normalized_payload(coordinates[obligation.subject.id], semantic_type)
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
                CUTOFF - timedelta(hours=2),
                CUTOFF - timedelta(hours=2),
            ),
        ).fetchone()[0]
        vintage = SourceVintage(
            source_request_id=request.source_request_id,
            source_record_id=source_record_id,
            source_published_at=CUTOFF - timedelta(hours=2),
            raw_object_id=f"raw-object:{raw_sha256}",
        )
        ledger = AttemptLedger(work_item_id=work_item.work_item_id, retry_policy=policy.retry)
        attempt = ledger.start(started_at=CUTOFF - timedelta(hours=1))
        unchanged = stale_unchanged_first_observation and ordinal == 0
        attempt_result = ledger.finish(
            attempt=attempt,
            completed_at=CUTOFF - timedelta(minutes=59),
            outcome=FetchAttemptOutcome.UNCHANGED if unchanged else FetchAttemptOutcome.SUCCESS,
            status_code=200,
            source_vintage_id=None if unchanged else vintage.source_vintage_id,
            reused_source_vintage_id=vintage.source_vintage_id if unchanged else None,
        )
        observation = NormalizedObservation(
            semantic_type=semantic_type,
            semantic_version=obligation.capture_requirement_id,
            subject=obligation.subject,
            valid_from=CUTOFF - timedelta(days=2),
            valid_to=CUTOFF - timedelta(days=2),
            knowable_at=CUTOFF - (timedelta(days=3) if unchanged else timedelta(minutes=58)),
            source_vintage_id=vintage.source_vintage_id,
            parser_version="production-topt-integration-parser:v1",
            mapping_version="production-topt-integration-map:v1",
            normalized_payload_sha256=canonical_sha256(normalized_payload),
        )
        terminal = ListObligationResult(
            obligation_id=obligation.obligation.obligation_id,
            terminal_state=(ObligationTerminalState.UNCHANGED if unchanged else ObligationTerminalState.SUCCESS),
            completed_at=CUTOFF - timedelta(minutes=57),
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
                valid_from=CUTOFF + timedelta(days=1),
                knowable_at=CUTOFF - timedelta(minutes=30),
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
                    source_published_at=CUTOFF - timedelta(hours=2),
                    raw_object_id=f"raw-object:{raw_sha256}",
                )
                repository.put_source_vintage(tied_vintage, raw_fetch_id=raw_fetch_id)
                tied_observations.append(
                    NormalizedObservation(
                        semantic_type=semantic_type,
                        semantic_version=obligation.capture_requirement_id,
                        subject=obligation.subject,
                        valid_from=CUTOFF - timedelta(days=2),
                        valid_to=CUTOFF - timedelta(days=2),
                        knowable_at=CUTOFF - timedelta(minutes=30),
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
                source_published_at=CUTOFF - timedelta(hours=2),
                raw_object_id=f"raw-object:{raw_sha256}",
            )
            repository.put_source_vintage(foreign_vintage, raw_fetch_id=raw_fetch_id)
            foreign_observation = NormalizedObservation(
                semantic_type=semantic_type,
                semantic_version=obligation.capture_requirement_id,
                subject=obligation.subject,
                valid_from=CUTOFF - timedelta(days=2),
                valid_to=CUTOFF - timedelta(days=2),
                knowable_at=CUTOFF - timedelta(minutes=10),
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
    assert sum(item.availability is ToptCoreAvailability.AVAILABLE for item in results) == 19
    assert {item.gppe for item in results if item.gppe is not None} == {Decimal("2000000")}
    alphabet = next(item for item in results if item.issuer_id == "issuer:lei:5493006MHB84DD0ZWV18")
    assert alphabet.current_ps == Decimal("8")
    financial = next(item for item in results if item.issuer_id == "issuer:lei:8I5DZWZKVSZI1NUHU748")
    assert financial.availability is ToptCoreAvailability.UNAVAILABLE
    assert financial.operating_efficiency == Decimal("800000")
    assert tuple(item.value for item in financial.reason_codes) == ("financial_valuation_not_comparable",)
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
