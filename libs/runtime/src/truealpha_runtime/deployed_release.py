"""One implementation of "what release is this environment serving".

#585. The fact that a deployed environment reports its release as a TAG
(``v0.0.20``), not a commit sha, was encoded independently in four places:

    tools/health_check.py         kind classification + version keys
    tools/deploy_freshness.py     read, validate, resolve
    tools/issue_close_guard.py    read, validate, resolve
    .github/workflows/deploy-freshness.yml   `curl | jq -r '.git_sha'`

The fourth is the one that runs daily and validated nothing: a non-object body
made ``jq`` emit ``null``, which reached ``walk_evidence.py`` as a release name
and produced "no 'Deploy prod null' run in the last 100 deploy-release runs" —
a true sentence about the wrong question. Two of the other three had learned to
reject a ref ``git`` would read as an option; the second learned it from review
after the first had already fixed it.

Where the four disagreed, this picks one answer deliberately rather than
inheriting whichever file was edited last:

    non-200                 error, naming the status
    missing / "unknown"     error — a check that cannot see the identity must
                            not go on to produce a verdict about it
    non-object JSON         error, naming the body prefix
    a ref git could misread  error, before the value is used for anything

All four are the same judgement: this module either returns an identity it is
willing to stand behind, or it raises. It never returns something a caller has
to re-validate, which is how the four copies came about.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

# (status_code, body_text) — the infra2_sdk.deploy_health convention.
HttpGet = Callable[[str], tuple[int, str]]

#: Health payload keys that may carry the release identity, in preference order.
VERSION_KEYS = ("git_sha", "version")

_COMMIT_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_RELEASE_TAG = re.compile(r"^v\d+\.\d+\.\d+$")
# The identity arrives over HTTP and is handed to git, where a leading "-" is
# read as an option and whitespace makes the failure non-deterministic.
_SAFE_REF = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._/-]{0,199}$")


class ReleaseIdentityError(RuntimeError):
    """The environment's release identity could not be established."""


def identifier_kind(value: str) -> str:
    """Name the kind of release identifier, for a message a human can act on."""
    if not value or value == "unknown":
        return "unset"
    if _COMMIT_SHA.match(value):
        return "commit sha"
    if _RELEASE_TAG.match(value):
        return "release tag"
    return "unrecognised"


def identity_from_body(body: str, *, url: str = "") -> str:
    """The release identity in a health payload, or raise.

    Split from :func:`read_deployed_release` because ``health_check`` inspects a
    body it already has, mid-poll, and must not fetch it twice.
    """
    where = url or "the health endpoint"
    try:
        payload = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise ReleaseIdentityError(f"{where} did not answer JSON: {body[:120]!r}") from exc
    if not isinstance(payload, dict):
        # Valid JSON, wrong shape. `.get` on a list raises AttributeError, which
        # surfaces as a crash rather than a verdict; and `jq -r '.git_sha'` on
        # one prints "null", which reaches a caller as a release name.
        raise ReleaseIdentityError(f"{where} answered JSON that is not an object: {body[:120]!r}")

    for key in VERSION_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value and value != "unknown":
            break
    else:
        raise ReleaseIdentityError(
            f"{where} does not report a release identity (body {body[:120]!r}); nothing about "
            f"the deployed release can be judged until the deployer threads GIT_COMMIT_SHA through"
        )

    if not _SAFE_REF.match(value):
        raise ReleaseIdentityError(
            f"{where} reports {value!r}, which is not a usable release identifier — a leading "
            f"'-' would be read by git as an option, and whitespace makes the failure "
            f"non-deterministic. This value is not used"
        )
    return value


def read_deployed_release(url: str, http_get: HttpGet) -> str:
    """Fetch ``url`` and return the release identity it reports, or raise."""
    status_code, body = http_get(url)
    if status_code != 200:
        raise ReleaseIdentityError(f"{url} answered HTTP {status_code}; its release is unknown")
    return identity_from_body(body, url=url)
