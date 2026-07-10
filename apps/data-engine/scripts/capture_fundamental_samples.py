"""Phase -1 sample capture: pull one pass of moomoo's per-ticker fundamental
data (financials, valuation, ownership, morningstar, ratings) for the test
universe and persist it to samples/moomoo/, alongside the existing
sec/filings/nport/prices fixtures (see samples/README.md).

Deliberately NOT a repeated-trial-and-error probe: every endpoint here was
picked by reading moomoo's proto definitions and SDK source first (see the
docstrings in data_engine/sources/moomoo.py) rather than discovering them by
trial and error against the live API. One call per endpoint per ticker, no
pagination beyond the single default page — this is a first-pass capability
snapshot, not an attempt to pull full history (moomoo's fundamental/quote
endpoints are rate-limited, not monthly-quota-capped — see init.md Section 5 —
so a deeper pull is a matter of choice, not a hard ceiling).

REQUIRES the moomoo OpenD gateway already running and logged in (see
data_engine/sources/moomoo.py).

Usage:
    uv run --package truealpha-data-engine python apps/data-engine/scripts/capture_fundamental_samples.py
"""

import json
from pathlib import Path

import pandas as pd
from data_engine.sources import moomoo as mm
from data_engine.sources.moomoo_ledger import BudgetExceededError, calls_this_month

TICKERS = ["DDOG", "NICE", "SHOP", "DUOL"]
OUT_DIR = Path(__file__).resolve().parents[1] / "samples" / "moomoo"
CALLER = "capture_fundamental_samples"

# moomoo's Python SDK doesn't expose these as enum classes (unlike e.g.
# moomoo.Market) — it takes the raw Qot_Common.proto enum ints directly.
FS_INCOME, FS_BALANCE_SHEET, FS_CASH_FLOW, FS_MAIN_INDEX = 1, 2, 3, 4  # FinancialStatementsType
VALUATION_PE = 1  # ValuationType
INTERVAL_YEAR10 = 7  # ValuationIntervalType


def _to_jsonable(obj):
    """moomoo's SDK return shapes aren't uniform (plain dict, DataFrame, or a
    tuple of DataFrames) — normalize whatever comes back into something
    json.dump can write without guessing per-endpoint ahead of time."""
    if isinstance(obj, pd.DataFrame):
        return json.loads(obj.to_json(orient="records"))
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, tuple | list):
        return [_to_jsonable(v) for v in obj]
    return obj


def capture_ticker(ctx, ticker: str) -> dict:
    code = f"US.{ticker}"
    endpoints: dict[str, object] = {
        "company_profile": lambda: mm.get_company_profile(ctx, code, caller=CALLER),
        "financials_income": lambda: mm.get_financials_statements(ctx, code, statement_type=FS_INCOME, caller=CALLER),
        "financials_balance_sheet": lambda: mm.get_financials_statements(
            ctx, code, statement_type=FS_BALANCE_SHEET, caller=CALLER
        ),
        "financials_cash_flow": lambda: mm.get_financials_statements(
            ctx, code, statement_type=FS_CASH_FLOW, caller=CALLER
        ),
        "financials_main_index": lambda: mm.get_financials_statements(
            ctx, code, statement_type=FS_MAIN_INDEX, caller=CALLER
        ),
        "financials_revenue_breakdown": lambda: mm.get_financials_revenue_breakdown(ctx, code, caller=CALLER),
        "valuation_pe": lambda: mm.get_valuation_detail(
            ctx, code, valuation_type=VALUATION_PE, interval_type=INTERVAL_YEAR10, caller=CALLER
        ),
        "analyst_consensus": lambda: mm.get_analyst_consensus(ctx, code, caller=CALLER),
        "rating_summary": lambda: mm.get_rating_summary(ctx, code, analyst_dimension=True, num=20, caller=CALLER),
        "morningstar_report": lambda: mm.get_research_morningstar_report(ctx, code, caller=CALLER),
        "shareholders_overview": lambda: mm.get_shareholders_overview(ctx, code, caller=CALLER),
        "insider_trades": lambda: mm.get_insider_trade_list(ctx, code, num=20, caller=CALLER),
        "dividends": lambda: mm.get_corporate_actions_dividends(ctx, code, caller=CALLER),
        "short_interest": lambda: mm.get_short_interest(ctx, code, num=20, caller=CALLER),
    }

    results: dict[str, object] = {}
    for name, call in endpoints.items():
        try:
            results[name] = {"ok": True, "data": _to_jsonable(call())}
            print(f"  {ticker}.{name}: ok")
        except mm.MoomooConnectionError as e:
            results[name] = {"ok": False, "error": str(e)}
            print(f"  {ticker}.{name}: FAILED — {e}")
        except BudgetExceededError:
            raise
    return results


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"moomoo calls used this month before this run: {calls_this_month()}")

    with mm.connect() as ctx:
        for ticker in TICKERS:
            print(f"\n{ticker}")
            data = capture_ticker(ctx, ticker)
            out_path = OUT_DIR / f"{ticker}.json"
            out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            print(f"  -> {out_path}")

        try:
            codes = [f"US.{t}" for t in TICKERS]
            owner_plate = mm.get_owner_plate(ctx, codes, caller=CALLER)
            out_path = OUT_DIR / "owner_plate.json"
            out_path.write_text(json.dumps(_to_jsonable(owner_plate), indent=2, ensure_ascii=False))
            print(f"\nowner_plate (all tickers batched): ok -> {out_path}")
        except mm.MoomooConnectionError as e:
            print(f"\nowner_plate: FAILED — {e}")

    print(f"\nmoomoo calls used this month after this run: {calls_this_month()}")


if __name__ == "__main__":
    main()
