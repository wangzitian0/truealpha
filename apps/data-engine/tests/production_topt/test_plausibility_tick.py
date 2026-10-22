"""#1323 through the deployed tick (`run_topt_live_tick`) over offline vendor routes.

One refused issuer is excluded with a named reason, the other nineteen decisions publish, and the
pointer advances. The ticks of 2026-10-09 to 10-12 each refused one issuer (MU) and rolled back all
twenty.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from data_engine.datahub.production_topt.sec_financial_adapter import FinancialFactsBundle
from factors.composite import plausibility_policy
from factors.forest import IssuerClass, SignPolicy, published_node
from factors.production_topt import OperatingBranch
from truealpha_contracts.strategy import ExclusionReason
from truealpha_runtime.testing import isolated_test_database

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "support"))
from macro_seed import store_risk_free_series  # noqa: E402
from production_topt.test_degraded_capture_forced import (  # noqa: E402
    _arm,
    _bundle,
    _live_topt_tick,
    _quote,
    _served_identity,
    _served_report,
)
from split_series_seed import store_topt_series  # noqa: E402

#: The settled session the tick prices on; the same day keeps every core row fresh.
SESSION = date(2026, 4, 8)


@pytest.fixture(scope="module")
def tick_database_url():
    with isolated_test_database("plausibility_tick") as target_url:
        # The strategy seed takes each issuer's splits from the stored daily series (#1262).
        store_topt_series(target_url)
        # The tick reads the risk-free rate of its cutoff from the stored series (#1255).
        store_risk_free_series(target_url)
        yield target_url


def _with_total_assets(ticker: str, total_assets: Decimal) -> Callable[[str, OperatingBranch], FinancialFactsBundle]:
    def bundle(name: str, branch: OperatingBranch) -> FinancialFactsBundle:
        return replace(_bundle(branch), total_assets=total_assets) if name == ticker else _bundle(branch)

    return bundle


_LISTING_SQL = """
    select distinct p.normalized_payload->>'listing_id', p.normalized_payload->>'issuer_id'
    from raw.capture_obligations ob
    join staging.capture_observation_obligations link on link.capture_obligation_id = ob.obligation_id
    join staging.capture_observation_payloads p on p.observation_id = link.observation_id
    where ob.run_id = %s and ob.capture_requirement_id = 'listing-identity:v1'
      and p.normalized_payload->>'ticker' = %s
"""
_HEAD_SQL = """
    select target_run_id from mart.current_pointer_head
    where universe_id like 'universe:topt-%%' order by advanced_at desc limit 1
"""


def test_one_refused_issuer_does_not_roll_back_the_tick(tick_database_url, monkeypatch) -> None:
    """#1323 criterion 1: the node forbids a negative non-financial metric and NVDA's is negative.
    v3 refused the whole tick for it; v4 excludes NVDA as `plausibility_refused_node_sign_policy`,
    publishes the other nineteen decisions and advances the pointer."""
    node = published_node(plausibility_policy.JUDGED_TABLE, plausibility_policy.JUDGED_COLUMN)
    strict = node.model_copy(
        update={"sign_policy": {**node.sign_policy, IssuerClass.NON_FINANCIAL: SignPolicy.MUST_BE_NON_NEGATIVE}}
    )
    monkeypatch.setattr(plausibility_policy, "published_node", lambda table, column: strict)
    # A capital charge above the gross profit: 5% of 10B is 500M against 210M.
    _arm(
        monkeypatch,
        quote=lambda: _quote(SESSION, Decimal("40")),
        price_cutoff=SESSION,
        bundle=_with_total_assets("NVDA", Decimal("10000000000")),
    )
    cutoff = datetime.combine(SESSION, time(22, 15), tzinfo=UTC)
    metadata = _live_topt_tick(tick_database_url, monkeypatch, executed_at=cutoff, accept=True)

    reason = ExclusionReason.PLAUSIBILITY_REFUSED_NODE_SIGN_POLICY.value
    with psycopg.connect(tick_database_url) as reader:
        (coordinates,) = reader.execute(_LISTING_SQL, (metadata["capture_run_id"], "NVDA")).fetchall()
        listing, issuer = str(coordinates[0]), str(coordinates[1])
        published = reader.execute(
            "select availability, operating_efficiency from mart.topt_core_results where run_id = %s and listing_id = %s",
            (metadata["capture_run_id"], listing),
        ).fetchone()
        # Precondition: the refused row is published as available with a negative metric.
        assert published[0] == "available" and published[1] < 0
        decisions = dict(
            reader.execute(
                "select issuer_id, exclusion_reason from mart.strategy_decisions where strategy_run_id = %s",
                (metadata["strategy_run_id"],),
            ).fetchall()
        )
        assert len(decisions) == 20 and decisions[issuer] == reason
        assert [i for i, why in decisions.items() if why == reason] == [issuer]
        # the pointer advanced to this run
        assert reader.execute(_HEAD_SQL).fetchone()[0] == metadata["capture_run_id"]
        # #1323 criterion 3: MCP `strategy_run` reads this repository; the reason reaches it verbatim
        reader.execute("set session_replication_role = replica")
        reader.execute("update mart.current_pointer set advanced_at = %s", (datetime.now(UTC),))
        reader.execute("set session_replication_role = origin")
        reader.commit()
        served_issuer = _served_identity(reader, issuer)
    assert metadata["plausibility_excluded"] == [f"{plausibility_policy.RULE_NODE_SIGN_POLICY} {listing}"]
    report = _served_report(tick_database_url)
    assert report.strategy_run_id == metadata["strategy_run_id"]
    (served,) = [decision for decision in report.decisions if decision.issuer_id == served_issuer]
    assert served.outcome == "excluded" and served.exclusion_reason == reason
