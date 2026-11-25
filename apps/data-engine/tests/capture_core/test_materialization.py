from __future__ import annotations

import dataclasses
import json
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from data_engine.config import settings
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
from data_engine.contracts.evidence_graph import BitemporalStamp, EvidenceNode, EvidenceNodeKind, EvidenceNodeRef
from data_engine.datahub import quality_report
from data_engine.datahub.a1_evidence import (
    ACCEPTED_SERVICE_OBJECTIVES,
    ServiceObjectives,
    register_run_evidence,
)
from data_engine.datahub.capture_core import PostgresCoreRepository
from data_engine.datahub.capture_core.materialization import _ObservationRow
from data_engine.datahub.capture_core.universe_corpus import corpus_list_version, frozen_topt_list_version
from data_engine.datahub.control_plane import AttemptLedger, expand_obligations, replay_retry_policy
from data_engine.datahub.evidence_graph_repository import PostgresEvidenceGraphRepository
from data_engine.datahub.repository import PostgresCaptureControlRepository
from data_engine.datahub.resolve_coordinates import resolve_coordinates
from data_engine.datahub.strategy_bridge import run_strategy_replay_for_cutoff, seed_strategy_inputs_from_capture
from factors.snapshot_inputs import CoreReasonCode, GppeV0Definition, MetricAvailability, MetricFreshness
from psycopg.types.json import Jsonb
from truealpha_contracts.common import CaptureEnvironment, canonical_sha256
from truealpha_runtime.access import AccessContext, AuthenticationMethod, PrincipalKind
from truealpha_runtime.research_report_mart import (
    MetricNodeValueReport,
    MetricNodeValueUnavailable,
    PostgresMetricNodeValueRepository,
)
from truealpha_runtime.strategy_run import StrategyRunReport
from truealpha_runtime.strategy_run_postgres import PostgresStrategyRunRepository

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "support"))
from macro_seed import stored_rate_for_cutoff  # noqa: E402
from split_series_seed import flat_history, share_count_vintage, store_flat_daily_series  # noqa: E402
from topt_symbols import TOPT_SYMBOLS  # noqa: E402

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
    minimum_independent_origin_groups=0,
    minimum_corroborated_share=Decimal("0"),
)


def _normalized_payload(
    coordinates: tuple[str, str, str, str],
    semantic_type: str,
    *,
    financial_vintage: dict[str, Any] | None = None,
    gross_profit_none_tickers: frozenset[str] = frozenset(),
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
        # (the SEC financial-fact adapter provides them). gross_profit stays None for a
        # bank (it reports pre-provision profit as its industry-branch numerator).
        payload: dict[str, Any] = {
            **identity,
            "operating_branch": "financial" if financial else "non_financial",
            "currency": "USD",
            "gross_profit": None if (financial or ticker in gross_profit_none_tickers) else "210000000",
            "total_assets": "200000000",
            "headcount": "100",
            "revenue": "100000000",
            "shares_outstanding": "10000000",
            "listing_units_per_share": "1",
            "pre_provision_profit": "80000000" if financial else None,
            # #1518: the bank's four banking leaves. TCE = 50M - 5M - (3M + 2M) = 40M, a fifth of its assets.
            "stockholders_equity": "50000000" if financial else None,
            "preferred_stock_value": "5000000" if financial else None,
            "goodwill": "3000000" if financial else None,
            "intangible_assets_net_excluding_goodwill": "2000000" if financial else None,
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
    declare_primary_listings: bool = True,
    gross_profit_none_tickers: frozenset[str] = frozenset(),
):
    """Seed one complete run in the given capture environment.

    A semantic type that `valid_from_by_semantic` omits gets valid_from = cutoff - 2 days.
    A semantic type that `valid_to_by_semantic` omits gets an open valid_to.
    The capture sink writes an open valid_to only.
    A `financial_vintage` is written into every financial-fact payload as its `vintage`.
    `declare_primary_listings` resolves the issuers with several listings, as `plan_and_persist` does before
    a capture. The resolver then writes their `primary_listing` rows (#1518). With False the store holds
    no entity, and such an issuer has no primary listing.
    """
    valid_from_by_semantic = valid_from_by_semantic or {}
    valid_to_by_semantic = valid_to_by_semantic or {}
    corpus = corpus if corpus is not None else json.loads(CORPUS.read_text())
    denominator = corpus["denominator"]
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
        insert into raw.capture_run_plans (
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
            coordinates[obligation.subject.id],
            semantic_type,
            financial_vintage=financial_vintage,
            gross_profit_none_tickers=gross_profit_none_tickers,
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
                f"s3://truealpha-raw/production-topt-integration/{raw_sha256}",
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
    if declare_primary_listings:
        by_issuer: dict[str, list[list[str]]] = {}
        for row in denominator["instruments"]:
            by_issuer.setdefault(str(row[0]), []).append(row)
        resolve_coordinates(
            connection,
            [row for rows in by_issuer.values() if len(rows) > 1 for row in rows],
            as_of=cutoff.date(),
            known_at=cutoff,
        )
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
    ) = _seed_strategy_capture(connection)
    assert capture_repository.status(run.run_id).complete

    repository = PostgresCoreRepository(connection)
    snapshot = repository.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
    selected_observation_ids = {
        observation_id for member in snapshot.members for observation_id in member.observation_ids
    }
    assert terminal_observation_id in selected_observation_ids
    assert selected_observation_ids.isdisjoint(unselected_same_request_observation_ids)
    assert foreign_observation_id not in selected_observation_ids
    assert connection.execute(
        """
        select observation_id from mart.capture_meta_info
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
    # The bank is 780,000 on its measured TCE (#1518); on total assets it would be 700,000.
    non_fin_rows = connection.execute(
        """
        select issuer_id, value, availability_status, reason_codes
        from mart.metric_node_values
        where run_id = %s and node_key = 'gppe_uniform_charge_v0'
        """,
        (run.run_id,),
    ).fetchall()
    bank_rows = connection.execute(
        """
        select issuer_id, node_key, value, availability_status, reason_codes
        from mart.metric_node_values
        where run_id = %s and node_key in ('gppe_banking_tce_v1', 'capital_adjusted_tce')
        """,
        (run.run_id,),
    ).fetchall()
    assert len(non_fin_rows) == 20
    assert {row[1] for row in non_fin_rows} == {Decimal("2000000"), Decimal("700000")}
    assert {row[2] for row in non_fin_rows} == {"available"}
    assert {(row[0], row[1]): (row[2], row[3], row[4]) for row in bank_rows} == {
        ("issuer:lei:8I5DZWZKVSZI1NUHU748", "gppe_banking_tce_v1"): (Decimal("780000"), "available", []),
        ("issuer:lei:8I5DZWZKVSZI1NUHU748", "capital_adjusted_tce"): (Decimal("78000000"), "available", []),
    }
    alphabet = next(item for item in results if item.issuer_id == "issuer:lei:5493006MHB84DD0ZWV18")
    # #705: dual-listed issuer values at the execution listing's price: 40 * 10M / 100M = 4.
    alphabet_mv = (
        alphabet.market_value_components[0].market_price.value
        * alphabet.market_value_components[0].shares_outstanding.value
    )
    assert alphabet_mv / alphabet.revenue.value == Decimal("4")
    single_class = next(
        item
        for item in results
        if item.issuer_id != alphabet.issuer_id
        and item.market_value_components[0].market_price.value is not None
        and item.market_value_components[0].shares_outstanding.value is not None
        and item.revenue is not None
        and item.revenue.value is not None
    )
    single_mv = (
        single_class.market_value_components[0].market_price.value
        * single_class.market_value_components[0].shares_outstanding.value
    )
    assert alphabet_mv / alphabet.revenue.value == single_mv / single_class.revenue.value
    lineage_rows = connection.execute(
        "select lineage from mart.capture_issuer_lineage where run_id = %s",
        (run.run_id,),
    ).fetchall()
    assert sorted(len(row[0]) for row in lineage_rows) == [4] * 19 + [8]

    assert GppeV0Definition(risk_free_rate="0.04") != GppeV0Definition(risk_free_rate="0.05")
    assert connection.execute("select count(*) from staging.capture_observation_payloads").fetchone() == (89,)
    assert connection.execute("select count(*) from staging.core_snapshot_members").fetchone() == (20,)

    with pytest.raises(psycopg.errors.RaiseException, match="append-only"), connection.transaction():
        connection.execute(
            "update mart.metric_node_values set confidence = 0 where run_id = %s and node_key = 'gppe_uniform_charge_v0'",
            (run.run_id,),
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

    snapshot = PostgresCoreRepository(connection).freeze_snapshot(
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
        select observation_id, freshness_state from mart.capture_meta_info
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

    snapshot = PostgresCoreRepository(connection).freeze_snapshot(
        run_id=run.run_id, release_manifest_id=release_manifest_id
    )

    assert len(snapshot.members) == 21


@pytest.mark.parametrize("semantic_type", SEMANTIC_TYPES)
def test_freeze_refuses_a_fact_that_starts_after_the_cutoff_date(connection, semantic_type: str) -> None:
    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(
        connection, valid_from_by_semantic={semantic_type: CUTOFF + timedelta(days=1)}
    )

    with pytest.raises(ValueError, match=_NO_PAYLOAD_PER_OBLIGATION):
        PostgresCoreRepository(connection).freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)


@pytest.mark.parametrize("semantic_type", SEMANTIC_TYPES)
def test_freeze_refuses_a_fact_that_ended_before_the_cutoff_date(connection, semantic_type: str) -> None:
    # valid_to is after the anchor (2026-03-31) and before the cutoff date (2026-04-02).
    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(
        connection, valid_to_by_semantic={semantic_type: CUTOFF - timedelta(days=1)}
    )

    with pytest.raises(ValueError, match=_NO_PAYLOAD_PER_OBLIGATION):
        PostgresCoreRepository(connection).freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)


def _exposed_by_semantic(connection, run_id: str) -> dict[str, int]:
    """The number of obligations per semantic type for which the view exposes an observation."""
    rows = connection.execute(
        """
        select regexp_replace(capture_requirement_id, ':v1$', ''), count(observation_id)
        from mart.capture_meta_info
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
        "select observation_id from mart.capture_meta_info where obligation_id = %s", (obligation_id,)
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

    snapshot = PostgresCoreRepository(connection).freeze_snapshot(
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
        PostgresCoreRepository(connection).freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
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
        join mart.capture_meta_info meta using (observation_id)
        where observation.observation_id = %s
        """,
        (terminal_observation_id,),
    ).fetchone()
    assert stored == "fresh"
    assert projected == "stale"

    repository = PostgresCoreRepository(connection)
    snapshot = repository.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
    selected_cells = [
        cell for member in snapshot.members for cell in member.cell_inputs if cell.input_id == terminal_observation_id
    ]
    assert len(selected_cells) == 1
    assert selected_cells[0].freshness.value == "stale"
    results = repository.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))
    affected = next(result for result in results if terminal_observation_id in result.observation_ids)
    status, reasons = connection.execute(
        """
        select availability_status, reason_codes
        from mart.metric_node_values
        where run_id = %s and issuer_id = %s and node_key = 'gppe_uniform_charge_v0'
        """,
        (run.run_id, affected.issuer_id),
    ).fetchone()
    assert status == "unavailable"
    assert tuple(reasons) == ("stale_input",)

    # #530: the quality report grades the same run from the same table this test just
    # proved is stale (mart.capture_meta_info); it must not fall back to the
    # frozen capture_normalized_observations.freshness_state and call the cell fresh.
    graded = quality_report.build_report(connection, run.run_id)
    live_fresh_count = connection.execute(
        "select count(*) from mart.capture_meta_info where run_id = %s and freshness_state = 'fresh'",
        (run.run_id,),
    ).fetchone()[0]
    assert graded["fresh_count"] == live_fresh_count


def test_mart_freshness_state_takes_more_than_one_value_on_a_run_with_a_stale_cell(connection) -> None:
    """#530 acceptance, mart side: the served freshness is not a constant.

    The seeded run holds 84 selected observations. One reused observation is older than
    its window at the cutoff. The mart view grades that row stale and the other 83 fresh.
    A view that returns one literal for every row fails this test.
    """
    (_, run, *_rest) = _seed_complete_production_run(connection, stale_unchanged_first_observation=True)

    distinct_count, states = connection.execute(
        """
        select count(distinct freshness_state), array_agg(distinct freshness_state order by freshness_state)
        from mart.capture_meta_info
        where run_id = %s and observation_id is not null
        """,
        (run.run_id,),
    ).fetchone()
    assert distinct_count > 1, f"mart freshness_state is a constant across the run: {states}"
    assert states == ["fresh", "stale"]


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
        PostgresCoreRepository(connection).freeze_snapshot(
            run_id=run.run_id,
            release_manifest_id=release_manifest_id,
        )
    assert connection.execute(
        """
        select observation_id from mart.capture_meta_info
        where run_id = %s and obligation_id = %s
        """,
        (run.run_id, obligation_id),
    ).fetchall() == [(None,)]


def test_snapshot_rejects_unknown_run(connection) -> None:
    repository = PostgresCoreRepository(connection)
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
        PostgresCoreRepository._snapshot_member("listing:probe", by_type, histories=(flat_history(),))


def test_a_run_whose_listing_cells_are_unbalanced_but_sum_to_the_obligations_is_refused(
    connection, monkeypatch
) -> None:
    """#1061: one listing holds three cells and another holds five. The row count equals the
    obligation count. The sum `listings * 4 == obligations` holds. The removed run-wide
    equality passed this input. The member builder refuses it, and nothing is stored."""
    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(connection)
    repository = PostgresCoreRepository(connection)
    rows = list(repository._load_observations(run.run_id, cutoff=CUTOFF))
    assert len(rows) == 84
    donor, receiver = rows[0], rows[-1]
    assert donor.listing_id != receiver.listing_id
    moved = dataclasses.replace(donor, listing_id=receiver.listing_id, semantic_type="extra-cell")
    unbalanced = tuple([moved, *rows[1:]])
    assert len(unbalanced) == 84
    monkeypatch.setattr(PostgresCoreRepository, "_load_observations", lambda self, run_id, *, cutoff: unbalanced)

    with pytest.raises(ValueError, match="does not have the exact four TOPT semantic cells"):
        repository.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
    assert connection.execute(
        "select count(*) from staging.core_snapshots where run_id = %s", (run.run_id,)
    ).fetchone() == (0,)


def _insert_snapshot_row(connection, run_id: str, release_manifest_id: str, *, instruments: int, observations: int):
    """A hand-written snapshot row. Every column except the counts satisfies the snapshot
    trigger. So a refusal names the count rule, not an unrelated column."""
    universe_id, universe_version, universe_sha256, cutoff = connection.execute(
        "select universe_id, universe_version, universe_sha256, cutoff from mart.capture_status where run_id = %s",
        (run_id,),
    ).fetchone()
    payload = {"probe": run_id, "instruments": instruments}
    digest = connection.execute("select raw.canonical_sha256(%s::jsonb)", (Jsonb(payload),)).fetchone()[0]
    return connection.execute(
        """
        insert into staging.core_snapshots (
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
    assert refused.value.diag.constraint_name == "core_snapshots_observation_count_check"

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
    snapshot = PostgresCoreRepository(connection).freeze_snapshot(
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
            insert into staging.core_snapshot_members (
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
        "select success_count + unchanged_count, obligation_count from mart.capture_status where run_id = %s",
        (run_id,),
    ).fetchone() == (83, 84)


@pytest.mark.parametrize("how", _NOT_SUCCESSFUL)
def test_freeze_refuses_a_run_with_one_obligation_that_did_not_succeed(connection, how: str) -> None:
    """#1061: `success + unchanged == obligations` is the one status condition freeze keeps.
    It leaves no room for an unavailable, skipped or failed obligation, or for a gap."""
    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(connection)
    _spoil_one_result(connection, run.run_id, how)

    with pytest.raises(ValueError, match="core snapshot requires a completely successful run"):
        PostgresCoreRepository(connection).freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
    assert connection.execute(
        "select count(*) from staging.core_snapshots where run_id = %s", (run.run_id,)
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
        from mart.capture_status where run_id = %s
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

    monkeypatch.setattr(PostgresCoreRepository, "_load_observations", load)
    return loaded


def _snapshot_count(connection, run_id: str) -> tuple[int]:
    return connection.execute("select count(*) from staging.core_snapshots where run_id = %s", (run_id,)).fetchone()


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
            PostgresCoreRepository(connection).freeze_snapshot(
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
    repository = PostgresCoreRepository(connection)
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
    connection: `PostgresCoreRepository` connects per call, but the governed
    read must see the same uncommitted rows the real writers just produced, and
    committing the deterministic corpus identity would break the first-insert
    assertions other tests make against a shared database. Every SQL statement
    still executes against the real schema; only connection acquisition is
    redirected, rows are shaped per cursor (dict_row, the repository's own row
    factory), and close/commit are withheld so the test's rollback owns the
    data's lifetime. This is the same seam _topt_read_support.py fakes — here the
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
    """#462: the governed reader (`PostgresMetricNodeValueRepository`) reads real
    materialized rows through the governed pointer, against the real schema —
    seeded by the real writers, never skipped for lack of data.
    """
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: _BorrowedConnection(connection))
    reader = PostgresMetricNodeValueRepository(database_url=settings.database_url)

    connection.execute("set session_replication_role = replica")
    connection.execute(
        "delete from mart.current_pointer where environment = 'production' and factor_id = %s",
        ("gross_profit_per_employee",),
    )
    connection.execute("set session_replication_role = origin")

    before = reader.latest(node_key="gppe_uniform_charge_v0")
    fresh_database = isinstance(before, MetricNodeValueUnavailable)
    if fresh_database:
        assert before.reason == "no served head for strategy"

    _repository, run, _list_version, release_manifest_id, *_ = _seed_complete_production_run(connection)
    core = PostgresCoreRepository(connection)
    snapshot = core.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
    results = core.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))
    assert len(results) == 20

    if fresh_database:
        withheld = reader.latest(node_key="gppe_uniform_charge_v0")
        assert isinstance(withheld, MetricNodeValueUnavailable)
        assert withheld.reason == "no served head for strategy"

    graded = quality_report.build_report(connection, run.run_id)
    quality_report.persist(connection, graded)

    if fresh_database:
        still_withheld = reader.latest(node_key="gppe_uniform_charge_v0")
        assert isinstance(still_withheld, MetricNodeValueUnavailable)
        assert still_withheld.reason == "no served head for strategy"

    registration = register_run_evidence(
        connection,
        run_id=run.run_id,
        release_manifest_id=release_manifest_id,
        quality_report=graded,
        objectives=_CORPUS_OBJECTIVES,
    )
    assert registration.accepted, registration.summary
    assert registration.sequence is not None and registration.sequence >= 0

    connection.execute("set session_replication_role = replica")
    connection.execute(
        "update mart.current_pointer set advanced_at = %s where target_run_id = %s",
        (datetime.now(UTC), run.run_id),
    )
    connection.execute("set session_replication_role = origin")

    served = reader.latest(node_key="gppe_uniform_charge_v0")
    assert isinstance(served, MetricNodeValueReport), f"expected a report after the pointer advance, got {served!r}"
    assert served.run_id == run.run_id
    assert len(served.cells) >= 19
    assert served.available_count >= 19


def _seed_strategy_capture(connection):
    """A complete run that the strategy seed can value (#1262).

    Each financial fact dates its share count, and both stored daily series of every symbol
    reach the close's session. The series show no split, so each count stays as filed.
    """
    seeded = _seed_complete_production_run(
        connection,
        financial_vintage={
            "shares_outstanding": share_count_vintage(filed=date(2026, 3, 2), own_date=date(2026, 2, 20))
        },
    )
    store_flat_daily_series(connection, TOPT_SYMBOLS, first=date(2026, 1, 2), last=CUTOFF.date())
    return seeded


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
    connection.execute("set session_replication_role = replica")
    connection.execute("delete from mart.strategy_runs where strategy_key = 'large_model_value_v0'")
    connection.execute("set session_replication_role = origin")
    _repository, run, _list_version, _release_manifest_id, *_ = _seed_strategy_capture(connection)

    written = seed_strategy_inputs_from_capture(
        connection, run.run_id, cutoff=CUTOFF, parser_version="production-topt-integration-parser:v1"
    )
    # 19 non-financial issuers × 5 financial keys + JPM's 5 (with pre_provision_profit under its own metric, #1519 step 6)
    # + JPM's 4 banking leaves (#1518) + 20 last_close rows.
    assert written == 19 * 5 + 5 + 4 + 20

    executed_at = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)
    run_id, decision_count, snapshot_id = run_strategy_replay_for_cutoff(
        connection,
        cutoff=CUTOFF,
        executed_at=executed_at,
        risk_free_rate=stored_rate_for_cutoff(connection, CUTOFF, "0.05"),
        capture_run_id=run.run_id,
        plausibility_refusals={},
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
    assert jpm.eligible is True and jpm.exclusion_reason is None
    assert jpm.labor_efficiency_metric == "gppe_banking_tce_v1"
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
    connection.execute("set session_replication_role = replica")
    connection.execute("delete from mart.strategy_runs where strategy_key = 'large_model_value_v0'")
    connection.execute("set session_replication_role = origin")
    _repository, run, _list_version, _release_manifest_id, *_ = _seed_strategy_capture(connection)
    seed_strategy_inputs_from_capture(
        connection, run.run_id, cutoff=CUTOFF, parser_version="production-topt-integration-parser:v1"
    )
    first_executed = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)
    run_strategy_replay_for_cutoff(
        connection,
        cutoff=CUTOFF,
        executed_at=first_executed,
        risk_free_rate=stored_rate_for_cutoff(connection, CUTOFF, "0.05"),
        capture_run_id=run.run_id,
        plausibility_refusals={},
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
        risk_free_rate=stored_rate_for_cutoff(connection, CUTOFF, "0.05"),
        capture_run_id=run.run_id,
        plausibility_refusals={},
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
        "denominator": {
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

    repository = PostgresCoreRepository(connection)
    snapshot = repository.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
    assert len(snapshot.members) == 3
    assert len({member.issuer_id for member in snapshot.members}) == 2
    # The persisted row passed 0042's self-consistency trigger (4 observations per
    # listing, issuer_count <= instrument_count) with counts that are neither 84 nor 408.
    persisted = connection.execute(
        "select issuer_count, instrument_count, observation_count from staging.core_snapshots where run_id = %s",
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
    repository = PostgresCoreRepository(connection)
    snapshot = repository.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
    results = repository.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))
    assert results
    node_rows = connection.execute(
        """
        select m.availability_status, m.source_evidence_status, m.factor_validation_status,
               m.value, m.confidence
        from mart.metric_node_values m
        where m.run_id = %s and m.node_key = 'gppe_uniform_charge_v0'
        """,
        (run.run_id,),
    ).fetchall()
    assert len(node_rows) == len(results)
    for availability_status, evidence, validation, value, confidence in node_rows:
        assert availability_status in {
            "available",
            "unavailable",
            "stale",
            "excluded",
            "low_confidence",
            "not_applicable",
            "error",
        }
        assert validation == "not_evaluated"
        assert evidence in {"verified", "degraded"}, "a seeded run never carries a dangling pointer"
        if availability_status == "available":
            assert value is not None and confidence is not None and confidence >= Decimal("0.5")
    # The dimensions are not part of the identity: a repeated materialization of the same
    # snapshot returns the same ids (the existing idempotence test) and re-derives the same columns.
    repeated = repository.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))
    assert [item.issuer_id for item in repeated] == [item.issuer_id for item in results]


def _member_rows_for_probe(
    *,
    confidence: Decimal = Decimal("0.92"),
    shares_outstanding: str = "10000000",
    shares_period_end: str | None = "2026-01-20",
    financial_vintage: dict[str, Any] | None = None,
) -> dict[str, _ObservationRow]:
    coordinates = ("issuer:cik:0001045810", "security:figi:NVDA", "listing:xnas:nvda", "NVDA")
    financial_payload = _normalized_payload(coordinates, "financial-fact", financial_vintage=financial_vintage)
    financial_payload["shares_outstanding"] = shares_outstanding
    if shares_period_end is not None:
        financial_payload["shares_period_end"] = shares_period_end
    rows: dict[str, _ObservationRow] = {}
    for ordinal, semantic_type in enumerate(SEMANTIC_TYPES):
        payload = (
            financial_payload if semantic_type == "financial-fact" else _normalized_payload(coordinates, semantic_type)
        )
        knowable_at = (
            datetime(2026, 2, 1, tzinfo=UTC) if semantic_type == "financial-fact" else datetime(2026, 3, 31, tzinfo=UTC)
        )
        rows[semantic_type] = _ObservationRow(
            obligation_id=f"capture-list-obligation:{ordinal + 10:064x}",
            listing_id=coordinates[2],
            semantic_type=semantic_type,
            observation_id=f"normalized-observation:{ordinal + 10:064x}",
            confidence=confidence,
            freshness=MetricFreshness.FRESH,
            knowable_at=knowable_at,
            payload=payload,
        )
    return rows


def test_snapshot_member_preserves_financial_confidence_without_revenue_proxy_penalty() -> None:
    """#1519 step 6: the revenue proxy on `gross_profit` is removed, so `_snapshot_member`
    passes through the observation's continuous confidence and `FinancialFactPayload` rejects
    undeclared keys via `extra="forbid"`."""
    control_rows = _member_rows_for_probe(
        confidence=Decimal("0.92"),
        financial_vintage={"gross_profit": None},
    )
    control_member = PostgresCoreRepository._snapshot_member(
        "listing:xnas:nvda", control_rows, histories=(flat_history(),)
    )
    assert control_member.gross_profit is not None
    assert control_member.gross_profit.confidence == Decimal("0.92")
    assert {cell.confidence for cell in control_member.cell_inputs} == {Decimal("0.92")}

    from pydantic import ValidationError

    duplicate_key_rows = _member_rows_for_probe(confidence=Decimal("0.92"))
    duplicate_key_rows["financial-fact"].payload["undeclared_extra_key"] = True
    with pytest.raises(ValidationError):
        PostgresCoreRepository._snapshot_member("listing:xnas:nvda", duplicate_key_rows, histories=(flat_history(),))


def test_snapshot_member_drops_stale_operating_or_revenue_period_with_stale_period_reason() -> None:
    """#1519 step 7: a restated 2018 period filed in 2026 has a fresh `knowable_at` but a
    `period.end` older than `maximum_input_age_days`, so `_snapshot_member` drops the metric
    to `None` and records `CoreReasonCode.STALE_PERIOD`."""
    from data_engine.datahub.capture_core.materialization import InputAgeLimit

    input_age = InputAgeLimit(
        cutoff=CUTOFF,
        maximum_age=timedelta(days=455),
        deciding={"general": frozenset({"gross_profit", "total_assets", "headcount", "revenue"})},
    )
    stale_rows = _member_rows_for_probe(confidence=Decimal("0.92"))
    stale_rows["financial-fact"].payload["operating_period_end"] = "2018-12-31"
    stale_rows["financial-fact"].payload["revenue_period_end"] = "2025-12-31"
    stale_member = PostgresCoreRepository._snapshot_member(
        "listing:xnas:nvda", stale_rows, histories=(flat_history(),), input_age=input_age
    )
    assert stale_member.gross_profit is not None
    assert stale_member.gross_profit.value is None
    assert stale_member.gross_profit.unavailable_reason is CoreReasonCode.STALE_PERIOD
    assert stale_member.revenue is not None
    assert stale_member.revenue.value is not None
    assert stale_member.revenue.unavailable_reason is None

    fresh_rows = _member_rows_for_probe(confidence=Decimal("0.92"))
    fresh_rows["financial-fact"].payload["operating_period_end"] = "2025-12-31"
    fresh_rows["financial-fact"].payload["revenue_period_end"] = "2025-12-31"
    fresh_member = PostgresCoreRepository._snapshot_member(
        "listing:xnas:nvda", fresh_rows, histories=(flat_history(),), input_age=input_age
    )
    assert fresh_member.gross_profit is not None
    assert fresh_member.gross_profit.value is not None
    assert fresh_member.gross_profit.unavailable_reason is None


def test_snapshot_member_accepts_listing_reused_price_with_foreign_issuer_id() -> None:
    """#1333 / #1352: `market-price` is reused across universes by `(listing, session_date)`,
    so a price payload carrying a foreign `issuer_id` / `instrument_id` (e.g., LEI vs CIK)
    must materialize cleanly under the target run's `listing.issuer_id` as long as
    `price.listing_id == listing.listing_id`. Mismatched `price.listing_id` or mismatched
    `financial.issuer_id` must still raise `ValueError`."""
    reused_price_rows = _member_rows_for_probe()
    reused_price_rows["market-price"].payload["issuer_id"] = "issuer:lei:549300S4KLFTLO7GSQ80"
    reused_price_rows["market-price"].payload["instrument_id"] = "instrument:us67066g1040"
    member = PostgresCoreRepository._snapshot_member(
        "listing:xnas:nvda", reused_price_rows, histories=(flat_history(),)
    )
    assert member.issuer_id == "issuer:cik:0001045810"
    assert member.listing_id == "listing:xnas:nvda"

    wrong_price_listing_rows = _member_rows_for_probe()
    wrong_price_listing_rows["market-price"].payload["listing_id"] = "listing:xnas:aapl"
    with pytest.raises(ValueError, match="normalized payload identities disagree"):
        PostgresCoreRepository._snapshot_member(
            "listing:xnas:nvda", wrong_price_listing_rows, histories=(flat_history(),)
        )

    wrong_financial_issuer_rows = _member_rows_for_probe()
    wrong_financial_issuer_rows["financial-fact"].payload["issuer_id"] = "issuer:lei:549300S4KLFTLO7GSQ80"
    with pytest.raises(ValueError, match="normalized payload identities disagree"):
        PostgresCoreRepository._snapshot_member(
            "listing:xnas:nvda", wrong_financial_issuer_rows, histories=(flat_history(),)
        )


def test_snapshot_member_aligns_stock_split_via_live_on_close_basis() -> None:
    """#1277: `_snapshot_member` calls `share_basis.live_on_close_basis` to adjust
    `shares_outstanding` for any split with `ex_date` in `(basis_date, close_session]`."""
    from data_engine.datahub.share_basis import SplitHistory
    from factors.backtest.corporate_actions import SplitEvent

    rows = _member_rows_for_probe(
        shares_outstanding="10000000",
        shares_period_end="2026-01-20",
        financial_vintage={
            "shares_outstanding": share_count_vintage(
                filed=date(2026, 2, 1),
                own_date=date(2026, 1, 20),
                accession="0001045810-26-000001",
            )
        },
    )
    history = SplitHistory(
        symbol="NVDA",
        events=(SplitEvent("NVDA", date(2026, 3, 2), 10, 1),),
        first_session=date(2025, 12, 1),
        last_session=date(2026, 3, 31),
        last_unadjusted_close=Decimal("40"),
    )
    member = PostgresCoreRepository._snapshot_member(
        "listing:xnas:nvda",
        rows,
        histories=(history,),
    )
    assert member.shares_outstanding.value == Decimal("100000000")
    assert member.shares_outstanding.knowable_at == datetime(2026, 3, 31, tzinfo=UTC)


def test_a_run_whose_listings_have_no_stored_series_never_values_a_filed_count(connection) -> None:
    """#1277. No symbol of the run has a stored bar in either series, so no split can be proved.

    The core drops each filed count and names the cause. Red on main: `_issuer_split_histories`
    returned no histories when no symbol had a series, `_snapshot_member` skipped the adjustment,
    and the core multiplied the filed count by the close for all 20 issuers.
    """
    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(
        connection,
        financial_vintage={
            "shares_outstanding": share_count_vintage(filed=date(2026, 3, 2), own_date=date(2026, 2, 20)),
        },
    )
    repository = PostgresCoreRepository(connection)

    snapshot = repository.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
    results = repository.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))

    assert len(snapshot.members) == 21
    assert {member.shares_outstanding.value for member in snapshot.members} == {None}
    assert {member.shares_outstanding.unavailable_reason for member in snapshot.members} == {
        CoreReasonCode.NO_SPLIT_ADJUSTED_SERIES
    }
    assert len(results) == 20
    assert {item.market_value_components[0].shares_outstanding.availability for item in results} == {
        MetricAvailability.UNAVAILABLE
    }
    assert {item.market_value_components[0].shares_outstanding.unavailable_reason for item in results} == {
        CoreReasonCode.NO_SPLIT_ADJUSTED_SERIES
    }


def test_the_reasons_of_the_live_share_basis_are_exactly_the_registered_share_basis_codes() -> None:
    """`_snapshot_member` turns the reason of `live_on_close_basis` into a core reason code.

    An unregistered reason raises at the tick, after the capture is committed. This test raises first.
    It reads each reason that `share_basis` builds into an `Unavailable(...)`: a named constant or a
    string literal. The reasons equal the registered share-basis codes, in both directions (#1277).
    """
    import ast
    import inspect

    from data_engine.datahub import share_basis
    from factors.snapshot_inputs import SHARE_BASIS_REASON_CODES

    reasons: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(share_basis))):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Unavailable"):
            continue
        reason = node.args[0] if node.args else next(item.value for item in node.keywords if item.arg == "reason")
        if isinstance(reason, ast.Constant) and isinstance(reason.value, str):
            reasons.add(reason.value)
        elif isinstance(reason, ast.Name):
            reasons.add(getattr(share_basis, reason.id))
        # Another expression, such as `gaps[0].reason`, passes on a reason that another call built.
    registered = {code.value for code in SHARE_BASIS_REASON_CODES}

    assert reasons, "the reader found no reason"
    assert reasons == registered, (
        f"only in share_basis: {sorted(reasons - registered)}; only registered: {sorted(registered - reasons)}"
    )
    assert registered <= {code.value for code in CoreReasonCode}


def test_a_member_with_an_empty_history_drops_its_count_by_name() -> None:
    """`freeze_snapshot` passes `()` for an issuer that has no listing ticker. `live_on_close_basis` names it."""
    rows = _member_rows_for_probe(
        shares_outstanding="10000000",
        shares_period_end="2026-01-20",
        financial_vintage={
            "shares_outstanding": share_count_vintage(filed=date(2026, 2, 1), own_date=date(2026, 1, 20))
        },
    )

    member = PostgresCoreRepository._snapshot_member("listing:xnas:nvda", rows, histories=())

    assert member.shares_outstanding.value is None
    assert member.shares_outstanding.unavailable_reason is CoreReasonCode.NO_SPLIT_ADJUSTED_SERIES


def test_freeze_and_materialize_aligns_post_split_shares(
    connection,
) -> None:
    """#1277: end-to-end `freeze_snapshot` -> `materialize` adjusts NVDA's
    pre-split share count by 10x when `staging.market_prices_daily` records a 10:1 split
    between the filing date (`2026-03-02`) and the close session (`2026-04-01`)."""
    from split_series_seed import store_daily_series

    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(
        connection,
        financial_vintage={
            "shares_outstanding": share_count_vintage(filed=date(2026, 3, 2), own_date=date(2026, 2, 20)),
        },
    )
    close_session = (CUTOFF - timedelta(minutes=58)).astimezone(UTC).date()
    ex_date = date(2026, 3, 16)

    def _nvda_shape(day: date) -> tuple[Decimal, Decimal]:
        return (Decimal("400") if day < ex_date else Decimal("40"), Decimal("40"))

    def _flat_shape(_day: date) -> tuple[Decimal, Decimal]:
        return (Decimal("40"), Decimal("40"))

    for symbol in TOPT_SYMBOLS:
        shape = _nvda_shape if symbol == "NVDA" else _flat_shape
        store_daily_series(connection, symbol, shape, first=date(2026, 1, 2), last=close_session)

    repository = PostgresCoreRepository(connection)
    snapshot = repository.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
    results = repository.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))
    by_listing = {item.listing_id: item for item in results}

    nvda_member = next(m for m in snapshot.members if m.listing_id == "listing:xnas:nvda")
    aapl_member = next(m for m in snapshot.members if m.listing_id == "listing:xnas:aapl")
    assert nvda_member.shares_outstanding.value == Decimal("100000000")
    assert aapl_member.shares_outstanding.value == Decimal("10000000")
    assert nvda_member.gross_profit is not None and nvda_member.gross_profit.confidence > Decimal("0.5")

    nvda_result = by_listing["listing:xnas:nvda"]
    aapl_result = by_listing["listing:xnas:aapl"]
    nvda_mv = (
        nvda_result.market_value_components[0].market_price.value
        * nvda_result.market_value_components[0].shares_outstanding.value
    )
    aapl_mv = (
        aapl_result.market_value_components[0].market_price.value
        * aapl_result.market_value_components[0].shares_outstanding.value
    )
    assert nvda_mv / nvda_result.revenue.value == Decimal("40")
    assert aapl_mv / aapl_result.revenue.value == Decimal("4")
    assert nvda_result.gross_profit.confidence == nvda_member.gross_profit.confidence
    assert (
        nvda_result.market_value_components[0].shares_outstanding.confidence
        == nvda_member.shares_outstanding.confidence
    )
    assert aapl_result.gross_profit.confidence == aapl_member.gross_profit.confidence
    assert (
        aapl_result.market_value_components[0].shares_outstanding.confidence
        == aapl_member.shares_outstanding.confidence
    )


def test_a_qqq_listing_outside_topt_is_valued_once_the_qqq_price_lane_stores_its_two_series(connection) -> None:
    """#1277: no lane stored a daily series of a QQQ listing outside TOPT, so its issuer was unavailable (#1309).

    The core finds no split history for the issuer and drops its share count with a named reason. The QQQ price lane's
    weekday run stores both series of that listing through the deployed op body. The same capture
    then values the issuer on its filed share count. The head's TOPT symbol keeps the series of the
    market-data lane, and the lane does not fetch it.
    """
    from datetime import time

    import dagster as dg
    from data_engine.datahub.capture_core.universe_plane import resolve_universe_corpus
    from data_engine.lanes import qqq_prices
    from factors.snapshot_inputs import CoreReasonCode
    from qqq_head_seed import daily_series_client, publish_qqq_head
    from split_series_seed import sessions
    from truealpha_contracts.universes import lane_name

    publish_qqq_head(connection, [("AAPL", 320193), ("T1277Q", 1277)])
    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(
        connection,
        corpus=resolve_universe_corpus(connection, lane_name("qqq")),
        financial_vintage={
            "shares_outstanding": share_count_vintage(filed=date(2026, 3, 2), own_date=date(2026, 2, 20)),
        },
    )
    close_session = (CUTOFF - timedelta(minutes=58)).astimezone(UTC).date()
    store_flat_daily_series(connection, ["AAPL"], first=date(2026, 1, 2), last=close_session)
    repository = PostgresCoreRepository(connection)

    def results_by_listing() -> dict[str, Any]:
        """The core results of the capture, frozen and materialized in a savepoint that rolls back."""
        with connection.transaction() as savepoint:
            snapshot = repository.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
            results = repository.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))
            found = {item.listing_id: item for item in results}
            raise psycopg.Rollback(savepoint)
        return found

    before = results_by_listing()
    assert (
        before["listing:xnas:aapl"].market_value_components[0].shares_outstanding.availability
        is MetricAvailability.AVAILABLE
    )
    assert (
        before["listing:xnas:t1277q"].market_value_components[0].shares_outstanding.availability
        is MetricAvailability.UNAVAILABLE
    )
    assert (
        before["listing:xnas:t1277q"].market_value_components[0].shares_outstanding.unavailable_reason
        is CoreReasonCode.NO_SPLIT_ADJUSTED_SERIES
    )

    requests: list[tuple[str, str, str]] = []
    outcome = qqq_prices._refresh_qqq_prices(
        dg.build_op_context(),
        connection,
        # The lane's weekday run, after the close of the session that the capture values.
        now=datetime.combine(close_session, time(21, 35), tzinfo=UTC),
        environment="production",
        client=daily_series_client(
            requests, sessions(date(2023, 1, 3), close_session), caller=qqq_prices.QQQ_PRICE_CALLER
        ),
        commit=lambda: None,
    )

    assert outcome.stored == ["T1277Q"]
    assert {symbol for symbol, _interval, _adjust in requests} == {"T1277Q"}
    after = results_by_listing()
    assert (
        after["listing:xnas:t1277q"].market_value_components[0].shares_outstanding.availability
        is MetricAvailability.AVAILABLE
    )
    assert after["listing:xnas:t1277q"].market_value_components[0].shares_outstanding.unavailable_reason is None
    assert (
        after["listing:xnas:aapl"].market_value_components[0].shares_outstanding.availability
        is MetricAvailability.AVAILABLE
    )


def test_materialize_persists_not_applicable_status_for_exempt_topt_issuer(connection) -> None:
    """Owner Decision 2 (#1664): materialize() persists 'not_applicable' into gppe and core tables for exempt TOPT issuers."""
    from data_engine.datahub.question_coverage import gppe_cells

    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(
        connection,
        gross_profit_none_tickers=frozenset({"XOM", "V", "MA", "BRK.B", "AAPL"}),
    )
    repository = PostgresCoreRepository(connection)
    snapshot = repository.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
    results = repository.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))
    assert len(results) == 20

    gppe_statuses = dict(
        connection.execute(
            "select issuer_id, availability_status from mart.metric_node_values "
            "where run_id = %s and node_key = 'gppe_uniform_charge_v0'",
            (run.run_id,),
        ).fetchall()
    )
    xom_id = "issuer:lei:J3WHBG0MTS7O8ZVMDC91"
    v_id = "issuer:lei:549300JZ4OKEHW3DPJ59"
    ma_id = "issuer:lei:AR5L2ODV9HN37376R084"
    brk_id = "issuer:lei:5493000C01ZX7D35SD85"
    aapl_id = "issuer:lei:HWUPKR0MPOU8FGXBT394"

    for exempt_id in (xom_id, v_id, ma_id, brk_id):
        assert gppe_statuses[exempt_id] == "not_applicable"
    assert gppe_statuses[aapl_id] == "unavailable"

    cells = gppe_cells(connection, run.run_id)
    by_subject = {cell.subject_id: cell for cell in cells}
    assert all(by_subject[eid].not_applicable for eid in (xom_id, v_id, ma_id, brk_id))
    assert by_subject[aapl_id].not_applicable is False
    assert by_subject[aapl_id].answered is False
