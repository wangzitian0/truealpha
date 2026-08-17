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

# Resolve runs the way the CONSUMERS do, not a third way. Review caught all
# three invariants inventing their own: `order by created_at desc` picked a GPPE
# run nothing serves (production's pointer targets capture-run:8c49eec8… while
# the newest row belongs to capture-run:49a1f57e…), and `max()` over
# current_pointer_head mixed a table that is keyed per (environment,
# universe_id, universe_version, factor_id). Both were correct only by accident
# of today's data — one pointer row, one strategy key — and a second factor or
# strategy would have made them compare across planes silently. That is #462's
# defect class, which this file exists to guard against.
#
# Mirrors apps/app-web/src/server/mart/topt-gppe-repository.ts's GOVERNED_HEAD_SQL.
# test_output_invariants.py asserts the two stay in step.
GOVERNED_HEAD = """
    select target_run_id from mart.current_pointer_head
    where environment = 'production' and factor_id = 'gross_profit_per_employee'
    order by advanced_at desc limit 1
"""
# Mirrors strategy-run-repository.ts: latest per strategy_key, that ordering.
DASHBOARD_STRATEGY = "large_model_value_v0"
LATEST_STRATEGY_RUN = f"""
    select strategy_run_id from mart.strategy_runs
    where strategy_key = '{DASHBOARD_STRATEGY}'
    order by executed_at desc, created_at desc, strategy_run_id desc limit 1
"""


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
        violations=f"""
            select listing_id, gppe::text, operating_branch
            from mart.topt_gppe_results
            where run_id = ({GOVERNED_HEAD})
              and gppe < 0
        """,
        population=f"""
            select count(*) from mart.topt_gppe_results
            where run_id = ({GOVERNED_HEAD})
        """,
    ),
    Invariant(
        id="available-means-a-value",
        claim=(
            "a cell marked `available` carries a value and a non-zero confidence — "
            "'available with nothing in it' is a contradiction, not a state"
        ),
        violations=f"""
            select listing_id, availability, coalesce(gppe::text, 'null') as gppe, confidence::text
            from mart.topt_gppe_results
            where run_id = ({GOVERNED_HEAD})
              and availability = 'available'
              and (gppe is null or confidence = 0)
        """,
        population=f"""
            select count(*) from mart.topt_gppe_results
            where run_id = ({GOVERNED_HEAD})
              and availability = 'available'
        """,
    ),
    Invariant(
        id="selected-weights-close",
        claim="the selected positions of a strategy run sum to a whole portfolio",
        violations=f"""
            select strategy_run_id, round(sum(target_weight), 6)::text as total, count(*)::text as selected
            from mart.strategy_decisions
            where strategy_run_id = ({LATEST_STRATEGY_RUN})
              and outcome = 'selected'
            group by strategy_run_id
            having abs(sum(target_weight) - 1) > 0.000001
        """,
        population=f"""
            select count(*) from mart.strategy_decisions
            where strategy_run_id = ({LATEST_STRATEGY_RUN})
              and outcome = 'selected'
        """,
    ),
    Invariant(
        id="every-consumed-input-is-populated",
        claim=(
            "an input key the strategy consumes must be written for at least one issuer at "
            "the latest cutoff — a key that is declared, permitted by the 0039 CHECK and "
            "written for NOBODY is a wiring gap, and it publishes as a missing factor rather "
            "than an error (#284)"
        ),
        # The check that would have caught #284's real defect. `net_income` and
        # `earnings_cagr_3y` were absent from BOTH environments for as long as they
        # existed, because the only deployed caller never asked the parser for them.
        # Every gate stayed green: the keys were declared, the CHECK allowed them, the
        # payload carried them as nulls, `mart...peg` was simply NULL for all 20 issuers
        # and the runs reported SUCCESS. Nothing anywhere asked "is this key populated".
        #
        # Deliberately not "populated for every issuer": correct refusals are normal and
        # common (declining earnings, a loss year inside the window, a refused share
        # count). Zero issuers across a whole cutoff is not a data gap, it is nobody
        # calling the code.
        #
        # The expected list restates migration 0039's CHECK on purpose. A single source
        # would only prove the list agrees with itself; the value here is a second,
        # independent statement of what the deployed run is supposed to produce.
        violations="""
            with expected(input_key) as (values
                ('gross_profit'), ('total_assets'), ('headcount'), ('revenue'),
                ('shares_outstanding'), ('last_close'), ('net_income'), ('earnings_cagr_3y')
            ), latest as (
                select max(cutoff_at) as cutoff_at from staging.strategy_backtest_inputs
            )
            select expected.input_key, (select cutoff_at from latest)::text as cutoff_at
            from expected
            where exists (select 1 from latest where cutoff_at is not null)
              and not exists (
                select 1 from staging.strategy_backtest_inputs i
                where i.input_key = expected.input_key
                  and i.cutoff_at = (select cutoff_at from latest)
              )
        """,
        population="""
            select count(distinct input_key) from staging.strategy_backtest_inputs
            where cutoff_at = (select max(cutoff_at) from staging.strategy_backtest_inputs)
        """,
    ),
    Invariant(
        id="pointer-has-advanced-recently",
        claim=(
            "the governed pointer must advance with each accepted run — /research tells every "
            "visitor it IS what they are looking at, and the tick is daily (#594)"
        ),
        # Single-plane on purpose. An earlier version compared the pointer's age
        # against the latest STRATEGY run, which is a different plane: the
        # pointer is per (environment, factor_id) over capture runs. That the
        # page conflates the two is #594's subject, not something an invariant
        # should reproduce. 36h is one daily cycle plus slack, under two.
        violations="""
            select
              round(extract(epoch from (now() - advanced_at)) / 3600, 1)::text as hours_since,
              target_run_id,
              sequence::text
            from mart.current_pointer_head
            where environment = 'production' and factor_id = 'gross_profit_per_employee'
              and now() - advanced_at > interval '36 hours'
            order by advanced_at desc limit 1
        """,
        population="""
            select count(*) from mart.current_pointer_head
            where environment = 'production' and factor_id = 'gross_profit_per_employee'
        """,
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
