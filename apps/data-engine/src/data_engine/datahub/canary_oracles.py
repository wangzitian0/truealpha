"""Post-deploy canary oracles (#648): judge the newest canary run by NAMED expectations.

The deploy lane triggers `canary_live_pipeline` (a `staging.pipeline_trigger_requests`
insert), waits for the run, then executes THIS script. Every gate this replaces
verified a proxy (workflow conclusions, probe exit codes, model shapes); these oracles
read what the deployed code actually produced from real sources in the real database.

Exit 0 = every oracle holds; exit 1 = the failures, named, one per line.

Packaged INSIDE the image (a docker-cp'd copy died with the first container
replacement — the deploy lane runs `python -m data_engine.datahub.canary_oracles`
so the verdict machinery ships with the code it judges).

Usage:
    python -m data_engine.datahub.canary_oracles [--run-id capture-run:...]
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal

import psycopg

from data_engine.config import settings

_CANARY_UNIVERSE_LIKE = "universe:canary-us-%"
# AAPL's gross profit per employee, generous band: ~$1.07M on 2026 facts; an order
# of magnitude off in either direction means a parse/unit/branch defect, which is
# exactly what #528 shipped and no proxy gate caught.
_AAPL_GPPE_BAND = (Decimal("300000"), Decimal("4000000"))


def failures_for_run(connection: psycopg.Connection, run_id: str) -> list[str]:
    bad: list[str] = []

    status = connection.execute(
        "select obligation_count, success_count + unchanged_count, failed_count, complete"
        " from mart.topt_capture_status where run_id = %s",
        (run_id,),
    ).fetchone()
    if status is None:
        return [f"no capture status for {run_id}"]
    obligations, resolved, failed, complete = status
    if not complete or resolved != obligations or failed:
        bad.append(f"capture incomplete: {resolved}/{obligations} resolved, {failed} failed")

    snapshot = connection.execute(
        "select issuer_count, instrument_count, observation_count from staging.topt_core_snapshots where run_id = %s",
        (run_id,),
    ).fetchone()
    if snapshot is None:
        bad.append("no frozen snapshot")
    elif snapshot != (5, 6, 24):
        # Six listings, five issuers: GOOGL+GOOG share one issuer, and 5 < 6 proves
        # dual-class identity survived end to end.
        bad.append(f"snapshot counts {snapshot} != (5, 6, 24)")

    branches: dict[str, str] = dict(
        connection.execute(
            "select issuer_id, operating_branch from mart.topt_core_results where run_id = %s",
            (run_id,),
        ).fetchall()
    )
    expected_branches = {
        "issuer:cik:0000320193": "non_financial",  # AAPL
        "issuer:cik:0000049196": "financial",  # HBAN, SIC 6021
        "issuer:cik:0000020286": "insurance",  # CINF, SIC 6331
    }
    for issuer, expected in expected_branches.items():
        got = branches.get(issuer)
        if got != expected:
            bad.append(f"{issuer}: operating_branch {got!r} != {expected!r}")

    aapl = connection.execute(
        "select availability, gppe from mart.topt_core_results where run_id = %s and issuer_id = %s",
        (run_id, "issuer:cik:0000320193"),
    ).fetchone()
    if aapl is None:
        bad.append("AAPL row missing from mart")
    else:
        availability, gppe = aapl
        if availability != "available":
            bad.append(f"AAPL not available (availability={availability})")
        elif gppe is None:
            bad.append("AAPL available but GPPE is NULL")
        elif not (_AAPL_GPPE_BAND[0] <= gppe <= _AAPL_GPPE_BAND[1]):
            bad.append(f"AAPL GPPE {gppe} outside {_AAPL_GPPE_BAND}")

    # #705: a multi-listing issuer's P/S must equal the recomputation from any
    # single listing's own captured payloads — every listing carries the
    # company-total dei share count, so the old per-listing summation valued
    # Alphabet twice and no plausibility band noticed. 1% tolerance covers
    # numeric context differences, never a doubling.
    alphabet = connection.execute(
        """
        with payload as (
            select observation.semantic_type, payload.normalized_payload as body
            from raw.capture_obligations obligation
            join raw.capture_obligation_results done on done.capture_obligation_id = obligation.obligation_id
            join raw.capture_attempt_results attempt on attempt.attempt_id = done.final_attempt_id
            join staging.capture_normalized_observations observation
              on observation.source_vintage_id = coalesce(attempt.source_vintage_id, attempt.reused_source_vintage_id)
             and observation.subject_id = obligation.subject_id
             and observation.semantic_type = regexp_replace(obligation.capture_requirement_id, ':v1$', '')
            join staging.capture_observation_payloads payload on payload.observation_id = observation.observation_id
            where obligation.run_id = %s and obligation.subject_id = 'listing:xnas:googl'
        )
        select result.current_ps,
               (select (body->>'revenue')::numeric from payload where semantic_type = 'financial-fact' limit 1),
               (select (body->>'shares_outstanding')::numeric from payload where semantic_type = 'financial-fact' limit 1),
               (select (body->>'close')::numeric from payload where semantic_type = 'market-price' limit 1)
        from mart.topt_core_results result
        where result.run_id = %s and result.issuer_id = 'issuer:cik:0001652044'
        """,
        (run_id, run_id),
    ).fetchone()
    if alphabet is None:
        bad.append("Alphabet row missing from mart")
    else:
        current_ps, revenue, shares, close = alphabet
        if None in (current_ps, revenue, shares, close) or not revenue:
            bad.append(f"Alphabet recompute inputs incomplete: ps={current_ps} rev={revenue} sh={shares} px={close}")
        else:
            recomputed = close * shares / revenue
            if recomputed == 0 or abs(current_ps - recomputed) / recomputed > Decimal("0.01"):
                bad.append(
                    f"Alphabet current_ps {current_ps} != recomputed {recomputed} "
                    "from its own payloads (dual-class double count, #705)"
                )

    report = connection.execute(
        "select payload->'factor_availability'->'gross_profit_per_employee'->>'universe_subjects'"
        " from mart.datahub_quality_report where run_id = %s"
        " order by created_at desc limit 1",
        (run_id,),
    ).fetchone()
    if report is None or report[0] is None:
        bad.append("quality report missing factor_availability (#644)")
    elif int(report[0]) != 6:
        bad.append(f"factor_availability universe_subjects {report[0]} != 6")

    return bad


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=None, help="default: the newest canary capture run")
    args = parser.parse_args()
    with psycopg.connect(settings.database_url) as connection:
        run_id = (
            args.run_id
            or (
                connection.execute(
                    "select run_id from mart.topt_capture_status where universe_id like %s order by cutoff desc limit 1",
                    (_CANARY_UNIVERSE_LIKE,),
                ).fetchone()
                or [None]
            )[0]
        )
        if run_id is None:
            print("canary_assert: no canary run found", file=sys.stderr)
            return 1
        bad = failures_for_run(connection, run_id)
    if bad:
        for line in bad:
            print(f"canary_assert FAIL [{run_id}]: {line}", file=sys.stderr)
        return 1
    print(f"canary_assert OK [{run_id}]: every named oracle holds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
