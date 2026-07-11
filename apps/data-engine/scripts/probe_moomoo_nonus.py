"""Spot check moomoo's fundamental endpoints on NON-US listing lines before the
full sweep — every existing sample covers US.* only, and per-market coverage/
field behavior is the sweep's biggest unknown (which endpoints exist for HK/SH/
SZ, whether field ids match, language quirks).

Runs the same endpoint registry as the sweep and writes full JSON per code to
samples/moomoo/ for eyeballing, exactly like the Phase -1 US captures. Default
codes: Tencent (HK) + Kweichow Moutai (SH A-share) ≈ 28 calls.

REQUIRES OpenD running and logged in (see data_engine/sources/moomoo.py).

Usage:
    uv run --package truealpha-data-engine python apps/data-engine/scripts/probe_moomoo_nonus.py [CODE ...]
"""

import json
import sys
from pathlib import Path

from data_engine import jsonable
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
                    results[name] = {"ok": True, "data": jsonable.to_jsonable(call(ctx, code, CALLER))}
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
    main()
