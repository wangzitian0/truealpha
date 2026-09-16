"""Is the governed datahub pointer still advancing? — the alert #536's gate never had.

The a1 gate withholds the pointer when a captured run's weakest cell lost
corroboration, and it is right to. What was missing is the page: in August the
governed head froze for three days on a rate-limit collision while every deploy
check stayed green, because nothing outside the admin funnel reads the pointer's
age, and the funnel pages nobody. This check reads what `/api/health` now reports
(`governed_pointers`: per universe, when the pointer last advanced) and fails when
any universe's pointer is older than the bound, so deploy-freshness's scheduled
escalation files the issue.

Bound: captures are daily, so a fresh pointer is at most ~24 h old and a weekend
of ordinary operation ~72 h. The bound is 72 h: it cannot fire on a normal
weekend and it cannot stay quiet through the three-day freeze it exists for.

Exit codes:
  0 - every universe's pointer advanced within the bound, or this release predates
      the report (the deploy-freshness leg already bounds release lag; see below)
  1 - a pointer is stale, no pointer has ever advanced, the report is unreadable,
      or the endpoint is unreachable
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from infra2_sdk.deploy_health import HttpGet, default_http_get

# Daily captures; a weekend is two missed days. Three days cannot fire on ordinary
# operation and is exactly the freeze that went unpaged (2026-08-15/17).
MAX_AGE_HOURS = 72


class PointerFreshnessFailure(RuntimeError):
    """The environment could not be confirmed to be advancing its pointer."""


@dataclass(frozen=True)
class PointerHead:
    universe_id: str
    advanced_at: datetime
    age_hours: float


def read_pointers(url: str, http_get: HttpGet) -> list[PointerHead] | None:
    """The governed pointers the environment reports, `None` when the release predates
    the report (no key at all — an older build, not a broken one)."""
    try:
        status, body = http_get(url)
    except Exception as exc:  # noqa: BLE001 - reported, never a traceback
        raise PointerFreshnessFailure(f"{url} unreachable: {exc}") from exc
    if status != 200:
        raise PointerFreshnessFailure(f"{url} answered HTTP {status}")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise PointerFreshnessFailure(f"{url} did not answer JSON: {body[:80]!r}") from exc
    if not isinstance(payload, dict):
        raise PointerFreshnessFailure(f"{url} did not answer a JSON object: {body[:80]!r}")
    if "governed_pointers" not in payload:
        return None
    reported = payload["governed_pointers"]
    if reported == "unknown":
        raise PointerFreshnessFailure(f'{url} could not read its governed pointers (reported "unknown")')
    if not isinstance(reported, list):
        raise PointerFreshnessFailure(f"{url} reports governed_pointers of the wrong shape: {reported!r}")
    heads: list[PointerHead] = []
    for entry in reported:
        try:
            advanced_at = datetime.fromisoformat(str(entry["advanced_at"]))
            if advanced_at.tzinfo is None:
                # The service writes a timestamptz's isoformat (offset-aware); a naive stamp
                # is read as UTC rather than crashing the subtraction against our clock.
                advanced_at = advanced_at.replace(tzinfo=UTC)
            heads.append(PointerHead(str(entry["universe_id"]), advanced_at, float(entry["age_hours"])))
        except (KeyError, TypeError, ValueError) as exc:
            raise PointerFreshnessFailure(f"{url} reports a malformed pointer entry: {entry!r}") from exc
    return heads


def check_pointer_freshness(
    url: str,
    *,
    environment: str = "",
    max_age_hours: float = MAX_AGE_HOURS,
    http_get: HttpGet | None = None,
    now: datetime | None = None,
) -> int:
    http_get = http_get or default_http_get()
    name = environment or url
    try:
        heads = read_pointers(url, http_get)
    except PointerFreshnessFailure as exc:
        print(f"pointer freshness check failed: {exc}", file=sys.stderr)
        return 1
    if heads is None:
        print(f"{name} serves a release that predates governed_pointers on /api/health; nothing to bound yet")
        return 0
    if not heads:
        print(
            f"pointer freshness check failed: {name} has no governed pointer at all — no captured run has ever "
            f"headed the pointer, or every one was withheld",
            file=sys.stderr,
        )
        return 1
    reference = now or datetime.now(UTC)
    # Age from the reported timestamp against OUR clock, not the service's own
    # `age_hours`: a service whose clock is wrong reports a wrong age with a straight face.
    stale = [
        (head, (reference - head.advanced_at).total_seconds() / 3600.0)
        for head in heads
        if (reference - head.advanced_at).total_seconds() / 3600.0 > max_age_hours
    ]
    if stale:
        for head, age in stale:
            print(
                f"pointer freshness check failed: {name} {head.universe_id} last advanced "
                f"{head.advanced_at.isoformat()} ({age:.1f} h ago, limit {max_age_hours:g} h) — the governed "
                f"head is frozen; every consumer reads a stale run while the capture ticks look green. Check "
                f"the a1 gate's withheld reasons on the newest runs.",
                file=sys.stderr,
            )
        return 1
    summary = ", ".join(
        f"{head.universe_id} {(reference - head.advanced_at).total_seconds() / 3600.0:.1f} h" for head in heads
    )
    print(f"{name} pointers are advancing: {summary} (limit {max_age_hours:g} h)")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url")
    parser.add_argument("--environment", default="")
    parser.add_argument("--max-age-hours", type=float, default=MAX_AGE_HOURS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    return check_pointer_freshness(
        arguments.url, environment=arguments.environment, max_age_hours=arguments.max_age_hours
    )


if __name__ == "__main__":
    raise SystemExit(main())
