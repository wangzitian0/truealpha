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
import json
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
    expected_data_engine_digest: str = "",
) -> int:
    """Poll url until healthy; print the outcome; return a shell exit code.

    `expected_data_engine_digest` (#712) turns the data-engine identity from a report into
    a verdict: after the app is healthy, the surface must report that exact
    `data_engine_image_digest` — the build the release's registry tag names — before the
    poll budget runs out. The promoted build stamps it with its boot canary
    (`data_engine.boot_canary`), so "the newest run was produced by the old build" is
    exactly the state this waits through, and "it never was" is the red.
    """
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
    if expected_data_engine_digest:
        verdict = _await_data_engine(
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
    engine = _data_engine_parser(result.body)
    if expected_version and engine == "unknown":
        # Un-assertable, not a pass. Said out loud so a health check that silently stopped
        # covering the data engine cannot look identical to one that covered it.
        print(f"health check: {url} reports no data_engine_parser — the data engine is UNVERIFIED")
    git_sha, digest = _data_engine_identity(result.body)
    # #712: the identity is reported and compared out loud. It does not gate yet — the app
    # lane and the data engine are still promoted separately, so a mismatch is the honest
    # state of most days until one release promotes all three images; hiding it was the
    # defect. The line is what the deploy log shows a reader who asks "which data engine".
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
        # The app lane stamps the release TAG and the data-engine lane stamps the commit
        # it was promoted from; the two are not comparable until one release promotes
        # all three images. Said plainly instead of printing a build line that looks
        # like agreement.
        print(
            f"health check: app reports {identifier_kind(expected_version)} {expected_version!r}, the data engine "
            f"that produced the newest run reports {identifier_kind(git_sha)} {git_sha!r} ({digest}) — not "
            f"comparable until the data engine is promoted by the same release (#712)"
        )
    else:
        print(f"health check: data engine build {git_sha} ({digest}) produced the newest run")
    print(f"health check passed: {url} is healthy ({result.body})")
    return 0


def _await_data_engine(
    url: str,
    expected_digest: str,
    *,
    http_get: HttpGet,
    sleep: Callable[[float], None],
    max_attempts: int,
    first_body: str,
) -> str | None:
    """None when the surface reports `expected_digest`; else the sentence for the red."""
    body = first_body
    for attempt in range(1, max(1, max_attempts) + 1):
        git_sha, digest = _data_engine_identity(body)
        if digest == expected_digest:
            print(
                f"health check: data engine build {git_sha} ({digest}) produced the newest run — "
                f"matches the release (attempt {attempt})"
            )
            return None
        if attempt == max_attempts:
            break
        sleep(INTERVAL_SECONDS)
        try:
            status_code, body = http_get(url)
        except Exception as exc:  # noqa: BLE001 - one bad poll is not a verdict
            status_code, body = 0, f"{exc.__class__.__name__}: {exc}"
        if status_code != 200:
            body = ""
    git_sha, digest = _data_engine_identity(body)
    return (
        f"DATA ENGINE MISMATCH — the release expects data-engine digest {expected_digest} but "
        f"{url} reports {digest} (build {git_sha}) after {max_attempts} attempts; the promoted "
        f"build has not produced a run, or a different build is running (#712)"
    )


def resolve_data_engine_digest(tag: str, *, http_get: HttpGet | None = None) -> str:
    """The registry digest of `ghcr.io/wangzitian0/truealpha-data-engine:<tag>`.

    Read through the anonymous Registry v2 manifest API with the OCI accept set, the
    same digest `docker pull image@…` and infra2's runner pin from the tag — so the gate
    and the promotion agree by construction, not by a copied string.
    """
    import urllib.request

    image = "wangzitian0/truealpha-data-engine"
    accept = ", ".join(
        [
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        ]
    )
    token_url = f"https://ghcr.io/token?scope=repository:{image}:pull"
    with urllib.request.urlopen(token_url, timeout=15) as response:  # noqa: S310 - fixed https host
        token = json.loads(response.read().decode()).get("token", "")
    request = urllib.request.Request(
        f"https://ghcr.io/v2/{image}/manifests/{tag}",
        method="HEAD",
        headers={"Accept": accept, "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
        digest = response.headers.get("Docker-Content-Digest", "")
    if not digest.startswith("sha256:") or len(digest) != 71:
        raise RuntimeError(f"registry returned no usable digest for {image}:{tag} ({digest!r})")
    return digest


def _same_commit(expected: str, reported: str) -> bool:
    """Either side may carry the short form of the same commit (7 vs 40 chars), so the
    match is a two-way prefix, the same rule the SDK applies to the app's own sha."""
    return bool(expected) and bool(reported) and (expected.startswith(reported) or reported.startswith(expected))


def _data_engine_identity(body: str) -> tuple[str, str]:
    """(git_sha, image_digest) the health surface reports for the data engine, or unknown."""
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError):
        return ("unknown", "unknown")
    if not isinstance(parsed, dict):
        return ("unknown", "unknown")
    return (
        str(parsed.get("data_engine_git_sha") or "unknown"),
        str(parsed.get("data_engine_image_digest") or "unknown"),
    )


def _data_engine_parser(body: str) -> str:
    """The data-engine vintage the health surface reports, or "unknown".

    The data engine has no HTTP surface, so until #712 every post-deploy check -- this
    one, the surface walk, both canaries -- exercised app-web or llm-service. A promotion
    could therefore leave the app that computes every published number one release behind
    with every step green, which is what v0.0.37 did. llm-service now reports the vintage
    from mart, so the lane can see it over a surface it already calls.
    """
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError):
        return "unknown"
    value = parsed.get("data_engine_parser") if isinstance(parsed, dict) else None
    return str(value) if value else "unknown"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("expected_version", nargs="?", default="")
    parser.add_argument("max_attempts", nargs="?", type=int, default=DEFAULT_MAX_ATTEMPTS)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--expect-data-engine-digest",
        default="",
        help="#712: fail unless /health reports this data_engine_image_digest (sha256:…) within the budget",
    )
    group.add_argument(
        "--expect-data-engine-tag",
        default="",
        help="#712: resolve this release tag's data-engine digest from ghcr.io and require it",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    expected_digest = args.expect_data_engine_digest
    if args.expect_data_engine_tag:
        try:
            expected_digest = resolve_data_engine_digest(args.expect_data_engine_tag)
        except Exception as exc:  # noqa: BLE001 - an unresolvable tag is a red, not a pass
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
