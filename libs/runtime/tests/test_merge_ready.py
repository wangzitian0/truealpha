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

import datetime
import json
from typing import Any

import pytest
from truealpha_runtime.testing import load_tool

merge_ready = load_tool("merge_ready")

HEAD = "97f7f43b" * 5
OLDER = "1381abf3" * 5


def responses(
    *,
    head_age_minutes: float = 0.0,
    head_in_commits: bool = True,
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
        if arguments[0] == "pr" and "commits" in arguments:
            pushed = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=head_age_minutes)
            oid = HEAD if head_in_commits else "0" * 40
            return {"commits": [{"oid": oid, "committedDate": pushed.isoformat().replace("+00:00", "Z")}]}
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
    assert any("nothing has reviewed" in p for p in problems), problems


def test_a_review_of_an_earlier_push_does_not_count(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A review of the previous commit says nothing about what is being merged
    now — the live state of #722 while this was written. Blocked until the
    settle window has passed; see the two tests at the end of this file for
    both sides of that boundary."""
    monkeypatch.setattr(merge_ready, "gh_json", responses(reviews=[review(OLDER)]))
    problems = merge_ready.blockers(1)
    assert any("no review covers it yet" in p for p in problems), problems


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


def test_a_fresh_push_with_no_review_of_it_blocks(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The incident shape: a review exists for an earlier commit, threads are
    clear, and the head was pushed seconds ago with a review possibly in
    flight. #714 and #718 both merged inside a minute of the push."""
    monkeypatch.setattr(merge_ready, "gh_json", responses(reviews=[review(OLDER)], head_age_minutes=0.5))
    problems = merge_ready.blockers(1)
    assert any("no review covers it yet" in p for p in problems), problems


def test_a_settled_push_merges_on_the_previous_review(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Copilot re-reviews on its own schedule and cannot be asked to — the
    request API answers 422 and an @copilot comment does nothing. Requiring a
    review OF THE HEAD unconditionally would deadlock every follow-up push, and
    a tool that deadlocks gets bypassed. After the settle window, with every
    thread resolved, the previous review carries it."""
    monkeypatch.setattr(
        merge_ready,
        "gh_json",
        responses(reviews=[review(OLDER)], head_age_minutes=merge_ready.SETTLE_MINUTES + 1),
    )
    assert merge_ready.blockers(1) == []


def test_settling_never_substitutes_for_a_review_that_never_happened(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Time alone is not review. A PR nobody has looked at stays blocked no
    matter how long it sits."""
    monkeypatch.setattr(merge_ready, "gh_json", responses(reviews=[], head_age_minutes=600.0))
    assert any("nothing has reviewed" in p for p in merge_ready.blockers(1))


def test_an_unrecognisable_commit_list_blocks_rather_than_ages_out(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """If the head is not among the PR's listed commits, the shape is not what
    this tool assumes. Treating an unknown age as OLD would wave the merge
    through on a response it did not understand; it is treated as brand new so
    the settle window blocks instead."""
    monkeypatch.setattr(
        merge_ready,
        "gh_json",
        responses(reviews=[review(OLDER)], head_age_minutes=600.0, head_in_commits=False),
    )
    problems = merge_ready.blockers(1)
    assert any("no review covers it yet" in p for p in problems), problems
