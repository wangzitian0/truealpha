"""The module-5 fund consolidation, at the deployed call site (#727, #36).

Two halves, because a passing factor test proves the arithmetic and not the wiring
(AGENTS.md rule 7: "a criterion must be able to fail where production calls"):

- against a real database, `materialize_fund_consolidation` selects the vintage knowable
  at the cutoff, joins the run's core rows and writes a `mart.fund_virtual_company` row;
- against the deployed op, the QQQ tick invokes it and the TOPT tick does not — the flag
  on the declaration is what production reads, so that is what is asserted.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from data_engine.datahub.capture_core.fund_consolidation import (
    _LINES_SQL,
    load_fund_vintages,
    materialize_fund_consolidation,
)
from data_engine.datahub.resolve_coordinates import resolve_entity
from data_engine.lanes import capture
from factors.shared import entity_resolution as er

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "support"))
from deployed_tick import CUTOFF, RUN, run_deployed_tick  # noqa: E402

FUND = "etf:series:TEST-CONSOLIDATION"
OLDEST_FILING = datetime(2026, 3, 20, tzinfo=UTC)
#: The newest vintage knowable AT the cutoff — the one the producer must choose.
KNOWABLE_FILING = datetime(2026, 5, 15, tzinfo=UTC)
#: Filed after the cutoff but before today: the retroactive application #36 forbids, and
#: the exact row `mart.fund_holdings_valuation`'s unconditional newest-per-fund returns.
LATER_FILING = datetime(2026, 8, 15, tzinfo=UTC)


def _fund_uuid(connection) -> str:
    return str(er.ensure_entity(connection, FUND, "fund", "Consolidation Test Fund", transaction_time=OLDEST_FILING))


def _seed(connection, *, isin: str, ticker: str, weight: str, filing: datetime, period: str) -> None:
    """One filed holding line, its ISIN->listing identity, and nothing else."""
    fund_uuid = _fund_uuid(connection)
    inst_uuid = er.ensure_entity(connection, f"company:isin:{isin}", "instrument", ticker, transaction_time=filing)
    for kind, value in (("isin", isin), ("ticker", ticker)):
        er.assert_identifier(
            connection,
            entity_id=inst_uuid,
            source="test",
            identifier_type=kind,
            identifier_value=value,
            confidence=1.0,
            transaction_time=filing,
            valid_from=period,
            raw_ref="test:consolidation",
        )
    connection.execute(
        """
        insert into staging.fund_holding_facts
            (fund_id, holding_id, holding_name, report_period, transaction_time,
             isin, balance, value_usd, percent_of_net_assets, confidence, raw_ref)
        values (%s, %s, %s, %s, %s, %s, 1, 1000, %s, 1.0, 'test:consolidation')
        on conflict do nothing
        """,
        (fund_uuid, inst_uuid, f"{ticker} Inc.", period, filing, isin, Decimal(weight)),
    )
    from truealpha_runtime.mart_refresh import refresh_mart_materialized_views

    refresh_mart_materialized_views(connection)


def test_the_vintage_is_the_one_knowable_at_the_cutoff(connection) -> None:
    """#36: the factor never applies a later filing retroactively. The newest vintage
    OVERALL is deliberately after the cutoff, so a producer that takes
    `mart.fund_holdings_valuation`'s unconditional newest-per-fund would pick it."""
    _seed(connection, isin="US0000000001", ticker="TCA", weight="70", filing=OLDEST_FILING, period="2025-12-31")
    _seed(connection, isin="US0000000002", ticker="TCB", weight="80", filing=KNOWABLE_FILING, period="2026-03-31")
    _seed(connection, isin="US0000000003", ticker="TCC", weight="90", filing=LATER_FILING, period="2026-06-30")

    fund_uuid = _fund_uuid(connection)
    vintages = {v.fund_id: v for v in load_fund_vintages(connection, run_id=RUN, cutoff=CUTOFF)}
    chosen = vintages[fund_uuid]

    assert chosen.transaction_time == KNOWABLE_FILING, "the newest filing AT OR BEFORE the cutoff"
    assert [line.holding_name for line in chosen.lines] == ["TCB Inc."]
    # Named so the assertion cannot rot into a tautology if the seed dates change: the
    # trap row must really be a later filing, and it must really exist.
    assert LATER_FILING > CUTOFF
    newest = connection.execute(
        "select max(transaction_time) from mart.fund_holdings where fund_id = %s", (fund_uuid,)
    ).fetchone()[0]
    assert newest == LATER_FILING, "the look-ahead row is present; the producer declined it"


def test_a_row_is_written_with_masses_the_database_accepts(connection) -> None:
    _seed(connection, isin="US0000000010", ticker="TCV", weight="60", filing=KNOWABLE_FILING, period="2026-06-30")
    _seed(connection, isin="US0000000011", ticker="TCU", weight="39", filing=KNOWABLE_FILING, period="2026-06-30")
    fund_uuid = _fund_uuid(connection)

    written = materialize_fund_consolidation(connection, run_id=RUN, cutoff=CUTOFF)

    # Other funds may exist in the database; this test owns exactly one.
    assert fund_uuid in {item.fund_id for item in written}
    row = connection.execute(
        """
        select weighted_valuation_gap, total_weight_pct, resolved_weight_pct, valued_weight_pct,
               lines, valued_lines, availability_status, source_evidence_status,
               factor_validation_status, reason_codes, definition_version, definition_sha256
        from mart.fund_virtual_company where run_id = %s and fund_id = %s
        """,
        (RUN, fund_uuid),
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
    # #1235: a refused aggregate has no gap, so it names no valuation basis.
    basis = connection.execute(
        "select valuation_basis from mart.fund_virtual_company where run_id = %s and fund_id = %s", (RUN, fund_uuid)
    ).fetchone()[0]
    assert basis is None


class _LinesRows:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple]:
        return self._rows


class _LinesConnection:
    """Returns `_LINES_SQL`'s row shape, so the basis logic runs without a database."""

    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def execute(self, *_args: object) -> _LinesRows:
        return _LinesRows(self._rows)


def _line(name: str, gap: str | None, basis: str | None) -> tuple:
    return (
        FUND,
        "Test fund",
        "2026-06-30",
        KNOWABLE_FILING,
        name,
        Decimal("50"),
        "listing:" + name,
        None if gap is None else Decimal(gap),
        "available" if gap is not None else None,
        Decimal("0.9") if gap is not None else None,
        "available" if gap is not None else None,
        basis,
    )


def test_the_fund_gap_takes_the_basis_of_its_valued_core_rows() -> None:
    """#1235 audit: the weighted gap averages core gaps, so it divides their multiple. The
    loader reads each valued row's basis; an unvalued line carries none."""
    rows = [
        _line("A", "0.20", "price_to_gross_profit"),
        _line("B", "0.10", "price_to_gross_profit"),
        _line("C", None, None),
    ]
    [vintage] = load_fund_vintages(_LinesConnection(rows), run_id=RUN, cutoff=CUTOFF)  # type: ignore[arg-type]
    assert vintage.valuation_basis == "price_to_gross_profit"
    [unvalued] = load_fund_vintages(_LinesConnection([_line("C", None, None)]), run_id=RUN, cutoff=CUTOFF)  # type: ignore[arg-type]
    assert unvalued.valuation_basis is None


def test_a_fund_whose_lines_mix_bases_fails_closed() -> None:
    rows = [_line("A", "0.20", "price_to_gross_profit"), _line("B", "0.10", "price_to_sales")]
    with pytest.raises(ValueError, match="mix valuation bases"):
        load_fund_vintages(_LinesConnection(rows), run_id=RUN, cutoff=CUTOFF)  # type: ignore[arg-type]


def test_the_table_refuses_a_basis_without_a_gap(connection) -> None:
    with pytest.raises(psycopg.errors.CheckViolation, match="fund_virtual_company_valuation_basis_check"):
        connection.execute(
            """
            insert into mart.fund_virtual_company (run_id, fund_id, report_period, transaction_time, cutoff,
                definition_version, definition_sha256, weighted_valuation_gap, valuation_basis,
                total_weight_pct, resolved_weight_pct, valued_weight_pct, lines, valued_lines, confidence,
                availability_status, source_evidence_status, factor_validation_status)
            values (%s, %s, '2026-06-30', %s, %s, 'v0', %s, null, 'price_to_gross_profit',
                99, 99, 0, 1, 0, 0, 'unavailable', 'degraded', 'not_evaluated')
            """,
            (RUN, FUND, KNOWABLE_FILING, CUTOFF, "a" * 64),
        )


def test_the_write_is_idempotent_per_run_and_fund(connection) -> None:
    _seed(connection, isin="US0000000020", ticker="TCI", weight="99", filing=KNOWABLE_FILING, period="2026-06-30")
    fund_uuid = _fund_uuid(connection)

    materialize_fund_consolidation(connection, run_id=RUN, cutoff=CUTOFF)
    materialize_fund_consolidation(connection, run_id=RUN, cutoff=CUTOFF)

    count = connection.execute(
        "select count(*) from mart.fund_virtual_company where run_id = %s and fund_id = %s", (RUN, fund_uuid)
    ).fetchone()[0]
    assert count == 1, "a retried tick restates its own row rather than accumulating"


def test_no_vintage_at_the_cutoff_writes_nothing(connection) -> None:
    _seed(connection, isin="US0000000030", ticker="TCF", weight="99", filing=LATER_FILING, period="2026-06-30")
    fund_uuid = _fund_uuid(connection)

    written = materialize_fund_consolidation(connection, run_id=RUN, cutoff=CUTOFF)

    assert all(item.fund_id != fund_uuid for item in written), (
        "a fund whose only filing postdates the cutoff is absent from the run, not zero"
    )


def _insert_core_result(
    connection,
    *,
    run_id: str,
    listing_id: str,
    valuation_gap: float,
    availability: str = "available",
    confidence: float = 0.85,
    availability_status: str | None = None,
    issuer_id: str | None = None,
) -> None:
    connection.execute("set local session_replication_role = replica")
    seed = "a" * 64
    resolved_issuer = issuer_id or f"issuer:{listing_id}"
    resolved_avail_status = availability_status or availability
    strat_run_id = f"strategy-run:{seed}"
    dec_sha = f"{abs(hash((strat_run_id, resolved_issuer, listing_id))):064x}"[:64]
    snap_id = f"topt-core-snapshot:{seed}"
    mem_sha = f"{abs(hash((snap_id, resolved_issuer, listing_id))):064x}"[:64]
    connection.execute(
        """
        insert into mart.strategy_runs (
            strategy_run_id, content_sha256, strategy_key, strategy_version,
            definition_content_sha256, corpus_sha256, claim_ceiling, executed_at
        ) values (%s, %s, 'large_model_value_v0', '1.1.0', %s, %s, 'internal_research_only', now())
        on conflict (strategy_run_id) do nothing
        """,
        (strat_run_id, seed, seed, seed),
    )
    connection.execute(
        """
        insert into mart.strategy_run_capture_bindings (capture_run_id, strategy_run_id, bound_by)
        values (%s, %s, 'tick')
        on conflict (capture_run_id, strategy_run_id) do nothing
        """,
        (run_id, strat_run_id),
    )
    connection.execute(
        """
        insert into mart.strategy_decisions (
            strategy_decision_id, content_sha256, strategy_run_id, issuer_id, cutoff_at,
            capital_adjusted_labor_efficiency, tier, current_price_to_sales, target_price_to_sales,
            valuation_gap, eligible, outcome, exclusion_reason, availability_status,
            source_evidence_status, factor_validation_status
        ) values (
            'strategy-decision:' || %s, %s, %s, %s, now(),
            1.5, 'traditional', 1.2, 1.5,
            %s, %s, %s, %s, %s,
            'verified', 'accepted'
        )
        on conflict do nothing
        """,
        (
            dec_sha,
            dec_sha,
            strat_run_id,
            resolved_issuer,
            valuation_gap,
            resolved_avail_status == "available",
            "selected" if resolved_avail_status == "available" else "excluded",
            None if resolved_avail_status == "available" else resolved_avail_status,
            resolved_avail_status,
        ),
    )
    connection.execute(
        """
        insert into mart.metric_node_values (
            run_id, issuer_id, node_id, node_key, period, period_tag,
            value, unit, availability_status, reason_codes,
            source_evidence_status, factor_validation_status,
            confidence, tree_id, tree_version, tree_sha256, input_observation_ids
        ) values (
            %s, %s, '6143cf17-ceba-5f78-a2d3-a7cc423c49a7'::uuid, 'gppe_uniform_charge_v0',
            '2026-06-30', 'FY', 1.5, 'per_employee', %s, '{}',
            'verified', 'accepted', %s,
            'ef13b940-b695-5f5f-846a-a36face5cc3e'::uuid, 'production-topt-v0.2.0', %s,
            array['obs1','obs2','obs3','obs4']
        )
        on conflict (run_id, issuer_id, node_id, period) do nothing
        """,
        (run_id, resolved_issuer, resolved_avail_status, confidence, seed),
    )
    connection.execute(
        """
        insert into staging.core_snapshots (
            snapshot_id, content_sha256, run_id, release_manifest_id,
            universe_id, universe_version, universe_sha256, cutoff,
            issuer_count, instrument_count, observation_count, payload
        ) values (%s, %s, %s, 'release-manifest:' || %s, 'universe:test', '1', %s, now(), 1, 1, 4, '{}'::jsonb)
        on conflict (snapshot_id) do nothing
        """,
        (snap_id, seed, run_id, seed, seed),
    )
    connection.execute(
        """
        insert into staging.core_snapshot_members (
            snapshot_id, instrument_id, issuer_id, listing_id,
            observation_ids, member_sha256, factor_input
        ) values (%s, %s, %s, %s, array['obs1','obs2','obs3','obs4'], %s, '{}'::jsonb)
        on conflict (snapshot_id, issuer_id) do nothing
        """,
        (snap_id, f"inst:{listing_id}", resolved_issuer, listing_id, mem_sha),
    )


def test_materialize_resolves_canonical_entity_uuid_listing_id(connection) -> None:
    """#1099: mart.topt_core_results (retired in #1730) stored canonical entity UUIDs.

    Fund consolidation joins through mart.entity_identity so canonical UUIDs resolve
    to the fund holding's listing_id and contribute to weighted valuation gap.
    """
    eid = resolve_entity(connection, "listing:xnas:tcu", "listing", as_of=CUTOFF.date(), known_at=CUTOFF)
    _seed(connection, isin="US0000000040", ticker="TCU", weight="65", filing=KNOWABLE_FILING, period="2026-06-30")
    fund_uuid = _fund_uuid(connection)
    _insert_core_result(connection, run_id=RUN, listing_id=str(eid), valuation_gap=0.42, confidence=0.85)

    written = materialize_fund_consolidation(connection, run_id=RUN, cutoff=CUTOFF)
    matched = [item for item in written if item.fund_id == fund_uuid]
    assert len(matched) == 1
    fund = matched[0]
    assert fund.valued_lines == 1
    assert fund.valued_weight == Decimal("65")
    assert fund.weighted_valuation_gap == Decimal("0.42")
    assert fund.result.confidence == Decimal("0.85")
    assert fund.result.data_availability == "verified"


def test_materialize_supports_legacy_string_listing_id(connection) -> None:
    """#1099: rows of mart.topt_core_results (retired in #1730) with legacy string listing_id still match directly."""
    _seed(connection, isin="US0000000041", ticker="TCL", weight="70", filing=KNOWABLE_FILING, period="2026-06-30")
    fund_uuid = _fund_uuid(connection)
    _insert_core_result(connection, run_id=RUN, listing_id="listing:xnas:tcl", valuation_gap=-0.15, confidence=0.90)

    written = materialize_fund_consolidation(connection, run_id=RUN, cutoff=CUTOFF)
    matched = [item for item in written if item.fund_id == fund_uuid]
    assert len(matched) == 1
    fund = matched[0]
    assert fund.valued_lines == 1
    assert fund.valued_weight == Decimal("70")
    assert fund.weighted_valuation_gap == Decimal("-0.15")
    assert fund.result.confidence == Decimal("0.90")


def test_dual_class_holding_joins_issuer_core_valuation_row(connection) -> None:
    """Two share-class holdings of the same issuer (GOOGL and GOOG) resolved to unpadded
    company:cik:1652044 in staging.entities both value against the issuer's single
    mart.topt_core_results (retired in #1730) row while keeping their own listing_ids."""
    _seed(connection, isin="US02079K3059", ticker="GOOGL", weight="40", filing=KNOWABLE_FILING, period="2026-06-30")
    _seed(connection, isin="US02079K1079", ticker="GOOG", weight="35", filing=KNOWABLE_FILING, period="2026-06-30")
    holding_uuid = er.ensure_entity(
        connection, "company:cik:1652044", "issuer", "Alphabet Inc.", transaction_time=KNOWABLE_FILING
    )
    for isin in ("US02079K3059", "US02079K1079"):
        er.assert_identifier(
            connection,
            entity_id=holding_uuid,
            source="openfigi",
            identifier_type="isin",
            identifier_value=isin,
            confidence=0.95,
            transaction_time=KNOWABLE_FILING + timedelta(seconds=1),
            valid_from=(KNOWABLE_FILING + timedelta(seconds=1)).date().isoformat(),
            raw_ref="test:dual",
        )
    issuer_uuid = resolve_entity(connection, "issuer:cik:0001652044", "issuer", as_of=CUTOFF.date(), known_at=CUTOFF)
    listing_uuid = resolve_entity(connection, "listing:xnas:goog", "listing", as_of=CUTOFF.date(), known_at=CUTOFF)
    from truealpha_runtime.mart_refresh import refresh_mart_materialized_views

    refresh_mart_materialized_views(connection)
    _insert_core_result(
        connection,
        run_id=RUN,
        listing_id=str(listing_uuid),
        valuation_gap=0.25,
        confidence=0.85,
        issuer_id=str(issuer_uuid),
    )

    fund_uuid = _fund_uuid(connection)
    written = materialize_fund_consolidation(connection, run_id=RUN, cutoff=CUTOFF)
    matched = [item for item in written if item.fund_id == fund_uuid]
    assert len(matched) == 1
    fund = matched[0]
    assert (fund.lines, fund.valued_lines) == (2, 2)
    assert fund.valued_weight == Decimal("75")
    assert fund.weighted_valuation_gap == Decimal("0.25")


def test_a_low_confidence_core_row_is_not_valued_and_does_not_zero_the_aggregate(connection) -> None:
    """#1114: the factor column `availability` stays `available` for a low-confidence core row.

    The producer reads the section 8 column `availability_status`. The low-confidence line
    leaves the valued mass, so the aggregate confidence is the minimum of the consumed lines.
    """
    _seed(connection, isin="US0000000050", ticker="TCG", weight="60", filing=KNOWABLE_FILING, period="2026-06-30")
    _seed(connection, isin="US0000000051", ticker="TCW", weight="30", filing=KNOWABLE_FILING, period="2026-06-30")
    fund_uuid = _fund_uuid(connection)
    _insert_core_result(
        connection,
        run_id=RUN,
        listing_id="listing:xnas:tcg",
        valuation_gap=0.20,
        confidence=0.85,
        availability_status="available",
    )
    _insert_core_result(
        connection,
        run_id=RUN,
        listing_id="listing:xnas:tcw",
        valuation_gap=-0.90,
        confidence=0.0,
        availability="available",
        availability_status="low_confidence",
    )

    written = materialize_fund_consolidation(connection, run_id=RUN, cutoff=CUTOFF)
    matched = [item for item in written if item.fund_id == fund_uuid]
    assert len(matched) == 1
    fund = matched[0]
    assert (fund.lines, fund.valued_lines) == (2, 1)
    assert fund.valued_weight == Decimal("60")
    assert fund.resolved_weight == Decimal("90")
    assert fund.weighted_valuation_gap == Decimal("0.20")
    assert fund.result.confidence == Decimal("0.85")
    row = connection.execute(
        "select valued_lines, confidence, availability_status from mart.fund_virtual_company "
        "where run_id = %s and fund_id = %s",
        (RUN, fund_uuid),
    ).fetchone()
    assert row == (1, Decimal("0.85"), "available")


# --- the deployed call site -------------------------------------------------------


def _run_deployed_tick(monkeypatch, op, *, spy: list) -> None:
    """Drive one deployed tick op with every collaborator faked but the consolidation
    call itself recorded — so this asserts the WIRING, not the arithmetic."""

    def _spy(connection, *, run_id, cutoff, **kwargs):
        spy.append((run_id, cutoff))
        return ()

    run_deployed_tick(monkeypatch, op, materialize=_spy)


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


def test_mart_fund_holdings_deduplicates_reparsed_holding_lines(connection) -> None:
    """#1359 AC3: when staging.fund_holding_facts retains both a legacy parse and a newer
    reparse (e.g. nport-holdings:2) for the same holding line in a vintage, mart.fund_holdings
    selects only the newest reparse so weights and lines are never double-counted."""
    fund_uuid = str(
        er.ensure_entity(
            connection,
            "etf:series:S000088434-DEDUP-TEST",
            "fund",
            "iShares Top 20 U.S. Stocks ETF",
            transaction_time=KNOWABLE_FILING,
        )
    )
    isin_uuid = er.ensure_entity(
        connection,
        "company:isin:US67066G1040",
        "instrument",
        "NVIDIA Corp.",
        transaction_time=KNOWABLE_FILING,
    )
    cik_uuid = er.ensure_entity(
        connection,
        "company:cik:1045810",
        "issuer",
        "NVIDIA Corp.",
        transaction_time=KNOWABLE_FILING,
    )
    for kind, value in (("isin", "US67066G1040"), ("ticker", "NVDA")):
        er.assert_identifier(
            connection,
            entity_id=cik_uuid,
            source="test",
            identifier_type=kind,
            identifier_value=value,
            confidence=1.0,
            transaction_time=KNOWABLE_FILING,
            valid_from=KNOWABLE_FILING.date().isoformat(),
            raw_ref="raw.fetches:217",
        )
    # Simulate the deployed staging index that permitted a second reparse row with a new id.
    connection.execute("drop index if exists staging.uq_fund_holding_line_vintage")
    for holding_uuid, raw_ref in (
        (isin_uuid, "raw.fetches:216"),
        (cik_uuid, "raw.fetches:217"),
    ):
        connection.execute(
            """
            insert into staging.fund_holding_facts
                (fund_id, holding_id, holding_name, report_period, transaction_time,
                 isin, cusip, balance, value_usd, percent_of_net_assets, confidence, raw_ref)
            values (%s, %s, 'NVIDIA Corp.', '2026-03-31', %s, 'US67066G1040', '67066G104', 100, 160127, 16.01272878741, 1.0, %s)
            """,
            (fund_uuid, holding_uuid, KNOWABLE_FILING, raw_ref),
        )
    from truealpha_runtime.mart_refresh import MART_FUND_HOLDINGS_MV_ORDER, refresh_mart_materialized_views

    refresh_mart_materialized_views(connection, MART_FUND_HOLDINGS_MV_ORDER)

    rows = connection.execute(
        "select holding_id, percent_of_net_assets, raw_ref from mart.fund_holdings where fund_id = %s",
        (fund_uuid,),
    ).fetchall()
    assert rows == [(str(cik_uuid), Decimal("16.01272878741"), "raw.fetches:217")]

    coverage = connection.execute(
        "select lines, resolved_lines, total_weight_pct, resolved_weight_pct from mart.fund_holdings_coverage where fund_id = %s",
        (fund_uuid,),
    ).fetchone()
    assert coverage == (1, 1, Decimal("16.01272878741"), Decimal("16.01272878741"))


def _apply_issue_1641_migration(connection) -> None:
    """Apply the #1641 migration inside the rolled-back test transaction."""
    migration = Path(__file__).resolve().parents[4] / "db/migrations/0003_mart.sql"
    connection.execute(migration.read_text(encoding="utf-8"))


def test_lines_sql_bounds_core_and_entity_identity_joins_to_one_row() -> None:
    """#1641: _LINES_SQL uses LATERAL LIMIT 1 joins so core results and entity_identity never fan out."""
    normalized = " ".join(_LINES_SQL.split()).lower()
    assert "left join lateral (" in normalized
    assert ") mnv on true" in normalized
    assert ") core on true" in normalized
    assert normalized.count("from mart.entity_identity ei_row") == 2
    assert normalized.count("limit 1") == 5
    assert "order by (coalesce(ei.listing_id, cil.listing_id) = h.listing_id) desc" in normalized


def test_dual_kg_entities_and_dual_listings_preserve_weight_conservation(connection) -> None:
    """#1641: multiple issuer entities and dual CIK core rows must not fan out holding weights."""
    _apply_issue_1641_migration(connection)
    _seed(connection, isin="US02079K3059", ticker="GOOGL", weight="40", filing=KNOWABLE_FILING, period="2026-06-30")
    _seed(connection, isin="US02079K1079", ticker="GOOG", weight="35", filing=KNOWABLE_FILING, period="2026-06-30")
    fund_uuid = _fund_uuid(connection)
    holding_company_uuid = er.ensure_entity(
        connection,
        "company:cik:1652044",
        "issuer",
        "Alphabet Inc.",
        transaction_time=KNOWABLE_FILING,
    )
    for isin in ("US02079K3059", "US02079K1079"):
        er.assert_identifier(
            connection,
            entity_id=holding_company_uuid,
            source="openfigi",
            identifier_type="isin",
            identifier_value=isin,
            confidence=0.95,
            transaction_time=KNOWABLE_FILING + timedelta(seconds=1),
            valid_from=(KNOWABLE_FILING + timedelta(seconds=1)).date().isoformat(),
            raw_ref="test:1641",
        )
    issuer_uuid = resolve_entity(connection, "issuer:cik:0001652044", "issuer", as_of=CUTOFF.date(), known_at=CUTOFF)
    listing_goog = resolve_entity(connection, "listing:xnas:goog", "listing", as_of=CUTOFF.date(), known_at=CUTOFF)
    listing_googl = resolve_entity(connection, "listing:xnas:googl", "listing", as_of=CUTOFF.date(), known_at=CUTOFF)
    from truealpha_runtime.mart_refresh import refresh_mart_materialized_views

    refresh_mart_materialized_views(connection)

    for entity_uuid in (issuer_uuid, listing_goog, listing_googl):
        count = connection.execute(
            "select count(*) from mart.entity_identity where entity_id = %s",
            (entity_uuid,),
        ).fetchone()[0]
        assert count == 1

    _insert_core_result(
        connection,
        run_id=RUN,
        listing_id=str(listing_goog),
        valuation_gap=0.20,
        confidence=0.85,
        issuer_id=str(issuer_uuid),
    )

    written = materialize_fund_consolidation(connection, run_id=RUN, cutoff=CUTOFF)
    matched = [item for item in written if item.fund_id == fund_uuid]
    assert len(matched) == 1
    fund = matched[0]
    assert (fund.lines, fund.valued_lines) == (2, 2)
    assert fund.total_weight == Decimal("75")
    assert fund.resolved_weight == Decimal("75")
    assert fund.valued_weight == Decimal("75")


def test_the_table_refuses_total_weight_above_conservation_ceiling(connection) -> None:
    """#1641: mart.fund_virtual_company rejects rows with total_weight_pct above 105."""
    _apply_issue_1641_migration(connection)
    with pytest.raises(psycopg.errors.CheckViolation, match="chk_fund_virtual_company_weight_conservation"):
        connection.execute(
            """
            insert into mart.fund_virtual_company (run_id, fund_id, report_period, transaction_time, cutoff,
                definition_version, definition_sha256, weighted_valuation_gap, valuation_basis,
                total_weight_pct, resolved_weight_pct, valued_weight_pct, lines, valued_lines, confidence,
                availability_status, source_evidence_status, factor_validation_status)
            values (%s, %s, '2026-06-30', %s, %s, 'v0', %s, null, null,
                198.2, 198.2, 0, 2, 0, 0, 'unavailable', 'degraded', 'not_evaluated')
            """,
            (RUN, FUND, KNOWABLE_FILING, CUTOFF, "a" * 64),
        )
