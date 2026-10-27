"""Did every nightly in-environment check run, and was it green? (#876 W1/W2)

The output invariants, the report surface proof, the confidence report, the daily head
reports and the model-provider key probe run inside each environment's own Dagster daemon.
Until #876 a red run was a row in `dagster.runs` and nothing else, and a daemon that stopped
ticking was indistinguishable from a quiet night — the model key sat revoked in production
until a human read the call ledger (#832). Each check now appends its verdict, green or red,
to `mart.nightly_verdicts`; `/api/health` reports the newest verdict per check
(`nightly_verdicts`), and this check fails when an expected check's newest verdict is
one of the cases below. The daily delivery report (`delivery_report.py`, #1412) runs
these rules for each environment and shows the result; it opens no issue (ADR A8):

  * red (`ok` false): the check ran and failed;
  * stale (older than twice the check's cadence): the schedule stopped ticking, or the job
    stopped reaching its verdict;
  * missing: the environment has never recorded it — unless the check is NEW here: the first
    successful deploy to this environment of a release that declares it finished less than
    twice its cadence ago, so its schedule has not had its chance yet (`--deploy-type`; the
    grace is reported, and it is measured from that FIRST deploy, never from the latest one,
    or a staging that releases several times a day would forgive a check that never ticks).

Which checks are expected is declared once, in `tools/nightly_verdicts.json`, and
`common/runtime/tests/test_nightly_verdicts.py` holds that set equal to what the lanes record.
A per-universe check (`per_universe` in that file) names no universe: the tool expands it over the lane
names of the universes that some environment of the release's contract schedules (#1522).
The set is read AS THE DEPLOYED RELEASE DECLARED IT (`git show <release>:tools/...`): a check
added on main is not demanded of a production release that lags by design and does not run
it yet, and a release that predates the file bounds nothing. When the release cannot be
resolved here, main's set is used — a false red beats a silent miss.

A per-universe check (`theme_purity@universe-list:qqq`) is expected only in an environment whose
contract schedules that universe. The contract is read AS THE SAME RELEASE DECLARED IT
(`environment_contract.v1.json`): staging runs TOPT only, so it is not asked for a QQQ verdict
(owner decision of 2026-10-21).

Exit codes:
  0 - every expected check's newest verdict is green and within twice its cadence, or the
      environment serves a release that predates the report
  1 - a verdict is red, stale or missing, the report is unreadable, or the endpoint is
      unreachable
"""

from __future__ import annotations

import argparse
import functools
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

# The SDK is imported where the default HTTP client is built, not here (#1412). The delivery
# report reuses this module on a bare `python3` without the SDK.
if TYPE_CHECKING:
    from infra2_sdk.deploy import HttpGet

GhApi = Callable[[str], str]
GitRun = Callable[..., subprocess.CompletedProcess[str]]
#: (check, deployed release) -> the check's first moment of being expected here, or None
#: when that cannot be shown.
FirstDeclared = Callable[[str, str], datetime | None]
#: Successful deploy-release.yml runs, newest first: "Deploy <deploy_type> <tag>".
DEPLOY_RUNS_PATH = "/repos/wangzitian0/truealpha/actions/workflows/deploy-release.yml/runs?status=success&per_page=100"

EXPECTATIONS_PATH = Path(__file__).resolve().parents[4] / "tools" / "nightly_verdicts.json"
CONTRACT_PATH = (
    Path(__file__).resolve().parents[4]
    / "common"
    / "contracts"
    / "src"
    / "truealpha_contracts"
    / "data"
    / "environment_contract.v1.json"
)
#: The same file as a release's tree names it.
RELEASE_EXPECTATIONS_PATH = "tools/nightly_verdicts.json"
#: The environment contract as a release's tree names it. It lists the universes of each environment.
RELEASE_CONTRACT_PATH = "common/contracts/src/truealpha_contracts/data/environment_contract.v1.json"
#: A daily check that missed one tick is late; one that missed two has stopped.
STALE_AFTER_CADENCES = 2
#: Clock skew allowed between the environment and this runner. A verdict dated further
#: ahead than this cannot vouch for today: the health read orders by `ran_at`, so a run
#: launched with a future tick would stand in front of every real one and never go stale.
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
    #: None when the release predates the report (no key at all — an older build).
    verdicts: list[Verdict] | None
    #: The release whose data engine writes the verdicts, when the endpoint names one.
    release: str | None
    #: The governed pointer statuses reported on `/api/health`, or None when absent.
    governed_pointers: object = None


def parse_expectations(text: str, lanes: Sequence[str] = ()) -> dict[str, Expectation]:
    """The checks of an expectations file. Each ``per_universe`` check becomes one check per lane name in ``lanes``."""
    raw = json.loads(text)
    entries = {name: entry for name, entry in raw["checks"].items()}
    entries.update({f"{check}@{lane}": entry for check, entry in raw.get("per_universe", {}).items() for lane in lanes})
    return {
        name: Expectation(name, float(entry["cadence_hours"]), str(entry.get("job", "")))
        for name, entry in entries.items()
    }


def load_expectations(path: Path = EXPECTATIONS_PATH, contract_path: Path = CONTRACT_PATH) -> dict[str, Expectation]:
    """The expected checks of this checkout, per-universe checks expanded over its contract."""
    return parse_expectations(
        path.read_text(encoding="utf-8"), scheduled_lanes(contract_path.read_text(encoding="utf-8"))
    )


def _universe_entries(spec: object) -> list[dict[str, str]]:
    """The ``universes`` of one environment, or [] when it lists none. A shape this tool cannot read raises."""
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
    """key -> lane name, for every universe record of the contract."""
    records = contract.get("universes")
    ok = isinstance(records, dict) and all(
        isinstance(record, dict) and isinstance(record.get("lane_name"), str) for record in records.values()
    )
    if not ok:
        raise VerdictCheckFailure("the environment contract holds no universe records this tool can read")
    return {key: record["lane_name"] for key, record in records.items()}


def scheduled_lanes(contract_text: str, environment: str | None = None) -> list[str]:
    """The lane names of the universes that ``environment`` schedules, or, without one, that some environment schedules.

    Raises ``VerdictCheckFailure`` for a contract without universe records or a list in a shape this tool
    cannot read; the caller then fails, because a check it cannot expand is a check it would miss.
    """
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
    """``expectations`` without the per-universe checks of a universe that the environment does not schedule."""
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
    except Exception as exc:  # noqa: BLE001 - reported, never a traceback
        raise VerdictCheckFailure(f"{url} unreachable: {exc}") from exc
    if status != 200:
        raise VerdictCheckFailure(f"{url} answered HTTP {status}")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise VerdictCheckFailure(f"{url} did not answer JSON: {body[:80]!r}") from exc
    if not isinstance(payload, dict):
        raise VerdictCheckFailure(f"{url} did not answer a JSON object: {body[:80]!r}")
    # The data engine writes the verdicts, so its build decides what is expected; the
    # service's own release stands in when the engine's identity is unreadable.
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


#: The expectations file's text at a release ref; None when that release predates the file.
#: Raises `VerdictCheckFailure` when the ref is not a commit here.
ReleaseReader = Callable[[str], str | None]


def git_release_reader(
    repo: str = ".",
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    path: str = RELEASE_EXPECTATIONS_PATH,
) -> ReleaseReader:
    """Read the file ``path`` as the tree of a release ref holds it."""

    def read(ref: str) -> str | None:
        if ref.startswith("-"):
            raise VerdictCheckFailure(f"{ref!r} is not a release ref")
        resolved = run(
            ["git", "-C", repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        commit = resolved.stdout.strip()
        if resolved.returncode != 0 or not commit:
            raise VerdictCheckFailure(f"{ref!r} is not a commit in this checkout (fetch tags)")
        # "The release predates the file" passes the check, so it must be a positive answer
        # (the tree lists no such path), never whatever a failed `git show` happens to mean.
        listed = run(
            ["git", "-C", repo, "ls-tree", "--name-only", commit, "--", path],
            capture_output=True,
            text=True,
            check=False,
        )
        if listed.returncode != 0:
            raise VerdictCheckFailure(f"cannot list {ref}'s tree: {listed.stderr.strip()[:120]}")
        if not listed.stdout.strip():
            return None
        shown = run(
            ["git", "-C", repo, "show", f"{commit}:{path}"],
            capture_output=True,
            text=True,
            check=False,
        )
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
    """(what the deployed release records in ``environment``, where that answer came from).

    The per-universe checks of the release expand over the universes that its contract schedules in some
    environment. With an ``environment``, the checks of a universe that the contract does not schedule in
    that environment are dropped. A release whose contract holds no universe records declares its
    per-universe checks by name, and nothing is dropped.
    """
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


def _gh_api(path: str) -> str:
    result = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise VerdictCheckFailure(f"gh api {path} failed: {result.stderr.strip()[:120]}")
    return result.stdout


def _utc(value: object) -> datetime:
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def first_declared_at(
    check: str,
    *,
    release: str,
    deploy_type: str,
    repo: str = ".",
    run: GitRun = subprocess.run,
    gh_api: GhApi = _gh_api,
) -> datetime | None:
    """When this environment first served a release that declares `check`.

    The commit that first put the check's key into the expectations file (among the deployed
    release's ancestors), the release tags that contain it, and the earliest successful
    `Deploy <deploy_type> <tag>` among them. None — no grace — whenever any step cannot be
    shown, including when the listed runs may not reach back far enough: the answer is only
    trusted when the listing is complete or reaches past the introducing commit, since no
    deploy of a tag containing a commit can precede that commit.
    """
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
        if len(first) != 2:
            return None
        introduced, introduced_at = first[0], _utc(first[1])
        tags = run(
            ["git", "-C", repo, "tag", "--contains", introduced, "--list", "v*"],
            capture_output=True,
            text=True,
            check=False,
        )
        if tags.returncode != 0:
            return None
        titles = {f"Deploy {deploy_type} {tag}" for tag in tags.stdout.split()}
        runs = json.loads(gh_api(DEPLOY_RUNS_PATH)).get("workflow_runs", [])
        served = [
            _utc(entry["updated_at"])
            for entry in runs
            if entry.get("display_title") in titles and entry.get("conclusion") == "success"
        ]
        if not served:
            return None
        complete = len(runs) < 100 or min(_utc(entry["created_at"]) for entry in runs) <= introduced_at
        return min(served) if complete else None
    # OSError: a missing or unrunnable git/gh binary is "cannot be shown" too, never a crash.
    except (VerdictCheckFailure, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def judge(
    verdicts: Sequence[Verdict],
    expectations: dict[str, Expectation],
    now: datetime,
    *,
    first_declared: Callable[[str], datetime | None] | None = None,
    notes: list[str] | None = None,
) -> list[str]:
    """One sentence per expected check that is red, stale or missing.

    A missing check that `first_declared` shows became expected here less than its bound ago
    is pending, not missing: a sentence goes to `notes` instead.
    """
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
        # Our clock, not the service's: a service with a wrong clock reports a wrong age.
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
                failures.append(
                    f"{name}: newest verdict is from {verdict.ran_at.isoformat()} ({age:.1f} h ago, limit "
                    f"{expectation.max_age_hours:g} h) — job {expectation.job or '?'} stopped ticking or stopped "
                    f"reaching its verdict; check the Dagster daemon and the job's recent runs"
                )
                continue
            if notes is not None:
                notes.append(f"{name}: pending ({verdict.summary})")
            continue
        if not verdict.ok:
            failures.append(f"{name}: red since {verdict.ran_at.isoformat()} ({age:.1f} h ago) — {verdict.summary}")
            continue
        if age > expectation.max_age_hours:
            failures.append(
                f"{name}: newest verdict is from {verdict.ran_at.isoformat()} ({age:.1f} h ago, limit "
                f"{expectation.max_age_hours:g} h) — job {expectation.job or '?'} stopped ticking or stopped "
                f"reaching its verdict; check the Dagster daemon and the job's recent runs"
            )
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
        # A check the deployed release no longer declares (retired, or recorded by an older
        # build): its old rows stay in the table, and bounding them would be red forever.
        print(f"{name}: not bounded (not declared by the deployed release): {', '.join(unexpected)}")
    if failures:
        for failure in failures:
            print(f"nightly verdict check failed: {name} {failure}", file=sys.stderr)
        print(f"({len(failures)} of {len(expectations)} expected checks; expectations {source})", file=sys.stderr)
        return 1
    print(f"{name}: all {len(expectations)} nightly checks are green and on time ({source})")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url")
    parser.add_argument("--environment", default="")
    parser.add_argument("--repo", default=".", help="checkout holding the release tags")
    # deploy-release.yml's run-name word ("staging", "prod"); omitted, a missing check is never pending.
    parser.add_argument("--deploy-type", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    first_declared: FirstDeclared | None = None
    if arguments.deploy_type:
        deploys = functools.cache(_gh_api)

        def first_declared(check: str, release: str) -> datetime | None:
            return first_declared_at(
                check, release=release, deploy_type=arguments.deploy_type, repo=arguments.repo, gh_api=deploys
            )

    return check_nightly_verdicts(
        arguments.url,
        environment=arguments.environment,
        reader=git_release_reader(arguments.repo),
        contract_reader=git_release_reader(arguments.repo, path=RELEASE_CONTRACT_PATH),
        first_declared=first_declared,
    )


if __name__ == "__main__":
    raise SystemExit(main())
