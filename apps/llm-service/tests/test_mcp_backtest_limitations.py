"""#758: the governed_backtest MCP response carries each run's stored limitations.

A reader of the backtest through MCP must see the same limits as a reader of the page: price
return only, survivorship, today's lineage, latest vintages. The run row is inserted in a
transaction the test rolls back; the reader's own `psycopg.connect` is redirected onto it.
"""

from __future__ import annotations

import json
import os
import secrets

import psycopg
import pytest
from llm_service import mcp_server
from llm_service.mcp_server import PostgresBacktestReader, backtest_run_limitations

LIMITS = ["Price return only. No dividend source exists, so dividends are not included."]


def test_a_run_without_stored_limitations_reports_an_empty_list() -> None:
    assert backtest_run_limitations({"metrics_payload": {}}) == []
    assert backtest_run_limitations({"metrics_payload": None}) == []
    assert backtest_run_limitations({"metrics_payload": {"limitations": LIMITS}}) == LIMITS


class _Borrowed:
    def __init__(self, connection: psycopg.Connection) -> None:
        self._connection = connection

    def __enter__(self) -> psycopg.Connection:
        return self._connection

    def __exit__(self, *_exc: object) -> None:
        return None


def test_the_backtest_report_returns_the_stored_limitations(monkeypatch: pytest.MonkeyPatch) -> None:
    from truealpha_runtime.testing import guard_db_write

    url = guard_db_write()
    try:
        connection = psycopg.connect(url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; ci-python runs this against a real database")
    run_id = f"backtest-run:{secrets.token_hex(32)}"
    try:
        connection.execute(
            """
            insert into mart.backtest_runs (
                run_id, strategy_key, strategy_version, universe_id, start_date, end_date, status,
                metrics_payload, executed_at, definition_content_sha256, price_basis, claim_ceiling, benchmark_rule
            ) values (%s, 'large_model_value_v0', '0.1.0', 'u', '2024-04-01', '2024-10-11', 'succeeded',
                      %s, now(), %s, 'unadjusted_open_explicit_splits', 'price_return_no_dividends',
                      'equal_weight_buy_and_hold_first_cutoff_issuers')
            """,
            (run_id, json.dumps({"limitations": LIMITS}), secrets.token_hex(32)),
        )
        connection.row_factory = psycopg.rows.dict_row
        monkeypatch.setattr(mcp_server.psycopg, "connect", lambda *_args, **_kwargs: _Borrowed(connection))

        report = PostgresBacktestReader(database_url=url).get_backtest_report(run_id=run_id)

        (run,) = report["runs"]
        assert run["limitations"] == LIMITS
        assert (run["price_basis"], run["claim_ceiling"]) == (
            "unadjusted_open_explicit_splits",
            "price_return_no_dividends",
        )
    finally:
        connection.rollback()
        connection.close()
