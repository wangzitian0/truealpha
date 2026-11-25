"""Unit and manifest tests for truealpha_tools.health_probe (#1647 F3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from truealpha_tools.health_probe import main

pytestmark = pytest.mark.gate

REPO_ROOT = Path(__file__).resolve().parents[3]


def _stub(responses: list[tuple[int, dict[str, object]]]):
    calls = 0

    def _get(_url: str) -> tuple[int, str]:
        nonlocal calls
        idx = min(calls, len(responses) - 1)
        calls += 1
        code, payload = responses[idx]
        return code, json.dumps(payload)

    return _get


def test_health_probe_fails_when_data_engine_tag_mismatches(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(
        [
            "--url",
            "https://fake.invalid/api/health",
            "--expected-version",
            "v0.3.5",
            "--expect-data-engine-version",
            "v0.3.5",
            "--max-attempts",
            "2",
            "--interval-seconds",
            "0",
        ],
        http_get=_stub([(200, {"status": "healthy", "version": "v0.3.5", "data_engine_git_sha": "v0.3.2"})]),
        sleep=lambda _s: None,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "data_engine_git_sha='v0.3.2'" in err
    assert "expected 'v0.3.5'" in err


@pytest.mark.parametrize("engine_value", [None, "", "unknown"])
def test_health_probe_fails_when_data_engine_tag_missing_or_unknown(
    engine_value: object, capsys: pytest.CaptureFixture[str]
) -> None:
    payload: dict[str, object] = {"status": "healthy", "version": "v0.3.5"}
    if engine_value is not None:
        payload["data_engine_git_sha"] = engine_value
    rc = main(
        [
            "--url",
            "https://fake.invalid/api/health",
            "--expected-version",
            "v0.3.5",
            "--expect-data-engine-version",
            "v0.3.5",
            "--max-attempts",
            "1",
        ],
        http_get=_stub([(200, payload)]),
        sleep=lambda _s: None,
    )
    assert rc == 1
    assert "data_engine_git_sha=" in capsys.readouterr().err


def test_health_probe_retries_until_data_engine_tag_matches(capsys: pytest.CaptureFixture[str]) -> None:
    sleeps: list[float] = []
    rc = main(
        [
            "--url",
            "https://fake.invalid/api/health",
            "--expected-version",
            "v0.3.5",
            "--expect-data-engine-version",
            "v0.3.5",
            "--max-attempts",
            "3",
            "--interval-seconds",
            "2",
        ],
        http_get=_stub(
            [
                (200, {"status": "healthy", "version": "v0.3.5", "data_engine_git_sha": "v0.3.2"}),
                (200, {"status": "healthy", "version": "v0.3.5", "data_engine_git_sha": "v0.3.5"}),
            ]
        ),
        sleep=sleeps.append,
    )
    assert rc == 0
    assert sleeps == [2.0]
    out = capsys.readouterr().out
    assert "attempts" not in out or "2 attempt(s)" in out
    assert "data_engine_git_sha=v0.3.5" in out


def test_health_probe_fails_when_app_version_mismatches(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(
        [
            "--url",
            "https://fake.invalid/api/health",
            "--expected-version",
            "v0.3.5",
            "--expect-data-engine-version",
            "v0.3.5",
            "--max-attempts",
            "1",
        ],
        http_get=_stub([(200, {"status": "healthy", "version": "v0.3.4", "data_engine_git_sha": "v0.3.5"})]),
        sleep=lambda _s: None,
    )
    assert rc == 1
    assert "version='v0.3.4'" in capsys.readouterr().err


def test_runtime_hooks_manifest_checks_data_engine_git_sha_on_smoke_and_health() -> None:
    raw = json.loads((REPO_ROOT / "runtime-hooks.manifest.json").read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in raw["checks"]}
    for check_id in ("smoke", "health"):
        cmd = " ".join(by_id[check_id]["argv"])
        assert "truealpha_tools.health_probe" in cmd, check_id
        assert '--expect-data-engine-version "$APP_VERSION_REF"' in cmd, check_id
