"""The module-5 fund consolidation, at the deployed call site (#727, #36).

Two halves, because a passing factor test proves the arithmetic and not the wiring
(AGENTS.md rule 7: "a criterion must be able to fail where production calls"):

- against a real database, `materialize_fund_consolidation` selects the vintage knowable
  at the cutoff, joins the run's core rows and writes a `mart.fund_virtual_company` row;
- against the deployed op, the QQQ tick invokes it and the TOPT tick does not — the flag
  on the declaration is what production reads, so that is what is asserted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import dagster as dg
import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.production_topt.fund_consolidation import (
    load_fund_vintages,
    materialize_fund_consolidation,
)
from data_engine.lanes import capture

#: A HISTORICAL replay cutoff. The look-ahead this guards against is not a filing dated
#: in the future — `staging.kg_identifiers` already refuses to record one (recorded_at >=
#: transaction_time) — it is a filing that has landed since, being applied to a replay of
#: an earlier date. That is the shape a backtest actually meets (#758/M3).
CUTOFF = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
FUND = "etf:series:TEST-CONSOLIDATION"
RUN = "capture-run:" + "f" * 64
OLDEST_FILING = datetime(2026, 3, 20, tzinfo=UTC)
#: The newest vintage knowable AT the cutoff — the one the producer must choose.
KNOWABLE_FILING = datetime(2026, 5, 15, tzinfo=UTC)
#: Filed after the cutoff but before today: the retroactive application #36 forbids, and
#: the exact row `mart.fund_holdings_valuation`'s unconditional newest-per-fund returns.
LATER_FILING = datetime(2026, 8, 15, tzinfo=UTC)


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        pytest.skip(f"no local database: {error}")
    try:
        yield active
    finally:
        active.rollback()
        active.close()


def _seed(connection, *, isin: str, ticker: str, weight: str, filing: datetime, period: str) -> None:
    """One filed holding line, its ISIN->listing identity, and nothing else."""
    connection.execute(
        "insert into staging.kg_entities (id, entity_type, display_name) values (%s, 'fund', %s) "
        "on conflict (id) do nothing",
        (FUND, "Consolidation Test Fund"),
    )
    entity = f"company:isin:{isin}"
    connection.execute(
        "insert into staging.kg_entities (id, entity_type, display_name) values (%s, 'company', %s) "
        "on conflict (id) do nothing",
        (entity, ticker),
    )
    for kind, value in (("isin", isin), ("ticker", ticker)):
        connection.execute(
            "insert into staging.kg_identifiers (entity_id, identifier_type, identifier_value, "
            "valid_time, transaction_time, confidence, source, raw_ref) "
            "values (%s, %s, %s, daterange(%s, null), %s, 1.0, 'test', 'test:consolidation') "
            "on conflict do nothing",
            (entity, kind, value, filing.date(), filing),
        )
    connection.execute(
        """
        insert into staging.fund_holding_facts
            (fund_id, holding_id, holding_name, report_period, transaction_time,
             isin, balance, value_usd, percent_of_net_assets, confidence, raw_ref)
        values (%s, %s, %s, %s, %s, %s, 1, 1000, %s, 1.0, 'test:consolidation')
        on conflict do nothing
        """,
        (FUND, entity, f"{ticker} Inc.", period, filing, isin, Decimal(weight)),
    )


def test_the_vintage_is_the_one_knowable_at_the_cutoff(connection) -> None:
    """#36: the factor never applies a later filing retroactively. The newest vintage
    OVERALL is deliberately after the cutoff, so a producer that takes
    `mart.fund_holdings_valuation`'s unconditional newest-per-fund would pick it."""
    _seed(connection, isin="US0000000001", ticker="TCA", weight="70", filing=OLDEST_FILING, period="2025-12-31")
    _seed(connection, isin="US0000000002", ticker="TCB", weight="80", filing=KNOWABLE_FILING, period="2026-03-31")
    _seed(connection, isin="US0000000003", ticker="TCC", weight="90", filing=LATER_FILING, period="2026-06-30")

    vintages = {v.fund_id: v for v in load_fund_vintages(connection, run_id=RUN, cutoff=CUTOFF)}
    chosen = vintages[FUND]

    assert chosen.transaction_time == KNOWABLE_FILING, "the newest filing AT OR BEFORE the cutoff"
    assert [line.holding_name for line in chosen.lines] == ["TCB Inc."]
    # Named so the assertion cannot rot into a tautology if the seed dates change: the
    # trap row must really be a later filing, and it must really exist.
    assert LATER_FILING > CUTOFF
    newest = connection.execute(
        "select max(transaction_time) from mart.fund_holdings where fund_id = %s", (FUND,)
    ).fetchone()[0]
    assert newest == LATER_FILING, "the look-ahead row is present; the producer declined it"


def test_a_row_is_written_with_masses_the_database_accepts(connection) -> None:
    _seed(connection, isin="US0000000010", ticker="TCV", weight="60", filing=KNOWABLE_FILING, period="2026-06-30")
    _seed(connection, isin="US0000000011", ticker="TCU", weight="39", filing=KNOWABLE_FILING, period="2026-06-30")

    written = materialize_fund_consolidation(connection, run_id=RUN, cutoff=CUTOFF)

    # Other funds may exist in the database; this test owns exactly one.
    assert FUND in {item.fund_id for item in written}
    row = connection.execute(
        """
        select weighted_valuation_gap, total_weight_pct, resolved_weight_pct, valued_weight_pct,
               lines, valued_lines, availability_status, source_evidence_status,
               factor_validation_status, reason_codes, definition_version, definition_sha256
        from mart.fund_virtual_company where run_id = %s and fund_id = %s
        """,
        (RUN, FUND),
    ).fetchone()
    assert row is not None, "the producer writes through the deployed table, not a dataclass"
    gap, total, resolved, valued, lines, valued_lines, availability, evidence, validation, reasons, ver, sha = row
    # No core rows exist for this synthetic run, so nothing is valued: the aggregate is
    # REFUSED and the row records why instead of publishing a zero.
    assert gap is None and valued == 0
    assert total == Decimal("99") and resolved == Decimal("99")
    assert (lines, valued_lines) == (2, 0)
    assert availability == "unavailable" and validation == "not_evaluated"
    assert evidence == "degraded"
    assert list(reasons) == ["valued_weight_below_minimum"]
    assert ver == "v0" and len(sha) == 64


def test_the_write_is_idempotent_per_run_and_fund(connection) -> None:
    _seed(connection, isin="US0000000020", ticker="TCI", weight="99", filing=KNOWABLE_FILING, period="2026-06-30")

    materialize_fund_consolidation(connection, run_id=RUN, cutoff=CUTOFF)
    materialize_fund_consolidation(connection, run_id=RUN, cutoff=CUTOFF)

    count = connection.execute(
        "select count(*) from mart.fund_virtual_company where run_id = %s and fund_id = %s", (RUN, FUND)
    ).fetchone()[0]
    assert count == 1, "a retried tick restates its own row rather than accumulating"


def test_no_vintage_at_the_cutoff_writes_nothing(connection) -> None:
    _seed(connection, isin="US0000000030", ticker="TCF", weight="99", filing=LATER_FILING, period="2026-06-30")

    written = materialize_fund_consolidation(connection, run_id=RUN, cutoff=CUTOFF)

    assert all(item.fund_id != FUND for item in written), (
        "a fund whose only filing postdates the cutoff is absent from the run, not zero"
    )


# --- the deployed call site -------------------------------------------------------


class _FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def commit(self) -> None:
        return None


def _run_deployed_tick(monkeypatch, op, *, spy: list) -> None:
    """Drive one deployed tick op with every collaborator faked but the consolidation
    call itself recorded — so this asserts the WIRING, not the arithmetic."""
    from data_engine.datahub.a1_evidence import PointerRegistration
    from data_engine.datahub.production_topt.composition import ToptPipelineResult
    from data_engine.datahub.production_topt.plausibility_gate import Verdict

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _FakeConnection())
    monkeypatch.setattr(
        capture,
        "run_topt_pipeline",
        lambda *a, **k: ToptPipelineResult(
            run_id=RUN,
            release_manifest_id="release-manifest:" + "b" * 64,
            core_result_count=20,
            quality_report_id="datahub-quality-report:" + "c" * 64,
            quality={"available_count": 20, "requested_count": 20, "independent_reconciliation": "20/20"},
        ),
    )
    monkeypatch.setattr(capture, "seed_strategy_inputs_from_capture", lambda *a, **k: 21)
    monkeypatch.setattr(capture, "persist_strategy_input_coverage", lambda *a, **k: (20, 20))
    monkeypatch.setattr(
        capture, "run_strategy_replay_for_cutoff", lambda *a, **k: ("strategy-run:" + "d" * 64, 20, "snap:" + "e" * 64)
    )
    monkeypatch.setattr(capture, "register_run_evidence", lambda *a, **k: PointerRegistration(RUN, 1, ()))
    monkeypatch.setattr(capture, "judge_run", lambda *a, **k: Verdict("v1", None, (), ()))

    def _spy(connection, *, run_id, cutoff, **kwargs):
        spy.append((run_id, cutoff))
        return ()

    monkeypatch.setattr(capture, "materialize_fund_consolidation", _spy)
    op(dg.build_op_context(), capture.ToptLiveTickConfig(executed_at=CUTOFF.isoformat()))


def test_the_qqq_tick_consolidates_funds(monkeypatch) -> None:
    """The QQQ universe IS the fund's holdings, so its tick is the one that can value
    them. If this goes green while the declaration says otherwise, the flag is dead."""
    assert capture.TICK_BY_JOB[capture.QQQ_LIVE_JOB_NAME].consolidate_funds is True
    spy: list = []
    _run_deployed_tick(monkeypatch, capture.run_qqq_live_tick, spy=spy)
    assert spy == [(RUN, CUTOFF)], "the deployed QQQ op materializes the consolidation for its own run"


def test_the_topt_tick_does_not_consolidate_funds(monkeypatch) -> None:
    """TOPT's 20 core rows would resolve a fraction of QQQ and refuse on coverage — a
    true answer to a question nobody asked. The negative is asserted so the flag cannot
    quietly become "always on"."""
    assert capture.TICK_BY_JOB[capture.TOPT_LIVE_JOB_NAME].consolidate_funds is False
    spy: list = []
    _run_deployed_tick(monkeypatch, capture.run_topt_live_tick, spy=spy)
    assert spy == [], "the TOPT op consolidates nothing"


def test_every_tick_declares_whether_it_consolidates() -> None:
    """A new universe must decide, not inherit a default (the declaration is the contract
    `build_tick` reads)."""
    for tick in capture.TICKS:
        assert isinstance(tick.consolidate_funds, bool), f"{tick.key} does not declare consolidate_funds"
