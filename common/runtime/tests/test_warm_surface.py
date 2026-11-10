# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import re; import subprocess; import threading; import time; from collections.abc import Iterator; from http.server import BaseHTTPRequestHandler, HTTPServer; from pathlib import Path; import pytest; REPO_ROOT = Path(__file__).resolve().parents[3]; WARM = REPO_ROOT / "tools" / "warm_surface.sh"
def build_handler(status: int, stall_seconds: float = 0.0) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None: return
        def do_GET(self) -> None:
            if stall_seconds: time.sleep(stall_seconds)
            self.send_response(status); self.send_header("Content-Length", "0"); self.end_headers()
    return Handler
@pytest.fixture
def serve() -> Iterator[object]:
    servers: list[HTTPServer] = []
    def start(status: int, stall_seconds: float = 0.0) -> str: server = HTTPServer(("127.0.0.1", 0), build_handler(status, stall_seconds)); servers.append(server); threading.Thread(target=server.serve_forever, daemon=True).start(); return f"http://127.0.0.1:{server.server_port}"
    yield start
    for server in servers: server.shutdown(); server.server_close()
def run_warm(base: str, *paths: str, budget: str = "3") -> subprocess.CompletedProcess[str]:
    try: return subprocess.run( ["bash", str(WARM), base, *paths], capture_output=True, text=True, check=False, timeout=30, env={"PATH": "/usr/bin:/bin:/usr/local/bin", "WARM_BUDGET_SECONDS": budget, "WARM_ATTEMPT_TIMEOUT": "2"}, )
    except subprocess.TimeoutExpired: pytest.fail( f'warm_surface.sh did not exit within 30s on a {budget}s budget — the deadline is not bounding the loop, so a release step would hang until the job timeout' )
def test_a_warm_surface_returns_immediately(serve) -> None:  # type: ignore[no-untyped-def]
    base = serve(200); started = time.monotonic(); result = run_warm(base, "/", "/research/rankings"); assert result.returncode == 0, result.stderr; assert time.monotonic() - started < 3, "a surface answering 200 should not consume the budget"; assert result.stdout.count("answered 200") == 2, result.stdout
def test_a_surface_that_never_recovers_still_exits_zero(serve) -> None:  # type: ignore[no-untyped-def]
    result = run_warm(serve(503), "/"); assert result.returncode == 0, ( f'warm_surface exited {result.returncode} on a 5xx surface — it can now fail a release for a page that is merely slow, which is the defect it was written to remove' ); assert "budget spent" in result.stdout, result.stdout
def test_an_unreachable_surface_still_exits_zero() -> None: result = run_warm("http://127.0.0.1:9", "/"); assert result.returncode == 0, result.stderr; assert "budget spent" in result.stdout, result.stdout
def test_the_budget_is_wall_clock_not_an_attempt_count(serve) -> None:  # type: ignore[no-untyped-def]
    started = time.monotonic(); result = run_warm(serve(503), "/", "/other", budget="4"); elapsed = time.monotonic() - started; assert result.returncode == 0; assert elapsed < 12, ( f"two paths against a failing surface took {elapsed:.1f}s on a 4s budget — the budget is per attempt again, so the walk starts minutes late and the log's claim is false" )
def test_misuse_does_not_fail_the_caller() -> None: result = subprocess.run(["bash", str(WARM)], capture_output=True, text=True, check=False); assert result.returncode == 0, "even a usage error must not fail the release step"; assert "usage" in result.stderr
def test_the_budget_never_reports_a_negative_remainder(serve) -> None:  # type: ignore[no-untyped-def]
    result = run_warm(serve(503, stall_seconds=3.0), "/", budget="1"); assert result.returncode == 0, result.stderr; negatives = re.findall(r"(-\d+)s of budget left", result.stdout); assert not negatives, f"reported {negatives} seconds remaining: {result.stdout!r}"
