from pathlib import Path

from truealpha_runtime.testing import load_tool

check_pr_ready = load_tool("check_pr_ready")


def test_evaluate_pr_crash_error():
    payload = [
        {
            "author": {"login": "copilot-pull-request-reviewer"},
            "body": "Copilot encountered an error and was unable to review this pull request.",
            "state": "COMMENTED",
        }
    ]
    assert check_pr_ready.evaluate_pr(payload) is False


def test_evaluate_pr_crash_error_explicit_bypass():
    payload = [
        {
            "author": {"login": "copilot-pull-request-reviewer"},
            "body": "Copilot encountered an error and was unable to review this pull request.",
            "state": "COMMENTED",
        }
    ]
    assert check_pr_ready.evaluate_pr(payload, ignored_reviewers=["copilot-pull-request-reviewer"]) is True


def test_evaluate_pr_changes_requested():
    payload = {"state": "OPEN", "reviewDecision": "CHANGES_REQUESTED", "reviews": [{"state": "CHANGES_REQUESTED"}]}
    assert check_pr_ready.evaluate_pr(payload) is False


def test_evaluate_pr_approved():
    payload = {"state": "OPEN", "reviewDecision": "APPROVED", "reviews": [{"state": "APPROVED"}]}
    assert check_pr_ready.evaluate_pr(payload) is True


def test_anti_exclusion_invariant_fails(monkeypatch, tmp_path):
    # Create a mock allowlist file
    allowlist_file = tmp_path / "some_allowlist.json"
    allowlist_file.touch()

    def mock_rglob(self, pattern):
        if pattern == "*allowlist*.json":
            return [allowlist_file]
        return []

    # Patch Path.rglob to return our mock file
    monkeypatch.setattr(Path, "rglob", mock_rglob)
    assert check_pr_ready.check_anti_exclusion_invariant() is False


def test_anti_exclusion_invariant_passes(monkeypatch):
    def mock_rglob(self, pattern):
        return []

    monkeypatch.setattr(Path, "rglob", mock_rglob)
    assert check_pr_ready.check_anti_exclusion_invariant() is True
