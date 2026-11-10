# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import hashlib; import hmac; import json; from collections.abc import Callable; from pydantic import SecretStr; from starlette.types import ASGIApp, Receive, Scope, Send; CHALLENGE = 'Bearer realm="truealpha-mcp"'
def presented_bearer(scope: Scope) -> str | None:
    values = [v for k, v in scope.get("headers", ()) if k.lower() == b"authorization"]
    if len(values) != 1: return None
    scheme, _, token = values[0].decode("latin-1").strip().partition(" "); token = token.strip(); return token if scheme.lower() == "bearer" and token else None
def token_matches(presented: str | None, expected: SecretStr | None) -> bool:
    if presented is None or expected is None: return False
    secret = expected.get_secret_value()
    if not secret: return False
    return hmac.compare_digest( hashlib.sha256(presented.encode("utf-8")).digest(), hashlib.sha256(secret.encode("utf-8")).digest() )
class BearerTokenGate:
    def __init__(self, app: ASGIApp, *, expected_token: Callable[[], SecretStr | None]) -> None: self._app = app; self._expected_token = expected_token
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan" or token_matches(presented_bearer(scope), self._expected_token()): await self._app(scope, receive, send); return
        if scope["type"] == "websocket": await send({"type": "websocket.close", "code": 1008}); return
        body = json.dumps( {"error": "invalid_token", "error_description": "The MCP route requires an Authorization: Bearer token."} ).encode("utf-8"); await send( { "type": "http.response.start", "status": 401, "headers": [ (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode("ascii")), (b"www-authenticate", CHALLENGE.encode("ascii")), ], } ); await send({"type": "http.response.body", "body": body})
