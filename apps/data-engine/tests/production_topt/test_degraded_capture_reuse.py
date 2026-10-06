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


_FILING_KNOWABLE_AT = datetime(2026, 2, 1, tzinfo=UTC)


def _bundle(branch: OperatingBranch, knowable_at: datetime = _FILING_KNOWABLE_AT) -> FinancialFactsBundle:
    financial = branch is OperatingBranch.FINANCIAL
    return FinancialFactsBundle(
        gross_profit=Decimal("80000000") if financial else Decimal("210000000"),
        total_assets=Decimal("200000000"),
        shares_outstanding=Decimal("10000000"),
        revenue=Decimal("100000000"),
        pre_provision_profit=Decimal("80000000") if financial else None,
        raw_bytes=b'{"facts":{}}',
        knowable_at=knowable_at,
    )


def _offline_routes(
    plan: PlannedRun,
    connection,
    *,
    quote: Callable[[], MarketPriceQuote] = _quote,
    price_cutoff: date | None = None,
    corroborating_origins: tuple[CorroboratingOrigin, ...] = (),
    cutoff_date: date | None = None,
    filing_knowable_at: datetime = _FILING_KNOWABLE_AT,
) -> dict[str, SourceFetchPort]:
    # The settled session (#530 item 1), matching build_route's context.price_cutoff_date
    # -- not the tick's own run clock. A caller with a differently-partitioned corpus
    # still passes its own cutoff_date explicitly; this is only the fallback.
    cutoff_date = cutoff_date or plan.timeline.partition_start.date()
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
        lambda cik, cutoff, branch: _bundle(branch, filing_knowable_at),
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


#: The identity the READ PATH serves for an entity coordinate: mart.entity_identity's
#: symbolic legacy id, else the coordinate itself. `mart.topt_core_results.issuer_id` and
#: `mart.strategy_decisions.issuer_id` hold the opaque entity UUID (#928), and
#: PostgresStrategyRunRepository translates it on the way out (#953) so MCP callers get an
#: id they can resolve. A test that correlates a served decision with a mart row therefore
#: has to translate on the mart side too, or it is comparing two different id spaces --
#: which is exactly what these assertions caught when the translation landed.
_SERVED_IDENTITY_SQL = """
    select coalesce(ei.legacy_id, t.issuer_id), t.confidence
    from mart.topt_core_results t
    left join lateral (
        select ei.legacy_id from mart.entity_identity ei
        where ei.entity_id::text = t.issuer_id limit 1
    ) ei on true
    where t.run_id = %s
"""


def _core_confidence(reader, run_id: str) -> dict[str, Decimal]:
    return dict(reader.execute(_SERVED_IDENTITY_SQL, (run_id,)).fetchall())


def _served_identity(reader, entity_id: str) -> str:
    """One coordinate through the same translation, for an assertion about one issuer."""
    row = reader.execute(
        "select coalesce(ei.legacy_id, %s) from (select %s as id) q "
        "left join lateral (select ei.legacy_id from mart.entity_identity ei "
        "where ei.entity_id::text = q.id limit 1) ei on true",
        (entity_id, entity_id),
    ).fetchone()
    return str(row[0])


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


def test_reuse_binds_a_bar_and_a_filing_dated_after_the_partition_anchor(tick_database_url, monkeypatch) -> None:
    """#1060: valid_from is the date of the fact itself, not the universe anchor.

    The corpus anchor is 2026-03-31. The bar is the settled session of the cutoff day
    (2026-04-21). The filing became knowable on 2026-04-10. Both dates follow the anchor
    and precede the cutoff, so the second run must reuse both, as the freeze accepts both.
    """
    anchor = date(2026, 3, 31)
    settled_day = date(2026, 4, 21)
    filing_at = datetime(2026, 4, 10, tzinfo=UTC)
    cutoff = datetime(2026, 4, 21, 22, 15, tzinfo=UTC)
    _arm(
        monkeypatch,
        quote=lambda: _quote(settled_day),
        price_cutoff=settled_day,
        cutoff_date=cutoff.date(),
        filing_knowable_at=filing_at,
    )
    first = _run_tick(tick_database_url, version="after-anchor-source", cutoff=cutoff)
    second = _run_tick(tick_database_url, version="after-anchor-target", cutoff=cutoff)

    assert _status_row(tick_database_url, first.run_id)[:4] == (OBLIGATIONS, OBLIGATIONS, OBLIGATIONS, 0)
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
        reused = reader.execute(
            """
            select o.semantic_type, (o.valid_from at time zone 'UTC')::date, ob.partition_key, count(*)
            from raw.capture_obligations ob
            join raw.capture_obligation_results done on done.capture_obligation_id = ob.obligation_id
            join raw.capture_attempt_results attempt on attempt.attempt_id = done.final_attempt_id
            join staging.capture_normalized_observations o
              on o.source_vintage_id = attempt.reused_source_vintage_id
             and o.subject_id = ob.subject_id
             and o.semantic_type = regexp_replace(ob.capture_requirement_id, ':v1$', '')
            where ob.run_id = %s and done.terminal_state = 'unchanged'
            group by 1, 2, 3 order by 1, 2
            """,
            (second.run_id,),
        ).fetchall()
    assert reused == [
        ("financial-fact", filing_at.date(), anchor.isoformat(), 21),
        ("market-price", settled_day, anchor.isoformat(), 21),
    ]
    assert _materialized(tick_database_url, second.run_id) == (1, 20, 20)


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
    # Settled session vs. run clock (#530 item 1): the quote's own date must be the fixed
    # corpus's real partition, not an arbitrary later day.
    day = date(2026, 3, 31)
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


@pytest.mark.xfail(
    reason="#1019: the TOPT-leg capture mints a fresh UUID per run for a shared listing, "
    "not the identity _key_topt_like_the_planes recorded before either capture ran; the "
    "strategy's universe-eligibility check does not recognize it and excludes all 20 "
    "decisions. Confirmed unrelated to #530 (both legs' own captures now correctly use "
    "each corpus's real partition; the reuse assertion above this point already passes).",
    strict=True,
)
def test_another_universe_at_the_same_cutoff_is_neither_reused_nor_joined(tick_database_url, monkeypatch) -> None:
    """#877 H1 and H3 together, in the world where TOPT and QQQ key an issuer alike."""
    from data_engine.datahub.production_topt import plausibility_gate

    # #530 item 1: this test shares one `day` across two DIFFERENT fixed corpuses whose
    # real partitions match neither -- default corpus.v1.json is 2026-03-31,
    # corpus.qqq.v1.json is 2026-06-30 (both verified by reading the checked-in files).
    # `as_of` (-> valid_from) must be each leg's own real partition, or freeze_snapshot's
    # `valid_from <= partition_key` refuses both captures outright (the ValueError this
    # test was red with). `knowable_at` and price_cutoff (target.cutoff, whose own
    # look-ahead guard requires target.cutoff >= knowable_at.date() --
    # market_price_adapter.py:224) stay on `day`, the run's own clock, unchanged --
    # nothing here exercises the settled-session reuse check the sibling fix in
    # test_degraded_capture_forced.py needed to split further. cutoff_date (SEC/release
    # targets) is no longer passed explicitly: _offline_routes' own fallback
    # (plan.timeline.partition_start.date()) now resolves it correctly per leg, since
    # each leg is a different plan/corpus.
    day = date(2026, 7, 14)
    qqq_partition = date(2026, 6, 30)
    topt_partition = date(2026, 3, 31)
    cutoff = datetime(2026, 7, 14, 22, 15, tzinfo=UTC)
    shared = _key_topt_like_the_planes(monkeypatch)
    assert len(shared) == 13, "TOPT and QQQ share 13 listings"
    aapl_issuer = shared["listing:xnas:aapl"][0]

    def _leg_quote(as_of: date, close: Decimal) -> MarketPriceQuote:
        return MarketPriceQuote(
            raw_bytes=f"bar:{as_of.isoformat()}:{close}".encode(),
            close=close,
            as_of=as_of,
            knowable_at=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
        )

    _arm(monkeypatch, quote=lambda: _leg_quote(qqq_partition, Decimal("50")), price_cutoff=day)
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
        # Scoped to market-price, not every semantic type. The old expectation (nothing is
        # reused) held only while validity was judged at the partition anchor: QQQ's bar
        # (valid from 2026-06-30) was ineligible for TOPT's 2026-03-31 obligations. #1060
        # judges validity at the cutoff day (2026-07-14), so the bar is valid and the 13
        # shared listings ARE reused. The invariant "reuse never binds what the freeze
        # refuses" is covered by
        # test_reuse_binds_a_bar_and_a_filing_dated_after_the_partition_anchor and
        # test_freeze_selects_a_fact_dated_after_the_anchor_and_before_the_cutoff.
        # The financial-fact cells are out of scope: this fixture's _bundle() knowable_at
        # (2026-02-01) predates both anchors, so it satisfies both universes by construction.
        reused = [
            binding.obligation.subject.id
            for work_item_id, binding in plan.bindings.items()
            if work_item_id in satisfied and binding.obligation.capture_requirement_id == "market-price:v1"
        ]
        assert sorted(reused) == sorted(shared), (
            f"a price valid on the cutoff day is reused by every shared listing: {sorted(set(reused))}"
        )
    finally:
        probe.rollback()
        probe.close()

    _arm(monkeypatch, quote=lambda: _leg_quote(topt_partition, Decimal("40")), price_cutoff=day)
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
        aapl_served = _served_identity(reader, aapl_issuer)

    report = _served_report(tick_database_url)
    assert len(report.decisions) == 20
    assert len({decision.issuer_id for decision in report.decisions}) == 20
    assert aapl_served in {decision.issuer_id for decision in report.decisions}
    assert all(decision.confidence == topt_confidence[decision.issuer_id] for decision in report.decisions)


def test_two_universes_at_one_cutoff_share_a_valid_price_and_are_not_joined(tick_database_url, monkeypatch) -> None:
    """#1060: two universes at one cutoff share a price that is valid on the cutoff day.

    TOPT and QQQ key an issuer alike (#877 H3). The shared listings reuse the price, and
    the TOPT leg is forced so that it holds its own price. The two runs stay distinct.
    The #1019 scenario is the test above, which keeps its own `xfail`.
    """
    from data_engine.datahub.production_topt import plausibility_gate

    # Both legs date their bar as production does (#530 item 1, #1060): `as_of` (-> valid_from)
    # is the settled session of the tick, 2026-07-15. That day follows both corpus anchors
    # (QQQ 2026-06-30, TOPT 2026-03-31), so the reuse and the freeze must judge it at the
    # cutoff day. A bar valid on the cutoff day is shared across universes (#635, #684).
    # The TOPT leg is forced, so it captures its own price and the two runs hold different
    # prices. The #1019 test above commits its runs at 2026-07-14 22:15 in the same database.
    # This cutoff is one day later, so those runs stay outside the 12 hour reuse window.
    day = date(2026, 7, 15)
    cutoff = datetime(2026, 7, 15, 22, 15, tzinfo=UTC)
    shared = _key_topt_like_the_planes(monkeypatch)
    assert len(shared) == 13, "TOPT and QQQ share 13 listings"
    aapl_issuer = shared["listing:xnas:aapl"][0]

    def _leg_quote(close: Decimal) -> MarketPriceQuote:
        return MarketPriceQuote(
            raw_bytes=f"bar:{day.isoformat()}:{close}".encode(),
            close=close,
            as_of=day,
            knowable_at=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
        )

    _arm(monkeypatch, quote=lambda: _leg_quote(Decimal("50")), price_cutoff=day)
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
        # Scoped to market-price. The financial-fact bar of this fixture predates both
        # anchors and is reusable by construction.
        reused = [
            binding.obligation.subject.id
            for work_item_id, binding in plan.bindings.items()
            if work_item_id in satisfied and binding.obligation.capture_requirement_id == "market-price:v1"
        ]
        assert sorted(reused) == sorted(shared), "a price valid on the cutoff day is reused by every shared listing"
    finally:
        probe.rollback()
        probe.close()

    _arm(monkeypatch, quote=lambda: _leg_quote(Decimal("40")), price_cutoff=day)
    topt = _live_topt_tick(tick_database_url, monkeypatch, executed_at=cutoff, force_fetch=True, accept=True)
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
        aapl_served = _served_identity(reader, aapl_issuer)

    report = _served_report(tick_database_url)
    assert len(report.decisions) == 20
    assert len({decision.issuer_id for decision in report.decisions}) == 20
    assert aapl_served in {decision.issuer_id for decision in report.decisions}
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


# -- valid time of a reuse anchor and of its bound set (#1060) ----------------------------
#
# Two committed captures of one UTC day. Capture B is forced, so it holds its own
# observations and ranks first as the anchor. Each test edits observations inside a probe
# transaction, which it rolls back. The observation table is append-only, so the edit turns
# the mutation trigger off for that transaction only.

_WINDOW_DAY = date(2026, 8, 10)
_WINDOW_CUTOFF_A = datetime(2026, 8, 10, 20, 30, tzinfo=UTC)
_WINDOW_CUTOFF_B = datetime(2026, 8, 10, 21, 15, tzinfo=UTC)
_WINDOW_TARGET = datetime(2026, 8, 10, 22, 15, tzinfo=UTC)
_DAY_START = datetime(2026, 8, 10, tzinfo=UTC)
_NEXT_DAY_START = datetime(2026, 8, 11, tzinfo=UTC)
_PREVIOUS_DAY_START = datetime(2026, 8, 9, tzinfo=UTC)
_EARLIER_START = datetime(2026, 8, 5, tzinfo=UTC)
_WINDOW_ZONES = ("America/Los_Angeles", "Asia/Shanghai")


def _arm_window(monkeypatch, *, close: Decimal) -> None:
    second_origin = CorroboratingOrigin(
        origin=twelve_data_origin.ORIGIN,
        parser_version=twelve_data_origin.PARSER_VERSION,
        mapping_version=twelve_data_origin.MAPPING_VERSION,
        value_key=twelve_data_origin.VALUE_KEY,
        confidence=Decimal("0.80"),
        fetch=lambda symbol, cutoff: MarketPriceQuote(
            raw_bytes=f"second:{symbol}:{close}".encode(),
            close=close,
            as_of=_WINDOW_DAY,
            knowable_at=datetime(2026, 8, 10, 20, 10, tzinfo=UTC),
        ),
        raw_source=DataSource.TWELVE_DATA,
    )
    _arm(
        monkeypatch,
        quote=lambda: _quote(_WINDOW_DAY, close),
        price_cutoff=_WINDOW_DAY,
        cutoff_date=_WINDOW_DAY,
        corroborating_origins=(second_origin,),
        origin_settings=_TWELVE_DATA_CONFIGURED,
    )


@pytest.fixture(scope="module")
def window_sources(tick_database_url) -> tuple[str, str]:
    """The run ids of capture A and capture B."""
    with pytest.MonkeyPatch.context() as patch:
        _arm_window(patch, close=Decimal("40"))
        first = _run_tick(tick_database_url, version="window-source-a", cutoff=_WINDOW_CUTOFF_A)
        _arm_window(patch, close=Decimal("41"))
        second = _run_tick(tick_database_url, version="window-source-b", cutoff=_WINDOW_CUTOFF_B, force_fetch=True)
    return first.run_id, second.run_id


def _run_observations(probe, run_id: str) -> set[str]:
    rows = probe.execute(
        """
        select link.observation_id
        from raw.capture_obligations ob
        join staging.capture_observation_obligations link on link.capture_obligation_id = ob.obligation_id
        where ob.run_id = %s
        """,
        (run_id,),
    ).fetchall()
    return {row[0] for row in rows}


def _edit_window(
    probe,
    run_id: str,
    *,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    parser: str | None = None,
) -> None:
    """Set valid_from and valid_to of a capture's observations. A None keeps the stored value."""
    edited = probe.execute(
        """
        update staging.capture_normalized_observations
        set valid_from = coalesce(%(valid_from)s, valid_from), valid_to = coalesce(%(valid_to)s, valid_to)
        where observation_id in (
            select link.observation_id
            from raw.capture_obligations ob
            join staging.capture_observation_obligations link on link.capture_obligation_id = ob.obligation_id
            where ob.run_id = %(run_id)s
        ) and (%(parser)s::text is null or parser_version = %(parser)s)
        """,
        {"valid_from": valid_from, "valid_to": valid_to, "run_id": run_id, "parser": parser},
    )
    assert edited.rowcount > 0, "the edit matched no observation"


def _reuse_after_edits(
    url: str,
    monkeypatch,
    *,
    cutoff: datetime,
    version: str,
    edits=(),
    zone: str | None = None,
) -> tuple[Counter[str], dict[str, set[str]]]:
    """Run the reuse query for a new plan after the edits. Returns the reused cells per
    semantic type and the observations the reuse bound per semantic type."""
    _arm(monkeypatch, origin_settings=_TWELVE_DATA_CONFIGURED)
    probe = psycopg.connect(url)
    try:
        if zone is not None:
            probe.execute("select set_config('TimeZone', %s, false)", (zone,))
        plan = composition.plan_and_persist(probe, cutoff=cutoff, version=version)
        probe.execute("set local session_replication_role = replica")
        for edit in edits:
            edit(probe)
        probe.execute("set local session_replication_role = origin")
        satisfied = composition._satisfy_from_recent_observations(probe, plan, cutoff=cutoff)
        reused = Counter(
            binding.obligation.capture_requirement_id.removesuffix(":v1")
            for work_item_id, binding in plan.bindings.items()
            if work_item_id in satisfied
        )
        bound: dict[str, set[str]] = {}
        for semantic, observation_id in probe.execute(
            """
            select regexp_replace(ob.capture_requirement_id, ':v1$', ''), link.observation_id
            from raw.capture_obligations ob
            join staging.capture_observation_obligations link on link.capture_obligation_id = ob.obligation_id
            where ob.run_id = %s
            """,
            (plan.run_id,),
        ).fetchall():
            bound.setdefault(semantic, set()).add(observation_id)
        return reused, bound
    finally:
        probe.rollback()
        probe.close()


@pytest.mark.parametrize("cutoff", [_WINDOW_TARGET, datetime(2026, 8, 10, 23, 59, 59, tzinfo=UTC)])
def test_reuse_binds_the_unedited_window_sources(tick_database_url, window_sources, monkeypatch, cutoff) -> None:
    """The control for the edited cases below: the same sources are reusable without an edit."""
    reused, _bound = _reuse_after_edits(
        tick_database_url, monkeypatch, cutoff=cutoff, version=f"control-{cutoff:%H%M%S}"
    )

    assert (reused["market-price"], reused["financial-fact"]) == (21, 21)


def test_reuse_skips_an_anchor_that_starts_after_the_cutoff_day(tick_database_url, window_sources, monkeypatch) -> None:
    run_a, run_b = window_sources
    probe = psycopg.connect(tick_database_url)
    try:
        observations_a, observations_b = _run_observations(probe, run_a), _run_observations(probe, run_b)
    finally:
        probe.close()
    assert observations_a and observations_b and observations_a.isdisjoint(observations_b)

    reused, bound = _reuse_after_edits(
        tick_database_url,
        monkeypatch,
        cutoff=_WINDOW_TARGET,
        version="anchor-starts-after",
        edits=[lambda probe: _edit_window(probe, run_b, valid_from=_NEXT_DAY_START)],
    )

    # The newer anchor is out of window, so the older anchor that is in window serves the cells.
    assert (reused["market-price"], reused["financial-fact"]) == (21, 21)
    vendor_bound = bound["market-price"] | bound["financial-fact"]
    assert vendor_bound <= observations_a
    assert vendor_bound.isdisjoint(observations_b)


def test_reuse_skips_an_anchor_that_ended_before_the_cutoff_day(tick_database_url, window_sources, monkeypatch) -> None:
    run_a, run_b = window_sources
    probe = psycopg.connect(tick_database_url)
    try:
        observations_a, observations_b = _run_observations(probe, run_a), _run_observations(probe, run_b)
    finally:
        probe.close()

    reused, bound = _reuse_after_edits(
        tick_database_url,
        monkeypatch,
        cutoff=_WINDOW_TARGET,
        version="anchor-ended-before",
        edits=[lambda probe: _edit_window(probe, run_b, valid_from=_EARLIER_START, valid_to=_PREVIOUS_DAY_START)],
    )

    assert (reused["market-price"], reused["financial-fact"]) == (21, 21)
    vendor_bound = bound["market-price"] | bound["financial-fact"]
    assert vendor_bound <= observations_a
    assert vendor_bound.isdisjoint(observations_b)


def test_reuse_refuses_a_bound_set_with_a_member_that_starts_after_the_cutoff_day(
    tick_database_url, window_sources, monkeypatch
) -> None:
    _run_a, run_b = window_sources
    second_origin = twelve_data_origin.PARSER_VERSION

    reused, bound = _reuse_after_edits(
        tick_database_url,
        monkeypatch,
        cutoff=_WINDOW_TARGET,
        version="bound-set-starts-after",
        edits=[lambda probe: _edit_window(probe, run_b, valid_from=_NEXT_DAY_START, parser=second_origin)],
    )

    # The anchor is in window and its second origin is not: the whole set stays unbound.
    assert reused["market-price"] == 0
    assert "market-price" not in bound
    assert reused["financial-fact"] == 21


def test_reuse_refuses_a_bound_set_with_a_member_that_ended_before_the_cutoff_day(
    tick_database_url, window_sources, monkeypatch
) -> None:
    _run_a, run_b = window_sources
    second_origin = twelve_data_origin.PARSER_VERSION

    reused, bound = _reuse_after_edits(
        tick_database_url,
        monkeypatch,
        cutoff=_WINDOW_TARGET,
        version="bound-set-ended-before",
        edits=[
            lambda probe: _edit_window(
                probe, run_b, valid_from=_EARLIER_START, valid_to=_PREVIOUS_DAY_START, parser=second_origin
            )
        ],
    )

    assert reused["market-price"] == 0
    assert "market-price" not in bound
    assert reused["financial-fact"] == 21


@pytest.mark.parametrize("zone", _WINDOW_ZONES)
def test_reuse_keeps_a_window_ending_at_midnight_of_the_cutoff_day_in_any_session_time_zone(
    tick_database_url, window_sources, monkeypatch, zone: str
) -> None:
    run_a, run_b = window_sources
    edits = [
        lambda probe, run_id=run_id: _edit_window(probe, run_id, valid_from=_EARLIER_START, valid_to=_DAY_START)
        for run_id in (run_a, run_b)
    ]

    reused, _bound = _reuse_after_edits(
        tick_database_url,
        monkeypatch,
        cutoff=_WINDOW_TARGET,
        version=f"midnight-end-{zone[:3]}",
        edits=edits,
        zone=zone,
    )

    assert (reused["market-price"], reused["financial-fact"]) == (21, 21)


@pytest.mark.parametrize("zone", _WINDOW_ZONES)
def test_reuse_refuses_a_valid_from_on_the_next_utc_day_in_any_session_time_zone(
    tick_database_url, window_sources, monkeypatch, zone: str
) -> None:
    run_a, run_b = window_sources
    edits = [
        lambda probe, run_id=run_id: _edit_window(probe, run_id, valid_from=_NEXT_DAY_START)
        for run_id in (run_a, run_b)
    ]

    reused, bound = _reuse_after_edits(
        tick_database_url,
        monkeypatch,
        cutoff=datetime(2026, 8, 10, 23, 59, 59, tzinfo=UTC),
        version=f"next-day-start-{zone[:3]}",
        edits=edits,
        zone=zone,
    )

    assert (reused["market-price"], reused["financial-fact"]) == (0, 0)
    assert "market-price" not in bound and "financial-fact" not in bound
