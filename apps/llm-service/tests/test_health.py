import json
import os
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from fastapi.testclient import TestClient
from llm_service import main
from llm_service.config import Settings
from llm_service.main import ROUTED_PREFIX, app
from psycopg import sql
from truealpha_runtime.testing import apply_migration_chain, load_tool, skip_or_fail


def test_health():
    resp = TestClient(app).get("/health")
    assert resp.status_code == 200
    # data_engine_parser is asserted separately (libs/runtime test_health_check.py): it
    # reports the DATA ENGINE's vintage, which has no HTTP surface of its own (#712). With
    # no database in a unit test the honest answer is "unknown" -- and it must never raise.
    payload = resp.json()
    # With a migrated but empty database (CI) the pointer view exists and reports no
    # head; with no database at all the read fails closed to "unknown". Both are honest
    # answers to "which pointers advanced"; a value that is neither is the bug.
    assert payload.pop("governed_pointers") in ([], "unknown")
    # #876: same two honest answers for the nightly verdicts.
    assert payload.pop("nightly_verdicts") in ([], "unknown")
    assert payload == {
        "status": "ok",
        "git_sha": "unknown",
        "data_engine_parser": "unknown",
        "data_engine_git_sha": "unknown",
        "data_engine_image_digest": "unknown",
    }


def test_health_reports_the_deployed_git_sha(monkeypatch):
    """#508: tools/health_check.py needs this to confirm the deployed release is live.

    Through `settings` since #784: the value this endpoint publishes is the one the manifest
    it boot-validates against declared, not whatever sits in the process environment beside
    it. The environment is set to a DIFFERENT value here, and must lose."""
    monkeypatch.setattr(main, "settings", Settings(_env_file=None, git_commit_sha="abc1234"))
    monkeypatch.setenv("GIT_COMMIT_SHA", "def5678-from-the-environment")
    resp = TestClient(app).get("/health")
    payload = resp.json()
    assert payload.pop("governed_pointers") in ([], "unknown")
    assert payload.pop("nightly_verdicts") in ([], "unknown")
    assert payload == {
        "status": "ok",
        "git_sha": "abc1234",
        "data_engine_parser": "unknown",
        "data_engine_git_sha": "unknown",
        "data_engine_image_digest": "unknown",
    }


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _VerdictsOnly:
    """A connection whose only readable relation is mart.nightly_verdicts: every other read
    fails the way a database that predates it does, and must not take this one down."""

    def __init__(self, rows):
        self.rows = rows
        self.asked = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def rollback(self):
        return None

    def execute(self, sql, *_args):
        import psycopg

        self.asked.append(sql)
        if "mart.nightly_verdicts" in sql:
            return _Rows(self.rows)
        raise psycopg.errors.UndefinedTable("relation does not exist")


def test_health_reports_the_newest_verdict_per_nightly_check(monkeypatch) -> None:
    """#876: `tools/nightly_verdicts.py` pages from this field — the runner cannot reach the
    database. Each entry carries the check, when it ran (a timestamp, so the checker measures
    the age on its own clock), whether it was green, and its summary; the other facts' reads
    failing must not cost it."""
    from datetime import UTC, datetime

    import psycopg

    ran = datetime(2026, 9, 16, 0, 15, tzinfo=UTC)
    connection = _VerdictsOnly(
        [
            ("model_key_health", ran, False, "failed: auth-rejected: the provider answered HTTP 401"),
            ("output_invariants", ran, True, "19 held, 0 deferred, 0 empty"),
        ]
    )
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: connection)
    payload = TestClient(app).get("/health").json()
    assert payload["nightly_verdicts"] == [
        {
            "check": "model_key_health",
            "ran_at": "2026-09-16T00:15:00+00:00",
            "ok": False,
            "summary": "failed: auth-rejected: the provider answered HTTP 401",
        },
        {
            "check": "output_invariants",
            "ran_at": "2026-09-16T00:15:00+00:00",
            "ok": True,
            "summary": "19 held, 0 deferred, 0 empty",
        },
    ]
    # The read is the newest row per check, never every row of an append-only table.
    (verdict_sql,) = [sql for sql in connection.asked if "mart.nightly_verdicts" in sql]
    assert "distinct on (check_name)" in verdict_sql and "ran_at desc" in verdict_sql
    # A cap would silently drop checks past it, which the tool then reports as missing.
    assert "limit" not in verdict_sql.lower()
    # Additive: every fact the endpoint reported before is still there.
    assert {"status", "git_sha", "data_engine_parser", "data_engine_git_sha", "governed_pointers"} <= set(payload)


def test_a_pending_verdict_is_reported_as_null_not_as_red(monkeypatch) -> None:
    """release_fetch_proof records `ok = null` until the deployment's first fetching tick has
    run. Coerced by `bool()`, the pending proof read as a failed check and paged every deploy."""
    from datetime import UTC, datetime

    import psycopg

    ran = datetime(2026, 9, 17, 6, 15, tzinfo=UTC)
    connection = _VerdictsOnly([("release_fetch_proof", ran, None, "no scheduled or forced run yet")])
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: connection)
    payload = TestClient(app).get("/health").json()
    assert payload["nightly_verdicts"] == [
        {
            "check": "release_fetch_proof",
            "ran_at": "2026-09-17T06:15:00+00:00",
            "ok": None,
            "summary": "no scheduled or forced run yet",
        }
    ]


class _PointersOnly(_VerdictsOnly):
    """A connection whose only readable relations are the two the pointer read uses (#1062).
    Every other read fails the way a database that predates it does, and must not take this one
    down. `environments` are the rows of `mart.served_head_environments`; None makes that read fail."""

    def __init__(self, rows, environments=()):
        super().__init__(rows)
        self.environments = environments

    def execute(self, sql, *_args):
        self.asked.append(sql)
        if "mart.served_head_environments" in sql:
            if self.environments is None:
                raise psycopg.errors.UndefinedTable("relation does not exist")
            return _Rows(list(self.environments))
        if "mart.served_head" in sql:
            return _Rows(self.rows)
        raise psycopg.errors.UndefinedTable("relation does not exist")


#: The newest head per universe, as `mart.served_head` returns it. One head is fresh, one is
#: stale and one is withheld. Each has its own cadence limit.
SERVED_ROWS = [
    (
        "universe:canary-us-2026-06-30",
        datetime(2026, 9, 3, 23, 47, tzinfo=UTC),
        Decimal("771.0123"),
        "stale",
        72,
        "older_than_30d",
        "unavailable",
    ),
    (
        "universe:qqq-us-2026-06-30",
        datetime(2026, 9, 23, 23, 20, tzinfo=UTC),
        Decimal("311.04"),
        "stale",
        72,
        "older_than_3d",
        "available",
    ),
    (
        "universe:topt-us-2026-03-31",
        datetime(2026, 10, 6, 1, 0, tzinfo=UTC),
        Decimal("5.04"),
        "fresh",
        72,
        None,
        "available",
    ),
]

#: The keys `/health` answered before #1062 and still answers. A new top-level key breaks the
#: tools and tests that compare the whole answer.
TOP_LEVEL_KEYS = {
    "status",
    "git_sha",
    "data_engine_parser",
    "data_engine_git_sha",
    "data_engine_image_digest",
    "governed_pointers",
    "nightly_verdicts",
}


def test_health_reports_each_pointer_with_its_label_limit_reason_and_availability(monkeypatch) -> None:
    """#1062: the endpoint reports what `mart.served_head` says, per universe, and adds no key
    at the top level. The age keeps the one-decimal rounding it always had."""
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: _PointersOnly(SERVED_ROWS))
    payload = TestClient(app).get("/health").json()
    assert set(payload) == TOP_LEVEL_KEYS
    assert payload["governed_pointers"] == [
        {
            "universe_id": "universe:canary-us-2026-06-30",
            "advanced_at": "2026-09-03T23:47:00+00:00",
            "age_hours": 771.0,
            "freshness": "stale",
            "limit_hours": 72,
            "staleness_reason": "older_than_30d",
            "availability": "unavailable",
        },
        {
            "universe_id": "universe:qqq-us-2026-06-30",
            "advanced_at": "2026-09-23T23:20:00+00:00",
            "age_hours": 311.0,
            "freshness": "stale",
            "limit_hours": 72,
            "staleness_reason": "older_than_3d",
            "availability": "available",
        },
        {
            "universe_id": "universe:topt-us-2026-03-31",
            "advanced_at": "2026-10-06T01:00:00+00:00",
            "age_hours": 5.0,
            "freshness": "fresh",
            "limit_hours": 72,
            "staleness_reason": None,
            "availability": "available",
        },
    ]


def test_a_stale_or_withheld_head_does_not_turn_the_status_red(monkeypatch) -> None:
    """`status` is liveness: the deploy walk treats it as "the service is up". A frozen head is
    the freshness check's page, not a reason to take the service out of rotation."""
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: _PointersOnly(SERVED_ROWS))
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert {entry["freshness"] for entry in response.json()["governed_pointers"]} >= {"stale", "fresh"}


def test_the_pointer_read_goes_through_the_served_head_and_computes_no_age(monkeypatch) -> None:
    connection = _PointersOnly(SERVED_ROWS)
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: connection)
    TestClient(app).get("/health")
    (pointer_sql,) = [sql for sql in connection.asked if sql == main.GOVERNED_POINTERS_SQL]
    assert connection.asked.count(pointer_sql) == 1 and len(connection.asked) >= 1
    lowered = " ".join(pointer_sql.lower().split())
    assert "from mart.served_head" in lowered
    assert "distinct on (universe_id)" in lowered and "order by universe_id, advanced_at desc" in lowered
    assert "current_pointer" not in lowered, "the endpoint must read the served head, not the raw pointer"
    assert "now()" not in lowered and "extract(" not in lowered, "the endpoint must not compute an age"
    # The endpoint unpacks the columns by position, so their order is part of the contract.
    select_list = lowered.split("select distinct on (universe_id)")[1].split(" from ")[0]
    assert [column.strip() for column in select_list.split(",")] == [
        "universe_id",
        "advanced_at",
        "age_hours",
        "freshness",
        "limit_hours",
        "staleness_reason",
        "availability",
    ]


def test_the_freshness_tool_still_reads_the_new_entries(monkeypatch) -> None:
    """`tools/datahub_freshness.py` reads universe_id, advanced_at and age_hours only. It must
    parse the richer entries unchanged and judge the head on its own clock."""
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: _PointersOnly(SERVED_ROWS))
    body = TestClient(app).get("/health").text
    tool = load_tool("datahub_freshness")
    heads = tool.read_pointers("https://example.invalid/api/health", lambda _url: (200, body))
    assert [head.universe_id for head in heads] == [row[0] for row in SERVED_ROWS]
    assert [head.age_hours for head in heads] == [771.0, 311.0, 5.0]
    reference = datetime(2026, 10, 6, 6, 0, tzinfo=UTC)
    assert (
        tool.check_pointer_freshness(
            "https://example.invalid/api/health", http_get=lambda _url: (200, body), now=reference
        )
        == 1
    )


def test_a_head_list_never_runs_the_environments_read(monkeypatch) -> None:
    connection = _PointersOnly(SERVED_ROWS, environments=[(1,)])
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: connection)
    TestClient(app).get("/health")
    assert main.HEAD_ENVIRONMENTS_SQL not in connection.asked


def test_no_served_row_and_a_head_in_some_environment_reports_unknown(monkeypatch) -> None:
    """The identity row hides every head. An empty list would read as a database that never advanced."""
    connection = _PointersOnly([], environments=[(1,)])
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: connection)
    payload = TestClient(app).get("/health").json()
    assert payload["governed_pointers"] == "unknown"
    assert payload["status"] == "ok"
    assert main.HEAD_ENVIRONMENTS_SQL in connection.asked


def test_no_served_row_and_no_head_anywhere_reports_an_empty_list(monkeypatch) -> None:
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: _PointersOnly([], environments=[]))
    assert TestClient(app).get("/health").json()["governed_pointers"] == []


def test_an_unreadable_environments_view_reports_unknown_not_an_empty_list(monkeypatch) -> None:
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: _PointersOnly([], environments=None))
    assert TestClient(app).get("/health").json()["governed_pointers"] == "unknown"


def test_the_freshness_tool_fails_on_unknown_pointers(monkeypatch) -> None:
    """The marker is the one `tools/datahub_freshness.py` already fails on, with a clear text."""
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: _PointersOnly([], environments=[(1,)]))
    body = TestClient(app).get("/health").text
    tool = load_tool("datahub_freshness")
    url = "https://example.invalid/api/health"
    with pytest.raises(tool.PointerFreshnessFailure, match="could not read its governed pointers"):
        tool.read_pointers(url, lambda _url: (200, body))
    assert tool.check_pointer_freshness(url, http_get=lambda _url: (200, body)) == 1


#: The real `psycopg.connect`, kept before any test replaces it. A fixture that runs its teardown
#: while a test's patch is still in place must still reach the database.
REAL_CONNECT = psycopg.connect


def _named(database: str) -> str:
    base = urlsplit(os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/truealpha"))
    return urlunsplit((base.scheme, base.netloc, f"/{database}", base.query, ""))


@pytest.fixture(scope="module")
def template_database() -> Iterator[str]:
    """A scratch database with the real chain and no head. Each test clones it."""
    name = f"truealpha_health_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    try:
        with REAL_CONNECT(_named("postgres"), connect_timeout=3, autocommit=True) as admin:
            admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        skip_or_fail(f"no local Postgres; CI runs the required integration coverage ({error})")
    try:
        apply_migration_chain(_named(name))
        yield name
    finally:
        with REAL_CONNECT(_named("postgres"), autocommit=True) as admin:
            admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))


#: Three heads of known age, for the universes the registry knows.
THREE_HEADS = (
    ("universe:topt-us-health", timedelta(days=4), None),
    ("universe:qqq-us-health", timedelta(days=1), None),
    ("universe:canary-us-health", timedelta(days=31), None),
)


@pytest.fixture
def clone(template_database: str) -> Iterator[Callable[..., str]]:
    """Make a database from the template, with committed heads and an identity row to order.

    A head is (universe, age, environment). An environment of None means the identity row's own.
    `identity` is "keep", "missing", or the environment name to store.
    """
    made: list[str] = []

    def make(heads=(), identity: str = "keep") -> str:
        name = f"truealpha_health_clone_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        made.append(name)
        with REAL_CONNECT(_named("postgres"), autocommit=True) as admin:
            admin.execute(
                sql.SQL("create database {} template {}").format(
                    sql.Identifier(name), sql.Identifier(template_database)
                )
            )
        with REAL_CONNECT(_named(name)) as connection:
            own = connection.execute("select environment from mart.environment_identity").fetchone()[0]
            for universe, age, environment in heads:
                digest = uuid.uuid4().hex * 2
                run_id = f"capture-run:{digest}"
                connection.execute(
                    "insert into staging.evidence_nodes (node_id, kind, content_sha256, valid_from, "
                    "transaction_time, recorded_at) values (%s, 'capture_run', %s, '2026-03-31', now(), now())",
                    (run_id, digest),
                )
                connection.execute(
                    "insert into mart.current_pointer (pointer_id, content_sha256, environment, universe_id, "
                    "universe_version, factor_id, target_run_id, sequence, previous_run_id, advanced_at) "
                    "values (%s, %s, %s, %s, 'v1', 'f', %s, 0, null, now() - %s)",
                    (f"current-pointer:{digest}", digest, environment or own, universe, run_id, age),
                )
            if identity == "missing":
                connection.execute("delete from mart.environment_identity")
            elif identity != "keep":
                connection.execute("update mart.environment_identity set environment = %s", (identity,))
            connection.commit()
        return _named(name)

    yield make
    with REAL_CONNECT(_named("postgres"), autocommit=True) as admin:
        for name in made:
            admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))


def health_of(monkeypatch, database: str) -> dict:
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: REAL_CONNECT(database))
    return TestClient(app).get("/health").json()


def test_health_ages_real_heads_through_the_real_served_head(monkeypatch, clone) -> None:
    """The endpoint, the real SQL and the real view together. A 4 day head reads stale. A 1 day
    head reads fresh. A 31 day head is withheld. Each uses its registry cadence."""
    payload = health_of(monkeypatch, clone(THREE_HEADS))
    by_universe = {entry["universe_id"]: entry for entry in payload["governed_pointers"]}
    assert set(by_universe) == {"universe:topt-us-health", "universe:qqq-us-health", "universe:canary-us-health"}
    assert payload["status"] == "ok"
    topt, qqq, canary = (by_universe[f"universe:{name}-us-health"] for name in ("topt", "qqq", "canary"))
    assert (topt["freshness"], topt["availability"], topt["staleness_reason"], topt["limit_hours"]) == (
        "stale",
        "available",
        "older_than_3d",
        72,
    )
    assert topt["age_hours"] == pytest.approx(96.0, abs=0.2)
    assert (qqq["freshness"], qqq["availability"], qqq["staleness_reason"]) == ("fresh", "available", None)
    assert (canary["freshness"], canary["availability"], canary["staleness_reason"]) == (
        "stale",
        "unavailable",
        "older_than_30d",
    )
    assert json.dumps(payload)  # the whole answer stays JSON


def test_a_missing_identity_row_with_heads_present_reports_unknown(monkeypatch, clone) -> None:
    payload = health_of(monkeypatch, clone(THREE_HEADS, identity="missing"))
    assert payload["governed_pointers"] == "unknown"
    assert payload["status"] == "ok"


def test_an_identity_that_matches_no_head_environment_reports_unknown(monkeypatch, clone) -> None:
    payload = health_of(monkeypatch, clone(THREE_HEADS, identity="somewhere-else"))
    assert payload["governed_pointers"] == "unknown"


def test_no_head_in_any_environment_reports_an_empty_list(monkeypatch, clone) -> None:
    assert health_of(monkeypatch, clone())["governed_pointers"] == []


def test_a_database_that_holds_two_environments_lists_only_the_one_it_serves(monkeypatch, clone) -> None:
    """Measured on Staging: the identity row says `staging` and the pointer view holds heads of
    both `production` and `staging`. The old query listed all of them."""
    heads = (
        ("universe:topt-us-health", timedelta(days=1), "staging"),
        ("universe:qqq-us-health", timedelta(days=1), "staging"),
        ("universe:topt-us-health", timedelta(days=9), "production"),
        ("universe:canary-us-health", timedelta(days=9), "production"),
    )
    payload = health_of(monkeypatch, clone(heads, identity="staging"))
    assert [entry["universe_id"] for entry in payload["governed_pointers"]] == [
        "universe:qqq-us-health",
        "universe:topt-us-health",
    ]
    assert {entry["freshness"] for entry in payload["governed_pointers"]} == {"fresh"}


def test_the_mcp_surface_keeps_tls_the_prefix_and_its_endpoint() -> None:
    """Three properties in one client, because they were traded for each other.

    On production `GET /api/mcp` answered

        307 -> http://truealpha.club/mcp/

    dropping TLS and the /api prefix in one hop, landing on app-web's 404.
    init.md principle 21 requires TLS on every non-local MCP endpoint and a
    client follows a 307.

    The first fix put the flags in the Dockerfile CMD, which infra2's compose
    overrides — it shipped as v0.0.26 and changed nothing. The second set
    FastAPI's `root_path`, which fixed the redirect and BROKE ROUTING: Starlette
    strips root_path while matching, Traefik had already stripped it, and
    staging's POST /api/mcp/ went 200 -> 404 while production stayed 200. A
    redirect pointing at a 404 is worse than the downgrade it replaced.

    So all three are asserted together, in one client: the session manager runs
    from the app lifespan and can only be started once per instance, which is
    why this is one test and not three.

    The request path is /mcp, not /api/mcp — Traefik strips the prefix before
    forwarding, and the redirect rebuilds it by hand.
    """
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        # Absorbed from test_app_starts_with_the_mcp_mount_and_serves_health_under
        # _its_lifespan (#348): the MCP session manager is a module-level
        # singleton whose run() may be entered ONCE per instance, so two tests
        # each opening a lifespan fail on whichever runs second. Same client,
        # same assertion.
        assert client.get("/health").status_code == 200, (
            "the /mcp mount's session manager wiring broke app startup (#348)"
        )
        redirect = client.get("/mcp", follow_redirects=False, headers={"X-Forwarded-Proto": "https"})
        # All three, not one. This handler covered GET only and a slashless
        # POST answered 405 on staging; asserting POST alone would leave the
        # same hole one method over, which is the shape being fixed (review).
        slashless = {
            method: client.request(
                method,
                "/mcp",
                follow_redirects=False,
                headers={"Accept": "application/json, text/event-stream"},
            )
            for method in ("GET", "POST", "DELETE")
        }
        endpoint = client.post(
            "/mcp/",
            headers={"Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        untrusted = TestClient(app, client=("203.0.113.7", 50000)).get(
            "/health/", follow_redirects=False, headers={"X-Forwarded-Proto": "https"}
        )
        trusted_slash = client.get("/health/", follow_redirects=False, headers={"X-Forwarded-Proto": "https"})

    # Status first, and `.get`: a method the handler does not accept answers 405
    # with no Location, and indexing turned that into a KeyError traceback
    # instead of a sentence naming what broke.
    assert redirect.status_code == 307, f"GET without the trailing slash answers {redirect.status_code}"
    location = redirect.headers.get("location", "")
    # Path-only. Asserting "starts with https" would REQUIRE the absolute form,
    # which is what lets a forged Host choose the destination (review). A
    # relative Location keeps the client's scheme without naming one.
    assert "://" not in location, f"an absolute Location lets the Host header choose the destination: {location}"
    assert location == f"{ROUTED_PREFIX}/mcp/", f"redirect is not the routed path: {location}"
    for method, answer in slashless.items():
        assert answer.status_code == 307, (
            f"{method} without the trailing slash answers {answer.status_code}; the "
            f"streamable-HTTP transport POSTs its body, GETs the SSE stream and DELETEs the "
            f"session, so any one missing makes a client unreachable"
        )
        # `.get`, not `[...]`: a method the handler does not accept answers 405
        # with no Location at all, and indexing turned that into a KeyError
        # traceback instead of a sentence naming the method.
        assert answer.headers.get("location") == f"{ROUTED_PREFIX}/mcp/", (
            f"{method} redirects to {answer.headers.get('location')!r}, not {ROUTED_PREFIX}/mcp/"
        )
    assert endpoint.status_code == 200, (
        f"the MCP endpoint answers {endpoint.status_code}; a redirect fix that breaks routing points clients at a 404"
    )
    # The redirect is identical for any peer now, so the trust boundary is
    # asserted where it remains observable: Starlette's own redirect_slashes
    # still builds an ABSOLUTE Location, so an unhandled trailing slash shows
    # what scheme the app believes it is serving.
    assert untrusted.headers["location"].startswith("http://"), (
        "an untrusted peer set the scheme — trusted_hosts is too wide"
    )
    # The negative alone proves nothing: with the middleware removed entirely
    # every peer gets http and the assertion above still passes. The positive is
    # what shows the boundary discriminates (found by red-proving it).
    assert trusted_slash.headers["location"].startswith("https://"), (
        "the proxy's X-Forwarded-Proto was ignored — ProxyHeadersMiddleware is not installed"
    )
