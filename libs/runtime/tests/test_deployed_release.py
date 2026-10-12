"""Tests for truealpha_runtime.deployed_release — #585.

The four copies this replaces disagreed on exactly three points: a body that is
valid JSON but not an object, an identity of "unknown", and a ref git could read
as an option. Two validated the ref, three handled the shape, and the one that
ran daily (deploy-freshness.yml's `curl | jq -r '.git_sha'`) validated nothing.
Each disagreement is one test here, so the answer is picked in one place.
"""

from __future__ import annotations

import json

import pytest
from truealpha_runtime.deployed_release import (
    ReleaseIdentityError,
    identifier_kind,
    identity_from_body,
    read_deployed_release,
)

URL = "https://truealpha.club/api/health"


def _serving(payload: object):
    body = payload if isinstance(payload, str) else json.dumps(payload)

    def http_get(url: str) -> tuple[int, str]:
        return 200, body

    return http_get


def test_identifier_kind_names_each_shape() -> None:
    assert identifier_kind("d2da931" + "a" * 33) == "commit sha"
    assert identifier_kind("abc1234") == "commit sha"
    assert identifier_kind("v0.0.20") == "release tag"
    assert identifier_kind("unknown") == "unset"
    assert identifier_kind("") == "unset"
    assert identifier_kind("refs/heads/main") == "unrecognised"


def test_reads_the_release_a_healthy_environment_reports() -> None:
    assert read_deployed_release(URL, _serving({"status": "ok", "git_sha": "v0.0.20"})) == "v0.0.20"


def test_falls_back_to_the_version_key() -> None:
    assert read_deployed_release(URL, _serving({"version": "v0.0.20"})) == "v0.0.20"


def test_a_non_200_is_not_an_identity() -> None:
    with pytest.raises(ReleaseIdentityError, match="HTTP 503"):
        read_deployed_release(URL, lambda _url: (503, "down"))


def test_json_that_is_not_an_object_says_so() -> None:
    """Disagreement 1. A list is valid JSON; `.get` on it raises AttributeError,
    and `jq -r '.git_sha'` on it prints "null" — which the daily check then
    carried into a run title. It now has its own message rather than being
    collapsed into "no identity field"."""
    with pytest.raises(ReleaseIdentityError, match="not an object"):
        read_deployed_release(URL, _serving(["not", "an", "object"]))


def test_a_body_that_is_not_json_says_so() -> None:
    with pytest.raises(ReleaseIdentityError, match="did not answer JSON"):
        read_deployed_release(URL, _serving("<html>gateway</html>"))


@pytest.mark.parametrize("payload", [{"status": "ok"}, {"git_sha": "unknown"}, {"git_sha": ""}])
def test_an_absent_identity_is_an_error_not_a_value(payload: dict) -> None:
    """Disagreement 2. `GIT_COMMIT_SHA` defaults to "unknown"; a caller that
    receives it as a value goes on to produce a verdict about a release nobody
    can name."""
    with pytest.raises(ReleaseIdentityError, match="does not report a release identity"):
        read_deployed_release(URL, _serving(payload))


@pytest.mark.parametrize("hostile", ["--upload-pack=touch /tmp/x", "-n", "v1 --all", "a" * 300, " v0.0.20"])
def test_a_ref_git_could_misread_is_refused(hostile: str) -> None:
    """Disagreement 3. Two of the four copies validated this; the second learned
    it from review after the first had already fixed it, and the daily path
    never did."""
    with pytest.raises(ReleaseIdentityError, match="not a usable release identifier"):
        read_deployed_release(URL, _serving({"git_sha": hostile}))


def test_identity_from_body_does_not_refetch() -> None:
    """`health_check` inspects a body it already holds, mid-poll."""
    assert identity_from_body(json.dumps({"git_sha": "v0.0.20"}), url=URL) == "v0.0.20"
