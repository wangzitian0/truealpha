"""Forced fetch and run-scope tests (#874, #877, #892).

Split from test_degraded_capture_record.py per #906 to eliminate the monolithic CI wall.
These tests exercise forced fetch overrides, retrying forced launches, anchor preferences,
and run-scoped readers across twins, gate rows, and pointer advances.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest
from data_engine import raw_store
from data_engine.config import settings
from data_engine.datahub.production_topt import composition
from data_engine.datahub.production_topt.composition import (
    CaptureNotPublishableError,
    PlannedRun,
    run_topt_pipeline,
)
from data_engine.datahub.production_topt.executor import (
    FetchFailure,
    FetchOutcome,
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
from truealpha_contracts.datahub import CaptureWorkItem
from truealpha_contracts.models import RawCapture, RawIngestionEnvelope, RawObjectRef
from truealpha_contracts.obligation_reason_codes import ObligationReasonCode
from truealpha_runtime.testing import apply_migration_chain

CUTOFF = datetime(2026, 4, 2, tzinfo=UTC)
OBLIGATIONS = 84
_RELEASE_OBLIGATIONS = 42
_BANK_TICKER = "JPM"
_VENDOR_SEMANTICS = ("market-price", "financial-fact")
_REUSE_CUTOFF = datetime(2026, 3, 31, 22, 15, tzinfo=UTC)
_STRATEGY = "large_model_value_v0"

_MAIN_GOVERNED_STRATEGY_RUN_SQL = """
    with head as (
        select environment, universe_id, universe_version, factor_id, target_run_id, sequence, advanced_at
        from mart.current_pointer_head
        where environment = 'production'
          and factor_id = 'gross_profit_per_employee'
          and universe_id like 'universe:topt-%'
        order by advanced_at desc
        limit 1
    )
    select head.target_run_id, run.strategy_run_id
    from head
    join mart.topt_capture_status status on status.run_id = head.target_run_id
    join mart.strategy_runs run on run.executed_at = status.cutoff
"""
_MAIN_LATEST_RUN_SQL = f"""
    select r.strategy_run_id,
           exists (select 1 from ({_MAIN_GOVERNED_STRATEGY_RUN_SQL.replace("%", "%%")}) g
                   where g.strategy_run_id = r.strategy_run_id) as is_governed
    from mart.strategy_runs r
    where r.strategy_key = %s
    order by is_governed desc, r.executed_at desc, r.created_at desc, r.strategy_run_id desc
    limit 1
"""
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
_MAIN_PEG_RUN_SQL = """
    select s.strategy_run_id
    from mart.strategy_runs s
    join mart.strategy_decisions d on d.strategy_run_id = s.strategy_run_id
    where d.cutoff_at <= %s
    group by s.strategy_run_id, s.executed_at
    order by max(d.cutoff_at) desc, s.executed_at desc
"""


@pytest.fixture(scope="module")
def tick_database_url():
    parameters = conninfo_to_dict(settings.database_url)
    database_name = f"truealpha_forced_{os.getpid()}_{uuid.uuid4().hex[:8]}"
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


class _FailingPort:
    def __init__(self, reason: ObligationReasonCode) -> None:
        self._reason = reason

    def fetch(self, work_item: CaptureWorkItem) -> FetchOutcome:
        return FetchFailure(self._reason)


class _CountingPort:
    def __init__(self, inner: SourceFetchPort, *, run_id: str, semantic: str, log: list[tuple[str, str, str]]):
        self._inner = inner
        self._run_id = run_id
        self._semantic = semantic
        self._log = log

    def fetch(self, work_item: CaptureWorkItem) -> FetchOutcome:
        self._log.append((self._run_id, work_item.work_item_id, self._semantic))
        return self._inner.fetch(work_item)


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


def _arm(
    monkeypatch,
    *,
    sabotage: tuple[int, ObligationReasonCode] | None = None,
    spy: list[str] | None = None,
    fetched: list[tuple[str, str, str]] | None = None,
    origin_settings: dict[str, object] | None = None,
    **route_options,
) -> None:
    from truealpha_runtime import EnvironmentTier as RuntimeEnvironmentTier

    monkeypatch.setattr(settings, "environment_tier", RuntimeEnvironmentTier.PRODUCTION)
    for name, value in {**_NO_CONFIGURED_ORIGINS, **(origin_settings or {})}.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(raw_store, "object_store", _InMemoryObjectStore)

    def build(plan: PlannedRun, connection=None) -> dict[str, SourceFetchPort]:
        if spy is not None:
            spy.append(plan.run_id)
        routes = _offline_routes(plan, connection, **route_options)
        if sabotage is not None:
            index, reason = sabotage
            routes[plan.work_items[index].work_item_id] = _FailingPort(reason)
        if fetched is not None:
            routes = {
                work_item_id: _CountingPort(
                    port,
                    run_id=plan.run_id,
                    semantic=plan.bindings[work_item_id].obligation.capture_requirement_id.removesuffix(":v1"),
                    log=fetched,
                )
                for work_item_id, port in routes.items()
            }
        return routes

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


def _report_payload(url: str, run_id: str) -> dict:
    with psycopg.connect(url) as reader:
        rows = reader.execute(
            "select payload from mart.datahub_quality_report where run_id = %s order by created_at",
            (run_id,),
        ).fetchall()
    assert len(rows) == 1, f"a run must persist exactly one quality report, found {len(rows)}"
    return rows[0][0]


def _materialized(url: str, run_id: str) -> tuple[int, int, int]:
    with psycopg.connect(url) as reader:
        snapshots = reader.execute(
            "select count(*) from staging.topt_core_snapshots where run_id = %s", (run_id,)
        ).fetchone()[0]
        gppe = reader.execute("select count(*) from mart.topt_gppe_results where run_id = %s", (run_id,)).fetchone()[0]
        core = reader.execute("select count(*) from mart.topt_core_results where run_id = %s", (run_id,)).fetchone()[0]
    return snapshots, gppe, core


def _run_plan(url: str, run_id: str) -> dict:
    with psycopg.connect(url) as reader:
        return reader.execute(
            "select payload from raw.production_topt_run_plans where run_id = %s", (run_id,)
        ).fetchone()[0]


def _fetch_row_count(url: str) -> int:
    with psycopg.connect(url) as reader:
        return reader.execute("select count(*) from raw.fetches").fetchone()[0]


def _served_closes(url: str, run_id: str) -> set[str]:
    with psycopg.connect(url) as reader:
        rows = reader.execute(
            """
            select distinct p.normalized_payload->>'close'
            from raw.capture_obligations ob
            join staging.capture_observation_obligations link on link.capture_obligation_id = ob.obligation_id
            join staging.capture_normalized_observations o on o.observation_id = link.observation_id
            join staging.capture_observation_payloads p on p.observation_id = o.observation_id
            where ob.run_id = %s and o.semantic_type = 'market-price'
            """,
            (run_id,),
        ).fetchall()
    return {row[0] for row in rows}


def _fetches_by_semantic(fetched: list[tuple[str, str, str]], run_id: str) -> Counter[str]:
    return Counter(semantic for run, _work_item, semantic in fetched if run == run_id)


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


def test_a_forced_run_fetches_every_obligation_despite_fresh_observations(tick_database_url, monkeypatch) -> None:
    """#874, the issue's own evidence: a same-day re-run reused the 03:38Z canary's observations."""
    fetched: list[tuple[str, str, str]] = []
    _arm(monkeypatch, fetched=fetched)
    _run_tick(tick_database_url, version="forced-seed", cutoff=_REUSE_CUTOFF)

    control = _run_tick(tick_database_url, version="forced-same-tick", cutoff=_REUSE_CUTOFF)
    control_fetches = _fetches_by_semantic(fetched, control.run_id)
    assert not any(control_fetches[semantic] for semantic in _VENDOR_SEMANTICS), control_fetches
    assert _status_row(tick_database_url, control.run_id)[3] == OBLIGATIONS - _RELEASE_OBLIGATIONS

    fetch_rows_before = _fetch_row_count(tick_database_url)
    forced = _run_tick(tick_database_url, version="forced-same-tick", cutoff=_REUSE_CUTOFF, force_fetch=True)

    assert forced.run_id != control.run_id
    per_cell = Counter(work_item for run, work_item, _semantic in fetched if run == forced.run_id)
    assert len(per_cell) == OBLIGATIONS and set(per_cell.values()) == {1}, "each obligation fetched exactly once"
    assert _fetches_by_semantic(fetched, forced.run_id) == {
        "market-price": 21,
        "financial-fact": 21,
        "listing-identity": 21,
        "universe-membership": 21,
    }
    assert _status_row(tick_database_url, forced.run_id) == (
        OBLIGATIONS,
        OBLIGATIONS,
        OBLIGATIONS,
        0,
        0,
        0,
        0,
        True,
    )
    assert _materialized(tick_database_url, forced.run_id) == (1, 20, 20)

    assert _run_plan(tick_database_url, forced.run_id)["forced_fetch"] is True
    assert _run_plan(tick_database_url, control.run_id)["forced_fetch"] is False
    assert forced.forced_fetch is True and control.forced_fetch is False
    assert forced.quality["forced_fetch"] is True
    assert _report_payload(tick_database_url, forced.run_id)["forced_fetch"] is True
    assert _report_payload(tick_database_url, control.run_id)["forced_fetch"] is False
    assert _fetch_row_count(tick_database_url) == fetch_rows_before


def test_a_forced_fetch_of_changed_bytes_serves_the_new_vintage(tick_database_url, monkeypatch) -> None:
    """The recovery half of #874: a forced re-run lands the corrected bytes as a new vintage."""
    # The quote's own settled session must be the fixed corpus's real partition (#530
    # item 1); the run's own clock (cutoff) is free to be any later date.
    day = date(2026, 3, 31)
    cutoff = datetime(2026, 4, 16, 22, 15, tzinfo=UTC)
    _arm(monkeypatch, quote=lambda: _quote(day, Decimal("40")), price_cutoff=day)
    first = _run_tick(tick_database_url, version="corrected-bytes", cutoff=cutoff)
    assert _served_closes(tick_database_url, first.run_id) == {"40"}

    fetch_rows_before = _fetch_row_count(tick_database_url)
    _arm(monkeypatch, quote=lambda: _quote(day, Decimal("41.5")), price_cutoff=day)
    forced = _run_tick(tick_database_url, version="corrected-bytes", cutoff=cutoff, force_fetch=True)

    assert forced.run_id != first.run_id
    assert _served_closes(tick_database_url, forced.run_id) == {"41.5"}
    assert _materialized(tick_database_url, forced.run_id) == (1, 20, 20)
    assert _fetch_row_count(tick_database_url) == fetch_rows_before + 21
    assert _served_closes(tick_database_url, first.run_id) == {"40"}


def test_retrying_a_forced_launch_resumes_instead_of_fetching_again(tick_database_url, monkeypatch) -> None:
    """The same forced launch retried is the same capture: a complete one resumes (#628)."""
    cutoff = CUTOFF + timedelta(days=6)
    fetched: list[tuple[str, str, str]] = []
    _arm(monkeypatch, fetched=fetched)
    first = _run_tick(tick_database_url, version="forced-retry", cutoff=cutoff, force_fetch=True)
    assert len([entry for entry in fetched if entry[0] == first.run_id]) == OBLIGATIONS

    fetched.clear()
    routes_built: list[str] = []
    _arm(monkeypatch, spy=routes_built, fetched=fetched)
    retried = _run_tick(tick_database_url, version="forced-retry", cutoff=cutoff, force_fetch=True)

    assert retried.run_id == first.run_id
    assert retried.quality_report_id == first.quality_report_id
    assert retried.forced_fetch is True
    assert routes_built == [] and fetched == [], "a retried forced launch must not fetch again"
    assert _report_payload(tick_database_url, first.run_id)["forced_fetch"] is True
    assert _materialized(tick_database_url, first.run_id) == (1, 20, 20)


def test_retrying_a_degraded_forced_launch_reports_the_record(tick_database_url, monkeypatch) -> None:
    """#538 holds for forced runs too: a degraded forced capture is settled history."""
    cutoff = CUTOFF + timedelta(days=9)
    _arm(monkeypatch, sabotage=(0, ObligationReasonCode.FIELD_UNAVAILABLE))
    with pytest.raises(CaptureNotPublishableError) as first:
        _run_tick(tick_database_url, version="forced-degraded", cutoff=cutoff, force_fetch=True)

    routes_built: list[str] = []
    _arm(monkeypatch, spy=routes_built)
    with pytest.raises(CaptureNotPublishableError) as replayed:
        _run_tick(tick_database_url, version="forced-degraded", cutoff=cutoff, force_fetch=True)

    assert replayed.value.run_id == first.value.run_id
    assert replayed.value.quality_report_id == first.value.quality_report_id
    assert routes_built == [], "a settled forced run must be refused before any vendor call"
    shortfall_report = _report_payload(tick_database_url, first.value.run_id)
    assert shortfall_report["forced_fetch"] is True
    assert shortfall_report["capture_shortfall"]["success_count"] == OBLIGATIONS - 1

    fresh = _run_tick(
        tick_database_url, version="forced-degraded", cutoff=cutoff + timedelta(minutes=1), force_fetch=True
    )
    assert fresh.run_id != first.value.run_id
    assert _status_row(tick_database_url, fresh.run_id)[2] == OBLIGATIONS


def test_a_forced_capture_version_is_distinct_and_stable() -> None:
    """No database: the identity half of #874."""
    version = composition.live_version_for(_REUSE_CUTOFF)
    forced = composition.forced_capture_version(version)
    assert forced != version and forced.startswith(version)
    assert composition.forced_capture_version(forced) == forced


def test_reuse_prefers_the_forced_capture_of_the_same_tick(tick_database_url, monkeypatch) -> None:
    """#874: at tie-break, the forced capture is the newer look at the vendor, and it wins."""
    # #530 item 1 split this test's one `day` into three genuinely different concepts
    # _quote()/a shared `price_cutoff=day` used to conflate:
    #   - `as_of` (-> valid_from) must be the fixed corpus's real partition (2026-03-31),
    #     or freeze_snapshot's `valid_from <= partition_key` now correctly refuses it.
    #   - `knowable_at` feeds composition._satisfy_from_recent_observations' session-bound
    #     reuse check (`_is_settled_session`), which requires it to equal
    #     last_settled_session_date(cutoff) -- verified locally
    #     (truealpha_contracts.calendar.settled_session_for_cutoff) to be 2026-04-21 for
    #     this cutoff, not the corpus's 2026-03-31.
    #   - `target.cutoff` (price_cutoff) is what the PRIMARY fetch's own look-ahead guard
    #     compares knowable_at against (market_price_adapter.py:224: `knowable_at.date() >
    #     target.cutoff` -> LOOK_AHEAD_VIOLATION, confirmed by CI when this was still
    #     `day`); it must be >= knowable_at, i.e. settled_day, not the corpus partition.
    # Before this PR none of these three were real, so one shared `day` equal to
    # cutoff.date() (the original author's choice) satisfied all three by accident.
    day = date(2026, 3, 31)
    settled_day = date(2026, 4, 21)
    cutoff = datetime(2026, 4, 21, 22, 15, tzinfo=UTC)

    def _reuse_quote(close: Decimal) -> MarketPriceQuote:
        return MarketPriceQuote(
            raw_bytes=f"bar:{day.isoformat()}:{close}".encode(),
            close=close,
            as_of=day,
            knowable_at=datetime.combine(settled_day, datetime.min.time(), tzinfo=UTC),
        )

    _arm(monkeypatch, quote=lambda: _reuse_quote(Decimal("40")), price_cutoff=settled_day)
    first_run = _run_tick(tick_database_url, version="anchor-choice", cutoff=cutoff)
    _arm(monkeypatch, quote=lambda: _reuse_quote(Decimal("39.25")), price_cutoff=settled_day)
    forced_run = _run_tick(tick_database_url, version="anchor-choice", cutoff=cutoff, force_fetch=True)
    print(f"DIAG first_run.run_id={first_run.run_id} forced_run.run_id={forced_run.run_id}")

    follower_cutoff = cutoff + timedelta(minutes=5)
    probe = psycopg.connect(tick_database_url)
    try:
        plan = composition.plan_and_persist(probe, cutoff=follower_cutoff, version="anchor-choice-follower")
        satisfied = composition._satisfy_from_recent_observations(probe, plan, cutoff=follower_cutoff)
        price_cells = [
            work_item_id
            for work_item_id, binding in plan.bindings.items()
            if binding.obligation.capture_requirement_id == "market-price:v1"
        ]
        if not (price_cells and all(work_item_id in satisfied for work_item_id in price_cells)):
            # #530 item 1 diagnostic: dump the anchor's real state instead of guessing
            # blind. Not local-DB reproducible from this environment.
            missing = [wi for wi in price_cells if wi not in satisfied]
            print(f"DIAG missing {len(missing)}/{len(price_cells)} price cells; satisfied total={len(satisfied)}")
            follower_ob = plan.bindings[missing[0]].obligation
            print(f"DIAG missing[0] obligation_id={follower_ob.obligation_id} subject_id={follower_ob.subject.id}")
            print(f"DIAG missing[0] partition_key={follower_ob.partition} follower plan.run_id={plan.run_id}")
            for label, run_id in (("first", first_run.run_id), ("forced", forced_run.run_id)):
                rows = probe.execute(
                    """
                    select ob.obligation_id, ob.partition_key, ob.subject_id, result.terminal_state,
                           result.completed_at, o.observation_id, o.valid_from, o.valid_to,
                           o.knowable_at, o.parser_version, o.source_vintage_id
                    from raw.capture_obligations ob
                    join raw.capture_obligation_results result on result.capture_obligation_id = ob.obligation_id
                    left join raw.capture_attempt_results attempt on attempt.attempt_id = result.final_attempt_id
                    left join staging.capture_normalized_observations o
                      on o.source_vintage_id = coalesce(attempt.source_vintage_id, attempt.reused_source_vintage_id)
                    where ob.run_id = %s and ob.capture_requirement_id = 'market-price:v1'
                      and ob.subject_id = %s
                    """,
                    (run_id, follower_ob.subject.id),
                ).fetchall()
                for row in rows:
                    print(f"DIAG {label}_run row: {row}")
        assert price_cells and all(work_item_id in satisfied for work_item_id in price_cells)
        closes = probe.execute(
            """
            select distinct p.normalized_payload->>'close'
            from raw.capture_obligations ob
            join staging.capture_observation_obligations link on link.capture_obligation_id = ob.obligation_id
            join staging.capture_observation_payloads p on p.observation_id = link.observation_id
            where ob.run_id = %s and ob.capture_requirement_id = 'market-price:v1'
            """,
            (plan.run_id,),
        ).fetchall()
        assert {row[0] for row in closes} == {"39.25"}, "every price cell must reuse the forced capture"
    finally:
        probe.rollback()
        probe.close()


def test_a_forced_tick_that_advances_the_head_is_read_once_through_its_own_run(tick_database_url, monkeypatch) -> None:
    """#877's live defect, confirmed: a forced TOPT tick shares the scheduled tick's cutoff."""
    from data_engine.datahub.production_topt import plausibility_gate
    from data_engine.datahub.question_coverage import peg_cells
    from truealpha_contracts.strategy_run_postgres import LATEST_RUN_SQL

    # Settled session vs. run clock, same as above (#530 item 1).
    day = date(2026, 3, 31)
    cutoff = datetime(2026, 5, 5, 22, 15, tzinfo=UTC)
    _arm(monkeypatch, quote=lambda: _quote(day, Decimal("40")), price_cutoff=day)
    scheduled = _live_topt_tick(tick_database_url, monkeypatch, executed_at=cutoff, accept=True)
    _arm(monkeypatch, quote=lambda: _quote(day, Decimal("41.5")), price_cutoff=day)
    forced = _live_topt_tick(tick_database_url, monkeypatch, executed_at=cutoff, force_fetch=True, accept=True)

    with psycopg.connect(tick_database_url) as reader:
        assert scheduled["capture_run_id"] != forced["capture_run_id"]
        assert scheduled["strategy_run_id"] != forced["strategy_run_id"]
        assert forced["pointer_sequence"] == scheduled["pointer_sequence"] + 1
        core_runs = reader.execute(
            "select count(distinct run_id) from mart.topt_core_results where cutoff = %s", (cutoff,)
        ).fetchone()[0]
        assert core_runs == 2

        main_governed = reader.execute(_MAIN_GOVERNED_STRATEGY_RUN_SQL).fetchall()
        assert sorted(run for _head, run in main_governed) == sorted(
            [scheduled["strategy_run_id"], forced["strategy_run_id"]]
        )
        main_decisions = reader.execute(_MAIN_DECISIONS_SQL, (forced["strategy_run_id"],)).fetchall()
        assert len(main_decisions) == 40 and len({issuer for issuer, *_ in main_decisions}) == 20
        main_gate = _gate_closes(reader, _MAIN_GATE_ROWS_SQL, forced["capture_run_id"])
        assert len(main_gate) == 40 and {close for _listing, close in main_gate} == {Decimal("40"), Decimal("41.5")}
        main_peg_runs = [row[0] for row in reader.execute(_MAIN_PEG_RUN_SQL, (cutoff,)).fetchall()[:2]]
        assert set(main_peg_runs) == {scheduled["strategy_run_id"], forced["strategy_run_id"]}

        governed = reader.execute("select target_run_id, strategy_run_id from mart.governed_strategy_run").fetchall()
        assert governed == [(forced["capture_run_id"], forced["strategy_run_id"])]
        latest = reader.execute(LATEST_RUN_SQL, (_STRATEGY,)).fetchone()
        assert (latest[0], latest[3]) == (forced["strategy_run_id"], True)

        forced_gate = plausibility_gate._rows(reader, forced["capture_run_id"])
        scheduled_gate = plausibility_gate._rows(reader, scheduled["capture_run_id"])
        assert len(forced_gate) == 20 and {row.last_close for row in forced_gate} == {Decimal("41.5")}
        assert len(scheduled_gate) == 20 and {row.last_close for row in scheduled_gate} == {Decimal("40")}

        peg = peg_cells(reader, run_id=forced["capture_run_id"])
        assert len(peg) == 20
        forced_confidence = _core_confidence(reader, forced["capture_run_id"])

    report = _served_report(tick_database_url)
    assert len(report.decisions) == 20
    assert {decision.issuer_id for decision in report.decisions} == set(forced_confidence)
    assert all(decision.confidence == forced_confidence[decision.issuer_id] for decision in report.decisions)


def test_a_withheld_forced_tick_never_displaces_the_governed_strategy_run(tick_database_url, monkeypatch) -> None:
    """The other side of the verdict: a forced tick whose advance is withheld does not move the head."""
    from data_engine.datahub.question_coverage import peg_cells
    from truealpha_contracts.strategy_run_postgres import LATEST_RUN_SQL

    # Settled session vs. run clock, same as above (#530 item 1).
    day = date(2026, 3, 31)
    cutoff = datetime(2026, 5, 7, 22, 15, tzinfo=UTC)
    _arm(monkeypatch, quote=lambda: _quote(day, Decimal("40")), price_cutoff=day)
    scheduled = _live_topt_tick(tick_database_url, monkeypatch, executed_at=cutoff, accept=True)
    _arm(monkeypatch, quote=lambda: _quote(day, Decimal("38")), price_cutoff=day)
    withheld = _live_topt_tick(tick_database_url, monkeypatch, executed_at=cutoff, force_fetch=True, accept=False)
    assert withheld["pointer_sequence"] == scheduled["pointer_sequence"], "the head did not move"
    assert withheld["strategy_run_id"] != scheduled["strategy_run_id"]

    with psycopg.connect(tick_database_url) as reader:
        head = reader.execute(
            "select target_run_id from mart.current_pointer_head "
            "where universe_id like 'universe:topt-%' order by advanced_at desc limit 1"
        ).fetchone()[0]
        assert head == scheduled["capture_run_id"]

        main_latest = reader.execute(_MAIN_LATEST_RUN_SQL, (_STRATEGY,)).fetchone()
        assert main_latest == (withheld["strategy_run_id"], True), main_latest

        latest = reader.execute(LATEST_RUN_SQL, (_STRATEGY,)).fetchone()
        assert (latest[0], latest[3]) == (scheduled["strategy_run_id"], True)
        assert reader.execute("select strategy_run_id from mart.governed_strategy_run").fetchall() == [
            (scheduled["strategy_run_id"],)
        ]
        head_decisions = reader.execute(
            "select issuer_id, peg is not null from mart.strategy_decisions where strategy_run_id = %s",
            (scheduled["strategy_run_id"],),
        ).fetchall()
        assert {(cell.subject_id, cell.answered) for cell in peg_cells(reader, run_id=head)} == set(head_decisions)
        assert peg_cells(reader, run_id=withheld["capture_run_id"]) != ()
        scheduled_confidence = _core_confidence(reader, scheduled["capture_run_id"])

    report = _served_report(tick_database_url)
    assert len(report.decisions) == 20
    assert all(decision.confidence == scheduled_confidence[decision.issuer_id] for decision in report.decisions)
