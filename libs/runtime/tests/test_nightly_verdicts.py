"""Tests for tools/nightly_verdicts.py — the page the nightly Dagster checks never had (#876).

A red nightly run was a row in `dagster.runs` and nothing else; a stopped daemon looked like a
quiet night; the model key sat revoked in production until a human read the ledger (#832).
The check reads `/api/health`'s `nightly_verdicts` and fails on a red, stale or missing one.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from truealpha_runtime.testing import load_tool

_module = load_tool("nightly_verdicts")
check = _module.check_nightly_verdicts
REPO_ROOT = Path(__file__).resolve().parents[3]

URL = "https://truealpha-staging.truealpha.club/api/health"
NOW = datetime(2026, 9, 16, 7, 0, tzinfo=UTC)
EXPECTED = _module.load_expectations()


def _health(verdicts: object, *, status: int = 200, release: str = "v0.0.70") -> object:
    def http_get(url: str) -> tuple[int, str]:
        return status, json.dumps(
            {"status": "ok", "git_sha": release, "data_engine_git_sha": release, "nightly_verdicts": verdicts}
        )

    return http_get


def _main_set(_ref: str) -> str:
    """The deployed release declares exactly main's set."""
    return _module.EXPECTATIONS_PATH.read_text(encoding="utf-8")


def _verdict(name: str, hours_ago: float, *, ok: bool = True, summary: str = "ok") -> dict[str, object]:
    return {"check": name, "ran_at": (NOW - timedelta(hours=hours_ago)).isoformat(), "ok": ok, "summary": summary}


def _all_green(hours_ago: float = 7.0) -> list[dict[str, object]]:
    return [_verdict(name, hours_ago) for name in sorted(EXPECTED)]


def _run(verdicts: object, **kwargs: object) -> int:
    kwargs.setdefault("reader", _main_set)
    return check(URL, environment="staging", http_get=_health(verdicts), now=NOW, **kwargs)


def test_every_check_green_and_on_time_passes(capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(_all_green()) == 0
    assert "green and on time" in capsys.readouterr().out


def test_a_red_verdict_fails_and_names_the_check_and_its_summary(capsys: pytest.CaptureFixture[str]) -> None:
    """The #832 shape: the probe ran on time and the provider refused the key."""
    verdicts = _all_green()
    verdicts = [v for v in verdicts if v["check"] != "model_key_health"]
    verdicts.append(_verdict("model_key_health", 1.0, ok=False, summary="failed: auth-rejected: HTTP 401"))
    assert _run(verdicts) == 1
    err = capsys.readouterr().err
    assert "model_key_health" in err and "auth-rejected: HTTP 401" in err
    assert "output_invariants" not in err


def test_a_stale_verdict_fails_even_when_green(capsys: pytest.CaptureFixture[str]) -> None:
    """The daemon-died shape: the last verdict was green, two days and more ago."""
    limit = EXPECTED["output_invariants"].max_age_hours
    verdicts = [v for v in _all_green() if v["check"] != "output_invariants"]
    verdicts.append(_verdict("output_invariants", limit + 1))
    assert _run(verdicts) == 1
    err = capsys.readouterr().err
    assert "output_invariants" in err and "stopped ticking" in err


def test_the_bound_is_twice_the_cadence() -> None:
    for expectation in EXPECTED.values():
        assert expectation.max_age_hours == 2 * expectation.cadence_hours
    within = [v for v in _all_green() if v["check"] != "output_invariants"]
    within.append(_verdict("output_invariants", EXPECTED["output_invariants"].max_age_hours - 1))
    assert _run(within) == 0


def test_a_missing_check_fails(capsys: pytest.CaptureFixture[str]) -> None:
    """A check the release declares and never recorded: nothing ever ticked it."""
    verdicts = [v for v in _all_green() if v["check"] != "theme_purity@topt"]
    assert _run(verdicts) == 1
    assert "theme_purity@topt: no verdict at all" in capsys.readouterr().err


def test_an_empty_report_fails_for_every_expected_check(capsys: pytest.CaptureFixture[str]) -> None:
    assert _run([]) == 1
    err = capsys.readouterr().err
    assert all(name in err for name in EXPECTED)


def test_the_newest_verdict_per_check_is_the_one_judged() -> None:
    verdicts = [v for v in _all_green() if v["check"] != "output_invariants"]
    verdicts += [_verdict("output_invariants", 30.0, ok=False), _verdict("output_invariants", 6.0)]
    assert _run(verdicts) == 0


def test_the_age_is_measured_against_the_checkers_clock() -> None:
    """The entry carries a timestamp, not an age: a service's clock cannot vouch for it."""
    verdicts = [v for v in _all_green() if v["check"] != "report_surface_proof"]
    verdicts.append(_verdict("report_surface_proof", 96.0))
    assert _run(verdicts) == 1


def test_a_future_dated_verdict_fails_instead_of_masking_the_schedule(capsys: pytest.CaptureFixture[str]) -> None:
    """The health read orders by ran_at, so a run launched with a future tick would stand in
    front of every scheduled one and never age: green forever, whatever the daemon does."""
    verdicts = [v for v in _all_green() if v["check"] != "output_invariants"]
    verdicts.append(_verdict("output_invariants", -30.0))
    assert _run(verdicts) == 1
    assert "in the future" in capsys.readouterr().err
    skewed = [v for v in _all_green() if v["check"] != "output_invariants"]
    skewed.append(_verdict("output_invariants", -_module.FUTURE_TOLERANCE_HOURS / 2))
    assert _run(skewed) == 0, "ordinary clock skew is not a failure"


def test_a_release_before_the_report_is_not_a_failure(capsys: pytest.CaptureFixture[str]) -> None:
    def http_get(url: str) -> tuple[int, str]:
        return 200, json.dumps({"status": "ok", "git_sha": "v0.0.56"})

    assert check(URL, environment="production", http_get=http_get, reader=_main_set, now=NOW) == 0
    assert "predates" in capsys.readouterr().out


def test_a_data_engine_release_before_the_expectations_bounds_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    """The service reports the field while the engine that writes the rows is older."""
    assert _run([], reader=lambda ref: None) == 0
    assert "predates" in capsys.readouterr().out


def test_a_check_added_on_main_is_not_demanded_of_a_release_that_does_not_run_it() -> None:
    """Production lags main by design (14 days): a check a later PR adds must not turn its
    leg red until a release that records it is deployed there."""
    released = json.loads(_module.EXPECTATIONS_PATH.read_text(encoding="utf-8"))
    del released["checks"]["model_key_health"]
    verdicts = [v for v in _all_green() if v["check"] != "model_key_health"]
    assert _run(verdicts, reader=lambda ref: json.dumps(released)) == 0
    assert _run(verdicts) == 1, "the same report is red against a release that declares the probe"


def test_the_expectations_are_read_at_the_data_engines_release() -> None:
    asked: list[str] = []

    def reader(ref: str) -> str:
        asked.append(ref)
        return _main_set(ref)

    def http_get(url: str) -> tuple[int, str]:
        return 200, json.dumps(
            {"git_sha": "v0.0.71", "data_engine_git_sha": "v0.0.70", "nightly_verdicts": _all_green()}
        )

    assert check(URL, http_get=http_get, reader=reader, now=NOW) == 0
    assert asked == ["v0.0.70"]


def test_an_unresolvable_release_bounds_mains_set_rather_than_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    def unknown_ref(ref: str) -> str:
        raise _module.VerdictCheckFailure(f"{ref!r} is not a commit in this checkout")

    assert _run([], reader=unknown_ref) == 1
    assert "main's set" in capsys.readouterr().err


def test_a_verdict_the_release_no_longer_declares_is_reported_not_bounded(capsys: pytest.CaptureFixture[str]) -> None:
    verdicts = [*_all_green(), _verdict("retired_check", 400.0, ok=False)]
    assert _run(verdicts) == 0
    assert "retired_check" in capsys.readouterr().out


def test_an_unreadable_report_fails_never_passes_by_omission(capsys: pytest.CaptureFixture[str]) -> None:
    assert _run("unknown") == 1
    assert "unknown" in capsys.readouterr().err


def test_unreachable_non_json_or_malformed_fails() -> None:
    assert check(URL, http_get=_health([], status=503), reader=_main_set, now=NOW) == 1

    def broken(url: str) -> tuple[int, str]:
        return 200, "<html>gateway</html>"

    assert check(URL, http_get=broken, reader=_main_set, now=NOW) == 1
    assert _run([{"check": "output_invariants"}]) == 1
    assert _run([{**_verdict("output_invariants", 1.0), "ok": "true"}]) == 1, "ok must be a boolean"
    assert _run({"output_invariants": True}) == 1


def test_a_naive_timestamp_is_read_as_utc() -> None:
    verdicts = [
        {**entry, "ran_at": (NOW - timedelta(hours=7)).replace(tzinfo=None).isoformat()} for entry in _all_green()
    ]
    assert _run(verdicts) == 0


def test_the_git_reader_reads_the_file_as_each_release_declared_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Isolate every git call below from the repository this test runs in. A git hook (the
    # pre-push run) exports GIT_DIR and friends, which would otherwise point `init`, `add`
    # and `commit` at the OUTER repository; the identity travels as environment variables
    # so nothing is ever written to any git config, and user/system config is not read.
    for name in [name for name in os.environ if name.startswith("GIT_")]:
        monkeypatch.delenv(name)
    scratch = tmp_path / "release-repo"
    scratch.mkdir()
    for name, value in {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CEILING_DIRECTORIES": str(tmp_path),
        "GIT_AUTHOR_NAME": "release-reader-test",
        "GIT_AUTHOR_EMAIL": "release-reader-test@example.invalid",
        "GIT_COMMITTER_NAME": "release-reader-test",
        "GIT_COMMITTER_EMAIL": "release-reader-test@example.invalid",
    }.items():
        monkeypatch.setenv(name, value)

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(scratch), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    subprocess.run(["git", "init", "-q", str(scratch)], check=True, capture_output=True)
    # Refuse to write anything unless the scratch directory is its own repository.
    assert Path(git("rev-parse", "--absolute-git-dir")).resolve() == (scratch / ".git").resolve()
    (scratch / "README").write_text("before\n", encoding="utf-8")
    git("add", "README")
    git("commit", "-q", "-m", "before")
    git("tag", "v0.0.1")
    (scratch / "tools").mkdir()
    (scratch / "tools" / "nightly_verdicts.json").write_text('{"checks": {}}', encoding="utf-8")
    git("add", "tools")
    git("commit", "-q", "-m", "after")
    git("tag", "v0.0.2")

    read = _module.git_release_reader(str(scratch))
    assert read("v0.0.1") is None
    assert json.loads(read("v0.0.2") or "") == {"checks": {}}
    with pytest.raises(_module.VerdictCheckFailure):
        read("v9.9.9")
    with pytest.raises(_module.VerdictCheckFailure):
        read("--output=/tmp/x")


def test_a_git_failure_is_never_read_as_a_release_that_predates_the_file() -> None:
    """ "Predates" passes the check, so only an empty tree listing may mean it: a failing
    listing or read falls back to main's set (red when verdicts are missing), never to 0."""

    def run_failing(step: str) -> object:
        def run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if "rev-parse" in args:
                return subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", "")
            if step in args:
                return subprocess.CompletedProcess(args, 128, "", "fatal: bad object")
            return subprocess.CompletedProcess(args, 0, "tools/nightly_verdicts.json\n", "")

        return run

    for step in ("ls-tree", "show"):
        read = _module.git_release_reader(".", run=run_failing(step))
        with pytest.raises(_module.VerdictCheckFailure):
            read("v0.0.70")
        assert _run([], reader=read) == 1, f"a failing git {step} passed the check"


# --- one list: what the lanes record is what the tool watches -------------------------


def test_the_lanes_record_exactly_the_checks_the_tool_watches() -> None:
    """A check a lane records and the tool does not know about is un-watched; a check the
    tool expects and no lane records is red forever. Both directions, from the lanes'
    own declarations (`NIGHTLY_VERDICTS`), not from a copy of them."""
    from data_engine.lanes import nightly_verdict_names

    recorded = nightly_verdict_names()
    assert recorded, "no lane declares a nightly verdict"
    assert set(EXPECTED) == set(recorded), (
        f"tools/nightly_verdicts.json {sorted(set(EXPECTED) - recorded)} not recorded by any lane; "
        f"recorded but not watched: {sorted(recorded - set(EXPECTED))}"
    )


def test_every_watched_check_names_a_deployed_job_and_a_valid_verdict_name() -> None:
    from data_engine.dagster_defs import defs
    from data_engine.quality.nightly_verdicts import is_valid_name

    jobs = {job.name for job in defs.jobs or ()}
    for name, expectation in EXPECTED.items():
        assert is_valid_name(name), f"{name} would be refused by mart.nightly_verdicts' check constraint"
        assert expectation.job in jobs, f"{name} names job {expectation.job!r}, which the deployed root lacks"
        assert expectation.cadence_hours > 0


def test_every_watched_job_is_scheduled() -> None:
    """A verdict whose job is not scheduled can only ever go stale."""
    from data_engine.dagster_defs import defs

    scheduled = {schedule.job_name for schedule in defs.schedules or ()}
    for name, expectation in EXPECTED.items():
        assert expectation.job in scheduled, f"{name}: job {expectation.job} has no schedule in the deployed root"
