"""Check the warehouse against oracles this repository did not author (#429).

Run this against a REAL database — Production or Staging — not a seeded CI one. A
database this repository populated agrees with itself by construction, so the check has
no signal there; the whole point is to compare captured data against facts that exist
whether or not our code is right.

Two layers, weakest assumption first:

  invariants  Self-evident statements ("gross profit cannot exceed revenue"). Pure SQL,
              no network, evaluable by anyone with no knowledge of this system.
  vendor      Re-derives revenue and gross profit from SEC company-facts live and
              compares. Catches the numbers that are self-consistent but not current —
              which no invariant can see.

Exits non-zero when anything fails, so a scheduler or alerting probe can consume it.

Usage:
    uv run --package truealpha-data-engine python apps/data-engine/scripts/assert_data_invariants.py
    uv run --package truealpha-data-engine python apps/data-engine/scripts/assert_data_invariants.py --with-vendor
    uv run --package truealpha-data-engine python apps/data-engine/scripts/assert_data_invariants.py --json
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

import psycopg
from data_engine.config import settings
from data_engine.quality.invariants import check
from data_engine.quality.vendor_oracle import compare


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-vendor", action="store_true", help="also re-derive values from SEC live (network)")
    parser.add_argument("--json", action="store_true", dest="as_json", help="machine-readable output for alerting")
    parser.add_argument("--cutoff", default=None, help="point-in-time date for the vendor check (default: today UTC)")
    args = parser.parse_args()

    failures = 0
    report: dict[str, object] = {}

    with psycopg.connect(settings.database_url) as connection:
        results = check(connection)
        report["invariants"] = [
            {
                "id": item.invariant.id,
                "statement": item.invariant.statement,
                "violations": item.violations,
                "samples": list(item.samples),
            }
            for item in results
        ]
        failures += sum(1 for item in results if not item.ok)

        if not args.as_json:
            print("== invariants (no network, no fixtures) ==")
            for item in results:
                mark = "PASS" if item.ok else "FAIL"
                print(f"  [{mark}] {item.invariant.id}  {item.invariant.statement}")
                if not item.ok:
                    print(f"         {item.violations} violations — true because {item.invariant.self_evident_because}")
                    for sample in item.samples:
                        print(f"           - {sample}")

        if args.with_vendor:
            if not settings.sec_user_agent:
                raise SystemExit("--with-vendor needs SEC_USER_AGENT (must include a contact email)")
            cutoff = datetime.fromisoformat(args.cutoff).date() if args.cutoff else datetime.now(UTC).date()
            drifts = compare(connection, cutoff=cutoff, user_agent=settings.sec_user_agent)
            disagreeing = [item for item in drifts if not item.agrees]
            report["vendor"] = [
                {
                    "ticker": item.ticker,
                    "field": item.field,
                    "mart_value": None if item.mart_value is None else str(item.mart_value),
                    "vendor_value": None if item.vendor is None else str(item.vendor.value),
                    "vendor_period_end": None if item.vendor is None else item.vendor.period_end.isoformat(),
                    "vendor_concept": None if item.vendor is None else item.vendor.concept,
                    "staleness_years": item.staleness_years,
                }
                for item in disagreeing
            ]
            failures += len(disagreeing)

            if not args.as_json:
                print(f"\n== vendor cross-check (SEC live, cutoff {cutoff}) ==")
                print(f"  {len(drifts) - len(disagreeing)}/{len(drifts)} fields agree with the source")
                for item in sorted(disagreeing, key=lambda d: -(d.staleness_years or 0)):
                    stale = f" [{item.staleness_years}y stale]" if item.staleness_years else ""
                    vendor = (
                        "no such fact" if item.vendor is None else f"{item.vendor.value} @ {item.vendor.period_end}"
                    )
                    print(f"  [FAIL] {item.ticker:<6} {item.field:<13} mart={item.mart_value} vendor={vendor}{stale}")

    if args.as_json:
        report["failures"] = failures
        print(json.dumps(report, indent=2))
    else:
        print(f"\n{failures} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
