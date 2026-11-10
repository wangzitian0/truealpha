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

import json
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
    # #42: the shape staging and production answered on 2026-10-20, before the gate.
    "mcp_open": {"rpc_status": {"initialize": 200, "tools/call": 400}, "endpoint_status": 406},
    "tools_call_open": {"rpc_status": {"tools/call": 200}},
    "no_challenge": {"challenge": ""},
    "no_tool_list": {"no_tool_list": True},
}


SEEN_AGENTS: list[str] = []
STUB_TOKEN = "stub-token"
SEEN_RPC: list[tuple[str, str]] = []


def build_handler(scenario: dict[str, object], host: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:  # keep pytest output readable
            return

        def _send(
            self, status: int, location: str = "", challenge: str = "", body: bytes = b"", session: str = ""
        ) -> None:
            SEEN_AGENTS.append(self.headers.get("User-Agent", ""))
            self.send_response(status)
            if location:
                self.send_header("Location", location.replace("HOST", host))
            if challenge:
                self.send_header("WWW-Authenticate", challenge)
            if session:
                self.send_header("Mcp-Session-Id", session)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authenticated(self, rpc: str) -> None:
            """The gated endpoint with the right token: a session, then the tool list."""
            if rpc == "initialize":
                self._send(200, body=b'{"result": {}}', session="stub-session")
            elif rpc == "tools/list" and self.headers.get("Mcp-Session-Id") == "stub-session":
                tools = b'{"result": {}}' if scenario.get("no_tool_list") else b'{"result": {"tools": [{"name": "x"}]}}'
                self._send(200, body=tools)
            else:
                self._send(202 if rpc.startswith("notifications/") else 400)

        def _gated(self, status: int) -> None:
            """The MCP endpoint: 401 with a Bearer challenge unless the scenario opens it."""
            challenge = str(scenario.get("challenge", 'Bearer realm="truealpha-mcp"'))
            self._send(status, challenge=challenge if status == 401 else "")

        def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's contract
            if self.path == "/api/health":
                self._send(int(scenario.get("health_status", 200)))
            elif self.path == "/api/mcp":
                status = int(scenario.get("redirect_status", 307))
                self._send(status, str(scenario.get("redirect_to", "http://HOST/api/mcp/")) if status >= 300 else "")
            elif self.path == "/api/mcp/":
                self._gated(int(scenario.get("endpoint_status", 401)))
            else:
                self._send(404)

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/api/mcp":
                self._send(int(scenario.get("post_status", 307)), "http://HOST/api/mcp/")
            elif self.path == "/api/mcp/":
                length = int(self.headers.get("Content-Length", "0"))
                rpc = json.loads(self.rfile.read(length) or b"{}").get("method", "")
                SEEN_RPC.append((rpc, self.headers.get("Authorization", "")))
                if self.headers.get("Authorization") == f"Bearer {STUB_TOKEN}":
                    self._authenticated(rpc)
                    return
                statuses = dict(scenario.get("rpc_status", {}))  # type: ignore[call-overload]
                self._gated(int(statuses.get(rpc, 401)))
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
        ("mcp_open", "initialize without a credential answered 200, not 401"),
        ("mcp_open", "tools/call without a credential answered 400, not 401"),
        ("tools_call_open", "tools/call without a credential answered 200, not 401"),
        ("no_challenge", "answered 401 without a Bearer challenge"),
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


def test_a_surface_that_does_not_answer_is_a_verdict_not_a_traceback() -> None:
    """2026-09-02, production: `/api/health` answered HTTP 0 (connection refused) and
    the probe died with a traceback instead of a sentence. An unanswered surface is
    the first property violated — reported with the URL and the reason."""
    import socket

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    # Bound but never listening: the kernel refuses the connection (an OSError, not a
    # URLError) and nothing else can take the port meanwhile (review on #752).
    try:
        failures = surface_contract.check(f"http://127.0.0.1:{port}")
    finally:
        probe.close()
    assert len(failures) == 1
    assert (
        failures[0].startswith("the surface is not serving: GET http://127.0.0.1:") and "did not answer" in failures[0]
    )


def test_a_server_that_hangs_up_without_answering_is_also_a_verdict() -> None:
    """The other shape of HTTP 0: the edge accepts the TCP connection and closes it
    without a response (`http.client.RemoteDisconnected`, an OSError that urllib does
    not wrap in URLError)."""
    import socket
    import threading

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def hang_up() -> None:
        connection, _ = listener.accept()
        connection.close()

    thread = threading.Thread(target=hang_up, daemon=True)
    thread.start()
    try:
        failures = surface_contract.check(f"http://127.0.0.1:{port}")
    finally:
        listener.close()
    assert len(failures) == 1 and failures[0].startswith("the surface is not serving: GET"), failures


def test_a_surface_that_answers_health_and_then_hangs_up_is_still_a_verdict() -> None:
    """The guard has to cover EVERY request, not the first one (review on #752): an
    edge that serves /api/health and closes the connection on /api/mcp is the exact
    shape of a half-deployed release, and it must read as a sentence, not a traceback."""
    import socket
    import threading

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]
    served: list[str] = []

    def serve() -> None:
        while True:
            connection, _ = listener.accept()
            with connection:
                request = connection.recv(4096).decode("latin-1")
                path = request.split(" ", 2)[1] if " " in request else ""
                served.append(path)
                if path == "/api/health":
                    body = b'{"status": "ok"}'
                    connection.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                        + str(len(body)).encode()
                        + b"\r\nConnection: close\r\n\r\n"
                        + body
                    )
                # any other path: hang up without a response

    threading.Thread(target=serve, daemon=True).start()
    try:
        failures = surface_contract.check(f"http://127.0.0.1:{port}")
    finally:
        listener.close()
    assert served[0] == "/api/health" and len(served) >= 2, served
    assert len(failures) == 1
    assert failures[0].startswith("the surface is not serving: ") and "did not answer" in failures[0]


def test_the_credential_probe_sends_both_requests_and_no_credential(serve) -> None:  # type: ignore[no-untyped-def]
    """#42: the verdict is about an UNAUTHENTICATED caller. A probe that sent a token, or
    that skipped one of the two requests, would pass against an open route."""
    SEEN_RPC.clear()
    assert surface_contract.check(serve("healthy")) == []
    assert sorted(rpc for rpc, _ in SEEN_RPC) == ["initialize", "tools/call"], SEEN_RPC
    assert all(authorization == "" for _, authorization in SEEN_RPC), SEEN_RPC


def test_an_open_mcp_route_fails_the_exit_code_ci_reads(serve) -> None:  # type: ignore[no-untyped-def]
    """The release walk and the daily freshness run read only the exit code."""
    assert surface_contract.main([serve("mcp_open")]) == 1


def test_the_authenticated_check_needs_a_session_and_a_tool_list(serve, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """#1437 acceptance 3: with the environment token the walk lists the tools (200). A
    wrong token and a missing tool list each fail, and the token never reaches a message."""
    base = serve("healthy")
    assert surface_contract.check_authenticated(base, STUB_TOKEN) == []

    wrong = surface_contract.check_authenticated(base, "not-the-token")
    assert len(wrong) == 1 and "answered 401" in wrong[0] and "not-the-token" not in wrong[0], wrong

    empty = surface_contract.check_authenticated(serve("no_tool_list"), STUB_TOKEN)
    assert len(empty) == 1 and "without a tool list" in empty[0], empty

    monkeypatch.setenv(surface_contract.TOKEN_ENV, STUB_TOKEN)
    assert surface_contract.main(["--authenticated", base]) == 0
    monkeypatch.setenv(surface_contract.TOKEN_ENV, "not-the-token")
    assert surface_contract.main(["--authenticated", base]) == 1
    monkeypatch.delenv(surface_contract.TOKEN_ENV)
    assert surface_contract.main(["--authenticated", base]) == 2, "no token must not read as a pass"
