# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import argparse; import json; import os; import sys; import urllib.error; import urllib.request; from collections.abc import Sequence; from dataclasses import dataclass; from urllib.parse import urlparse; TIMEOUT = 25.0; USER_AGENT = "truealpha-surface-contract (+https://github.com/wangzitian0/truealpha)"
@dataclass(frozen=True)
class Response: status: int; location: str; body: bytes; challenge: str = ""; session: str = ""
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *arguments: object, **keywords: object) -> None: return None
def fetch( url: str, method: str = "GET", payload: dict[str, object] | None = None, extra_headers: dict[str, str] | None = None, ) -> Response:
    opener = urllib.request.build_opener(_NoRedirect); headers = {"User-Agent": USER_AGENT, **(extra_headers or {})}; data = None
    if payload is not None: headers["Accept"] = "application/json, text/event-stream"; headers["Content-Type"] = "application/json"; data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request( url, data=data, method=method, headers=headers )
    try:
        with opener.open(request, timeout=TIMEOUT) as answer: return Response( answer.status, answer.headers.get("Location", ""), answer.read(), answer.headers.get("WWW-Authenticate", ""), answer.headers.get("Mcp-Session-Id", ""), )
    except urllib.error.HTTPError as error: return Response( error.code, error.headers.get("Location", ""), error.read(), error.headers.get("WWW-Authenticate", ""), )
    except OSError as error: reason = getattr(error, "reason", None) or error; raise SurfaceUnreachable(f"{method} {url} did not answer: {reason}") from None
class SurfaceUnreachable(Exception): 'The surface under test did not answer at all — reported as the first failed\n    property, never as a traceback.'
def judge_redirect(source_url: str, base_scheme: str, status: int, location: str) -> list[str]:
    if not 300 <= status < 400: return [f"GET {source_url} answered {status}, not a redirect"]
    target = urlparse(location); failures: list[str] = []
    if base_scheme == "https" and target.scheme == "http": failures.append( f'GET {source_url} redirects to {location} — the scheme is downgraded to http, and a client follows it (init.md principle 21; the v0.0.26 defect)' )
    if not target.path.startswith("/api/"): failures.append( f'GET {source_url} redirects to {location} — the /api prefix is dropped, so the client lands on app-web instead of the MCP endpoint (v0.0.26)' )
    return failures
UNAUTHENTICATED_MCP_REQUESTS: dict[str, dict[str, object]] = { "initialize": { "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": { "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "truealpha-surface-contract", "version": "1"}, }, }, "tools/call": { "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "topt_gppe", "arguments": {}}, }, }
def judge_credential(endpoint: str, rpc_method: str, answer: Response) -> list[str]:
    if answer.status != 401:
        return [ f'POST {endpoint} {rpc_method} without a credential answered {answer.status}, not 401 — the MCP route serves an unauthenticated caller (init.md rule 21; #728 closed without this probe and production stayed open)' ]
    if not answer.challenge.lower().startswith("bearer"): return [ f"POST {endpoint} {rpc_method} answered 401 without a Bearer challenge (WWW-Authenticate: {answer.challenge or '<none>'}), so a client cannot tell which credential to send" ]
    return []
def check(base: str) -> list[str]:
    try: return _check(base)
    except SurfaceUnreachable as unreachable: return [f"the surface is not serving: {unreachable}"]
def _check(base: str) -> list[str]:
    base = base.rstrip("/"); scheme = urlparse(base).scheme; failures: list[str] = []; alive = fetch(f"{base}/api/health")
    if alive.status != 200: return [ f'GET {base}/api/health answered {alive.status}, not 200 — the surface is not serving, so its routing shape cannot be judged (is the deploy live? tools/health_check.py)' ]
    slashless = f"{base}/api/mcp"; redirect = fetch(slashless); failures.extend(judge_redirect(slashless, scheme, redirect.status, redirect.location)); post = fetch(slashless, method="POST")
    if post.status == 405: failures.append( f'POST {slashless} answered 405 — the redirect handler covers GET only again, which is exactly the v0.0.30 defect (staging 405 while production stayed 200)' )
    endpoint = f"{base}/api/mcp/"; served = fetch(endpoint)
    if served.status == 404: failures.append( f'GET {endpoint} answered 404 — the redirect points at nothing, which is worse than the downgrade it replaced (the v0.0.28 root_path defect)' )
    for rpc_method, payload in UNAUTHENTICATED_MCP_REQUESTS.items(): failures.extend(judge_credential(endpoint, rpc_method, fetch(endpoint, method="POST", payload=payload)))
    return failures
TOKEN_ENV = "TA_MCP_TOKEN"
def check_authenticated(base: str, token: str) -> list[str]:
    try:
        endpoint = f"{base.rstrip('/')}/api/mcp/"; bearer = {"Authorization": f"Bearer {token}"}; opened = fetch( endpoint, method="POST", payload=UNAUTHENTICATED_MCP_REQUESTS["initialize"], extra_headers=bearer )
        if opened.status != 200 or not opened.session: return [ f"POST {endpoint} initialize with the environment token answered {opened.status} (session {('set' if opened.session else 'missing')}), not 200 with a session — the token in the walk secret and the deployed MCP_BEARER_TOKEN differ, or the route is down" ]
        session = {**bearer, "Mcp-Session-Id": opened.session}; fetch(endpoint, method="POST", payload={"jsonrpc": "2.0", "method": "notifications/initialized"}, extra_headers=session); listed = fetch( endpoint, method="POST", payload={"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, extra_headers=session )
    except SurfaceUnreachable as unreachable: return [f"the surface is not serving: {unreachable}"]
    if listed.status != 200 or b'"tools"' not in listed.body: return [f"POST {endpoint} tools/list with the environment token answered {listed.status} without a tool list"]
    return []
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assert a deployed surface's shape over real HTTPS."); parser.add_argument("base_url", help="e.g. https://truealpha.club"); parser.add_argument( "--authenticated", action="store_true", help=f"call tools/list with the token in ${TOKEN_ENV} instead of the unauthenticated checks", ); arguments = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if urlparse(arguments.base_url).scheme not in {"http", "https"}: print(f"surface_contract: {arguments.base_url} is not an http(s) URL", file=sys.stderr); return 2
    if arguments.authenticated:
        token = os.environ.get(TOKEN_ENV, "")
        if not token: print(f"surface_contract: --authenticated needs ${TOKEN_ENV}", file=sys.stderr); return 2
        failures = check_authenticated(arguments.base_url, token)
    else: failures = check(arguments.base_url)
    for failure in failures: print(f"::error::{failure}", file=sys.stderr)
    if failures: plural = "properties" if len(failures) > 1 else "property"; print(f"surface_contract: {len(failures)} {plural} violated on {arguments.base_url}", file=sys.stderr); return 1
    print(f"surface_contract: the deployed surface holds on {arguments.base_url}"); return 0
if __name__ == "__main__": raise SystemExit(main())
