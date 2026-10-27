"""#1517 A2: the MCP reader derives `confidence_band` from `confidence` and never reads the stored column.

The run is a complete production run, materialized and quality-reported, in a real Postgres schema.
It heads no pointer, so the reader takes the acceptance-gated fallback. Each
row of one node gets a confidence from the probe of the audit and a stored `confidence_band` word
that no band rule gives (the column stays until the second release of A2).
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from data_engine.datahub import quality_report
from truealpha_contracts.confidence_bands import confidence_band
from truealpha_contracts.metric_node_read import MetricNodeValueReport, PostgresMetricNodeValueRepository

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datahub.test_topt_read import _materialized_run, connection  # noqa: E402, F401

# confidence stored on one row each -> the word the three sentences of init.md give.
_EXPECTED_BANDS = {
    "0.8563": "high",
    "0.6247": "medium",
    "0.5971": "low",
    "0.8457": "high",
    "0": "low",
}
_STALE_STORED_WORD = "stored-word-no-rule-gives"


class _BorrowedConnection:
    """Gives the reader the test's own connection, so it reads the uncommitted rows."""

    def __init__(self, real: Any, row_factory: Any) -> None:
        self._real = real
        self._row_factory = row_factory

    def __enter__(self) -> _BorrowedConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, query: Any, params: Any = None) -> Any:
        return self._real.cursor(row_factory=self._row_factory).execute(query, params)


def test_the_reader_serves_the_band_of_the_stored_confidence_not_the_stored_word(
    connection,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, _release_manifest_id = _materialized_run(connection)
    quality_report.persist(connection, quality_report.build_report(connection, run.run_id))
    stored_words = connection.execute(
        "select count(*) from mart.metric_node_values where run_id = %s and confidence_band is not null", (run.run_id,)
    ).fetchone()
    assert stored_words == (0,), "the materializer writes no band word (the column stays until the second release)"
    node_key, issuers = _node_with_rows(connection, run.run_id, minimum=len(_EXPECTED_BANDS))
    connection.execute("set local session_replication_role = replica")
    for issuer_id, confidence in zip(issuers, _EXPECTED_BANDS, strict=False):
        connection.execute(
            "update mart.metric_node_values set confidence = %s, confidence_band = %s "
            "where run_id = %s and node_key = %s and issuer_id = %s",
            (Decimal(confidence), _STALE_STORED_WORD, run.run_id, node_key, issuer_id),
        )
    connection.execute("set local session_replication_role = origin")
    monkeypatch.setattr(
        psycopg, "connect", lambda *_a, **kwargs: _BorrowedConnection(connection, kwargs.get("row_factory"))
    )

    report = PostgresMetricNodeValueRepository(database_url="postgresql://borrowed").latest(node_key=node_key)

    assert isinstance(report, MetricNodeValueReport)
    served = {cell.issuer_id: cell for cell in report.cells}
    for issuer_id, (confidence, band) in zip(issuers, _EXPECTED_BANDS.items(), strict=False):
        cell = served[issuer_id]
        assert Decimal(cell.confidence or "") == Decimal(confidence)
        assert cell.confidence_band == band, f"confidence {confidence}"
        assert cell.confidence_band == confidence_band(Decimal(cell.confidence or ""))


def _node_with_rows(connection, run_id: str, *, minimum: int) -> tuple[str, list[str]]:  # noqa: F811
    rows = connection.execute(
        "select node_key, array_agg(issuer_id order by issuer_id) from mart.metric_node_values "
        "where run_id = %s group by node_key having count(*) >= %s order by node_key limit 1",
        (run_id, minimum),
    ).fetchone()
    assert rows is not None, "the accepted run materialized no node with enough issuer rows"
    return str(rows[0]), list(rows[1])
