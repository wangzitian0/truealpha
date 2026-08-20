import os

os.environ.setdefault("MCP_ALLOWED_HOSTS", '["testserver","localhost"]')

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


def test_app_starts_with_the_mcp_mount_and_serves_health_under_its_lifespan():
    """Proves the /mcp mount's session manager wiring doesn't break app startup (#348)."""
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200


def test_mcp_redirect_keeps_tls_and_the_routed_prefix() -> None:
    """`GET /api/mcp` must not hand a client a downgraded or off-service URL.

    On production it answered

        307 -> http://truealpha.club/mcp/

    dropping TLS and the /api prefix in one hop and landing on app-web's 404.
    init.md principle 21 requires TLS on every non-local MCP endpoint, and a
    client follows a 307.

    Both halves are asserted because each was fixed separately and the first
    attempt only fixed one: ProxyHeadersMiddleware restores the scheme, and
    Starlette's own redirect_slashes does NOT apply root_path to a Mount, so the
    Location was https://.../mcp/ — TLS restored, prefix still gone.

    The X-Forwarded-Proto header is what Traefik sends; nothing here can observe
    the real proxy, so the header is the contract being pinned.
    """
    response = TestClient(app).get("/mcp", follow_redirects=False, headers={"X-Forwarded-Proto": "https"})
    location = response.headers["location"]
    assert response.status_code == 307
    assert location.startswith("https://"), f"redirect drops TLS: {location}"
    assert "/api/mcp/" in location, f"redirect drops the routed prefix: {location}"
