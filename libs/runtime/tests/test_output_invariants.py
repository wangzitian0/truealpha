"""Tests for tools/output_invariants.py — #581.

The SQL is verified against production separately (the tool's verbatim queries
return JPM's negative GPPE and the 67.1h pointer lag there). These cover the
part SQL cannot: what the tool does with a violation, and what an exemption is
allowed to do.

The exemption mechanism is the load-bearing piece. Invariant suites die by
accumulating permanent waivers, so an exemption here defers and never disables:
it carries an issue and a date, an expired one fails anyway, and one left behind
after its defect is fixed also fails.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from truealpha_runtime.testing import load_tool

REPO_ROOT = Path(__file__).resolve().parents[3]
_module = load_tool("output_invariants")

check = _module.check
Invariant = _module.Invariant
Exemption = _module.Exemption

TODAY = date(2026, 8, 17)

ONE = Invariant(id="one", claim="a value must be possible", violations="V", population="P")


class _Cursor:
    def __init__(self, answers: dict[str, list[tuple]]) -> None:
        self._answers = answers
        self._rows: list[tuple] = []

    def execute(self, sql: str) -> None:
        self._rows = self._answers[sql.strip()]

    def fetchall(self) -> list[tuple]:
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        return None


class _Connection:
    def __init__(self, answers: dict[str, list[tuple]]) -> None:
        self._answers = answers

    def cursor(self) -> _Cursor:
        return _Cursor(self._answers)

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _db(violations: list[tuple], population: int):
    answers = {"V": violations, "P": [(population,)]}
    return lambda _url: _Connection(answers)


def _run(violations, population, exemptions=None, **kwargs):
    return check(
        "postgresql://unused",
        today=TODAY,
        exemptions=exemptions or {},
        invariants=(ONE,),
        connect=_db(violations, population),
        **kwargs,
    )


def test_a_holding_invariant_passes_and_reports_what_it_examined(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run([], 20) == 0
    assert "20 row(s) examined" in capsys.readouterr().out


def test_a_violation_fails_and_prints_the_offending_rows(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The production shape: one row, named, with its value."""
    assert _run([("listing:xnys:jpm", "-528985.79", "financial")], 20) == 1
    stderr = capsys.readouterr().err
    assert "listing:xnys:jpm" in stderr and "-528985.79" in stderr
    assert "a value must be possible" in stderr, "the claim must travel with the failure"


def test_an_unexpired_exemption_defers_rather_than_hides(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exemptions = {"one": Exemption("one", "#528", date(2026, 9, 16), "the financial branch")}
    assert _run([("jpm",)], 20, exemptions) == 0
    out = capsys.readouterr().out
    assert "DEFERRED" in out
    assert "#528" in out and "2026-09-16" in out, "a deferral must name its issue and its end"


def test_an_expired_exemption_fails_anyway(capsys: pytest.CaptureFixture[str]) -> None:
    """The whole point. `# TODO: exclude JPM` is how invariant suites die."""
    exemptions = {"one": Exemption("one", "#528", date(2026, 8, 16), "the financial branch")}
    assert _run([("jpm",)], 20, exemptions) == 1
    assert "expired 2026-08-16" in capsys.readouterr().err


def test_an_exemption_outliving_its_defect_fails(capsys: pytest.CaptureFixture[str]) -> None:
    """A standing waiver for something already fixed hides the next occurrence."""
    exemptions = {"one": Exemption("one", "#528", date(2026, 9, 16), "the financial branch")}
    assert _run([], 20, exemptions) == 1
    assert "no longer violated across 20 row(s)" in capsys.readouterr().err


def test_an_exemption_is_not_judged_stale_over_no_rows(capsys: pytest.CaptureFixture[str]) -> None:
    """Found by running it: on the CI fixture, which has neither JPM nor a
    pointer row, "exempt but no longer violated" fired for both shipped
    exemptions. An invariant that examined nothing proved nothing — including
    about its own waiver."""
    exemptions = {"one": Exemption("one", "#528", date(2026, 9, 16), "the financial branch")}
    assert _run([], 0, exemptions) == 0
    out = capsys.readouterr().out
    assert "proved nothing" in out
    assert "no longer violated" not in out


def test_an_invariant_over_no_rows_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    """A guard reporting ok over an empty set is worse than no guard. The CI
    fixture cannot populate every plane, so this is reported there."""
    assert _run([], 0) == 0
    assert "examined 0 rows, so it proved nothing" in capsys.readouterr().out


def test_coverage_can_be_required_where_the_data_is_real(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run([], 0, require_coverage=True) == 1
    assert "examined 0 rows where real data was required" in capsys.readouterr().err


def test_the_governed_head_matches_the_consumer_that_serves_it() -> None:
    """Review caught the invariants inventing their own run resolution: `order by
    created_at desc` picked a GPPE run nothing serves — production's pointer
    targets capture-run:8c49eec8… while the newest row belongs to
    capture-run:49a1f57e…. Checking a run no consumer reads is not checking a
    published number.

    So this asserts the Python and the TypeScript scope the head identically. The
    duplication is deliberate (a Python tool cannot call a TS repository) and
    therefore needs a guard — #462's defect is exactly this read reimplemented
    without one."""
    ts = (REPO_ROOT / "apps/app-web/src/server/mart/topt-gppe-repository.ts").read_text()
    head = ts.split("POINTER_HEAD_SQL", 1)[1].split("`", 2)[1]
    for predicate in (
        "current_pointer_head",
        "environment = 'production'",
        "factor_id = 'gross_profit_per_employee'",
        "order by advanced_at desc",
    ):
        assert predicate in head, f"the consumer no longer scopes the head by {predicate!r}"
        assert predicate in _module.GOVERNED_HEAD, (
            f"the invariants resolve the governed head without {predicate!r}, so they check a run "
            f"the App does not serve"
        )


def _run_ordering(sql: str) -> str:
    """The `order by` clause of a `mart.strategy_runs` read, whitespace-normalised.

    Comparing the two sides to EACH OTHER, rather than each to a literal typed
    in this file, is the whole point — see the test below.

    Case-insensitive and tolerant of an absent `limit`, so that reformatting one
    side to `ORDER BY` reads as the same ordering rather than as drift. It still
    fails closed: a missing `order by` raises here instead of returning a slice
    that happens to compare equal (review).
    """
    body = " ".join(sql.split()).lower()
    assert "from mart.strategy_runs" in body, f"not a strategy_runs read: {body!r}"
    assert "order by" in body, f"the read no longer orders its runs at all: {body!r}"
    return body.split("order by", 1)[1].split(" limit", 1)[0].strip().rstrip("`").strip()


def test_the_strategy_run_matches_the_consumer_that_serves_it() -> None:
    """Same, for the strategy plane: the App surfaces the latest run PER
    strategy_key, not the latest across all strategies.

    The first version asserted the ordering against a literal on the PYTHON side
    and checked the TypeScript side only for `where strategy_key = $1`. Breaking
    the consumer's ordering on purpose walked straight past it — a one-sided
    consistency check, which is #462's defect committed inside the guard written
    to prevent #462. Both sides are now read out and compared to each other, so
    changing either alone is what turns this red.
    """
    ts = (REPO_ROOT / "apps/app-web/src/server/mart/strategy-run-repository.ts").read_text()
    consumer = ts.split("LATEST_RUN_SQL", 1)[1].split("`", 2)[1]
    assert "where strategy_key = $1" in " ".join(consumer.split())
    assert f"strategy_key = '{_module.DASHBOARD_STRATEGY}'" in " ".join(_module.LATEST_STRATEGY_RUN.split())
    served, judged = _run_ordering(consumer), _run_ordering(_module.LATEST_STRATEGY_RUN)
    assert served == judged, (
        f"the App orders runs by {served!r} and the invariants by {judged!r}, so the invariants "
        f"judge a run nobody serves"
    )


def test_every_shipped_exemption_names_an_issue_and_a_future_date() -> None:
    """The file is the mechanism; a malformed entry silently disables a guard."""
    exemptions = _module.load_exemptions()
    ids = {invariant.id for invariant in _module.INVARIANTS}
    for name, exemption in exemptions.items():
        assert name in ids, f"{name} exempts an invariant that does not exist"
        assert exemption.issue.startswith("#"), f"{name} must name the issue that resolves it"
        assert exemption.reason.strip(), f"{name} must say why"
        assert exemption.expires > date(2026, 8, 17), (
            f"{name} ships already expired, which fails the gate on the first run"
        )
