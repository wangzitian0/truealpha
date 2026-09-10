"""A second standard reads ITS plane and reaches ITS adapter (#799).

The loop took a `MetricStandard` and used it for two fields while querying
`staging.issuer_headcount_facts` with `HEADCOUNT_SOURCE_PRIORITY`, then calling
`extract_headcount` for every `FILING_SPAN` standard whatever it declared. `STANDARDS` had
one entry, so every test that passed `standard=` got correct behaviour from the only standard
it could pass — AGENTS.md rule 7's "proves the parameter, not the wiring", exactly.

These drive the deployed functions with a SECOND standard, declared here and registered
nowhere, so they fail against the old code for the right reason rather than by construction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from data_engine.datahub.standards.backfill import _adapter, _resolve
from data_engine.datahub.standards.planner import OpenCell, UniverseIssuer, open_cells
from truealpha_contracts.standards import (
    STANDARDS,
    EvidenceRequirement,
    FactPlane,
    MetricStandard,
    StandardKind,
)

CUTOFF = datetime(2026, 9, 10, tzinfo=UTC)
ISSUER = UniverseIssuer(issuer_id="issuer:cik:320193", ticker="AAPL", listing_id="listing:xnas:aapl", cik=320193)

#: A standard that is NOT headcount, declaring a different plane and a different adapter.
#: `metric` must be registered, so it reuses a registered name; nothing here is added to
#: `STANDARDS` — the point is that the loop honours what it is handed.
OTHER = MetricStandard(
    metric="revenue",
    definition="A fixture standard for #799. Never registered; never run against a vendor.",
    acceptance_rule="Whatever the fixture adapter says.",
    kind=StandardKind.HARD,
    evidence=EvidenceRequirement.FILING_SPAN,
    confidence_policy_id="headcount-confidence:v1",
    max_age_days=400,
    evidence_bearing_sources=("fixture-extraction",),
    plane=FactPlane(
        table="staging.fixture_revenue_facts",
        issuer_column="issuer_cik",
        source_priority=("fixture-extraction", "fixture-seed"),
    ),
    adapter="tests_fixture_module:extract_fixture",
)


class _RecordingConnection:
    """Records the SQL the planner sends instead of executing it."""

    def __init__(self, row: tuple[Any, ...] | None = None) -> None:
        self.statements: list[tuple[str, Any]] = []
        self._row = row

    def execute(self, sql: str, params: Any = None):
        self.statements.append((" ".join(sql.split()), params))
        row = self._row
        return type("_Cursor", (), {"fetchone": lambda _self: row, "fetchall": lambda _self: []})()


def test_the_planner_queries_the_plane_the_standard_declares() -> None:
    connection = _RecordingConnection(row=None)
    open_cells(connection, [ISSUER], standard=OTHER, cutoff=CUTOFF)

    planner_sql = [sql for sql, _ in connection.statements if "select source, knowable_at" in sql]
    assert planner_sql, "the planner issued no fact query"
    sql = planner_sql[0]
    assert "staging.fixture_revenue_facts" in sql, f"the planner read someone else's plane: {sql}"
    assert "issuer_headcount_facts" not in sql, "the planner still reads headcount facts for a non-headcount standard"
    assert "where issuer_cik =" in sql, f"the planner ignored the declared issuer column: {sql}"


def test_the_planner_orders_by_the_priority_the_standard_declares() -> None:
    connection = _RecordingConnection(row=None)
    open_cells(connection, [ISSUER], standard=OTHER, cutoff=CUTOFF)

    _sql, params = next((s, p) for s, p in connection.statements if "select source, knowable_at" in s)
    assert params[-1] == ["fixture-extraction", "fixture-seed"], (
        f"the planner used a priority the standard did not declare: {params[-1]}"
    )


def test_employees_total_is_unchanged() -> None:
    """The regression guard on the change itself: the one registered standard must plan
    exactly as before, against its own table, column and priority."""
    connection = _RecordingConnection(row=None)
    open_cells(connection, [ISSUER], standard=STANDARDS["employees_total"], cutoff=CUTOFF)

    sql, params = next((s, p) for s, p in connection.statements if "select source, knowable_at" in s)
    assert "staging.issuer_headcount_facts" in sql
    assert "where cik =" in sql
    assert params[-1] == ["10k-extraction", "manual-review"]


def test_the_backfill_reaches_the_adapter_the_standard_declares(monkeypatch) -> None:
    """The half that made every FILING_SPAN standard extract headcount."""
    calls: list[int] = []

    def _fixture_adapter(cik: int, **_kwargs: Any):
        calls.append(cik)
        return type("_Outcome", (), {"status": "resolved", "value": 1})()

    monkeypatch.setattr(
        "data_engine.datahub.standards.backfill._adapter",
        lambda standard: _fixture_adapter if standard is OTHER else pytest.fail("wrong standard reached _adapter"),
    )
    cell = OpenCell(ISSUER, "no_fact", None, None)
    outcome = _resolve(cell, OTHER, _RecordingConnection(), None, None, CUTOFF, "probe", None)

    assert calls == [320193], "the declared adapter was not the one called"
    assert outcome.status == "resolved"


def test_an_unresolvable_adapter_names_the_standard_that_declared_it() -> None:
    """`tests_fixture_module` does not exist. The failure has to say which standard asked,
    or the next reader gets a bare ImportError from a dispatch they cannot see."""
    with pytest.raises(LookupError) as error:
        _adapter(OTHER)
    message = str(error.value)
    assert "revenue" in message and "tests_fixture_module:extract_fixture" in message


def test_the_registered_standard_still_resolves_its_real_adapter() -> None:
    resolved = _adapter(STANDARDS["employees_total"])
    assert f"{resolved.__module__}:{resolved.__name__}" == STANDARDS["employees_total"].adapter
