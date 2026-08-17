"""Poll a deployed TrueAlpha URL until the target release is actually live.

The generic polling algorithm (HTTP-200 + status-field check, the version/
git_sha stable-mismatch budget) is infra2_sdk.deploy_health's responsibility
(#508); this wrapper only supplies TrueAlpha's own health endpoint and its
{"status": "ok"} convention -- llm-service's /health, reached through
Traefik's /api prefix route (tools/route_manifest.json).

Usage:
  python tools/health_check.py <url> [expected_version] [max_attempts]

Exit codes:
  0 - Health check passed
  1 - Health check failed (connection error, HTTP error, or version never matched)
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Sequence

from infra2_sdk.deploy_health import HttpGet, default_http_get, poll_until_healthy
from truealpha_runtime.deployed_release import (
    ReleaseIdentityError,
    identifier_kind,
    identity_from_body,
)

DEFAULT_MAX_ATTEMPTS = 24
INTERVAL_SECONDS = 10.0

# #526: the two sides of this gate must speak the same kind of identifier.
# `deploy-release.yml` passed `source_sha` (a 40-hex commit sha) while the
# deployed service reports `GIT_COMMIT_SHA`, which the deployers set to the
# release TAG. The SDK's version match is a two-way prefix match, so a tag and
# a sha can never match and the gate burned its whole 24-attempt budget before
# reporting "did not become healthy (last status: HTTP 200)" — the endpoint was
# fine, the comparison was impossible. Every prod release recorded as FAILURE,
# and the run history was believed over the runtime for two days (#429's exact
# failure mode, manufactured on every release).
#
# A kind mismatch is never transitional: a service that reports tags will not
# start reporting shas mid-rollout. So it fails IMMEDIATELY and says which side
# reports what, instead of hiding behind a four-minute timeout. A same-kind
# mismatch keeps the SDK's rollout tolerance untouched.


class IdentifierKindMismatch(RuntimeError):
    """The expected and reported release identifiers can never compare equal."""


def _guarding_kind(http_get: HttpGet, expected: str) -> HttpGet:
    """Wrap `http_get` so the first usable response settles the kind question."""
    expected_kind = identifier_kind(expected)

    def guarded(url: str) -> tuple[int, str]:
        status_code, body = http_get(url)
        if not expected or status_code != 200:
            return status_code, body
        try:
            reported = identity_from_body(body, url=url)
        except ReleaseIdentityError as exc:
            # Same judgement as everywhere else now: a gate that cannot see the
            # release identity must not go on polling as if it might (#585).
            raise IdentifierKindMismatch(str(exc)) from exc
        reported_kind = identifier_kind(reported)
        if reported_kind != expected_kind:
            # Labelled pair rather than a sentence: `identifier_kind` returns
            # "unset" and "unrecognised" too, and no single article reads for
            # all four. This message is the whole point of the guard — an
            # operator reading only the failed step must be able to act on it —
            # so it must not degrade to "expected a unset" (review).
            raise IdentifierKindMismatch(
                f"identifier kinds disagree — gate expects {expected_kind} ({expected!r}); "
                f"{url} reports {reported_kind} ({reported!r}). These can never compare "
                f"equal, so the release cannot be confirmed either way. Fix the side that "
                f"is wrong — the gate must pass what the runtime actually reports (#526)"
            )
        return status_code, body

    return guarded


def check_health(
    url: str,
    *,
    expected_version: str = "",
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    http_get: HttpGet | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Poll url until healthy; print the outcome; return a shell exit code."""
    http_get = http_get or default_http_get()
    try:
        result = poll_until_healthy(
            url,
            http_get=_guarding_kind(http_get, expected_version),
            expected_version=expected_version,
            require_status="ok",
            max_attempts=max_attempts,
            interval_seconds=INTERVAL_SECONDS,
            sleep=sleep,
        )
    except IdentifierKindMismatch as exc:
        print(f"health check failed: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"health check failed: {exc}", file=sys.stderr)
        return 1
    print(f"health check passed: {url} is healthy ({result.body})")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("expected_version", nargs="?", default="")
    parser.add_argument("max_attempts", nargs="?", type=int, default=DEFAULT_MAX_ATTEMPTS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return check_health(
        args.url,
        expected_version=args.expected_version,
        max_attempts=args.max_attempts,
    )


if __name__ == "__main__":
    raise SystemExit(main())
