"""An unreviewed PR must not read as a clean one — AGENTS.md rule 4.

Two PRs merged on 2026-09-01 with findings outstanding because the check being
made was "zero unresolved threads" and nothing had reviewed them yet: #714 by
39 seconds (shipping a path bug that killed `Deploy staging v0.0.38`, fixed by
another lane in #717) and #718 by 35 seconds.

The API responses are supplied here rather than fetched. That is the whole
point: the failure being guarded against is a REAL PR whose thread list is
empty for the wrong reason, and only a fabricated response can put the tool in
that state on demand. A test that queried a live PR could not reproduce the
39-second window at all.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from truealpha_runtime.testing import load_tool

merge_ready = load_tool("merge_ready")

HEAD = "97f7f43b" * 5
OLDER = "1381abf3" * 5


def responses(
    *,
    state: str = "OPEN",
    merge_state: str = "CLEAN",
    reviews: list[dict[str, Any]] | None = None,
    unresolved: int = 0,
    total: int | None = None,
) -> Any:
    """One fake `gh` for the three calls the tool makes, in order."""
    threads = [{"isResolved": False}] * unresolved + [{"isResolved": True}]
    payloads = {
        "pr": {"headRefOid": HEAD, "mergeStateStatus": merge_state, "state": state},
        "reviews": reviews if reviews is not None else [],
        "graphql": {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "totalCount": total if total is not None else len(threads),
                            "nodes": threads,
                        }
                    }
                }
            }
        },
    }

    def fake(arguments: list[str]) -> Any:
        if arguments[0] == "pr":
            return payloads["pr"]
        if "graphql" in arguments:
            return payloads["graphql"]
        return payloads["reviews"]

    return fake


def review(commit: str, *, state: str = "COMMENTED", who: str = "copilot") -> dict[str, Any]:
    return {"commit_id": commit, "state": state, "user": {"login": who}, "submitted_at": "2026-09-01T04:32:11Z"}


def test_a_reviewed_and_clean_pr_passes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(merge_ready, "gh_json", responses(reviews=[review(HEAD)]))
    assert merge_ready.blockers(1) == []


def test_zero_threads_with_no_review_of_this_head_blocks(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The #714 state exactly: green, no open threads, nothing reviewed yet."""
    monkeypatch.setattr(merge_ready, "gh_json", responses(reviews=[]))
    problems = merge_ready.blockers(1)
    assert any("no review has been submitted for head" in p for p in problems), problems


def test_a_review_of_an_earlier_push_does_not_count(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A review of the previous commit says nothing about what is being merged
    now — the live state of #722 while this was written."""
    monkeypatch.setattr(merge_ready, "gh_json", responses(reviews=[review(OLDER)]))
    problems = merge_ready.blockers(1)
    assert any("no review has been submitted for head" in p for p in problems), problems


def test_unresolved_threads_block(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(merge_ready, "gh_json", responses(reviews=[review(HEAD)], unresolved=2))
    assert any("2 unresolved review thread(s)" in p for p in merge_ready.blockers(1))


def test_changes_requested_blocks_even_with_no_open_threads(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Rule 4: a latest decision of CHANGES_REQUESTED blocks even when it
    created no thread."""
    monkeypatch.setattr(
        merge_ready, "gh_json", responses(reviews=[review(HEAD, state="CHANGES_REQUESTED", who="human")])
    )
    assert any("changes requested" in p for p in merge_ready.blockers(1))


def test_a_red_or_pending_check_blocks(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(merge_ready, "gh_json", responses(reviews=[review(HEAD)], merge_state="BLOCKED"))
    assert any("not CLEAN" in p for p in merge_ready.blockers(1))


def test_more_threads_than_one_page_is_not_assumed_clean(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Fail closed past the page size, rather than judging on the first 100."""
    monkeypatch.setattr(merge_ready, "gh_json", responses(reviews=[review(HEAD)], total=140))
    assert any("more than one page" in p for p in merge_ready.blockers(1))


def test_a_failing_api_call_never_reads_as_nothing_objected(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The degraded path. An empty or errored response must not be mistaken for
    an absence of objections — that is the same conflation one layer down."""

    def broken(arguments: list[str]) -> Any:
        raise SystemExit("merge_ready: gh failed")

    monkeypatch.setattr(merge_ready, "gh_json", broken)
    with pytest.raises(SystemExit):
        merge_ready.blockers(1)


def test_the_tool_reports_every_blocker_not_just_the_first(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """One round per blocker is the cost of stopping at the first."""
    monkeypatch.setattr(merge_ready, "gh_json", responses(reviews=[], merge_state="BLOCKED", unresolved=1))
    assert len(merge_ready.blockers(1)) == 3, json.dumps(merge_ready.blockers(1), indent=2)
