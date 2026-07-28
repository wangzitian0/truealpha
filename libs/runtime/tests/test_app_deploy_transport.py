"""Tests for tools/app_deploy_transport.py -- the thin wrapper that validates a raw
request under TrueAlpha's sender policy, then delegates dispatch to
infra2_sdk.dispatch.dispatch_and_wait (the shared infra2-receiver-boundary
implementation; its own watermark/ambiguity-guard/log-verification behavior is
covered by infra2-sdk's own test suite, not re-tested here)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from infra2_sdk.dispatch import INFRA_REPOSITORY, ReceiverRun

import tools.app_deploy_transport as transport

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_PATH = REPO_ROOT / "libs/runtime/tests/fixtures/infra_boundary.v1.json"


def _valid_request() -> dict:
    corpus = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return next(case for case in corpus["cases"] if case["expected"] == "accepted")["request"]


def test_dispatch_and_wait_rejects_an_invalid_request_before_any_api_call() -> None:
    calls: list[str] = []

    def api(method: str, path: str, body: object = None) -> object:
        calls.append(method)
        return {"workflow_runs": []}

    with pytest.raises(ValueError, match="service must be truealpha/app"):
        transport.dispatch_and_wait(
            {**_valid_request(), "service": "not-truealpha/app"},
            api=api,
            fetch_logs=lambda run_id: b"",
        )
    assert calls == []


def test_dispatch_and_wait_delegates_the_validated_request_to_the_sdk() -> None:
    request = _valid_request()
    calls: list[tuple[str, str, object]] = []
    run_lists = iter(
        [
            {"workflow_runs": [{"id": 100}]},
            {
                "workflow_runs": [
                    {
                        "id": 101,
                        "status": "completed",
                        "conclusion": "success",
                        "html_url": f"https://github.com/{INFRA_REPOSITORY}/actions/runs/101",
                    }
                ]
            },
        ]
    )

    def api(method: str, path: str, body: object = None) -> object:
        calls.append((method, path, body))
        return next(run_lists) if method == "GET" else None

    result = transport.dispatch_and_wait(
        request,
        api=api,
        fetch_logs=lambda run_id: request["request_id"].encode(),
        sleep=lambda _: None,
        max_attempts=1,
    )

    assert result == ReceiverRun(run_id=101, url=f"https://github.com/{INFRA_REPOSITORY}/actions/runs/101")
    dispatch = next(call for call in calls if call[0] == "POST")
    assert dispatch[2]["client_payload"]["service"] == "truealpha/app"


def test_cli_requires_the_token_env(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("INFRA2_PAT", raising=False)
    assert transport.main([]) == 1
    assert "INFRA2_PAT is required" in capsys.readouterr().err


def test_cli_requires_positive_timeout_and_poll_interval(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("INFRA2_PAT", "test-token")
    assert transport.main(["--timeout", "0"]) == 1
    assert "must be positive" in capsys.readouterr().err


def test_cli_rejects_a_non_object_stdin_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("INFRA2_PAT", "test-token")
    monkeypatch.setattr(transport.sys, "stdin", io.StringIO("[]"))
    assert transport.main(["--timeout", "5", "--poll-interval", "5"]) == 1
    assert "must be a JSON object" in capsys.readouterr().err


def test_cli_prints_the_receiver_receipt_on_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("INFRA2_PAT", "test-token")
    monkeypatch.setattr(transport.sys, "stdin", io.StringIO(json.dumps(_valid_request())))
    monkeypatch.setattr(
        transport,
        "dispatch_and_wait",
        lambda *args, **kwargs: ReceiverRun(run_id=101, url=f"https://github.com/{INFRA_REPOSITORY}/actions/runs/101"),
    )
    assert transport.main(["--timeout", "6", "--poll-interval", "5"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "receiver_run_id": 101,
        "receiver_run_url": f"https://github.com/{INFRA_REPOSITORY}/actions/runs/101",
    }


def test_cli_reports_a_dispatch_failure_to_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("INFRA2_PAT", "test-token")
    monkeypatch.setattr(transport.sys, "stdin", io.StringIO(json.dumps(_valid_request())))
    monkeypatch.setattr(
        transport,
        "dispatch_and_wait",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("receiver failed")),
    )
    assert transport.main(["--timeout", "5", "--poll-interval", "5"]) == 1
    assert "receiver failed" in capsys.readouterr().err
