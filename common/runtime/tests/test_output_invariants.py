"""Tests for truealpha_tools.output_invariants — #581.

The SQL is verified against production separately. These tests cover what the runner
does with hard violations, non-blocking facts, and empty populations, plus
`revenue-period-is-recent` against a database that the migration chain builds (#1457).
"""

from __future__ import annotations

import calendar
import hashlib
import json
import re
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest
from psycopg.types.json import Jsonb
from truealpha_runtime.testing import REPO_ROOT, isolated_test_database
from truealpha_tools import output_invariants as _module

pytestmark = pytest.mark.deps

check = _module.check
Invariant = _module.Invariant

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


def _run(violations, population, *, invariant=ONE, signals=None, **kwargs):
    return check(
        "postgresql://unused",
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
    from factors.panel import PANEL_METRICS, PUBLISHED_COLUMNS

    for column, node_key in PUBLISHED_COLUMNS[_module.SIGN_POLICY_TABLE].items():
        assert f"'{column}' as published_column" in sign.violations
        assert str(PANEL_METRICS[node_key].node_id) in sign.violations
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
    """One head for BOTH the invariants and the backend: compares the SQL in
    `research_report_mart.py` (`POINTER_HEAD_SQL`), imported and called via `resolve_governed_head_run_id`."""
    mart_text = (REPO_ROOT / "common/runtime/src/truealpha_runtime/research_report_mart.py").read_text()
    panel_text = (REPO_ROOT / "apps/llm-service/src/llm_service/services/panel_service.py").read_text()
    entities_text = (REPO_ROOT / "apps/llm-service/src/llm_service/services/entities_service.py").read_text()
    assert "POINTER_HEAD_SQL" in panel_text, "backend no longer imports shared POINTER_HEAD_SQL"
    assert re.search(r"(?<!def )\bresolve_governed_head_run_id\s*\(", entities_text), (
        "backend no longer calls resolve_governed_head_run_id"
    )
    head = mart_text.split("POINTER_HEAD_SQL", 1)[1].split('"""', 2)[1]
    for predicate in (
        "served_head",
        "factor_id = 'gross_profit_per_employee'",
        "order by advanced_at desc",
    ):
        assert predicate in head, f"the consumer no longer scopes the head by {predicate!r}"
        assert predicate in _module.GOVERNED_HEAD, (
            f"the invariants resolve the governed head without {predicate!r}, so they check a run "
            f"the backend does not serve"
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
    """Same, for the strategy plane: the shared Python repository serves the latest run PER
    strategy_key matching data-engine's invariants."""
    repo_src = (REPO_ROOT / "common/runtime/src/truealpha_runtime/strategy_run_postgres.py").read_text()
    consumer = repo_src.split("LATEST_RUN_SQL", 1)[1].split('"""', 2)[1]
    assert "where r.strategy_key = %s" in " ".join(consumer.split())
    assert f"strategy_key = '{_module.DASHBOARD_STRATEGY}'" in " ".join(_module.LATEST_STRATEGY_RUN.split())
    served, judged = _run_ordering(consumer), _run_ordering(_module.LATEST_STRATEGY_RUN)
    assert served == judged, (
        f"the consumer orders runs by {served!r} and the invariants by {judged!r}, so the invariants "
        f"judge a run nobody serves"
    )


def test_importerror_fallbacks_match_live_registries(monkeypatch: pytest.MonkeyPatch) -> None:
    """`output_invariants` also runs where `data_engine` is not installed (a bare fixture DB job).
    Assert its `ImportError` fallback of the primary parser equals the live registry output so it never drifts.
    The parser ranks have no fallback: without the registry no parser family has a rank. The expected input keys
    have none: they come from `factors` and `truealpha_contracts`, which the tool always imports (#1518)."""
    import builtins

    live_primary = _module._primary_market_price_parser()
    assert _module._market_price_parser_ranks(), "the live registry ranks at least one market-price parser family"

    real_import = builtins.__import__

    def _block_data_engine(name: str, *args: object, **kwargs: object) -> object:
        if name.split(".")[0] in {"data_engine", "factors"}:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_data_engine)
    assert _module._primary_market_price_parser() == live_primary
    assert _module._market_price_parser_ranks() == ()
    assert _module._rank_values(()) == "(null::text, null::integer)"


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
        invariants=(invariant,),
        connect=lambda _url: _Connection(answers),
    )
    assert holding == 0

    for source in ("10k-extraction", "manual-review", "sec-10k-regex-v1", "curated-10k-2025"):
        violation = ("1", "1018724", source, "missing-accession", "none")
        answers[invariant.violations.strip()] = [violation]
        reported = check(
            "postgresql://unused",
            invariants=(invariant,),
            connect=lambda _url: _Connection(answers),
        )
        assert reported == 0


def test_hard_vs_fact_invariant_classification_and_persistence(capsys: pytest.CaptureFixture[str]) -> None:
    """Exactly 4 structural/conservation invariants are `hard` and 9 are `fact`; `--persist` writes all rows."""
    hard_ids = {inv.id for inv in _module.INVARIANTS if inv.kind == "hard"}
    fact_ids = {inv.id for inv in _module.INVARIANTS if inv.kind == "fact"}
    assert hard_ids == {
        "selected-weights-close",
        "entity-identity-unique-per-entity",
        "listing-names-one-issuer-and-one-instrument",
        "fund-virtual-company-weights-conserved",
    }
    assert "ledger-runs-in-dagster" in fact_ids
    assert len(fact_ids) == 9

    recorded: list[tuple[str, tuple]] = []

    class _PersistCursor:
        def __init__(self, answers: dict[str, list[tuple]]) -> None:
            self._answers = answers
            self._rows: list[tuple] = []

        def execute(self, sql: str, params: tuple | None = None) -> None:
            if params is not None:
                recorded.append((sql, params))
                self._rows = []
            else:
                self._rows = self._answers[sql.strip()]

        def fetchall(self) -> list[tuple]:
            return self._rows

        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None:
            return None

    class _PersistConn:
        def __init__(self, answers: dict[str, list[tuple]]) -> None:
            self._answers = answers
            self.committed = 0

        def cursor(self) -> _PersistCursor:
            return _PersistCursor(self._answers)

        def commit(self) -> None:
            self.committed += 1

        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None:
            return None

    hard_inv = Invariant(id="hard-check", kind="hard", claim="hard must hold", violations="VH", population="PH")
    fact_inv = Invariant(id="fact-check", kind="fact", claim="fact is observed", violations="VF", population="PF")
    answers = {"VH": [], "PH": [(5,)], "VF": [("row-1", "bad-val")], "PF": [(5,)]}
    conn_fact_only = _PersistConn(answers)
    checked_at = datetime(2026, 11, 25, 0, 15, tzinfo=UTC)

    code_fact = check(
        "postgresql://unused",
        require_coverage=True,
        invariants=(hard_inv, fact_inv),
        connect=lambda _url: conn_fact_only,
        persist=True,
        dagster_run_id="run-wave4",
        checked_at=checked_at,
    )
    assert code_fact == 0
    assert conn_fact_only.committed == 1
    out = capsys.readouterr().out
    assert "FACT     fact-check: 1 violation(s)" in out
    assert [(p[2], p[3], p[4], p[5]) for _, p in recorded] == [
        ("hard-check", "hard", 5, 0),
        ("fact-check", "fact", 5, 1),
    ]
    assert json.loads(recorded[1][1][7]) == [["row-1", "bad-val"]]

    answers["VH"] = [("row-h",)]
    recorded.clear()
    code_hard = check(
        "postgresql://unused",
        require_coverage=True,
        invariants=(hard_inv, fact_inv),
        connect=lambda _url: _PersistConn(answers),
        persist=True,
        dagster_run_id="run-wave4-hard",
        checked_at=checked_at,
    )
    assert code_hard == 1
    assert [(p[2], p[3], p[5]) for _, p in recorded] == [("hard-check", "hard", 1), ("fact-check", "fact", 1)]


# --- revenue-period-is-recent on a migrated database (#1457) -------------------------------
#
# The seeds use the identifiers that the writers give. The report names each subject by the
# listing id of its capture obligation (`listing:xnas:goog`). The core snapshot names each
# listing by an entity UUID. `_mint_listing` mints that UUID with the SQL and the aliases of
# `resolve_coordinates.resolve_entity`, so mart.entity_identity maps it to the listing id.
# The snapshot payload holds one member for each listing. The member table and the core hold
# one row for each issuer. A dual-class issuer (Alphabet) has two listings.

REVENUE_INVARIANT = "revenue-period-is-recent"
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
    #: The reason codes of an unavailable core result. None gives the default of its shape.
    reasons: tuple[str, ...] | None = None
    #: The period end of the revenue that the core result divided by. None stores no period.
    revenue_period_end: date | None = None


def _issuer(
    name: str,
    *tickers: str,
    facts_complete: bool = True,
    available: bool = True,
    reasons: tuple[str, ...] | None = None,
    revenue_period_end: date | None = None,
) -> SeededIssuer:
    listings = tuple(f"listing:xnas:{ticker}" for ticker in tickers)
    return SeededIssuer(f"issuer:{name}", listings, facts_complete, available, reasons, revenue_period_end)


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
    return [f"normalized-observation:{_digest(listing, str(cell))}" for cell in range(CELLS_PER_LISTING)]


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


def _mint_listing(conn: psycopg.Connection, listing: str) -> str:
    """Mint the entity of one listing as `resolve_coordinates.resolve_entity` does.

    The birth alias is the MIC-ticker pair. A `legacy-id` alias keeps the listing id."""
    mic, ticker = listing.split(":")[1:]
    value = f"{mic.upper()}:{ticker.upper()}"
    row = conn.execute(
        "select staging.entity_mint('listing', 'mic-ticker', %s, 'datahub.capture')", (value,)
    ).fetchone()
    assert row is not None
    for scheme, alias in (("mic-ticker", value), ("legacy-id", listing)):
        conn.execute(
            "insert into staging.entity_aliases (entity_id, scheme, value, valid_from, valid_to, transaction_time, "
            "source, raw_ref, method, confidence, mapping_version) values (%s, %s, %s, '-infinity'::date, null, "
            "clock_timestamp(), 'datahub.capture', %s, 'asserted', 1.0, 'datahub:v1')",
            (row[0], scheme, alias, f"mint:{row[0]}"),
        )
    return str(row[0])


def _seed_issuer(
    conn: psycopg.Connection,
    snapshot_id: str,
    run_id: str,
    universe_id: str,
    issuer: SeededIssuer,
    entity_ids: dict[str, str],
) -> None:
    """Write the member row and the core result of one issuer. Both use its first listing."""
    execution = entity_ids[issuer.listings[0]]
    instrument_id = f"instrument:{issuer.listings[0]}"
    factor_input = {
        "snapshot_id": snapshot_id,
        "issuer_id": issuer.issuer_id,
        "instrument_id": instrument_id,
        "listing_id": execution,
    }
    conn.execute(
        "insert into staging.core_snapshot_members (snapshot_id, instrument_id, issuer_id, listing_id, "
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
        value, reasons = 1, []
    else:
        # #1309: an issuer with complete facts and no stored daily series is unavailable.
        value = None
        default = ("no_split_adjusted_series",) if issuer.facts_complete else ("missing_gross_profit",)
        reasons = list(issuer.reasons or default)
    avail_status = "available" if issuer.available else "unavailable"
    observations = sorted(item for listing in issuer.listings for item in _observations(listing))
    if issuer.revenue_period_end is not None and observations:
        obs_id = observations[0]
        obs_sha = obs_id.split(":", 1)[1]
        payload = {"revenue_period_end": issuer.revenue_period_end.isoformat()}
        conn.execute(
            """
            insert into staging.capture_normalized_observations (
                observation_id, content_sha256, capture_obligation_id, source_vintage_id,
                semantic_type, semantic_version, subject_kind, subject_id,
                valid_from, knowable_at, parser_version, mapping_version,
                normalized_payload_sha256, is_restatement, confidence, freshness_state,
                payload, recorded_at
            ) values (
                %s, %s, %s, %s,
                'financial-fact', 'v1', 'listing', %s,
                '2026-01-01T00:00:00Z', '2026-03-31T20:00:00Z', 'v1', 'v1',
                %s, false, 0.9, 'fresh',
                '{}'::jsonb, '2026-03-31T20:00:00Z'
            ) on conflict (observation_id) do nothing
            """,
            (obs_id, obs_sha, f"ob:{obs_sha}", f"vintage:{obs_sha}", execution, obs_sha),
        )
        conn.execute(
            """
            insert into staging.capture_observation_payloads (
                observation_id, normalized_payload_sha256, normalized_payload
            ) values (%s, %s, %s)
            on conflict (observation_id) do nothing
            """,
            (obs_id, obs_sha, Jsonb(payload)),
        )
    conn.execute(
        """
        insert into mart.metric_node_values (
            run_id, issuer_id, node_id, node_key, period, period_tag,
            value, unit, availability_status, reason_codes,
            source_evidence_status, factor_validation_status,
            confidence, tree_id, tree_version, tree_sha256, input_observation_ids
        ) values (
            %(run)s, %(issuer)s, '6143cf17-ceba-5f78-a2d3-a7cc423c49a7'::uuid,
            'gppe_uniform_charge_v0', %(period)s, 'FY',
            %(value)s, 'per_employee', %(availability_status)s, %(reasons)s,
            'verified', 'accepted', 0.9,
            'ef13b940-b695-5f5f-846a-a36face5cc3e'::uuid, 'production-topt-v0.2.0',
            %(seed)s, %(observations)s
        )
        """,
        {
            "run": run_id,
            "issuer": issuer.issuer_id,
            "period": issuer.revenue_period_end.isoformat() if issuer.revenue_period_end else "2025-12-31",
            "value": value,
            "availability_status": avail_status,
            "reasons": reasons,
            "seed": SEED_SHA256,
            "observations": observations,
        },
    )


def _seed_head(
    conn: psycopg.Connection,
    universe_id: str,
    issuers: Sequence[SeededIssuer],
    *,
    core: bool = True,
    report_lists_subjects: bool = True,
    unminted: frozenset[str] = frozenset(),
) -> str:
    """Serve one governed head and write its quality report. With `core`, also write its core.

    The report counts and names one complete subject for each listing whose facts are
    complete. Without `report_lists_subjects` it is a report persisted before #1346: a count
    and no names. A listing in `unminted` gets a UUID that the identity store does not hold.
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
    complete = sorted(listing for issuer in issuers if issuer.facts_complete for listing in issuer.listings)
    grade: dict[str, object] = {"complete_subjects": len(complete), "universe_subjects": len(listings)}
    if report_lists_subjects:
        grade["complete_subject_ids"] = complete
    report_sha = _digest("report", universe_id)
    report = {"run_id": run_id, "factor_availability": {GPPE_FACTOR: grade}}
    conn.execute(
        "insert into mart.datahub_quality_report (report_id, content_sha256, run_id, requested_count, payload) "
        "values (%s, %s, %s, %s, %s)",
        (f"datahub-quality-report:{report_sha}", report_sha, run_id, CELLS_PER_LISTING * len(listings), Jsonb(report)),
    )
    if not core:
        return run_id
    entity_ids = {
        listing: str(uuid.uuid5(uuid.NAMESPACE_URL, listing)) if listing in unminted else _mint_listing(conn, listing)
        for listing in listings
    }
    # A real snapshot and a real core need a complete capture run. The replica role skips the
    # foreign keys and the validation triggers for the next rows. The check constraints apply.
    conn.execute("set local session_replication_role = replica")
    snapshot_sha = _digest("snapshot", universe_id)
    snapshot_id = f"topt-core-snapshot:{snapshot_sha}"
    members = [
        {
            "issuer_id": issuer.issuer_id,
            "instrument_id": f"instrument:{listing}",
            "listing_id": entity_ids[listing],
            "observation_ids": _observations(listing),
        }
        for issuer in issuers
        for listing in issuer.listings
    ]
    conn.execute(
        "insert into staging.core_snapshots (snapshot_id, content_sha256, run_id, release_manifest_id, "
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
        _seed_issuer(conn, snapshot_id, run_id, universe_id, issuer, entity_ids)
    conn.execute("set local session_replication_role = origin")
    return run_id


def _seeded_shape(conn: psycopg.Connection, run_id: str) -> tuple[int, ...]:
    """(listings, issuers, complete subjects of the report, available core results) of one run."""
    row = conn.execute(
        """
        select s.instrument_count, s.issuer_count,
               (q.payload->'factor_availability'->'gross_profit_per_employee'->>'complete_subjects')::int,
               (select count(*)::int from mart.metric_node_values r
                where r.run_id = s.run_id and r.node_key = 'gppe_uniform_charge_v0'
                  and r.availability_status = 'available')
        from staging.core_snapshots s
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


def _parity_verdict(
    conn: psycopg.Connection, capsys: pytest.CaptureFixture[str], invariant_id: str = REVENUE_INVARIANT
) -> tuple[int, str]:
    """Run a shipped invariant through `check`, with coverage required as on real data."""
    (invariant,) = [inv for inv in _module.INVARIANTS if inv.id == invariant_id]
    code = check(
        "postgresql://borrowed",
        require_coverage=True,
        invariants=(invariant,),
        connect=lambda _url: _Borrowed(conn),
    )
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def _offending(universe_id: str, run_id: str, finding: str, listing: str, reasons: str) -> str:
    """One offending row as `check` prints it."""
    return f"{universe_id} {run_id} {finding} {listing} {reasons}"


def test_the_seeded_members_resolve_through_the_identity_store(conn: psycopg.Connection) -> None:
    """The control for the seeds: each member listing is a UUID, and mart.entity_identity
    maps it to the listing id of the report. Without this, the tests below could pass on
    seeds that never used the identity store."""
    run_id = _seed_head(conn, "universe:canary-us-2026-06-30", [ALPHABET, _issuer("apple", "aapl")])
    from truealpha_runtime.mart_refresh import MART_ENTITY_IDENTITY, refresh_mart_materialized_views

    refresh_mart_materialized_views(conn, (MART_ENTITY_IDENTITY,))
    rows = conn.execute(
        """
        select member->>'listing_id', identity.listing_id
        from staging.core_snapshots s
        cross join lateral jsonb_array_elements(s.payload->'members') member
        left join mart.entity_identity identity on identity.entity_id::text = member->>'listing_id'
        where s.run_id = %s
        order by 2
        """,
        (run_id,),
    ).fetchall()
    assert [listing for _uuid, listing in rows] == ["listing:xnas:aapl", "listing:xnas:goog", "listing:xnas:googl"]
    assert all(str(uuid.UUID(member)) == member for member, _listing in rows)


@pytest.mark.parametrize("environment", ["staging", "production", "prod", "preview", "dev", ""])
@pytest.mark.parametrize("family", ["topt", "qqq", "sp500", "canary", "unknown"])
def test_the_rendered_schedule_predicate_agrees_with_the_environment_contract(
    conn: psycopg.Connection, family: str, environment: str
) -> None:
    """The invariants judge a head only where its environment ticks the universe (#1346). The SQL predicate is
    rendered from the contract at import (#1522), the way `_render` writes the expected input keys. This runs
    it in the database for each family and environment and compares it with the contract rule."""
    from truealpha_contracts.environment_contract import load_environment_contract

    contract = load_environment_contract()
    universe_id = f"universe:{family}-us-2026-09-30"
    predicate = _module.head_is_scheduled_sql(contract).replace("%", "%%")  # the query passes parameters
    row = conn.execute(
        f"select {predicate} from (select %s::text as universe_id, %s::text as environment) h",
        (universe_id, environment),
    ).fetchone()

    assert row is not None
    assert row[0] is contract.is_head_scheduled(universe_id, environment), (family, environment)


def _scheduled_sp500_contract():
    """The packaged contract with the S&P 500 scheduled in production: a variant record (#1522)."""
    import json

    from truealpha_contracts.environment_contract import EnvironmentContract, load_environment_contract

    raw = json.loads(load_environment_contract().model_dump_json())
    production = raw["environments"]["production"]
    production["universes"] = [
        {"key": entry["key"], "status": "scheduled"} if entry["key"] == "sp500" else entry
        for entry in production["universes"]
    ]
    return EnvironmentContract.model_validate_json(json.dumps(raw))


def test_a_variant_record_that_schedules_sp500_makes_the_invariants_count_its_head(
    conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    """The S&P 500 is stopped in the packaged contract, so no invariant examines its head. A variant that
    schedules it must change what the invariants examine, with no edit to the SQL (#1522)."""
    conn.execute("update mart.environment_identity set environment = 'production'")
    issuers = [_issuer(f"valued{i}", f"val{i}", revenue_period_end=date(2025, 12, 31)) for i in range(3)]
    _seed_head(conn, "universe:sp500-us-2026-09-30", issuers)

    def verdict(contract) -> tuple[int, str]:
        (invariant,) = [inv for inv in _module.render_invariants(contract) if inv.id == REVENUE_INVARIANT]
        code = check(
            "postgresql://borrowed",
            require_coverage=True,
            invariants=(invariant,),
            connect=lambda _url: _Borrowed(conn),
        )
        captured = capsys.readouterr()
        return code, captured.out + captured.err

    from truealpha_contracts.environment_contract import load_environment_contract

    stopped_code, stopped_output = verdict(load_environment_contract())
    assert stopped_code == 0 and f"FACT     {REVENUE_INVARIANT}: examined 0 rows" in stopped_output, stopped_output

    code, output = verdict(_scheduled_sp500_contract())
    assert code == 0, output
    assert f"ok       {REVENUE_INVARIANT}: 3 row(s) examined" in output


def test_no_invariant_needs_the_sql_schedule_function(
    conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    """The chain no longer holds `mart.is_head_scheduled` (#1522 release N+1), and a process of release N already
    ran without it. Every shipped invariant runs on a database without the function."""
    assert conn.execute("select to_regprocedure('mart.is_head_scheduled(text,text)') is null").fetchone() == (True,)
    code = check(
        "postgresql://borrowed",
        invariants=_module.INVARIANTS,
        connect=lambda _url: _Borrowed(conn),
    )
    output = capsys.readouterr()
    assert code == 0, output.out + output.err


# --- revenue-period-is-recent on a migrated database (#1457) -------------------------------
#
# Production QQQ served annual revenue of 2018 for CPRT and XEL at a cutoff of 2026-10-19. The
# plausibility gate judged the value, not its age. The invariant reads `revenue_period_end`
# of the available core results of every scheduled head.
#: The cutoff that `_seed_issuer` gives every core result.
SEEDED_CUTOFF = date(2026, 3, 31)
REVENUE_UNIVERSE = "universe:topt-us-2026-03-31"


def _months_before(day: date, months: int) -> date:
    """The day `months` calendar months before `day`. A shorter month clamps to its last day."""
    year, month_index = divmod(day.year * 12 + day.month - 1 - months, 12)
    return date(year, month_index + 1, min(day.day, calendar.monthrange(year, month_index + 1)[1]))


def test_a_head_whose_revenue_periods_are_recent_passes(
    conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    issuers = [
        _issuer("current", "cur0", revenue_period_end=date(2025, 12, 31)),
        _issuer("lagging", "lag0", revenue_period_end=date(2024, 12, 31)),
    ]
    _seed_head(conn, REVENUE_UNIVERSE, issuers)

    code, output = _parity_verdict(conn, capsys, REVENUE_INVARIANT)

    assert code == 0, output
    assert f"ok       {REVENUE_INVARIANT}: 2 row(s) examined" in output


def test_a_revenue_period_from_2018_fails_and_names_the_issuer(
    conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    """The injected row is the XEL of production: an available result on a revenue of 2018-12-31.

    Logged as a non-blocking `fact` invariant with sample rows (#1457, Wave 4).
    """
    issuers = [
        _issuer("current", "cur0", revenue_period_end=date(2025, 12, 31)),
        _issuer("xel", "xel0", revenue_period_end=date(2018, 12, 31)),
    ]
    run_id = _seed_head(conn, REVENUE_UNIVERSE, issuers)

    code, output = _parity_verdict(conn, capsys, REVENUE_INVARIANT)

    assert code == 0, output
    assert f"FACT     {REVENUE_INVARIANT}: 1 violation(s)" in output
    assert f"Offending rows: {REVENUE_UNIVERSE} {run_id} issuer:xel 2018-12-31 2026-03-31" in output


def test_the_limit_is_the_maximum_input_age_days_of_the_live_definition(
    conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    """A period that ends exactly on the limit passes cleanly; one day before emits a FACT line."""
    assert _module._MAX_INPUT_AGE_DAYS == 455
    limit = SEEDED_CUTOFF - timedelta(days=_module._MAX_INPUT_AGE_DAYS)
    assert limit == date(2024, 12, 31)
    _seed_head(conn, REVENUE_UNIVERSE, [_issuer("edge", "edge0", revenue_period_end=limit)])
    edge_code, edge_out = _parity_verdict(conn, capsys, REVENUE_INVARIANT)
    assert edge_code == 0 and f"ok       {REVENUE_INVARIANT}:" in edge_out

    conn.rollback()
    _seed_head(conn, REVENUE_UNIVERSE, [_issuer("late", "late0", revenue_period_end=limit - timedelta(days=1))])
    code, output = _parity_verdict(conn, capsys, REVENUE_INVARIANT)
    assert code == 0, output
    assert f"FACT     {REVENUE_INVARIANT}: 1 violation(s)" in output
    assert f"issuer:late {limit - timedelta(days=1)} 2026-03-31" in output


def test_an_unavailable_result_with_a_named_reason_may_carry_an_old_period(
    conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only an available result is served. An unavailable result names its reason in `reason_codes`."""
    issuers = [
        _issuer("current", "cur0", revenue_period_end=date(2025, 12, 31)),
        _issuer("refused", "ref0", available=False, reasons=("stale_input",), revenue_period_end=date(2018, 12, 31)),
    ]
    _seed_head(conn, REVENUE_UNIVERSE, issuers)

    code, output = _parity_verdict(conn, capsys, REVENUE_INVARIANT)

    assert code == 0, output
    assert f"ok       {REVENUE_INVARIANT}: 1 row(s) examined" in output


def test_a_head_that_states_no_revenue_period_fails_by_name(
    conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    """A result with no stored period has an unknown age. Emits FACT naming no-result-states-a-revenue-period."""
    run_id = _seed_head(conn, REVENUE_UNIVERSE, [_issuer("undated", "und0")])

    code, output = _parity_verdict(conn, capsys, REVENUE_INVARIANT)

    assert code == 0, output
    assert f"FACT     {REVENUE_INVARIANT}: 1 violation(s)" in output
    assert f"Offending rows: {REVENUE_UNIVERSE} {run_id} no-result-states-a-revenue-period - -" in output


def test_a_head_with_dated_results_cannot_hide_a_head_with_none(
    conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two scheduled heads. The first states fresh periods. The second states none -> still flagged in FACT."""
    _seed_head(conn, REVENUE_UNIVERSE, [_issuer("dated", "dat0", revenue_period_end=date(2025, 12, 31))])
    undated_run = _seed_head(conn, "universe:canary-us-2026-06-30", [_issuer("undated", "und0")])

    code, output = _parity_verdict(conn, capsys, REVENUE_INVARIANT)

    assert code == 0, output
    assert f"FACT     {REVENUE_INVARIANT}: 1 violation(s)" in output
    assert f"universe:canary-us-2026-06-30 {undated_run} no-result-states-a-revenue-period - -" in output


def test_a_head_with_some_dated_and_some_undated_results_is_not_judged_on_the_undated_rows(
    conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    """One head, two available results. One states a fresh period and one states none.

    The invariant judges a head with no period at all. It does not judge one undated row of a head that
    states other periods. A rule that flagged a head for ANY undated row would turn this test red.
    """
    _seed_head(
        conn,
        REVENUE_UNIVERSE,
        [_issuer("dated", "dat0", revenue_period_end=date(2025, 12, 31)), _issuer("undated", "und0")],
    )

    code, output = _parity_verdict(conn, capsys, REVENUE_INVARIANT)

    assert code == 0, output
    assert f"ok       {REVENUE_INVARIANT}: 1 row(s) examined" in output


def test_a_head_whose_results_are_all_unavailable_is_not_judged_by_this_invariant(
    conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing is served from this head, so no period is served. Another head carries the coverage."""
    _seed_head(conn, REVENUE_UNIVERSE, [_issuer("dated", "dat0", revenue_period_end=date(2025, 12, 31))])
    _seed_head(
        conn,
        "universe:canary-us-2026-06-30",
        [_issuer("refused", "ref0", available=False, reasons=("stale_input",))],
    )

    code, output = _parity_verdict(conn, capsys, REVENUE_INVARIANT)

    assert code == 0, output
    assert f"ok       {REVENUE_INVARIANT}: 1 row(s) examined" in output


def test_the_expected_inputs_follow_the_class_of_the_issuer() -> None:
    """#1518: a key is expected for the classes whose pinned tree consumes it. A bank needs its four banking leaves
    and no total assets. Every other class needs total assets and no banking leaf."""
    pairs = set(_module.EXPECTED_INPUTS)
    leaves = {"stockholders_equity", "preferred_stock_value", "goodwill", "intangible_assets_net_excluding_goodwill"}
    for leaf in leaves:
        assert (leaf, "financial") in pairs
        assert (leaf, "non_financial") not in pairs and (leaf, "insurance") not in pairs
    assert ("total_assets", "financial") not in pairs
    assert ("total_assets", "non_financial") in pairs and ("total_assets", "insurance") in pairs
    assert ("net_income", "financial") in pairs and ("net_income", "non_financial") in pairs, "PEG reads every class"


DISPLAY_NAME_INVARIANT = "governed-entities-resolve-display-name"


def test_governed_entities_resolve_display_name_passes_with_real_names_and_fails_on_null_or_ticker(
    conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1517: every member in an active governed head must resolve to a non-empty company name
    in `mart.entity_display_resolution` that is neither its raw `issuer_id` nor its bare `ticker`."""
    minted: list[tuple[str, str, str, str]] = []
    for cik, ticker, name in [
        ("0000019617", "jpm0", "JPMorgan Chase & Co."),
        ("0000059478", "lly0", "Eli Lilly and Company"),
    ]:
        eid = conn.execute("select staging.entity_mint('issuer', 'cik', %s, 'test')", (cik,)).fetchone()[0]
        conn.execute(
            "insert into staging.entity_aliases (entity_id, scheme, value, valid_from, transaction_time, source, raw_ref, method, confidence, mapping_version) "
            "values (%s, 'cik', %s, '-infinity', '2026-01-01T00:00:00Z', 'test', 'test', 'asserted', 1.0, 'v1')",
            (eid, cik),
        )
        minted.append((str(eid), ticker, name, cik))

    issuers = [
        SeededIssuer(eid, (f"listing:xnas:{ticker}",), True, True, None, date(2025, 12, 31))
        for eid, ticker, _, _ in minted
    ]
    run_id = _seed_head(conn, REVENUE_UNIVERSE, issuers)

    # Without name aliases, mart.entity_display_resolution resolves display_name as NULL -> FACT output
    code, output = _parity_verdict(conn, capsys, DISPLAY_NAME_INVARIANT)
    assert code == 0, output
    assert f"FACT     {DISPLAY_NAME_INVARIANT}: 2 violation(s)" in output

    # Insert real company names into staging.entity_aliases for both issuers -> clean ok
    for eid, _, name, _ in minted:
        conn.execute(
            "insert into staging.entity_aliases (entity_id, scheme, value, valid_from, transaction_time, source, raw_ref, method, confidence, mapping_version) "
            "values (%s::uuid, 'name', %s, '-infinity', '2026-01-01T00:00:00Z', 'test', 'test', 'asserted', 1.0, 'v1')",
            (eid, name),
        )
    code_ok, output_ok = _parity_verdict(conn, capsys, DISPLAY_NAME_INVARIANT)
    assert code_ok == 0, output_ok
    assert f"ok       {DISPLAY_NAME_INVARIANT}: 2 row(s) examined" in output_ok
    assert run_id is not None


REASON_INVARIANT = "unavailable-means-a-reason"


def test_an_unavailable_node_value_without_a_reason_fails_and_names_the_node(
    conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1804: an unavailable node value with a reason passes. One with empty `reason_codes` logs FACT.
    A `low_confidence` value carries a value and needs no reason."""
    issuer = SeededIssuer(
        "issuer:reasoned", ("listing:xnas:rsn0",), True, False, ("missing_gross_profit",), date(2025, 12, 31)
    )
    run_id = _seed_head(conn, REVENUE_UNIVERSE, [issuer])
    code, output = _parity_verdict(conn, capsys, REASON_INVARIANT)
    assert code == 0, output
    assert f"ok       {REASON_INVARIANT}: 1 row(s) examined" in output

    insert = (
        "insert into mart.metric_node_values (run_id, issuer_id, node_id, node_key, period, period_tag, value, unit,"
        " availability_status, reason_codes, source_evidence_status, factor_validation_status, confidence, tree_id,"
        " tree_version, tree_sha256) values (%s, 'issuer:reasoned', %s::uuid, %s, '2026-03-31', 'cutoff', %s, 'ratio',"
        " %s, '{}', 'degraded', 'not_evaluated', %s, 'ef13b940-b695-5f5f-846a-a36face5cc3e'::uuid, 'v1', %s)"
    )
    low_node, peg_node = "7f4b0c1e-3d0a-5c57-9d59-1f0d6f3c1803", "7f4b0c1e-3d0a-5c57-9d59-1f0d6f3c1804"
    conn.execute(insert, (run_id, low_node, "peg_low", Decimal("1.5"), "low_confidence", Decimal("0.4"), SEED_SHA256))
    code, output = _parity_verdict(conn, capsys, REASON_INVARIANT)
    assert code == 0, output
    assert f"ok       {REASON_INVARIANT}: 1 row(s) examined" in output

    conn.execute(insert, (run_id, peg_node, "peg_consensus", None, "unavailable", None, SEED_SHA256))
    code, output = _parity_verdict(conn, capsys, REASON_INVARIANT)
    assert code == 0, output
    assert f"FACT     {REASON_INVARIANT}: 1 violation(s)" in output
    assert "peg_consensus" in output


LISTING_INVARIANT = "listing-names-one-issuer-and-one-instrument"


def _relate(conn: psycopg.Connection, relation_type: str, source: uuid.UUID, target: uuid.UUID) -> None:
    conn.execute(
        "insert into staging.entity_relations (relation_id, relation_type, from_entity_id, to_entity_id, valid_from,"
        " transaction_time, source, raw_ref, method, confidence, mapping_version)"
        " select staging.entity_relation_uuid(%(type)s, %(from)s, %(to)s, '-infinity'::date,"
        " '2026-01-01T00:00:00Z'::timestamptz, 'test', 'asserted'), %(type)s, %(from)s, %(to)s, '-infinity'::date,"
        " '2026-01-01T00:00:00Z'::timestamptz, 'test', 'test', 'asserted', 1.0, 'v1'",
        {"type": relation_type, "from": source, "to": target},
    )


def test_a_listing_with_two_issuers_fails_until_the_issuers_are_merged(
    conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1798: one listing, one instrument, two issuers fails by the listing id. A `superseded_by`
    merge of the two issuers makes it pass."""

    def mint(kind: str, scheme: str, value: str) -> uuid.UUID:
        return conn.execute("select staging.entity_mint(%s, %s, %s, 'test')", (kind, scheme, value)).fetchone()[0]

    listing = uuid.UUID(_mint_listing(conn, "listing:xnys:xom0"))
    instrument = mint("instrument", "cusip", "30231G102")
    first, second = mint("issuer", "lei", "J3WHBG0MTS7O8ZVMDC91"), mint("issuer", "cik", "0002115436")
    _relate(conn, "listed_as", instrument, listing)
    _relate(conn, "issues", first, instrument)
    code, output = _parity_verdict(conn, capsys, LISTING_INVARIANT)
    assert code == 0, output

    _relate(conn, "issues", second, instrument)
    code, output = _parity_verdict(conn, capsys, LISTING_INVARIANT)
    assert code == 1, output
    assert f"{LISTING_INVARIANT}: 1 violation(s)" in output
    assert str(listing) in output

    for relation_type in ("same_as", "superseded_by"):
        _relate(conn, relation_type, second, first)
    code, output = _parity_verdict(conn, capsys, LISTING_INVARIANT)
    assert code == 0, output


ENTITY_IDENTITY_UNIQUE_INVARIANT = "entity-identity-unique-per-entity"
FUND_WEIGHTS_INVARIANT = "fund-virtual-company-weights-conserved"


def test_entity_identity_unique_and_fund_weights_conserved_structure_and_behavior() -> None:
    """#1641: verify structure and pass/fail behavior for entity_identity uniqueness and fund weight conservation."""
    (unique_inv,) = [inv for inv in _module.INVARIANTS if inv.id == ENTITY_IDENTITY_UNIQUE_INVARIANT]
    assert "from mart.entity_identity" in unique_inv.violations
    assert "group by entity_id" in unique_inv.violations
    assert "having count(*) > 1" in unique_inv.violations
    assert "select count(*) from mart.entity_identity" in unique_inv.population

    unique_answers = {
        unique_inv.violations.strip(): [],
        unique_inv.population.strip(): [(338,)],
    }
    assert (
        check(
            "postgresql://unused",
            invariants=(unique_inv,),
            connect=lambda _url: _Connection(unique_answers),
        )
        == 0
    )
    unique_answers[unique_inv.violations.strip()] = [("00000000-0000-0000-0000-000000001641", "2")]
    assert (
        check(
            "postgresql://unused",
            invariants=(unique_inv,),
            connect=lambda _url: _Connection(unique_answers),
        )
        == 1
    )

    (weights_inv,) = [inv for inv in _module.INVARIANTS if inv.id == FUND_WEIGHTS_INVARIANT]
    assert "from mart.fund_virtual_company" in weights_inv.violations
    assert "total_weight_pct > 105" in weights_inv.violations
    assert "resolved_weight_pct > total_weight_pct" in weights_inv.violations
    assert "valued_weight_pct > resolved_weight_pct" in weights_inv.violations
    assert "select count(*) from mart.fund_virtual_company" in weights_inv.population

    weights_answers = {
        weights_inv.violations.strip(): [],
        weights_inv.population.strip(): [(4,)],
    }
    assert (
        check(
            "postgresql://unused",
            invariants=(weights_inv,),
            connect=lambda _url: _Connection(weights_answers),
        )
        == 0
    )
    weights_answers[weights_inv.violations.strip()] = [
        ("capture-run:1641", "etf:series:QQQ", "198.2", "198.2", "190.0")
    ]
    assert (
        check(
            "postgresql://unused",
            invariants=(weights_inv,),
            connect=lambda _url: _Connection(weights_answers),
        )
        == 1
    )


def test_entity_identity_unique_per_entity_passes_with_dual_kg_rows_and_fails_on_duplicate(
    conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1641: mart.entity_identity emits one row per entity even when staging.entity_aliases holds multiple names."""
    entity_uuid = _mint_listing(conn, "listing:xnas:goog")
    for idx, name in enumerate(("Alphabet UUID", "Alphabet Legacy")):
        conn.execute(
            "insert into staging.entity_aliases (entity_id, scheme, value, valid_from, transaction_time, source, raw_ref, method, confidence, mapping_version) "
            "values (%s::uuid, 'name', %s, '-infinity', '2026-01-01T00:00:00Z'::timestamptz + (%s || ' minutes')::interval, 'test', 'test', 'asserted', 0.9, 'v1')",
            (entity_uuid, name, str(idx)),
        )
    from truealpha_runtime.mart_refresh import MART_ENTITY_IDENTITY, refresh_mart_materialized_views

    refresh_mart_materialized_views(conn, (MART_ENTITY_IDENTITY,))
    code, output = _parity_verdict(conn, capsys, ENTITY_IDENTITY_UNIQUE_INVARIANT)
    assert code == 0, output
    assert f"ok       {ENTITY_IDENTITY_UNIQUE_INVARIANT}: 1 row(s) examined" in output

    (invariant,) = [inv for inv in _module.INVARIANTS if inv.id == ENTITY_IDENTITY_UNIQUE_INVARIANT]
    answers = {
        invariant.violations.strip(): [(entity_uuid, "2")],
        invariant.population.strip(): [(2,)],
    }
    failing = check(
        "postgresql://unused",
        invariants=(invariant,),
        connect=lambda _url: _Connection(answers),
    )
    assert failing == 1


def test_fund_virtual_company_weights_conserved_passes_valid_row_and_fails_on_fanout(
    conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1641: fund-virtual-company-weights-conserved passes conserved rows and rejects fanout weights."""
    conn.execute(
        """
        insert into mart.fund_virtual_company (
            run_id, fund_id, report_period, transaction_time, cutoff,
            definition_version, definition_sha256, weighted_valuation_gap, valuation_basis,
            total_weight_pct, resolved_weight_pct, valued_weight_pct, lines, valued_lines, confidence,
            availability_status, source_evidence_status, factor_validation_status
        ) values (
            'capture-run:1641', 'etf:series:QQQ', '2026-06-30', '2026-06-30T00:00:00Z',
            '2026-06-30T00:00:00Z', 'v0', repeat('a', 64), 0.12, 'price_to_gross_profit',
            99.8, 98.5, 95.0, 100, 95, 0.90,
            'available', 'verified', 'accepted'
        )
        """
    )
    code, output = _parity_verdict(conn, capsys, FUND_WEIGHTS_INVARIANT)
    assert code == 0, output
    assert f"ok       {FUND_WEIGHTS_INVARIANT}: 1 row(s) examined" in output

    (invariant,) = [inv for inv in _module.INVARIANTS if inv.id == FUND_WEIGHTS_INVARIANT]
    answers = {
        invariant.violations.strip(): [("capture-run:1641", "etf:series:QQQ", "198.2", "198.2", "190.0")],
        invariant.population.strip(): [(1,)],
    }
    failing = check(
        "postgresql://unused",
        invariants=(invariant,),
        connect=lambda _url: _Connection(answers),
    )
    assert failing == 1


def test_persist_records_hard_and_fact_invariants_and_only_hard_fails(
    conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    """Persist both hard and fact results into mart.output_invariant_results inside a transaction."""
    from truealpha_runtime.mart_refresh import MART_ENTITY_IDENTITY, refresh_mart_materialized_views

    run_id = _seed_head(
        conn,
        REVENUE_UNIVERSE,
        [
            SeededIssuer(
                "issuer:persist", ("listing:xnas:prs0",), True, False, ("missing_gross_profit",), date(2025, 12, 31)
            )
        ],
    )
    refresh_mart_materialized_views(conn, (MART_ENTITY_IDENTITY,))
    conn.execute(
        "insert into mart.metric_node_values (run_id, issuer_id, node_id, node_key, period, period_tag, value, unit,"
        " availability_status, reason_codes, source_evidence_status, factor_validation_status, confidence, tree_id,"
        " tree_version, tree_sha256) values (%s, 'issuer:persist', '7f4b0c1e-3d0a-5c57-9d59-1f0d6f3c1804'::uuid,"
        " 'peg_consensus', '2026-03-31', 'cutoff', null, 'ratio', 'unavailable', '{}', 'degraded', 'not_evaluated',"
        " null, 'ef13b940-b695-5f5f-846a-a36face5cc3e'::uuid, 'v1', %s)",
        (run_id, SEED_SHA256),
    )

    (fact_inv,) = [inv for inv in _module.INVARIANTS if inv.id == REASON_INVARIANT]
    (hard_inv,) = [inv for inv in _module.INVARIANTS if inv.id == ENTITY_IDENTITY_UNIQUE_INVARIANT]

    code_fact = check(
        "postgresql://borrowed",
        require_coverage=True,
        invariants=(fact_inv, hard_inv),
        connect=lambda _url: _Borrowed(conn),
        persist=True,
        dagster_run_id="persist-wave4-fact-only",
        commit=False,
    )
    _ = capsys.readouterr()
    assert code_fact == 0

    rows_fact = conn.execute(
        """
        select invariant_id, kind, population, violation_count, deferred, sample_rows
        from mart.output_invariant_results
        where dagster_run_id = 'persist-wave4-fact-only'
        order by invariant_id
        """
    ).fetchall()
    by_fact = {row[0]: row[1:] for row in rows_fact}
    assert set(by_fact) == {REASON_INVARIANT, ENTITY_IDENTITY_UNIQUE_INVARIANT}
    assert by_fact[REASON_INVARIANT][0] == "fact" and by_fact[REASON_INVARIANT][2] == 1
    assert len(by_fact[REASON_INVARIANT][4]) == 1
    assert by_fact[ENTITY_IDENTITY_UNIQUE_INVARIANT][0] == "hard" and by_fact[ENTITY_IDENTITY_UNIQUE_INVARIANT][2] == 0

    def mint(kind: str, scheme: str, value: str) -> uuid.UUID:
        return conn.execute("select staging.entity_mint(%s, %s, %s, 'test')", (kind, scheme, value)).fetchone()[0]

    listing = uuid.UUID(_mint_listing(conn, "listing:xnys:prs1"))
    instrument = mint("instrument", "cusip", "30231G199")
    first, second = mint("issuer", "cik", "0002115498"), mint("issuer", "cik", "0002115499")
    _relate(conn, "listed_as", instrument, listing)
    _relate(conn, "issues", first, instrument)
    _relate(conn, "issues", second, instrument)
    (hard_fail_inv,) = [inv for inv in _module.INVARIANTS if inv.id == LISTING_INVARIANT]

    code_hard = check(
        "postgresql://borrowed",
        require_coverage=True,
        invariants=(fact_inv, hard_fail_inv),
        connect=lambda _url: _Borrowed(conn),
        persist=True,
        dagster_run_id="persist-wave4-hard",
        commit=False,
    )
    _ = capsys.readouterr()
    assert code_hard == 1

    rows_hard = conn.execute(
        """
        select invariant_id, kind, population, violation_count, deferred, sample_rows
        from mart.output_invariant_results
        where dagster_run_id = 'persist-wave4-hard'
        order by invariant_id
        """
    ).fetchall()
    by_hard = {row[0]: row[1:] for row in rows_hard}
    assert set(by_hard) == {REASON_INVARIANT, LISTING_INVARIANT}
    assert by_hard[REASON_INVARIANT][0] == "fact" and by_hard[REASON_INVARIANT][2] == 1
    assert by_hard[LISTING_INVARIANT][0] == "hard" and by_hard[LISTING_INVARIANT][2] == 1
    assert len(by_hard[LISTING_INVARIANT][4]) == 1


def test_ledger_runs_in_dagster_invariant_against_real_db(
    conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1649 H2: flag Dagster-scoped ledger run keys missing from dagster.runs; skip safely without dagster schema."""
    (inv,) = [inv for inv in _module.INVARIANTS if inv.id == "ledger-runs-in-dagster"]
    assert inv.kind == "fact" and inv.requires_table == "dagster.runs"

    # Without dagster.runs (bare CI migrated schema), invariant skips cleanly without UndefinedTable.
    code_no_dagster = check(
        "postgresql://borrowed",
        invariants=(inv,),
        connect=lambda _url: _Borrowed(conn),
        commit=False,
    )
    out_no_dagster = capsys.readouterr().out
    assert code_no_dagster == 0
    assert "examined 0 rows" in out_no_dagster

    conn.execute("create schema if not exists dagster")
    conn.execute("create table if not exists dagster.runs (run_id text primary key)")
    conn.execute("insert into dagster.runs (run_id) values ('run-known-1') on conflict do nothing")
    conn.execute(
        """
        insert into staging.api_call_ledger (source, endpoint, caller, called_at, ok, run_key)
        values
            ('twelvedata', '/time_series', 'capture', now() - interval '1 day', true, 'dagster:run-known-1'),
            ('twelvedata', '/time_series', 'rogue-caller', now() - interval '1 day', true, 'dagster:run-orphan-1'),
            ('twelvedata', '/time_series', 'script', now() - interval '1 day', true, 'script:vendor_smoke:1'),
            ('twelvedata', '/time_series', 'old-caller', now() - interval '8 days', true, 'dagster:run-old-1')
        """
    )

    code_with_dagster = check(
        "postgresql://borrowed",
        invariants=(inv,),
        connect=lambda _url: _Borrowed(conn),
        persist=True,
        dagster_run_id="ledger-h2-test",
        commit=False,
    )
    _ = capsys.readouterr()
    assert code_with_dagster == 0

    rows = conn.execute(
        """
        select invariant_id, kind, population, violation_count, sample_rows
        from mart.output_invariant_results
        where dagster_run_id = 'ledger-h2-test'
        """
    ).fetchall()
    assert len(rows) == 1
    invariant_id, kind, population, violation_count, sample_rows = rows[0]
    assert invariant_id == "ledger-runs-in-dagster"
    assert kind == "fact"
    assert population == 2
    assert violation_count == 1
    assert len(sample_rows) == 1 and sample_rows[0][0] == "dagster:run-orphan-1"
