"""Gateway and Postgres integration tests for analyst ratings enrichment (#1791)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from data_engine.datahub.analyst_ratings import _consensus_confidence, materialize_universe_analyst_ratings
from data_engine.sources import moomoo as mm

pytestmark = pytest.mark.deps


class _StubMoomooRatingsContext:
    def __init__(self, *, summary_fails: bool) -> None:
        self._summary_fails = summary_fails

    def get_research_analyst_consensus(self, code: str) -> tuple[int, dict[str, Any]]:
        return mm.moomoo.RET_OK, {
            "rating": 4,
            "total": 10,
            "buy": 70,
            "hold": 20,
            "sell": 10,
            "target_high": 220,
            "target_low": 160,
            "target_mean": 195,
        }

    def get_research_rating_summary(self, code: str, **_kwargs: Any) -> tuple[int, dict[str, Any]]:
        if self._summary_fails:
            return mm.moomoo.RET_ERROR, {"error": "rating summary unavailable"}
        return mm.moomoo.RET_OK, {
            "analyst_rating_summary_list": [
                {
                    "analyst_info": {"analyst_id": "a-1", "success_rate": 80},
                    "rating_item_list": [{"recommendation_date_str": "2026-11-20", "rating": 5}],
                }
            ]
        }

    def get_valuation_detail(self, code: str, **_kwargs: Any) -> tuple[int, dict[str, Any]]:
        raise AssertionError(f"get_valuation_detail must not be called for {code}")


@pytest.mark.usefixtures("idle_moomoo_gate")
def test_failed_rating_summary_never_calls_valuation_detail_or_boosts_confidence(
    connection: psycopg.Connection[Any],
    call_ledger: Any,
) -> None:
    """Missing rating summary must not call `get_valuation_detail` or upgrade consensus confidence."""
    cutoff = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    issuer_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "https://truealpha.invalid/test/1791/fail"))
    run_id = "test-run-1791-no-valuation"

    capture = materialize_universe_analyst_ratings(
        connection,
        run_id=run_id,
        cutoff=cutoff,
        tickers={issuer_id: "MSFT"},
        ctx=_StubMoomooRatingsContext(summary_fails=True),
    )
    assert capture.rows == 1 and capture.failures == ()
    assert [row.endpoint for row in call_ledger] == ["get_research_analyst_consensus", "get_research_rating_summary"]

    stored = connection.execute(
        "select confidence, reason_codes from mart.issuer_analyst_ratings where run_id = %s and issuer_id = %s",
        (run_id, issuer_id),
    ).fetchone()
    assert stored is not None
    confidence, reason_codes = stored
    assert Decimal(str(confidence)) == _consensus_confidence(issuer_id)
    assert "vendor_timestamp" not in (reason_codes or [])


@pytest.mark.usefixtures("idle_moomoo_gate")
def test_nonempty_rating_summary_enriches_consensus_confidence(
    connection: psycopg.Connection[Any],
    call_ledger: Any,
) -> None:
    """Valid rating summary items still enrich consensus confidence and tag `vendor_timestamp`."""
    cutoff = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    issuer_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "https://truealpha.invalid/test/1791/ok"))
    run_id = "test-run-1791-with-summary"

    capture = materialize_universe_analyst_ratings(
        connection,
        run_id=run_id,
        cutoff=cutoff,
        tickers={issuer_id: "AAPL"},
        ctx=_StubMoomooRatingsContext(summary_fails=False),
    )
    assert capture.rows == 1 and capture.failures == ()
    assert [row.endpoint for row in call_ledger] == ["get_research_analyst_consensus", "get_research_rating_summary"]

    stored = connection.execute(
        "select consensus_rating, reason_codes from mart.issuer_analyst_ratings where run_id = %s and issuer_id = %s",
        (run_id, issuer_id),
    ).fetchone()
    assert stored is not None
    consensus_rating, reason_codes = stored
    assert Decimal(str(consensus_rating)) == Decimal("5")
    assert "vendor_timestamp" in (reason_codes or [])
