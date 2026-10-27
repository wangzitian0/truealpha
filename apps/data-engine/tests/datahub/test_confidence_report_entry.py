"""#1517 A1, A2: the nightly report, driven through its Dagster op on a real schema.

The run is a complete production run, materialized and accepted. Its 21 close observations are
re-labelled as Yahoo observations, so the report holds cells with one origin beside cells with none.
A cell shows its confidence from 0 to 1, its `reason` and its `independent_origins`. It shows no
band word and no 0-100 score.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import dagster as dg
import psycopg
import pytest
from data_engine.config import settings
from data_engine.lanes import quality

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datahub.test_topt_read import _accepted_run, connection  # noqa: E402, F401

_YAHOO_PARSER = "production-topt-live-parser:v1"
_EXECUTED_AT = "2026-09-16T00:45:00+00:00"


class _BorrowedConnection:
    """Gives the op the test's own connection. The op commits and closes; the test rolls back."""

    def __init__(self, real: Any) -> None:
        self._real = real

    def __enter__(self) -> _BorrowedConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def commit(self) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def test_the_op_persists_cells_with_a_reason_and_independent_origins_and_no_band_word(
    connection,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _accepted_run(connection)
    connection.execute("set local session_replication_role = replica")
    relabelled = connection.execute(
        """
        update staging.capture_normalized_observations o set parser_version = %s
        from staging.capture_observation_obligations oo
        join raw.capture_obligations ob on ob.obligation_id = oo.capture_obligation_id
        where oo.observation_id = o.observation_id and ob.run_id = %s and o.semantic_type = 'market-price'
        """,
        (_YAHOO_PARSER, run.run_id),
    ).rowcount
    connection.execute("set local session_replication_role = origin")
    assert relabelled == 21
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: _BorrowedConnection(connection))
    monkeypatch.setattr(quality, "_await_settled", lambda _context: None)
    monkeypatch.setattr(quality, "stale_served_heads", lambda *_a, **_k: [])
    monkeypatch.setattr(settings, "sec_user_agent", "")

    quality.run_confidence_report(
        dg.build_op_context(), quality.ConfidenceReportConfig(executed_at=_EXECUTED_AT, universe="topt")
    )

    payload = connection.execute(
        "select payload from mart.datahub_confidence_report where run_id = %s", (run.run_id,)
    ).fetchone()[0]
    close_cells = payload["cells"]["close"]
    assert len(close_cells) == 21
    for cell in close_cells.values():
        assert {"band", "score_100"}.isdisjoint(cell)
        assert cell["reason"] == "single_origin" and cell["independent_origins"] == 1
        assert 0 < float(cell["confidence"]) < 1
    assert {cell["reason"] for cell in payload["cells"]["revenue"].values()} == {"no_origin_value"}
    close_family = payload["families"]["close"]
    assert close_family["by_independent_origins"] == {"1": 21}
    assert {"high", "medium", "low", "missing", "share", "mean_score_100"}.isdisjoint(close_family)
    assert 0 < float(close_family["mean_confidence"]) < 1
    assert "bands" not in payload
