"""Check the warehouse against self-evident SQL invariants and live SEC vendor facts."""

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
    parser.add_argument("--with-vendor", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--cutoff", default=None)
    args = parser.parse_args()

    failures = 0
    report: dict[str, object] = {}
    with psycopg.connect(settings.database_url) as connection:
        results = check(connection)
        report["invariants"] = [
            {
                "id": i.invariant.id,
                "statement": i.invariant.statement,
                "violations": i.violations,
                "samples": list(i.samples),
            }
            for i in results
        ]
        failures += sum(1 for i in results if not i.ok)
        if not args.as_json:
            for item in results:
                print(f"  [{'PASS' if item.ok else 'FAIL'}] {item.invariant.id}  {item.invariant.statement}")

        if args.with_vendor:
            if not settings.sec_user_agent:
                raise SystemExit("--with-vendor needs SEC_USER_AGENT")
            cutoff = datetime.fromisoformat(args.cutoff).date() if args.cutoff else datetime.now(UTC).date()
            drifts = compare(connection, cutoff=cutoff, user_agent=settings.sec_user_agent)
            disagreeing = [item for item in drifts if not item.agrees]
            report["vendor"] = [
                {
                    "ticker": d.ticker,
                    "field": d.field,
                    "mart_value": None if d.mart_value is None else str(d.mart_value),
                    "vendor_value": None if d.vendor is None else str(d.vendor.value),
                    "vendor_period_end": None if d.vendor is None else d.vendor.period_end.isoformat(),
                    "vendor_concept": None if d.vendor is None else d.vendor.concept,
                    "staleness_years": d.staleness_years,
                }
                for d in disagreeing
            ]
            failures += len(disagreeing)

    if args.as_json:
        report["failures"] = failures
        print(json.dumps(report, indent=2))
    else:
        print(f"\n{failures} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
