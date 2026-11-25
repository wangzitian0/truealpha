"""Point a release tag at the exact image that main published (#1878).

The script copies the manifest bytes of ``<image>:sha-<short>`` to ``<image>:<tag>``.
The registry computes a digest from the bytes, so the tag gets the tested digest.
The script builds nothing.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

REGISTRY = "https://ghcr.io"
MANIFEST_TYPES = ",".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)

# 404: main has not published yet. 429 and 5xx: a transient registry fault.
RETRY_STATUSES = frozenset({404, 429, 500, 502, 503, 504})
PUSH_RETRIES = 3


def source_tag_for(commit: str) -> str:
    """Return the tag that docker/metadata-action `type=sha,format=short` writes on main."""
    if len(commit) < 7 or any(c not in "0123456789abcdef" for c in commit.lower()):
        raise ValueError(f"commit {commit!r} is not a hex SHA of 7 or more characters")
    return f"sha-{commit.lower()[:7]}"


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


Transport = Callable[[str, str, dict[str, str], bytes | None], Response]


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as reply:  # noqa: S310 - fixed https registry URL
            return Response(reply.status, {k.lower(): v for k, v in reply.headers.items()}, reply.read())
    except urllib.error.HTTPError as error:
        return Response(error.code, {k.lower(): v for k, v in error.headers.items()}, error.read())


def registry_token(repository: str, user: str, password: str, transport: Transport) -> str:
    basic = base64.b64encode(f"{user}:{password}".encode()).decode()
    url = f"{REGISTRY}/token?service=ghcr.io&scope=repository:{repository}:pull,push"
    reply = transport("GET", url, {"Authorization": f"Basic {basic}"}, None)
    if reply.status != 200:
        raise RuntimeError(f"token request for {repository} returned HTTP {reply.status}")
    return json.loads(reply.body)["token"]


def digest_of(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def fetch_manifest(repository: str, reference: str, token: str, transport: Transport) -> Response:
    headers = {"Authorization": f"Bearer {token}", "Accept": MANIFEST_TYPES}
    return transport("GET", f"{REGISTRY}/v2/{repository}/manifests/{reference}", headers, None)


def promote(
    repository: str,
    source_tag: str,
    release_tag: str,
    token: str,
    transport: Transport,
    *,
    attempts: int = 20,
    delay_seconds: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Copy the source manifest to the release tag and return the shared digest."""
    source = fetch_manifest(repository, source_tag, token, transport)
    for _ in range(attempts - 1):
        if source.status not in RETRY_STATUSES:
            break
        # The main publish can still run when the tag arrives, or GHCR can blip; wait, do not build.
        sleep(delay_seconds)
        source = fetch_manifest(repository, source_tag, token, transport)
    if source.status != 200:
        raise RuntimeError(
            f"{repository}:{source_tag} is not published (HTTP {source.status}); main must publish it first"
        )
    digest = digest_of(source.body)
    if source.headers.get("docker-content-digest", digest) != digest:
        raise RuntimeError(f"{repository}:{source_tag} body digest {digest} differs from the registry header")
    content_type = source.headers.get("content-type", "")
    if not content_type:
        raise RuntimeError(f"{repository}:{source_tag} has no manifest content type")

    put_headers = {"Authorization": f"Bearer {token}", "Content-Type": content_type}
    put_url = f"{REGISTRY}/v2/{repository}/manifests/{release_tag}"
    put = transport("PUT", put_url, put_headers, source.body)
    for _ in range(PUSH_RETRIES):
        if put.status not in RETRY_STATUSES or put.status == 404:
            break
        sleep(delay_seconds)
        put = transport("PUT", put_url, put_headers, source.body)
    if put.status not in (200, 201):
        raise RuntimeError(f"push of {repository}:{release_tag} returned HTTP {put.status}")

    check = fetch_manifest(repository, release_tag, token, transport)
    for _ in range(PUSH_RETRIES):
        if check.status not in RETRY_STATUSES:
            break
        sleep(delay_seconds)
        check = fetch_manifest(repository, release_tag, token, transport)
    if check.status != 200 or digest_of(check.body) != digest:
        raise RuntimeError(f"{repository}:{release_tag} does not resolve to {digest} after the push")
    return digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help="owner/image, for example wangzitian0/truealpha-app-web")
    parser.add_argument("--commit", required=True, help="full or short commit SHA that main published")
    parser.add_argument("--tag", required=True, help="release tag vX.Y.Z")
    args = parser.parse_args(argv)
    token = registry_token(args.repository, os.environ["GHCR_USER"], os.environ["GHCR_TOKEN"], urllib_transport)
    source_tag = source_tag_for(args.commit)
    digest = promote(args.repository, source_tag, args.tag, token, urllib_transport)
    print(f"{args.repository}:{args.tag} -> {source_tag} {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
