"""The warm-up can never fail the gate it runs before — #698.

`tools/warm_surface.sh` pays the cold render once so the surface walk's first
navigation does not race the container swap. Its single non-negotiable
property is that it VERIFIES NOTHING: a warm-up that can go red turns a slow
page into a failed release, which is a worse version of the problem it was
added to fix.

That property is proven by running the script — against a server that never
answers, one that answers 5xx forever, and one that answers immediately. The
first version of this guard asserted it by slicing the workflow's YAML text
instead, and the slice silently selected the whole script: PyYAML strips a
block scalar's common indentation, so a separator copied from the file's
appearance was never found and `split` returned its input unchanged. It
passed for a reason unrelated to the property.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
WARM = REPO_ROOT / "tools" / "warm_surface.sh"


def build_handler(status: int, stall_seconds: float = 0.0) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's contract
            # A stall, not a refusal: a refused connection returns instantly and
            # consumes none of the budget, so it cannot reproduce an attempt
            # that OVERRUNS what was left — which is the case under test.
            if stall_seconds:
                time.sleep(stall_seconds)
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler


@pytest.fixture
def serve() -> Iterator[object]:
    servers: list[HTTPServer] = []

    def start(status: int, stall_seconds: float = 0.0) -> str:
        server = HTTPServer(("127.0.0.1", 0), build_handler(status, stall_seconds))
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{server.server_port}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def run_warm(base: str, *paths: str, budget: str = "3") -> subprocess.CompletedProcess[str]:
    """A hard timeout, because the failure mode under test is a loop that never
    exits. Without it a broken budget makes these tests HANG rather than fail —
    which is how a red case ate a ten-minute run before this line existed."""
    try:
        return subprocess.run(
            ["bash", str(WARM), base, *paths],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env={"PATH": "/usr/bin:/bin:/usr/local/bin", "WARM_BUDGET_SECONDS": budget, "WARM_ATTEMPT_TIMEOUT": "2"},
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"warm_surface.sh did not exit within 30s on a {budget}s budget — the deadline is not "
            f"bounding the loop, so a release step would hang until the job timeout"
        )


def test_a_warm_surface_returns_immediately(serve) -> None:  # type: ignore[no-untyped-def]
    base = serve(200)
    started = time.monotonic()
    result = run_warm(base, "/", "/research/rankings")
    assert result.returncode == 0, result.stderr
    assert time.monotonic() - started < 3, "a surface answering 200 should not consume the budget"
    assert result.stdout.count("answered 200") == 2, result.stdout


def test_a_surface_that_never_recovers_still_exits_zero(serve) -> None:  # type: ignore[no-untyped-def]
    """The property the whole script exists for. A 5xx forever is a real
    outage, and it is still not this script's verdict to render — the walk
    that runs next is the gate, and it reports the failure with a page name
    and a screenshot instead of a curl status."""
    result = run_warm(serve(503), "/")
    assert result.returncode == 0, (
        f"warm_surface exited {result.returncode} on a 5xx surface — it can now fail a release "
        f"for a page that is merely slow, which is the defect it was written to remove"
    )
    assert "budget spent" in result.stdout, result.stdout


def test_an_unreachable_surface_still_exits_zero() -> None:
    """No listener at all: curl fails rather than returning a status, which is
    the path where an unguarded `code=$(curl ...)` under `set -e` would abort."""
    result = run_warm("http://127.0.0.1:9", "/")
    assert result.returncode == 0, result.stderr
    assert "budget spent" in result.stdout, result.stdout


def test_the_budget_is_wall_clock_not_an_attempt_count(serve) -> None:  # type: ignore[no-untyped-def]
    """An attempt count means nothing when each attempt can run to its own
    timeout: 18 attempts at a 20 s timeout is anywhere from seconds to
    7 minutes, and the state this exists for — up but slow — is exactly the one
    where attempts run long (review). The message promised 90 s; the bound must
    be real."""
    started = time.monotonic()
    result = run_warm(serve(503), "/", "/other", budget="4")
    elapsed = time.monotonic() - started
    assert result.returncode == 0
    assert elapsed < 12, (
        f"two paths against a failing surface took {elapsed:.1f}s on a 4s budget — the budget is "
        f"per attempt again, so the walk starts minutes late and the log's claim is false"
    )


def test_misuse_does_not_fail_the_caller() -> None:
    result = subprocess.run(["bash", str(WARM)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, "even a usage error must not fail the release step"
    assert "usage" in result.stderr


def test_the_budget_never_reports_a_negative_remainder(serve) -> None:  # type: ignore[no-untyped-def]
    """An attempt can overrun what was left — a 1 s budget against a server
    that stalls past the 2 s attempt timeout — and "-1s of budget left" is not
    something an operator can act on (review on #722).

    The server STALLS rather than refusing: a refused connection returns
    instantly and consumes none of the budget, so it cannot produce the
    overrun this is about. The first version of this test used a closed port
    and redprove called it INERT.
    """
    result = run_warm(serve(503, stall_seconds=3.0), "/", budget="1")
    assert result.returncode == 0, result.stderr
    negatives = re.findall(r"(-\d+)s of budget left", result.stdout)
    assert not negatives, f"reported {negatives} seconds remaining: {result.stdout!r}"
