"""#544 acceptance 2 and 5, against a real materialized run: the gate reads this run's
published rows and the previous accepted head, refuses a run the policy rejects, defers
what the nightly suite's exemption file defers, and judges the empty eligible set here.
#528: the sign rule reads the metric forest, and a declared low signal is shown, not refused."""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.production_topt import PostgresToptCoreRepository, plausibility_gate
from factors.composite import plausibility_policy
from factors.composite.plausibility_policy import RULE_EMPTY_ELIGIBLE, RULE_NODE_SIGN_POLICY, RULE_UNIVERSE_MAX
from factors.forest import IssuerClass, SignPolicy, published_node
from factors.production_topt import GppeV0Definition
from truealpha_contracts.common import canonical_sha256

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


def test_a_plausible_run_against_its_own_predecessor_passes(connection, tmp_path) -> None:
    run_id, snapshot = _materialized_run(connection)
    _point_head_at(connection, run_id, snapshot)
    verdict = plausibility_gate.judge_run(
        connection, run_id=run_id, exemptions_path=_exemptions(tmp_path, "2000-01-01"), today=date(2026, 9, 8)
    )
    # the head IS this run: no previous, nothing to regress against, nothing negative
    assert verdict.previous_run_id is None and not verdict.refused and verdict.deferred == ()
    assert verdict.lines()[-1].strip().startswith("ok:")


def _register_previous_run(connection, snapshot, *, sequence: int = 0) -> str:
    """A previous accepted run for the same universe, as the pointer sees it: an evidence
    node the head can target. Its published rows are supplied by the test (the seed
    helper can freeze one corpus per transaction), the current run's rows are real."""
    sha = canonical_sha256({"previous-run-for": snapshot.snapshot_id})
    run_id = f"capture-run:{sha}"
    connection.execute(
        """
        insert into staging.evidence_nodes (node_id, kind, content_sha256, valid_from, transaction_time, recorded_at)
        values (%s, 'capture_run', %s, current_date, clock_timestamp(), clock_timestamp())
        """,
        (run_id, sha),
    )
    _point_head_at(connection, run_id, snapshot, sequence=sequence)
    return run_id


def test_xoms_shape_is_refused_against_the_previous_accepted_run(connection, tmp_path, monkeypatch) -> None:
    """#533 replayed: one issuer's operating metric above 1.5x the previous accepted
    universe maximum. The previous accepted run is the head of the SAME universe, resolved
    through mart.current_pointer_head; its rows are the 2026-09-06 production shape."""
    from factors.composite.plausibility_policy import Row

    current_run_id, snapshot = _materialized_run(connection)
    previous_run_id = _register_previous_run(connection, snapshot)
    real_rows = plausibility_gate._rows
    previous_rows = [
        Row("listing:xnas:nvda", "non_financial", "available", Decimal("3272605"), Decimal("25.71"), Decimal("180")),
        Row("listing:xnas:meta", "non_financial", "available", Decimal("1804263"), Decimal("7.74"), Decimal("750")),
    ]
    monkeypatch.setattr(
        plausibility_gate,
        "_rows",
        lambda conn, run_id: previous_rows if run_id == previous_run_id else real_rows(conn, run_id),
    )
    _bypass_append_only(connection)
    connection.execute(
        "update mart.topt_core_results set operating_efficiency = 4984153, availability = 'available' "
        "where run_id = %s and listing_id = 'listing:xnys:xom'",
        (current_run_id,),
    )
    verdict = plausibility_gate.judge_run(
        connection, run_id=current_run_id, exemptions_path=_exemptions(tmp_path, "2000-01-01"), today=date(2026, 9, 8)
    )
    assert verdict.previous_run_id == previous_run_id
    assert [(v.rule, v.listing_id) for v in verdict.violations] == [(RULE_UNIVERSE_MAX, "listing:xnys:xom")]
    assert any("REFUSED" in line for line in verdict.lines())
    # the same current rows with XOM inside the ceiling pass against the same head
    connection.execute(
        "update mart.topt_core_results set operating_efficiency = 4000000 where run_id = %s and listing_id = 'listing:xnys:xom'",
        (current_run_id,),
    )
    assert not plausibility_gate.judge_run(
        connection, run_id=current_run_id, exemptions_path=_exemptions(tmp_path, "2000-01-01"), today=date(2026, 9, 8)
    ).refused


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
    assert expired.refused and [(v.rule, v.listing_id) for v in expired.violations] == [
        (RULE_NODE_SIGN_POLICY, "listing:xnys:jpm")
    ]
    assert "must-be-non-negative" in expired.violations[0].detail


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
    assert verdict.refused and [v.rule for v in verdict.violations] == [RULE_EMPTY_ELIGIBLE]


def test_the_price_a_row_is_judged_by_is_its_own_runs(connection) -> None:
    """#877: the gate joined `last_close` from `staging.strategy_backtest_inputs` on
    (issuer_id, cutoff), a table with no run. Another run's seed at the same cutoff — a
    forced re-run (#874), a re-published tick, or another universe keyed alike — made every
    row come back once per vintage, carrying either run's price. The price is now the
    market-price observation the row's own core result was computed from."""
    run_id, _snapshot = _materialized_run(connection)
    own = plausibility_gate._rows(connection, run_id)
    assert len(own) == 20
    # The seeded fixture's close is 40 on every listing (test_materialization's payloads).
    assert {row.last_close for row in own} == {Decimal("40")}

    # A foreign seed for every issuer at this run's cutoff, at another price.
    connection.execute(
        """
        insert into staging.strategy_backtest_inputs (issuer_id, cutoff_at, input_key, value, confidence, knowable_at)
        select distinct issuer_id, cutoff, 'last_close', 97, 0.9, cutoff
        from mart.topt_core_results where run_id = %s
        """,
        (run_id,),
    )
    assert plausibility_gate._rows(connection, run_id) == own
    # The run-less join on the same rows: this run seeded no strategy input, so every row
    # carried the foreign price as its own.
    unscoped = connection.execute(
        """
        select r.listing_id, p.value
        from mart.topt_core_results r
        left join staging.strategy_backtest_inputs p
          on p.issuer_id = r.issuer_id and p.cutoff_at = r.cutoff and p.input_key = 'last_close'
        where r.run_id = %s
        """,
        (run_id,),
    ).fetchall()
    assert len(unscoped) == 20 and {value for _listing, value in unscoped} == {Decimal("97")}
