"""Falsifiable unit tests for runtime-hooks.manifest.json and SDK delivery health polling CLI."""

from __future__ import annotations

import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from infra2_sdk.delivery import RuntimeHooksManifest, resolve_check_cells

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_delivery_poll_cli_sends_non_urllib_user_agent_and_checks_version() -> None:
    seen_ua: list[str] = []
    mode = ["200"]

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            ua = self.headers.get("User-Agent", "")
            seen_ua.append(ua)
            if not ua or ua.startswith("Python-urllib"):
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"cloudflare 403 blocked")
                return
            if mode[0] == "redir":
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
        ok = subprocess.run(
            [
                sys.executable,
                "-m",
                "infra2_sdk.delivery",
                "poll",
                "--url",
                f"http://127.0.0.1:{srv.server_port}/api/health",
                "--expected-version",
                "abcdef1",
                "--timeout-seconds",
                "2",
                "--interval-seconds",
                "0.2",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert ok.returncode == 0, ok.stderr
        assert seen_ua and all(ua.startswith("infra2-sdk/") for ua in seen_ua)

        mismatch = subprocess.run(
            [
                sys.executable,
                "-m",
                "infra2_sdk.delivery",
                "poll",
                "--url",
                f"http://127.0.0.1:{srv.server_port}/api/health",
                "--expected-version",
                "v0.3.99",
                "--timeout-seconds",
                "0.4",
                "--interval-seconds",
                "0.2",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert mismatch.returncode != 0

        mode[0] = "redir"
        redir = subprocess.run(
            [
                sys.executable,
                "-m",
                "infra2_sdk.delivery",
                "poll",
                "--url",
                f"http://127.0.0.1:{srv.server_port}/api/health",
                "--timeout-seconds",
                "0.4",
                "--interval-seconds",
                "0.2",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert redir.returncode != 0
    finally:
        srv.shutdown()
        srv.server_close()


def test_runtime_hooks_manifest_smoke_and_health_cells() -> None:
    manifest = RuntimeHooksManifest.from_file(_REPO_ROOT / "runtime-hooks.manifest.json")
    by_id = {row.id: row for row in manifest.checks}
    assert resolve_check_cells(by_id["smoke"]) == ("preview.operation", "staging.operation", "prod.operation")
    assert resolve_check_cells(by_id["health"]) == ("preview.post", "staging.post", "prod.post")
    assert "infra2_sdk.delivery poll" in " ".join(by_id["smoke"].argv)
    assert "infra2_sdk.delivery poll" in " ".join(by_id["health"].argv)
