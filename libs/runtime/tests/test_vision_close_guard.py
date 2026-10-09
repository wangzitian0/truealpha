"""Tests for tools/vision_close_guard.py — #1174.

A closing keyword that names a `scope:vision` issue closes it without the
deployed evidence that Rule 6 requires. GitHub acts on negated text too:
PR #730 closed #56 with "It does not close #56". Two checks guard the gap:
a PR check that blocks any closing keyword naming a vision issue, and a
reopen step that reverses a vision close with no evidence comment.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from truealpha_runtime.testing import load_tool

_module = load_tool("vision_close_guard")
GuardError = _module.GuardError
REPO = _module.REPO
check_pr_body = _module.check_pr_body
pull_request_body = _module.pull_request_body
reopen_verdict = _module.reopen_verdict
run_check_pr = _module.run_check_pr
run_reopen = _module.run_reopen

VISION = "scope:vision"
EVIDENCE = "## Deployed call site\n`svc.route()` at dagster_defs.py:12\n\n## Real-data output\n| rows | 42 |\n"
_COMMENTS_PATH = re.compile(
    r"^/repos/(?P<repo>[^/]+/[^/]+)/issues/(?P<number>[0-9]+)"
    r"/comments\?per_page=100&page=(?P<page>[0-9]+)$"
)
_ISSUE_PATH = re.compile(r"^/repos/(?P<repo>[^/]+/[^/]+)/issues/(?P<number>[0-9]+)$")


def fake_gh(
    labels: dict[tuple[str, int], Sequence[str]],
    *,
    comments: dict[tuple[str, int], list[list[str]]] | None = None,
    touched: list[str] | None = None,
) -> Callable[[str], str]:
    """A `gh api` stand-in for the two reads the guard makes.

    `labels` maps (repo, issue number) to label names. `comments` maps the same key
    to pages of comment bodies, served 100 per page as GitHub serves them.
    """

    def gh_api(path: str) -> str:
        if touched is not None:
            touched.append(path)
        if match := _COMMENTS_PATH.match(path):
            key = (match["repo"], int(match["number"]))
            pages = (comments or {}).get(key, [])
            page = int(match["page"])
            bodies = pages[page - 1] if page <= len(pages) else []
            return json.dumps([{"body": body} for body in bodies])
        match = _ISSUE_PATH.match(path)
        if match is None:
            raise AssertionError(f"unexpected gh api path {path!r}")
        key = (match["repo"], int(match["number"]))
        return json.dumps({"labels": [{"name": name} for name in labels[key]]})

    return gh_api


def _labels_of(
    labels: dict[tuple[str, int], Sequence[str]],
) -> Callable[[str, int], Sequence[str]]:
    return lambda repo, number: labels[(repo, number)]


VISION_56 = {(REPO, 56): [VISION], (REPO, 1159): ["type:quality"]}


def test_a_negated_closing_keyword_on_a_vision_issue_fails() -> None:
    """GitHub closes #56 on "does not close #56". The check must not read the negation."""
    verdict = check_pr_body(
        "This PR does not close #56. It only prepares the work.",
        labels_of=_labels_of(VISION_56),
        default_repo=REPO,
    )
    assert verdict.passed is False
    assert "#56" in verdict.reason
    assert "Refs #56" in verdict.reason, "the verdict must name the fix"


def test_a_refs_reference_to_a_vision_issue_passes() -> None:
    verdict = check_pr_body(
        "Refs #56. This PR does not close the issue.",
        labels_of=_labels_of(VISION_56),
        default_repo=REPO,
    )
    assert verdict.passed is True


def test_a_closing_keyword_on_a_non_vision_issue_passes() -> None:
    verdict = check_pr_body(
        "Closes #1159",
        labels_of=_labels_of(VISION_56),
        default_repo=REPO,
    )
    assert verdict.passed is True


@pytest.mark.parametrize(
    "text",
    [
        "close #56",
        "closes #56",
        "closed #56",
        "fix #56",
        "fixes #56",
        "fixed #56",
        "resolve #56",
        "resolves #56",
        "resolved #56",
        "Closes: #56",
        "CLOSES #56",
        "Fixes:#56",
    ],
)
def test_every_github_closing_keyword_spelling_is_a_reference(text: str) -> None:
    """The GitHub keyword list, written out here and not read from the tool."""
    verdict = check_pr_body(text, labels_of=_labels_of(VISION_56), default_repo=REPO)
    assert verdict.passed is False, text


def test_a_partial_closing_phrase_still_names_its_issue() -> None:
    """PR #360 closed #26 with "Closes #26's other tracked gap". The apostrophe ends the number."""
    verdict = check_pr_body(
        "Closes #56's other tracked gap",
        labels_of=_labels_of(VISION_56),
        default_repo=REPO,
    )
    assert verdict.passed is False


def test_a_word_that_only_contains_a_keyword_is_not_a_reference() -> None:
    verdict = check_pr_body(
        "This encloses #56 and prefixes #56.",
        labels_of=_labels_of(VISION_56),
        default_repo=REPO,
    )
    assert verdict.passed is True


def test_a_keyword_naming_another_repository_is_judged_by_that_repository() -> None:
    labels = {(REPO, 56): ["type:quality"], ("other/repo", 7): [VISION]}
    verdict = check_pr_body(
        "Fixes other/repo#7",
        labels_of=_labels_of(labels),
        default_repo=REPO,
    )
    assert verdict.passed is False
    assert "other/repo#7" in verdict.reason


def test_the_check_blocks_with_exit_1_and_passes_with_exit_0(
    capsys: pytest.CaptureFixture[str],
) -> None:
    blocked = run_check_pr("does not close #56", gh_api=fake_gh(VISION_56), default_repo=REPO)
    assert blocked == 1
    assert "blocked" in capsys.readouterr().err

    passed = run_check_pr("Refs #56", gh_api=fake_gh(VISION_56), default_repo=REPO)
    assert passed == 0


def test_a_failed_label_lookup_fails_closed_with_exit_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unreachable(path: str) -> str:
        raise GuardError(f"gh api {path} failed: HTTP 503")

    exit_code = run_check_pr("Closes #56", gh_api=unreachable, default_repo=REPO)
    assert exit_code == 2
    assert "could not reach a verdict" in capsys.readouterr().err


def test_a_pull_request_without_a_body_is_empty_text() -> None:
    assert pull_request_body({"pull_request": {"body": None}}) == ""


def test_an_event_without_a_pull_request_fails_closed() -> None:
    with pytest.raises(GuardError):
        pull_request_body({"issue": {"number": 56}})


def test_a_vision_close_without_evidence_is_reopened() -> None:
    verdict = reopen_verdict([VISION], comments=lambda: ["a note, no evidence"])
    assert verdict.reopen is True
    assert "Deployed call site" in verdict.reason
    assert "Real-data output" in verdict.reason


def test_a_vision_close_with_an_evidence_comment_is_not_reopened() -> None:
    verdict = reopen_verdict([VISION], comments=lambda: [EVIDENCE])
    assert verdict.reopen is False


def test_both_headings_must_sit_in_one_comment() -> None:
    verdict = reopen_verdict(
        [VISION],
        comments=lambda: ["## Deployed call site\nroute", "## Real-data output\nrows"],
    )
    assert verdict.reopen is True


def test_a_heading_word_in_prose_is_not_evidence() -> None:
    verdict = reopen_verdict(
        [VISION],
        comments=lambda: ["We wrote Deployed call site and Real-data output in prose."],
    )
    assert verdict.reopen is True


def test_crlf_line_endings_still_count_as_headings() -> None:
    crlf = EVIDENCE.replace("\n", "\r\n")
    verdict = reopen_verdict([VISION], comments=lambda: [crlf])
    assert verdict.reopen is False


def test_a_non_vision_close_is_not_touched() -> None:
    def must_not_read_comments() -> list[str]:
        raise AssertionError("a non-vision close must not read comments")

    verdict = reopen_verdict(["type:quality"], comments=must_not_read_comments)
    assert verdict.reopen is False


def test_a_non_vision_close_makes_no_reopen_and_no_comment_call() -> None:
    touched: list[str] = []
    reopened: list[tuple[int, str]] = []
    labels = {(REPO, 1159): ["type:quality"]}
    exit_code = run_reopen(
        1159,
        gh_api=fake_gh(labels, touched=touched),
        reopen=lambda issue, reason: reopened.append((issue, reason)),
    )
    assert exit_code == 0
    assert reopened == []
    assert touched and not any("/comments" in path for path in touched)


def test_a_vision_close_without_evidence_reopens_once() -> None:
    labels = {(REPO, 1174): [VISION]}
    reopened: list[tuple[int, str]] = []
    exit_code = run_reopen(
        1174,
        gh_api=fake_gh(labels, comments={(REPO, 1174): [["no evidence yet"]]}),
        reopen=lambda issue, reason: reopened.append((issue, reason)),
    )
    assert exit_code == 0
    assert len(reopened) == 1
    assert reopened[0][0] == 1174
    assert "Deployed call site" in reopened[0][1]


def test_evidence_on_a_later_comments_page_counts() -> None:
    """A long issue has more than 100 comments; reading one page would reopen a closed issue."""
    labels = {(REPO, 1174): [VISION]}
    filler = ["status note"] * 100
    reopened: list[tuple[int, str]] = []
    exit_code = run_reopen(
        1174,
        gh_api=fake_gh(labels, comments={(REPO, 1174): [filler, [EVIDENCE]]}),
        reopen=lambda issue, reason: reopened.append((issue, reason)),
    )
    assert exit_code == 0
    assert reopened == [], "the evidence comment on page 2 must count"


def test_a_failed_lookup_on_reopen_reopens_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    reopened: list[tuple[int, str]] = []

    def unreachable(path: str) -> str:
        raise GuardError(f"gh api {path} failed: HTTP 503")

    exit_code = run_reopen(
        1174,
        gh_api=unreachable,
        reopen=lambda issue, reason: reopened.append((issue, reason)),
    )
    assert exit_code == 2
    assert reopened == []
    assert "could not reach a verdict" in capsys.readouterr().err


def test_the_cli_reads_the_body_from_the_event_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The workflow hands the tool a GitHub event file, not an interpolated body.

    The body "Refs #56" needs no lookup, so the check passes without a network call.
    A payload without a pull_request object fails closed with exit 2.
    """
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"body": "Refs #56"}}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    assert _module.main(["check-pr"]) == 0

    event.write_text(json.dumps({"issue": {"number": 56}}), encoding="utf-8")
    assert _module.main(["check-pr"]) == 2
