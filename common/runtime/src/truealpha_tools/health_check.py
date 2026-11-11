"""Poll a deployed TrueAlpha URL until the target release is live (#508, #526, #712)."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from infra2_sdk.deploy import (
    HttpGet,
    ReleaseIdentityError,
    default_http_get,
    identity_from_body,
    poll_until_healthy,
)
from infra2_sdk.refs import ReleaseError, identifier_kind, resolve_image_digest

if TYPE_CHECKING:
    from infra2_sdk.transport import HttpTransport

DEFAULT_MAX_ATTEMPTS = 24
INTERVAL_SECONDS = 10.0
DATA_ENGINE_IMAGE = "wangzitian0/truealpha-data-engine"


class IdentifierKindMismatch(RuntimeError):
    """The expected and reported release identifiers can never compare equal."""


def _guarding_kind(http_get: HttpGet, expected: str) -> HttpGet:
    expected_kind = identifier_kind(expected)

    def guarded(url: str) -> tuple[int, str]:
        status_code, body = http_get(url)
        if not expected or status_code != 200:
            return status_code, body
        try:
            reported = identity_from_body(body, url=url)
        except ReleaseIdentityError as exc:
            raise IdentifierKindMismatch(str(exc)) from exc
        reported_kind = identifier_kind(reported)
        if reported_kind != expected_kind:
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
    expected_data_engine_digest: str = "",
) -> int:
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
    except RuntimeError as exc:
        print(f"health check failed: {exc}", file=sys.stderr)
        return 1
    body = result.body
    if expected_data_engine_digest:
        verdict, body = _await_data_engine(
            url,
            expected_data_engine_digest,
            http_get=http_get,
            sleep=sleep,
            max_attempts=max_attempts,
            first_body=result.body,
        )
        if verdict is not None:
            print(f"health check failed: {verdict}", file=sys.stderr)
            return 1
    if expected_version and _data_engine_parser(body) == "unknown":
        print(f"health check: {url} reports no data_engine_parser — the data engine is UNVERIFIED")
    git_sha, digest = _data_engine_identity(body)
    if git_sha == "unknown":
        print(f"health check: {url} reports no data-engine identity — the data engine build is UNKNOWN")
    elif (
        expected_version
        and identifier_kind(expected_version) == "commit sha"
        and not _same_commit(expected_version, git_sha)
    ):
        print(
            f"health check: DATA ENGINE MISMATCH — app {expected_version} but the newest run was produced by "
            f"data-engine {git_sha} ({digest}); #712"
        )
    elif expected_version and identifier_kind(expected_version) != identifier_kind(git_sha):
        print(
            f"health check: app reports {identifier_kind(expected_version)} {expected_version!r}, the data engine "
            f"that produced the newest run reports {identifier_kind(git_sha)} {git_sha!r} ({digest}) — not "
            f"comparable until the data engine is promoted by the same release (#712)"
        )
    else:
        print(f"health check: data engine build {git_sha} ({digest}) produced the newest run")
    print(f"health check passed: {url} is healthy ({body})")
    return 0


def _await_data_engine(
    url: str,
    expected_digest: str,
    *,
    http_get: HttpGet,
    sleep: Callable[[float], None],
    max_attempts: int,
    first_body: str,
) -> tuple[str | None, str]:
    attempts = max(1, max_attempts)
    body = first_body
    for attempt in range(1, attempts + 1):
        git_sha, digest = _data_engine_identity(body)
        if digest == expected_digest:
            print(
                f"health check: data engine build {git_sha} ({digest}) produced the newest run — "
                f"matches the release (attempt {attempt})"
            )
            return None, body
        if attempt == attempts:
            break
        sleep(INTERVAL_SECONDS)
        try:
            status_code, polled = http_get(url)
        except Exception as exc:  # noqa: BLE001
            print(f"health check: poll {attempt + 1} failed ({exc.__class__.__name__}: {exc}); keeping the last answer")
            continue
        if status_code == 200:
            body = polled
    git_sha, digest = _data_engine_identity(body)
    return (
        f"DATA ENGINE MISMATCH — the release expects data-engine digest {expected_digest} but "
        f"{url} reports {digest} (build {git_sha}) after {attempts} attempts; the promoted "
        f"build has not produced a run, or a different build is running (#712)"
    ), body


def resolve_data_engine_digest(tag: str, *, transport: HttpTransport | None = None) -> str:
    try:
        return resolve_image_digest(image=DATA_ENGINE_IMAGE, reference=tag, transport=transport)
    except (ReleaseError, ValueError) as exc:
        raise RuntimeError(f"registry returned no usable digest for {DATA_ENGINE_IMAGE}:{tag} ({exc})") from exc


def _same_commit(expected: str, reported: str) -> bool:
    return bool(expected) and bool(reported) and (expected.startswith(reported) or reported.startswith(expected))


def _json_dict(body: str) -> dict[str, object]:
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _data_engine_identity(body: str) -> tuple[str, str]:
    parsed = _json_dict(body)
    return (
        str(parsed.get("data_engine_git_sha") or "unknown"),
        str(parsed.get("data_engine_image_digest") or "unknown"),
    )


def _data_engine_parser(body: str) -> str:
    value = _json_dict(body).get("data_engine_parser")
    return str(value) if value else "unknown"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("expected_version", nargs="?", default="")
    parser.add_argument("max_attempts", nargs="?", type=int, default=DEFAULT_MAX_ATTEMPTS)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--expect-data-engine-digest", default="")
    group.add_argument("--expect-data-engine-tag", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    expected_digest = args.expect_data_engine_digest
    if args.expect_data_engine_tag:
        try:
            expected_digest = resolve_data_engine_digest(args.expect_data_engine_tag)
        except Exception as exc:  # noqa: BLE001
            print(
                f"health check failed: cannot resolve the data-engine digest for {args.expect_data_engine_tag}: {exc}",
                file=sys.stderr,
            )
            return 1
        print(f"health check: release {args.expect_data_engine_tag} names data-engine digest {expected_digest}")
    return check_health(
        args.url,
        expected_version=args.expected_version,
        max_attempts=args.max_attempts,
        expected_data_engine_digest=expected_digest,
    )


if __name__ == "__main__":
    raise SystemExit(main())
