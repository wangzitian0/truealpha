from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from data_engine.contracts.capture_control import CaptureObligationWorkBinding
from data_engine.contracts.datahub import (
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
from data_engine.contracts.evidence_graph import (
    BitemporalStamp,
    CurrentPointer,
    CurrentPointerKey,
    EvidenceEdge,
    EvidenceNode,
    EvidenceNodeKind,
    EvidenceNodeRef,
    EvidenceRelation,
)
from data_engine.datahub import PostgresCaptureControlRepository, expand_obligations
from data_engine.datahub.capture_core.universe_corpus import frozen_topt_list_version
from data_engine.datahub.control_plane import AttemptLedger, frozen_topt_universe, replay_retry_policy
from data_engine.datahub.evidence_graph_repository import PostgresEvidenceGraphRepository
from truealpha_contracts.common import CaptureEnvironment, canonical_sha256
from truealpha_contracts.universe import SubjectRef
from truealpha_runtime.testing import apply_migration_chain, isolated_test_database, load_tool

CORPUS = Path(__file__).parents[1] / "fixtures" / "capture_control" / "corpus.v1.json"
STARTED_AT = datetime(2026, 4, 1, 1, tzinfo=UTC)


# The three helpers below lived in `medium_replay` until #1061. Their values are unchanged.
def _schedule_policy() -> CaptureSchedulePolicy:
    return CaptureSchedulePolicy(
        policy_version="d5-medium-replay:v1",
        demanded_cadence=timedelta(days=1),
        provider_availability_cadence="fixture-daily:v1",
        freshness_max_age=timedelta(days=2),
        retry=replay_retry_policy(3),
    )


def _capture_run(
    corpus: dict[str, object], *, cutoff: datetime, sequence: int
) -> tuple[CaptureSchedulePolicy, CaptureCampaign, CaptureRun]:
    universe = frozen_topt_universe(corpus)
    schedule_policy = _schedule_policy()
    campaign = CaptureCampaign(
        campaign_policy_id="capture-policy:d5-medium-v1",
        environment=CaptureEnvironment.LOCAL_DEV,
        cutoff=cutoff,
        universe_refs=(universe,),
    )
    scope_id = f"capture-scope:{canonical_sha256({'corpus_id': corpus['corpus_id'], 'rung': 'E3'})}"
    run = CaptureRun(
        campaign_id=campaign.campaign_id,
        run_sequence=sequence,
        schedule_policy_id=schedule_policy.schedule_policy_id,
        capture_scope_id=scope_id,
    )
    return schedule_policy, campaign, run


def _source_request(
    *,
    member: SubjectRef,
    semantic_types: tuple[str, ...],
    partition: str,
) -> SourceRequest:
    requirement_ids = tuple(f"{semantic_type}:v1" for semantic_type in semantic_types)
    request_coordinate = {
        "member": member.model_dump(mode="json"),
        "requirements": requirement_ids,
        "partition": partition,
    }
    return SourceRequest(
        source_registry_entry_id=f"source-registry-entry:{canonical_sha256({'source': 'd5-medium-fixture:v1', 'semantic_types': semantic_types})}",
        source_policy_id="source-policy:d5-medium-fixture-v1",
        request_fingerprint_version="d5-medium-request:v1",
        canonical_request_sha256=canonical_sha256(request_coordinate),
        subject_refs=(member,),
        capture_requirement_ids=requirement_ids,
        partition=partition,
    )


def test_repository_persists_and_reads_terminal_capture_chain(connection) -> None:
    corpus = json.loads(CORPUS.read_text())
    list_version = frozen_topt_list_version(corpus)
    policy, campaign, run = _capture_run(corpus, cutoff=STARTED_AT, sequence=1)
    obligation = expand_obligations(
        run_id=run.run_id,
        list_version=list_version,
        semantic_types=("market-price",),
        partition="2026-03-31",
    )[0]
    request = _source_request(
        member=obligation.subject,
        semantic_types=("market-price",),
        partition=obligation.partition,
    )
    work_item = CaptureWorkItem(
        campaign_id=campaign.campaign_id,
        source_request_id=request.source_request_id,
        schedule_policy_id=policy.schedule_policy_id,
    )
    binding = CaptureObligationWorkBinding(
        obligation_id=obligation.obligation_id,
        work_item_id=work_item.work_item_id,
    )
    raw_sha256 = canonical_sha256({"close": "175.20"})
    vintage = SourceVintage(
        source_request_id=request.source_request_id,
        source_record_id="yahoo-chart:NVDA:2026-03-31",
        source_published_at=STARTED_AT,
        raw_object_id=f"raw-object:{raw_sha256}",
    )
    ledger = AttemptLedger(work_item_id=work_item.work_item_id, retry_policy=policy.retry)
    attempt = ledger.start(started_at=STARTED_AT)
    attempt_result = ledger.finish(
        attempt=attempt,
        completed_at=STARTED_AT + timedelta(seconds=1),
        outcome=FetchAttemptOutcome.SUCCESS,
        status_code=200,
        source_vintage_id=vintage.source_vintage_id,
    )
    observation = NormalizedObservation(
        semantic_type="market-price",
        semantic_version="market-price:v1",
        subject=obligation.subject,
        valid_from=STARTED_AT - timedelta(days=1),
        knowable_at=STARTED_AT,
        source_vintage_id=vintage.source_vintage_id,
        parser_version="yahoo-chart-parser:v1",
        mapping_version="topt-listing-map:v1",
        normalized_payload_sha256=canonical_sha256({"close": "175.20"}),
    )
    obligation_result = ListObligationResult(
        obligation_id=obligation.obligation.obligation_id,
        terminal_state=ObligationTerminalState.SUCCESS,
        completed_at=STARTED_AT + timedelta(seconds=2),
        final_attempt_id=attempt.attempt_id,
        reason_codes=("success",),
    )
    repository = PostgresCaptureControlRepository(connection)

    raw_fetch_id = connection.execute(
        """
        insert into raw.fetches (
            source, source_record_id, payload_sha256, object_uri, content_type,
            byte_length, fetched_at, recorded_at, metadata
        ) values (%s, %s, %s, %s, %s, %s, %s, %s, '{}'::jsonb)
        returning id
        """,
        (
            "yahoo",
            vintage.source_record_id,
            raw_sha256,
            f"s3://truealpha-raw/test/{raw_sha256}",
            "application/json",
            18,
            STARTED_AT,
            STARTED_AT,
        ),
    ).fetchone()[0]

    assert repository.put_schedule_policy(policy)
    assert repository.put_campaign(campaign)
    assert repository.put_list_version(list_version)
    repository.bind_campaign_list(campaign.campaign_id, list_version.list_version_id)
    assert repository.put_run(run)
    assert repository.put_obligation(campaign.campaign_id, obligation)
    assert repository.put_source_request(request)
    assert repository.put_work_item(work_item, policy.retry)
    assert repository.put_binding(binding)
    assert repository.put_attempt(attempt)
    assert repository.put_source_vintage(vintage, raw_fetch_id=raw_fetch_id)
    assert repository.put_attempt_result(attempt_result)
    assert repository.put_observation(
        obligation.obligation_id,
        observation,
        normalized_payload={"close": "175.20"},
        confidence=Decimal("0.95"),
        freshness_state="fresh",
    )
    assert repository.put_obligation_result(obligation.obligation_id, obligation_result)

    status = repository.status(run.run_id)
    assert (status.obligation_count, status.terminal_count, status.success_count) == (1, 1, 1)
    assert status.complete
    assert status.environment == "local_dev"

    meta_info = repository.meta_info(run.run_id)
    assert len(meta_info) == 1
    assert meta_info[0].logical_obligation_id == obligation.obligation.obligation_id
    assert meta_info[0].attempt_count == 1
    assert meta_info[0].final_status_code == 200
    assert meta_info[0].observation_id == observation.observation_id
    assert meta_info[0].reason_codes == ("success",)
    assert meta_info[0].confidence == Decimal("0.95")

    assert repository.put_schedule_policy(policy) is False
    assert repository.put_campaign(campaign) is False
    assert repository.put_list_version(list_version) is False
    assert repository.put_run(run) is False
    assert repository.put_obligation(campaign.campaign_id, obligation) is False
    assert repository.put_source_request(request) is False
    assert repository.put_work_item(work_item, policy.retry) is False
    assert repository.put_binding(binding) is False
    assert repository.put_attempt(attempt) is False
    assert repository.put_source_vintage(vintage, raw_fetch_id=raw_fetch_id) is False
    assert repository.put_attempt_result(attempt_result) is False
    assert (
        repository.put_observation(
            obligation.obligation_id,
            observation,
            normalized_payload={"close": "175.20"},
            confidence=Decimal("0.95"),
        )
        is False
    )
    with pytest.raises(ValueError, match="does not match the observation hash"):
        repository.put_observation(
            obligation.obligation_id,
            observation,
            normalized_payload={"close": "0"},
            confidence=Decimal("0.95"),
        )
    assert repository.put_obligation_result(obligation.obligation_id, obligation_result) is False

    mismatched_result = ListObligationResult(
        obligation_id="list-obligation:" + "0" * 64,
        terminal_state=ObligationTerminalState.SUCCESS,
        completed_at=STARTED_AT + timedelta(seconds=2),
        final_attempt_id=attempt.attempt_id,
        reason_codes=("success",),
    )
    with pytest.raises(ValueError, match="logical obligation does not match"):
        repository.put_obligation_result(obligation.obligation_id, mismatched_result)


def test_repository_reads_are_bounded_and_capture_rows_are_append_only(connection) -> None:
    repository = PostgresCaptureControlRepository(connection)

    with pytest.raises(ValueError, match="bounded range"):
        repository.meta_info("capture-run:" + "0" * 64, limit=501)
    with pytest.raises(ValueError, match="bounded range"):
        repository.meta_info("capture-run:" + "0" * 64, offset=-1)
    with pytest.raises(LookupError, match="capture run not found"):
        repository.status("capture-run:" + "0" * 64)

    corpus = json.loads(CORPUS.read_text())
    policy, _, _ = _capture_run(corpus, cutoff=STARTED_AT, sequence=1)
    repository.put_schedule_policy(policy)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"), connection.transaction():
        connection.execute(
            "update raw.capture_schedule_policies set policy_version = %s where schedule_policy_id = %s",
            ("mutated:v1", policy.schedule_policy_id),
        )
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"), connection.transaction():
        connection.execute(
            "delete from raw.capture_schedule_policies where schedule_policy_id = %s",
            (policy.schedule_policy_id,),
        )


def test_purge_migration_reroots_current_pointer_and_preserves_shared_fetch_nodes() -> None:
    """Verify #1815 purge removes dangling-bucket chains, re-roots pointers, and keeps shared hashes."""
    corpus = json.loads(CORPUS.read_text())
    list_version = frozen_topt_list_version(corpus)
    universe = frozen_topt_universe(corpus)
    policy = _schedule_policy()
    campaign = CaptureCampaign(
        campaign_policy_id="capture-policy:d5-medium-v1",
        environment=CaptureEnvironment.LOCAL_DEV,
        cutoff=STARTED_AT,
        universe_refs=(universe,),
    )
    scope_id = f"capture-scope:{canonical_sha256({'corpus_id': corpus['corpus_id'], 'rung': '1815'})}"
    key = CurrentPointerKey(
        environment=CaptureEnvironment.LOCAL_DEV,
        universe_id=universe.universe_id,
        universe_version=universe.universe_version,
        factor_id="gross_profit_per_employee",
    )
    stamp = BitemporalStamp(valid_from=date(2026, 3, 31), transaction_time=STARTED_AT, recorded_at=STARTED_AT)
    shared_payload = {"close": "shared"}
    shared_sha = canonical_sha256(shared_payload)
    p1 = {"close": "bad-1"}
    p2 = {"close": "good-2"}
    p3 = {"close": "bad-3"}
    p4 = {"close": "good-4"}
    run_specs = [
        (
            1,
            f"s3://legacy-bucket/a/{canonical_sha256(p1)}",
            p1,
            "2026-03-31",
            datetime(2026, 4, 1, 1, 0, 0, tzinfo=UTC),
            shared_payload,
            f"s3://legacy-bucket/shared/{shared_sha}",
        ),
        (
            2,
            f"s3://truealpha-raw/good/{canonical_sha256(p2)}",
            p2,
            "2026-04-01",
            datetime(2026, 4, 2, 1, 0, 0, 123456, tzinfo=UTC),
            shared_payload,
            f"s3://truealpha-raw/shared/{shared_sha}",
        ),
        (
            3,
            f"s3://other-bucket/c/{canonical_sha256(p3)}",
            p3,
            "2026-04-02",
            datetime(2026, 4, 3, 1, 0, 0, tzinfo=UTC),
            None,
            None,
        ),
        (
            4,
            f"s3://truealpha-raw/good/{canonical_sha256(p4)}",
            p4,
            "2026-04-03",
            datetime(2026, 4, 4, 1, 0, 0, 654321, tzinfo=UTC),
            None,
            None,
        ),
    ]

    with isolated_test_database("purge_1815", template=None) as db_url:
        runs: list[CaptureRun] = []
        bad_unshared_sha = canonical_sha256(p1)
        with psycopg.connect(db_url, autocommit=False) as conn:
            conn.execute("alter table raw.fetches drop constraint raw_fetches_bucket_check")
            conn.execute(
                "alter table raw.fetches add constraint fetches_object_uri_check check ((object_uri ~~ 's3://%'::text))"
            )
            ctrl = PostgresCaptureControlRepository(conn)
            ev = PostgresEvidenceGraphRepository(conn)
            assert ctrl.put_schedule_policy(policy)
            assert ctrl.put_campaign(campaign)
            assert ctrl.put_list_version(list_version)
            ctrl.bind_campaign_list(campaign.campaign_id, list_version.list_version_id)

            shared_node = EvidenceNode(
                ref=EvidenceNodeRef(kind=EvidenceNodeKind.RAW_FETCH, node_id=f"raw-fetch:{shared_sha}"),
                content_sha256=shared_sha,
                stamp=stamp,
            )
            ev.append([shared_node], [])

            issuer_id = conn.execute("select staging.entity_mint('issuer', 'cik', '0000320193', 'test')").fetchone()[0]
            conn.execute(
                "insert into staging.entity_aliases (entity_id, scheme, value, valid_from, transaction_time, source, raw_ref, method, confidence, mapping_version) "
                "values (%s, 'cik', '0000320193', '2026-04-01', %s, 'sec', %s, 'asserted', 0.9, 'v1')",
                (issuer_id, STARTED_AT, f"raw-fetch:{bad_unshared_sha}"),
            )

            prev_ref: EvidenceNodeRef | None = None
            for idx, (run_seq, uri, primary_payload, partition, adv_at, extra_payload, extra_uri) in enumerate(
                run_specs
            ):
                sha = canonical_sha256(primary_payload)
                run = CaptureRun(
                    campaign_id=campaign.campaign_id,
                    run_sequence=run_seq,
                    schedule_policy_id=policy.schedule_policy_id,
                    capture_scope_id=scope_id,
                )
                runs.append(run)
                assert ctrl.put_run(run)
                obligations = expand_obligations(
                    run_id=run.run_id,
                    list_version=list_version,
                    semantic_types=("market-price",),
                    partition=partition,
                )
                items = [(obligations[0], primary_payload, uri)]
                if extra_payload is not None and extra_uri is not None and len(obligations) > 1:
                    items.append((obligations[1], extra_payload, extra_uri))
                elif extra_payload is not None and extra_uri is not None:
                    items.append(
                        (
                            expand_obligations(
                                run_id=run.run_id,
                                list_version=list_version,
                                semantic_types=("fundamental-fact",),
                                partition=partition,
                            )[0],
                            extra_payload,
                            extra_uri,
                        )
                    )

                for ob_i, (ob, item_payload, item_uri) in enumerate(items):
                    item_sha = canonical_sha256(item_payload)
                    req = _source_request(
                        member=ob.subject,
                        semantic_types=(ob.obligation.capture_requirement_id.removesuffix(":v1"),),
                        partition=f"{ob.partition}:{run_seq}:{ob_i}",
                    )
                    wi = CaptureWorkItem(
                        campaign_id=campaign.campaign_id,
                        source_request_id=req.source_request_id,
                        schedule_policy_id=policy.schedule_policy_id,
                    )
                    binding = CaptureObligationWorkBinding(obligation_id=ob.obligation_id, work_item_id=wi.work_item_id)
                    vintage = SourceVintage(
                        source_request_id=req.source_request_id,
                        source_record_id=f"rec:{run_seq}:{ob_i}",
                        source_published_at=STARTED_AT,
                        raw_object_id=f"raw-object:{item_sha}",
                    )
                    ledger = AttemptLedger(work_item_id=wi.work_item_id, retry_policy=policy.retry)
                    attempt = ledger.start(started_at=STARTED_AT)
                    att_res = ledger.finish(
                        attempt=attempt,
                        completed_at=STARTED_AT + timedelta(seconds=1),
                        outcome=FetchAttemptOutcome.SUCCESS,
                        status_code=200,
                        source_vintage_id=vintage.source_vintage_id,
                    )
                    obs = NormalizedObservation(
                        semantic_type=ob.obligation.capture_requirement_id.removesuffix(":v1"),
                        semantic_version=ob.obligation.capture_requirement_id,
                        subject=ob.subject,
                        valid_from=STARTED_AT - timedelta(days=1),
                        knowable_at=STARTED_AT,
                        source_vintage_id=vintage.source_vintage_id,
                        parser_version="yahoo-chart-parser:v1",
                        mapping_version="topt-listing-map:v1",
                        normalized_payload_sha256=item_sha,
                    )
                    ob_res = ListObligationResult(
                        obligation_id=ob.obligation.obligation_id,
                        terminal_state=ObligationTerminalState.SUCCESS,
                        completed_at=STARTED_AT + timedelta(seconds=2),
                        final_attempt_id=attempt.attempt_id,
                        reason_codes=("success",),
                    )
                    fetch_id = conn.execute(
                        "insert into raw.fetches (source, source_record_id, payload_sha256, object_uri, content_type, byte_length, fetched_at, recorded_at, metadata) "
                        "values ('yahoo', %s, %s, %s, 'application/json', 10, %s, %s, '{}'::jsonb) returning id",
                        (f"rec:{run_seq}:{ob_i}", item_sha, item_uri, STARTED_AT, STARTED_AT),
                    ).fetchone()[0]
                    assert ctrl.put_obligation(campaign.campaign_id, ob)
                    assert ctrl.put_source_request(req)
                    assert ctrl.put_work_item(wi, policy.retry)
                    assert ctrl.put_binding(binding)
                    assert ctrl.put_attempt(attempt)
                    assert ctrl.put_source_vintage(vintage, raw_fetch_id=fetch_id)
                    assert ctrl.put_attempt_result(att_res)
                    assert ctrl.put_observation(
                        ob.obligation_id, obs, normalized_payload=item_payload, confidence=Decimal("0.95")
                    )
                    assert ctrl.put_obligation_result(ob.obligation_id, ob_res)

                run_node = EvidenceNode(
                    ref=EvidenceNodeRef(kind=EvidenceNodeKind.CAPTURE_RUN, node_id=run.run_id),
                    content_sha256=run.run_id.split(":", 1)[1],
                    stamp=stamp,
                )
                fetch_node = EvidenceNode(
                    ref=EvidenceNodeRef(kind=EvidenceNodeKind.RAW_FETCH, node_id=f"raw-fetch:{sha}"),
                    content_sha256=sha,
                    stamp=stamp,
                )
                edge = EvidenceEdge(
                    from_ref=run_node.ref, to_ref=fetch_node.ref, relation=EvidenceRelation.DERIVED_FROM, stamp=stamp
                )
                ev.append([run_node, fetch_node], [edge])
                ev.advance(
                    CurrentPointer(
                        key=key,
                        target_run=run_node.ref,
                        sequence=idx,
                        previous_run=prev_ref,
                        advanced_at=adv_at,
                    )
                )
                prev_ref = run_node.ref
            conn.commit()

        apply_migration_chain(db_url)
        apply_migration_chain(db_url)

        with psycopg.connect(db_url) as conn:
            assert conn.execute(
                "select count(*) from raw.fetches where object_uri !~ '^s3://truealpha-raw/.+'"
            ).fetchone() == (0,)
            remaining_runs = {r[0] for r in conn.execute("select run_id from raw.capture_runs").fetchall()}
            assert remaining_runs == {runs[1].run_id, runs[3].run_id}
            assert conn.execute(
                "select 1 from staging.evidence_nodes where node_id = %s", (f"raw-fetch:{shared_sha}",)
            ).fetchone() == (1,)
            assert (
                conn.execute(
                    "select 1 from staging.evidence_nodes where node_id = %s", (f"raw-fetch:{bad_unshared_sha}",)
                ).fetchone()
                is None
            )
            assert conn.execute(
                "select count(*) from staging.entity_aliases where raw_ref = %s", (f"raw-fetch:{bad_unshared_sha}",)
            ).fetchone() == (1,)

            cp_rows = conn.execute(
                "select pointer_id, content_sha256, target_run_id, sequence, previous_run_id, advanced_at "
                "from mart.current_pointer where environment = %s and universe_id = %s and universe_version = %s and factor_id = %s order by sequence",
                (key.environment.value, key.universe_id, key.universe_version, key.factor_id),
            ).fetchall()
            expected_0 = CurrentPointer(
                key=key,
                target_run=EvidenceNodeRef(kind=EvidenceNodeKind.CAPTURE_RUN, node_id=runs[1].run_id),
                sequence=0,
                previous_run=None,
                advanced_at=run_specs[1][4],
            )
            expected_1 = CurrentPointer(
                key=key,
                target_run=EvidenceNodeRef(kind=EvidenceNodeKind.CAPTURE_RUN, node_id=runs[3].run_id),
                sequence=1,
                previous_run=expected_0.target_run,
                advanced_at=run_specs[3][4],
            )
            assert [(r[0], r[1], r[2], r[3], r[4]) for r in cp_rows] == [
                (expected_0.pointer_id, expected_0.content_sha256, runs[1].run_id, 0, None),
                (expected_1.pointer_id, expected_1.content_sha256, runs[3].run_id, 1, runs[1].run_id),
            ]
            head = PostgresEvidenceGraphRepository(conn).head(key)
            assert head is not None and head == expected_1

            with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
                conn.execute(
                    "insert into raw.fetches "
                    "(source, source_record_id, payload_sha256, object_uri, content_type, byte_length, fetched_at) "
                    "values ('sec', 'bad-bucket-check', %s, 's3://wrong-bucket/x', 'application/json', 1, %s)",
                    ("f" * 64, STARTED_AT),
                )

        drift = load_tool("schema_drift")
        with drift.reference_database(db_url) as reference_url:
            report, _ = drift.compare(db_url, reference_url)
        assert report == [], report
