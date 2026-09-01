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
from hashlib import sha256

import psycopg

from data_engine.config import settings

_CANARY_UNIVERSE_LIKE = "universe:canary-us-%"
# AAPL's gross profit per employee, generous band: ~$1.07M on 2026 facts; an order
# of magnitude off in either direction means a parse/unit/branch defect, which is
# exactly what #528 shipped and no proxy gate caught.
_AAPL_GPPE_BAND = (Decimal("300000"), Decimal("4000000"))


#: The infra2-sdk release this repository pins in `pyproject.toml`. Asserted against what
#: the deployed image actually loaded, because a pin only binds the resolver -- an image
#: built from a stale lock, or one where the wheel failed to install, satisfies the pin on
#: paper and loads something else.
PINNED_INFRA2_SDK = "1.2.0"


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

    bad.extend(_infra_failures(connection, run_id))
    return bad


def _infra_failures(connection: psycopg.Connection, run_id: str) -> list[str]:
    """Infra oracles: dereference real bytes, and pin the SDK the image actually loaded.

    Every other oracle here reads Postgres, so Postgres proves itself by their passing.
    Object storage and the infra2 SDK do not: a run can complete, write correctly-shaped
    `s3://` URIs, and leave nothing behind them. #531 is exactly that -- production injects
    no `S3_*` variables, the client fell back to a default pointing at its own loopback,
    and three runs died with what looked like a network fault.

    A TCP or HTTP probe cannot catch this class. Only DEREFERENCING an object can: it
    exercises the endpoint, the credentials, the bucket and the write in one assertion, and
    hashing what comes back proves the bytes are the ones the database claims.
    """
    bad: list[str] = []

    row = connection.execute(
        """
        select f.object_uri, f.payload_sha256, f.byte_length, f.content_type
        from staging.capture_normalized_observations o
        join raw.capture_obligations ob on ob.obligation_id = o.capture_obligation_id
        join raw.capture_source_vintages v on v.source_vintage_id = o.source_vintage_id
        join raw.fetches f on f.id = v.raw_fetch_id
        where ob.run_id = %s and f.object_uri is not null
        order by f.id desc limit 1
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        bad.append("no fetch with an object_uri in this run — nothing to dereference (#531)")
    else:
        object_uri, expected_sha, expected_len, content_type = row
        try:
            from truealpha_contracts import RawObjectRef
            from truealpha_runtime.storage import S3RawObjectStore

            bucket, _, key = object_uri.removeprefix("s3://").partition("/")
            payload = S3RawObjectStore().get(
                RawObjectRef(
                    bucket=bucket,
                    key=key,
                    sha256=expected_sha,
                    byte_length=int(expected_len or 0),
                    content_type=content_type or "application/octet-stream",
                )
            )
        except Exception as error:  # noqa: BLE001 - any failure here is the finding
            bad.append(f"object storage unreachable or misconfigured: {type(error).__name__} ({object_uri})")
        else:
            actual = sha256(payload).hexdigest()
            if actual != expected_sha:
                bad.append(f"object bytes do not match the recorded checksum for {object_uri}")
            elif expected_len is not None and len(payload) != int(expected_len):
                bad.append(f"object byte_length {len(payload)} != recorded {expected_len} for {object_uri}")

    # The SDK is a released contract, not a vendored copy: the image must have loaded the
    # version this repository pins, or every shared dispatch/health primitive is a guess.
    try:
        from importlib.metadata import version as _installed

        loaded = _installed("infra2-sdk")
    except Exception as error:  # noqa: BLE001
        bad.append(f"infra2-sdk is not importable in the deployed image: {type(error).__name__}")
    else:
        if loaded != PINNED_INFRA2_SDK:
            bad.append(f"infra2-sdk {loaded} loaded, repository pins {PINNED_INFRA2_SDK}")

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
