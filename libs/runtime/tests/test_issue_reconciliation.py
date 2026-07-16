from __future__ import annotations

import ast
import hashlib
import json
import re
import textwrap
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parents[3]
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "issue_reconciliation.v1.json"
BATCH_PATH = ROOT / "governance" / "batches" / "D12-vision-issue-reconciliation.v1.json"
BATCH_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "sync-batch-issues.yml"
STANDALONE_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "sync-standalone-issue.yml"


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _route_standalone(case: dict[str, Any]) -> str:
    action = case["issue_action"]
    if action in {"managed-by-batch", "keep-open"}:
        return "skip"
    if action != "complete-on-merge":
        return "reject"
    issue_match = re.fullmatch(r"#([1-9][0-9]*)", str(case["work_issue"]))
    if issue_match is None:
        return "reject"
    issue = issue_match.group(1)
    if case["work_identity"] != f"standalone-{issue}":
        return "reject"
    return f"close-{issue}"


def _lifecycle(status: str) -> tuple[str, str | None]:
    return {
        "done": ("closed", "completed"),
        "cancelled": ("closed", "not_planned"),
    }.get(status, ("open", None))


class _Runner:
    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self.responses = responses
        self.calls: list[tuple[list[str], str | None]] = []

    def run(self, arguments: list[str], *, input: str | None = None, **_: object) -> SimpleNamespace:
        self.calls.append((arguments, input))
        return self.responses.pop(0)


class _Clock:
    def __init__(self) -> None:
        self.sleeps: list[int] = []

    @staticmethod
    def time() -> int:
        return 100

    def sleep(self, delay: int) -> None:
        self.sleeps.append(delay)


def _response(returncode: int, *, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _load_workflow_api(runner: _Runner, clock: _Clock) -> tuple[type[RuntimeError], Callable[..., str]]:
    workflow = BATCH_WORKFLOW_PATH.read_text(encoding="utf-8")
    script = textwrap.dedent(workflow.split("python3 - <<'PY'\n", 1)[1].split("\n          PY", 1)[0])
    tree = ast.parse(script)
    definitions: list[ast.stmt] = [
        node
        for node in tree.body
        if (isinstance(node, ast.ClassDef) and node.name == "ApiFailure")
        or (isinstance(node, ast.FunctionDef) and node.name == "api")
    ]
    namespace: dict[str, Any] = {"re": re, "subprocess": runner, "time": clock}
    exec(compile(ast.Module(body=definitions, type_ignores=[]), str(BATCH_WORKFLOW_PATH), "exec"), namespace)
    return cast(type[RuntimeError], namespace["ApiFailure"]), cast(Callable[..., str], namespace["api"])


def test_frozen_corpus_matches_batch_manifest() -> None:
    manifest = json.loads(BATCH_PATH.read_text(encoding="utf-8"))
    digest = hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()

    assert manifest["corpus"]["manifest_path"] == str(FIXTURE_PATH.relative_to(ROOT))
    assert manifest["corpus"]["sha256"] == digest


def test_standalone_routes_skip_batch_metadata_before_key_validation() -> None:
    cases = _fixture()["standalone_routes"]

    assert {_route_standalone(case) for case in cases} >= {"skip", "reject", "close-9001"}
    for case in cases:
        assert _route_standalone(case) == case["expected"]


def test_batch_lifecycle_is_total_and_deterministic() -> None:
    cases = _fixture()["batch_lifecycle"]

    for case in cases:
        assert _lifecycle(case["status"]) == (case["expected_state"], case["expected_reason"])


def test_parity_export_uses_authoritative_labels_not_search_results() -> None:
    cases = _fixture()["parity_exports"]

    for case in cases:
        included = case["authoritative_issue_has_scope_label"]
        expected = case["expected"].startswith("include")
        assert included is expected


def test_concurrent_edit_never_allows_a_stale_patch() -> None:
    cases = _fixture()["concurrency"]

    for case in cases:
        result = "retry-or-fail-visible" if case["etag_changed_before_patch"] else "patch"
        assert result == case["expected"]


def test_api_resilience_cases_fail_visible_and_preserve_denominators() -> None:
    cases = {case["name"]: case for case in _fixture()["api_resilience"]}

    pagination = cases["pagination_preserves_every_issue"]
    assert sum(pagination["page_sizes"]) == pagination["expected_issue_count"]
    assert pagination["expected"] == "complete"

    transient = cases["transient_server_error_retries"]
    assert transient["statuses"][-1] == 200
    assert any(status >= 500 for status in transient["statuses"][:-1])
    runner = _Runner([_response(1, stderr="upstream failed (HTTP 502)"), _response(0, stdout="ok")])
    clock = _Clock()
    _, api = _load_workflow_api(runner, clock)
    assert api(["endpoint"]) == "ok"
    assert clock.sleeps == [1]

    rate_limit = cases["rate_limit_waits_for_reset"]
    assert rate_limit["statuses"] == [403, 200]
    assert rate_limit["rate_limit_remaining"] == 0
    runner = _Runner(
        [
            _response(1, stderr="rate limited (HTTP 403)"),
            _response(0, stdout="101\n"),
            _response(0, stdout="ok"),
        ]
    )
    clock = _Clock()
    _, api = _load_workflow_api(runner, clock)
    assert api(["endpoint"]) == "ok"
    assert runner.calls[1][0] == ["gh", "api", "rate_limit", "--jq", ".resources.core.reset"]
    assert clock.sleeps == [2]

    partial = cases["partial_patch_failure_never_reports_parity"]
    assert partial["expected"] == "fail-visible"
    runner = _Runner([_response(1, stderr="patch failed (HTTP 422)")])
    clock = _Clock()
    api_failure, api = _load_workflow_api(runner, clock)
    with pytest.raises(api_failure, match="patch failed"):
        api(["--method", "PATCH", "endpoint"], attempts=1)
    assert clock.sleeps == []

    rerun = cases["idempotent_rerun_writes_nothing"]
    assert rerun["initial_state_matches"] is True
    assert rerun["expected_patch_count"] == 0


def test_workflows_compile_and_enforce_reconciliation_contract() -> None:
    batch_workflow = BATCH_WORKFLOW_PATH.read_text(encoding="utf-8")
    standalone_workflow = STANDALONE_WORKFLOW_PATH.read_text(encoding="utf-8")
    batch_script = batch_workflow.split("python3 - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]
    standalone_script = standalone_workflow.split("python3 - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]

    compile(textwrap.dedent(batch_script), str(BATCH_WORKFLOW_PATH), "exec")
    compile(textwrap.dedent(standalone_script), str(STANDALONE_WORKFLOW_PATH), "exec")

    assert "group: vision-issue-reconciliation-${{ github.repository }}" in batch_workflow
    assert "cancel-in-progress: false" in batch_workflow
    assert "If-Match: {etag}" in batch_workflow
    assert "error.status != 412" in batch_workflow
    assert "attempts=1" not in batch_workflow
    assert '"done": ("closed", "completed")' in batch_workflow
    assert '"cancelled": ("closed", "not_planned")' in batch_workflow
    assert 'graph["gates"][str(batch["owner_gate"])]["milestone"]' in batch_workflow
    assert "rate_limit" in batch_workflow
    assert "time.sleep(delay)" in batch_workflow
    assert "issues?state=all&per_page=100" in batch_workflow
    assert "labels=scope" not in batch_workflow
    assert 'any(label["name"] == "scope:vision" for label in issue["labels"])' in batch_workflow
    assert "refetched_pages = json.loads" in batch_workflow

    action_index = standalone_workflow.index('fields["Issue-Action"] = action_values[0]')
    work_key_index = standalone_workflow.index('for key in ("Work-Issue", "Work-Key")')
    assert action_index < work_key_index
    assert '{"managed-by-batch", "keep-open"}' in standalone_workflow
    assert 'fields["Issue-Action"] != "complete-on-merge"' in standalone_workflow
