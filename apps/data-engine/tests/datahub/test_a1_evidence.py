"""#536: `register_run_evidence` advances the governed pointer only for a run whose
quality report meets the accepted service objectives.

Two layers, both standing checks (AGENTS.md rule 7):

* `unmet_objectives` is a pure judgement over a report dict, so its checks need no
  database and run on every collection — including the two report shapes that made
  Production's head meaningless: a report with no reconciled cells, and one whose cells
  carry two origins that disagree (`conflict_abstained`, the #535 shape). Two disagreeing
  origins corroborate nothing; counting them would be the raw origin count the fusion
  contract rules out.
* `register_run_evidence` is exercised against the real schema, in the deployed order
  (report persists, then the pointer is offered the run), driving both outcomes on one
  seeded universe: an accepted run takes the head, and a following run that misses an
  objective leaves that head exactly where it is while its own run, report and evidence
  nodes still persist.

The Postgres tests run inside one transaction that is rolled back, so "persists" here
means the same thing it means to the deployed op: present in the transaction the tick
commits.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub import quality_report
from data_engine.datahub.a1_evidence import (
    ACCEPTED_SERVICE_OBJECTIVES,
    POINTER_FACTOR_ID,
    register_run_evidence,
    unmet_objectives,
)
from data_engine.datahub.control_plane import expand_obligations, replay_retry_policy
from data_engine.datahub.evidence_graph_repository import PostgresEvidenceGraphRepository
from data_engine.datahub.repository import PostgresCaptureControlRepository
from truealpha_contracts import (
    BitemporalStamp,
    CaptureEnvironment,
    EvidenceNode,
    EvidenceNodeKind,
    EvidenceNodeRef,
    canonical_sha256,
)
from truealpha_contracts.capture_control import CaptureListVersion
from truealpha_contracts.datahub import CaptureCampaign, CaptureRun, CaptureSchedulePolicy
from truealpha_contracts.universe import SubjectKind, SubjectRef, UniverseRef

CUTOFF = datetime(2026, 4, 2, tzinfo=UTC)
PARTITION = "2026-04-01"
# The accepted demand's machine-readable form; `docs/datahub-service-demand.md` describes
# this exact object.
DEMAND_FIXTURE = (
    Path(__file__).resolve().parents[4] / "libs" / "contracts" / "tests" / "fixtures" / "datahub_service_demand.v1.json"
)


# -- the judgement, no database ---------------------------------------------------------


def _report(**overrides: Any) -> dict[str, Any]:
    """A report shaped like `quality_report.build_report`'s output, meeting every
    accepted objective. Each check below moves exactly one figure."""
    report: dict[str, Any] = {
        "run_id": "capture-run:" + "a" * 64,
        "requested_count": 4,
        "terminal_coverage": "1.0000",
        "availability": "1.0000",
        "freshness": "1.0000",
        "denominator_mean_confidence": "0.9012",
        "independent_reconciliation": "1.0000",
        "reconciliation_cells": {
            "listing:first": {"outcome": "agreed", "origin_groups": 2},
            "listing:second": {"outcome": "agreed", "origin_groups": 2},
        },
        "complete": True,
    }
    report.update(overrides)
    return report


def test_the_gate_thresholds_are_the_accepted_demands_own_numbers() -> None:
    # The constant must never drift from the demand it claims to enforce. This binds it
    # to the accepted demand's checked-in objective, the same numbers
    # `docs/datahub-service-demand.md` states in prose.
    objective = json.loads(DEMAND_FIXTURE.read_text())["quality_objective"]
    assert Decimal(objective["minimum_coverage"]) == ACCEPTED_SERVICE_OBJECTIVES.minimum_coverage
    assert Decimal(objective["minimum_availability"]) == ACCEPTED_SERVICE_OBJECTIVES.minimum_availability
    assert Decimal(objective["minimum_confidence_score"]) == ACCEPTED_SERVICE_OBJECTIVES.minimum_confidence_score
    assert (
        objective["minimum_independent_origin_groups"] == ACCEPTED_SERVICE_OBJECTIVES.minimum_independent_origin_groups
    )
    assert objective["confidence_target_band"] == "high"


def test_a_report_meeting_every_objective_raises_no_objection() -> None:
    assert unmet_objectives(_report()) == ()


@pytest.mark.parametrize(
    ("objective", "overrides"),
    [
        ("denominator_coverage", {"terminal_coverage": "0.9900"}),
        ("availability", {"availability": "0.9400"}),
        # 0.6999 x 100 = 69.99, just under the demanded 70 on the presentation scale.
        ("continuous_confidence", {"denominator_mean_confidence": "0.6999"}),
        # One canonical origin: nothing corroborated it (#344's missing second origin).
        (
            "corroborated_share",
            {
                "reconciliation_cells": {
                    "listing:first": {"outcome": "insufficient_independent_origins", "origin_groups": 1}
                }
            },
        ),
        # Two origins that DISAGREE (#535's after-hours shape). The cell abstains and
        # serves nothing, so its two origins corroborate nothing.
        (
            "corroborated_share",
            {"reconciliation_cells": {"listing:first": {"outcome": "conflict_abstained", "origin_groups": 2}}},
        ),
        # A report that graded no cell at all corroborates nothing either.
        ("corroborated_share", {"reconciliation_cells": {}}),
    ],
)
def test_a_report_missing_one_objective_objects_by_name(objective: str, overrides: dict[str, Any]) -> None:
    unmet = unmet_objectives(_report(**overrides))
    assert [item.objective for item in unmet] == [objective]
    # The objection carries both numbers, so the op's metadata reads without the DB.
    assert unmet[0].required and unmet[0].observed


def test_one_abstained_cell_no_longer_freezes_a_corroborated_universe() -> None:
    """#623's live shape: 20/21 agreed with one same-day abstain (LLY) is 0.952 —
    above the accepted 0.95 share — so the head advances; the abstained cell is
    served with its honest grade instead of holding everyone else's fresher data."""
    cells = {f"listing:{i}": {"outcome": "agreed", "origin_groups": 2} for i in range(20)}
    cells["listing:xnys:lly"] = {"outcome": "conflict_abstained", "origin_groups": 2}
    assert unmet_objectives(_report(reconciliation_cells=cells)) == ()


def test_a_mostly_uncorroborated_universe_still_holds_the_head() -> None:
    """Take-5's shape (24/102 agreed = 0.235) stays refused under the share objective."""
    cells = {f"listing:{i}": {"outcome": "agreed", "origin_groups": 2} for i in range(24)}
    cells.update(
        {f"listing:x{i}": {"outcome": "insufficient_independent_origins", "origin_groups": 1} for i in range(78)}
    )
    unmet = unmet_objectives(_report(reconciliation_cells=cells))
    assert [item.objective for item in unmet] == ["corroborated_share"]


def test_the_share_objective_scales_with_universe_size() -> None:
    # In a two-cell universe one abstain is half the served surface (0.5 < 0.95) and
    # still withholds the head — the share objective is not a fixed miss allowance.
    unmet = unmet_objectives(
        _report(
            reconciliation_cells={
                "listing:first": {"outcome": "agreed", "origin_groups": 2},
                "listing:second": {"outcome": "conflict_abstained", "origin_groups": 2},
            }
        )
    )
    assert [item.objective for item in unmet] == ["corroborated_share"]


def test_a_malformed_report_fails_closed() -> None:
    # Missing and unreadable figures must withhold the pointer, never advance it by
    # omission.
    assert {item.objective for item in unmet_objectives({})} == {
        "denominator_coverage",
        "availability",
        "continuous_confidence",
        "corroborated_share",
    }
    assert any(item.objective == "availability" for item in unmet_objectives(_report(availability="n/a")))


# -- the pointer itself, against the real schema ----------------------------------------


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


def _seed_runs(connection, *, label: str, count: int) -> tuple[str, ...]:
    """Seed the smallest capture-control chain `mart.topt_capture_status` resolves, with
    `count` runs over ONE universe so every run shares one governed pointer key.

    The run's own evidence node is the capture executor's to append (#171 A1/A4); this
    seeder stands in for the executor exactly as the production_topt seeder does.
    """
    universe = UniverseRef(
        universe_id=f"universe:a1-evidence-{label}",
        universe_version="v1",
        content_sha256=canonical_sha256({"universe": label}),
    )
    list_version = CaptureListVersion(
        universe=universe,
        members=(SubjectRef(kind=SubjectKind.LISTING, id=f"listing:a1-evidence-{label}"),),
        effective_at=CUTOFF,
    )
    policy = CaptureSchedulePolicy(
        policy_version=f"a1-evidence-{label}:v1",
        demanded_cadence=timedelta(days=1),
        provider_availability_cadence="manual-only:v1",
        freshness_max_age=timedelta(days=2),
        retry=replay_retry_policy(3),
    )
    campaign = CaptureCampaign(
        campaign_policy_id=f"capture-policy:a1-evidence-{label}-v1",
        environment=CaptureEnvironment.PRODUCTION,
        cutoff=CUTOFF,
        universe_refs=(universe,),
    )
    repository = PostgresCaptureControlRepository(connection)
    repository.put_schedule_policy(policy)
    repository.put_campaign(campaign)
    repository.put_list_version(list_version)
    repository.bind_campaign_list(campaign.campaign_id, list_version.list_version_id)

    evidence = PostgresEvidenceGraphRepository(connection)
    stamp = BitemporalStamp(valid_from=CUTOFF.date(), transaction_time=CUTOFF, recorded_at=CUTOFF)
    run_ids = []
    for sequence in range(1, count + 1):
        run = CaptureRun(
            campaign_id=campaign.campaign_id,
            run_sequence=sequence,
            schedule_policy_id=policy.schedule_policy_id,
            capture_scope_id=f"capture-scope:{canonical_sha256({'scope': label})}",
        )
        repository.put_run(run)
        for obligation in expand_obligations(
            run_id=run.run_id,
            list_version=list_version,
            semantic_types=("market-price",),
            partition=PARTITION,
        ):
            repository.put_obligation(campaign.campaign_id, obligation)
        evidence.append(
            [
                EvidenceNode(
                    ref=EvidenceNodeRef(kind=EvidenceNodeKind.CAPTURE_RUN, node_id=run.run_id),
                    content_sha256=run.run_id.split(":", 1)[1],
                    stamp=stamp,
                )
            ],
            [],
        )
        run_ids.append(run.run_id)
    return tuple(run_ids)


def _release_manifest_id(run_id: str) -> str:
    return f"release-manifest:{canonical_sha256({'release': run_id})}"


def _head(connection, universe_id: str) -> tuple[str, int] | None:
    return connection.execute(
        """
        select target_run_id, sequence from mart.current_pointer_head
        where environment = 'production' and universe_id = %s
          and universe_version = 'v1' and factor_id = %s
        """,
        (universe_id, POINTER_FACTOR_ID),
    ).fetchone()


def _register(connection, run_id: str, report: dict[str, Any]):
    """The deployed order: the quality report persists, then the pointer is offered the
    run (`dagster_defs.run_topt_live_tick`)."""
    quality_report.persist(connection, report)
    return register_run_evidence(
        connection,
        run_id=run_id,
        release_manifest_id=_release_manifest_id(run_id),
        quality_report=report,
    )


def test_a_run_meeting_every_objective_takes_the_governed_head(connection) -> None:
    universe_id = "universe:a1-evidence-accepted"
    (run_id,) = _seed_runs(connection, label="accepted", count=1)
    assert _head(connection, universe_id) is None

    registration = _register(connection, run_id, _report(run_id=run_id))

    assert registration.accepted, registration.summary
    assert registration.unmet == ()
    assert registration.sequence == 0
    assert _head(connection, universe_id) == (run_id, 0)


def test_a_run_missing_an_objective_leaves_the_head_and_still_persists_everything(connection) -> None:
    universe_id = "universe:a1-evidence-withheld"
    accepted_run, degraded_run = _seed_runs(connection, label="withheld", count=2)
    # An accepted tick first, so "unchanged" means a real head stayed put rather than an
    # empty pointer staying empty.
    assert _register(connection, accepted_run, _report(run_id=accepted_run)).accepted
    assert _head(connection, universe_id) == (accepted_run, 0)

    # The exact Production shape from #536: every price cell graded, none corroborated.
    degraded = _report(
        run_id=degraded_run,
        independent_reconciliation="0.0000",
        reconciliation_cells={"listing:first": {"outcome": "conflict_abstained", "origin_groups": 2}},
    )
    registration = _register(connection, degraded_run, degraded)

    assert not registration.accepted
    assert [item.objective for item in registration.unmet] == ["corroborated_share"]
    # 1. the head did not move
    assert _head(connection, universe_id) == (accepted_run, 0)
    assert registration.sequence == 0  # the incumbent's sequence, not the refused run's
    # 2. the run still exists
    assert (
        connection.execute("select 1 from raw.capture_runs where run_id = %s", (degraded_run,)).fetchone() is not None
    )
    # 3. its report still exists, with the numbers that refused it
    stored = connection.execute(
        "select payload from mart.datahub_quality_report where run_id = %s", (degraded_run,)
    ).fetchone()
    assert stored is not None and stored[0]["independent_reconciliation"] == "0.0000"
    # 4. its evidence still exists: the release-manifest node and the bound_to edge this
    #    step owes the run, appended before the objectives were judged
    manifest_id = _release_manifest_id(degraded_run)
    assert (
        connection.execute("select 1 from staging.evidence_nodes where node_id = %s", (manifest_id,)).fetchone()
        is not None
    )
    assert (
        connection.execute(
            "select 1 from staging.evidence_edges where from_id = %s and to_id = %s and relation = 'bound_to'",
            (degraded_run, manifest_id),
        ).fetchone()
        is not None
    )


def test_a_retried_tick_that_already_heads_the_pointer_advances_nothing(connection) -> None:
    universe_id = "universe:a1-evidence-retried"
    (run_id,) = _seed_runs(connection, label="retried", count=1)
    report = _report(run_id=run_id)

    first = _register(connection, run_id, report)
    second = _register(connection, run_id, report)

    assert first.sequence == second.sequence == 0
    assert second.accepted
    assert _head(connection, universe_id) == (run_id, 0)
    assert (
        connection.execute(
            "select count(*) from mart.current_pointer where universe_id = %s and factor_id = %s",
            (universe_id, POINTER_FACTOR_ID),
        ).fetchone()[0]
        == 1
    )
