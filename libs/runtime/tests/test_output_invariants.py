"""Tests for tools/output_invariants.py — #581.

The SQL is verified against production separately (the tool's verbatim queries
return JPM's negative GPPE and the 67.1h pointer lag there). These cover the
part SQL cannot: what the tool does with a violation, and what an exemption is
allowed to do.

The exemption mechanism is the load-bearing piece. Invariant suites die by
accumulating permanent waivers, so an exemption here defers and never disables:
it carries an issue and a date, an expired one fails anyway, and one left behind
after its defect is fixed also fails.

The tests at the end of this file run `quality-report-matches-core-results`
against a database that the migration chain builds (#1346).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb
from truealpha_runtime.testing import isolated_test_database, load_tool

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


def _db(violations: list[tuple], population: int, signals: list[tuple] | None = None):
    answers = {"V": violations, "P": [(population,)], "S": signals or []}
    return lambda _url: _Connection(answers)


def _run(violations, population, exemptions=None, *, invariant=ONE, signals=None, **kwargs):
    return check(
        "postgresql://unused",
        today=TODAY,
        exemptions=exemptions or {},
        invariants=(invariant,),
        connect=_db(violations, population, signals),
        **kwargs,
    )


SIGNALLING = Invariant(id="signs", claim="a sign means what the node says", violations="V", population="P", signals="S")


def test_a_declared_signal_holds_and_is_printed_by_name(capsys: pytest.CaptureFixture[str]) -> None:
    """#528: a negative value the node declares `sign-is-signal` is not a failure, and it is
    never a silent pass either — every one is printed, whatever else the invariant says."""
    jpm = ("listing:xnys:jpm", "financial", "gppe", "-514726.13", "sign-is-signal")
    assert _run([], 20, invariant=SIGNALLING, signals=[jpm]) == 0
    out = capsys.readouterr().out
    assert "  SIGNAL   signs: listing:xnys:jpm financial gppe -514726.13 sign-is-signal" in out
    assert "20 row(s) examined" in out
    # printed on a failing run too, beside the violation
    assert _run([("listing:x",)], 20, invariant=SIGNALLING, signals=[jpm]) == 1
    assert "SIGNAL" in capsys.readouterr().out


def test_the_sign_invariant_is_generated_from_the_forest() -> None:
    """The suite does not restate what a negative number means: its sign invariant is built
    from `factors.forest`, so the definition and the check cannot disagree again (#528: the
    old `gppe-not-negative` said the opposite of the definition it judged)."""
    ids = {invariant.id for invariant in _module.INVARIANTS}
    assert "node-sign-policy" in ids and "gppe-not-negative" not in ids
    (sign,) = [invariant for invariant in _module.INVARIANTS if invariant.id == "node-sign-policy"]
    from factors.forest import FOREST, PUBLISHED_COLUMNS

    for column, node_key in PUBLISHED_COLUMNS[_module.SIGN_POLICY_TABLE].items():
        assert f"'{column}' as published_column" in sign.violations
        assert str(FOREST.node(node_key).node_id) in sign.violations
    assert sign.signals is not None and "'sign-is-signal' as policy" in sign.signals
    assert _module.GOVERNED_HEAD in sign.violations and _module.GOVERNED_HEAD in sign.signals


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
        "served_head",
        "factor_id = 'gross_profit_per_employee'",
        "order by advanced_at desc",
    ):
        assert predicate in head, f"the consumer no longer scopes the head by {predicate!r}"
        assert predicate in _module.GOVERNED_HEAD, (
            f"the invariants resolve the governed head without {predicate!r}, so they check a run "
            f"the App does not serve"
        )
    # #756: Each consumer's head query must carry no environment literal
    assert "'production'" not in head, "consumer carries 'production' literal (#756)"
    assert "'staging'" not in head, "consumer carries 'staging' literal (#756)"
    assert "'production'" not in _module.GOVERNED_HEAD, "GOVERNED_HEAD carries 'production' literal (#756)"
    assert "'staging'" not in _module.GOVERNED_HEAD, "GOVERNED_HEAD carries 'staging' literal (#756)"


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
    assert "where r.strategy_key = $1" in " ".join(consumer.split())
    assert f"strategy_key = '{_module.DASHBOARD_STRATEGY}'" in " ".join(_module.LATEST_STRATEGY_RUN.split())
    served, judged = _run_ordering(consumer), _run_ordering(_module.LATEST_STRATEGY_RUN)
    assert served == judged, (
        f"the App orders runs by {served!r} and the invariants by {judged!r}, so the invariants "
        f"judge a run nobody serves"
    )


def test_no_exemption_defers_the_sign_policy() -> None:
    """#528: the per-node sign policy replaced the `gppe-not-negative` exemption; nothing
    defers it, so a value a node forbids fails the day it is published."""
    assert "node-sign-policy" not in _module.load_exemptions()
    assert all(exemption.issue != "#528" for exemption in _module.load_exemptions().values())


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


def test_extraction_citations_resolve_invariant_structure_and_behavior() -> None:
    """#1295, #735, #70, #1264: every issuer headcount fact must carry an 18-digit SEC accession and a
    real filing knowable_at, and 10k-extraction facts must resolve their evidence_ref pointer to
    raw.fetches. Superseded legacy rows in the append-only table are allowed only when an eligible
    cited row exists for that CIK."""
    (invariant,) = [inv for inv in _module.INVARIANTS if inv.id == "extraction-citations-resolve"]
    assert "staging.issuer_headcount_facts" in invariant.violations
    assert "raw.fetches" in invariant.violations
    assert "accession=([0-9]{18}|[0-9]{10}-[0-9]{2}-[0-9]{6})" in invariant.violations
    assert "accession=([0-9]{4}-[0-9]{2}-[0-9]{6})" in invariant.violations
    assert "with eligible as" in invariant.violations
    assert "2026-01-01T00:00:00Z" in invariant.violations
    assert "staging.issuer_headcount_facts" in invariant.population
    assert "where source = '10k-extraction'" not in invariant.population

    # Behavior check with mock database: holding passes, violation fails across sources
    answers = {
        invariant.violations.strip(): [],
        invariant.population.strip(): [(105,)],
    }
    holding = check(
        "postgresql://unused",
        today=TODAY,
        exemptions={},
        invariants=(invariant,),
        connect=lambda _url: _Connection(answers),
    )
    assert holding == 0

    for source in ("10k-extraction", "manual-review", "sec-10k-regex-v1", "curated-10k-2025"):
        violation = ("1", "1018724", source, "missing-accession", "none")
        answers[invariant.violations.strip()] = [violation]
        failing = check(
            "postgresql://unused",
            today=TODAY,
            exemptions={},
            invariants=(invariant,),
            connect=lambda _url: _Connection(answers),
        )
        assert failing == 1


def test_quality_report_matches_core_results_invariant_structure_and_behavior() -> None:
    """#1299, #1227, #1346: for every governed head in mart.served_head, the gross_profit_per_employee
    complete_subjects count in mart.datahub_quality_report must equal the number of listings of
    the available issuers in mart.topt_core_results. The DB tests below prove the SQL."""
    (invariant,) = [inv for inv in _module.INVARIANTS if inv.id == "quality-report-matches-core-results"]
    assert "mart.served_head" in invariant.violations
    assert "mart.datahub_quality_report" in invariant.violations
    assert "mart.topt_core_results" in invariant.violations
    assert "complete_subjects" in invariant.violations
    assert "availability = 'available'" in invariant.violations
    assert "mart.served_head" in invariant.population

    answers = {
        invariant.violations.strip(): [],
        invariant.population.strip(): [(2,)],
    }
    assert (
        check(
            "postgresql://unused",
            today=TODAY,
            exemptions={},
            invariants=(invariant,),
            connect=lambda _url: _Connection(answers),
        )
        == 0
    )

    answers[invariant.violations.strip()] = [
        ("universe:topt-us-2026-03-31", "capture-run:abc", "19", "20"),
    ]
    assert (
        check(
            "postgresql://unused",
            today=TODAY,
            exemptions={},
            invariants=(invariant,),
            connect=lambda _url: _Connection(answers),
        )
        == 1
    )


# --- quality-report-matches-core-results on a migrated database (#1346) --------------------
#
# The report counts subjects. A subject is a listing: the report keys its cells by
# `raw.capture_obligations.subject_id`. The core writes one result for each issuer, and a
# dual-class issuer (Alphabet) has two listings. The seeds have the shapes that the writers
# give. The snapshot payload holds one member for each listing. The member table and the core
# hold one row for each issuer.

PARITY_INVARIANT = "quality-report-matches-core-results"
GPPE_FACTOR = "gross_profit_per_employee"
#: 0042 requires observation_count = 4 * instrument_count on a core snapshot.
CELLS_PER_LISTING = 4
SEED_SHA256 = "0" * 64


@dataclass(frozen=True)
class SeededIssuer:
    """One issuer of a seeded head, with its listings, its report facts and its core result."""

    issuer_id: str
    listings: tuple[str, ...]
    facts_complete: bool = True
    available: bool = True


def _issuer(name: str, *tickers: str, facts_complete: bool = True, available: bool = True) -> SeededIssuer:
    listings = tuple(f"listing:xnas:{ticker}" for ticker in tickers)
    return SeededIssuer(f"issuer:{name}", listings, facts_complete, available)


def _singles(state: str, count: int, *, facts_complete: bool = True, available: bool = True) -> list[SeededIssuer]:
    """`count` issuers with one listing each, all in the same state."""
    return [
        _issuer(f"{state}-{index}", f"{state}{index}", facts_complete=facts_complete, available=available)
        for index in range(count)
    ]


ALPHABET = _issuer("alphabet", "goog", "googl")


def _digest(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _observations(listing: str) -> list[str]:
    return [f"observation:{_digest(listing, str(cell))}" for cell in range(CELLS_PER_LISTING)]


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[str]:
    """A database that only the migration chain built, so the seeded heads are its only heads."""
    with isolated_test_database("output_invariants", template=None) as database:
        yield str(database)


@pytest.fixture
def conn(migrated_database: str) -> Iterator[psycopg.Connection]:
    """A connection in a transaction that the test always rolls back."""
    with psycopg.connect(migrated_database) as connection:
        try:
            yield connection
        finally:
            connection.rollback()


def _seed_issuer(
    conn: psycopg.Connection, snapshot_id: str, run_id: str, universe_id: str, issuer: SeededIssuer
) -> None:
    """Write the member row and the core result of one issuer. Both use its first listing."""
    execution = issuer.listings[0]
    instrument_id = f"instrument:{execution}"
    factor_input = {
        "snapshot_id": snapshot_id,
        "issuer_id": issuer.issuer_id,
        "instrument_id": instrument_id,
        "listing_id": execution,
    }
    conn.execute(
        "insert into staging.topt_core_snapshot_members (snapshot_id, instrument_id, issuer_id, listing_id, "
        "observation_ids, member_sha256, factor_input) values (%s, %s, %s, %s, %s, %s, %s)",
        (
            snapshot_id,
            instrument_id,
            issuer.issuer_id,
            execution,
            sorted(item for listing in issuer.listings for item in _observations(listing)),
            _digest("member", snapshot_id, issuer.issuer_id),
            Jsonb(factor_input),
        ),
    )
    if issuer.available:
        value, tier, reasons = 1, "tech", []
    else:
        # #1309: an issuer with complete facts and no stored daily series is unavailable.
        value, tier = None, None
        reasons = ["no_split_adjusted_series"] if issuer.facts_complete else ["missing_gross_profit"]
    result_sha = _digest("result", snapshot_id, issuer.issuer_id)
    conn.execute(
        """
        insert into mart.topt_core_results (
            result_id, content_sha256, invocation_id, snapshot_id, run_id, release_manifest_id,
            universe_id, universe_version, universe_sha256, cutoff, issuer_id, instrument_id, listing_id,
            operating_branch, operating_metric, availability, operating_efficiency,
            capital_adjusted_gross_profit, gppe, tier, target_ps_lower, target_ps_upper,
            target_ps_midpoint, current_ps, valuation_gap, confidence, freshness, reason_codes,
            input_observation_ids, gppe_invocation_id, gppe_result_id, gppe_definition_id,
            gppe_definition_sha256, tier_definition_id, tier_definition_sha256, payload
        ) values (
            %(result_id)s, %(result_sha)s, %(invocation)s, %(snapshot)s, %(run)s, %(release)s,
            %(universe)s, 'v1', %(seed)s, '2026-03-31T20:00:00Z', %(issuer)s, %(instrument)s, %(listing)s,
            'non_financial', 'capital_adjusted_gppe', %(availability)s, %(value)s,
            %(value)s, %(value)s, %(tier)s, %(value)s, %(value)s,
            %(value)s, %(value)s, %(value)s, 0.9, 'fresh', %(reasons)s,
            %(observations)s, %(gppe_invocation)s, %(gppe_result)s, %(gppe_definition)s,
            %(seed)s, %(tier_definition)s, %(seed)s, '{}'::jsonb
        )
        """,
        {
            "result_id": f"topt-core-result:{result_sha}",
            "result_sha": result_sha,
            "invocation": f"topt-core-invocation:{_digest('core', snapshot_id)}",
            "snapshot": snapshot_id,
            "run": run_id,
            "release": f"release-manifest:{SEED_SHA256}",
            "universe": universe_id,
            "seed": SEED_SHA256,
            "issuer": issuer.issuer_id,
            "instrument": instrument_id,
            "listing": execution,
            "availability": "available" if issuer.available else "unavailable",
            "value": value,
            "tier": tier,
            "reasons": reasons,
            "observations": sorted(item for listing in issuer.listings for item in _observations(listing)),
            "gppe_invocation": f"topt-gppe-invocation:{_digest('gppe', snapshot_id)}",
            "gppe_result": f"topt-gppe-result:{_digest('gppe', snapshot_id, issuer.issuer_id)}",
            "gppe_definition": f"gppe-definition:{SEED_SHA256}",
            "tier_definition": f"three-tier-definition:{SEED_SHA256}",
        },
    )


def _seed_head(
    conn: psycopg.Connection, universe_id: str, issuers: Sequence[SeededIssuer], *, core: bool = True
) -> str:
    """Serve one governed head and write its quality report. With `core`, also write its core.

    The report counts one complete subject for each listing whose facts are complete.
    Returns the run id of the head."""
    run_sha = _digest("run", universe_id)
    run_id = f"capture-run:{run_sha}"
    environment = conn.execute("select environment from mart.environment_identity").fetchone()
    assert environment is not None
    conn.execute(
        "insert into staging.evidence_nodes (node_id, kind, content_sha256, valid_from, transaction_time, "
        "recorded_at) values (%s, 'capture_run', %s, '2026-03-31', now(), now())",
        (run_id, run_sha),
    )
    pointer_sha = _digest("pointer", universe_id)
    conn.execute(
        "insert into mart.current_pointer (pointer_id, content_sha256, environment, universe_id, "
        "universe_version, factor_id, target_run_id, sequence, previous_run_id, advanced_at) "
        "values (%s, %s, %s, %s, 'v1', %s, %s, 0, null, now())",
        (f"current-pointer:{pointer_sha}", pointer_sha, environment[0], universe_id, GPPE_FACTOR, run_id),
    )
    listings = [listing for issuer in issuers for listing in issuer.listings]
    complete = [listing for issuer in issuers if issuer.facts_complete for listing in issuer.listings]
    report_sha = _digest("report", universe_id)
    report = {
        "run_id": run_id,
        "factor_availability": {
            GPPE_FACTOR: {"complete_subjects": len(complete), "universe_subjects": len(listings)},
        },
    }
    conn.execute(
        "insert into mart.datahub_quality_report (report_id, content_sha256, run_id, requested_count, payload) "
        "values (%s, %s, %s, %s, %s)",
        (f"datahub-quality-report:{report_sha}", report_sha, run_id, CELLS_PER_LISTING * len(listings), Jsonb(report)),
    )
    if not core:
        return run_id
    # A real snapshot and a real core need a complete capture run. The replica role skips the
    # foreign keys and the validation triggers for the next rows. The check constraints apply.
    conn.execute("set local session_replication_role = replica")
    snapshot_sha = _digest("snapshot", universe_id)
    snapshot_id = f"topt-core-snapshot:{snapshot_sha}"
    members = [
        {
            "issuer_id": issuer.issuer_id,
            "instrument_id": f"instrument:{listing}",
            "listing_id": listing,
            "observation_ids": _observations(listing),
        }
        for issuer in issuers
        for listing in issuer.listings
    ]
    conn.execute(
        "insert into staging.topt_core_snapshots (snapshot_id, content_sha256, run_id, release_manifest_id, "
        "universe_id, universe_version, universe_sha256, cutoff, issuer_count, instrument_count, "
        "observation_count, payload) values (%s, %s, %s, %s, %s, 'v1', %s, '2026-03-31T20:00:00Z', %s, %s, %s, %s)",
        (
            snapshot_id,
            snapshot_sha,
            run_id,
            f"release-manifest:{SEED_SHA256}",
            universe_id,
            SEED_SHA256,
            len(issuers),
            len(listings),
            CELLS_PER_LISTING * len(listings),
            Jsonb({"run_id": run_id, "universe_id": universe_id, "members": members}),
        ),
    )
    for issuer in issuers:
        _seed_issuer(conn, snapshot_id, run_id, universe_id, issuer)
    conn.execute("set local session_replication_role = origin")
    return run_id


def _seeded_shape(conn: psycopg.Connection, run_id: str) -> tuple[int, ...]:
    """(listings, issuers, complete subjects of the report, available core results) of one run."""
    row = conn.execute(
        """
        select s.instrument_count, s.issuer_count,
               (q.payload->'factor_availability'->'gross_profit_per_employee'->>'complete_subjects')::int,
               (select count(*)::int from mart.topt_core_results r
                where r.run_id = s.run_id and r.availability = 'available')
        from staging.topt_core_snapshots s
        join mart.datahub_quality_report q on q.run_id = s.run_id
        where s.run_id = %s
        """,
        (run_id,),
    ).fetchone()
    assert row is not None
    return tuple(row)


class _Borrowed:
    """The suite opens and closes its own connection. The seeded rows are in the open
    transaction of the test, so the suite borrows that connection and leaves it open."""

    def __init__(self, connection: psycopg.Connection) -> None:
        self._connection = connection

    def __enter__(self) -> psycopg.Connection:
        return self._connection

    def __exit__(self, *_: object) -> None:
        return None


def _parity_verdict(conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    """Run the shipped invariant through `check`, with coverage required as on real data."""
    (invariant,) = [inv for inv in _module.INVARIANTS if inv.id == PARITY_INVARIANT]
    code = check(
        "postgresql://borrowed",
        require_coverage=True,
        today=TODAY,
        exemptions={},
        invariants=(invariant,),
        connect=lambda _url: _Borrowed(conn),
    )
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_a_dual_class_issuer_counts_both_listings_and_its_head_passes(
    conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1346 criterion 1. All facts are complete and all 3 issuers are available. The report
    counts 4 complete listings. The 3 available issuers have 4 listings, so the head passes."""
    issuers = [ALPHABET, _issuer("apple", "aapl"), _issuer("microsoft", "msft")]
    run_id = _seed_head(conn, "universe:canary-us-2026-06-30", issuers)
    assert _seeded_shape(conn, run_id) == (4, 3, 4, 3)
    code, output = _parity_verdict(conn, capsys)
    assert code == 0, output
    assert f"ok       {PARITY_INVARIANT}: 1 row(s) examined" in output


def test_the_production_topt_head_of_1346_passes(conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]) -> None:
    """#1346 production numbers: 21 listings of 20 issuers. BRK.B and XOM have no gross profit,
    so the report counts 19 complete listings and the core has 18 available issuers. The 18
    available issuers have 19 listings, so the head passes."""
    issuers = [
        ALPHABET,
        *_singles("valued", 17),
        *_singles("nogrossprofit", 2, facts_complete=False, available=False),
    ]
    run_id = _seed_head(conn, "universe:topt-us-2026-03-31", issuers)
    assert _seeded_shape(conn, run_id) == (21, 20, 19, 18)
    code, output = _parity_verdict(conn, capsys)
    assert code == 0, output


def test_single_listing_issuers_that_are_all_available_pass(
    conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1346 criterion 3, the control: no dual-class issuer, all facts complete, all available."""
    run_id = _seed_head(conn, "universe:topt-us-2026-03-31", _singles("valued", 3))
    assert _seeded_shape(conn, run_id) == (3, 3, 3, 3)
    code, output = _parity_verdict(conn, capsys)
    assert code == 0, output


@pytest.mark.parametrize(
    ("issuers", "shape", "row"),
    [
        pytest.param(
            [ALPHABET, _issuer("apple", "aapl"), _issuer("costco", "cost", available=False)],
            (4, 3, 4, 2),
            "4 3 2",
            id="one-complete-issuer-unavailable",
        ),
        pytest.param(
            [
                ALPHABET,
                *_singles("valued", 11),
                *_singles("noseries", 53, available=False),
                *_singles("nofacts", 36, facts_complete=False, available=False),
            ],
            (102, 101, 66, 12),
            "66 13 12",
            id="production-qqq-alphabet-available",
        ),
        pytest.param(
            [
                replace(ALPHABET, available=False),
                *_singles("valued", 12),
                *_singles("noseries", 52, available=False),
                *_singles("nofacts", 36, facts_complete=False, available=False),
            ],
            (102, 101, 66, 12),
            "66 12 12",
            id="production-qqq-alphabet-unavailable",
        ),
    ],
)
def test_a_complete_issuer_without_an_available_result_fails_and_names_its_head(
    conn: psycopg.Connection,
    capsys: pytest.CaptureFixture[str],
    issuers: list[SeededIssuer],
    shape: tuple[int, ...],
    row: str,
) -> None:
    """#1346 criterion 2, the QQQ shape. #1309 makes an issuer without a stored daily series
    unavailable in the core, and the report still counts its facts complete. The head fails.
    The row gives the complete listings of the report, then the listings and the issuers of
    the available core results."""
    universe_id = "universe:qqq-us-2026-09-30"
    run_id = _seed_head(conn, universe_id, issuers)
    assert _seeded_shape(conn, run_id) == shape
    code, output = _parity_verdict(conn, capsys)
    assert code == 1, output
    assert f"Offending rows: {universe_id} {run_id} {row}" in output


def test_a_head_without_a_core_fails(conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]) -> None:
    """A head whose run has a report and no core snapshot stays in the check, and it fails."""
    universe_id = "universe:topt-us-2026-03-31"
    run_id = _seed_head(conn, universe_id, _singles("valued", 3), core=False)
    code, output = _parity_verdict(conn, capsys)
    assert code == 1, output
    assert f"Offending rows: {universe_id} {run_id} 3 0 0" in output
