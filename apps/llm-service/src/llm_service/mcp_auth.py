"""The credential check in front of the MCP route (#42, #728, init.md rule 21).

Every non-local MCP endpoint requires authentication. Until this gate, any caller on the
internet reached every tool, and the server built a service identity for it. #728 closed
without a deployed probe, and production stayed open. The probe is now
`truealpha_tools.surface_contract`: an unauthenticated `initialize` and an unauthenticated
`tools/call` must both answer 401, on staging and production, on every release walk and
every day.

The gate is plain ASGI, so a streamed MCP response passes through without a buffer. It
reads the token on each request, so a test (or a later rotation) changes it without a
new app. It fails closed: no configured token means no request passes.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Callable

from pydantic import SecretStr
from starlette.types import ASGIApp, Receive, Scope, Send

#: RFC 6750 section 3: the challenge a client reads to know which credential to send.
CHALLENGE = 'Bearer realm="truealpha-mcp"'


def presented_bearer(scope: Scope) -> str | None:
    """The token of an `Authorization: Bearer <token>` header, or None.

    The scheme name is case-insensitive (RFC 7235 section 2.1). A header with another
    scheme, no token, or a second Authorization header is not a bearer credential.
    """
    values = [value for name, value in scope.get("headers", ()) if name.lower() == b"authorization"]
    if len(values) != 1:
        return None
    scheme, _, token = values[0].decode("latin-1").strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def token_matches(presented: str | None, expected: SecretStr | None) -> bool:
    """True only when a token is configured and the presented one is equal to it.

    `hmac.compare_digest` takes the same time for every wrong token of a given length, so
    the answer time does not show how many leading characters matched.
    """
    if presented is None or expected is None:
        return False
    secret = expected.get_secret_value()
    if not secret:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), secret.encode("utf-8"))


class BearerTokenGate:
    """Pass a request to `app` only when it presents the configured bearer token."""

    def __init__(self, app: ASGIApp, *, expected_token: Callable[[], SecretStr | None]) -> None:
        self._app = app
        self._expected_token = expected_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Lifespan and other non-HTTP scopes carry no request; FastAPI drives the MCP
        # session manager from its own lifespan, not through this mount.
        if scope["type"] != "http" or token_matches(presented_bearer(scope), self._expected_token()):
            await self._app(scope, receive, send)
            return
        body = json.dumps(
            {
                "error": "invalid_token",
                "error_description": "The MCP route requires an Authorization: Bearer token.",
            }
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"www-authenticate", CHALLENGE.encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
