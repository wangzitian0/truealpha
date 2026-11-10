# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
import json; import logging; import socket; from collections.abc import Iterator; from contextlib import contextmanager; import moomoo; from data_engine.config import settings; from data_engine.datahub.fetch_key import FetchKey; from data_engine.sources.gateway import record_call; from data_engine.sources.moomoo_ledger import gate, normalize_stock_uri, stored_history_kl_quota; logger = logging.getLogger(__name__)
class MoomooConnectionError(Exception): pass
def _preflight_check(host: str, port: int, timeout: float = 3.0) -> None:
    try:
        with socket.create_connection((host, port), timeout=timeout): return
    except OSError as e: raise MoomooConnectionError( f"OpenD not reachable at {host}:{port} ({e}). OpenD must already be running and " "logged into a real moomoo account before this module can connect — see " "https://www.moomoo.com/download/OpenAPI. This module cannot start or log into it." ) from e
@contextmanager
def connect() -> Iterator["moomoo.OpenQuoteContext"]:
    if not settings.moomoo_opend_host or not settings.moomoo_opend_port: raise RuntimeError("MOOMOO_OPEND_HOST / MOOMOO_OPEND_PORT are not configured")
    _preflight_check(settings.moomoo_opend_host, settings.moomoo_opend_port); ctx = moomoo.OpenQuoteContext(host=settings.moomoo_opend_host, port=settings.moomoo_opend_port)
    try: yield ctx
    finally: ctx.close()
def canonical_moomoo_bytes(payload) -> bytes: from data_engine.jsonable import to_jsonable; return json.dumps(to_jsonable(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
def _call(ctx, endpoint: str, caller: str, fn, *, identity: FetchKey, request_uri: str | None = None):
    gate(endpoint, caller, request_uri=request_uri)
    with record_call( "moomoo", endpoint, identity=identity, caller=caller, request_uri=normalize_stock_uri(request_uri) ) as call:
        try: result = fn(ctx)
        except Exception as e: call.fail(f"{type(e).__name__}: {e}"); raise MoomooConnectionError(f"{endpoint} raised: {e}") from e
        ret, rest = result[0], result[1:]; ok = ret == moomoo.RET_OK; raw: bytes | None = None
        if ok:
            try: raw = canonical_moomoo_bytes(rest[0] if len(rest) == 1 else rest)
            except Exception: raw = None
        call.observe(status_code=int(ret) if isinstance(ret, int) else None, body=raw)
        if not ok: call.fail(str(rest[0] if rest else "unknown error"))
    if not ok: raise MoomooConnectionError(f"{endpoint} failed: {rest[0] if rest else 'unknown error'}")
    return rest[0] if len(rest) == 1 else rest
def get_analyst_consensus(ctx, code: str, *, caller: str = "probe_moomoo_capabilities", identity: FetchKey): return _call( ctx, "get_research_analyst_consensus", caller, lambda c: c.get_research_analyst_consensus(code), identity=identity, )
def get_rating_summary( ctx, code: str, *, analyst_dimension: bool = True, uid: str | None = None, num: int | None = None, next_key: str | None = None, caller: str = "probe_moomoo_capabilities", identity: FetchKey, ): dim = 2 if analyst_dimension else 1; return _call( ctx, "get_research_rating_summary", caller, lambda c: c.get_research_rating_summary(code, rating_dimension_type=dim, uid=uid, num=num, next_key=next_key), identity=identity, )
def get_market_rating_changes( ctx, *, count: int = 10, page: str | None = None, caller: str = "probe_moomoo_capabilities", identity: FetchKey ): return _call( ctx, "get_rating_change", caller, lambda c: c.get_rating_change(moomoo.Market.US, count=count, page=page), identity=identity, )
def get_company_profile(ctx, code: str, *, caller: str = "probe_moomoo_capabilities", identity: FetchKey): return _call(ctx, "get_company_profile", caller, lambda c: c.get_company_profile(code), identity=identity)
def get_financials_statements( ctx, code: str, *, statement_type: "moomoo.FinancialStatementsType" = None, financial_type: "moomoo.F10Type" = None, num: int = 50, next_key: str | None = None, caller: str = "probe_moomoo_capabilities", identity: FetchKey, ): return _call( ctx, "get_financials_statements", caller, lambda c: c.get_financials_statements( code, statement_type=statement_type, financial_type=financial_type, num=num, next_key=next_key ), identity=identity, )
def get_financials_revenue_breakdown(ctx, code: str, *, caller: str = "probe_moomoo_capabilities", identity: FetchKey): return _call( ctx, "get_financials_revenue_breakdown", caller, lambda c: c.get_financials_revenue_breakdown(code), identity=identity, )
def get_valuation_detail( ctx, code: str, *, valuation_type: "moomoo.ValuationType" = None, interval_type: "moomoo.ValuationIntervalType" = None, caller: str = "probe_moomoo_capabilities", identity: FetchKey, ): return _call( ctx, "get_valuation_detail", caller, lambda c: c.get_valuation_detail(code, valuation_type=valuation_type, interval_type=interval_type), identity=identity, )
def get_research_morningstar_report(ctx, code: str, *, caller: str = "probe_moomoo_capabilities", identity: FetchKey): return _call( ctx, "get_research_morningstar_report", caller, lambda c: c.get_research_morningstar_report(code), identity=identity, )
def get_shareholders_overview(ctx, code: str, *, caller: str = "probe_moomoo_capabilities", identity: FetchKey): return _call( ctx, "get_shareholders_overview", caller, lambda c: c.get_shareholders_overview(code), identity=identity )
def get_insider_trade_list( ctx, code: str, *, num: int = 20, caller: str = "probe_moomoo_capabilities", identity: FetchKey ): return _call( ctx, "get_insider_trade_list", caller, lambda c: c.get_insider_trade_list(code, num=num), identity=identity )
def get_corporate_actions_dividends(ctx, code: str, *, caller: str = "probe_moomoo_capabilities", identity: FetchKey): return _call( ctx, "get_corporate_actions_dividends", caller, lambda c: c.get_corporate_actions_dividends(code), identity=identity, )
def get_short_interest(ctx, code: str, *, num: int = 20, caller: str = "probe_moomoo_capabilities", identity: FetchKey): return _call(ctx, "get_short_interest", caller, lambda c: c.get_short_interest(code, num=num), identity=identity)
def get_owner_plate(ctx, code_list: list[str], *, caller: str = "probe_moomoo_capabilities", identity: FetchKey): return _call(ctx, "get_owner_plate", caller, lambda c: c.get_owner_plate(code_list), identity=identity)
def get_history_kline( ctx, code: str, *, start: str, end: str, max_count: int = 30, caller: str = "moomoo_origin", identity: FetchKey ):
    page_req_key: list = [None]
    def _invoke(c):
        res = c.request_history_kline( code, start=start, end=end, ktype=moomoo.KLType.K_DAY, autype=moomoo.AuType.NONE, fields=[moomoo.KL_FIELD.ALL], max_count=max_count, extended_time=False, )
        if isinstance(res, tuple) and len(res) >= 3: page_req_key[0] = res[2]; return (res[0], res[1])
        return res
    if stored_history_kl_quota() is None and hasattr(ctx, "get_history_kl_quota"):
        try: get_history_kl_quota( ctx, caller=caller, identity=FetchKey("moomoo", "get_history_kl_quota", f"kline_quota:{code}", identity.as_of), )
        except Exception: logger.warning("Could not refresh moomoo historical kline quota reading", exc_info=True)
    bars = _call(ctx, "request_history_kline", caller, _invoke, request_uri=f"moomoo://{code}", identity=identity); return bars, page_req_key[0]
def get_history_kl_quota(ctx, *, caller: str = "moomoo_origin", identity: FetchKey):
    res = _call( ctx, "get_history_kl_quota", caller, lambda c: c.get_history_kl_quota(get_detail=False), identity=identity )
    if isinstance(res, (tuple, list)) and len(res) >= 2:
        try: from data_engine.sources.moomoo_ledger import store_history_kl_quota; store_history_kl_quota(int(res[0]) + int(res[1]))
        except Exception: logger.warning("Failed to store moomoo get_history_kl_quota reading", exc_info=True)
    return res
FS_INCOME, FS_BALANCE_SHEET, FS_CASH_FLOW, FS_MAIN_INDEX = 1, 2, 3, 4; VALUATION_PE = 1; INTERVAL_YEAR10 = 7; FUNDAMENTAL_ENDPOINTS = { "company_profile": lambda ctx, code, caller, identity: get_company_profile( ctx, code, caller=caller, identity=identity ), "financials_income": lambda ctx, code, caller, identity: get_financials_statements( ctx, code, statement_type=FS_INCOME, caller=caller, identity=identity ), "financials_balance_sheet": lambda ctx, code, caller, identity: get_financials_statements( ctx, code, statement_type=FS_BALANCE_SHEET, caller=caller, identity=identity ), "financials_cash_flow": lambda ctx, code, caller, identity: get_financials_statements( ctx, code, statement_type=FS_CASH_FLOW, caller=caller, identity=identity ), "financials_main_index": lambda ctx, code, caller, identity: get_financials_statements( ctx, code, statement_type=FS_MAIN_INDEX, caller=caller, identity=identity ), "financials_revenue_breakdown": lambda ctx, code, caller, identity: get_financials_revenue_breakdown( ctx, code, caller=caller, identity=identity ), "valuation_pe": lambda ctx, code, caller, identity: get_valuation_detail( ctx, code, valuation_type=VALUATION_PE, interval_type=INTERVAL_YEAR10, caller=caller, identity=identity ), "analyst_consensus": lambda ctx, code, caller, identity: get_analyst_consensus( ctx, code, caller=caller, identity=identity ), "rating_summary": lambda ctx, code, caller, identity: get_rating_summary( ctx, code, analyst_dimension=True, num=20, caller=caller, identity=identity ), "morningstar_report": lambda ctx, code, caller, identity: get_research_morningstar_report( ctx, code, caller=caller, identity=identity ), "shareholders_overview": lambda ctx, code, caller, identity: get_shareholders_overview( ctx, code, caller=caller, identity=identity ), "insider_trades": lambda ctx, code, caller, identity: get_insider_trade_list( ctx, code, num=20, caller=caller, identity=identity ), "dividends": lambda ctx, code, caller, identity: get_corporate_actions_dividends( ctx, code, caller=caller, identity=identity ), "short_interest": lambda ctx, code, caller, identity: get_short_interest( ctx, code, num=20, caller=caller, identity=identity ), }; CORE_ENDPOINTS = ( "company_profile", "financials_income", "financials_balance_sheet", "financials_cash_flow", "financials_main_index", "financials_revenue_breakdown", "valuation_pe", "analyst_consensus", "rating_summary", )
