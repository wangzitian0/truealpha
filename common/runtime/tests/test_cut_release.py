"""`tools/cut_release.sh`, run end to end against a throwaway origin — #913.

`--resume` and `--redeploy` exist for a ceremony that died after its tag push
(#811). Without `--prs`, the PR list is derived from every merge since "the
newest release tag reachable from main HEAD" (#855 A3, #860) — and on a resume
that tag IS the one being resumed, so the range was always empty and the script
refused with "nothing to release". Both automatic retries on 2026-09-17 (v0.0.80
and v0.0.83) died exactly there.

These tests run the real script from a real clone of a local bare "origin", with
`gh`, `curl` and `sleep` replaced on PATH, so the derivation is exercised through
the same entry point an operator uses. The safety checks around it are pinned in
the same way: a tag at another commit is still a collision, and a fresh release
of an already-released HEAD is still "nothing to release".
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CUT_RELEASE = REPO_ROOT / "tools" / "cut_release.sh"
TIMEOUT_SECONDS = 60

# Stand-in for `gh`. It answers only what cut_release.sh asks, and logs every
# call so a test can say what was verified and what was dispatched. `-q` filters
# are ignored: each answer is already the filtered value the script expects.
FAKE_GH = r"""#!/usr/bin/env python3
import json, os, re, sys

args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\n")

def flag(name):
    return args[args.index(name) + 1] if name in args else ""

merges = json.loads(os.environ["FAKE_GH_MERGES"])
commit_prs = {sha: number for number, sha in merges.items()}
if args[:2] == ["pr", "view"]:
    fields = flag("--json")
    if fields == "state":
        print("MERGED" if args[2] in merges else "OPEN")
    elif fields == "mergeCommit":
        print(merges[args[2]])
    else:
        sys.exit(f"fake gh: unexpected pr view fields {fields!r}")
elif args[:2] == ["api", "graphql"]:
    unresolved = int(os.environ.get("FAKE_UNRESOLVED_THREADS", "0"))
    nodes = [{"isResolved": False, "comments": {"nodes": [{"path": "foo.py", "body": "nit"}]}} for _ in range(unresolved)]
    print(json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {"totalCount": unresolved, "nodes": nodes}}}}}))
elif args[0] == "api" and "/actions/" in (args[1] if len(args) > 1 else ""):
    # #1412 R1: the --prod path reads the staging evidence of the tag through the REST API.
    # FAKE_GH_RUNS holds the runs of each workflow. The fake ignores the query filters
    # (branch, created) on purpose: the code under test must apply its own time and SHA rules,
    # so a stale run in this list is a stale run the script itself has to refuse.
    path = args[1]
    runs = json.loads(os.environ.get("FAKE_GH_RUNS", "{}"))
    if path.endswith("/jobs"):
        run_id = path.split("/runs/")[1].split("/")[0]
        print(json.dumps({"jobs": [{"steps": runs.get("jobs", {}).get(run_id, [])}]}))
    else:
        workflow = re.search(r"/workflows/([^/]+)/runs", path)
        kinds = {"ci-required.yml": "ci", "deploy-release.yml": "deploy", "walk-release.yml": "walk"}
        if workflow is None or workflow.group(1) not in kinds:
            sys.exit(f"fake gh: unexpected api path {path!r}")
        # `--paginate --jq '.workflow_runs[]'` prints one run per line.
        for run in runs.get(kinds[workflow.group(1)], []):
            print(json.dumps(run))
elif args[0] == "api" and re.fullmatch(r"repos/[^/]+/[^/]+/commits/[0-9a-f]+/pulls", args[1] if len(args) > 1 else ""):
    # #1022: cut_release.sh resolves a commit's PR via this endpoint instead of
    # guessing from commit-message text — the same SHA->PR fact the ceremony
    # fixture already tracks in `merges`, just inverted.
    sha = args[1].split("/")[-2]
    number = commit_prs.get(sha)
    print(json.dumps([{"number": int(number)}] if number else []))
elif args[:2] == ["run", "list"]:
    workflow = flag("--workflow")
    if workflow == "ci-required.yml":
        # main HEAD's run. Empty is what the real filter prints when main HEAD's run is no
        # longer among the last 20 ci-required runs (an older tag, #1412).
        print(os.environ.get("FAKE_MAIN_CI_STATE", "101 completed success"))
    elif workflow == "":
        # The tag's own push run (also what --redeploy checks). Overridable so
        # a test can reproduce #940: a fresh tag whose own ci-required never
        # started returns exactly this shape for `[.[]|select(...)][0]` over
        # an empty array — real jq indexes null, not an error, so the tag's
        # real 2026-09-22 failure read "last seen: null null null" verbatim.
        print(os.environ.get("FAKE_TAG_RUN_STATE", "202 completed success"))
    elif workflow == "deploy-release.yml":
        print("303")
    elif workflow == "walk-release.yml":
        print("404")
    else:
        sys.exit(f"fake gh: unexpected workflow {workflow!r}")
elif args[:2] == ["run", "view"]:
    print("completed success")
elif args[:2] == ["workflow", "run"]:
    pass
else:
    sys.exit(f"fake gh: unexpected call {args!r}")
"""

FAKE_CURL = """#!/usr/bin/env bash
printf '{"git_sha": "%s"}' "$FAKE_SERVED_TAG"
"""

# Every poll loop in the script ends on its first read against the fake `gh`; a
# loop that does not would otherwise sleep for up to 20 minutes before failing.
FAKE_SLEEP = "#!/usr/bin/env bash\nexit 0\n"


def git(cwd: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=cwd, capture_output=True, text=True, check=True, env=git_env(cwd)
    ).stdout.strip()


def git_env(cwd: Path) -> dict[str, str]:
    # The operator's own git config (signing, hooks, default branch) must not
    # leak into a throwaway repository.
    empty = cwd.parent / "gitconfig"
    empty.touch()
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(empty),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "release-test",
        "GIT_AUTHOR_EMAIL": "release-test@example.invalid",
        "GIT_COMMITTER_NAME": "release-test",
        "GIT_COMMITTER_EMAIL": "release-test@example.invalid",
    }


#: #1412 R4: the one declaration of data replays, read from the release commit.
DECLARATION = "apps/data-engine/release-replays.json"
DEMO_PARSER = "apps/data-engine/src/demo/parser_identity.py"
DEMO_PROJECTOR = "apps/data-engine/src/demo/projector.py"
PARSER_V1 = 'PARSER_VERSION_HISTORY = ("demo-parser:v1",)\n'
PARSER_V2 = 'PARSER_VERSION_HISTORY = ("demo-parser:v1", "demo-parser:v2")\n'
PROJECTOR_V1 = '"""Demo projector."""\n\n\ndef project(shares, close):\n    return shares * close\n'


def declaration(*replays: dict[str, str]) -> str:
    """The throwaway origin's declaration: one parser constant and one projector module."""
    return json.dumps(
        {
            "identities": {
                "demo-parser": {"path": DEMO_PARSER, "name": "PARSER_VERSION_HISTORY"},
                "demo-projector": {"paths": [DEMO_PROJECTOR]},
            },
            "replays": list(replays),
        },
        indent=2,
    )


@dataclass
class Ceremony:
    root: Path
    work: Path
    merges: dict[str, str]

    def write(self, relative: str, text: str) -> None:
        """Stage a file for the next commit."""
        path = self.work / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        git(self.work, "add", relative)

    def remove(self, relative: str) -> None:
        git(self.work, "rm", "-q", relative)

    def commit(self, subject: str, *, pr: str | None = None) -> str:
        git(self.work, "commit", "--allow-empty", "-q", "-m", subject)
        sha = git(self.work, "rev-parse", "HEAD")
        # `pr` overrides the number parsed from the subject's trailing (#N):
        # #1022's real commit had a subject ending "(#1001)" — the issue it
        # closed — while its actual PR was #1002, because the squash-merge box
        # was hand-edited and GitHub's own suffix never landed in the subject.
        number = pr if pr is not None else subject.rsplit("(#", 1)[1].rstrip(")")
        self.merges[number] = sha
        git(self.work, "push", "-q", "origin", "main")
        return sha

    def tag(self, name: str, at: str = "HEAD") -> None:
        """What a first attempt's lock claim leaves behind: an annotated tag on origin."""
        git(self.work, "tag", "-a", name, at, "-m", f"{name}\n\nPRs: first attempt")
        git(self.work, "push", "-q", "origin", name)

    def remote_tags(self) -> dict[str, str]:
        out = git(self.work, "ls-remote", "--tags", "origin")
        tags: dict[str, str] = {}
        for line in out.splitlines():
            sha, ref = line.split()
            if ref.endswith("^{}"):
                tags[ref[len("refs/tags/") : -3]] = sha
        return tags

    def tag_message(self, name: str) -> str:
        """The exact annotation origin holds for `name` — what
        `tools/auto_release.py`'s `read_tags` will see on its next run."""
        git(self.work, "fetch", "-q", "origin", f"refs/tags/{name}:refs/tags/{name}")
        return git(self.work, "for-each-ref", "--format=%(contents)", f"refs/tags/{name}")

    def run(
        self,
        tag: str,
        *arguments: str,
        served: str = "",
        extra_env: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], list]:
        log = self.root / "gh.log"
        log.write_text("", encoding="utf-8")
        env = {
            **git_env(self.work),
            "PATH": f"{self.root / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "FAKE_GH_LOG": str(log),
            "FAKE_GH_MERGES": json.dumps(self.merges),
            "FAKE_SERVED_TAG": served or tag,
            **(extra_env or {}),
        }
        result = subprocess.run(
            ["bash", str(CUT_RELEASE), tag, "--message", "test release", *arguments],
            cwd=self.work,
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=TIMEOUT_SECONDS,
        )
        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        return result, calls


@pytest.fixture
def ceremony(tmp_path: Path) -> Ceremony:
    """origin has v0.0.1 at `seed (#10)`, then `feature (#11)` and `fix (#12)`
    on main — the batch a v0.0.2 release describes."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name, body in (("gh", FAKE_GH), ("curl", FAKE_CURL), ("sleep", FAKE_SLEEP)):
        path = binaries / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "-q", "--bare", "-b", "main")
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "remote", "add", "origin", str(origin))
    stage = Ceremony(root=tmp_path, work=work, merges={})
    stage.write(DECLARATION, declaration())
    stage.write(DEMO_PARSER, PARSER_V1)
    stage.write(DEMO_PROJECTOR, PROJECTOR_V1)
    stage.commit("seed (#10)")
    stage.tag("v0.0.1")
    stage.commit("feature (#11)")
    stage.commit("fix (#12)")
    return stage


def dispatched(calls: list) -> list[list[str]]:
    return [call for call in calls if call[:2] == ["workflow", "run"]]


def verified_prs(calls: list) -> list[str]:
    return [call[2] for call in calls if call[:2] == ["pr", "view"] and "state" in call]


@pytest.mark.parametrize("mode", ["--resume", "--redeploy"])
def test_a_resumed_release_derives_its_prs_from_the_release_before_it(ceremony: Ceremony, mode: str) -> None:
    """#913: v0.0.2 is already on origin at main HEAD (the first attempt pushed
    it and then died). Resuming without --prs must derive the same batch the
    first attempt did — #11 and #12, counted from v0.0.1 — not an empty range
    counted from v0.0.2 itself."""
    ceremony.tag("v0.0.2")
    before = ceremony.remote_tags()

    result, calls = ceremony.run("v0.0.2", mode)

    assert "nothing to release" not in result.stderr, (
        f"{mode} counted the derived range from the tag it is resuming (#913):\n{result.stderr}"
    )
    assert result.returncode == 0, f"{mode} failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "every merge on main since v0.0.1" in result.stdout
    assert "derived --prs 11,12" in result.stdout
    assert verified_prs(calls) == ["11", "12"], "every PR in the batch must still be verified merged"
    # The printed promotion command carries the batch to the --prod run.
    assert '--prs "11,12"' in result.stdout
    # A resume never re-claims the number: the tag on origin is untouched.
    assert ceremony.remote_tags() == before
    [deploy] = dispatched(calls)
    assert "version_ref=v0.0.2" in deploy and "deploy_type=staging" in deploy
    assert "source_run_id=202" in deploy


def test_derivation_resolves_the_pr_from_the_commit_not_the_subjects_trailing_number(ceremony: Ceremony) -> None:
    """#1022: v0.0.96's redeploy retry hit a commit whose subject already ended
    in "(#1001)" — the issue it closed, not its PR (#1002) — because the
    squash-merge box was hand-edited and GitHub's own auto-appended PR number
    never landed in the subject. The old regex-on-text derivation grabbed
    #1001 and failed resolving it as a PR. Derivation must resolve the PR
    from the commit SHA via the API instead, immune to whatever text a human
    typed into the merge box."""
    ceremony.commit("bump dep (#1001)", pr="13")
    ceremony.tag("v0.0.2")

    result, calls = ceremony.run("v0.0.2", "--redeploy")

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "derived --prs 11,12,13" in result.stdout, result.stdout
    assert verified_prs(calls) == ["11", "12", "13"]


def test_an_explicit_prs_list_on_resume_is_used_as_given(ceremony: Ceremony) -> None:
    """The break-glass path is unchanged: --prs skips the derivation entirely."""
    ceremony.tag("v0.0.2")

    result, calls = ceremony.run("v0.0.2", "--resume", "--prs", "12")

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "using explicit --prs 12" in result.stdout
    assert "deriving --prs" not in result.stdout
    assert verified_prs(calls) == ["12"]


def test_a_fresh_release_of_an_already_released_head_is_still_nothing_to_release(ceremony: Ceremony) -> None:
    """The resumed tag is passed over only on --resume/--redeploy. A new number
    cut at a HEAD that v0.0.2 already released must keep counting from v0.0.2
    and refuse, rather than re-release #11 and #12 under v0.0.3."""
    ceremony.tag("v0.0.2")

    result, calls = ceremony.run("v0.0.3")

    assert result.returncode != 0
    assert "no commits between v0.0.2 and main HEAD" in result.stderr
    assert "v0.0.3" not in ceremony.remote_tags(), "a refused release must not claim its number"
    assert not dispatched(calls)


@pytest.mark.parametrize(
    ("arguments", "refusal"),
    [
        (("--resume",), "not at main HEAD"),
        (("--redeploy",), "not at main HEAD"),
        ((), "already exists on origin — release identity is immutable"),
    ],
)
def test_an_existing_tag_at_another_commit_is_still_a_collision(
    ceremony: Ceremony, arguments: tuple[str, ...], refusal: str
) -> None:
    """The tag push is the lock (docs/release-protocol.md). v0.0.2 was claimed
    at #12; main has moved on to #13, so v0.0.2 is another release — neither a
    resume nor a plain run may proceed under that number."""
    ceremony.tag("v0.0.2")
    ceremony.commit("later (#13)")

    result, calls = ceremony.run("v0.0.2", *arguments)

    assert result.returncode != 0
    assert refusal in result.stderr, result.stderr
    assert not dispatched(calls)
    assert not verified_prs(calls), "the collision must fail before any PR is verified"


# --- --auto: the owner's 2026-09-17 decision ("先在 staging 做吧，prod 回头再说") --
# is that an automatic release is staging-only, full stop. #860.


def test_auto_and_prod_cannot_be_combined_and_nothing_happens(ceremony: Ceremony) -> None:
    """The direct proof that --prod can never be passed out of the automatic
    path: even a caller that DID combine them is refused before the tag regex,
    before --prs derivation, before any git or gh call — not merely "the
    workflow happens not to write --prod today"."""
    before = ceremony.remote_tags()

    result, calls = ceremony.run("v0.0.2", "--auto", "--prod")

    assert result.returncode != 0
    assert "--auto and --prod cannot be combined" in result.stderr, result.stderr
    assert ceremony.remote_tags() == before, "a refused combination must not claim the tag number"
    assert calls == [], "must refuse before a single gh call — not merely before dispatching a deploy"
    assert not dispatched(calls)


def test_auto_marks_the_tag_so_the_daily_cap_can_count_it(ceremony: Ceremony) -> None:
    """`tools/auto_release.py`'s daily cap counts tags carrying AUTO_TRAILER
    (`Release-Trigger: auto-staging`) — this is the only place that line is
    written, and it must reach the pushed tag's own annotation, not just this
    run's log."""
    result, calls = ceremony.run("v0.0.2", "--auto")

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "Release-Trigger: auto-staging" in ceremony.tag_message("v0.0.2")
    # #945: --auto also explicitly dispatches the staging walk (see below), so
    # this is no longer the only dispatched call — isolate the deploy one.
    [deploy] = [call for call in dispatched(calls) if call[2] == "deploy-release.yml"]
    assert "deploy_type=staging" in deploy
    assert not any("deploy_type=prod" in call for call in dispatched(calls)), "--auto must never reach a prod deploy"


# --- #940: a fresh --auto tag whose own ci-required never goes green -------
# GitHub's recursive-workflow guard silently drops any run-triggering event
# produced by a push made with a workflow's own GITHUB_TOKEN — v0.0.87 was
# pushed that way, triggered 0 downstream runs, and this script's 20-minute
# wait timed out with nothing to show for it. auto-release-staging.yml now
# pushes with a real user PAT instead (the actual fix, not testable here —
# see its own comment); these tests cover the other half, what happens when a
# fresh tag's own CI still fails to go green for any other reason.


def test_auto_releases_its_own_dangling_tag_when_ci_required_never_goes_green(ceremony: Ceremony) -> None:
    """Reproduces the exact failure v0.0.87 hit: `[.[]|select(...)][0]` over
    an empty run list is `null`, and real jq happily interpolates that as the
    literal string "null" three times — which is what the real incident's log
    actually read. --auto must not leave that number dangling on origin for
    the next retry to trip over and burn another slot of the daily cap."""
    before = ceremony.remote_tags()

    result, calls = ceremony.run("v0.0.2", "--auto", extra_env={"FAKE_TAG_RUN_STATE": "null null null"})

    assert result.returncode != 0
    assert "not green after 20 minutes" in result.stderr, result.stderr
    assert "last seen: null null null" in result.stderr, result.stderr
    assert ceremony.remote_tags() == before, "the dangling v0.0.2 tag must be released back, not left on origin"
    assert not dispatched(calls), "a tag whose own CI never went green must never reach a deploy dispatch"


def test_a_manual_release_leaves_its_dangling_tag_alone(ceremony: Ceremony) -> None:
    """The converse: without --auto, the same failure must NOT delete the
    tag. docs/release-protocol.md's "An abandoned tag costs nothing" already
    covers the manual ceremony on purpose — an operator who chose vX.Y.Z by
    hand may already be coordinating around that exact number elsewhere, and
    this repository never deletes a release tag a human asked for out from
    under them. Only the fully-automatic path — nothing outside the run has
    seen or can reference a number it alone chose — gets the cleanup above."""
    result, calls = ceremony.run("v0.0.2", extra_env={"FAKE_TAG_RUN_STATE": "null null null"})

    assert result.returncode != 0
    assert "not green after 20 minutes" in result.stderr, result.stderr
    assert "v0.0.2" in ceremony.remote_tags(), (
        "a manually-cut dangling tag must be left exactly as docs/release-protocol.md says"
    )
    assert not dispatched(calls)


def test_a_hand_cut_release_never_carries_the_automatic_trailer(ceremony: Ceremony) -> None:
    """The converse of the above: an operator release (no --auto) must not
    accidentally look automatic, or a hand release would silently eat into
    the next day's automatic budget."""
    result, calls = ceremony.run("v0.0.2")

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "Release-Trigger: auto-staging" not in ceremony.tag_message("v0.0.2")


# --- #945: --auto's deploy dispatch runs under github.token (the SAME token
# whose recursive-workflow guard #940 already hit one hop earlier), so
# deploy-release.yml's own completion never cascades walk-release.yml's
# workflow_run trigger. wait_for_walk would then always wait out its full
# 3-minute budget for a run that can never appear. --auto must instead
# dispatch the staging walk explicitly once the staging deploy is confirmed
# green. The non-auto (real operator PAT) path is unchanged: a real PAT DOES
# cascade, so dispatching explicitly there too would walk the same deploy
# twice.


def workflow_dispatches(calls: list, workflow: str) -> list[list[str]]:
    return [call for call in dispatched(calls) if call[2] == workflow]


def test_auto_explicitly_dispatches_the_staging_walk(ceremony: Ceremony) -> None:
    result, calls = ceremony.run("v0.0.2", "--auto")

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    walk_calls = workflow_dispatches(calls, "walk-release.yml")
    assert len(walk_calls) == 1, f"expected exactly one explicit walk dispatch, got: {walk_calls}"
    [walk] = walk_calls
    assert "deploy_type=staging" in walk and "version_ref=v0.0.2" in walk


def test_a_manual_release_never_explicitly_dispatches_the_walk(ceremony: Ceremony) -> None:
    """Without --auto, a real operator PAT triggers deploy-release.yml's own
    workflow_run cascade — dispatching the walk explicitly here too would walk
    the same deploy twice."""
    result, calls = ceremony.run("v0.0.2")

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    walk_calls = workflow_dispatches(calls, "walk-release.yml")
    assert walk_calls == [], (
        f"a manual release must rely on the workflow_run cascade, not dispatch directly: {walk_calls}"
    )


# --- #1056: only a production deployment needs the owner ---------------------
# Owner instruction, 2026-10-06 (infra2#1035): production needs the owner's
# approval of the exact SHA and the owner's presence; everything else is the
# agent's. `--prod` is the one path in this script that reaches production, so it
# refuses unless `--owner-approved-sha` names, in full, the commit it promotes.
# The value cannot prove who typed it; it proves the caller named THIS commit, so
# a promotion cannot happen by accident, from the automatic path, or for a commit
# other than the one that was approved.

GATE = REPO_ROOT / "tools" / "owner_approval_gate.sh"
FULL_SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"


def head_sha(ceremony: Ceremony) -> str:
    return git(ceremony.work, "rev-parse", "HEAD")


def prod_dispatches(calls: list) -> list[list[str]]:
    return [call for call in workflow_dispatches(calls, "deploy-release.yml") if "deploy_type=prod" in call]


def staging_dispatches(calls: list) -> list[list[str]]:
    return [call for call in workflow_dispatches(calls, "deploy-release.yml") if "deploy_type=staging" in call]


# #1412 R1: the staging evidence that GitHub holds for a tag. The run ids differ from the ids
# the fake `gh run list` returns (202, 303, 404), so a prod dispatch that carries 2020 and 3030
# carries the evidence, not a run this ceremony started itself.
EVIDENCE_TAG_RUN = 2020
EVIDENCE_STAGING_RUN = 3030
EVIDENCE_WALK_RUN = 4040
WALK_STEP = "Walk the deployed surface"


def ci_run(run_id: int, tag: str, sha: str, created: str, conclusion: str = "success") -> dict[str, object]:
    return {
        "id": run_id,
        "head_branch": tag,
        "head_sha": sha,
        "event": "push",
        "status": "completed",
        "conclusion": conclusion,
        "created_at": created,
        "run_started_at": created,
        "updated_at": created,
    }


def workflow_run(
    run_id: int, title: str, started: str, ended: str, *, event: str = "workflow_dispatch", conclusion: str = "success"
) -> dict[str, object]:
    return {
        "id": run_id,
        "display_title": title,
        "event": event,
        "status": "completed",
        "conclusion": conclusion,
        "created_at": started,
        "run_started_at": started,
        "updated_at": ended,
        # deploy-release.yml and walk-release.yml run from main, never from the tag.
        "head_sha": "f" * 40,
    }


def staging_evidence(
    sha: str,
    tag: str = "v0.0.2",
    *,
    ci: list[dict[str, object]] | None = None,
    deploys: list[dict[str, object]] | None = None,
    walks: list[dict[str, object]] | None = None,
    walk_step: str | None = "success",
) -> dict[str, str]:
    """The timeline of v0.2.23 on 2026-10-20: tag CI at 04:05:21, staging deploy 04:09:11 to
    04:16:30, walk 04:16:33 to 04:18:32 (measured with `gh api`). Each keyword replaces one part."""
    runs = {
        "ci": ci if ci is not None else [ci_run(EVIDENCE_TAG_RUN, tag, sha, "2026-10-20T04:05:21Z")],
        "deploy": deploys
        if deploys is not None
        else [
            workflow_run(EVIDENCE_STAGING_RUN, f"Deploy staging {tag}", "2026-10-20T04:09:11Z", "2026-10-20T04:16:30Z")
        ],
        "walk": walks
        if walks is not None
        else [
            workflow_run(
                EVIDENCE_WALK_RUN,
                f"Walk Deploy staging {tag}",
                "2026-10-20T04:16:33Z",
                "2026-10-20T04:18:32Z",
                event="workflow_run",
            )
        ],
        "jobs": {str(EVIDENCE_WALK_RUN): [] if walk_step is None else [{"name": WALK_STEP, "conclusion": walk_step}]},
    }
    return {"FAKE_GH_RUNS": json.dumps(runs)}


def test_prod_without_the_owners_approval_is_refused_before_anything_happens(ceremony: Ceremony) -> None:
    before = ceremony.remote_tags()

    result, calls = ceremony.run("v0.0.2", "--prod")

    assert result.returncode == 2, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    # The script's OWN message, not owner_approval_gate.sh's: the gate would also refuse an
    # empty approval, but only after `git fetch`, and this check exists to fire before it.
    assert "cut_release: --prod needs the owner's approval of the exact release SHA" in result.stderr, result.stderr
    assert "--owner-approved-sha" in result.stderr, "the refusal must name the way forward"
    assert ceremony.remote_tags() == before, "a refused promotion must not claim the tag number"
    assert calls == [], "must refuse before a single gh call, not merely before the prod dispatch"


def test_an_empty_approval_is_no_approval(ceremony: Ceremony) -> None:
    result, calls = ceremony.run("v0.0.2", "--prod", "--owner-approved-sha", "")

    assert result.returncode == 2
    assert "cut_release: --prod needs the owner's approval of the exact release SHA" in result.stderr, result.stderr
    assert calls == []


def test_a_dangling_owner_approved_sha_flag_is_refused_with_its_own_message(ceremony: Ceremony) -> None:
    result, calls = ceremony.run("v0.0.2", "--prod", "--owner-approved-sha")

    assert result.returncode == 2
    assert "--owner-approved-sha needs a value" in result.stderr, result.stderr
    assert calls == []


def test_an_approval_without_prod_is_refused(ceremony: Ceremony) -> None:
    """An approval with nothing to promote is an operator mistake, and silently ignoring
    it would let the operator believe an approval was recorded."""
    before = ceremony.remote_tags()

    result, calls = ceremony.run("v0.0.2", "--owner-approved-sha", head_sha(ceremony))

    assert result.returncode == 2
    assert "applies only with --prod" in result.stderr, result.stderr
    assert ceremony.remote_tags() == before
    assert calls == []


def test_the_approval_must_be_the_commit_being_promoted(ceremony: Ceremony) -> None:
    """The realistic mistake: the owner approved the SHA that was main HEAD yesterday and
    main has moved. The approval must equal main HEAD, not merely be a real commit."""
    approved_yesterday = head_sha(ceremony)
    ceremony.commit("later (#13)")
    before = ceremony.remote_tags()

    result, calls = ceremony.run("v0.0.2", "--prod", "--owner-approved-sha", approved_yesterday)

    assert result.returncode == 2, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "does not cover this commit" in result.stderr, result.stderr
    assert approved_yesterday in result.stderr and head_sha(ceremony) in result.stderr
    assert ceremony.remote_tags() == before, "a mismatch must not claim the tag number"
    assert calls == [], "a mismatch must fail before any PR is verified or any workflow is dispatched"


def test_an_abbreviated_approval_is_refused(ceremony: Ceremony) -> None:
    """A seven-character prefix names a commit loosely. The owner approves one exact SHA."""
    result, calls = ceremony.run("v0.0.2", "--prod", "--owner-approved-sha", head_sha(ceremony)[:7])

    assert result.returncode == 2
    assert "exactly 40 characters" in result.stderr, result.stderr
    assert calls == []


@pytest.mark.parametrize("mode", ["--resume", "--redeploy"])
def test_a_resumed_prod_promotion_needs_the_approval_too(ceremony: Ceremony, mode: str) -> None:
    """--resume and --redeploy reach the same prod dispatch, so they may not skip the gate."""
    ceremony.tag("v0.0.2")

    refused, refused_calls = ceremony.run("v0.0.2", mode, "--prod")

    assert refused.returncode == 2, f"stdout:\n{refused.stdout}\nstderr:\n{refused.stderr}"
    assert not dispatched(refused_calls)

    sha = head_sha(ceremony)
    allowed, allowed_calls = ceremony.run(
        "v0.0.2", mode, "--prod", "--owner-approved-sha", sha, extra_env=staging_evidence(sha)
    )

    assert allowed.returncode == 0, f"stdout:\n{allowed.stdout}\nstderr:\n{allowed.stderr}"
    assert len(prod_dispatches(allowed_calls)) == 1


def test_prod_with_the_approved_sha_dispatches_prod_carrying_that_sha(ceremony: Ceremony) -> None:
    sha = head_sha(ceremony)
    ceremony.tag("v0.0.2")

    result, calls = ceremony.run("v0.0.2", "--prod", "--owner-approved-sha", sha, extra_env=staging_evidence(sha))

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    [prod] = prod_dispatches(calls)
    assert f"owner_approved_sha={sha}" in prod, "deploy-release.yml checks the approval again, so it must receive it"
    assert staging_dispatches(calls) == [], "#1412: a promotion never deploys staging again"


def test_the_dry_run_checks_the_approval_and_dispatches_nothing(ceremony: Ceremony) -> None:
    """--dry-run is how an agent proves, before asking, that the SHA it holds is the one
    the ceremony would promote: it runs the same gate and then stops. Since #1412 it also
    reads the staging evidence, so a dry run shows that the promotion would be accepted."""
    before = ceremony.remote_tags()

    refused, refused_calls = ceremony.run("v0.0.2", "--prod", "--dry-run", "--owner-approved-sha", OTHER_SHA)

    assert refused.returncode == 2, f"stdout:\n{refused.stdout}\nstderr:\n{refused.stderr}"
    assert "does not cover this commit" in refused.stderr, refused.stderr
    assert not dispatched(refused_calls)
    assert ceremony.remote_tags() == before, "a refused dry run must not claim the tag number"

    sha = head_sha(ceremony)
    ceremony.tag("v0.0.2")
    tagged = ceremony.remote_tags()
    allowed, allowed_calls = ceremony.run(
        "v0.0.2", "--prod", "--dry-run", "--owner-approved-sha", sha, extra_env=staging_evidence(sha)
    )

    assert allowed.returncode == 0, f"stdout:\n{allowed.stdout}\nstderr:\n{allowed.stderr}"
    assert "dry run: would promote v0.0.2" in allowed.stdout, allowed.stdout
    assert f"staging run {EVIDENCE_STAGING_RUN}" in allowed.stdout, allowed.stdout
    assert not dispatched(allowed_calls)
    assert ceremony.remote_tags() == tagged, "a dry run must not claim or move a tag"

    unverified, unverified_calls = ceremony.run(
        "v0.0.2", "--prod", "--dry-run", "--owner-approved-sha", sha, extra_env=staging_evidence(sha, walks=[])
    )

    assert unverified.returncode != 0, "a dry run must refuse what the real promotion would refuse"
    assert not dispatched(unverified_calls)


def test_a_staging_release_still_needs_no_approval_and_prints_the_promotion_rule(ceremony: Ceremony) -> None:
    """The other half of the owner's instruction: everything that is not production is the
    agent's, so a staging release must not start asking for an approval."""
    result, calls = ceremony.run("v0.0.2")

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert not prod_dispatches(calls)
    assert f"production needs the owner's approval of release SHA {head_sha(ceremony)}" in result.stdout
    assert "--prod --owner-approved-sha <the SHA the owner approved>" in result.stdout


# The gate both entry points run (this script and deploy-release.yml): one rule, one file.


def run_gate(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(GATE), *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=TIMEOUT_SECONDS,
    )


def test_the_gate_passes_only_when_the_approval_equals_the_release_sha() -> None:
    result = run_gate(FULL_SHA, FULL_SHA)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("arguments", "refusal"),
    [
        ((), "the release SHA is not a 40-character"),
        (("", FULL_SHA), "needs the owner's approval of the exact release SHA"),
        ((OTHER_SHA, FULL_SHA), "does not cover this commit"),
        ((FULL_SHA[:7], FULL_SHA), "exactly 40 characters"),
        ((FULL_SHA.upper(), FULL_SHA), "exactly 40 characters"),
        ((FULL_SHA + "0", FULL_SHA), "exactly 40 characters"),
        (("z" * 40, FULL_SHA), "exactly 40 characters"),
        ((FULL_SHA, ""), "the release SHA is not a 40-character"),
        ((FULL_SHA, FULL_SHA[:7]), "the release SHA is not a 40-character"),
    ],
)
def test_the_gate_refuses_everything_else(arguments: tuple[str, ...], refusal: str) -> None:
    result = run_gate(*arguments)

    assert result.returncode == 2, (arguments, result.stdout, result.stderr)
    assert refusal in result.stderr, result.stderr


def test_the_gate_never_echoes_an_unvalidated_approval() -> None:
    """The approval is typed by a person and lands in a CI log, where a line that starts
    with `::error::` is a workflow command."""
    crafted = "::error::forged\n" + "a" * 40

    result = run_gate(crafted, FULL_SHA)

    assert result.returncode == 2
    assert "::error::" not in result.stderr + result.stdout, result.stderr


def test_unresolved_review_threads_on_merged_batch_prs_emit_warning_without_blocking(ceremony: Ceremony) -> None:
    """#1322 Tier 2: advisory review threads on already-merged PRs must emit a
    `::warning::` rather than failing the batch release ceremony."""
    result, calls = ceremony.run("v0.0.2", extra_env={"FAKE_UNRESOLVED_THREADS": "2"})

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "::warning::#11 has 2 unresolved review thread(s)" in result.stderr
    assert "::warning::#12 has 2 unresolved review thread(s)" in result.stderr
    assert len(workflow_dispatches(calls, "deploy-release.yml")) == 1


def test_prod_can_promote_an_existing_ancestor_tag_after_main_has_advanced(ceremony: Ceremony) -> None:
    """#1322 Tier 3: an existing staging-verified tag on origin (`v0.0.2` at #12)
    can be promoted to production even after `main` has advanced to #13, provided
    `--owner-approved-sha` equals the tag's exact commit SHA."""
    tag_sha = head_sha(ceremony)
    ceremony.tag("v0.0.2", at=tag_sha)
    later_sha = ceremony.commit("later (#13)")
    evidence = staging_evidence(tag_sha)

    wrong_approval, wrong_calls = ceremony.run(
        "v0.0.2", "--prod", "--owner-approved-sha", later_sha, extra_env=evidence
    )
    assert wrong_approval.returncode == 2, f"stdout:\n{wrong_approval.stdout}\nstderr:\n{wrong_approval.stderr}"
    assert "does not cover this commit" in wrong_approval.stderr
    assert not dispatched(wrong_calls)

    promoted, promoted_calls = ceremony.run("v0.0.2", "--prod", "--owner-approved-sha", tag_sha, extra_env=evidence)
    assert promoted.returncode == 0, f"stdout:\n{promoted.stdout}\nstderr:\n{promoted.stderr}"
    assert f"promoting existing release tag v0.0.2 at ancestor {tag_sha[:8]}" in promoted.stdout
    assert "derived --prs 11,12" in promoted.stdout
    [prod] = prod_dispatches(promoted_calls)
    assert f"owner_approved_sha={tag_sha}" in prod
    assert "reviewed_change_url=https://github.com/wangzitian0/truealpha/pull/12" in prod


# --- #1412 R1: production promotes the verified staging tag --------------------------------
# Before #1412, `--prod` on an existing tag deployed staging again, waited for that walk, and
# only then deployed production. Staging succeeded at the first try in 0.74 of the runs and
# production in 0.68, so a production attempt succeeded at the first try about half the time.
# `--prod` now reads the evidence that GitHub already holds for the commit of the tag and
# dispatches production alone. It refuses when a fact is missing and never deploys staging.

REDEPLOY_HINT = 'tools/cut_release.sh v0.0.2 --message "..." --redeploy'
REWALK_HINT = "gh workflow run walk-release.yml -f deploy_type=staging -f version_ref=v0.0.2"


def test_prod_promotes_the_verified_staging_tag_without_a_staging_redeploy(ceremony: Ceremony) -> None:
    sha = head_sha(ceremony)
    ceremony.tag("v0.0.2")

    result, calls = ceremony.run("v0.0.2", "--prod", "--owner-approved-sha", sha, extra_env=staging_evidence(sha))

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert staging_dispatches(calls) == [], "--prod deployed staging again (#1412 R1)"
    assert workflow_dispatches(calls, "walk-release.yml") == [], "--prod walked staging again (#1412 R1)"
    [prod] = workflow_dispatches(calls, "deploy-release.yml")
    assert "deploy_type=prod" in prod and "version_ref=v0.0.2" in prod
    assert f"staging_run_url=https://github.com/wangzitian0/truealpha/actions/runs/{EVIDENCE_STAGING_RUN}" in prod, (
        "the prod dispatch must name the staging run that verified this tag"
    )
    assert f"source_run_id={EVIDENCE_TAG_RUN}" in prod, "the prod dispatch must name the tag's own green CI run"


def test_prod_does_not_wait_for_a_main_run_that_left_the_run_list(ceremony: Ceremony) -> None:
    """A tag soaks staging for hours or days before the owner approves it. Main HEAD's run of
    that commit then is no longer among the last 20 ci-required runs, and the main-HEAD wait
    would spend 15 minutes and fail. The tag's own green CI run is part of the evidence."""
    sha = head_sha(ceremony)
    ceremony.tag("v0.0.2")

    result, calls = ceremony.run(
        "v0.0.2",
        "--prod",
        "--owner-approved-sha",
        sha,
        extra_env={**staging_evidence(sha), "FAKE_MAIN_CI_STATE": ""},
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert len(prod_dispatches(calls)) == 1


@pytest.mark.parametrize(
    ("evidence", "missing", "hint"),
    [
        pytest.param({"walks": []}, "Walk Deploy staging v0.0.2", REWALK_HINT, id="no-walk"),
        pytest.param({"walk_step": "failure"}, "'failure'", REWALK_HINT, id="walk-failed"),
        # Credentials unconfigured: the walk run succeeds and its walk step is skipped (#560).
        pytest.param({"walk_step": "skipped"}, "'skipped'", REWALK_HINT, id="walk-skipped"),
        pytest.param({"walk_step": None}, WALK_STEP, REWALK_HINT, id="walk-without-a-walk-step"),
        pytest.param(
            {
                "walks": [
                    workflow_run(
                        EVIDENCE_WALK_RUN, "Walk Deploy staging v0.0.2", "2026-10-20T04:12:00Z", "2026-10-20T04:14:00Z"
                    )
                ]
            },
            "Walk Deploy staging v0.0.2",
            REWALK_HINT,
            id="walk-before-the-deploy-ended",
        ),
        pytest.param({"deploys": []}, "Deploy staging v0.0.2", REDEPLOY_HINT, id="no-staging-deploy"),
        pytest.param(
            {
                "deploys": [
                    workflow_run(
                        EVIDENCE_STAGING_RUN,
                        "Deploy staging v0.0.2",
                        "2026-10-20T04:09:11Z",
                        "2026-10-20T04:16:30Z",
                        conclusion="failure",
                    )
                ]
            },
            "Deploy staging v0.0.2",
            REDEPLOY_HINT,
            id="staging-deploy-failed",
        ),
        pytest.param(
            {
                "deploys": [
                    workflow_run(
                        EVIDENCE_STAGING_RUN, "Deploy staging v0.0.2", "2026-10-20T04:09:11Z", "2026-10-20T04:16:30Z"
                    ),
                    workflow_run(3031, "Deploy staging v0.0.3", "2026-10-20T04:16:00Z", "2026-10-20T04:25:00Z"),
                ]
            },
            "Deploy staging v0.0.3",
            REDEPLOY_HINT,
            id="another-release-deployed-during-the-walk",
        ),
        pytest.param({"ci": []}, "ci-required", REDEPLOY_HINT, id="no-tag-ci-run"),
    ],
)
def test_prod_refuses_a_tag_whose_staging_evidence_is_incomplete(
    ceremony: Ceremony, evidence: dict[str, object], missing: str, hint: str
) -> None:
    sha = head_sha(ceremony)
    ceremony.tag("v0.0.2")

    result, calls = ceremony.run(
        "v0.0.2",
        "--prod",
        "--owner-approved-sha",
        sha,
        extra_env=staging_evidence(sha, **evidence),
    )

    assert result.returncode != 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert not dispatched(calls), "a refused promotion dispatches nothing: no staging redeploy and no prod deploy"
    assert missing in result.stderr, result.stderr
    assert hint in result.stderr, "the refusal must name the command that produces the missing fact"


def test_prod_refuses_staging_evidence_from_before_the_tag_named_this_commit(ceremony: Ceremony) -> None:
    """--auto deletes a tag whose own CI never went green (#940), and a later release can take
    the same number at another commit. deploy-release.yml and walk-release.yml run from main,
    so the head_sha of their runs never names the commit of the tag. Only the timeline binds a
    staging run to the commit: it must start after the tag's own CI run at that commit."""
    reused_from = ceremony.merges["11"]
    sha = head_sha(ceremony)
    ceremony.tag("v0.0.2")
    evidence = staging_evidence(
        sha,
        ci=[
            ci_run(2019, "v0.0.2", reused_from, "2026-10-19T10:00:00Z"),
            ci_run(EVIDENCE_TAG_RUN, "v0.0.2", sha, "2026-10-20T04:05:21Z"),
        ],
        deploys=[workflow_run(3029, "Deploy staging v0.0.2", "2026-10-19T10:05:00Z", "2026-10-19T10:12:00Z")],
        walks=[
            workflow_run(
                EVIDENCE_WALK_RUN,
                "Walk Deploy staging v0.0.2",
                "2026-10-19T10:12:05Z",
                "2026-10-19T10:14:00Z",
                event="workflow_run",
            )
        ],
    )

    result, calls = ceremony.run("v0.0.2", "--prod", "--owner-approved-sha", sha, extra_env=evidence)

    assert result.returncode != 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert not dispatched(calls)
    assert f"run {EVIDENCE_TAG_RUN}" in result.stderr, "the refusal must name the CI run that anchors the commit"
    assert REDEPLOY_HINT in result.stderr, result.stderr


def test_prod_on_a_tag_that_does_not_exist_is_refused_before_anything_is_tagged(ceremony: Ceremony) -> None:
    """A production promotion needs a tag that staging verified. A new number has no staging
    evidence, so --prod refuses it and names the staging release that produces the evidence."""
    sha = head_sha(ceremony)
    before = ceremony.remote_tags()

    result, calls = ceremony.run("v0.0.2", "--prod", "--owner-approved-sha", sha, extra_env=staging_evidence(sha))

    assert result.returncode != 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "does not exist on origin" in result.stderr, result.stderr
    assert 'tools/cut_release.sh v0.0.2 --message "..."' in result.stderr, result.stderr
    assert ceremony.remote_tags() == before, "a refused promotion must not claim the tag number"
    assert not dispatched(calls)


# --- #1412 R4: a release declares its data replays and its rollback tag ---------------------
# #1270: a projector fix reached main, and the next backtest ran on rows the old projector
# wrote. A release whose diff changes a parser, mapping or projector identity must name the
# replay that rebuilds those rows, and its tag records the replays and the rollback tag.

PARSER_V3 = 'PARSER_VERSION_HISTORY = ("demo-parser:v1", "demo-parser:v2", "demo-parser:v3")\n'
PARSER_REPLAY = {"identity": "demo-parser", "replay": "demo_reparse_job over every stored fetch"}


@pytest.mark.parametrize(
    ("path", "text", "identity"),
    [
        pytest.param(DEMO_PARSER, PARSER_V2, "demo-parser", id="parser-version"),
        pytest.param(
            DEMO_PROJECTOR,
            PROJECTOR_V1.replace("shares * close", "shares * close * 10"),
            "demo-projector",
            id="projector",
        ),
    ],
)
def test_a_version_change_without_a_declared_replay_is_refused_before_the_tag_push(
    ceremony: Ceremony, path: str, text: str, identity: str
) -> None:
    ceremony.write(path, text)
    ceremony.commit("change a data version (#13)")
    before = ceremony.remote_tags()

    result, calls = ceremony.run("v0.0.2")

    assert result.returncode != 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert identity in result.stderr and DECLARATION in result.stderr, result.stderr
    assert ceremony.remote_tags() == before, "an undeclared replay must fail before the tag push claims the number"
    assert not dispatched(calls)


def test_a_declared_replay_lets_the_release_through_and_the_tag_records_it(ceremony: Ceremony) -> None:
    ceremony.write(DEMO_PARSER, PARSER_V2)
    ceremony.write(DECLARATION, declaration(PARSER_REPLAY))
    ceremony.commit("parser v2 with its replay (#13)")

    result, calls = ceremony.run("v0.0.2")

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    lines = ceremony.tag_message("v0.0.2").splitlines()
    assert f"Replay: demo-parser: {PARSER_REPLAY['replay']}" in lines, lines
    assert "Rollback-Tag: v0.0.1" in lines, lines
    assert len(staging_dispatches(calls)) == 1


def test_a_replay_declared_for_an_earlier_release_does_not_cover_a_new_change(ceremony: Ceremony) -> None:
    """The declaration file keeps old entries. Only an entry that the base release does not
    hold belongs to this release; otherwise one old entry would cover every later bump."""
    ceremony.write(DEMO_PARSER, PARSER_V2)
    ceremony.write(DECLARATION, declaration(PARSER_REPLAY))
    ceremony.commit("parser v2 with its replay (#13)")
    ceremony.tag("v0.0.2")
    ceremony.write(DEMO_PARSER, PARSER_V3)
    ceremony.commit("parser v3 (#14)")

    result, calls = ceremony.run("v0.0.3")

    assert result.returncode != 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "demo-parser" in result.stderr, result.stderr
    assert "v0.0.3" not in ceremony.remote_tags()
    assert not dispatched(calls)


def test_comments_and_docstrings_are_not_a_version_change(ceremony: Ceremony) -> None:
    ceremony.write(
        DEMO_PROJECTOR,
        PROJECTOR_V1.replace('"""Demo projector."""', '"""Demo projector, reworded."""') + "# a comment\n",
    )
    ceremony.write(DEMO_PARSER, "# The parser vintages, oldest first.\n" + PARSER_V1)
    ceremony.commit("reword (#13)")

    result, calls = ceremony.run("v0.0.2")

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    lines = ceremony.tag_message("v0.0.2").splitlines()
    assert "Replay: none" in lines and "Rollback-Tag: v0.0.1" in lines, lines


def test_a_release_without_its_replay_declaration_is_refused(ceremony: Ceremony) -> None:
    """A deleted declaration must not turn the check into a silent pass."""
    ceremony.remove(DECLARATION)
    ceremony.commit("drop the declaration (#13)")
    before = ceremony.remote_tags()

    result, calls = ceremony.run("v0.0.2")

    assert result.returncode != 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert DECLARATION in result.stderr, result.stderr
    assert ceremony.remote_tags() == before
    assert not dispatched(calls)
