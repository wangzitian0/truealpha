"""Tests for tools/issue_close_guard.py — #562.

Rule 7 landed 2026-07-30 and its defect recurred three times after: #371 on
2026-08-14 (two seconds after #555 merged), #494 on 2026-08-14 (two seconds
after #565), both with PR bodies that said "Not `Closes`". A convention the
platform ignores is a run that happened, not a check that runs again.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = REPO_ROOT / "tools/issue_close_guard.py"
SPEC = importlib.util.spec_from_file_location("truealpha_issue_close_guard", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = _module
SPEC.loader.exec_module(_module)
run = _module.run


def _timeline(*events: dict, pages: list[list[dict]] | None = None):
    """One short page by default; `pages` exercises the pagination loop."""
    chunks = pages if pages is not None else [list(events)]

    def gh_api(path: str) -> str:
        assert "/timeline" in path
        page = int(path.rsplit("page=", 1)[1])
        return json.dumps(chunks[page - 1] if page <= len(chunks) else [])

    return gh_api


def _git(contained: bool = False, code: int | None = None):
    """`git merge-base --is-ancestor`: 0 = production contains it, 1 = it does not."""

    def git(args):  # noqa: ANN001
        return (code if code is not None else (0 if contained else 1)), ""

    return git


def _serving(tag: str = "v0.0.19"):
    def http_get(url: str) -> tuple[int, str]:
        return 200, json.dumps({"status": "ok", "git_sha": tag})

    return http_get


def _capture() -> tuple[list[tuple[int, str]], object]:
    calls: list[tuple[int, str]] = []
    return calls, lambda issue, reason: calls.append((issue, reason))


def test_a_merge_closing_an_unreleased_issue_is_reopened(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The exact #371/#494 event."""
    calls, reopen = _capture()
    exit_code = run(
        494,
        gh_api=_timeline({"event": "closed", "commit_id": "1acd82c" + "0" * 33}),
        git=_git(),
        http_get=_serving(),
        reopen=reopen,
    )
    assert exit_code == 0
    assert len(calls) == 1 and calls[0][0] == 494
    assert "production is NOT serving" in calls[0][1]
    assert "#562" in calls[0][1], "the comment must name the mechanism, not just scold"


def test_the_real_close_event_shape_is_recognised() -> None:
    """Verbatim from #494's own timeline, which is the event this guard exists
    for. The `closed` event carries NO commit_id; the merge appears as a
    commit-bearing event at the same instant. Keying on the close event alone
    made the guard inert against its own defect."""
    calls, reopen = _capture()
    exit_code = run(
        494,
        gh_api=_timeline(
            {"event": "cross-referenced", "created_at": "2026-08-14T10:34:19Z", "commit_id": None},
            {"event": "closed", "created_at": "2026-08-14T10:45:28Z", "commit_id": None},
            {
                "event": "referenced",
                "created_at": "2026-08-14T10:45:28Z",
                "commit_id": "3057e9704594df7613469e0dbdf3b22fad057a48",
            },
        ),
        git=_git(),
        http_get=_serving(),
        reopen=reopen,
    )
    assert exit_code == 0
    assert len(calls) == 1, "an auto-close whose commit lives on a sibling event must be caught"
    assert "3057e970" in calls[0][1]


def test_a_human_close_is_left_alone(capsys: pytest.CaptureFixture[str]) -> None:
    """Closing by hand IS rule 7. The guard must not fight the behaviour it wants."""
    calls, reopen = _capture()
    exit_code = run(
        494,
        gh_api=_timeline({"event": "closed", "created_at": "2026-08-17T03:00:00Z"}),
        git=_git(),
        http_get=_serving(),
        reopen=reopen,
    )
    assert exit_code == 0
    assert calls == []
    assert "closed by a person" in capsys.readouterr().out


def test_a_released_capability_stays_closed(capsys: pytest.CaptureFixture[str]) -> None:
    """Once a tag contains the commit, the merge close was correct after all."""
    calls, reopen = _capture()
    exit_code = run(
        494,
        gh_api=_timeline({"event": "closed", "commit_id": "abc1234" + "0" * 33}),
        git=_git(contained=True),
        http_get=_serving("v0.0.20"),
        reopen=reopen,
    )
    assert exit_code == 0
    assert calls == []
    assert "production is serving" in capsys.readouterr().out


def test_the_latest_close_is_the_one_judged() -> None:
    """#371 and #494 were each closed, reopened and closed again."""
    calls, reopen = _capture()
    run(
        371,
        gh_api=_timeline(
            {"event": "closed"},
            {"event": "reopened"},
            {"event": "closed", "commit_id": "fca4ba8" + "0" * 33},
        ),
        git=_git(),
        http_get=_serving(),
        reopen=reopen,
    )
    assert len(calls) == 1, "an old hand close must not excuse a later auto close"


def test_an_unanswerable_history_fails_loudly(capsys: pytest.CaptureFixture[str]) -> None:
    """A shallow clone cannot answer "does a tag contain this"; guessing "no"
    would reopen every issue in the repository."""
    calls, reopen = _capture()
    exit_code = run(
        494,
        gh_api=_timeline({"event": "closed", "commit_id": "abc1234" + "0" * 33}),
        git=_git(code=128),
        http_get=_serving(),
        reopen=reopen,
    )
    assert exit_code == 1
    assert calls == []
    assert "fetch tags and full history" in capsys.readouterr().err


def test_a_tag_that_exists_but_is_not_deployed_is_not_evidence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The weakness a first version had, caught against reality: v0.0.20
    contains the commits that closed #371, #494 and #495, and production serves
    v0.0.19. Judging by tag existence would have called all three released while
    nothing a user touches had them — the precise thing rule 6 refuses."""
    calls, reopen = _capture()
    run(
        494,
        gh_api=_timeline(
            {"event": "closed", "created_at": "t", "commit_id": None},
            {"event": "referenced", "created_at": "t", "commit_id": "3057e970" + "0" * 32},
        ),
        git=_git(contained=False),  # production's tag does not contain it
        http_get=_serving("v0.0.19"),
        reopen=reopen,
    )
    assert len(calls) == 1
    assert "serves v0.0.19" in calls[0][1]


def test_an_unreachable_production_fails_rather_than_guesses(
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls, reopen = _capture()
    exit_code = run(
        494,
        gh_api=_timeline({"event": "closed", "created_at": "t", "commit_id": "a" * 40}),
        git=_git(),
        http_get=lambda _url: (503, "down"),
        reopen=reopen,
    )
    assert exit_code == 1
    assert calls == []
    assert "HTTP 503" in capsys.readouterr().err


def test_the_latest_close_is_found_beyond_the_first_page() -> None:
    """A long-running issue accumulates hundreds of timeline events; reading
    only the first page judges a stale close (review)."""
    filler = [{"event": "commented", "created_at": f"t{i}"} for i in range(100)]
    calls, reopen = _capture()
    run(
        494,
        gh_api=_timeline(
            pages=[
                filler,
                [
                    {"event": "closed", "created_at": "late", "commit_id": None},
                    {"event": "referenced", "created_at": "late", "commit_id": "b" * 40},
                ],
            ]
        ),
        git=_git(contained=False),
        http_get=_serving("v0.0.19"),
        reopen=reopen,
    )
    assert len(calls) == 1, "the close on page 2 must be the one judged"


def test_non_object_health_json_is_a_verdict_failure_not_a_crash(
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls, reopen = _capture()
    exit_code = run(
        494,
        gh_api=_timeline({"event": "closed", "created_at": "t", "commit_id": "a" * 40}),
        git=_git(),
        http_get=lambda _url: (200, json.dumps(["not", "an", "object"])),
        reopen=reopen,
    )
    assert exit_code == 1
    assert calls == []
    # #585 sharpened this: non-object JSON now says so, instead of being
    # collapsed into "no identity field" as all three copies used to do.
    assert "not an object" in capsys.readouterr().err


def test_a_release_identity_git_could_read_as_an_option_never_reaches_git(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The same lesson tools/deploy_freshness.py had to learn: the identifier
    comes from an HTTP response and is handed to git (review)."""
    reached_git = False

    def git(args):  # noqa: ANN001
        nonlocal reached_git
        reached_git = True
        return 0, ""

    calls, reopen = _capture()
    exit_code = run(
        494,
        gh_api=_timeline({"event": "closed", "created_at": "t", "commit_id": "a" * 40}),
        git=git,
        http_get=_serving("--upload-pack=touch /tmp/x"),
        reopen=reopen,
    )
    assert exit_code == 1
    assert not reached_git
    assert "not a usable release identifier" in capsys.readouterr().err
