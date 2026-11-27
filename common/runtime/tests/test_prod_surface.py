"""Focused unit tests for prod_surface read-only post-deploy check."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from truealpha_tools.prod_surface import main, verify_prod_surface

pytestmark = pytest.mark.gate


def _healthy_payload(**overrides: object) -> str:
    payload: dict[str, object] = {
        "status": "healthy",
        "version": "v0.3.8",
        "git_sha": "v0.3.8",
        "data_engine_git_sha": "v0.3.8",
        "governed_pointers": [{"universe_id": "topt", "freshness": "fresh", "availability": "available"}],
    }
    payload.update(overrides)
    return json.dumps(payload)


def _fake_http(responses: dict[str, tuple[int, str]]):
    def _get(url: str) -> tuple[int, str]:
        return responses[url]

    return _get


def test_prod_surface_passes_on_valid_health_and_web_routes() -> None:
    base = "https://truealpha.club"
    getter = _fake_http(
        {
            f"{base}/api/health": (200, _healthy_payload()),
            f"{base}/": (200, "<html></html>"),
            f"{base}/login": (200, "<html>Sign in</html>"),
        }
    )
    assert verify_prod_surface(base, version_ref="v0.3.8", http_get=getter) == []


def test_prod_surface_allows_local_version_ref_without_matching_release_tag() -> None:
    base = "https://truealpha.club"
    getter = _fake_http(
        {
            f"{base}/api/health": (
                200,
                _healthy_payload(version="local", git_sha="unknown", data_engine_git_sha="abc1234"),
            ),
            f"{base}/": (200, "<html></html>"),
            f"{base}/login": (200, "<html></html>"),
        }
    )
    assert verify_prod_surface(base, version_ref="local", http_get=getter) == []


@pytest.mark.parametrize("de_sha", ["", None, "unknown"])
def test_prod_surface_fails_closed_on_missing_or_unknown_data_engine_git_sha(de_sha: object) -> None:
    base = "https://truealpha.club"
    getter = _fake_http(
        {
            f"{base}/api/health": (200, _healthy_payload(data_engine_git_sha=de_sha)),
            f"{base}/": (200, "<html></html>"),
            f"{base}/login": (200, "<html></html>"),
        }
    )
    problems = verify_prod_surface(base, version_ref="v0.3.8", http_get=getter)
    assert any("data_engine_git_sha" in p for p in problems), problems


def test_prod_surface_fails_closed_on_mismatched_data_engine_tag() -> None:
    base = "https://truealpha.club"
    getter = _fake_http(
        {
            f"{base}/api/health": (200, _healthy_payload(data_engine_git_sha="v0.3.7")),
            f"{base}/": (200, "<html></html>"),
            f"{base}/login": (200, "<html></html>"),
        }
    )
    problems = verify_prod_surface(base, version_ref="v0.3.8", http_get=getter)
    assert any("data_engine_git_sha" in p for p in problems), problems


@pytest.mark.parametrize("pointers", [[], "unknown", None, {}])
def test_prod_surface_fails_closed_on_empty_or_invalid_governed_pointers(pointers: object) -> None:
    base = "https://truealpha.club"
    getter = _fake_http(
        {
            f"{base}/api/health": (200, _healthy_payload(governed_pointers=pointers)),
            f"{base}/": (200, "<html></html>"),
            f"{base}/login": (200, "<html></html>"),
        }
    )
    problems = verify_prod_surface(base, version_ref="v0.3.8", http_get=getter)
    assert any("governed_pointers" in p for p in problems), problems


def test_prod_surface_fails_closed_on_version_mismatch() -> None:
    base = "https://truealpha.club"
    getter = _fake_http(
        {
            f"{base}/api/health": (200, _healthy_payload(version="v0.3.7", git_sha="v0.3.7")),
            f"{base}/": (200, "<html></html>"),
            f"{base}/login": (200, "<html></html>"),
        }
    )
    problems = verify_prod_surface(base, version_ref="v0.3.8", http_get=getter)
    assert any("APP_VERSION_REF" in p for p in problems), problems


@pytest.mark.parametrize("failing_path", ["/", "/login"])
def test_prod_surface_fails_closed_on_non_200_web_route(failing_path: str) -> None:
    base = "https://truealpha.club"
    responses = {
        f"{base}/api/health": (200, _healthy_payload()),
        f"{base}/": (200, "<html></html>"),
        f"{base}/login": (200, "<html></html>"),
    }
    responses[f"{base}{failing_path}"] = (502, "Bad Gateway")
    problems = verify_prod_surface(base, version_ref="v0.3.8", http_get=_fake_http(responses))
    assert any(failing_path in p and "502" in p for p in problems), problems


def test_prod_surface_default_urllib_follows_root_redirect_to_login() -> None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/api/health":
                body = _healthy_payload().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/":
                self.send_response(307)
                self.send_header("Location", "/login")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/login":
                body = b"<html>Sign in</html>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        rc = main(["--app-url", f"http://127.0.0.1:{server.server_port}", "--version-ref", "v0.3.8"])
        assert rc == 0
    finally:
        server.shutdown()
        server.server_close()


def test_prod_surface_sends_audit_user_agent() -> None:
    seen: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            seen.append((self.path, self.headers.get("User-Agent", "")))
            if self.path == "/api/health":
                body = _healthy_payload(version="v0.3.17", git_sha="v0.3.17", data_engine_git_sha="v0.3.17").encode(
                    "utf-8"
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = b"<html>ok</html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        rc_ps = main(["--app-url", f"http://127.0.0.1:{server.server_port}", "--version-ref", "v0.3.17"])
        assert rc_ps == 0
        assert seen
        assert all(ua == "truealpha-ops-audit/1.0" for _, ua in seen)
    finally:
        server.shutdown()
        server.server_close()
