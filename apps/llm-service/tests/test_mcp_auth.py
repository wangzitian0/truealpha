"""The MCP credential gate, alone (#42).

`test_health.py` asserts the gate through the deployed app, in the one lifespan the MCP
session manager allows. This file drives `BearerTokenGate` around a recording app, so
each refusal case is one test and none needs the session manager.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from llm_service.mcp_auth import CHALLENGE, BearerTokenGate, presented_bearer, token_matches
from pydantic import SecretStr
from starlette.types import Receive, Scope, Send

TOKEN = "test-mcp-token"


class _RecordingApp:
    """Answers 200 and counts the requests that reached it."""

    def __init__(self) -> None:
        self.reached = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.reached += 1
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"2")]})
        await send({"type": "http.response.body", "body": b"ok"})


def _client(expected: Callable[[], SecretStr | None]) -> tuple[TestClient, _RecordingApp]:
    inner = _RecordingApp()
    return TestClient(BearerTokenGate(inner, expected_token=expected)), inner


def test_the_configured_token_passes() -> None:
    client, inner = _client(lambda: SecretStr(TOKEN))
    answer = client.post("/", headers={"Authorization": f"Bearer {TOKEN}"})
    assert answer.status_code == 200
    assert inner.reached == 1


def test_the_scheme_name_is_case_insensitive() -> None:
    client, inner = _client(lambda: SecretStr(TOKEN))
    assert client.post("/", headers={"Authorization": f"bearer {TOKEN}"}).status_code == 200
    assert inner.reached == 1


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": f"Bearer {TOKEN}x"},
        {"Authorization": f"Bearer {TOKEN[:-1]}"},
    ],
    ids=["none", "wrong", "other-scheme", "scheme-only", "empty-token", "longer", "prefix"],
)
def test_every_other_credential_is_refused_before_the_app(headers: dict[str, str]) -> None:
    client, inner = _client(lambda: SecretStr(TOKEN))
    answer = client.post("/", headers=headers)
    assert answer.status_code == 401
    assert answer.headers["www-authenticate"] == CHALLENGE
    assert answer.json()["error"] == "invalid_token"
    assert inner.reached == 0, "a refused request reached the MCP app"


@pytest.mark.parametrize("configured", [None, SecretStr("")], ids=["unset", "empty"])
def test_an_unconfigured_token_closes_the_route(configured: SecretStr | None) -> None:
    """Fail closed: a deployment that did not receive MCP_BEARER_TOKEN serves nobody. An
    empty Authorization token must not match an empty configured token."""
    client, inner = _client(lambda: configured)
    for headers in ({}, {"Authorization": "Bearer "}, {"Authorization": f"Bearer {TOKEN}"}):
        assert client.post("/", headers=headers).status_code == 401
    assert inner.reached == 0


def test_the_token_is_read_on_each_request() -> None:
    """A rotated token applies without a new app object."""
    current = {"token": SecretStr("first")}
    client, inner = _client(lambda: current["token"])
    assert client.post("/", headers={"Authorization": "Bearer first"}).status_code == 200
    current["token"] = SecretStr("second")
    assert client.post("/", headers={"Authorization": "Bearer first"}).status_code == 401
    assert client.post("/", headers={"Authorization": "Bearer second"}).status_code == 200
    assert inner.reached == 2


def test_two_authorization_headers_are_not_a_credential() -> None:
    scope = {"type": "http", "headers": [(b"authorization", f"Bearer {TOKEN}".encode())] * 2}
    assert presented_bearer(scope) is None


def test_the_comparison_needs_both_sides() -> None:
    assert token_matches(TOKEN, SecretStr(TOKEN)) is True
    assert token_matches(None, SecretStr(TOKEN)) is False
    assert token_matches(TOKEN, None) is False
    assert token_matches("", SecretStr("")) is False
