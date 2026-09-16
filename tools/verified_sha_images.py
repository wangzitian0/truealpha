#!/usr/bin/env python3
"""Split a release's images into what main already published at this SHA and what must build (#860).

    python3 tools/verified_sha_images.py --owner wangzitian0 --sha <40-hex commit> < images.json

stdin is release-images' plan, `[{"image": "truealpha-app-web", "dockerfile": ..., "context": ...}, ...]`.
stdout is `{"retag": [{"image", "source", "digest"}, ...], "build": [<the input entries main did not publish>]}`.

A tag names a SHA main already proved (`tag_verified`, #673 D2), and since #731 a main push
publishes `<image>:sha-<short>` for every image whose inputs that merge touched. An image main
published at this exact SHA therefore has nothing left to build: the tag can name that digest.
An image main did NOT publish at this SHA -- #731 skipped it because the merge left its inputs
alone -- has no verified digest at this SHA and builds as before. Never `latest`: by tag time it
may already be a later merge's image.

`sha-<short>` is docker/metadata-action's `type=sha,format=short` spelling (seven hex chars);
test_ci_workflows.py pins that the publish step still tags that way. The registry read is the
anonymous Registry v2 flow infra2's `_image_manifest_exists` and `infra2_sdk.release.
resolve_image_digest` use -- an anonymous pull token, then HEAD on the manifest with the OCI
accept set -- in the stdlib, because the `plan` job that runs this installs nothing. 404 is the
one answer that means "build". Every other failure raises: a refused or unavailable registry must
fail the tag run, not quietly turn every release back into the rebuild nobody would notice.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
#: docker/metadata-action `type=sha,format=short`: the first seven hex characters.
SHORT_SHA_LENGTH = 7
#: A transient registry answer is retried this many times in total before it is a failure.
ATTEMPTS = 3
_RETRIED_STATUSES = frozenset({429, 500, 502, 503, 504})
_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_DIGEST_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")


class RegistryError(RuntimeError):
    """The registry answered something other than "here is the digest" or "no such tag"."""


class TransportError(RegistryError):
    """No answer at all -- DNS, a refused connection, a timeout. Retried like a 5xx, then red
    through the same `::error::` path; a traceback from a tag run would say nothing about
    which image or which SHA (Copilot review on #868)."""


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes = b""

    def header(self, name: str) -> str:
        return next((value for key, value in self.headers.items() if key.lower() == name.lower()), "")


Transport = Callable[[str, str, dict[str, str]], Response]
"""(method, url, headers) -> Response; injectable so the tests never reach a registry."""


def urllib_transport(method: str, url: str, headers: dict[str, str]) -> Response:
    request = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return Response(response.status, dict(response.headers.items()), response.read())
    except urllib.error.HTTPError as exc:
        return Response(exc.code, dict(exc.headers.items()), exc.read())
    except OSError as exc:  # URLError, socket timeouts and refusals are all OSError
        raise TransportError(f"{method} {url}: no answer ({exc})") from exc


def _send(
    transport: Transport,
    method: str,
    url: str,
    headers: dict[str, str],
    sleep: Callable[[float], None],
) -> Response:
    """One request under the retry policy: 429/5xx and no-answer are retried with backoff,
    everything else is returned for the caller to judge. The policy lives here, once, so the
    token fetch and the manifest read cannot drift apart on what counts as transient."""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            response = transport(method, url, headers)
        except TransportError as exc:
            if attempt < ATTEMPTS:
                sleep(float(2**attempt))
                continue
            raise RegistryError(f"{exc} after {ATTEMPTS} attempts") from exc
        if response.status in _RETRIED_STATUSES and attempt < ATTEMPTS:
            sleep(float(2**attempt))
            continue
        return response
    raise AssertionError("unreachable: every attempt returns or raises")


def short_sha(sha: str) -> str:
    """The `sha-<short>` reference main published this commit under: metadata-action's seven."""
    if not _SHA_RE.fullmatch(sha):
        raise ValueError(f"--sha must be the full 40-hex commit sha the tag points at, got {sha!r}")
    return sha[:SHORT_SHA_LENGTH]


def _anonymous_token(transport: Transport, challenge: str, sleep: Callable[[float], None]) -> str:
    if not challenge.lower().startswith("bearer "):
        raise RegistryError(f"the registry's challenge is not a Bearer challenge: {challenge!r}")
    params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    realm = params.get("realm")
    if not realm:
        raise RegistryError(f"the registry's Bearer challenge names no realm: {challenge!r}")
    query = urllib.parse.urlencode({key: params[key] for key in ("service", "scope") if key in params})
    response = _send(transport, "GET", f"{realm}?{query}", {}, sleep)
    if response.status != 200:
        raise RegistryError(f"anonymous pull token refused by {realm} (status {response.status})")
    payload = json.loads(response.body or b"{}")
    token = payload.get("token") or payload.get("access_token")
    if not token:
        raise RegistryError(f"the token response from {realm} names no token")
    return str(token)


def published_digest(
    repository: str,
    reference: str,
    *,
    registry: str = "ghcr.io",
    transport: Transport = urllib_transport,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """The digest `registry/repository:reference` names, or None when no such tag exists.

    The same HEAD-with-Bearer read `infra2_sdk.release.resolve_image_digest` performs, so the
    digest a tag is re-pointed at is the one infra2's deployer will resolve from that tag.
    """
    url = f"https://{registry}/v2/{repository}/manifests/{reference}"
    headers = {"Accept": MANIFEST_ACCEPT}
    response = _send(transport, "HEAD", url, headers, sleep)
    if response.status == 401:
        token = _anonymous_token(transport, response.header("www-authenticate"), sleep)
        response = _send(transport, "HEAD", url, {**headers, "Authorization": f"Bearer {token}"}, sleep)
    if response.status == 404:
        return None
    if 200 <= response.status < 300:
        digest = response.header("docker-content-digest")
        if not _DIGEST_RE.fullmatch(digest):
            raise RegistryError(f"{registry}/{repository}:{reference} answered without a usable digest ({digest!r})")
        return digest
    raise RegistryError(
        f"{registry}/{repository}:{reference} answered status {response.status} — refusing to guess whether "
        f"main published it; a rebuild here would hide a registry problem behind a slower green"
    )


def split(
    images: list[dict[str, object]],
    *,
    owner: str,
    sha: str,
    registry: str = "ghcr.io",
    transport: Transport = urllib_transport,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, list[dict[str, object]]]:
    """`{"retag": [...], "build": [...]}` -- every input entry lands in exactly one list."""
    reference = f"sha-{short_sha(sha)}"
    retag: list[dict[str, object]] = []
    build: list[dict[str, object]] = []
    for entry in images:
        image = entry.get("image")
        if not isinstance(image, str) or not image:
            raise ValueError(f"every plan entry names its image; got {entry!r}")
        repository = f"{owner}/{image}"
        digest = published_digest(repository, reference, registry=registry, transport=transport, sleep=sleep)
        if digest is None:
            build.append(dict(entry))
        else:
            retag.append({"image": image, "source": f"{registry}/{repository}:{reference}", "digest": digest})
    return {"retag": retag, "build": build}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--owner", required=True, help="the GHCR namespace (github.repository_owner)")
    parser.add_argument("--sha", required=True, help="the full commit sha the tag points at (github.sha)")
    parser.add_argument("--registry", default="ghcr.io")
    args = parser.parse_args(argv)

    images = json.load(sys.stdin)
    if not isinstance(images, list):
        print("::error::stdin must be the plan's JSON list of images", file=sys.stderr)
        return 2
    try:
        # The module-level transport, looked up at call time rather than bound as a default,
        # so a test can stand a fake registry in front of the CLI; the first version bound it
        # as a default and the CLI tests silently read the real registry.
        result = split(images, owner=args.owner, sha=args.sha, registry=args.registry, transport=urllib_transport)
    except (RegistryError, ValueError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    for entry in result["retag"]:
        print(f"retag {entry['image']}: main published {entry['source']} = {entry['digest']}", file=sys.stderr)
    for entry in result["build"]:
        print(
            f"build {entry['image']}: main did not publish {args.registry}/{args.owner}/{entry['image']}:sha-"
            f"{short_sha(args.sha)} (#731 leaves an untouched image unpublished), so this SHA builds it",
            file=sys.stderr,
        )
    json.dump(result, sys.stdout, separators=(",", ":"))
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
