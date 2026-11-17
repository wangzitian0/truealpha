"""Probe moomoo endpoints on non-US listings."""

import json
import sys
from pathlib import Path

from data_engine import jsonable
from data_engine.datahub.fetch_key import FetchKey
from data_engine.sources import gateway
from data_engine.sources import moomoo as mm
from data_engine.sources.moomoo_ledger import BudgetExceededError, calls_this_month

DEFAULT_CODES = ["HK.00700", "SH.600519"]
OUT_DIR = Path(__file__).resolve().parents[1] / "samples" / "moomoo"
CALLER = "probe_moomoo_nonus"


def main() -> None:
    codes = sys.argv[1:] or DEFAULT_CODES
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"moomoo calls used this month before this run: {calls_this_month()}")

    with mm.connect() as ctx:
        for code in codes:
            print(f"\n{code}")
            results: dict[str, object] = {}
            for name, call in mm.FUNDAMENTAL_ENDPOINTS.items():
                try:
                    identity = FetchKey("moomoo", name, f"symbol:{code}", gateway.utc_day())
                    results[name] = {"ok": True, "data": jsonable.to_jsonable(call(ctx, code, CALLER, identity))}
                    print(f"  {code}.{name}: ok")
                except mm.MoomooConnectionError as e:
                    results[name] = {"ok": False, "error": str(e)}
                    print(f"  {code}.{name}: FAILED — {e}")
                except BudgetExceededError:
                    raise
            out_path = OUT_DIR / f"{code.replace('.', '_')}.json"
            out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False))
            print(f"  -> {out_path}")

    print(f"\nmoomoo calls used this month after this run: {calls_this_month()}")


if __name__ == "__main__":
    with gateway.script_run("probe_moomoo_nonus"):
        main()
