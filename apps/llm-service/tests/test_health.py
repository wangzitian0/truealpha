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
