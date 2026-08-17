"""Tests for tools/health_check.py -- confirms TrueAlpha's deployed release is
actually live, using infra2_sdk.deploy_health.poll_until_healthy's shared polling
algorithm against llm-service's {"status": "ok", "git_sha": ...} convention."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = REPO_ROOT / "tools/health_check.py"
SPEC = importlib.util.spec_from_file_location("truealpha_health_check", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = _module
SPEC.loader.exec_module(_module)
check_health = _module.check_health

URL = "https://truealpha.club/api/health"


def _responses(*pairs: tuple[int, str]):
    it = iter(pairs)

    def http_get(url: str) -> tuple[int, str]:
        return next(it)

    return http_get


def test_succeeds_immediately_on_a_healthy_response() -> None:
    exit_code = check_health(
        URL,
        http_get=_responses((200, json.dumps({"status": "ok", "git_sha": "abc1234"}))),
        sleep=lambda _: None,
    )
    assert exit_code == 0


def test_succeeds_when_the_reported_sha_matches_expected(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = check_health(
        URL,
        expected_version="abc1234",
        http_get=_responses((200, json.dumps({"status": "ok", "git_sha": "abc1234"}))),
        sleep=lambda _: None,
    )
    assert exit_code == 0
    assert "health check passed" in capsys.readouterr().out


def test_retries_through_a_connection_failure_then_succeeds() -> None:
    exit_code = check_health(
        URL,
        http_get=_responses((0, "connection refused"), (200, json.dumps({"status": "ok"}))),
        max_attempts=5,
        sleep=lambda _: None,
    )
    assert exit_code == 0


def test_fails_after_a_stable_version_mismatch(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = check_health(
        URL,
        expected_version="new1111",
        http_get=_responses(*[(200, json.dumps({"status": "ok", "git_sha": "old0000"}))] * 3),
        max_attempts=3,
        sleep=lambda _: None,
    )
    assert exit_code == 1
    assert "still reporting version 'old0000'" in capsys.readouterr().err


def test_fails_when_the_status_field_never_reports_ok(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = check_health(
        URL,
        http_get=_responses((200, json.dumps({"status": "degraded"}))),
        max_attempts=1,
        sleep=lambda _: None,
    )
    assert exit_code == 1
    assert "did not become healthy" in capsys.readouterr().err


# --- #526: the two sides must speak the same kind of identifier -------------
#
# `deploy-release.yml` passed a 40-hex commit sha while the deployed service
# reports the release tag. The SDK's version match is a two-way prefix match, so
# those can never match: the gate exhausted 24 attempts and reported "did not
# become healthy (last status: HTTP 200)" on every prod release, which actually
# deployed fine. The run history was believed over the runtime for two days.
#
# A kind mismatch is never transitional, so it must fail immediately and name
# both sides. A same-kind mismatch keeps the SDK's rollout tolerance.

_TAG_BODY = json.dumps({"status": "ok", "git_sha": "v0.0.19"})
_SHA_40 = "d2da931" + "a" * 33


def test_identifier_kind_names_each_shape() -> None:
    assert _module.identifier_kind(_SHA_40) == "commit sha"
    assert _module.identifier_kind("abc1234") == "commit sha"
    assert _module.identifier_kind("v0.0.19") == "release tag"
    assert _module.identifier_kind("unknown") == "unset"
    assert _module.identifier_kind("") == "unset"


def test_fails_immediately_when_a_sha_is_compared_against_a_reported_tag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The exact #526 configuration: it must fail on the FIRST response."""
    attempts = 0

    def http_get(url: str) -> tuple[int, str]:
        nonlocal attempts
        attempts += 1
        return 200, _TAG_BODY

    exit_code = check_health(URL, expected_version=_SHA_40, http_get=http_get, sleep=lambda _: None)
    assert exit_code == 1
    assert attempts == 1, "a kind mismatch is not transitional; it must not burn the budget"
    stderr = capsys.readouterr().err
    assert "identifier kinds disagree" in stderr
    assert "commit sha" in stderr and "release tag" in stderr


def test_the_mismatch_message_reads_for_every_kind() -> None:
    """The message is the deliverable: an operator reading only the failed step
    must be able to act on it. `identifier_kind` returns "unset" and
    "unrecognised" too, and an article hardcoded for one kind degrades the
    others into "expected a unset" (review)."""
    for expected in (_SHA_40, "v0.0.19", "refs/heads/main"):
        buffered: list[str] = []
        try:
            _module._guarding_kind(lambda _url: (200, json.dumps({"status": "ok", "git_sha": "abcdef1"})), expected)(
                URL
            )
        except _module.IdentifierKindMismatch as exc:
            buffered.append(str(exc))
        if not buffered:
            continue
        message = buffered[0]
        assert " a unset" not in message and " a unrecognised" not in message
        assert "expects" in message and "reports" in message


def test_passes_when_both_sides_are_release_tags() -> None:
    exit_code = check_health(
        URL,
        expected_version="v0.0.19",
        http_get=_responses((200, _TAG_BODY)),
        sleep=lambda _: None,
    )
    assert exit_code == 0


def test_fails_immediately_when_the_runtime_reports_no_release_identity(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`GIT_COMMIT_SHA` defaults to "unknown"; a gate cannot confirm anything then."""
    exit_code = check_health(
        URL,
        expected_version="v0.0.19",
        http_get=_responses((200, json.dumps({"status": "ok", "git_sha": "unknown"}))),
        sleep=lambda _: None,
    )
    assert exit_code == 1
    assert "does not report a release identity" in capsys.readouterr().err


def test_same_kind_mismatch_still_gets_the_rollout_budget() -> None:
    """A tag that is merely the WRONG tag may be a rollout in progress."""
    attempts = 0

    def http_get(url: str) -> tuple[int, str]:
        nonlocal attempts
        attempts += 1
        return 200, json.dumps({"status": "ok", "git_sha": "v0.0.18"})

    exit_code = check_health(URL, expected_version="v0.0.19", http_get=http_get, max_attempts=3, sleep=lambda _: None)
    assert exit_code == 1
    assert attempts == 3, "a same-kind mismatch must keep the SDK's tolerance for a rollout"


def test_the_release_workflow_passes_the_kind_the_runtime_reports() -> None:
    """The two sides are in different files; nothing else asserts they agree.

    The runtime reports `GIT_COMMIT_SHA`, which the deployers set to the release
    tag, so the gate must pass `version_ref`. If a future change makes the
    service report a real commit sha, this test is the thing that says the
    workflow has to move with it.
    """
    workflow = (REPO_ROOT / ".github/workflows/deploy-release.yml").read_text()
    gate = workflow.split("Confirm the deployed release is healthy", 1)[1]
    assert "version_ref" in gate, "the health gate must compare the release ref the runtime reports"
    assert "source_sha" not in gate.split("health_check.py", 1)[0], (
        "the health gate must not pass a commit sha while the runtime reports a tag (#526)"
    )
