"""#544 acceptance 2 and 5, against a real materialized run: the gate reads this run's
published rows, refuses what the policy rejects, defers what the nightly suite's exemption
file defers, and judges the empty eligible set here.
#528: the sign rule reads the metric forest, and a declared low signal is shown, not refused.
#1443: the gate reads no other run. #1323: an issuer refusal names one listing and does not
refuse the run."""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.production_topt import PostgresToptCoreRepository, plausibility_gate
from factors.composite import plausibility_policy
from factors.composite.plausibility_policy import RULE_EMPTY_ELIGIBLE, RULE_NODE_SIGN_POLICY
from factors.forest import IssuerClass, SignPolicy, published_node
from factors.production_topt import GppeV0Definition
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.strategy import ExclusionReason

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from production_topt.test_materialization import _seed_complete_production_run  # noqa: E402


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


def _materialized_run(connection):
    _repository, run, _list_version, release_manifest_id, *_ = _seed_complete_production_run(connection)
    core = PostgresToptCoreRepository(connection)
    snapshot = core.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
    results = core.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))
    assert len(results) == 20
    return run.run_id, snapshot


def _point_head_at(connection, run_id: str, snapshot, sequence: int = 0) -> None:
    pointer_sha = canonical_sha256({"probe": run_id})
    connection.execute(
        """
        insert into mart.current_pointer (pointer_id, content_sha256, environment, universe_id, universe_version,
                                          factor_id, target_run_id, sequence, previous_run_id, advanced_at)
        values (%s, %s, 'production', %s, %s, 'gross_profit_per_employee', %s, %s, null, clock_timestamp())
        """,
        (
            f"current-pointer:{pointer_sha}",
            pointer_sha,
            snapshot.universe_id,
            snapshot.universe_version,
            run_id,
            sequence,
        ),
    )


def _bypass_append_only(connection) -> None:
    # The mart tables are append-only by trigger; the test mutates one published row to
    # manufacture the defect the gate must catch (the same bypass test_persistence uses).
    connection.execute("set local session_replication_role = replica")


def _exemptions(tmp_path: Path, expires: str) -> Path:
    path = tmp_path / "exemptions.json"
    path.write_text(
        json.dumps(
            {
                "exemptions": [
                    {
                        "invariant": "node-sign-policy",
                        "issue": "#528",
                        "expires": expires,
                        "reason": "test",
                    }
                ]
            }
        )
    )
    return path


def test_a_plausible_run_passes(connection, tmp_path) -> None:
    run_id, snapshot = _materialized_run(connection)
    _point_head_at(connection, run_id, snapshot)
    verdict = plausibility_gate.judge_run(
        connection, run_id=run_id, exemptions_path=_exemptions(tmp_path, "2000-01-01"), today=date(2026, 9, 8)
    )
    # nothing negative, nothing refused
    assert not verdict.refused and verdict.refusals == () and verdict.deferred == ()
    assert verdict.lines()[-1].strip().startswith("ok:")


def _jpm_negative(connection, run_id: str) -> None:
    _bypass_append_only(connection)
    connection.execute(
        "update mart.topt_core_results set operating_efficiency = -514726, availability = 'available' "
        "where run_id = %s and listing_id = 'listing:xnys:jpm'",
        (run_id,),
    )


def test_a_negative_bank_metric_is_a_declared_signal_shown_on_the_tick(connection, tmp_path) -> None:
    """#528: GPPE v0.2.0 declares a negative value a valid low signal for every class (#59), so
    JPM's -514,726 is neither refused nor deferred: it is carried as a signal and printed by
    name, with no exemption in force at all."""
    run_id, snapshot = _materialized_run(connection)
    _point_head_at(connection, run_id, snapshot)
    _jpm_negative(connection, run_id)
    verdict = plausibility_gate.judge_run(
        connection, run_id=run_id, exemptions_path=_exemptions(tmp_path, "2000-01-01"), today=date(2026, 9, 24)
    )
    assert not verdict.refused and verdict.deferred == ()
    assert [(s.rule, s.listing_id) for s in verdict.signals] == [(RULE_NODE_SIGN_POLICY, "listing:xnys:jpm")]
    (line,) = [line for line in verdict.lines() if line.startswith("  SIGNAL")]
    assert "listing:xnys:jpm" in line and "-514,726.00" in line and "sign-is-signal" in line


def test_a_sign_the_node_forbids_is_refused_unless_an_unexpired_exemption_defers_it(
    connection, tmp_path, monkeypatch
) -> None:
    """The rule has teeth: had the definition declared the financial component
    `must-be-non-negative`, the same row is refused — deferred only while an exemption lives."""
    node = published_node(plausibility_policy.JUDGED_TABLE, plausibility_policy.JUDGED_COLUMN)
    strict = node.model_copy(
        update={"sign_policy": {**node.sign_policy, IssuerClass.FINANCIAL: SignPolicy.MUST_BE_NON_NEGATIVE}}
    )
    monkeypatch.setattr(plausibility_policy, "published_node", lambda table, column: strict)
    run_id, snapshot = _materialized_run(connection)
    _point_head_at(connection, run_id, snapshot)
    _jpm_negative(connection, run_id)
    live = plausibility_gate.judge_run(
        connection, run_id=run_id, exemptions_path=_exemptions(tmp_path, "2026-09-16"), today=date(2026, 9, 8)
    )
    assert not live.refused and [v.rule for v, _ in live.deferred] == [RULE_NODE_SIGN_POLICY]
    assert "exempt until 2026-09-16 under #528" in live.deferred[0][1]
    assert live.signals == ()
    expired = plausibility_gate.judge_run(
        connection, run_id=run_id, exemptions_path=_exemptions(tmp_path, "2026-09-16"), today=date(2026, 9, 17)
    )
    # #1323: the refusal names JPM and excludes its issuer; it does not refuse the run
    assert [(v.rule, v.listing_id) for v in expired.refusals] == [(RULE_NODE_SIGN_POLICY, "listing:xnys:jpm")]
    assert "must-be-non-negative" in expired.refusals[0].detail
    assert not expired.refused
    assert expired.excluded_issuers() == {
        expired.issuers["listing:xnys:jpm"]: ExclusionReason.PLAUSIBILITY_REFUSED_NODE_SIGN_POLICY
    }
    (line,) = [line for line in expired.lines() if line.startswith("  EXCLUDED")]
    assert "listing:xnys:jpm" in line and "plausibility_refused_node_sign_policy" in line


def test_an_empty_eligible_set_is_judged_here_not_on_a_second_issue(connection, tmp_path) -> None:
    run_id, snapshot = _materialized_run(connection)
    _point_head_at(connection, run_id, snapshot)
    verdict = plausibility_gate.judge_run(
        connection,
        run_id=run_id,
        l2_complete=0,
        exemptions_path=_exemptions(tmp_path, "2000-01-01"),
        today=date(2026, 9, 8),
    )
    assert verdict.refused and [v.rule for v in verdict.run_refusals] == [RULE_EMPTY_ELIGIBLE]
    # #544: a strategy run with zero published decisions must also be refused
    empty_strategy_verdict = plausibility_gate.judge_run(
        connection,
        run_id=run_id,
        strategy_run_id="run:empty-strategy-0-decisions",
        l2_complete=5,
        exemptions_path=_exemptions(tmp_path, "2000-01-01"),
        today=date(2026, 9, 8),
    )
    assert empty_strategy_verdict.refused and [v.rule for v in empty_strategy_verdict.run_refusals] == [
        RULE_EMPTY_ELIGIBLE
    ]
