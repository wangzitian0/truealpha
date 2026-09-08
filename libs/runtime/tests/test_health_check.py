"""Tests for tools/health_check.py -- confirms TrueAlpha's deployed release is
actually live, using infra2_sdk.deploy_health.poll_until_healthy's shared polling
algorithm against llm-service's {"status": "ok", "git_sha": ...} convention."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from truealpha_runtime.testing import load_tool

REPO_ROOT = Path(__file__).resolve().parents[3]
_module = load_tool("health_check")
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


def test_the_data_engine_vintage_is_read_from_the_health_body() -> None:
    """#712: the data engine has no HTTP surface, so nothing in the deploy verdict saw it.

    Every post-deploy check exercised app-web or llm-service, which is how v0.0.37 promoted
    web and llm to the tag while the data engine kept an older digest with every step green.
    llm-service now reports the vintage from mart; this asserts the lane reads it.
    """
    import json as _json

    _data_engine_parser = load_tool("health_check")._data_engine_parser

    assert _data_engine_parser(_json.dumps({"status": "ok", "data_engine_parser": "p:v8"})) == "p:v8"
    # Absent, empty and unparseable all mean the same thing to the caller: UNVERIFIED, and
    # never a silent pass.
    assert _data_engine_parser(_json.dumps({"status": "ok"})) == "unknown"
    assert _data_engine_parser(_json.dumps({"data_engine_parser": ""})) == "unknown"
    assert _data_engine_parser("not json") == "unknown"


def test_llm_service_health_reports_the_data_engine_vintage() -> None:
    """The producing half of the same contract — the reader above is useless without it.

    Calls the handler rather than grepping its source: a text assertion passed when the
    line was merely COMMENTED OUT, because the string it looked for was still in the file.
    The mutation reproof caught that, which is what it is for.
    """
    llm = pytest.importorskip("llm_service.main")

    body = llm.health()
    assert "data_engine_parser" in body, "the deploy lane cannot verify what is not reported"
    # No database in a unit test, so the honest answer is "unknown" -- and it must be a
    # STRING, never an exception: health answers "is the service up", and a read it only
    # reports must never be able to fail it.
    assert isinstance(body["data_engine_parser"], str)
    assert body["status"] == "ok"


def test_the_data_engine_build_is_reported_next_to_the_app(capsys: pytest.CaptureFixture[str]) -> None:
    """#712: the gate says which data-engine build produced the newest run, in the same
    log line a reader checks for the app's sha. Report-only for now — the two lanes are
    still promoted separately, so a mismatch is the honest state most days."""
    body = {
        "status": "ok",
        "git_sha": "abc1234",
        "data_engine_parser": "p:v8",
        "data_engine_git_sha": "abc1234",
        "data_engine_image_digest": "sha256:00f7",
    }
    exit_code = check_health(
        URL, expected_version="abc1234", http_get=_responses((200, json.dumps(body))), sleep=lambda _: None
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "data engine build abc1234 (sha256:00f7) produced the newest run" in out
    assert "MISMATCH" not in out


def test_a_data_engine_behind_the_app_is_said_out_loud(capsys: pytest.CaptureFixture[str]) -> None:
    body = {
        "status": "ok",
        "git_sha": "abc1234",
        "data_engine_parser": "p:v8",
        "data_engine_git_sha": "0ld0000",
        "data_engine_image_digest": "sha256:0000",
    }
    exit_code = check_health(
        URL, expected_version="abc1234", http_get=_responses((200, json.dumps(body))), sleep=lambda _: None
    )
    assert exit_code == 0, "report-only until one release promotes all three images (#712)"
    out = capsys.readouterr().out
    assert "DATA ENGINE MISMATCH" in out and "0ld0000" in out and "#712" in out


def test_a_health_body_without_an_identity_reads_unknown_not_matched(capsys: pytest.CaptureFixture[str]) -> None:
    body = {"status": "ok", "git_sha": "abc1234", "data_engine_parser": "p:v8"}
    exit_code = check_health(
        URL, expected_version="abc1234", http_get=_responses((200, json.dumps(body))), sleep=lambda _: None
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "data engine build is UNKNOWN" in out
    assert "produced the newest run" not in out


def test_a_tag_against_a_data_engine_sha_is_named_not_comparable(capsys: pytest.CaptureFixture[str]) -> None:
    """The app lane stamps the release tag, the data-engine lane the commit it was
    promoted from (#712). The gate must not print a build line that reads as agreement."""
    body = {
        "status": "ok",
        "git_sha": "v0.0.45",
        "data_engine_parser": "p:v8",
        "data_engine_git_sha": "4cf7291",
        "data_engine_image_digest": "sha256:00f7",
    }
    exit_code = check_health(
        URL, expected_version="v0.0.45", http_get=_responses((200, json.dumps(body))), sleep=lambda _: None
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "not comparable until the data engine is promoted by the same release" in out
    assert "produced the newest run\n" not in out


def test_a_short_and_a_full_sha_of_one_commit_agree(capsys: pytest.CaptureFixture[str]) -> None:
    """Review on #753: the app lane may pass the 40-char sha while the data engine
    stamped the 7-char form, or the reverse. Same commit, no MISMATCH."""
    full = "abc1234" + "0" * 33
    body = {
        "status": "ok",
        "git_sha": full,
        "data_engine_parser": "p:v8",
        "data_engine_git_sha": "abc1234",
        "data_engine_image_digest": "sha256:00f7",
    }
    exit_code = check_health(
        URL, expected_version=full, http_get=_responses((200, json.dumps(body))), sleep=lambda _: None
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "MISMATCH" not in out and "produced the newest run" in out


def test_the_release_digest_gate_waits_for_the_promoted_build_to_produce_a_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#712: with an expected data-engine digest, the identity is a verdict. The first
    polls still show the previous build (its run is the newest until the boot canary
    lands); the gate waits, then passes on the first body that names the release's digest."""
    old = {
        "status": "ok",
        "git_sha": "v0.0.47",
        "data_engine_parser": "p:v8",
        "data_engine_git_sha": "v0.0.46",
        "data_engine_image_digest": "sha256:" + "0" * 64,
    }
    new = dict(old, data_engine_git_sha="v0.0.47", data_engine_image_digest="sha256:" + "1" * 64)
    responses = _responses((200, json.dumps(old)), (200, json.dumps(old)), (200, json.dumps(new)))
    naps: list[float] = []
    exit_code = check_health(
        URL,
        expected_version="v0.0.47",
        max_attempts=5,
        http_get=responses,
        sleep=naps.append,
        expected_data_engine_digest="sha256:" + "1" * 64,
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "matches the release (attempt 3)" in out
    assert len(naps) == 2
    # the lines after the verdict describe the body the verdict was reached on: the new
    # build, not the first poll's (staging v0.0.47 reported the previous build here)
    assert "data engine build v0.0.47 (sha256:" + "1" * 64 + ") produced the newest run" in out
    assert "v0.0.46" not in out.split("matches the release")[1]


def test_the_release_digest_gate_is_red_when_the_build_never_shows(
    capsys: pytest.CaptureFixture[str],
) -> None:
    old = {
        "status": "ok",
        "git_sha": "v0.0.47",
        "data_engine_parser": "p:v8",
        "data_engine_git_sha": "v0.0.46",
        "data_engine_image_digest": "sha256:" + "0" * 64,
    }
    responses = _responses(*([(200, json.dumps(old))] * 4))
    exit_code = check_health(
        URL,
        expected_version="v0.0.47",
        max_attempts=3,
        http_get=responses,
        sleep=lambda _: None,
        expected_data_engine_digest="sha256:" + "1" * 64,
    )
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "DATA ENGINE MISMATCH" in err and "after 3 attempts" in err and "#712" in err


def test_without_an_expected_digest_the_identity_stays_a_report(capsys: pytest.CaptureFixture[str]) -> None:
    body = {
        "status": "ok",
        "git_sha": "v0.0.47",
        "data_engine_parser": "p:v8",
        "data_engine_git_sha": "v0.0.46",
        "data_engine_image_digest": "sha256:" + "0" * 64,
    }
    assert (
        check_health(
            URL, expected_version="v0.0.47", http_get=_responses((200, json.dumps(body))), sleep=lambda _: None
        )
        == 0
    )
    assert "MISMATCH" not in capsys.readouterr().err


def test_the_tag_resolver_reads_the_registry_digest_and_refuses_junk() -> None:
    """#712: the digest the gate requires is the one the registry names for the tag, read
    through `infra2_sdk.release.resolve_image_digest` (SDK 1.5.0) -- anonymous pull token,
    then a HEAD on the manifest with that bearer. Every way the registry fails to name a
    digest is the one RuntimeError the CLI prints as its red."""
    from infra2_sdk._transport import HttpResponse

    resolve = load_tool("health_check").resolve_data_engine_digest
    digest = "sha256:" + "a" * 64
    seen: list[tuple[str, str, dict[str, str]]] = []

    def registry(head_digest: str, *, manifest_status: int = 200):
        def transport(method: str, url: str, headers, body) -> HttpResponse:
            seen.append((method, url, dict(headers)))
            if url.startswith("https://ghcr.io/token?"):
                return HttpResponse(200, {}, b'{"token": "anon"}')
            return HttpResponse(manifest_status, {"docker-content-digest": head_digest}, b"")

        return transport

    assert resolve("v0.0.47", transport=registry(digest)) == digest
    token, manifest = seen
    assert token[0] == "GET" and token[1].endswith("scope=repository:wangzitian0/truealpha-data-engine:pull")
    assert manifest[0] == "HEAD" and manifest[1].endswith("/v2/wangzitian0/truealpha-data-engine/manifests/v0.0.47")
    assert manifest[2]["Authorization"] == "Bearer anon"

    with pytest.raises(RuntimeError, match="no usable digest"):
        resolve("v0.0.47", transport=registry("sha256:nope"))
    with pytest.raises(RuntimeError, match="no usable digest"):
        resolve("v0.0.99", transport=registry("", manifest_status=404))
    # A reference that is not a tag never reaches the registry and is refused the same way.
    seen.clear()
    with pytest.raises(RuntimeError, match="no usable digest"):
        resolve("not a tag", transport=registry(digest))
    assert seen == []


def test_the_cli_turns_a_tag_into_a_digest_expectation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = load_tool("health_check")
    monkeypatch.setattr(tool, "resolve_data_engine_digest", lambda tag: "sha256:" + "b" * 64)
    seen: dict[str, object] = {}

    def fake_check(url, **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(tool, "check_health", fake_check)
    assert tool.main([URL, "v0.0.47", "12", "--expect-data-engine-tag", "v0.0.47"]) == 0
    assert seen["expected_data_engine_digest"] == "sha256:" + "b" * 64 and seen["max_attempts"] == 12
    assert "names data-engine digest" in capsys.readouterr().out
