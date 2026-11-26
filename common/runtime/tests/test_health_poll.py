"""Falsifiable unit tests for tools/health_poll.py and runtime-hooks.manifest.json."""

from __future__ import annotations

import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from infra2_sdk.delivery import RuntimeHooksManifest, resolve_check_cells

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_health_poll():
    spec = importlib.util.spec_from_file_location("health_poll", _REPO_ROOT / "tools" / "health_poll.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    import sys

    sys.modules["health_poll"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_default_http_get_sends_non_urllib_user_agent_and_no_redirect() -> None:
    hp = _load_health_poll()
    seen_ua: list[str] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            ua = self.headers.get("User-Agent", "")
            seen_ua.append(ua)
            if not ua or ua.startswith("Python-urllib"):
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"cloudflare 403 blocked")
                return
            if self.path == "/redir":
                self.send_response(302)
                self.send_header("Location", "/api/health")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"healthy","version":"v0.3.15","git_sha":"abcdef123456"}')

        def log_message(self, *args: object) -> None:
            return None

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert hp._default_http_get(f"http://127.0.0.1:{srv.server_port}/redir", 2.0)[0] == 302
        rc = hp.main(
            [
                "--url",
                f"http://127.0.0.1:{srv.server_port}/api/health",
                "--expected-version",
                "v0.3.15",
                "--timeout-seconds",
                "2",
                "--interval-seconds",
                "0.5",
            ]
        )
        assert rc == 0
        assert all(ua.startswith("infra2-sdk/") for ua in seen_ua)
    finally:
        srv.shutdown()
        srv.server_close()


def test_poll_until_healthy_version_matching_and_timeout() -> None:
    hp = _load_health_poll()
    now = [0.0]
    responses = [
        (200, b'{"version":"v0.3.14"}'),
        (200, b'{"version":"v0.3.14","git_sha":"abcdef1234567890"}'),
    ]
    res = hp.poll_until_healthy(
        "https://truealpha-staging.truealpha.club/api/health",
        expected_version="abcdef1",
        timeout_seconds=10.0,
        interval_seconds=1.0,
        http_get=lambda _u, _t: responses.pop(0),
        monotonic=lambda: now[0],
        sleep=lambda s: now.__setitem__(0, now[0] + s),
    )
    assert res.ok is True and res.attempts == 2

    fail_res = hp.poll_until_healthy(
        "https://truealpha-staging.truealpha.club/api/health",
        expected_version="v0.3.15",
        timeout_seconds=2.0,
        interval_seconds=1.0,
        http_get=lambda _u, _t: (200, b'{"version":"v0.3.14"}'),
        monotonic=lambda: now[0],
        sleep=lambda s: now.__setitem__(0, now[0] + s),
    )
    assert fail_res.ok is False and "version mismatch" in fail_res.detail


def test_runtime_hooks_manifest_smoke_and_health_cells() -> None:
    manifest = RuntimeHooksManifest.from_file(_REPO_ROOT / "runtime-hooks.manifest.json")
    by_id = {row.id: row for row in manifest.checks}
    assert resolve_check_cells(by_id["smoke"]) == ("preview.operation", "staging.operation", "prod.operation")
    assert resolve_check_cells(by_id["health"]) == ("preview.post", "staging.post", "prod.post")
    assert "tools/health_poll.py" in " ".join(by_id["smoke"].argv)
    assert "tools/health_poll.py" in " ".join(by_id["health"].argv)
