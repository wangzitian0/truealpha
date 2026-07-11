"""Sweep SEC company-facts for every KG company with a CIK into raw.fetches.

Free and quota-less (SEC fair use is ~10 req/s; we pace at 4/s), so this can run
from anywhere — it doesn't need the OpenD host. Covers the US-filer part of the
universe only; HK/CN-listed names have no SEC data and are moomoo's job.

Resume: a CIK with a successful raw.fetches row is skipped, so rerunning after
any abort continues where it left off. A deliberate refresh (new vintage of
everything) is `--refetch`, which skips the check — never an overwrite, raw is
append-only.

Usage:
    uv run --package truealpha-data-engine python apps/data-engine/scripts/sweep_sec_facts.py [--limit N] [--refetch]
"""

import argparse
import time

import httpx
from data_engine import db, raw_store
from data_engine.sources import sec
from factors.shared import entity_resolution as er

SOURCE, ENDPOINT = "sec", "companyfacts"
PACE_SECONDS = 0.25


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="stop after N fetches (spot-check runs)")
    parser.add_argument("--refetch", action="store_true", help="pull a new vintage even if one exists")
    args = parser.parse_args()

    conn = db.connect()
    ciks = er.identifiers(conn, "cik")
    print(f"universe: {len(ciks)} companies with a CIK")

    fetched = skipped = failed = 0
    with sec._client() as client:
        for cik_str, entity_id in ciks:
            if args.limit is not None and fetched >= args.limit:
                break
            cik = int(cik_str)
            key = f"CIK{cik:010d}"
            if not args.refetch and raw_store.already_fetched(conn, source=SOURCE, endpoint=ENDPOINT, entity_key=key):
                skipped += 1
                continue
            try:
                facts = sec.fetch_company_facts(cik, client)
            except httpx.HTTPStatusError as e:
                # 404 = registrant with no XBRL facts (rare but real); anything
                # else is worth seeing loudly in the log too, but neither should
                # kill a 500-company sweep.
                failed += 1
                print(f"  {key} ({entity_id}): HTTP {e.response.status_code}, skipping")
                continue
            raw_id = raw_store.insert_fetch(conn, source=SOURCE, endpoint=ENDPOINT, entity_key=key, payload=facts)
            conn.commit()
            fetched += 1
            if fetched % 25 == 0:
                print(f"  {fetched} fetched (latest {key} -> raw.fetches:{raw_id})")
            time.sleep(PACE_SECONDS)

    conn.close()
    print(f"done: {fetched} fetched, {skipped} already present, {failed} failed")


if __name__ == "__main__":
    main()
