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
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\n")

def flag(name):
    return args[args.index(name) + 1] if name in args else ""

merges = json.loads(os.environ["FAKE_GH_MERGES"])
if args[:2] == ["pr", "view"]:
    fields = flag("--json")
    if fields == "state":
        print("MERGED" if args[2] in merges else "OPEN")
    elif fields == "mergeCommit":
        print(merges[args[2]])
    else:
        sys.exit(f"fake gh: unexpected pr view fields {fields!r}")
elif args[:2] == ["api", "graphql"]:
    print(json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {"totalCount": 0, "nodes": []}}}}}))
elif args[:2] == ["run", "list"]:
    workflow = flag("--workflow")
    if workflow == "ci-required.yml":
        print("101 completed success")
    elif workflow == "":
        print("202 completed success")  # the tag's own push run
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


@dataclass
class Ceremony:
    root: Path
    work: Path
    merges: dict[str, str]

    def commit(self, subject: str) -> str:
        git(self.work, "commit", "--allow-empty", "-q", "-m", subject)
        sha = git(self.work, "rev-parse", "HEAD")
        number = subject.rsplit("(#", 1)[1].rstrip(")")
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

    def run(self, tag: str, *arguments: str, served: str = "") -> tuple[subprocess.CompletedProcess[str], list]:
        log = self.root / "gh.log"
        log.write_text("", encoding="utf-8")
        env = {
            **git_env(self.work),
            "PATH": f"{self.root / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "FAKE_GH_LOG": str(log),
            "FAKE_GH_MERGES": json.dumps(self.merges),
            "FAKE_SERVED_TAG": served or tag,
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
