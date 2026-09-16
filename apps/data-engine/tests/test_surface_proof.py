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
REPORT = {"questions": {"q1": {"answered": 18, "unavailable": {"x": 2}, "missing": 0}}}


class _Tables:
    """Answers each reader's query from a small dict of what the tables hold."""

    def __init__(self, *, strategy=None, themes=None, holdings=None, funds=0, coverage=()):
        self.strategy, self.themes, self.holdings, self.funds, self.coverage = (
            strategy,
            themes,
            holdings,
            funds,
            coverage,
        )
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
        return None if run is None else surface_proof.GovernedHead(universe_prefix + "us-2026", run, NOW)

    monkeypatch.setattr(surface_proof, "governed_head", governed_head)


def _reports(monkeypatch, fresh):
    monkeypatch.setattr(surface_proof, "compile_report", lambda *_a, **_k: fresh)


def test_a_surface_serving_the_previous_head_is_a_mismatch_by_name(monkeypatch) -> None:
    """Staging, 2026-09-16 00:00: the 22:45 tick advanced the topt head; rankings followed it,
    themes and the coverage report still served yesterday's."""
    _heads(monkeypatch, qqq=None)
    _reports(monkeypatch, REPORT)
    tables = _Tables(strategy=NEW, themes=OLD, coverage=[("universe:topt-us-2026-03-31", OLD, REPORT)])
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
        coverage=[("universe:qqq-us-2026", QQQ, REPORT), ("universe:topt-us-2026-03-31", NEW, REPORT)],
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
    tables = _Tables(strategy=NEW, themes=NEW, coverage=[("universe:topt-us-2026-03-31", NEW, REPORT)])
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
