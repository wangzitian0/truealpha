"""#1248: one shared action installs uv and retries the GitHub download failures.

On 2026-10-12 GitHub Releases answered 504 for about 2 minutes. Four CI jobs failed while
every test was green, and a red main blocked the staging release. Two downloads failed:
the infra2-sdk wheel that `uv sync` fetches, and the uv archive that astral-sh/setup-uv fetches.
`.github/actions/setup-uv/action.yml` holds the retry numbers. Three rules keep it true:

1. Every job that runs uv installs it through that action, before the first uv command.
2. The action sets UV_HTTP_RETRIES once, to a value that covers the outage.
3. The action runs astral-sh/setup-uv up to 3 times, with a pause before each retry.
   The last try fails the job when the failure lasts.

Each rule has a checker that returns one message for each break. The tests run each checker on
the real files, and on broken copies of them. A checker that never failed is not evidence of
protection. The test that the ci-required path filters reach the action is in
`test_ci_workflows.py`, beside the other path filter tests.
"""

from __future__ import annotations

import copy
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
ACTION_PATH = ".github/actions/setup-uv/action.yml"
ACTION_USE = "./.github/actions/setup-uv"
UPSTREAM_ACTION = "astral-sh/setup-uv@v5"

#: Seconds in which GitHub Releases answered 504 on 2026-10-12 (11:59:41 to 12:01:44 UTC).
OUTAGE_SECONDS = 123
#: Seconds to wait before the second and before the third try of astral-sh/setup-uv.
PAUSES = ("20", "60")

#: A command that starts with uv: `uv sync`, `uv run`, `uvx tool`, also after `&&`, `|` or `$(`.
UV_COMMAND = re.compile(r"(?:^|[\s;&|(])uvx?\s+[a-z]")


def load_workflows() -> dict[str, dict[str, Any]]:
    return {path.name: yaml.safe_load(path.read_text(encoding="utf-8")) for path in sorted(WORKFLOWS.glob("*.yml"))}


def load_action() -> dict[str, Any]:
    return yaml.safe_load((REPO_ROOT / ACTION_PATH).read_text(encoding="utf-8"))


# --- rule 1: every job that runs uv installs it through the shared action ----------------


def runs_uv(step: dict[str, Any]) -> bool:
    lines = str(step.get("run", "")).splitlines()
    return any(UV_COMMAND.search(line) for line in lines if not line.lstrip().startswith("#"))


def job_steps(job: Any) -> list[dict[str, Any]]:
    return list(job.get("steps") or []) if isinstance(job, dict) else []


def jobs_that_run_uv(workflows: dict[str, dict[str, Any]]) -> list[tuple[str, str]]:
    return [
        (name, job_id)
        for name, workflow in workflows.items()
        for job_id, job in (workflow.get("jobs") or {}).items()
        if any(runs_uv(step) for step in job_steps(job))
    ]


def one_job(*steps: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """A workflow set with one workflow, one job and these steps. For the red cases."""
    return {"w.yml": {"jobs": {"j": {"steps": list(steps)}}}}


def uv_install_problems(workflows: dict[str, dict[str, Any]]) -> list[str]:
    """One message for each job that calls setup-uv directly, or runs uv before the shared action."""
    problems: list[str] = []
    for name, workflow in workflows.items():
        for job_id, job in (workflow.get("jobs") or {}).items():
            steps = job_steps(job)
            where = f"{name}:{job_id}"
            if any(str(step.get("uses", "")).startswith("astral-sh/setup-uv") for step in steps):
                problems.append(f"{where} calls astral-sh/setup-uv directly; call {ACTION_USE} instead")
            first_uv = next((i for i, step in enumerate(steps) if runs_uv(step)), None)
            if first_uv is None:
                continue
            installs = [i for i, step in enumerate(steps) if step.get("uses") == ACTION_USE]
            if not installs or installs[0] > first_uv:
                problems.append(f"{where} runs uv at step {first_uv} without {ACTION_USE} before it")
    return problems


# --- rules 2 and 3: the shared action ----------------------------------------------------


def expected_wait_seconds(retries: int) -> float:
    """Mean total seconds that uv 0.13.0 waits across `retries` retries.

    Before retry n (n starts at 0), uv waits a random time from 1 s to min(30, 2 ** (n + 1)) s.
    Source: `retry_policy` in uv's crates/uv-client/src/base_client.rs (Jitter::Bounded,
    bounds 2 s to 30 s). The default of 3 retries waits 8.5 s on average.
    """
    return sum((1 + min(30, 2 ** (n + 1))) / 2 for n in range(retries))


def run_step_script(script: str, *, extra_env: dict[str, str], path: str) -> subprocess.CompletedProcess[str]:
    """Run the script text of one step the way a runner does for `shell: bash`."""
    bash = shutil.which("bash")
    assert bash is not None, "bash is required to run the step scripts of the shared action"
    return subprocess.run(
        [bash, "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
        env={"PATH": path, **extra_env},
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )


def exported_variables(script: str) -> dict[str, str]:
    """The variables that a step script writes to the file named by GITHUB_ENV."""
    with tempfile.TemporaryDirectory() as tmp:
        env_file = Path(tmp) / "github_env"
        env_file.touch()
        run_step_script(script, extra_env={"GITHUB_ENV": str(env_file)}, path=os.environ["PATH"])
        lines = env_file.read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def sleep_arguments(script: str) -> list[str]:
    """The arguments that a step script passes to `sleep`.

    A shell function with that name runs instead of the program. It records the arguments
    and returns at once, so the script cannot wait and no file needs to be executable.
    """
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "sleep.log"
        function = f'sleep() {{ echo "$@" >> "{log}"; }}\n'
        run_step_script(function + script, extra_env={}, path=os.environ["PATH"])
        return log.read_text(encoding="utf-8").split() if log.exists() else []


def export_steps(action: dict[str, Any]) -> list[dict[str, Any]]:
    return [spec for spec in action["runs"]["steps"] if "UV_HTTP_RETRIES" in str(spec.get("run", ""))]


def exported_retry_count(action: dict[str, Any]) -> int:
    """The value that the action's export step puts into GITHUB_ENV."""
    (spec,) = export_steps(action)
    return int(exported_variables(str(spec["run"]))["UV_HTTP_RETRIES"])


def action_problems(action: dict[str, Any]) -> list[str]:
    """One message for each way that the shared action breaks rule 2 or rule 3."""
    problems: list[str] = []
    steps: list[dict[str, Any]] = action["runs"]["steps"]

    # Rule 2: the retry count is exported once, and it covers the outage.
    exporters = export_steps(action)
    if len(exporters) != 1:
        problems.append(f"{len(exporters)} steps mention UV_HTTP_RETRIES; expected exactly 1")
    else:
        value = exported_variables(str(exporters[0]["run"])).get("UV_HTTP_RETRIES", "")
        if not value.isdigit():
            problems.append(f"UV_HTTP_RETRIES is {value!r} in GITHUB_ENV after the step ran; expected an integer")
        elif expected_wait_seconds(int(value)) < OUTAGE_SECONDS:
            problems.append(
                f"{value} retries wait {expected_wait_seconds(int(value)):.0f} s on average, "
                f"and the outage lasted {OUTAGE_SECONDS} s"
            )

    # Rule 3: three tries of setup-uv, each retry after a pause, the last try without a safety net.
    positions = [i for i, spec in enumerate(steps) if spec.get("uses") == UPSTREAM_ACTION]
    if len(positions) != 3:
        problems.append(f"the action runs {UPSTREAM_ACTION} {len(positions)} time(s); expected 3 tries")
        return problems
    tries = [steps[i] for i in positions]
    for number, spec in enumerate(tries, start=1):
        if not spec.get("id"):
            problems.append(f"try {number} has no id, so the next try cannot read its outcome")
        if spec.get("with", {}).get("enable-cache") != "${{ inputs.enable-cache }}":
            problems.append(f"try {number} must pass the enable-cache input unchanged")
    if "if" in tries[0]:
        problems.append("try 1 must run without a condition")
    for number in (1, 2):
        if tries[number - 1].get("continue-on-error") is not True:
            problems.append(f"try {number} must set continue-on-error: true, or its failure ends the job")
    if "continue-on-error" in tries[2]:
        problems.append("try 3 must not set continue-on-error: a failure that lasts must fail the job")
    for number in (2, 3):
        condition = f"steps.{tries[number - 2].get('id')}.outcome == 'failure'"
        if tries[number - 1].get("if") != condition:
            problems.append(f"try {number} must run only if {condition}; it has {tries[number - 1].get('if')!r}")
        pause = steps[positions[number - 1] - 1]
        if pause.get("if") != condition:
            problems.append(f"the pause before try {number} must run only if {condition}; it has {pause.get('if')!r}")
        if sleep_arguments(str(pause.get("run", ""))) != [PAUSES[number - 2]]:
            problems.append(f"the pause before try {number} must run `sleep {PAUSES[number - 2]}` and nothing else")

    # The input keeps the default of the upstream action, so a caller that passes nothing sees no change.
    if action.get("inputs", {}).get("enable-cache", {}).get("default") != "auto":
        problems.append("the enable-cache input must default to auto, the default of astral-sh/setup-uv")
    return problems


def files_that_set_the_retry_count() -> list[str]:
    assignment = re.compile(r"UV_HTTP_RETRIES\s*[=:]")
    return [
        str(path.relative_to(REPO_ROOT))
        for path in sorted((REPO_ROOT / ".github").rglob("*"))
        if path.is_file() and assignment.search(path.read_text(encoding="utf-8", errors="replace"))
    ]


# --- breaks of the action, to prove that the checker reports them ------------------------


def _steps(action: dict[str, Any]) -> list[dict[str, Any]]:
    return action["runs"]["steps"]


def _tries(action: dict[str, Any]) -> list[dict[str, Any]]:
    return [spec for spec in _steps(action) if spec.get("uses") == UPSTREAM_ACTION]


def _pause_before(action: dict[str, Any], number: int) -> dict[str, Any]:
    steps = _steps(action)
    return steps[steps.index(_tries(action)[number - 1]) - 1]


def _set_run(step: dict[str, Any], script: str) -> None:
    step["run"] = script


def _remove(action: dict[str, Any], *specs: dict[str, Any]) -> None:
    for spec in specs:
        _steps(action).remove(spec)


#: Name of a break -> (the change to a copy of the action, a text that the checker must report).
MUTATIONS: dict[str, tuple[Callable[[dict[str, Any]], None], str]] = {
    "the retry export is missing": (lambda a: _remove(a, *export_steps(a)), "UV_HTTP_RETRIES"),
    "the retry count is the uv default of 3": (
        lambda a: _set_run(export_steps(a)[0], 'echo "UV_HTTP_RETRIES=3" >> "$GITHUB_ENV"'),
        "outage lasted",
    ),
    "the retry count goes to stdout, not to GITHUB_ENV": (
        lambda a: _set_run(export_steps(a)[0], 'echo "UV_HTTP_RETRIES=12"'),
        "after the step ran",
    ),
    "try 1 can fail the job": (lambda a: _tries(a)[0].pop("continue-on-error"), "try 1 must set continue-on-error"),
    "try 2 runs although try 1 passed": (lambda a: _tries(a)[1].pop("if"), "try 2 must run only if"),
    "try 2 has no pause before it": (lambda a: _remove(a, _pause_before(a, 2)), "pause before try 2"),
    "the pause before try 3 is 5 s": (lambda a: _set_run(_pause_before(a, 3), "sleep 5"), "pause before try 3"),
    "try 3 hides a failure that lasts": (lambda a: _tries(a)[2].update({"continue-on-error": True}), "try 3 must not"),
    "try 2 drops the enable-cache input": (lambda a: _tries(a)[1].pop("with"), "try 2 must pass the enable-cache"),
    "only two tries": (lambda a: _remove(a, _pause_before(a, 3), _tries(a)[2]), "expected 3 tries"),
}


def problems_after_break(name: str) -> tuple[list[str], str]:
    """Break a copy of the real action as `MUTATIONS[name]` says. Return what the checker reports."""
    mutate, expected = MUTATIONS[name]
    broken = copy.deepcopy(load_action())
    mutate(broken)
    return action_problems(broken), expected


# --- the tests ---------------------------------------------------------------------------

_SHARED_UV = {"uses": ACTION_USE}
_UV_SYNC = {"run": "uv sync --frozen --all-packages"}


def test_every_job_that_runs_uv_installs_it_through_the_shared_action() -> None:
    workflows = load_workflows()
    assert uv_install_problems(workflows) == []

    # GREEN-WHILE-EMPTY guard: 11 jobs ran uv when #1248 was fixed. A detector that finds none
    # reports no problem for every workflow.
    jobs = jobs_that_run_uv(workflows)
    assert ("ci-python.yml", "gates") in jobs and ("deploy-release.yml", "request") in jobs, jobs
    assert len(jobs) >= 11, f"the scan found {len(jobs)} jobs that run uv; 11 ran uv when #1248 was fixed: {jobs}"


@pytest.mark.parametrize(
    ("steps", "fragment", "count"),
    [
        # A direct call is one break. The job then also lacks the shared action before uv.
        pytest.param([{"uses": UPSTREAM_ACTION}, _UV_SYNC], "directly", 2, id="direct call of setup-uv"),
        pytest.param([_UV_SYNC], "without", 1, id="uv with no install step"),
        pytest.param([_UV_SYNC, _SHARED_UV], "without", 1, id="install after the first uv command"),
        pytest.param([{"run": "echo start\nuv run pytest"}], "without", 1, id="uv on a later script line"),
        pytest.param([{"run": "make x && uvx ruff check"}], "without", 1, id="uvx after &&"),
    ],
)
def test_the_uv_install_check_reports_each_break(steps: list[dict[str, str]], fragment: str, count: int) -> None:
    problems = uv_install_problems(one_job(*steps))
    assert len(problems) == count and any(fragment in problem for problem in problems), problems


def test_the_uv_install_check_accepts_the_shared_action_before_uv() -> None:
    assert uv_install_problems(one_job(_SHARED_UV, _UV_SYNC)) == []


def test_the_uv_install_check_ignores_a_comment_that_names_uv() -> None:
    assert uv_install_problems(one_job({"run": "# uv sync runs later\necho done"})) == []


def test_the_shared_uv_action_meets_the_retry_contract() -> None:
    assert action_problems(load_action()) == []


def test_only_the_shared_uv_action_sets_the_retry_count() -> None:
    """One definition: a second place that sets the number can drift from the first."""
    assert files_that_set_the_retry_count() == [ACTION_PATH]


def test_the_uv_retry_budget_covers_the_outage_that_failed_four_jobs() -> None:
    """The default budget is below the 123 s outage of 2026-10-12. The exported budget is not.

    The default of 3 retries waits 8.5 s on average. That is why the wheel download
    failed after 10.8 s, 13.5 s and 21.1 s.
    """
    action = load_action()
    assert len(export_steps(action)) == 1, "the shared action must export UV_HTTP_RETRIES in one step"
    retries = exported_retry_count(action)
    wait = expected_wait_seconds
    assert wait(3) < OUTAGE_SECONDS <= wait(retries), (
        f"mean wait: {wait(3):.1f} s for uv's default of 3 retries, {wait(retries):.1f} s for {retries} retries, "
        f"outage {OUTAGE_SECONDS} s"
    )


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_the_uv_retry_check_reports_each_break_of_the_action(name: str) -> None:
    problems, expected = problems_after_break(name)
    assert any(expected in problem for problem in problems), f"{name!r}: expected {expected!r} in {problems}"
