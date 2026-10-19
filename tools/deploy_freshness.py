"""Fail when a deployed environment is serving work that finished days ago.

#560. Every web property this repository guards is guaranteed on ``main`` and
guaranteed nowhere a person is. On 2026-08-14 both environments served
``v0.0.19`` (2026-07-30) while ``main`` was 18 commits ahead, including the
factor corrections, the datahub fixes and the entire app-web remediation. That
lasted 15 days and **nothing was red about it**, because no acceptance criterion
in this repository was ever about how old what a user sees is.

The bound is age, not commit count, and that is a deliberate choice: a count
measures how busy the repository has been, while age measures how long finished
work has been invisible — which is the thing that actually hurts. Ten commits
merged this morning are not a problem; one commit merged last week is.

The bound is per environment (#819). ``tools/cut_release.sh`` deploys staging
after every tag and promotes production only with ``--prod``: the owner promotes
deliberately, so production lags staging by design, and the bound that is right
for staging (3 days: finished work must reach the soak quickly) had the
production leg red every day for the lag the release protocol asks for.
Production gets 14 days — a fortnight for a deliberate promotion, still under
the 15-day gap that went unnoticed, so the failure above would fire at this
bound too. The caller names the bound: ``deploy-freshness.yml`` carries it in
its matrix and passes ``--max-age-days``; this tool only measures against it.

Usage:
  python tools/deploy_freshness.py <url> [--environment NAME] [--max-age-days N] [--repo PATH]

Exit codes:
  0 - the deployed release is within the bound (or main has nothing newer)
  1 - stale, unreachable, or the runtime does not report a release identity
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from infra2_sdk.deploy import (
    FreshnessFailure,
    HttpGet,
    ReleaseIdentityError,
    Staleness,
    check_deploy_freshness,
    default_http_get,
    format_freshness_failure,
    measure_freshness,
    read_deployed_release,
)

STAGING_MAX_AGE_DAYS = 3
PRODUCTION_MAX_AGE_DAYS = 14
DEFAULT_MAX_AGE_DAYS = STAGING_MAX_AGE_DAYS

measure = measure_freshness
format_failure = format_freshness_failure
check_freshness = check_deploy_freshness

__all__ = [
    "DEFAULT_MAX_AGE_DAYS",
    "FreshnessFailure",
    "HttpGet",
    "PRODUCTION_MAX_AGE_DAYS",
    "ReleaseIdentityError",
    "STAGING_MAX_AGE_DAYS",
    "Staleness",
    "check_deploy_freshness",
    "check_freshness",
    "default_http_get",
    "format_failure",
    "format_freshness_failure",
    "main",
    "measure",
    "measure_freshness",
    "read_deployed_release",
]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--environment", default="")
    parser.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--warn-only", action="store_true", default=False, help="Emit warning and exit 0 on age exceedance")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return check_freshness(
        args.url,
        environment=args.environment,
        max_age_days=args.max_age_days,
        repo=args.repo,
        warn_only=args.warn_only,
    )


if __name__ == "__main__":
    raise SystemExit(main())
