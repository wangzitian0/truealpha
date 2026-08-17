"""Ask whether a published number is possible at all.

#581. Every gate in this repository compares the system against something the
same process authored — schemas, contracts, twin canons, route freezes. None
asks whether a value can be true. Production is publishing
`listing:xnys:jpm` GPPE = -528,985.79 today; a gross profit per employee cannot
be negative for a profitable bank, #528 has carried that exact number in its
title since 2026-07-30, and nothing is red about it.

These need no vendor call and no external oracle: pure SQL over materialized
`mart`. That is the point — an invariant that depends on the network becomes a
check people mute.

A new tool rather than a function in an existing one because it asks a different
question of a different input at a different time: `deploy_freshness.py` and
`walk_evidence.py` read an HTTP endpoint and git, once per environment, about
the release; this reads SQL, about the numbers, and runs in two places (ci-web
against the seeded fixture and the daily gate against production). It also owns
a file the others have no use for — the exemptions.

Exemptions expire, and that is the load-bearing part. Invariant suites die by
accumulating `# TODO: exclude JPM`; an exemption here carries an issue and a
date, and an expired one FAILS. The check cannot be quietly turned off, only
loudly deferred.

Usage:
  python tools/output_invariants.py --database-url postgresql://…
  python tools/output_invariants.py --database-url … --require-coverage

`--require-coverage` fails when an invariant examined zero rows. A guard that
reports "ok" over an empty set is worse than no guard, and the CI fixture cannot
populate every plane — so coverage is required where the data is real and merely
reported where it is not.

Exit codes:
  0 - every invariant holds (or is exempted and unexpired)
  1 - an invariant is violated, an exemption has expired, or coverage is missing
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import psycopg

EXEMPTIONS_PATH = Path(__file__).with_name("output_invariant_exemptions.json")


@dataclass(frozen=True)
class Invariant:
    id: str
    claim: str
    #: Rows returned ARE the violations; the query must select enough to act on.
    violations: str
    #: How many rows the invariant looked at, so a vacuous pass is visible.
    population: str


INVARIANTS: tuple[Invariant, ...] = (
    Invariant(
        id="gppe-not-negative",
        claim=(
            "gross profit per employee cannot be negative — a company with positive gross "
            "profit and at least one employee has a positive ratio (#528)"
        ),
        violations="""
            select listing_id, gppe::text, operating_branch
            from mart.topt_gppe_results
            where run_id = (select run_id from mart.topt_gppe_results order by created_at desc limit 1)
              and gppe < 0
        """,
        population="""
            select count(*) from mart.topt_gppe_results
            where run_id = (select run_id from mart.topt_gppe_results order by created_at desc limit 1)
        """,
    ),
    Invariant(
        id="available-means-a-value",
        claim=(
            "a cell marked `available` carries a value and a non-zero confidence — "
            "'available with nothing in it' is a contradiction, not a state"
        ),
        violations="""
            select listing_id, availability, coalesce(gppe::text, 'null') as gppe, confidence::text
            from mart.topt_gppe_results
            where run_id = (select run_id from mart.topt_gppe_results order by created_at desc limit 1)
              and availability = 'available'
              and (gppe is null or confidence = 0)
        """,
        population="""
            select count(*) from mart.topt_gppe_results
            where run_id = (select run_id from mart.topt_gppe_results order by created_at desc limit 1)
              and availability = 'available'
        """,
    ),
    Invariant(
        id="selected-weights-close",
        claim="the selected positions of a strategy run sum to a whole portfolio",
        violations="""
            select strategy_run_id, round(sum(target_weight), 6)::text as total, count(*)::text as selected
            from mart.strategy_decisions
            where strategy_run_id = (select strategy_run_id from mart.strategy_runs order by executed_at desc limit 1)
              and outcome = 'selected'
            group by strategy_run_id
            having abs(sum(target_weight) - 1) > 0.000001
        """,
        population="""
            select count(*) from mart.strategy_decisions
            where strategy_run_id = (select strategy_run_id from mart.strategy_runs order by executed_at desc limit 1)
              and outcome = 'selected'
        """,
    ),
    Invariant(
        id="pointer-tracks-the-latest-run",
        claim=(
            "the governed pointer must not lag the latest accepted run by more than one "
            "scheduled cycle — /research tells every visitor the pointer IS what it is "
            "showing (#594)"
        ),
        # 36h: the tick is daily at 22:15Z, so this is one cycle plus slack and
        # well under two. A pointer that has missed a whole cycle is the defect.
        violations="""
            select
              round(extract(epoch from (r.executed_at - p.advanced_at)) / 3600, 1)::text as lag_hours,
              p.target_run_id,
              r.strategy_run_id
            from (select max(advanced_at) as advanced_at, max(target_run_id) as target_run_id
                  from mart.current_pointer_head) p,
                 (select max(executed_at) as executed_at, max(strategy_run_id) as strategy_run_id
                  from mart.strategy_runs) r
            where r.executed_at - p.advanced_at > interval '36 hours'
        """,
        population="select count(*) from mart.current_pointer_head",
    ),
)


@dataclass(frozen=True)
class Exemption:
    invariant: str
    issue: str
    expires: date
    reason: str


def load_exemptions(path: Path = EXEMPTIONS_PATH) -> dict[str, Exemption]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, Exemption] = {}
    for entry in raw["exemptions"]:
        out[entry["invariant"]] = Exemption(
            invariant=entry["invariant"],
            issue=entry["issue"],
            expires=date.fromisoformat(entry["expires"]),
            reason=entry["reason"],
        )
    return out


def _rows(connection: Any, sql: str) -> list[tuple]:
    with connection.cursor() as cursor:
        cursor.execute(sql)
        return cursor.fetchall()


def check(
    database_url: str,
    *,
    require_coverage: bool = False,
    today: date | None = None,
    exemptions: dict[str, Exemption] | None = None,
    invariants: Sequence[Invariant] = INVARIANTS,
    connect: Callable[[str], Any] = psycopg.connect,
) -> int:
    today = today or date.today()
    exemptions = load_exemptions() if exemptions is None else exemptions
    failures: list[str] = []

    with connect(database_url) as connection:
        for invariant in invariants:
            population = _rows(connection, invariant.population)[0][0]
            violations = _rows(connection, invariant.violations)
            exemption = exemptions.get(invariant.id)

            if violations and exemption and exemption.expires >= today:
                print(
                    f"  DEFERRED {invariant.id}: {len(violations)} violation(s), exempt until "
                    f"{exemption.expires} under {exemption.issue} — {exemption.reason}"
                )
                continue
            if violations:
                detail = "; ".join(" ".join(str(field) for field in row) for row in violations[:5])
                expired = f" The exemption under {exemption.issue} expired {exemption.expires}." if exemption else ""
                failures.append(
                    f"{invariant.id}: {len(violations)} violation(s) — {invariant.claim}."
                    f"{expired} Offending rows: {detail}"
                )
                continue
            if population == 0:
                # Nothing was examined, so nothing was proved — including about
                # any exemption. Judging a waiver stale here would fail the CI
                # fixture for not containing the production defect it exempts,
                # which is the vacuity problem pointed the other way.
                print(f"  EMPTY    {invariant.id}: examined 0 rows, so it proved nothing")
                if require_coverage:
                    failures.append(f"{invariant.id}: examined 0 rows where real data was required")
                continue
            if exemption:
                failures.append(
                    f"{invariant.id}: exempt under {exemption.issue} but no longer violated across "
                    f"{population} row(s). Remove the exemption — a standing waiver for a fixed "
                    f"defect hides the next one"
                )
                continue
            print(f"  ok       {invariant.id}: {population} row(s) examined")

    for failure in failures:
        print(f"invariant failed: {failure}", file=sys.stderr)
    return 1 if failures else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--require-coverage", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return check(args.database_url, require_coverage=args.require_coverage)


if __name__ == "__main__":
    raise SystemExit(main())
