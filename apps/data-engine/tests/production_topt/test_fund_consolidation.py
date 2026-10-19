"""The module-5 fund consolidation, at the deployed call site (#727, #36).

Two halves, because a passing factor test proves the arithmetic and not the wiring
(AGENTS.md rule 7: "a criterion must be able to fail where production calls"):

- against a real database, `materialize_fund_consolidation` selects the vintage knowable
  at the cutoff, joins the run's core rows and writes a `mart.fund_virtual_company` row;
- against the deployed op, the QQQ tick invokes it and the TOPT tick does not — the flag
  on the declaration is what production reads, so that is what is asserted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import dagster as dg
import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.production_topt.fund_consolidation import (
    load_fund_vintages,
    materialize_fund_consolidation,
)
from data_engine.datahub.resolve_coordinates import resolve_entity
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
    # #1235: a refused aggregate has no gap, so it names no valuation basis.
    basis = connection.execute(
        "select valuation_basis from mart.fund_virtual_company where run_id = %s and fund_id = %s", (RUN, FUND)
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


def _insert_core_result(
    connection,
    *,
    run_id: str,
    listing_id: str,
    valuation_gap: float,
    availability: str = "available",
    confidence: float = 0.85,
    availability_status: str | None = None,
) -> None:
    connection.execute("set local session_replication_role = replica")
    res_sha = f"{abs(hash((run_id, listing_id))):064x}"[:64]
    connection.execute(
        """
        insert into mart.topt_core_results (
            result_id, content_sha256, invocation_id, snapshot_id, run_id,
            release_manifest_id, universe_id, universe_version, universe_sha256,
            cutoff, issuer_id, instrument_id, listing_id, operating_branch,
            operating_metric, availability, operating_efficiency,
            capital_adjusted_gross_profit, gppe, tier, target_ps_lower,
            target_ps_upper, target_ps_midpoint, current_ps, valuation_gap,
            confidence, freshness, reason_codes, input_observation_ids,
            gppe_invocation_id, gppe_result_id,
            gppe_definition_id, gppe_definition_sha256,
            tier_definition_id, tier_definition_sha256, payload, availability_status
        ) values (
            'topt-core-result:' || %s, %s,
            'topt-core-invocation:' || repeat('b', 64),
            'topt-core-snapshot:' || repeat('c', 64),
            %s,
            'release-manifest:' || repeat('d', 64),
            'universe:test', '1', repeat('e', 64),
            now(), 'issuer:' || %s, 'inst:test', %s,
            'non_financial', 'capital_adjusted_gppe', %s,
            100, 100, 1.5, 'tech', 1.0, 2.0, 1.5, 1.2, %s,
            %s, 'fresh', '{}', array['obs1','obs2','obs3','obs4'],
            'gppe-inv:' || repeat('f', 64), 'gppe-res:' || repeat('0', 64),
            'gppe-def:v0', repeat('1', 64),
            'tier-def:v0', repeat('2', 64),
            '{}'::jsonb, %s
        )
        on conflict (result_id) do nothing
        """,
        (
            res_sha,
            res_sha,
            run_id,
            listing_id,
            listing_id,
            availability,
            valuation_gap,
            confidence,
            availability_status,
        ),
    )


def test_materialize_resolves_canonical_entity_uuid_listing_id(connection) -> None:
    """#1099: mart.topt_core_results stores canonical entity UUIDs.

    Fund consolidation joins through mart.entity_identity so canonical UUIDs resolve
    to the fund holding's listing_id and contribute to weighted valuation gap.
    """
    eid = resolve_entity(connection, "listing:xnas:tcu", "listing", as_of=CUTOFF.date(), known_at=CUTOFF)
    _seed(connection, isin="US0000000040", ticker="TCU", weight="65", filing=KNOWABLE_FILING, period="2026-06-30")
    _insert_core_result(connection, run_id=RUN, listing_id=str(eid), valuation_gap=0.42, confidence=0.85)

    written = materialize_fund_consolidation(connection, run_id=RUN, cutoff=CUTOFF)
    matched = [item for item in written if item.fund_id == FUND]
    assert len(matched) == 1
    fund = matched[0]
    assert fund.valued_lines == 1
    assert fund.valued_weight == Decimal("65")
    assert fund.weighted_valuation_gap == Decimal("0.42")
    assert fund.result.confidence == Decimal("0.85")
    assert fund.result.data_availability == "verified"


def test_materialize_supports_legacy_string_listing_id(connection) -> None:
    """#1099: mart.topt_core_results rows with legacy string listing_id still match directly."""
    _seed(connection, isin="US0000000041", ticker="TCL", weight="70", filing=KNOWABLE_FILING, period="2026-06-30")
    _insert_core_result(connection, run_id=RUN, listing_id="listing:xnas:tcl", valuation_gap=-0.15, confidence=0.90)

    written = materialize_fund_consolidation(connection, run_id=RUN, cutoff=CUTOFF)
    matched = [item for item in written if item.fund_id == FUND]
    assert len(matched) == 1
    fund = matched[0]
    assert fund.valued_lines == 1
    assert fund.valued_weight == Decimal("70")
    assert fund.weighted_valuation_gap == Decimal("-0.15")
    assert fund.result.confidence == Decimal("0.90")


def test_dual_class_holding_joins_issuer_core_valuation_row(connection) -> None:
    """Two share-class holdings of the same issuer (GOOGL and GOOG) resolved to unpadded
    company:cik:1652044 in staging.kg_identifiers both value against the issuer's single
    mart.topt_core_results row (issuer:cik:0001652044) while keeping their own listing_ids."""
    _seed(connection, isin="US02079K3059", ticker="GOOGL", weight="40", filing=KNOWABLE_FILING, period="2026-06-30")
    _seed(connection, isin="US02079K1079", ticker="GOOG", weight="35", filing=KNOWABLE_FILING, period="2026-06-30")
    holding_company_cik = "company:cik:1652044"
    connection.execute(
        "insert into staging.kg_entities (id, entity_type, display_name) values (%s, 'company', 'Alphabet Inc.') "
        "on conflict (id) do nothing",
        (holding_company_cik,),
    )
    for isin in ("US02079K3059", "US02079K1079"):
        connection.execute(
            "insert into staging.kg_identifiers (entity_id, identifier_type, identifier_value, "
            "valid_time, transaction_time, confidence, source, raw_ref) "
            "values (%s, 'isin', %s, daterange(%s, null), %s, 0.95, 'openfigi', 'test:dual') "
            "on conflict do nothing",
            (
                holding_company_cik,
                isin,
                (KNOWABLE_FILING + timedelta(seconds=1)).date(),
                KNOWABLE_FILING + timedelta(seconds=1),
            ),
        )
    issuer_uuid = resolve_entity(connection, "issuer:cik:0001652044", "issuer", as_of=CUTOFF.date(), known_at=CUTOFF)
    listing_uuid = resolve_entity(connection, "listing:xnas:goog", "listing", as_of=CUTOFF.date(), known_at=CUTOFF)
    connection.execute("set local session_replication_role = replica")
    res_sha = f"{abs(hash((RUN, str(issuer_uuid)))):064x}"[:64]
    connection.execute(
        """
        insert into mart.topt_core_results (
            result_id, content_sha256, invocation_id, snapshot_id, run_id,
            release_manifest_id, universe_id, universe_version, universe_sha256,
            cutoff, issuer_id, instrument_id, listing_id, operating_branch,
            operating_metric, availability, operating_efficiency,
            capital_adjusted_gross_profit, gppe, tier, target_ps_lower,
            target_ps_upper, target_ps_midpoint, current_ps, valuation_gap,
            confidence, freshness, reason_codes, input_observation_ids,
            gppe_invocation_id, gppe_result_id,
            gppe_definition_id, gppe_definition_sha256,
            tier_definition_id, tier_definition_sha256, payload, availability_status
        ) values (
            'topt-core-result:' || %s, %s,
            'topt-core-invocation:' || repeat('b', 64),
            'topt-core-snapshot:' || repeat('c', 64),
            %s,
            'release-manifest:' || repeat('d', 64),
            'universe:test', '1', repeat('e', 64),
            now(), %s, 'inst:test', %s,
            'non_financial', 'capital_adjusted_gppe', 'available',
            100, 100, 1.5, 'tech', 1.0, 2.0, 1.5, 1.2, 0.25,
            0.85, 'fresh', '{}', array['obs1','obs2','obs3','obs4'],
            'gppe-inv:' || repeat('f', 64), 'gppe-res:' || repeat('0', 64),
            'gppe-def:v0', repeat('1', 64),
            'tier-def:v0', repeat('2', 64),
            '{}'::jsonb, 'available'
        )
        """,
        (res_sha, res_sha, RUN, str(issuer_uuid), str(listing_uuid)),
    )

    written = materialize_fund_consolidation(connection, run_id=RUN, cutoff=CUTOFF)
    matched = [item for item in written if item.fund_id == FUND]
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
    matched = [item for item in written if item.fund_id == FUND]
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
        (RUN, FUND),
    ).fetchone()
    assert row == (1, Decimal("0.85"), "available")


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


def test_mart_fund_holdings_deduplicates_reparsed_holding_lines(connection) -> None:
    """#1359 AC3: when staging.fund_holding_facts retains both a legacy parse and a newer
    reparse (e.g. nport-holdings:2) for the same holding line in a vintage, mart.fund_holdings
    selects only the newest reparse so weights and lines are never double-counted."""
    fund_id = "etf:series:S000088434-DEDUP-TEST"
    connection.execute(
        "insert into staging.kg_entities (id, entity_type, display_name) values (%s, 'etf', 'iShares Top 20 U.S. Stocks ETF') "
        "on conflict (id) do nothing",
        (fund_id,),
    )
    for entity_id in ("company:isin:US67066G1040", "company:cik:1045810"):
        connection.execute(
            "insert into staging.kg_entities (id, entity_type, display_name) values (%s, 'company', 'NVIDIA Corp.') "
            "on conflict (id) do nothing",
            (entity_id,),
        )
    for kind, value in (("isin", "US67066G1040"), ("ticker", "NVDA")):
        connection.execute(
            "insert into staging.kg_identifiers (entity_id, identifier_type, identifier_value, "
            "valid_time, transaction_time, confidence, source, raw_ref) "
            "values ('company:cik:1045810', %s, %s, daterange(%s, null), %s, 1.0, 'test', 'raw.fetches:217') "
            "on conflict do nothing",
            (kind, value, KNOWABLE_FILING.date(), KNOWABLE_FILING),
        )
    # Simulate the deployed staging index that permitted a second reparse row with a new id.
    connection.execute("drop index if exists staging.uq_fund_holding_line_vintage")
    for holding_id, raw_ref in (
        ("company:isin:US67066G1040", "raw.fetches:216"),
        ("company:cik:1045810", "raw.fetches:217"),
    ):
        connection.execute(
            """
            insert into staging.fund_holding_facts
                (fund_id, holding_id, holding_name, report_period, transaction_time,
                 isin, cusip, balance, value_usd, percent_of_net_assets, confidence, raw_ref)
            values (%s, %s, 'NVIDIA Corp.', '2026-03-31', %s, 'US67066G1040', '67066G104', 100, 160127, 16.01272878741, 1.0, %s)
            """,
            (fund_id, holding_id, KNOWABLE_FILING, raw_ref),
        )

    rows = connection.execute(
        "select holding_id, percent_of_net_assets, raw_ref from mart.fund_holdings where fund_id = %s",
        (fund_id,),
    ).fetchall()
    assert rows == [("company:cik:1045810", Decimal("16.01272878741"), "raw.fetches:217")]

    coverage = connection.execute(
        "select lines, resolved_lines, total_weight_pct, resolved_weight_pct from mart.fund_holdings_coverage where fund_id = %s",
        (fund_id,),
    ).fetchone()
    assert coverage == (1, 1, Decimal("16.01272878741"), Decimal("16.01272878741"))
