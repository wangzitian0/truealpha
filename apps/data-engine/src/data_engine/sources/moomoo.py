"""moomoo OpenAPI client — Phase -1 capability audit only (init.md Section 5:
"whether it has analyst ratings... all unverified, don't assume it does").

Prerequisite this module cannot satisfy itself: the moomoo OpenD gateway must
already be running and logged into a real moomoo account (2FA/app
confirmation — download from https://www.moomoo.com/download/OpenAPI). This
module only talks to OpenD over localhost/TCP; it never touches moomoo
credentials directly.

Every call goes through moomoo_ledger.gate() first (init.md Section 1 rule 6:
no module decides for itself whether to call). The gate is a defensive
throttle/audit trail, not enforcement of a real moomoo-side monthly quota —
moomoo's own docs only rate-limit these endpoints (bursts per 30s); see
init.md Section 5's 2026-07-10 correction.
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
    uid: str | None = None,
    num: int | None = None,
    next_key: str | None = None,
    caller: str = "probe_moomoo_capabilities",
):
    """Per-stock rating list, or (with uid set) one analyst/institution's own
    detail row(s) for this stock — per moomoo's docs this is the only way to
    reach an analyst's history rather than just who currently covers the
    stock. analyst_dimension=True asks for individual-analyst rows
    (rating_dimension_type=2) rather than institution-aggregated ones — this
    is the candidate for module 4's per-analyst backtesting, if the detail
    rows carry enough history to reconstruct a track record. num paginates
    the summary list (moomoo caps it at 20/page); uid switches the response
    from a summary list to one analyst/institution's detail."""
    dim = 2 if analyst_dimension else 1
    return _call(
        ctx,
        "get_research_rating_summary",
        caller,
        lambda c: c.get_research_rating_summary(code, rating_dimension_type=dim, uid=uid, num=num, next_key=next_key),
    )


def get_market_rating_changes(
    ctx, *, count: int = 10, page: str | None = None, caller: str = "probe_moomoo_capabilities"
):
    """US-market-wide rating changes (upgrade/downgrade/new) — a live feed,
    not a per-ticker history. count is capped at 20/page by the API itself;
    page is the pagination cursor moomoo returns alongside next_page/all_count
    for reaching older pages."""
    return _call(
        ctx,
        "get_rating_change",
        caller,
        lambda c: c.get_rating_change(moomoo.Market.US, count=count, page=page),
    )


def get_company_profile(ctx, code: str, *, caller: str = "probe_moomoo_capabilities"):
    """Company profile — a key/value table (columns: name, value, field_type),
    not one row per company. Candidate ground-truth for company metadata."""
    return _call(ctx, "get_company_profile", caller, lambda c: c.get_company_profile(code))


def get_financials_statements(
    ctx,
    code: str,
    *,
    statement_type: "moomoo.FinancialStatementsType" = None,
    financial_type: "moomoo.F10Type" = None,
    num: int = 50,
    next_key: str | None = None,
    caller: str = "probe_moomoo_capabilities",
):
    """One of income/balance-sheet/cash-flow/key-metrics (statement_type),
    quarterly+annual history in one call (financial_type default
    QuarterlyAnnual). Candidate for PEG / gross-profit-per-employee —
    moomoo's own field normalization may sidestep the raw SEC XBRL
    tag-inconsistency problem (see samples/README.md), needs cross-checking,
    not blind trust. Paginated (num 1-50/page, next_key cursor)."""
    return _call(
        ctx,
        "get_financials_statements",
        caller,
        lambda c: c.get_financials_statements(
            code, statement_type=statement_type, financial_type=financial_type, num=num, next_key=next_key
        ),
    )


def get_financials_revenue_breakdown(ctx, code: str, *, caller: str = "probe_moomoo_capabilities"):
    """Revenue breakdown by product/industry/region for the latest reported period."""
    return _call(ctx, "get_financials_revenue_breakdown", caller, lambda c: c.get_financials_revenue_breakdown(code))


def get_valuation_detail(
    ctx,
    code: str,
    *,
    valuation_type: "moomoo.ValuationType" = None,
    interval_type: "moomoo.ValuationIntervalType" = None,
    caller: str = "probe_moomoo_capabilities",
):
    """PE/PB/PS historical trend (with sector-average overlay), market and
    industry-peer distribution, and profit/revenue growth multiples —
    profit_growth_rate is the closest thing moomoo has to a ready-made PEG
    input (empty for valuation_type=PB)."""
    return _call(
        ctx,
        "get_valuation_detail",
        caller,
        lambda c: c.get_valuation_detail(code, valuation_type=valuation_type, interval_type=interval_type),
    )


def get_research_morningstar_report(ctx, code: str, *, caller: str = "probe_moomoo_capabilities"):
    """Morningstar star rating, fair value, economic moat / uncertainty /
    financial-health labels, bull/bear case text, full analyst narrative.
    Coverage isn't guaranteed for every ticker — Morningstar doesn't cover
    the whole market, expect gaps particularly for smaller/newer names."""
    return _call(ctx, "get_research_morningstar_report", caller, lambda c: c.get_research_morningstar_report(code))


def get_shareholders_overview(ctx, code: str, *, caller: str = "probe_moomoo_capabilities"):
    """Major holders + holder-type breakdown in one call (dict of 3 DataFrames:
    main_holder, holder_type, holding_period)."""
    return _call(ctx, "get_shareholders_overview", caller, lambda c: c.get_shareholders_overview(code))


def get_insider_trade_list(ctx, code: str, *, num: int = 20, caller: str = "probe_moomoo_capabilities"):
    """Recent insider (officer/director) trades for this stock, paginated."""
    return _call(ctx, "get_insider_trade_list", caller, lambda c: c.get_insider_trade_list(code, num=num))


def get_corporate_actions_dividends(ctx, code: str, *, caller: str = "probe_moomoo_capabilities"):
    """Dividend/distribution history for this stock."""
    return _call(ctx, "get_corporate_actions_dividends", caller, lambda c: c.get_corporate_actions_dividends(code))


def get_short_interest(ctx, code: str, *, num: int = 20, caller: str = "probe_moomoo_capabilities"):
    """Short-interest history. Returns (us_df, hk_df) — moomoo always returns
    both market slots regardless of which market the ticker is actually in;
    the other one is expected to come back empty."""
    return _call(ctx, "get_short_interest", caller, lambda c: c.get_short_interest(code, num=num))


def get_owner_plate(ctx, code_list: list[str], *, caller: str = "probe_moomoo_capabilities"):
    """Sector/industry-plate classification for one or more stocks in a single
    call — pass the whole universe at once rather than one call per ticker."""
    return _call(ctx, "get_owner_plate", caller, lambda c: c.get_owner_plate(code_list))
