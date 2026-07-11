"""Full-universe moomoo fundamental sweep into raw.fetches.

Every call goes through the ledger gate + burst throttle (init.md Section 1 rule
6). Runs on the OpenD host and REQUIRES MOOMOO_LEDGER_BACKEND=postgres — at
sweep volume the JSON file ledger would rewrite itself thousands of times, and
its state isn't what anyone audits.

Endpoint set: CORE_ENDPOINTS (what the current factor modules read, ~9
calls/line) by default; --all-endpoints adds the rest of FUNDAMENTAL_ENDPOINTS.
owner_plate (sector classification) batches 50 codes/call at the end, persisted
per code so resume stays line-granular.

Per-market coverage is an open question (samples only ever covered US.*): an
endpoint failing for a market is recorded (ledger ok=false), the sweep moves on,
and the final summary prints market x endpoint failure counts — run
probe_moomoo_nonus.py first to eyeball the payload shapes on a couple of HK/CN
codes before spending a full sweep.

Resume: a (code, endpoint) pair with a successful raw.fetches row is skipped.
Aborts (BudgetExceededError, OpenD restart) lose nothing — rerun to continue.

Usage:
    uv run --package truealpha-data-engine python apps/data-engine/scripts/sweep_moomoo_fundamentals.py \
        [--codes US.NVDA,HK.00700] [--limit N] [--all-endpoints] [--dry-run]
"""

import argparse
from collections import Counter
from datetime import UTC, datetime

from data_engine import db, jsonable, raw_store
from data_engine.config import settings
from data_engine.sources import moomoo as mm
from data_engine.sources.moomoo_ledger import BudgetExceededError, calls_this_month
from factors.shared import entity_resolution as er

SOURCE = "moomoo"
CALLER = "sweep_moomoo_fundamentals"
OWNER_PLATE_CHUNK = 50


def plan_calls(conn, codes: list[str], endpoints: list[str], refetch: bool) -> list[tuple[str, str]]:
    plan = []
    for code in codes:
        for endpoint in endpoints:
            if refetch or not raw_store.already_fetched(conn, source=SOURCE, endpoint=endpoint, entity_key=code):
                plan.append((code, endpoint))
    return plan


def sweep_owner_plate(conn, ctx, codes: list[str], stats: Counter) -> None:
    """Batched, then persisted per code so the resume check stays line-granular.
    A code absent from its batch's response is counted as a failure and NOT
    persisted — an empty raw row would satisfy already_fetched forever, turning
    one bad response into a permanent silent gap."""
    pending = [
        c for c in codes if not raw_store.already_fetched(conn, source=SOURCE, endpoint="owner_plate", entity_key=c)
    ]
    for start in range(0, len(pending), OWNER_PLATE_CHUNK):
        chunk = pending[start : start + OWNER_PLATE_CHUNK]
        try:
            df = mm.get_owner_plate(ctx, chunk, caller=CALLER)
        except mm.MoomooConnectionError as e:
            for code in chunk:
                stats[(code.split(".")[0], "owner_plate", "fail")] += 1
            print(f"  owner_plate chunk failed: {e}")
            continue
        rows_by_code: dict[str, list] = {}
        for row in jsonable.to_jsonable(df):
            rows_by_code.setdefault(row.get("code"), []).append(row)
        for code in chunk:
            market = code.split(".")[0]
            rows = rows_by_code.get(code)
            if not rows:
                stats[(market, "owner_plate", "fail")] += 1
                print(f"  {code}.owner_plate: absent from batched response — will retry next run")
                continue
            raw_store.insert_fetch(
                conn, source=SOURCE, endpoint="owner_plate", entity_key=code, payload=rows, params={"batched": True}
            )
            stats[(market, "owner_plate", "ok")] += 1
        conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codes", default=None, help="comma-separated moomoo codes (default: every KG moomoo line)")
    parser.add_argument("--limit", type=int, default=None, help="stop after N successful calls (spot-check runs)")
    parser.add_argument("--all-endpoints", action="store_true", help="all 14 endpoints, not just the factor core set")
    parser.add_argument("--refetch", action="store_true", help="pull new vintages even where rows exist")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and budget state, make no calls")
    args = parser.parse_args()

    if settings.moomoo_ledger_backend != "postgres":
        raise SystemExit(
            "sweep requires MOOMOO_LEDGER_BACKEND=postgres (staging.api_call_ledger) — "
            "the JSON file ledger is for small probe sessions only"
        )

    conn = db.connect()
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        codes = [code for code, _entity in er.identifiers(conn, "moomoo", as_of=datetime.now(UTC))]
    endpoints = list(mm.FUNDAMENTAL_ENDPOINTS) if args.all_endpoints else list(mm.CORE_ENDPOINTS)

    plan = plan_calls(conn, codes, endpoints, args.refetch)
    used = calls_this_month()
    print(
        f"universe: {len(codes)} listing lines x {len(endpoints)} endpoints; "
        f"{len(plan)} calls to make (rest already fetched)\n"
        f"ledger: {used}/{settings.moomoo_monthly_call_budget} used this month; "
        f"throttle {settings.moomoo_calls_per_30s}/30s "
        f"=> est. {len(plan) * 30 / settings.moomoo_calls_per_30s / 3600:.1f}h"
    )
    if args.dry_run:
        conn.close()
        return

    stats: Counter = Counter()
    made = 0
    aborted = False
    with mm.connect() as ctx:
        for code, endpoint in plan:
            if args.limit is not None and made >= args.limit:
                break
            market = code.split(".")[0]
            try:
                data = mm.FUNDAMENTAL_ENDPOINTS[endpoint](ctx, code, CALLER)
            except BudgetExceededError as e:
                print(f"\nABORT: {e}\nrerun to resume — completed calls are skipped")
                aborted = True
                break
            except mm.MoomooConnectionError as e:
                stats[(market, endpoint, "fail")] += 1
                print(f"  {code}.{endpoint}: FAILED — {e}")
                continue
            raw_store.insert_fetch(
                conn, source=SOURCE, endpoint=endpoint, entity_key=code, payload=jsonable.to_jsonable(data)
            )
            conn.commit()
            stats[(market, endpoint, "ok")] += 1
            made += 1
            if made % 100 == 0:
                print(f"  {made}/{len(plan)} calls done")

        if not aborted and args.limit is None and not args.codes:
            try:
                sweep_owner_plate(conn, ctx, codes, stats)
            except BudgetExceededError as e:
                # The gated get_owner_plate path raises this too; a budget hit
                # this late must still reach the summary below, not a traceback.
                print(f"\nABORT during owner_plate: {e}\nrerun to resume")

    conn.close()
    print("\nper market x endpoint:")
    markets = sorted({k[0] for k in stats if len(k) == 3})
    for market in markets:
        ok = sum(v for k, v in stats.items() if len(k) == 3 and k[0] == market and k[2] == "ok")
        fails = {k[1]: v for k, v in stats.items() if len(k) == 3 and k[0] == market and k[2] == "fail"}
        print(f"  {market}: {ok} ok" + (f", failures: {fails}" if fails else ""))
    print(f"ledger after run: {calls_this_month()}/{settings.moomoo_monthly_call_budget}")


if __name__ == "__main__":
    main()
