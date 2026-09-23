"""Cross-run observation reuse and boundary tests (#635, #684, #788, #877, #885).

Split from test_degraded_capture_record.py per #906 to eliminate the monolithic CI wall.
These tests exercise observation reuse, coordinate equality, look-ahead bounds, parser vintage
rules, whole-bound-set consistency, timezone independence, and multi-universe boundaries.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import psycopg
import pytest
from data_engine import raw_store
from data_engine.config import settings
from data_engine.datahub.production_topt import composition, twelve_data_origin
from data_engine.datahub.production_topt.composition import (
    PlannedRun,
    run_topt_pipeline,
)
from data_engine.datahub.production_topt.executor import (
    SourceFetchPort,
)
from data_engine.datahub.production_topt.headcount import PostgresHeadcountExtractor, record_headcount
from data_engine.datahub.production_topt.market_price_adapter import (
    CorroboratingOrigin,
    MarketPriceAdapter,
    MarketPriceQuote,
    MarketPriceTarget,
)
from data_engine.datahub.production_topt.release_derived_adapter import ReleaseDerivedAdapter, ReleaseDerivedRecord
from data_engine.datahub.production_topt.sec_financial_adapter import (
    FinancialFactsBundle,
    SecFinancialFactAdapter,
    SecTarget,
)
from factors.production_topt import OperatingBranch
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from truealpha_contracts.models import DataSource, RawCapture, RawIngestionEnvelope, RawObjectRef
from truealpha_runtime.testing import apply_migration_chain

CUTOFF = datetime(2026, 4, 2, tzinfo=UTC)
OBLIGATIONS = 84
_RELEASE_OBLIGATIONS = 42
_BANK_TICKER = "JPM"
_REUSE_CUTOFF = datetime(2026, 3, 31, 22, 15, tzinfo=UTC)
_STRATEGY = "large_model_value_v0"

_MAIN_DECISIONS_SQL = """
    select d.issuer_id, t.confidence, t.run_id
    from mart.strategy_decisions d
    left join mart.topt_core_results t
      on t.issuer_id = d.issuer_id and t.cutoff = d.cutoff_at
    where d.strategy_run_id = %s
    order by d.cutoff_at, d.issuer_id
"""
_MAIN_GATE_ROWS_SQL = """
    select r.listing_id, p.value as last_close
    from mart.topt_core_results r
    left join staging.strategy_backtest_inputs p
      on p.issuer_id = r.issuer_id and p.cutoff_at = r.cutoff and p.input_key = 'last_close'
    where r.run_id = %s
    order by r.listing_id
"""


@pytest.fixture(scope="module")
def tick_database_url():
    parameters = conninfo_to_dict(settings.database_url)
    database_name = f"truealpha_reuse_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    admin_url = make_conninfo(**(parameters | {"dbname": "postgres"}))
    target_url = make_conninfo(**(parameters | {"dbname": database_name}))
    try:
        with psycopg.connect(admin_url, connect_timeout=3, autocommit=True) as admin:
            admin.execute(sql.SQL("create database {}").format(sql.Identifier(database_name)))
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        # The one applier, not a fourth copy of its loop (#984).
        apply_migration_chain(target_url)
        yield target_url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity where datname = %s",
                (database_name,),
            )
            admin.execute(sql.SQL("drop database if exists {}").format(sql.Identifier(database_name)))


class _InMemoryObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def store(self, capture: RawCapture) -> RawIngestionEnvelope:
        digest = hashlib.sha256(capture.body).hexdigest()
        key = f"raw/{capture.source.value}/{digest[:2]}/{digest}"
        self.objects[key] = capture.body
        return RawIngestionEnvelope(
            source=capture.source,
            source_record_id=capture.source_record_id,
            object=RawObjectRef(
                bucket="truealpha-raw",
                key=key,
                sha256=digest,
                byte_length=len(capture.body),
                content_type=capture.content_type,
            ),
            fetched_at=capture.fetched_at,
            source_published_at=capture.source_published_at,
            metadata=capture.metadata,
        )

    def get(self, ref: RawObjectRef) -> bytes:
        return self.objects[ref.key]


def _quote(day: date = date(2026, 3, 31), close: Decimal = Decimal("40")) -> MarketPriceQuote:
    return MarketPriceQuote(
        raw_bytes=f"bar:{day.isoformat()}:{close}".encode(),
        close=close,
        as_of=day,
        knowable_at=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
    )


def _bundle(branch: OperatingBranch) -> FinancialFactsBundle:
    financial = branch is OperatingBranch.FINANCIAL
    return FinancialFactsBundle(
        gross_profit=Decimal("80000000") if financial else Decimal("210000000"),
        total_assets=Decimal("200000000"),
        shares_outstanding=Decimal("10000000"),
        revenue=Decimal("100000000"),
        pre_provision_profit=Decimal("80000000") if financial else None,
        raw_bytes=b'{"facts":{}}',
        knowable_at=datetime(2026, 2, 1, tzinfo=UTC),
    )


def _offline_routes(
    plan: PlannedRun,
    connection,
    *,
    quote: Callable[[], MarketPriceQuote] = _quote,
    price_cutoff: date | None = None,
    corroborating_origins: tuple[CorroboratingOrigin, ...] = (),
    cutoff_date: date | None = None,
) -> dict[str, SourceFetchPort]:
    cutoff_date = cutoff_date or CUTOFF.date()
    price_targets: dict[str, MarketPriceTarget] = {}
    sec_targets: dict[str, SecTarget] = {}
    release_targets: dict[str, ReleaseDerivedRecord] = {}
    cik_by_ticker: dict[str, int] = {}
    for work_item_id, binding in plan.bindings.items():
        semantic_type = binding.obligation.capture_requirement_id.removesuffix(":v1")
        subject_id = binding.obligation.subject.id
        issuer_id, instrument_id, listing_id, ticker = plan.coordinates[subject_id]
        cik_by_ticker.setdefault(ticker, 100000 + sorted(plan.coordinates).index(subject_id))
        if semantic_type == "market-price":
            price_targets[work_item_id] = MarketPriceTarget(
                symbol=ticker,
                cutoff=price_cutoff or cutoff_date,
                issuer_id=issuer_id,
                instrument_id=instrument_id,
                listing_id=listing_id,
            )
        elif semantic_type == "financial-fact":
            sec_targets[work_item_id] = SecTarget(
                cik=cik_by_ticker[ticker],
                cutoff=cutoff_date,
                issuer_id=issuer_id,
                instrument_id=instrument_id,
                listing_id=listing_id,
                operating_branch=(
                    OperatingBranch.FINANCIAL if ticker == _BANK_TICKER else OperatingBranch.NON_FINANCIAL
                ),
            )
        else:
            release_targets[work_item_id] = ReleaseDerivedRecord(
                semantic_type=semantic_type,
                subject_id=listing_id,
                payload={
                    "issuer_id": issuer_id,
                    "instrument_id": instrument_id,
                    "listing_id": listing_id,
                    "ticker": ticker,
                },
                knowable_at=plan.timeline.partition_start,
            )

    for ticker, cik in sorted(cik_by_ticker.items()):
        record_headcount(
            connection,
            cik=cik,
            headcount=Decimal("164000"),
            knowable_at=datetime(2026, 1, 1, tzinfo=UTC),
            source="test-fixture",
            evidence_ref=f"test:{ticker}",
            confidence=Decimal("0.7"),
        )

    price = MarketPriceAdapter(
        price_targets,
        lambda symbol, cutoff: quote(),
        corroborating_origins=corroborating_origins,
    )
    financial = SecFinancialFactAdapter(
        sec_targets,
        lambda cik, cutoff, branch: _bundle(branch),
        headcount_extractor=PostgresHeadcountExtractor(connection),
    )
    release = ReleaseDerivedAdapter(release_targets, cutoff=cutoff_date)

    routes: dict[str, SourceFetchPort] = {}
    routes.update(dict.fromkeys(price_targets, price))
    routes.update(dict.fromkeys(sec_targets, financial))
    routes.update(dict.fromkeys(release_targets, release))
    return routes


_NO_CONFIGURED_ORIGINS: dict[str, object] = {
    "twelve_data_api_key": "",
    "moomoo_kline_origin_enabled": False,
    "moomoo_financials_origin_enabled": False,
}

_TWELVE_DATA_CONFIGURED: dict[str, object] = {
    **_NO_CONFIGURED_ORIGINS,
    "twelve_data_api_key": "test-key-configured",
}


def _arm(
    monkeypatch,
    *,
    origin_settings: dict[str, object] | None = None,
    **route_options,
) -> None:
    for name, value in {**_NO_CONFIGURED_ORIGINS, **(origin_settings or {})}.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(raw_store, "object_store", _InMemoryObjectStore)

    def build(plan: PlannedRun, connection=None) -> dict[str, SourceFetchPort]:
        return _offline_routes(plan, connection, **route_options)

    monkeypatch.setattr(composition, "build_routes", build)


def _run_tick(url: str, *, version: str, cutoff: datetime = CUTOFF, force_fetch: bool = False):
    with psycopg.connect(url) as tick:
        result = run_topt_pipeline(tick, cutoff=cutoff, version=version, force_fetch=force_fetch)
        tick.commit()
        return result


def _status_row(url: str, run_id: str):
    with psycopg.connect(url) as reader:
        return reader.execute(
            """
            select obligation_count, terminal_count, success_count, unchanged_count,
                   unavailable_count, skipped_count, failed_count, complete
            from mart.topt_capture_status where run_id = %s
            """,
            (run_id,),
        ).fetchone()


def _materialized(url: str, run_id: str) -> tuple[int, int, int]:
    with psycopg.connect(url) as reader:
        snapshots = reader.execute(
            "select count(*) from staging.topt_core_snapshots where run_id = %s", (run_id,)
        ).fetchone()[0]
        gppe = reader.execute("select count(*) from mart.topt_gppe_results where run_id = %s", (run_id,)).fetchone()[0]
        core = reader.execute("select count(*) from mart.topt_core_results where run_id = %s", (run_id,)).fetchone()[0]
    return snapshots, gppe, core


def _context():
    from truealpha_contracts.access import AccessContext, AuthenticationMethod, PrincipalKind

    issued = datetime(2026, 9, 16, tzinfo=UTC)
    return AccessContext(
        context_id="ctx:run-scope",
        principal_id="principal:run-scope",
        tenant_id="tenant:run-scope",
        session_id="session:run-scope",
        authentication_method=AuthenticationMethod.SERVICE_IDENTITY,
        principal_kind=PrincipalKind.SERVICE,
        issued_at=issued,
        expires_at=issued + timedelta(hours=1),
    )


def _served_report(url: str):
    from truealpha_contracts.strategy_run import StrategyRunReport
    from truealpha_contracts.strategy_run_postgres import PostgresStrategyRunRepository

    report = PostgresStrategyRunRepository(database_url=url).get_latest(strategy_id=_STRATEGY, context=_context())
    assert isinstance(report, StrategyRunReport), report
    return report


def _core_confidence(reader, run_id: str) -> dict[str, Decimal]:
    return dict(
        reader.execute(
            "select issuer_id, confidence from mart.topt_core_results where run_id = %s", (run_id,)
        ).fetchall()
    )


def _gate_closes(reader, sql: str, run_id: str) -> list[tuple[str, Decimal | None]]:
    return [(str(listing), close) for listing, close in reader.execute(sql, (run_id,)).fetchall()]


def _live_topt_tick(url: str, monkeypatch, *, executed_at: datetime, force_fetch: bool = False, accept: bool) -> dict:
    import dagster as dg
    from data_engine.datahub import a1_evidence
    from data_engine.lanes.capture import ToptLiveTickConfig, run_topt_live_tick

    monkeypatch.setattr(settings, "database_url", url)
    unmet = (
        () if accept else (a1_evidence.UnmetObjective(objective="corroborated_share", required="0.95", observed="0"),)
    )
    monkeypatch.setattr(a1_evidence, "unmet_objectives", lambda *_args, **_kwargs: unmet)
    context = dg.build_op_context()
    run_topt_live_tick(context, ToptLiveTickConfig(executed_at=executed_at.isoformat(), force_fetch=force_fetch))
    metadata = context.get_output_metadata("result")
    assert metadata["pointer_advanced"] is accept
    assert metadata["forced_fetch"] is force_fetch
    return metadata


def _key_topt_like_the_planes(monkeypatch) -> dict[str, tuple[str, str]]:
    from data_engine.datahub.production_topt.universe_corpus import load_corpus

    plane = {
        str(row[2]): (str(row[0]), str(row[1]))
        for row in load_corpus("corpus.qqq.v1.json")["topt_denominator"]["instruments"]
    }
    topt = {str(row[2]) for row in load_corpus("corpus.v1.json")["topt_denominator"]["instruments"]}
    shared = {listing: ids for listing, ids in plane.items() if listing in topt}
    real = composition.plan_and_persist

    def plan_and_persist(connection, **kwargs):
        planned = real(connection, **kwargs)
        if kwargs.get("corpus_filename", "corpus.v1.json") != "corpus.v1.json":
            for subject, coord in planned.coordinates.items():
                plane[subject] = (coord[0], coord[1])
                if subject in shared:
                    shared[subject] = (coord[0], coord[1])
            return planned
        return dataclasses.replace(
            planned,
            coordinates={
                subject: (*plane.get(subject, (issuer, instrument)), listing, ticker)
                for subject, (issuer, instrument, listing, ticker) in planned.coordinates.items()
            },
        )

    monkeypatch.setattr(composition, "plan_and_persist", plan_and_persist)
    return shared


def test_a_second_run_reuses_committed_observations_without_vendor_calls(tick_database_url, monkeypatch) -> None:
    """#635 as amended by #684: the 13 TOPT∩QQQ overlap names were fetched once per
    universe per day — VENDOR semantics reuse those observations, every terminal
    UNCHANGED with the reused primary vintage and both price origins re-bound.
    Release-derived semantics are the run's own identity and must NOT ride reuse
    (reusing them imported a foreign corpus's issuer keying, #684): they execute
    fresh, from this run's own coordinates, on every run."""
    _arm(monkeypatch)
    reuse_cutoff = datetime(2026, 3, 31, 22, 15, tzinfo=UTC)
    first = _run_tick(tick_database_url, version="reuse-source", cutoff=reuse_cutoff)
    second = _run_tick(tick_database_url, version="reuse-target", cutoff=reuse_cutoff)

    assert second.run_id != first.run_id
    assert _status_row(tick_database_url, second.run_id) == (
        OBLIGATIONS,
        OBLIGATIONS,
        _RELEASE_OBLIGATIONS,
        OBLIGATIONS - _RELEASE_OBLIGATIONS,
        0,
        0,
        0,
        True,
    )
    with psycopg.connect(tick_database_url) as reader:
        foreign_identity = reader.execute(
            """
            select count(*)
            from raw.capture_obligations ob
            join raw.capture_obligation_results done on done.capture_obligation_id = ob.obligation_id
            join raw.capture_attempt_results attempt on attempt.attempt_id = done.final_attempt_id
            where ob.run_id = %s
              and regexp_replace(ob.capture_requirement_id, ':v1$', '') in ('listing-identity', 'universe-membership')
              and (done.terminal_state <> 'success' or attempt.reused_source_vintage_id is not null)
            """,
            (second.run_id,),
        ).fetchone()[0]
    assert foreign_identity == 0
    snapshots, gppe, core = _materialized(tick_database_url, second.run_id)
    assert (snapshots, gppe, core) == (1, 20, 20)


def test_reuse_never_looks_ahead_of_its_own_cutoff(tick_database_url, monkeypatch) -> None:
    """Review on #664: a run completed AFTER this run's cutoff must not satisfy it —
    reuse without an upper bound would be a look-ahead violation. The earlier tick
    here finds only future evidence and captures fresh (every terminal SUCCESS)."""
    _arm(monkeypatch)
    future_cutoff = datetime(2026, 4, 9, 22, 15, tzinfo=UTC)
    _run_tick(tick_database_url, version="lookahead-source", cutoff=future_cutoff)

    earlier_cutoff = future_cutoff - timedelta(hours=2)
    result = _run_tick(tick_database_url, version="lookahead-target", cutoff=earlier_cutoff)
    assert _status_row(tick_database_url, result.run_id) == (
        OBLIGATIONS,
        OBLIGATIONS,
        OBLIGATIONS,
        0,
        0,
        0,
        0,
        True,
    )


def test_a_fully_reused_run_still_exists_on_the_evidence_plane(tick_database_url, monkeypatch) -> None:
    """The first LIVE full-reuse canary failed at publish: the capture executor is
    what appends the run's evidence node, a fully reused run skips the executor,
    and binding the release manifest to a missing node is an FK violation. The
    skip path now appends the node itself."""
    _arm(monkeypatch)
    reuse_cutoff = datetime(2026, 3, 31, 22, 15, tzinfo=UTC)
    _run_tick(tick_database_url, version="evidence-source", cutoff=reuse_cutoff)
    second = _run_tick(tick_database_url, version="evidence-target", cutoff=reuse_cutoff)
    assert _status_row(tick_database_url, second.run_id) == (
        OBLIGATIONS,
        OBLIGATIONS,
        _RELEASE_OBLIGATIONS,
        OBLIGATIONS - _RELEASE_OBLIGATIONS,
        0,
        0,
        0,
        True,
    )
    with psycopg.connect(tick_database_url) as reader:
        node = reader.execute(
            "select count(*) from staging.evidence_nodes where node_id = %s", (second.run_id,)
        ).fetchone()[0]
    assert node == 1


def test_reuse_requires_identity_coordinate_equality(tick_database_url, monkeypatch) -> None:
    """#684's second half: every normalized payload embeds the capturing run's
    (issuer, instrument, listing) trio, keyed by THAT run's corpus. A run whose
    plan keys the same subject differently (TOPT's LEI vs the planes' CIK) must
    capture fresh — reusing the foreign trio either mis-keys mart (pre-fix) or
    trips the snapshot's payload-agreement check (the live canary FAILURE this
    encodes). Same-keyed subjects keep sharing vendor bytes."""
    _arm(monkeypatch)
    cutoff = datetime(2026, 3, 31, 22, 15, tzinfo=UTC)
    _run_tick(tick_database_url, version="coordinate-source", cutoff=cutoff)

    probe = psycopg.connect(tick_database_url)
    try:
        plan = composition.plan_and_persist(probe, cutoff=cutoff, version="coordinate-target")
        victim = sorted(plan.coordinates)[0]
        _issuer_id, instrument_id, listing_id, ticker = plan.coordinates[victim]
        patched = dataclasses.replace(
            plan,
            coordinates={
                **plan.coordinates,
                victim: ("issuer:cik:0000999999", instrument_id, listing_id, ticker),
            },
        )
        satisfied = composition._satisfy_from_recent_observations(probe, patched, cutoff=cutoff)

        reused_by_subject: dict[str, dict[str, bool]] = {}
        for work_item_id, binding in patched.bindings.items():
            semantic = binding.obligation.capture_requirement_id.removesuffix(":v1")
            cells = reused_by_subject.setdefault(binding.obligation.subject.id, {})
            cells[semantic] = work_item_id in satisfied
        assert reused_by_subject[victim]["market-price"] is False
        assert reused_by_subject[victim]["financial-fact"] is False
        others = [subject for subject in reused_by_subject if subject != victim]
        assert others, "the corpus has more than one subject"
        assert all(reused_by_subject[s]["market-price"] and reused_by_subject[s]["financial-fact"] for s in others)
        assert not any(
            cells.get("listing-identity") or cells.get("universe-membership") for cells in reused_by_subject.values()
        )
    finally:
        probe.rollback()
        probe.close()


def test_reuse_requires_parser_vintage_equality(tick_database_url, monkeypatch) -> None:
    """#788: an anchor parsed by a different primary vintage must not satisfy an obligation."""
    _arm(monkeypatch)
    cutoff = datetime(2026, 3, 31, 22, 15, tzinfo=UTC)
    _run_tick(tick_database_url, version="parser-vintage-source", cutoff=cutoff)

    probe = psycopg.connect(tick_database_url)
    try:
        plan = composition.plan_and_persist(probe, cutoff=cutoff, version="parser-vintage-target")
        same_vintage = composition._satisfy_from_recent_observations(probe, plan, cutoff=cutoff)
        assert same_vintage, "an unbumped parser must still reuse (#635 is not disabled)"
        probe.rollback()

        plan = composition.plan_and_persist(probe, cutoff=cutoff, version="parser-vintage-bumped")
        monkeypatch.setattr(composition, "PARSER_VERSION", "production-topt-live-parser:v999")
        bumped = composition._satisfy_from_recent_observations(probe, plan, cutoff=cutoff)
        assert bumped == frozenset(), (
            "a parser bump must force a fresh capture; reusing the old vintage produces a "
            "COMPLETE run that seeds nothing (#788)"
        )
    finally:
        probe.rollback()
        probe.close()


def test_reuse_binds_the_whole_bound_set_or_nothing(tick_database_url, monkeypatch) -> None:
    """#885 item 4: the reuse query's comment promised to fail closed over the WHOLE bound set."""
    day = date(2026, 4, 14)
    late_origin = CorroboratingOrigin(
        origin=twelve_data_origin.ORIGIN,
        parser_version=twelve_data_origin.PARSER_VERSION,
        mapping_version=twelve_data_origin.MAPPING_VERSION,
        value_key=twelve_data_origin.VALUE_KEY,
        confidence=Decimal("0.80"),
        fetch=lambda symbol, cutoff: MarketPriceQuote(
            raw_bytes=f"late:{symbol}:{day.isoformat()}".encode(),
            close=Decimal("40"),
            as_of=day,
            knowable_at=datetime(2026, 4, 14, 23, 0, tzinfo=UTC),
        ),
        raw_source=DataSource.TWELVE_DATA,
    )
    _arm(
        monkeypatch,
        quote=lambda: _quote(day),
        price_cutoff=day,
        corroborating_origins=(late_origin,),
        origin_settings=_TWELVE_DATA_CONFIGURED,
    )
    _run_tick(tick_database_url, version="bound-set-source", cutoff=datetime(2026, 4, 14, 23, 30, tzinfo=UTC))

    target_cutoff = datetime(2026, 4, 14, 22, 40, tzinfo=UTC)
    probe = psycopg.connect(tick_database_url)
    try:
        plan = composition.plan_and_persist(probe, cutoff=target_cutoff, version="bound-set-target")
        satisfied = composition._satisfy_from_recent_observations(probe, plan, cutoff=target_cutoff)
        reused = Counter(
            binding.obligation.capture_requirement_id.removesuffix(":v1")
            for work_item_id, binding in plan.bindings.items()
            if work_item_id in satisfied
        )
        assert reused["market-price"] == 0, "a bound set with a look-ahead member must not be reused in part"
        assert reused["financial-fact"] == 21
        bound = probe.execute(
            """
            select count(*)
            from raw.capture_obligations ob
            join staging.capture_observation_obligations link on link.capture_obligation_id = ob.obligation_id
            where ob.run_id = %s and ob.capture_requirement_id = 'market-price:v1'
            """,
            (plan.run_id,),
        ).fetchone()[0]
        assert bound == 0
    finally:
        probe.rollback()
        probe.close()


def test_reuse_reads_the_session_date_in_utc_whatever_the_session_time_zone(tick_database_url, monkeypatch) -> None:
    """#885 item 2, through the database: psycopg hands a timestamptz back in the connection's TimeZone."""
    _arm(monkeypatch)
    _run_tick(tick_database_url, version="session-zone-source", cutoff=_REUSE_CUTOFF)

    probe = psycopg.connect(tick_database_url)
    try:
        probe.execute("set time zone 'America/New_York'")
        plan = composition.plan_and_persist(probe, cutoff=_REUSE_CUTOFF, version="session-zone-target")
        satisfied = composition._satisfy_from_recent_observations(probe, plan, cutoff=_REUSE_CUTOFF)
        reused = Counter(
            binding.obligation.capture_requirement_id.removesuffix(":v1")
            for work_item_id, binding in plan.bindings.items()
            if work_item_id in satisfied
        )
        assert reused["market-price"] == 21
    finally:
        probe.rollback()
        probe.close()


@pytest.mark.parametrize("zone", ["UTC", "America/New_York", "Asia/Singapore", "America/Los_Angeles"])
def test_the_session_check_is_utc_in_any_zone(zone: str) -> None:
    """No database: #885 item 2 at the unit level."""
    knowable_at = datetime(2026, 3, 31, tzinfo=UTC).astimezone(ZoneInfo(zone))
    assert composition._is_settled_session(knowable_at, date(2026, 3, 31))
    assert not composition._is_settled_session(knowable_at, date(2026, 3, 30))


def test_another_universe_at_the_same_cutoff_is_neither_reused_nor_joined(tick_database_url, monkeypatch) -> None:
    """#877 H1 and H3 together, in the world where TOPT and QQQ key an issuer alike."""
    from data_engine.datahub.production_topt import plausibility_gate

    day = date(2026, 7, 14)
    cutoff = datetime(2026, 7, 14, 22, 15, tzinfo=UTC)
    shared = _key_topt_like_the_planes(monkeypatch)
    assert len(shared) == 13, "TOPT and QQQ share 13 listings"
    aapl_issuer = shared["listing:xnas:aapl"][0]

    _arm(monkeypatch, quote=lambda: _quote(day, Decimal("50")), price_cutoff=day, cutoff_date=day)
    with psycopg.connect(tick_database_url) as tick:
        qqq = run_topt_pipeline(
            tick,
            cutoff=cutoff,
            version=composition.live_version_for(cutoff),
            corpus_filename="corpus.qqq.v1.json",
            label_prefix="production-qqq",
        )
        tick.commit()

    probe = psycopg.connect(tick_database_url)
    try:
        plan = composition.plan_and_persist(probe, cutoff=cutoff, version="run-scope-h1-probe")
        assert all(plan.coordinates[listing][:2] == ids for listing, ids in shared.items())
        satisfied = composition._satisfy_from_recent_observations(probe, plan, cutoff=cutoff)
        reused = [
            binding.obligation.subject.id
            for work_item_id, binding in plan.bindings.items()
            if work_item_id in satisfied
        ]
        assert reused == [], f"observations frozen for a later partition must not be reused: {sorted(set(reused))}"
    finally:
        probe.rollback()
        probe.close()

    _arm(monkeypatch, quote=lambda: _quote(day, Decimal("40")), price_cutoff=day, cutoff_date=day)
    topt = _live_topt_tick(tick_database_url, monkeypatch, executed_at=cutoff, accept=True)
    assert _status_row(tick_database_url, topt["capture_run_id"])[:4] == (OBLIGATIONS, OBLIGATIONS, OBLIGATIONS, 0)
    assert _materialized(tick_database_url, topt["capture_run_id"]) == (1, 20, 20)

    with psycopg.connect(tick_database_url) as reader:
        aapl_issuer = shared["listing:xnas:aapl"][0]
        per_run = reader.execute(
            "select run_id from mart.topt_core_results where issuer_id = %s and cutoff = %s",
            (aapl_issuer, cutoff),
        ).fetchall()
        assert sorted(row[0] for row in per_run) == sorted([qqq.run_id, topt["capture_run_id"]])

        main_decisions = reader.execute(_MAIN_DECISIONS_SQL, (topt["strategy_run_id"],)).fetchall()
        shared_issuers = {issuer for issuer, _instrument in shared.values()}
        assert len(shared_issuers) == 12
        assert len(main_decisions) == 20 + len(shared_issuers)
        main_qqq_gate = dict(_gate_closes(reader, _MAIN_GATE_ROWS_SQL, qqq.run_id))
        aapl_listing = plan.coordinates["listing:xnas:aapl"][2]
        assert main_qqq_gate[aapl_listing] == Decimal("40"), "main read TOPT's price into the QQQ run"

        qqq_gate = {row.listing_id: row.last_close for row in plausibility_gate._rows(reader, qqq.run_id)}
        topt_gate = {row.listing_id: row.last_close for row in plausibility_gate._rows(reader, topt["capture_run_id"])}
        assert qqq_gate[aapl_listing] == Decimal("50") and set(qqq_gate.values()) == {Decimal("50")}
        assert topt_gate[aapl_listing] == Decimal("40") and len(topt_gate) == 20
        topt_confidence = _core_confidence(reader, topt["capture_run_id"])

    report = _served_report(tick_database_url)
    assert len(report.decisions) == 20
    assert len({decision.issuer_id for decision in report.decisions}) == 20
    assert aapl_issuer in {decision.issuer_id for decision in report.decisions}
    assert all(decision.confidence == topt_confidence[decision.issuer_id] for decision in report.decisions)


def test_reuse_age_is_bounded_to_original_success_fetch_not_unchanged_renewal(tick_database_url, monkeypatch) -> None:
    """#635: an observation captured at T0 is reused at T0 + 4h with terminal_state UNCHANGED.
    At T0 + 14h, the original SUCCESS fetch completed 14h ago (> 12h max age). It must NOT reuse."""
    _arm(monkeypatch)
    t0 = datetime(2026, 3, 31, 22, 15, tzinfo=UTC)

    _run_tick(tick_database_url, version="t0-fetch", cutoff=t0)

    t1 = t0 + timedelta(hours=4)
    probe = psycopg.connect(tick_database_url)
    try:
        plan1 = composition.plan_and_persist(probe, cutoff=t1, version="t1-check")
        satisfied1 = composition._satisfy_from_recent_observations(probe, plan1, cutoff=t1)
        assert len(satisfied1) > 0, "T1 must reuse from T0"
    finally:
        probe.rollback()
        probe.close()
    _run_tick(tick_database_url, version="t1-reuse", cutoff=t1)

    t2 = t0 + timedelta(hours=14)
    probe = psycopg.connect(tick_database_url)
    try:
        plan2 = composition.plan_and_persist(probe, cutoff=t2, version="t2-check")
        satisfied2 = composition._satisfy_from_recent_observations(probe, plan2, cutoff=t2)
        assert len(satisfied2) == 0, f"Expected 0 satisfied, but got {len(satisfied2)}: {satisfied2}"
    finally:
        probe.rollback()
        probe.close()


def test_reuse_refuses_when_configured_origin_sources_differ(tick_database_url, monkeypatch) -> None:
    """An anchor captured with only primary must NOT be reused if Twelve Data is configured,
    and vice versa."""
    t0 = datetime(2026, 3, 31, 22, 15, tzinfo=UTC)
    _arm(monkeypatch, origin_settings=_NO_CONFIGURED_ORIGINS)
    _run_tick(tick_database_url, version="t0-primary-only", cutoff=t0)

    t1 = t0 + timedelta(hours=1)
    monkeypatch.setattr(settings, "twelve_data_api_key", "test-key-configured")
    probe = psycopg.connect(tick_database_url)
    try:
        plan1 = composition.plan_and_persist(probe, cutoff=t1, version="t1-twelve-configured")
        satisfied1 = composition._satisfy_from_recent_observations(probe, plan1, cutoff=t1)
        reused_by_subject: dict[str, dict[str, bool]] = {}
        for work_item_id, binding in plan1.bindings.items():
            semantic = binding.obligation.capture_requirement_id.removesuffix(":v1")
            cells = reused_by_subject.setdefault(binding.obligation.subject.id, {})
            cells[semantic] = work_item_id in satisfied1
        assert all(cells["financial-fact"] for cells in reused_by_subject.values())
        assert not any(cells["market-price"] for cells in reused_by_subject.values())
    finally:
        probe.rollback()
        probe.close()
