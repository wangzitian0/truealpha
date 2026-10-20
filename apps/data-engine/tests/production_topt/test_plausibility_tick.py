"""#1323 and #1443 through the deployed tick (`run_topt_live_tick`) over offline vendor routes.

One refused issuer is excluded with a named reason, the other nineteen decisions publish, and the
pointer advances: the ticks of 2026-10-09 to 10-12 each refused one issuer (MU) and rolled back all
twenty. More than a quarter of the run refused is a data fault: the tick fails and the pointer
stays on the previous run. The independent reference is the run's own second financial-fact
origin, captured through the same adapter the deployed tick uses.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path

import dagster as dg
import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.production_topt.moomoo_origin import (
    FINANCIALS_CONFIDENCE,
    FINANCIALS_MAPPING_VERSION,
    FINANCIALS_ORIGIN,
    FINANCIALS_PARSER_VERSION,
)
from data_engine.datahub.production_topt.sec_financial_adapter import (
    FinancialFactAssertion,
    FinancialFactCorroboratingOrigin,
    FinancialFactsBundle,
)
from factors.composite import plausibility_policy
from factors.forest import IssuerClass, SignPolicy, published_node
from factors.production_topt import OperatingBranch
from truealpha_contracts.models import DataSource
from truealpha_contracts.strategy import ExclusionReason
from truealpha_runtime.testing import isolated_test_database

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "support"))
from production_topt.test_degraded_capture_forced import (  # noqa: E402
    _BANK_TICKER,
    _arm,
    _bundle,
    _live_topt_tick,
    _quote,
    _served_identity,
    _served_report,
)
from split_series_seed import store_topt_series  # noqa: E402

D = Decimal
FY2025 = date(2025, 12, 31)
#: A later fiscal year the second origin reports before the primary files it (MU: 2026-09-03).
LATER_YEAR = date(2026, 3, 28)
_PLAUSIBILITY_REASONS = {
    ExclusionReason.PLAUSIBILITY_REFUSED_INDEPENDENT_REFERENCE.value,
    ExclusionReason.PLAUSIBILITY_REFUSED_NODE_SIGN_POLICY.value,
}


@pytest.fixture(scope="module")
def tick_database_url():
    with isolated_test_database("plausibility_tick") as target_url:
        # The strategy seed takes each issuer's splits from the stored daily series (#1262).
        store_topt_series(target_url)
        yield target_url


def _dated_bundle(*, total_assets: dict[str, Decimal]) -> Callable[[str, OperatingBranch], FinancialFactsBundle]:
    """The harness bundle with dated periods, so a reference has a served period to judge."""

    def bundle(ticker: str, branch: OperatingBranch) -> FinancialFactsBundle:
        dated = replace(_bundle(branch), revenue_period_end=FY2025, operating_period_end=FY2025)
        if ticker in total_assets:
            dated = replace(dated, total_assets=total_assets[ticker])
        return dated

    return bundle


def _second_origin(superseded: frozenset[str]) -> FinancialFactCorroboratingOrigin:
    """The second financial-fact origin: the primary's FY2025 figures for every issuer, and for the
    `superseded` tickers a later fiscal year with five times the revenue."""

    def fetch(ticker: str, cutoff: date) -> FinancialFactAssertion:
        served: dict[str, Decimal | None] = {
            "revenue": D("100000000"),
            "gross_profit": D("80000000") if ticker == _BANK_TICKER else D("210000000"),
            "total_assets": D("200000000"),
        }
        by_period = {FY2025: served}
        if ticker in superseded:
            by_period[LATER_YEAR] = {**served, "revenue": D("500000000")}
        newest = max(by_period)
        return FinancialFactAssertion(
            raw_bytes=f'{{"ticker":"{ticker}","years":{len(by_period)}}}'.encode(),
            period_end=newest,
            values=by_period[newest],
            by_period_end=by_period,
            knowable_at=datetime(2026, 2, 1, tzinfo=UTC),
        )

    return FinancialFactCorroboratingOrigin(
        origin=FINANCIALS_ORIGIN,
        parser_version=FINANCIALS_PARSER_VERSION,
        mapping_version=FINANCIALS_MAPPING_VERSION,
        confidence=FINANCIALS_CONFIDENCE,
        fetch=fetch,
        raw_source=DataSource.MOOMOO,
    )


def _arm_tick(
    monkeypatch,
    *,
    session: date,
    superseded: frozenset[str] = frozenset(),
    total_assets: dict[str, Decimal] | None = None,
) -> datetime:
    """Arm the offline routes for a tick at 22:15 on `session`, priced on that session."""
    _arm(
        monkeypatch,
        quote=lambda: _quote(session, D("40")),
        price_cutoff=session,
        bundle=_dated_bundle(total_assets=total_assets or {}),
        financial_origins=(_second_origin(superseded),),
    )
    return datetime.combine(session, time(22, 15), tzinfo=UTC)


_LISTING_SQL = """
    select distinct p.normalized_payload->>'listing_id', p.normalized_payload->>'issuer_id'
    from raw.capture_obligations ob
    join staging.capture_observation_obligations link on link.capture_obligation_id = ob.obligation_id
    join staging.capture_observation_payloads p on p.observation_id = link.observation_id
    where ob.run_id = %s and ob.capture_requirement_id = 'listing-identity:v1'
      and p.normalized_payload->>'ticker' = %s
"""
_DECISIONS_SQL = "select issuer_id, outcome, exclusion_reason from mart.strategy_decisions where strategy_run_id = %s"
_HEAD_SQL = """
    select target_run_id from mart.current_pointer_head
    where universe_id like 'universe:topt-%%' order by advanced_at desc limit 1
"""


def _coordinates(reader, run_id: str, ticker: str) -> tuple[str, str]:
    (row,) = reader.execute(_LISTING_SQL, (run_id, ticker)).fetchall()
    return str(row[0]), str(row[1])


def _decisions(reader, strategy_run_id: str) -> dict[str, tuple[str, str | None]]:
    return {
        str(issuer): (str(outcome), reason)
        for issuer, outcome, reason in reader.execute(_DECISIONS_SQL, (strategy_run_id,)).fetchall()
    }


def _assert_one_excluded(url: str, metadata: dict, *, ticker: str, rule: str, reason: ExclusionReason) -> None:
    with psycopg.connect(url) as reader:
        listing, issuer = _coordinates(reader, metadata["capture_run_id"], ticker)
        decisions = _decisions(reader, metadata["strategy_run_id"])
        # twenty decisions: the refused issuer excluded with the rule's reason, nineteen others decided
        assert len(decisions) == 20 and metadata["decision_count"] == 20
        assert decisions[issuer] == ("excluded", reason.value)
        assert [i for i, (_outcome, why) in decisions.items() if why in _PLAUSIBILITY_REASONS] == [issuer]
        assert any(outcome != "excluded" for outcome, _why in decisions.values())
        # the pointer advanced to this run
        assert reader.execute(_HEAD_SQL).fetchone()[0] == metadata["capture_run_id"]
        # #1323 criterion 3: MCP `strategy_run` reads this repository; the reason reaches it verbatim
        reader.execute("set session_replication_role = replica")
        reader.execute("update mart.current_pointer set advanced_at = %s", (datetime.now(UTC),))
        reader.execute("set session_replication_role = origin")
        reader.commit()
        served_issuer = _served_identity(reader, issuer)
    assert metadata["plausibility_excluded"] == [f"{rule} {listing}"]
    report = _served_report(url)
    assert report.strategy_run_id == metadata["strategy_run_id"]
    (served,) = [decision for decision in report.decisions if decision.issuer_id == served_issuer]
    assert served.outcome == "excluded" and served.exclusion_reason == reason.value


def test_an_issuer_the_independent_reference_contradicts_is_excluded_and_the_run_publishes(
    tick_database_url, monkeypatch
) -> None:
    """#1443 through the deployed path: MU's served FY2025 revenue is 5x off the later year the
    second origin reports. The gate reads that origin from the run's own capture and refuses MU
    alone; nineteen decisions publish and the pointer advances."""
    cutoff = _arm_tick(monkeypatch, session=date(2026, 4, 6), superseded=frozenset({"MU"}))
    metadata = _live_topt_tick(tick_database_url, monkeypatch, executed_at=cutoff, accept=True)
    _assert_one_excluded(
        tick_database_url,
        metadata,
        ticker="MU",
        rule=plausibility_policy.RULE_INDEPENDENT_REFERENCE,
        reason=ExclusionReason.PLAUSIBILITY_REFUSED_INDEPENDENT_REFERENCE,
    )


def test_one_refused_issuer_does_not_roll_back_the_tick(tick_database_url, monkeypatch) -> None:
    """#1323 criterion 1, with a rule that refused the whole tick before v4: the node forbids a
    negative non-financial metric and NVDA's is negative. The tick succeeds with 19 decisions and
    NVDA excluded as `plausibility_refused_node_sign_policy`, and the pointer advances."""
    node = published_node(plausibility_policy.JUDGED_TABLE, plausibility_policy.JUDGED_COLUMN)
    strict = node.model_copy(
        update={"sign_policy": {**node.sign_policy, IssuerClass.NON_FINANCIAL: SignPolicy.MUST_BE_NON_NEGATIVE}}
    )
    monkeypatch.setattr(plausibility_policy, "published_node", lambda table, column: strict)
    # A capital charge above the gross profit: 5% of 10B is 500M against 210M.
    cutoff = _arm_tick(monkeypatch, session=date(2026, 4, 8), total_assets={"NVDA": D("10000000000")})
    metadata = _live_topt_tick(tick_database_url, monkeypatch, executed_at=cutoff, accept=True)
    with psycopg.connect(tick_database_url) as reader:
        listing, _issuer = _coordinates(reader, metadata["capture_run_id"], "NVDA")
        published = reader.execute(
            "select availability, operating_efficiency from mart.topt_core_results where run_id = %s and listing_id = %s",
            (metadata["capture_run_id"], listing),
        ).fetchone()
    # Precondition: the refused row is published as available with a negative metric.
    assert published[0] == "available" and published[1] < 0
    _assert_one_excluded(
        tick_database_url,
        metadata,
        ticker="NVDA",
        rule=plausibility_policy.RULE_NODE_SIGN_POLICY,
        reason=ExclusionReason.PLAUSIBILITY_REFUSED_NODE_SIGN_POLICY,
    )


def test_a_large_refused_share_fails_the_tick_and_the_pointer_stays(tick_database_url, monkeypatch) -> None:
    """#1323 criterion 2: six of twenty refused is a data fault. The tick raises, nothing of the run
    is published, and the pointer stays on the previous run."""
    from data_engine.datahub import a1_evidence
    from data_engine.lanes.capture import ToptLiveTickConfig, run_topt_live_tick

    with psycopg.connect(tick_database_url) as reader:
        head = reader.execute(_HEAD_SQL).fetchone()
    cutoff = _arm_tick(
        monkeypatch, session=date(2026, 4, 10), superseded=frozenset({"MU", "AAPL", "AMZN", "AVGO", "COST", "META"})
    )
    monkeypatch.setattr(settings, "database_url", tick_database_url)
    monkeypatch.setattr(a1_evidence, "unmet_objectives", lambda *_args, **_kwargs: ())
    with pytest.raises(dg.Failure, match="refused-share-of-run") as failure:
        run_topt_live_tick(dg.build_op_context(), ToptLiveTickConfig(executed_at=cutoff.isoformat()))
    assert "6 of 20 rows refused (30%)" in str(failure.value)
    with psycopg.connect(tick_database_url) as reader:
        assert reader.execute(_HEAD_SQL).fetchone() == head
        assert (
            reader.execute("select count(*) from mart.topt_core_results where cutoff = %s", (cutoff,)).fetchone()[0]
            == 0
        )
        assert (
            reader.execute("select count(*) from mart.strategy_runs where executed_at = %s", (cutoff,)).fetchone()[0]
            == 0
        )
