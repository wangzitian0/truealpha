"""The report-surface proof (#855 C1): every reader's own run selection against the head.

Driven by a connection that answers each reader's SQL the way the tables would, so the
verdict logic is pinned without a database; what the SQL says against REAL tables is the
nightly job's business, and the shape measured on staging on 2026-09-16 — rankings on the new
head, themes and coverage on the previous one — is the first case below.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from data_engine.quality import surface_proof
from data_engine.quality.surface_proof import prove, summary_lines

NOW = datetime(2026, 9, 16, 0, 15, tzinfo=UTC)
NEW = "capture-run:" + "a" * 64
OLD = "capture-run:" + "b" * 64
QQQ = "capture-run:" + "c" * 64
TOPT_UNIVERSE = "universe:topt-us-2026-03-31"
QQQ_UNIVERSE = "universe:qqq-us-2026"
REPORT = {"questions": {"q1": {"answered": 18, "unavailable": {"x": 2}, "missing": 0}}}


class _Tables:
    """Answers each reader's query from a small dict of what the tables hold."""

    def __init__(
        self,
        *,
        strategy=None,
        themes=None,
        holdings=None,
        funds=0,
        coverage=(),
        partitions=0,
        theme_runs=None,
        theme_universes=None,
        environment="staging",
    ):
        self.strategy, self.themes, self.holdings, self.funds, self.coverage = (
            strategy,
            themes,
            holdings,
            funds,
            coverage,
        )
        self.partitions = partitions
        #: What this database declares itself to be (#756). Deliberately NOT "production":
        #: the literal these call sites used to name was `production`, so a fake that
        #: declared the same thing would pass whether or not the resolution was converted.
        self.environment = environment
        #: Runs with purity rows; by default only the one the reader serves.
        self.theme_runs = set(theme_runs) if theme_runs is not None else ({themes} if themes else set())
        #: Universe prefixes whose pointer ever named a run with purity rows; by default the
        #: rows there are TOPT's.
        self.theme_universes = (
            set(theme_universes) if theme_universes is not None else ({"universe:topt-"} if self.theme_runs else set())
        )
        self._rows: list = []

    def execute(self, sql, params=()):
        text = " ".join(sql.split())
        if text == "select environment from mart.environment_identity":
            # #756: the proof resolves heads under the environment this database declares
            # rather than under a literal. Matched on the WHOLE statement, not as a
            # substring: the head queries embed this same text as a subquery, and a
            # substring test answered them with an environment string instead of a run id.
            self._rows = [(self.environment,)]
        elif "from mart.current_pointer p join mart.issuer_theme_purity" in text:
            self._rows = [(params[2].removesuffix("%") in self.theme_universes,)]
        elif "from mart.issuer_theme_purity where run_id = %s" in text:
            self._rows = [(params[0] in self.theme_runs,)]
        elif "mart.governed_strategy_run" in text:
            self._rows = [(self.strategy, "strategy-run:x")] if self.strategy else []
        elif "from mart.issuer_theme_purity group by run_id" in text:
            self._rows = [(self.themes, NOW)] if self.themes else []
        elif "from mart.current_pointer_head" in text:
            self._rows = [(self.holdings,)] if self.holdings else []
        elif "from mart.fund_virtual_company" in text:
            self._rows = [(self.funds,)]
        elif "from mart.question_coverage_report" in text:
            self._rows = list(self.coverage)
        elif "from staging.issuer_segment_revenue_facts" in text:
            self._rows = [(self.partitions,)]
        else:
            raise AssertionError(f"unexpected query: {text[:80]}")
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def _heads(monkeypatch, *, topt=NEW, qqq=QQQ, topt_cutoff=NOW, qqq_cutoff=NOW):
    def governed_head(_connection, *, universe_prefix):
        # #756: no environment argument to check. The signature is the assertion now -- a
        # caller that still names one raises TypeError here instead of quietly resolving a
        # lineage this database stopped advancing.
        is_topt = universe_prefix.startswith("universe:topt")
        run = topt if is_topt else qqq
        universe_id = TOPT_UNIVERSE if is_topt else QQQ_UNIVERSE
        cutoff = topt_cutoff if is_topt else qqq_cutoff
        return None if run is None else surface_proof.GovernedHead(universe_id, run, cutoff)

    monkeypatch.setattr(surface_proof, "governed_head", governed_head)


def _reports(monkeypatch, fresh):
    monkeypatch.setattr(surface_proof, "compile_report", lambda *_a, **_k: fresh)


def test_a_surface_serving_the_previous_head_is_a_mismatch_by_name(monkeypatch) -> None:
    """Staging, 2026-09-16 00:00: the 22:45 tick advanced the topt head; rankings followed it,
    themes and the coverage report still served yesterday's."""
    _heads(monkeypatch, qqq=None)
    _reports(monkeypatch, REPORT)
    tables = _Tables(strategy=NEW, themes=OLD, coverage=[(TOPT_UNIVERSE, OLD, REPORT)])
    verdicts = prove(tables, executed_at=NOW)
    by_surface = {v.surface: v for v in verdicts}
    assert by_surface["/research/rankings, /strategy, /compare, /trace, /coverage"].ok
    assert not by_surface["/research/themes"].ok
    assert not by_surface["/admin/datahub coverage [topt]"].ok
    assert by_surface["/research/holdings"].ok, "no QQQ head and nothing served: not run here, not a mismatch"
    assert summary_lines(verdicts)[-1].endswith("2 do not")
    assert summary_lines(verdicts)[1].startswith("MISMATCH /research/themes: serves capture-run:bbbbbbbbbbbb")


def test_every_surface_on_the_head_with_an_agreeing_report_is_green(monkeypatch) -> None:
    _heads(monkeypatch)
    _reports(monkeypatch, REPORT)
    tables = _Tables(
        strategy=NEW,
        themes=NEW,
        holdings=QQQ,
        funds=1,
        coverage=[(QQQ_UNIVERSE, QQQ, REPORT), (TOPT_UNIVERSE, NEW, REPORT)],
    )
    verdicts = prove(tables, executed_at=NOW)
    assert all(v.ok for v in verdicts), [v.line for v in verdicts if not v.ok]
    assert summary_lines(verdicts)[-1] == "report surface proof: 5/5 surfaces serve the governed head"


def test_a_stored_report_the_tables_no_longer_agree_with_is_stale_even_on_the_right_run(monkeypatch) -> None:
    """The run id can be right and the numbers wrong: purity rows written after the report
    was stored change q6, and the page would show the old count until the next weekly run."""
    _heads(monkeypatch, qqq=None)
    fresh = {"questions": {"q1": {"answered": 19, "unavailable": {"x": 1}, "missing": 0}}}
    _reports(monkeypatch, fresh)
    tables = _Tables(strategy=NEW, themes=NEW, coverage=[(TOPT_UNIVERSE, NEW, REPORT)])
    coverage = next(v for v in prove(tables, executed_at=NOW) if v.surface == "/admin/datahub coverage [topt]")
    assert coverage.served_run == NEW and not coverage.ok
    assert coverage.detail == "q1: stored 18 answered, tables say 19"


def test_a_head_with_no_strategy_run_and_a_holdings_head_with_no_fund_row_are_named(monkeypatch) -> None:
    _heads(monkeypatch)
    _reports(monkeypatch, REPORT)
    tables = _Tables(strategy=None, themes=NEW, holdings=QQQ, funds=0, coverage=[])
    by_surface = {v.surface: v for v in prove(tables, executed_at=NOW)}
    strategy = by_surface["/research/rankings, /strategy, /compare, /trace, /coverage"]
    assert not strategy.ok and strategy.detail == "the view is empty: no strategy run at the head's cutoff"
    holdings = by_surface["/research/holdings"]
    assert holdings.served_run == QQQ and not holdings.ok, "the pointer is right and the page has nothing to value"
    assert by_surface["/admin/datahub coverage [topt]"].detail == "no stored report"


def test_the_coverage_report_is_matched_to_the_heads_own_universe_id(monkeypatch) -> None:
    """Two TOPT partitions share a prefix; the report that counts is the one stored for the
    universe id the head names, not the first row whose id starts the same way (review on #859)."""
    _heads(monkeypatch, qqq=None)
    _reports(monkeypatch, REPORT)
    other = ("universe:topt-us-2025-12-31", OLD, REPORT)
    tables = _Tables(strategy=NEW, themes=NEW, coverage=[other, (TOPT_UNIVERSE, NEW, REPORT)])
    coverage = next(v for v in prove(tables, executed_at=NOW) if v.surface == "/admin/datahub coverage [topt]")
    assert coverage.ok and coverage.served_run == NEW


# --- a universe that has not settled is in progress, never a mismatch (2026-09-17) -------

QQQ_OLD = "capture-run:" + "d" * 64


def test_production_2026_09_17_names_both_mismatches_and_what_the_theme_plane_holds(monkeypatch) -> None:
    """The night this file's IN-PROGRESS state and the theme detail were written for: QQQ's
    coverage report one head behind (head reports had run before the tick committed), and no
    theme purity row at all, on an environment whose model provider IS seated."""
    from data_engine.config import settings

    monkeypatch.setattr(settings, "llm_api_key", "seated")
    _heads(monkeypatch)
    _reports(monkeypatch, REPORT)
    tables = _Tables(
        strategy=NEW,
        themes=None,
        holdings=QQQ,
        funds=1,
        coverage=[(QQQ_UNIVERSE, QQQ_OLD, REPORT), (TOPT_UNIVERSE, NEW, REPORT)],
        partitions=0,
    )
    verdicts = prove(tables, executed_at=NOW)
    lines = summary_lines(verdicts)
    assert lines[-1] == "report surface proof: 3/5 surfaces serve the governed head; 2 do not"
    themes = next(v for v in verdicts if v.surface == "/research/themes")
    assert themes.state == "MISMATCH"
    assert "no segment partition is knowable at the head's cutoff" in themes.line


def test_a_mismatch_in_a_settling_universe_is_in_progress_and_named(monkeypatch) -> None:
    _heads(monkeypatch)
    _reports(monkeypatch, REPORT)
    tables = _Tables(
        strategy=NEW,
        themes=OLD,
        holdings=QQQ,
        funds=1,
        coverage=[(QQQ_UNIVERSE, QQQ_OLD, REPORT), (TOPT_UNIVERSE, NEW, REPORT)],
    )
    why = "head_reports_pipeline run 1a2b3c4d is started"
    verdicts = prove(tables, executed_at=NOW, settling={"universe-list:qqq": why})
    by_surface = {v.surface: v for v in verdicts}

    qqq = by_surface["/admin/datahub coverage [universe-list:qqq]"]
    assert (qqq.state, qqq.ok, qqq.mismatched) == ("IN-PROGRESS", False, False)
    assert qqq.line.startswith("IN-PROGRESS /admin/datahub coverage [universe-list:qqq]") and qqq.line.endswith(why)
    # A matching surface of the settling universe is still a match: what it serves is proven.
    assert by_surface["/research/holdings"].state == "MATCH"
    # A quiet universe's mismatch is not excused by another universe settling.
    assert by_surface["/research/themes"].state == "MISMATCH"
    assert summary_lines(verdicts)[-1] == (
        "report surface proof: 3/5 surfaces serve the governed head; 1 in progress; 1 do not"
    )


def test_the_theme_lane_is_not_run_where_no_model_provider_is_seated(monkeypatch) -> None:
    """Holdings' notion, for themes: a lane that cannot run here has nothing to serve wrongly."""
    from data_engine.config import settings

    monkeypatch.setattr(settings, "llm_api_key", "")
    _heads(monkeypatch, qqq=None)
    _reports(monkeypatch, REPORT)
    tables = _Tables(strategy=NEW, themes=None, coverage=[(TOPT_UNIVERSE, NEW, REPORT)])
    themes = next(v for v in prove(tables, executed_at=NOW) if v.surface == "/research/themes")
    assert themes.ok and themes.served_run == themes.expected_run == "not-run-in-this-environment"
    assert "no model provider seated" in themes.line

    # Rows written while the lane was on are still served, so they are still judged.
    tables = _Tables(strategy=NEW, themes=OLD, coverage=[(TOPT_UNIVERSE, NEW, REPORT)])
    themes = next(v for v in prove(tables, executed_at=NOW) if v.surface == "/research/themes")
    assert themes.state == "MISMATCH" and themes.served_run == OLD


def test_a_theme_lane_that_is_on_and_produced_nothing_is_a_mismatch(monkeypatch) -> None:
    from data_engine.config import settings

    monkeypatch.setattr(settings, "llm_api_key", "seated")
    _heads(monkeypatch, qqq=None)
    _reports(monkeypatch, REPORT)
    tables = _Tables(strategy=NEW, themes=None, coverage=[(TOPT_UNIVERSE, NEW, REPORT)], partitions=12)
    themes = next(v for v in prove(tables, executed_at=NOW) if v.surface == "/research/themes")
    assert themes.state == "MISMATCH" and themes.served_run is None
    assert (
        themes.detail == "no theme purity rows at all, though 12 segment partition(s) are knowable at the head's cutoff"
    )


# --- the theme reader is universe-blind: its head is the newest covered one (#910) -------

TOPT_CUTOFF = datetime(2026, 9, 16, 22, 15, tzinfo=UTC)
QQQ_CUTOFF = datetime(2026, 9, 16, 23, 20, tzinfo=UTC)
TOPT_HEAD = "capture-run:c2a75d9baef8" + "0" * 52
QQQ_HEAD = "capture-run:a62da9660ff9" + "0" * 52
BOTH_COVERED = {"universe:topt-", "universe:qqq-"}
SETTLING = "head_reports_pipeline run 1a2b3c4d is started"


def _production(monkeypatch) -> None:
    """Production on 2026-09-17: the QQQ tick cuts off at 23:20, an hour after TOPT's 22:15."""
    _heads(monkeypatch, topt=TOPT_HEAD, qqq=QQQ_HEAD, topt_cutoff=TOPT_CUTOFF, qqq_cutoff=QQQ_CUTOFF)
    _reports(monkeypatch, REPORT)


def _production_tables(*, themes, theme_runs, theme_universes=BOTH_COVERED) -> _Tables:
    return _Tables(
        strategy=TOPT_HEAD,
        themes=themes,
        holdings=QQQ_HEAD,
        funds=1,
        coverage=[(QQQ_UNIVERSE, QQQ_HEAD, REPORT), (TOPT_UNIVERSE, TOPT_HEAD, REPORT)],
        theme_runs=theme_runs,
        theme_universes=theme_universes,
    )


def _themes(verdicts):
    return next(v for v in verdicts if v.surface == "/research/themes")


def test_production_2026_09_17_themes_serve_the_qqq_head_whose_tick_cuts_off_last(monkeypatch) -> None:
    """Run 4c7b4cdb on v0.0.83: both heads had purity rows and the reader, which has no
    universe filter, served QQQ's (cutoff 23:20). The proof expected TOPT's (22:15) and called
    a governed head a MISMATCH, which it would have done every night."""
    _production(monkeypatch)
    tables = _production_tables(themes=QQQ_HEAD, theme_runs={TOPT_HEAD, QQQ_HEAD})
    verdicts = prove(tables, executed_at=NOW)
    themes = _themes(verdicts)
    assert (themes.state, themes.universe, themes.expected_run) == ("MATCH", "universe-list:qqq", QQQ_HEAD)
    assert themes.line == (
        "MATCH /research/themes: serves capture-run:a62da9660ff9 vs head capture-run:a62da9660ff9"
        " — the universe-list:qqq head, the newest of a universe with theme rows"
    )
    assert summary_lines(verdicts)[-1] == "report surface proof: 5/5 surfaces serve the governed head"


def test_a_themes_page_serving_no_governed_head_is_still_a_mismatch(monkeypatch) -> None:
    """A run no pointer names outranks both heads (a hand-run purity pass, a pointer that moved
    back): the page is stale whichever head it should serve, and another universe settling
    does not excuse it."""
    _production(monkeypatch)
    tables = _production_tables(themes=OLD, theme_runs={TOPT_HEAD, QQQ_HEAD, OLD})
    themes = _themes(prove(tables, executed_at=NOW))
    assert (themes.state, themes.universe, themes.expected_run, themes.served_run) == (
        "MISMATCH",
        "universe-list:qqq",
        QQQ_HEAD,
        OLD,
    )
    assert themes.detail == "the served run is no universe's governed head"
    assert _themes(prove(tables, executed_at=NOW, settling={"topt": SETTLING})).state == "MISMATCH"

    # Rows only under runs no pointer ever named: every head is a candidate, the newest is named.
    tables = _production_tables(themes=OLD, theme_runs={OLD}, theme_universes=set())
    themes = _themes(prove(tables, executed_at=NOW))
    assert (themes.state, themes.universe, themes.expected_run) == ("MISMATCH", "universe-list:qqq", QQQ_HEAD)
    assert themes.detail == (
        "no run a governed pointer named has theme rows; the served run is no universe's governed head"
    )

    # The previous QQQ head, still served after QQQ advanced and before its purity rows landed,
    # on a day TOPT did not tick: named for QQQ, whose head reports are what the page waits on.
    _heads(
        monkeypatch,
        topt=TOPT_HEAD,
        qqq=QQQ_HEAD,
        topt_cutoff=datetime(2026, 9, 15, 22, 15, tzinfo=UTC),
        qqq_cutoff=QQQ_CUTOFF,
    )
    tables = _production_tables(themes=QQQ_OLD, theme_runs={TOPT_HEAD, QQQ_OLD})
    themes = _themes(prove(tables, executed_at=NOW))
    assert (themes.state, themes.universe) == ("MISMATCH", "universe-list:qqq")
    assert themes.detail == (
        "the universe-list:qqq head, the newest of a universe with theme rows, has none;"
        " the served run is no universe's governed head"
    )
    assert _themes(prove(tables, executed_at=NOW, settling={"universe-list:qqq": SETTLING})).state == "IN-PROGRESS"


def test_a_newer_qqq_head_without_theme_rows_is_judged_as_qqqs_while_topts_head_is_served(monkeypatch) -> None:
    """QQQ's tick committed; its head reports have not written purity rows, so the reader still
    serves TOPT's head. The page will move when QQQ's rows land, so the verdict is QQQ's: its
    head-reports run in flight is IN-PROGRESS, TOPT settling excuses nothing, and a quiet QQQ in
    this state is a MISMATCH. Partitions only accumulate, so a universe the lane has covered
    gets rows again at its next head; none means its head reports did not do their job."""
    _production(monkeypatch)
    tables = _production_tables(themes=TOPT_HEAD, theme_runs={TOPT_HEAD, QQQ_OLD})
    quiet = _themes(prove(tables, executed_at=NOW))
    assert (quiet.state, quiet.universe, quiet.expected_run, quiet.served_run) == (
        "MISMATCH",
        "universe-list:qqq",
        QQQ_HEAD,
        TOPT_HEAD,
    )
    assert quiet.detail == (
        "the universe-list:qqq head, the newest of a universe with theme rows, has none;"
        " the served run is the topt head"
    )
    assert _themes(prove(tables, executed_at=NOW, settling={"topt": SETTLING})).state == "MISMATCH"
    waiting = _themes(prove(tables, executed_at=NOW, settling={"universe-list:qqq": SETTLING}))
    assert waiting.state == "IN-PROGRESS" and waiting.line.endswith(SETTLING)


def test_a_universe_the_theme_lane_never_covered_is_not_expected_on_the_themes_page(monkeypatch) -> None:
    """No run QQQ's pointer ever named has purity rows (its segment backfill has landed
    nothing): the reader cannot select a QQQ run, so TOPT's head is the one it serves."""
    _production(monkeypatch)
    tables = _production_tables(themes=TOPT_HEAD, theme_runs={TOPT_HEAD}, theme_universes={"universe:topt-"})
    themes = _themes(prove(tables, executed_at=NOW))
    assert (themes.state, themes.universe, themes.expected_run) == ("MATCH", "topt", TOPT_HEAD)


def test_staging_with_only_a_topt_head_serves_the_topt_head_on_the_themes_page(monkeypatch) -> None:
    """Staging captures no QQQ: TOPT's head is the only candidate, as before #910."""
    _heads(monkeypatch, topt=TOPT_HEAD, qqq=None, topt_cutoff=TOPT_CUTOFF)
    _reports(monkeypatch, REPORT)
    tables = _Tables(
        strategy=TOPT_HEAD,
        themes=TOPT_HEAD,
        coverage=[(TOPT_UNIVERSE, TOPT_HEAD, REPORT)],
        theme_runs={TOPT_HEAD, OLD},
        theme_universes={"universe:topt-"},
    )
    verdicts = prove(tables, executed_at=NOW)
    themes = _themes(verdicts)
    assert (themes.state, themes.universe, themes.expected_run) == ("MATCH", "topt", TOPT_HEAD)
    assert summary_lines(verdicts)[-1] == "report surface proof: 5/5 surfaces serve the governed head"


# --- the settling window, against the real schema ---------------------------------------


def _db():
    import os

    import psycopg
    import pytest
    from data_engine.config import settings

    try:
        return psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            raise
        pytest.skip("no local Postgres")


def _seed_head(
    connection, *, universe_id: str, run_id: str, previous: str | None = None, sequence: int = 0
) -> datetime:
    """One capture-run evidence node and the pointer row naming it; returns when the row was
    recorded. Rolled back by the caller."""
    import hashlib

    digest = run_id.split(":", 1)[1]
    connection.execute(
        """
        insert into staging.evidence_nodes (node_id, kind, content_sha256, valid_from, transaction_time, recorded_at)
        values (%s, 'capture_run', %s, %s, %s, %s)
        on conflict (node_id) do nothing
        """,
        (run_id, digest, NOW.date(), NOW, NOW),
    )
    pointer = hashlib.sha256(f"{universe_id}|{run_id}".encode()).hexdigest()
    return connection.execute(
        """
        insert into mart.current_pointer
            (pointer_id, content_sha256, environment, universe_id, universe_version, factor_id,
             target_run_id, sequence, previous_run_id, advanced_at)
        values (%s, %s, 'production', %s, 'v1', 'gross_profit_per_employee', %s, %s, %s, %s)
        returning created_at
        """,
        (f"current-pointer:{pointer}", pointer, universe_id, run_id, sequence, previous, NOW),
    ).fetchone()[0]


def _store_report(connection, *, universe_id: str, run_id: str) -> None:
    import hashlib

    digest = hashlib.sha256(f"report|{universe_id}|{run_id}".encode()).hexdigest()
    connection.execute(
        """
        insert into mart.question_coverage_report
            (report_id, content_sha256, universe_id, run_id, cutoff, requirements_sha256, payload)
        values (%s, %s, %s, %s, %s, %s, '{}'::jsonb)
        """,
        (f"question-coverage-report:{digest}", digest, universe_id, run_id, NOW, "0" * 64),
    )


def test_a_head_recorded_minutes_ago_without_its_reports_is_settling_and_then_is_not(monkeypatch) -> None:
    """The window between a tick's commit and the head-reports run the sensor launches for it:
    measured from when the pointer row was WRITTEN (`advanced_at` is the tick's cutoff, which
    the 2026-09-16 QQQ tick passed 35 minutes before it committed)."""
    from data_engine.quality.surface_proof import fresh_heads_without_reports

    universe_id = "universe:topt-settling-test"
    run = "capture-run:" + "e" * 64

    def governed_head(_connection, *, universe_prefix):
        if universe_prefix.startswith("universe:topt"):
            return surface_proof.GovernedHead(universe_id, run, NOW)
        return None

    monkeypatch.setattr(surface_proof, "governed_head", governed_head)
    grace = timedelta(minutes=10)
    connection = _db()
    try:
        recorded = _seed_head(connection, universe_id=universe_id, run_id=run)
        _store_report(connection, universe_id=universe_id, run_id="capture-run:" + "f" * 64)

        settling = fresh_heads_without_reports(connection, now=recorded + timedelta(minutes=3), grace=grace)
        assert list(settling) == ["topt"]
        assert settling["topt"] == f"head {run[:24]} recorded 3 min ago; its reports are not written yet"
        # Past the grace, the same state is the sensor's failure: judged, not waited on.
        assert fresh_heads_without_reports(connection, now=recorded + timedelta(minutes=11), grace=grace) == {}

        # Once a report names the head, nothing is settling however fresh the head is.
        _store_report(connection, universe_id=universe_id, run_id=run)
        assert fresh_heads_without_reports(connection, now=recorded + timedelta(minutes=1), grace=grace) == {}
    finally:
        connection.rollback()
        connection.close()


def _seed_theme_row(connection, *, run_id: str, cutoff: datetime) -> None:
    """One purity row for `run_id`: the only thing the theme queries look at. Rolled back by the caller."""
    connection.execute(
        """
        insert into mart.issuer_theme_purity
            (run_id, issuer_id, cik, theme_id, theme, definition_version, definition_sha256, cutoff,
             period_end, partition_id, theme_share, consolidated_revenue, in_theme_revenue,
             out_of_theme_revenue, unclassified_revenue, partition_residual, segments, confidence,
             extractor, availability_status, source_evidence_status, factor_validation_status)
        values (%s, 'issuer:themes-910-test', %s, 'ai-compute', 'AI compute', 'v1', %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                'rule:themes-910-test', 'available', 'verified', 'accepted')
        """,
        (
            run_id,
            910,
            "0" * 64,
            cutoff,
            cutoff.date(),
            "segment-partition:" + "0" * 64,
            "0.5",
            "100",
            "50",
            "50",
            "0",
            "0",
            2,
            "0.9",
        ),
    )


def test_the_theme_lane_covers_a_universe_once_a_run_its_pointer_named_has_rows(monkeypatch) -> None:
    """The two theme queries against the real schema (#910): a universe the lane never covered
    is not expected; once one of its runs has rows its newer head is, and is named as having
    none until its own rows land."""
    from data_engine.quality.surface_proof import themes_verdict

    topt_universe, qqq_universe = "universe:topt-themes-910-test", "universe:qqq-themes-910-test"
    monkeypatch.setattr(
        surface_proof,
        "UNIVERSE_PREFIXES",
        {"universe-list:qqq": "universe:qqq-themes-910-", "topt": "universe:topt-themes-910-"},
    )
    topt_run, qqq_old, qqq_run = ("capture-run:" + digit * 64 for digit in "789")
    heads = {
        "topt": surface_proof.GovernedHead(topt_universe, topt_run, TOPT_CUTOFF),
        "universe-list:qqq": surface_proof.GovernedHead(qqq_universe, qqq_run, QQQ_CUTOFF),
    }
    connection = _db()
    try:
        _seed_head(connection, universe_id=topt_universe, run_id=topt_run)
        _seed_head(connection, universe_id=qqq_universe, run_id=qqq_old)
        _seed_head(connection, universe_id=qqq_universe, run_id=qqq_run, previous=qqq_old, sequence=1)
        _seed_theme_row(connection, run_id=topt_run, cutoff=TOPT_CUTOFF)

        never_covered = themes_verdict(connection, heads, topt_run)
        assert (never_covered.state, never_covered.universe) == ("MATCH", "topt")

        _seed_theme_row(connection, run_id=qqq_old, cutoff=QQQ_CUTOFF - timedelta(days=1))
        waiting = themes_verdict(connection, heads, topt_run)
        assert (waiting.state, waiting.universe, waiting.expected_run) == ("MISMATCH", "universe-list:qqq", qqq_run)
        assert waiting.detail.startswith(
            "the universe-list:qqq head, the newest of a universe with theme rows, has none"
        )

        _seed_theme_row(connection, run_id=qqq_run, cutoff=QQQ_CUTOFF)
        landed = themes_verdict(connection, heads, qqq_run)
        assert (landed.state, landed.universe) == ("MATCH", "universe-list:qqq")
    finally:
        connection.rollback()
        connection.close()
