"""Tests for `staging_issue`: one issue in step with the staging line (#1468 F1)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from truealpha_tools import staging_issue
from truealpha_tools.staging_line import StagingRed

REPO = "wangzitian0/truealpha"
HEAD = "c" * 40
LABEL = "walk-release / walk / Walk the deployed surface"
TITLE = f"[truealpha] Staging red since v0.2.32: {LABEL}"
LINE = (
    "open; newest release v0.2.34 walked green; no two releases in a row failed at one step since the last green walk"
)


def red(newest: str = "v0.2.33") -> StagingRed:
    return StagingRed(
        since="v0.2.32",
        label=LABEL,
        failed_tags=("v0.2.32", newest),
        newest_failed=newest,
        run=39522977807,
        last_green_walk=datetime(2026, 10, 20, 12, 32, tzinfo=UTC),
    )


class Desk:
    """A fake `gh issue` CLI. It records each call."""

    def __init__(
        self, *, open_issues: list[dict] | None = None, body: str = "", comments: list[str] | None = None
    ) -> None:
        self.open_issues = open_issues or []
        self.body = body
        self.comments = comments or []
        self.calls: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(list(args))
        verb = args[2]
        if verb == "list":
            return json.dumps(self.open_issues)
        if verb == "view":
            return json.dumps({"body": self.body, "comments": [{"body": text} for text in self.comments]})
        if verb == "create":
            return "https://github.com/wangzitian0/truealpha/issues/9999\n"
        return ""

    def verbs(self) -> list[str]:
        return [call[2] for call in self.calls]

    def arg(self, verb: str, flag: str) -> str:
        call = next(c for c in self.calls if c[2] == verb)
        return call[call.index(flag) + 1]


def test_the_title_names_the_first_tag_and_the_step() -> None:
    assert red().title == TITLE


def test_no_open_issue_opens_one_with_the_failing_step_the_run_and_the_way_out() -> None:
    desk = Desk()
    out = staging_issue.reconcile(REPO, red(), LINE, HEAD, desk)
    assert desk.verbs() == ["list", "create"]
    assert desk.arg("create", "--title") == TITLE
    body = desk.arg("create", "--body")
    assert LABEL in body
    assert "v0.2.32, v0.2.33" in body
    assert f"https://github.com/{REPO}/actions/runs/39522977807" in body
    assert "tools/cut_release.sh" in body and "walk-release.yml" in body and "tools/auto_release.py" in body
    assert body.splitlines()[-1] == staging_issue.state_marker(red())
    assert out == "opened https://github.com/wangzitian0/truealpha/issues/9999"


def test_an_open_issue_gets_a_comment_and_no_second_issue_opens() -> None:
    desk = Desk(open_issues=[{"number": 41, "title": TITLE}], body="older text")
    out = staging_issue.reconcile(REPO, red(), LINE, HEAD, desk)
    assert desk.verbs() == ["list", "view", "comment"]
    assert "create" not in desk.verbs()
    assert desk.calls[2][3] == "41"
    comment = desk.arg("comment", "--body")
    assert HEAD[:8] in comment and LABEL in comment
    assert out == "commented on #41"


def test_the_oldest_open_issue_gets_the_comment() -> None:
    desk = Desk(open_issues=[{"number": 90, "title": TITLE}, {"number": 41, "title": TITLE}])
    staging_issue.reconcile(REPO, red(), LINE, HEAD, desk)
    assert desk.calls[2][3] == "41"


def test_a_state_that_the_body_holds_adds_no_comment() -> None:
    desk = Desk(open_issues=[{"number": 41, "title": TITLE}], body=f"text\n{staging_issue.state_marker(red())}")
    out = staging_issue.reconcile(REPO, red(), LINE, HEAD, desk)
    assert desk.verbs() == ["list", "view"]
    assert out == "#41 already holds this state"


def test_a_state_that_an_old_comment_holds_adds_no_comment_even_after_a_human_comment() -> None:
    desk = Desk(
        open_issues=[{"number": 41, "title": TITLE}],
        body="text",
        comments=[f"held\n{staging_issue.state_marker(red())}", "I am looking at this.", "Walk flake? Checking."],
    )
    staging_issue.reconcile(REPO, red(), LINE, HEAD, desk)
    assert desk.verbs() == ["list", "view"]


def test_a_new_newest_failed_release_adds_a_comment() -> None:
    desk = Desk(open_issues=[{"number": 41, "title": TITLE}], body=staging_issue.state_marker(red("v0.2.33")))
    staging_issue.reconcile(REPO, red("v0.2.34"), LINE, HEAD, desk)
    assert desk.verbs() == ["list", "view", "comment"]


def test_an_issue_without_the_title_prefix_is_not_a_staging_red_issue() -> None:
    desk = Desk(open_issues=[{"number": 5, "title": "Staging red since last week: who knows"}])
    staging_issue.reconcile(REPO, red(), LINE, HEAD, desk)
    assert desk.verbs() == ["list", "create"]


def test_an_open_line_closes_each_open_issue_and_says_the_state() -> None:
    desk = Desk(open_issues=[{"number": 41, "title": TITLE}, {"number": 43, "title": TITLE}])
    out = staging_issue.reconcile(REPO, None, LINE, HEAD, desk)
    assert desk.verbs() == ["list", "close", "close"]
    assert desk.arg("close", "--comment") == f"The staging line is open again: {LINE}."
    assert out == "closed #41, #43"


def test_an_open_line_with_no_open_issue_does_nothing() -> None:
    desk = Desk()
    assert staging_issue.reconcile(REPO, None, LINE, HEAD, desk) == "none open"
    assert desk.verbs() == ["list"]


def test_an_issue_list_that_is_no_list_fails_closed() -> None:
    with pytest.raises(ValueError, match="unexpected issue list payload"):
        staging_issue.open_red_issues(REPO, lambda args: "{}")


def test_the_issue_list_is_read_with_the_limit_and_without_the_search_index() -> None:
    desk = Desk()
    staging_issue.open_red_issues(REPO, desk)
    call = desk.calls[0]
    assert call[call.index("--limit") + 1] == str(staging_issue.ISSUE_LIST_LIMIT)
    assert "--search" not in call


def test_a_list_that_fills_the_limit_fails_closed_because_an_older_issue_may_be_cut_off() -> None:
    full = [{"number": n, "title": f"issue {n}"} for n in range(staging_issue.ISSUE_LIST_LIMIT)]
    with pytest.raises(ValueError, match="reach the list limit"):
        staging_issue.open_red_issues(REPO, lambda args: json.dumps(full))
