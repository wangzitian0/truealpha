"""Full-universe moomoo fundamental sweep into raw.fetches."""

import argparse
from collections import Counter
from datetime import UTC, datetime

from data_engine import db, jsonable, raw_store
from data_engine.datahub.fetch_key import FetchKey
from data_engine.sources import gateway
from data_engine.sources import moomoo as mm
from data_engine.sources.moomoo_ledger import BudgetExceededError, calls_this_month
from factors.shared import entity_resolution as er
from truealpha_contracts.models import DataSource

CALLER = "sweep_moomoo_fundamentals"
OWNER_PLATE_CHUNK = 50


def plan_calls(conn, codes: list[str], endpoints: list[str], refetch: bool) -> list[tuple[str, str]]:
    return [
        (code, endpoint)
        for code in codes
        for endpoint in endpoints
        if refetch
        or not raw_store.already_fetched(conn, source=DataSource.MOOMOO, source_record_id=f"{endpoint}:{code}")
    ]


def sweep_owner_plate(conn, ctx, codes: list[str], stats: Counter) -> None:
    pending = [
        c
        for c in codes
        if not raw_store.already_fetched(conn, source=DataSource.MOOMOO, source_record_id=f"owner_plate:{c}")
    ]
    for start in range(0, len(pending), OWNER_PLATE_CHUNK):
        chunk = pending[start : start + OWNER_PLATE_CHUNK]
        try:
            df = mm.get_owner_plate(
                ctx,
                chunk,
                caller=CALLER,
                identity=FetchKey.for_batch("moomoo", "get_owner_plate", chunk, gateway.utc_day()),
            )
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
                continue
            raw_store.insert_fetch(
                conn,
                source=DataSource.MOOMOO,
                source_record_id=f"owner_plate:{code}",
                body=mm.canonical_moomoo_bytes(rows),
                content_type="application/json",
                fetched_at=datetime.now(UTC),
                metadata={"batched": True},
            )
            stats[(market, "owner_plate", "ok")] += 1
        conn.commit()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codes", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--all-endpoints", action="store_true")
    parser.add_argument("--refetch", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    conn = db.connect()
    codes = (
        [c.strip() for c in args.codes.split(",") if c.strip()]
        if args.codes
        else [code for code, _ in er.identifiers(conn, "moomoo", as_of=datetime.now(UTC))]
    )
    endpoints = list(mm.FUNDAMENTAL_ENDPOINTS) if args.all_endpoints else list(mm.CORE_ENDPOINTS)
    plan = plan_calls(conn, codes, endpoints, args.refetch)
    rate = int(gateway.CAPACITIES["moomoo"].calls_per_window)
    print(f"universe: {len(codes)} x {len(endpoints)}; {len(plan)} to fetch; used={calls_this_month()} rate={rate}/30s")
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
                data = mm.FUNDAMENTAL_ENDPOINTS[endpoint](
                    ctx, code, CALLER, FetchKey("moomoo", endpoint, f"symbol:{code}", gateway.utc_day())
                )
            except BudgetExceededError as e:
                print(f"ABORT: {e}")
                aborted = True
                break
            except mm.MoomooConnectionError as e:
                stats[(market, endpoint, "fail")] += 1
                print(f"  {code}.{endpoint}: FAILED — {e}")
                continue
            raw_store.insert_fetch(
                conn,
                source=DataSource.MOOMOO,
                source_record_id=f"{endpoint}:{code}",
                body=mm.canonical_moomoo_bytes(data),
                content_type="application/json",
                fetched_at=datetime.now(UTC),
            )
            conn.commit()
            stats[(market, endpoint, "ok")] += 1
            made += 1
        if not aborted and args.limit is None and not args.codes:
            try:
                sweep_owner_plate(conn, ctx, codes, stats)
            except BudgetExceededError as e:
                print(f"ABORT during owner_plate: {e}")
    conn.close()


if __name__ == "__main__":
    with gateway.script_run("sweep_moomoo_fundamentals"):
        main()
