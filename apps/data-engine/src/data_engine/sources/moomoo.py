"""moomoo OpenAPI client — Phase -1 capability audit only (init.md Section 5:
"whether it has analyst ratings... all unverified, don't assume it does").

Prerequisite this module cannot satisfy itself: the moomoo OpenD gateway must
already be running and logged into a real moomoo account (2FA/app
confirmation — download from https://www.moomoo.com/download/OpenAPI). This
module only talks to OpenD over localhost/TCP; it never touches moomoo
credentials directly.

Every call goes through moomoo_ledger.gate() first (init.md Section 1 rule 6:
2,000 calls/month, no module decides for itself whether to call).
"""

import socket
from collections.abc import Iterator
from contextlib import contextmanager

import moomoo

from data_engine.config import settings
from data_engine.sources.moomoo_ledger import gate, record


class MoomooConnectionError(Exception):
    pass


def _preflight_check(host: str, port: int, timeout: float = 3.0) -> None:
    """Raise immediately if nothing is listening at (host, port).

    Required because moomoo.OpenQuoteContext's constructor has no connect
    timeout of its own: on failure it retries internally every 6s forever
    (_auto_reconnect=True, no bounded retry count) rather than raising — so
    without this check, constructing it against a down/not-yet-logged-in
    OpenD hangs indefinitely instead of failing fast.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return
    except OSError as e:
        raise MoomooConnectionError(
            f"OpenD not reachable at {host}:{port} ({e}). OpenD must already be running and "
            "logged into a real moomoo account before this module can connect — see "
            "https://www.moomoo.com/download/OpenAPI. This module cannot start or log into it."
        ) from e


@contextmanager
def connect() -> Iterator["moomoo.OpenQuoteContext"]:
    _preflight_check(settings.moomoo_opend_host, settings.moomoo_opend_port)
    ctx = moomoo.OpenQuoteContext(host=settings.moomoo_opend_host, port=settings.moomoo_opend_port)
    try:
        yield ctx
    finally:
        ctx.close()


def _call(ctx, endpoint: str, caller: str, fn):
    """Gate, call, record — the one path every moomoo call must go through.

    moomoo's SDK return shape isn't uniform: most calls return (ret, data),
    but get_rating_change returns (ret, data, next_page, all_count). ret is
    always first; everything after it is returned to the caller as-is (a
    single value for a 2-tuple, a tuple of the rest otherwise).

    record() must run even if fn(ctx) raises: a request can reach moomoo's
    server (spending real quota) and still raise locally (timeout, a
    malformed/unexpected response) — if we skip recording on exception, the
    local ledger undercounts and the gate stops protecting the real budget.
    Re-raised as MoomooConnectionError (chained, original still inspectable
    via __cause__) so every caller-facing failure mode is one exception type.
    """
    gate(endpoint, caller)
    try:
        result = fn(ctx)
    except Exception as e:
        record(endpoint, caller, ok=False)
        raise MoomooConnectionError(f"{endpoint} raised: {e}") from e
    ret, rest = result[0], result[1:]
    ok = ret == moomoo.RET_OK
    record(endpoint, caller, ok)
    if not ok:
        raise MoomooConnectionError(f"{endpoint} failed: {rest[0] if rest else 'unknown error'}")
    return rest[0] if len(rest) == 1 else rest


def get_analyst_consensus(ctx, code: str, *, caller: str = "probe_moomoo_capabilities"):
    """Per-stock analyst consensus snapshot. code e.g. 'US.DDOG'."""
    return _call(ctx, "get_research_analyst_consensus", caller, lambda c: c.get_research_analyst_consensus(code))


def get_rating_summary(
    ctx,
    code: str,
    *,
    analyst_dimension: bool = True,
    caller: str = "probe_moomoo_capabilities",
):
    """Per-stock rating list. analyst_dimension=True asks for individual-analyst
    rows (rating_dimension_type=2) rather than institution-aggregated ones —
    this is the candidate for module 4's per-analyst backtesting, if the rows
    carry enough history to reconstruct a track record."""
    dim = 2 if analyst_dimension else 1
    return _call(
        ctx,
        "get_research_rating_summary",
        caller,
        lambda c: c.get_research_rating_summary(code, rating_dimension_type=dim),
    )


def get_market_rating_changes(ctx, *, count: int = 10, caller: str = "probe_moomoo_capabilities"):
    """Recent US-market-wide rating changes (upgrade/downgrade/new) — a live
    feed, not a per-ticker history. count is capped at 20 by the API itself."""
    return _call(
        ctx,
        "get_rating_change",
        caller,
        lambda c: c.get_rating_change(moomoo.Market.US, count=count),
    )
