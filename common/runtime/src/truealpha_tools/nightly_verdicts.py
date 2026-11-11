"""Verify nightly in-environment Dagster verdicts reported on /api/health (#876)."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra2_sdk.deploy import HttpGet

GitRun = Callable[..., subprocess.CompletedProcess[str]]
FirstDeclared = Callable[[str, str], datetime | None]
ReleaseReader = Callable[[str], str | None]

REPO_ROOT = Path(__file__).resolve().parents[4]
EXPECTATIONS_PATH = REPO_ROOT / "tools" / "nightly_verdicts.json"
CONTRACT_PATH = REPO_ROOT / "common/contracts/src/truealpha_contracts/data/environment_contract.v1.json"
RELEASE_EXPECTATIONS_PATH = "tools/nightly_verdicts.json"
RELEASE_CONTRACT_PATH = "common/contracts/src/truealpha_contracts/data/environment_contract.v1.json"
STALE_AFTER_CADENCES = 2
FUTURE_TOLERANCE_HOURS = 1.0


class VerdictCheckFailure(RuntimeError):
    """The environment's nightly verdicts could not be read."""


@dataclass(frozen=True)
class Expectation:
    check: str
    cadence_hours: float
    job: str

    @property
    def max_age_hours(self) -> float:
        return self.cadence_hours * STALE_AFTER_CADENCES


@dataclass(frozen=True)
class Verdict:
    check: str
    ran_at: datetime
    ok: bool | None
    summary: str


@dataclass(frozen=True)
class HealthReport:
    verdicts: list[Verdict] | None
    release: str | None
    governed_pointers: object = None


def parse_expectations(text: str, lanes: Sequence[str] = ()) -> dict[str, Expectation]:
    raw = json.loads(text)
    entries = dict(raw["checks"])
    entries.update({f"{check}@{lane}": entry for check, entry in raw.get("per_universe", {}).items() for lane in lanes})
    return {
        name: Expectation(name, float(entry["cadence_hours"]), str(entry.get("job", "")))
        for name, entry in entries.items()
    }


def load_expectations(path: Path = EXPECTATIONS_PATH, contract_path: Path = CONTRACT_PATH) -> dict[str, Expectation]:
    return parse_expectations(
        path.read_text(encoding="utf-8"), scheduled_lanes(contract_path.read_text(encoding="utf-8"))
    )


def _universe_entries(spec: object) -> list[dict[str, str]]:
    entries = spec.get("universes", []) if isinstance(spec, dict) else None
    ok = isinstance(entries, list) and all(
        isinstance(entry, dict)
        and isinstance(entry.get("key"), str)
        and entry.get("status") in ("scheduled", "stopped")
        for entry in entries
    )
    if not ok:
        raise VerdictCheckFailure("the environment contract lists universes in a shape this tool cannot read")
    return entries  # type: ignore[return-value]


def _lane_names(contract: dict) -> dict[str, str]:
    records = contract.get("universes")
    if not isinstance(records, dict) or not all(
        isinstance(record, dict) and isinstance(record.get("lane_name"), str) for record in records.values()
    ):
        raise VerdictCheckFailure("the environment contract holds no universe records this tool can read")
    return {key: record["lane_name"] for key, record in records.items()}


def scheduled_lanes(contract_text: str, environment: str | None = None) -> list[str]:
    contract = json.loads(contract_text)
    lanes = _lane_names(contract)
    environments = contract.get("environments", {})
    if environment is None:
        specs = list(environments.values())
    else:
        name = "production" if environment.strip().lower() == "prod" else environment.strip().lower()
        specs = [environments[name]] if name in environments else []
    scheduled: list[str] = []
    for spec in specs:
        for entry in _universe_entries(spec):
            if entry["status"] == "scheduled" and lanes[entry["key"]] not in scheduled:
                scheduled.append(lanes[entry["key"]])
    return scheduled


def expected_in(expectations: dict[str, Expectation], scheduled: Sequence[str]) -> dict[str, Expectation]:
    return {
        name: expectation
        for name, expectation in expectations.items()
        if "@" not in name or name.partition("@")[2] in scheduled
    }


def _known(value: object) -> str | None:
    text = str(value or "")
    return text if text and text != "unknown" else None


def read_report(url: str, http_get: HttpGet) -> HealthReport:
    try:
        status, body = http_get(url)
    except Exception as exc:  # noqa: BLE001
        raise VerdictCheckFailure(f"{url} unreachable: {exc}") from exc
    if status != 200:
        raise VerdictCheckFailure(f"{url} answered HTTP {status}")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise VerdictCheckFailure(f"{url} did not answer JSON: {body[:80]!r}") from exc
    if not isinstance(payload, dict):
        raise VerdictCheckFailure(f"{url} did not answer a JSON object: {body[:80]!r}")
    release = _known(payload.get("data_engine_git_sha")) or _known(payload.get("git_sha"))
    governed_pointers = payload.get("governed_pointers")
    if "nightly_verdicts" not in payload:
        return HealthReport(None, release, governed_pointers)
    reported = payload["nightly_verdicts"]
    if reported == "unknown":
        raise VerdictCheckFailure(f'{url} could not read its nightly verdicts (reported "unknown")')
    if not isinstance(reported, list):
        raise VerdictCheckFailure(f"{url} reports nightly_verdicts of the wrong shape: {reported!r}")
    verdicts: list[Verdict] = []
    for entry in reported:
        try:
            ran_at = datetime.fromisoformat(str(entry["ran_at"]))
            if ran_at.tzinfo is None:
                ran_at = ran_at.replace(tzinfo=UTC)
            ok = entry["ok"]
            if ok is not None and not isinstance(ok, bool):
                raise TypeError(f"ok is {ok!r}, not a boolean or null")
            verdicts.append(Verdict(str(entry["check"]), ran_at, ok, str(entry.get("summary", ""))))
        except (KeyError, TypeError, ValueError) as exc:
            raise VerdictCheckFailure(f"{url} reports a malformed verdict entry: {entry!r}") from exc
    return HealthReport(verdicts, release, governed_pointers)


def _git_cmd(run: GitRun, repo: str, *args: str) -> subprocess.CompletedProcess[str]:
    return run(["git", "-C", repo, *args], capture_output=True, text=True, check=False)


def git_release_reader(
    repo: str = ".",
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    path: str = RELEASE_EXPECTATIONS_PATH,
) -> ReleaseReader:
    def read(ref: str) -> str | None:
        if ref.startswith("-"):
            raise VerdictCheckFailure(f"{ref!r} is not a release ref")
        resolved = _git_cmd(run, repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        commit = resolved.stdout.strip()
        if resolved.returncode != 0 or not commit:
            raise VerdictCheckFailure(f"{ref!r} is not a commit in this checkout (fetch tags)")
        listed = _git_cmd(run, repo, "ls-tree", "--name-only", commit, "--", path)
        if listed.returncode != 0:
            raise VerdictCheckFailure(f"cannot list {ref}'s tree: {listed.stderr.strip()[:120]}")
        if not listed.stdout.strip():
            return None
        shown = _git_cmd(run, repo, "show", f"{commit}:{path}")
        if shown.returncode != 0:
            raise VerdictCheckFailure(f"cannot read {path} at {ref}: {shown.stderr.strip()[:120]}")
        return shown.stdout

    return read


def expectations_for(
    release: str | None,
    reader: ReleaseReader,
    *,
    environment: str = "",
    contract_reader: ReleaseReader | None = None,
) -> tuple[dict[str, Expectation] | None, str]:
    if release is None:
        return load_expectations(), "the release identity is unreadable, so main's set is bounded"
    try:
        text = reader(release)
    except VerdictCheckFailure as exc:
        return load_expectations(), f"{exc}; main's set is bounded"
    if text is None:
        return None, f"{release} predates {RELEASE_EXPECTATIONS_PATH}"
    source = f"as {release} declares them"
    contract_text = (contract_reader or git_release_reader(path=RELEASE_CONTRACT_PATH))(release)
    try:
        lanes = scheduled_lanes(contract_text) if contract_text is not None else None
    except (VerdictCheckFailure, json.JSONDecodeError, AttributeError, TypeError):
        lanes = None
    if lanes is None and "per_universe" in json.loads(text):
        raise VerdictCheckFailure(f"the contract of {release} holds no universe records to expand its checks over")
    expectations = parse_expectations(text, lanes or [])
    if not environment or lanes is None or contract_text is None:
        return expectations, source
    scheduled = scheduled_lanes(contract_text, environment)
    kept = expected_in(expectations, scheduled)
    if len(kept) == len(expectations):
        return kept, source
    return kept, f"{source}; the universes {environment} schedules: {', '.join(sorted(scheduled)) or 'none'}"


def _utc(value: object) -> datetime:
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def first_declared_at(
    check: str,
    *,
    release: str,
    repo: str = ".",
    run: GitRun = subprocess.run,
) -> datetime | None:
    try:
        log = run(
            [
                "git",
                "-C",
                repo,
                "log",
                "--reverse",
                "--format=%H %cI",
                "-S",
                f'"{check}"',
                release,
                "--",
                RELEASE_EXPECTATIONS_PATH,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        lines = log.stdout.split("\n") if log.returncode == 0 else []
        first = lines[0].split() if lines and lines[0].strip() else []
        return _utc(first[1]) if len(first) == 2 else None
    except (VerdictCheckFailure, OSError, KeyError, TypeError, ValueError):
        return None


def _stale_message(name: str, verdict: Verdict, age: float, expectation: Expectation) -> str:
    return (
        f"{name}: newest verdict is from {verdict.ran_at.isoformat()} ({age:.1f} h ago, limit "
        f"{expectation.max_age_hours:g} h) — job {expectation.job or '?'} stopped ticking or stopped "
        f"reaching its verdict; check the Dagster daemon and the job's recent runs"
    )


def judge(
    verdicts: Sequence[Verdict],
    expectations: dict[str, Expectation],
    now: datetime,
    *,
    first_declared: Callable[[str], datetime | None] | None = None,
    notes: list[str] | None = None,
) -> list[str]:
    newest: dict[str, Verdict] = {}
    for entry in verdicts:
        if entry.check not in newest or entry.ran_at > newest[entry.check].ran_at:
            newest[entry.check] = entry
    failures: list[str] = []
    for name, expectation in sorted(expectations.items()):
        verdict = newest.get(name)
        if verdict is None:
            since = first_declared(name) if first_declared is not None else None
            if since is not None and (now - since).total_seconds() / 3600.0 < expectation.max_age_hours:
                if notes is not None:
                    notes.append(
                        f"{name}: no verdict yet — first expected here since {since.isoformat()} "
                        f"({(now - since).total_seconds() / 3600.0:.1f} h ago, limit {expectation.max_age_hours:g} h); "
                        f"its schedule has not had its chance"
                    )
                continue
            failures.append(
                f"{name}: no verdict at all — job {expectation.job or '?'} has never recorded one in this "
                f"environment; its schedule is not ticking, or the job never reaches its verdict"
            )
            continue
        age = (now - verdict.ran_at).total_seconds() / 3600.0
        if age < -FUTURE_TOLERANCE_HOURS:
            failures.append(
                f"{name}: newest verdict is dated {verdict.ran_at.isoformat()} ({-age:.1f} h in the future) — "
                f"a run launched with a future tick masks every real one, so this verdict cannot vouch for today"
            )
            continue
        if verdict.ok is None:
            stale = age > expectation.max_age_hours
            if stale:
                failures.append(_stale_message(name, verdict, age, expectation))
                continue
            if notes is not None:
                notes.append(f"{name}: pending ({verdict.summary})")
            continue
        if not verdict.ok:
            failures.append(f"{name}: red since {verdict.ran_at.isoformat()} ({age:.1f} h ago) — {verdict.summary}")
            continue
        if age > expectation.max_age_hours:
            failures.append(_stale_message(name, verdict, age, expectation))
    return failures


def check_nightly_verdicts(
    url: str,
    *,
    environment: str = "",
    http_get: HttpGet | None = None,
    reader: ReleaseReader | None = None,
    contract_reader: ReleaseReader | None = None,
    now: datetime | None = None,
    first_declared: FirstDeclared | None = None,
) -> int:
    if http_get is None:
        from infra2_sdk.deploy import default_http_get

        http_get = default_http_get()
    reader = reader or git_release_reader()
    name = environment or url
    try:
        report = read_report(url, http_get)
    except VerdictCheckFailure as exc:
        print(f"nightly verdict check failed: {exc}", file=sys.stderr)
        return 1
    if report.verdicts is None:
        print(f"{name} serves a release that predates nightly_verdicts on /api/health; nothing to bound yet")
        return 0
    try:
        expectations, source = expectations_for(
            report.release, reader, environment=environment, contract_reader=contract_reader
        )
    except VerdictCheckFailure as exc:
        print(f"nightly verdict check failed: {exc}", file=sys.stderr)
        return 1
    if expectations is None:
        print(f"{name}: the data engine release {source}; nothing to bound yet")
        return 0
    reference = now or datetime.now(UTC)
    notes: list[str] = []
    release = report.release
    since = (lambda check: first_declared(check, release)) if first_declared and release else None
    failures = judge(report.verdicts, expectations, reference, first_declared=since, notes=notes)
    for note in notes:
        print(f"{name}: pending: {note}")
    unexpected = sorted({entry.check for entry in report.verdicts} - set(expectations))
    if unexpected:
        print(f"{name}: not bounded (not declared by the deployed release): {', '.join(unexpected)}")
    if failures:
        for failure in failures:
            print(f"nightly verdict check failed: {name} {failure}", file=sys.stderr)
        print(f"({len(failures)} of {len(expectations)} expected checks; expectations {source})", file=sys.stderr)
        return 1
    print(f"{name}: all {len(expectations)} nightly checks are green and on time ({source})")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--environment", default="")
    parser.add_argument("--repo", default=".", help="checkout holding the release tags")
    arguments = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    return check_nightly_verdicts(
        arguments.url,
        environment=arguments.environment,
        reader=git_release_reader(arguments.repo),
        contract_reader=git_release_reader(arguments.repo, path=RELEASE_CONTRACT_PATH),
        first_declared=lambda check, release: first_declared_at(check, release=release, repo=arguments.repo),
    )


if __name__ == "__main__":
    raise SystemExit(main())
