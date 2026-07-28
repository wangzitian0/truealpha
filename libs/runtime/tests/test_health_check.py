"""Tests for tools/health_check.py -- confirms TrueAlpha's deployed release is
actually live, using infra2_sdk.deploy_health.poll_until_healthy's shared polling
algorithm against llm-service's {"status": "ok", "git_sha": ...} convention."""

from __future__ import annotations

import json

import pytest

from tools.health_check import check_health

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
