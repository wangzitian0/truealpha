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

from data_engine.jsonable import to_jsonable
from data_engine.sources import moomoo as mm
from data_engine.sources.moomoo_ledger import BudgetExceededError, calls_this_month

TICKERS = ["DDOG", "NICE", "SHOP", "DUOL"]
OUT_DIR = Path(__file__).resolve().parents[1] / "samples" / "moomoo"
CALLER = "capture_fundamental_samples"


def capture_ticker(ctx, ticker: str) -> dict:
    code = f"US.{ticker}"
    results: dict[str, object] = {}
    for name, call in mm.FUNDAMENTAL_ENDPOINTS.items():
        try:
            results[name] = {"ok": True, "data": to_jsonable(call(ctx, code, CALLER))}
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
            out_path.write_text(json.dumps(to_jsonable(owner_plate), indent=2, ensure_ascii=False))
            print(f"\nowner_plate (all tickers batched): ok -> {out_path}")
        except mm.MoomooConnectionError as e:
            print(f"\nowner_plate: FAILED — {e}")

    print(f"\nmoomoo calls used this month after this run: {calls_this_month()}")


if __name__ == "__main__":
    main()
