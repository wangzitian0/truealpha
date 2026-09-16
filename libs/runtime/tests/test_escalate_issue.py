"""Tests for tools/escalate_issue.py — #876 (W3, W12).

The watchdogs used to open or comment on a tracking issue and never close it, so
#818 stayed open two days after the fault cleared. These pin both halves of the
lifecycle against a fake `gh` (no network): what is listed, what is written, and
what is refused. Where the workflows call the tool, and under which guards, is
pinned in test_ci_workflows.py.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from truealpha_runtime.testing import load_tool

_module = load_tool("escalate_issue")

REPO = "owner/repo"
TITLE = "main is red: ci-required failed on push"


class FakeGh:
    """Records every call; answers `issue list` from `listed`, writes from `fail`."""

    def __init__(
        self,
        listed: list[dict] | None = None,
        *,
        list_code: int = 0,
        list_out: str | None = None,
        fail: Sequence[str] = (),
    ) -> None:
        self.listed = listed or []
        self.list_code = list_code
        self.list_out = list_out
        self.fail = set(fail)
        self.calls: list[list[str]] = []

    def __call__(self, arguments: Sequence[str]) -> tuple[int, str, str]:
        arguments = list(arguments)
        self.calls.append(arguments)
        verb = arguments[1]
        if verb == "list":
            if self.list_code:
                return self.list_code, "", "API rate limit exceeded"
            return 0, self.list_out if self.list_out is not None else json.dumps(self.listed), ""
        if verb in self.fail:
            return 1, "", f"{verb}: HTTP 403 Resource not accessible by integration"
        return 0, "", ""

    def writes(self) -> list[list[str]]:
        return [call for call in self.calls if call[1] != "list"]


@pytest.fixture
def body(tmp_path: Path) -> str:
    path = tmp_path / "body.md"
    path.write_text("run: https://example.invalid/runs/1\n", encoding="utf-8")
    return str(path)


# --- open ----------------------------------------------------------------------


def test_open_files_the_issue_when_none_is_open(body: str) -> None:
    gh = FakeGh([])
    assert _module.escalate(TITLE, body, repo=REPO, gh=gh) == 0
    assert gh.writes() == [["issue", "create", "--repo", REPO, "--title", TITLE, "--body-file", body]]


def test_open_comments_on_the_open_issue_instead_of_filing_again(body: str) -> None:
    """#680: one open issue per breakage; reruns comment."""
    gh = FakeGh([{"number": 818, "title": TITLE}])
    assert _module.escalate(TITLE, body, repo=REPO, gh=gh) == 0
    assert gh.writes() == [["issue", "comment", "818", "--repo", REPO, "--body-file", body]]


def test_the_search_is_scoped_to_open_issues_and_the_exact_phrase(body: str) -> None:
    gh = FakeGh([])
    _module.escalate(TITLE, body, repo=REPO, gh=gh)
    listing = gh.calls[0]
    assert listing[:2] == ["issue", "list"]
    assert listing[listing.index("--state") + 1] == "open"
    assert listing[listing.index("--search") + 1] == f'in:title "{TITLE}"'
    assert "title" in listing[listing.index("--json") + 1].split(","), (
        "the listing must return titles, or equality cannot be checked"
    )


def test_a_superstring_title_is_a_different_issue(body: str) -> None:
    """GitHub's `in:title` search is a word match: the longer title comes back
    for the shorter query. Trusting the hit would comment on the wrong issue —
    and in resolve mode, close it."""
    longer = f"{TITLE} (flaky runner, not a code fault)"
    gh = FakeGh([{"number": 900, "title": longer}])
    assert _module.escalate(TITLE, body, repo=REPO, gh=gh) == 0
    assert [call[:2] for call in gh.writes()] == [["issue", "create"]], (
        "a superstring title was treated as this breakage's issue"
    )


def test_a_prefix_title_is_a_different_issue_too(body: str) -> None:
    """The mirror case: the search for a longer title can surface the shorter one."""
    gh = FakeGh([{"number": 901, "title": "deploy-freshness is red"}])
    title = "deploy-freshness is red: staging failed its standing checks"
    assert _module.escalate(title, body, repo=REPO, gh=gh) == 0
    assert [call[:2] for call in gh.writes()] == [["issue", "create"]]


def test_open_picks_the_oldest_exact_match_among_duplicates(body: str) -> None:
    """#687/#688 were two issues from one forced failure; the history must not split further."""
    gh = FakeGh([{"number": 688, "title": TITLE}, {"number": 687, "title": TITLE}])
    _module.escalate(TITLE, body, repo=REPO, gh=gh)
    assert gh.writes() == [["issue", "comment", "687", "--repo", REPO, "--body-file", body]]


@pytest.mark.parametrize(
    "gh",
    [FakeGh(list_code=1), FakeGh(list_out="<html>502</html>"), FakeGh(list_out='{"message": "Bad credentials"}')],
    ids=["gh-exits-non-zero", "not-json", "not-a-list"],
)
def test_a_failed_listing_never_falls_through_to_create(gh: FakeGh, body: str, capsys) -> None:  # noqa: ANN001
    """#680 review: a rate limit must not turn one breakage into duplicates. The
    run is already red, so the skip is a warning, not a second failure."""
    assert _module.escalate(TITLE, body, repo=REPO, gh=gh) == 0
    assert gh.writes() == [], "a failed listing wrote something"
    assert "::warning::issue listing failed" in capsys.readouterr().out


@pytest.mark.parametrize("verb", ["create", "comment"])
def test_a_failed_write_is_red(verb: str, body: str) -> None:
    listed = [] if verb == "create" else [{"number": 5, "title": TITLE}]
    gh = FakeGh(listed, fail=[verb])
    assert _module.escalate(TITLE, body, repo=REPO, gh=gh) == 1


# --- resolve -------------------------------------------------------------------


def test_resolve_comments_then_closes_the_open_issue(body: str) -> None:
    """The #818 case: a green run closes what the red one opened, saying why."""
    gh = FakeGh([{"number": 818, "title": TITLE}])
    assert _module.resolve(TITLE, body, repo=REPO, gh=gh) == 0
    assert gh.writes() == [
        ["issue", "comment", "818", "--repo", REPO, "--body-file", body],
        ["issue", "close", "818", "--repo", REPO, "--reason", "completed"],
    ]


def test_resolve_with_nothing_open_writes_nothing(body: str) -> None:
    """The normal green day."""
    gh = FakeGh([])
    assert _module.resolve(TITLE, body, repo=REPO, gh=gh) == 0
    assert gh.writes() == []


def test_resolve_never_closes_a_superstring_title(body: str) -> None:
    gh = FakeGh([{"number": 900, "title": f"{TITLE} (flaky runner, not a code fault)"}])
    assert _module.resolve(TITLE, body, repo=REPO, gh=gh) == 0
    assert gh.writes() == [], "resolve closed an issue whose title only contains this one"


def test_resolve_closes_every_exact_duplicate(body: str) -> None:
    gh = FakeGh(
        [
            {"number": 688, "title": TITLE},
            {"number": 999, "title": f"{TITLE} (flaky)"},
            {"number": 687, "title": TITLE},
        ]
    )
    assert _module.resolve(TITLE, body, repo=REPO, gh=gh) == 0
    closed = [call[2] for call in gh.writes() if call[1] == "close"]
    assert closed == ["687", "688"]


def test_resolve_on_a_failed_listing_writes_nothing_and_stays_green(body: str, capsys) -> None:  # noqa: ANN001
    """A listing blip must not turn a green watchdog red; the next green run retries."""
    gh = FakeGh(list_code=1)
    assert _module.resolve(TITLE, body, repo=REPO, gh=gh) == 0
    assert gh.writes() == []
    assert "::warning::issue listing failed" in capsys.readouterr().out


def test_resolve_does_not_close_what_it_could_not_explain(body: str) -> None:
    """A close without its resolution comment reads as a human dismissing the alert."""
    gh = FakeGh([{"number": 818, "title": TITLE}], fail=["comment"])
    assert _module.resolve(TITLE, body, repo=REPO, gh=gh) == 1
    assert [call[1] for call in gh.writes()] == ["comment"]


def test_a_failed_close_is_red(body: str) -> None:
    """Silent close failures are how #818 stayed open: a permission regression must show."""
    gh = FakeGh([{"number": 818, "title": TITLE}], fail=["close"])
    assert _module.resolve(TITLE, body, repo=REPO, gh=gh) == 1


# --- the command line ----------------------------------------------------------


@pytest.mark.parametrize(("action", "writes"), [("open", [["issue", "create"]]), ("resolve", [])])
def test_the_cli_routes_each_action(action: str, writes: list[list[str]], body: str) -> None:
    gh = FakeGh([])
    assert _module.main([action, "--title", TITLE, "--body-file", body, "--repo", REPO], gh=gh) == 0
    assert [call[:2] for call in gh.writes()] == writes
    assert gh.calls[0][gh.calls[0].index("--repo") + 1] == REPO


def test_the_cli_defaults_the_repo_from_the_runner(body: str, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("GITHUB_REPOSITORY", "someone/else")
    gh = FakeGh([])
    _module.main(["resolve", "--title", TITLE, "--body-file", body], gh=gh)
    assert gh.calls[0][gh.calls[0].index("--repo") + 1] == "someone/else"


def test_the_cli_refuses_an_empty_title(body: str) -> None:
    gh = FakeGh([])
    assert _module.main(["open", "--title", "  ", "--body-file", body, "--repo", REPO], gh=gh) == 1
    assert gh.calls == []


@pytest.mark.parametrize("action", ["open", "resolve"])
def test_the_cli_has_no_fallback_repository(action: str, body: str, monkeypatch, capsys) -> None:  # noqa: ANN001
    """A hand run outside Actions that forgot --repo must not write to whichever
    repository a constant names (review)."""
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    gh = FakeGh([])
    assert _module.main([action, "--title", TITLE, "--body-file", body], gh=gh) == 1
    assert gh.calls == [], "the tool reached gh without knowing which repository it is writing to"
    assert "GITHUB_REPOSITORY" in capsys.readouterr().err


def test_the_cli_rejects_an_unknown_action(body: str) -> None:
    with pytest.raises(SystemExit):
        _module.main(["close", "--title", TITLE, "--body-file", body], gh=FakeGh([]))
