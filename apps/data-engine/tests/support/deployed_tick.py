"""One deployed tick op, with every collaborator faked but the database connection (#1522).

`run_deployed_tick` drives `capture.run_topt_live_tick` (or any tick op) the way Dagster calls it. The capture
pipeline, the plausibility gate and the strategy replay are replaced; the connection is a real one, lent for
the tick. A test asserts what the tick itself writes, with no vendor, no object store and no strategy run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import dagster as dg
import psycopg
from data_engine.datahub.a1_evidence import PointerRegistration
from data_engine.datahub.production_topt.composition import ToptPipelineResult
from data_engine.datahub.production_topt.plausibility_gate import Verdict
from data_engine.lanes import capture
from macro_seed import stub_rate

RUN = "capture-run:" + "f" * 64
#: A HISTORICAL replay cutoff. The look-ahead the consolidation tests guard against is not a filing dated
#: in the future (`staging.kg_identifiers` already refuses to record one: recorded_at >= transaction_time).
#: It is a filing that has landed since, applied to a replay of an earlier date: the shape a backtest meets (#758/M3).
CUTOFF = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


class LentConnection:
    """A real connection, lent to the tick. The tick commits and leaves the block; the test rolls back."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def __enter__(self) -> LentConnection:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def commit(self) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def run_deployed_tick(monkeypatch: Any, op: Any, *, connection: Any = None, materialize: Any = None) -> None:
    """Run the tick op once. ``connection`` is lent to the tick. Without one the tick gets an empty stand-in and
    the registry write is skipped: that test asserts the wiring of another step. ``materialize`` replaces the
    fund consolidation, for a test that records its call."""
    lent = LentConnection(connection)
    if connection is None:
        monkeypatch.setattr(capture, "ensure_served_head_artifacts", lambda *_a, **_k: 0)
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: lent)
    monkeypatch.setattr(
        capture,
        "run_topt_pipeline",
        lambda *_a, **_k: ToptPipelineResult(
            run_id=RUN,
            release_manifest_id="release-manifest:" + "b" * 64,
            core_result_count=20,
            quality_report_id="datahub-quality-report:" + "c" * 64,
            quality={"available_count": 20, "requested_count": 20, "independent_reconciliation": "20/20"},
            risk_free_rate=stub_rate(),
        ),
    )
    monkeypatch.setattr(capture, "seed_strategy_inputs_from_capture", lambda *_a, **_k: 21)
    monkeypatch.setattr(capture, "persist_strategy_input_coverage", lambda *_a, **_k: (20, 20))
    monkeypatch.setattr(
        capture,
        "run_strategy_replay_for_cutoff",
        lambda *_a, **_k: ("strategy-run:" + "d" * 64, 20, "snap:" + "e" * 64),
    )
    monkeypatch.setattr(capture, "register_run_evidence", lambda *_a, **_k: PointerRegistration(RUN, 1, ()))
    passing = Verdict("v4", (), (), ())
    monkeypatch.setattr(capture, "judge_issuers", lambda *_a, **_k: passing)
    monkeypatch.setattr(capture, "judge_strategy", lambda *_a, **_k: passing)
    monkeypatch.setattr(capture, "registry_unavailable_issuers", lambda *_a, **_k: [])
    monkeypatch.setattr(capture, "materialize_fund_consolidation", materialize or (lambda *_a, **_k: ()))
    op(dg.build_op_context(), capture.ToptLiveTickConfig(executed_at=CUTOFF.isoformat()))
