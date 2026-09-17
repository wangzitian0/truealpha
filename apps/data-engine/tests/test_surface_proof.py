"""The report-surface proof (#855 C1): every reader's own run selection against the head.

Driven by a connection that answers each reader's SQL the way the tables would, so the
verdict logic is pinned without a database; what the SQL says against REAL tables is the
nightly job's business, and the shape measured on staging on 2026-09-16 — rankings on the new
head, themes and coverage on the previous one — is the first case below.
"""

from __future__ import annotations

from datetime import UTC, datetime

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

    def __init__(self, *, strategy=None, themes=None, holdings=None, funds=0, coverage=(), partitions=0):
        self.strategy, self.themes, self.holdings, self.funds, self.coverage = (
            strategy,
            themes,
            holdings,
            funds,
            coverage,
        )
        self.partitions = partitions
        self._rows: list = []

    def execute(self, sql, params=()):
        text = " ".join(sql.split())
        if "mart.governed_strategy_run" in text:
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


def _heads(monkeypatch, *, topt=NEW, qqq=QQQ):
    def governed_head(_connection, *, universe_prefix, environment):
        assert environment == "production"
        run = topt if universe_prefix.startswith("universe:topt") else qqq
        universe_id = TOPT_UNIVERSE if universe_prefix.startswith("universe:topt") else QQQ_UNIVERSE
        return None if run is None else surface_proof.GovernedHead(universe_id, run, NOW)

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


def _seed_head(connection, *, universe_id: str, run_id: str) -> datetime:
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
        values (%s, %s, 'production', %s, 'v1', 'gross_profit_per_employee', %s, 0, null, %s)
        returning created_at
        """,
        (f"current-pointer:{pointer}", pointer, universe_id, run_id, NOW),
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
    from datetime import timedelta

    from data_engine.quality.surface_proof import fresh_heads_without_reports

    universe_id = "universe:topt-settling-test"
    run = "capture-run:" + "e" * 64

    def governed_head(_connection, *, universe_prefix, environment):
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
