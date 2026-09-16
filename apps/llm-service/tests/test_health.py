from fastapi.testclient import TestClient
from llm_service import main
from llm_service.config import Settings
from llm_service.main import ROUTED_PREFIX, app


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
    # Additive: every fact the endpoint reported before is still there.
    assert {"status", "git_sha", "data_engine_parser", "data_engine_git_sha", "governed_pointers"} <= set(payload)


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
    with TestClient(app, client=("10.0.1.76", 50000)) as client:
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
