from fastapi.testclient import TestClient
from llm_service.main import app


def test_health():
    resp = TestClient(app).get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "git_sha": "unknown"}


def test_health_reports_the_deployed_git_sha(monkeypatch):
    """#508: tools/health_check.py needs this to confirm the deployed release is live."""
    monkeypatch.setenv("GIT_COMMIT_SHA", "abc1234")
    resp = TestClient(app).get("/health")
    assert resp.json() == {"status": "ok", "git_sha": "abc1234"}


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
            "/mcp", follow_redirects=False, headers={"X-Forwarded-Proto": "https"}
        )

    location = redirect.headers["location"]
    assert redirect.status_code == 307
    assert location.startswith("https://"), f"redirect drops TLS: {location}"
    assert "/api/mcp/" in location, f"redirect drops the routed prefix: {location}"
    assert endpoint.status_code == 200, (
        f"the MCP endpoint answers {endpoint.status_code}; a redirect fix that breaks routing points clients at a 404"
    )
    assert untrusted.headers["location"].startswith("http://"), (
        "an untrusted peer set the scheme — trusted_hosts is too wide"
    )
