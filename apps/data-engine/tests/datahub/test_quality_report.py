"""Real-Postgres coverage for `quality_report.persist` (truealpha#462 AC4).

`persist` is the one function in this module genuinely testable standalone: it
inserts a caller-supplied report dict straight into `mart.datahub_quality_report`,
an append-only table with no foreign-key dependency on the capture-control tables.
`latest_run`/`build_report`/`_reconcile_market_price_cells` all read from
`mart.topt_capture_status`, a view over the full capture-control pipeline (campaigns,
obligations, attempts) -- exercising those against a real Postgres needs that whole
chain seeded first, which is out of scope here; this file closes the narrower,
achievable slice of the coverage gap #462 found (this module had zero tests
referencing it anywhere in the repo), not the whole thing.
"""

from __future__ import annotations

import os

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.quality_report import persist


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=True)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        yield active
    finally:
        active.close()


def _report(run_id: str, **overrides: object) -> dict[str, object]:
    report: dict[str, object] = {
        "run_id": run_id,
        "requested_count": 84,
        "complete": True,
        "freshness": "1.0000",
    }
    report.update(overrides)
    return report


def test_persist_writes_a_content_addressed_row(connection) -> None:
    run_id = "capture-run:" + "e" * 64
    report_id = persist(connection, _report(run_id))

    row = connection.execute(
        "select run_id, requested_count, payload from mart.datahub_quality_report where report_id = %s",
        (report_id,),
    ).fetchone()

    assert row is not None
    assert row[0] == run_id
    assert row[1] == 84
    assert row[2]["run_id"] == run_id
    assert report_id.startswith("datahub-quality-report:")


def test_persist_is_idempotent_for_identical_content(connection) -> None:
    run_id = "capture-run:" + "f" * 64
    report = _report(run_id, note="idempotency-check")

    first_id = persist(connection, report)
    second_id = persist(connection, report)

    assert first_id == second_id
    count = connection.execute(
        "select count(*) from mart.datahub_quality_report where report_id = %s", (first_id,)
    ).fetchone()[0]
    assert count == 1


def test_persist_gives_distinct_reports_distinct_ids(connection) -> None:
    run_id = "capture-run:" + "1" * 64
    first_id = persist(connection, _report(run_id, freshness="1.0000"))
    second_id = persist(connection, _report(run_id, freshness="0.9000"))

    assert first_id != second_id
    rows = connection.execute(
        "select report_id from mart.datahub_quality_report where run_id = %s", (run_id,)
    ).fetchall()
    assert {r[0] for r in rows} == {first_id, second_id}
