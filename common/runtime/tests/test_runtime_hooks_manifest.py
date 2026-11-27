"""Falsifiable unit tests for runtime-hooks.manifest.json and SDK delivery polling."""

from __future__ import annotations

import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from infra2_sdk.delivery import RuntimeHooksManifest, resolve_check_cells

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_runtime_hooks_manifest_smoke_and_health_use_sdk_delivery_poll() -> None:
    assert not (_REPO_ROOT / "tools" / "health_poll.py").exists()
    manifest = RuntimeHooksManifest.from_file(_REPO_ROOT / "runtime-hooks.manifest.json")
    by_id = {row.id: row for row in manifest.checks}
    assert resolve_check_cells(by_id["smoke"]) == ("preview.operation", "staging.operation", "prod.operation")
    assert resolve_check_cells(by_id["health"]) == ("preview.post", "staging.post", "prod.post")
    assert "infra2_sdk.delivery poll" in " ".join(by_id["smoke"].argv)
    assert "infra2_sdk.delivery poll" in " ".join(by_id["health"].argv)


def test_sdk_delivery_poll_cli_matches_version_and_rejects_mismatch() -> None:
    seen_ua: list[str] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            ua = self.headers.get("User-Agent", "")
            seen_ua.append(ua)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"healthy","version":"v0.3.23","git_sha":"abcdef123456"}')

        def log_message(self, *args: object) -> None:
            return None

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}/api/health"
        ok = subprocess.run(
            [
                sys.executable,
                "-m",
                "infra2_sdk.delivery",
                "poll",
                "--url",
                url,
                "--expected-version",
                "v0.3.23",
                "--timeout-seconds",
                "2",
                "--interval-seconds",
                "0.2",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert ok.returncode == 0, ok.stderr + ok.stdout
        assert json.loads(ok.stdout)["ok"] is True

        bad = subprocess.run(
            [
                sys.executable,
                "-m",
                "infra2_sdk.delivery",
                "poll",
                "--url",
                url,
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
        assert bad.returncode != 0
        assert json.loads(bad.stdout)["ok"] is False
        assert all(ua.startswith("infra2-sdk/") for ua in seen_ua)
    finally:
        srv.shutdown()
        srv.server_close()
