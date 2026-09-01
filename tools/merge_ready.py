"""Is this PR actually reviewed, or merely un-objected-to — AGENTS.md rule 4.

Two PRs merged on 2026-09-01 with review findings outstanding, because the
check being made was "zero unresolved threads" and nothing had reviewed them
yet. Zero threads on an unreviewed PR is byte-identical to zero threads on a
clean one:

    #714  merged 04:31:32, review submitted 04:32:11  (39 s)
          -> `working-directory: apps/app-web` made a bare
             `tools/warm_surface.sh` resolve under apps/app-web;
             `Deploy staging v0.0.38` died with exit 127 and another lane
             spent a PR fixing it (#717)
    #718  merged 05:52:04, review submitted 05:51:29  (35 s)
          -> three findings, one a real logic defect in a guard

Rule 4 already says it — "an empty thread list before completion is not
evidence of a clean review" — so this mechanises the sentence rather than
adding a policy. The discriminator is a review whose `commit_id` IS the
current head: a review of an earlier push says nothing about what is being
merged now.

Deliberately stricter than rule 4's budget (High = 0, Medium <= 2, Low <= 4):
any unresolved thread blocks. Copilot does not emit severity labels, so a
budget evaluated over unlabelled findings would be a budget over guesses, and
the practice here has been to resolve every thread anyway.

Not a duplicate of `tools/cut_release.sh`: that one asks the same question
about ALREADY-MERGED PRs at release time, when the answer arrives too late to
stop the merge. This is the merge-time check. Both are needed — #714's review
did land before the release, so cut_release would have refused v0.0.38 for the
right reason, after the code was already on main.

Usage:
    python tools/merge_ready.py 722          # exit 0 = safe to merge
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence

REPO = "wangzitian0/truealpha"


def gh_json(arguments: Sequence[str]) -> object:
    result = subprocess.run(["gh", *arguments], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        # Fail closed: an API error must never read as "nothing objected".
        raise SystemExit(f"merge_ready: gh {' '.join(arguments)} failed: {result.stderr.strip()[:200]}")
    return json.loads(result.stdout or "null")


def blockers(number: int) -> list[str]:
    view = gh_json(["pr", "view", str(number), "--repo", REPO, "--json", "headRefOid,mergeStateStatus,state"])
    assert isinstance(view, dict)
    if view["state"] != "OPEN":
        return [f"#{number} is {view['state']}, not OPEN"]

    head = str(view["headRefOid"])
    problems: list[str] = []

    if view["mergeStateStatus"] != "CLEAN":
        problems.append(
            f"mergeStateStatus is {view['mergeStateStatus']}, not CLEAN (checks still running, "
            f"failing, or the branch is behind)"
        )

    reviews = gh_json(["api", f"repos/{REPO}/pulls/{number}/reviews"])
    assert isinstance(reviews, list)
    for_head = [r for r in reviews if isinstance(r, dict) and r.get("commit_id") == head]
    if not for_head:
        seen = sorted({str(r.get("commit_id", ""))[:8] for r in reviews if isinstance(r, dict)})
        problems.append(
            f"no review has been submitted for head {head[:8]} (reviews exist for {seen or 'nothing'}) "
            f"— zero unresolved threads on an unreviewed push is not a clean review (rule 4; #714 "
            f"merged 39 s before its review and broke a staging deploy)"
        )
    # Latest effective decision per reviewer, over reviews of THIS head.
    latest: dict[str, str] = {}
    for review in sorted(for_head, key=lambda r: str(r.get("submitted_at", ""))):
        author = str((review.get("user") or {}).get("login", "?"))
        if str(review.get("state")) != "COMMENTED":
            latest[author] = str(review.get("state"))
    blocked = [who for who, state in latest.items() if state == "CHANGES_REQUESTED"]
    if blocked:
        problems.append(f"changes requested by {blocked} and not since dismissed or approved")

    query = (
        f'query {{ repository(owner: "wangzitian0", name: "truealpha") {{ pullRequest(number: {number}) '
        f"{{ reviewThreads(first: 100) {{ totalCount nodes {{ isResolved }} }} }} }} }}"
    )
    threads = gh_json(["api", "graphql", "-f", f"query={query}"])
    assert isinstance(threads, dict)
    node = threads["data"]["repository"]["pullRequest"]["reviewThreads"]
    if node["totalCount"] > 100:
        problems.append(f"{node['totalCount']} review threads, more than one page — verify by hand")
    unresolved = sum(1 for t in node["nodes"] if not t["isResolved"])
    if unresolved:
        problems.append(f"{unresolved} unresolved review thread(s)")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("number", type=int)
    arguments = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    problems = blockers(arguments.number)
    for problem in problems:
        print(f"merge_ready: {problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"merge_ready: #{arguments.number} is reviewed at its current head and clear to merge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
