"""Each deployed-surface property fails when violated — A4 C1 (#673).

`tools/surface_contract.py` exists because four production releases were spent
on one redirect, and every defect in it was invisible to the TestClient that
"covered" it. A probe for that class is worth nothing unless each of its
verdicts is proven to fire, so every property here is driven by a stub server
that violates exactly one thing — the deployed instances cannot be broken on
purpose, and a probe validated only against a healthy target is a probe that
has never been observed to fail.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from truealpha_runtime.testing import load_tool

REPO_ROOT = Path(__file__).resolve().parents[3]

# load_tool, not a hand-rolled spec_from_file_location: it registers the module
# in sys.modules before exec, which a hand copy forgets and the tool's own
# dataclass then fails on (#583, enforced by test_ci_workflows.py).
surface_contract = load_tool("surface_contract")

# Each scenario names one violated property; "healthy" violates none.
SCENARIOS = {
    "healthy": {},
    "health_down": {"health_status": 503},
    "scheme_downgraded": {"redirect_to": "http://HOST/api/mcp/"},
    "prefix_dropped": {"redirect_to": "http://HOST/mcp/"},
    "post_not_allowed": {"post_status": 405},
    "endpoint_missing": {"endpoint_status": 404},
    "no_redirect": {"redirect_status": 200},
}


SEEN_AGENTS: list[str] = []


def build_handler(scenario: dict[str, object], host: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:  # keep pytest output readable
            return

        def _send(self, status: int, location: str = "") -> None:
            SEEN_AGENTS.append(self.headers.get("User-Agent", ""))
            self.send_response(status)
            if location:
                self.send_header("Location", location.replace("HOST", host))
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's contract
            if self.path == "/api/health":
                self._send(int(scenario.get("health_status", 200)))
            elif self.path == "/api/mcp":
                status = int(scenario.get("redirect_status", 307))
                self._send(status, str(scenario.get("redirect_to", "http://HOST/api/mcp/")) if status >= 300 else "")
            elif self.path == "/api/mcp/":
                self._send(int(scenario.get("endpoint_status", 406)))
            else:
                self._send(404)

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/api/mcp":
                self._send(int(scenario.get("post_status", 307)), "http://HOST/api/mcp/")
            else:
                self._send(404)

    return Handler


@pytest.fixture
def serve() -> Iterator[object]:
    servers: list[HTTPServer] = []

    def start(scenario_name: str) -> str:
        scenario = dict(SCENARIOS[scenario_name])
        server = HTTPServer(("127.0.0.1", 0), build_handler(scenario, "x"))
        host = f"127.0.0.1:{server.server_port}"
        server.RequestHandlerClass = build_handler(scenario, host)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://{host}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def test_a_healthy_surface_reports_nothing(serve) -> None:  # type: ignore[no-untyped-def]
    assert surface_contract.check(serve("healthy")) == []


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("health_down", "the surface is not serving"),
        ("prefix_dropped", "the /api prefix is dropped"),
        ("post_not_allowed", "the redirect handler covers GET only again"),
        ("endpoint_missing", "the redirect points at nothing"),
        ("no_redirect", "not a redirect"),
    ],
)
def test_each_violated_property_names_itself(serve, scenario: str, expected: str) -> None:  # type: ignore[no-untyped-def]
    failures = surface_contract.check(serve(scenario))
    assert any(expected in failure for failure in failures), (
        f"scenario {scenario!r} violates a property the probe did not report; it said {failures}"
    )


def test_a_tls_downgrade_is_reported_and_an_http_base_is_not_slandered() -> None:
    """The founding defect (v0.0.26): the redirect dropped https, and a client
    follows it. Exercised through the real judgement, not by reading the source
    for a substring — that version passed while an `if ...: pass` would have
    silently stopped detecting it (review). A stub cannot serve TLS, and
    binding it would prove nothing about Traefik, so the pure judgement is the
    honest place to drive this.
    """
    downgraded = surface_contract.judge_redirect("https://x/api/mcp", "https", 307, "http://x/api/mcp/")
    assert any("scheme is downgraded" in failure for failure in downgraded), downgraded

    # A http base redirecting to http is correct; crying wolf there would make
    # every local and preview target fail, and a probe that fails everywhere
    # stops being read.
    assert surface_contract.judge_redirect("http://x/api/mcp", "http", 307, "http://x/api/mcp/") == []


def test_the_exit_code_ci_actually_reads_follows_the_verdict(serve) -> None:  # type: ignore[no-untyped-def]
    """`main()` is the only code CI observes — the workflow step is bare
    `python3 tools/surface_contract.py <base>` under `set -e`. Testing only
    check() leaves the verdict-to-exit-code translation unproven, which is the
    same shape as the TestClient that "covered" the redirect: everything green,
    nothing that runs in production actually exercised (review).
    """
    assert surface_contract.main([serve("healthy")]) == 0
    assert surface_contract.main([serve("endpoint_missing")]) == 1
    assert surface_contract.main(["not-a-url"]) == 2


def test_the_probe_identifies_itself_to_the_edge(serve) -> None:  # type: ignore[no-untyped-def]
    """Measured: the edge answers urllib's default `Python-urllib/3.x` with 403
    while curl gets 200 for the same URL. Without an explicit User-Agent the
    daily probe reports the surface down every day, and a check that cries wolf
    is worse than no check.

    Asserted from what the SERVER received, not from the tool's source text: a
    source grep passes while the header is built and then dropped, and this file
    has already had one such placeholder replaced (review).
    """
    SEEN_AGENTS.clear()
    surface_contract.check(serve("healthy"))
    assert SEEN_AGENTS, "the probe made no request at all"
    for agent in SEEN_AGENTS:
        assert "Python-urllib" not in agent, (
            f"the probe sent User-Agent {agent!r} — the edge answers urllib's default with 403, "
            f"so the daily run would report a healthy surface as down"
        )
        assert "truealpha" in agent.lower(), f"the probe does not identify itself: {agent!r}"
