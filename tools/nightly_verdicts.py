"""Did every nightly in-environment check run, and was it green? (#876 W1/W2)

The output invariants, the report surface proof, the confidence report, the daily head
reports and the model-provider key probe run inside each environment's own Dagster daemon.
Until #876 a red run was a row in `dagster.runs` and nothing else, and a daemon that stopped
ticking was indistinguishable from a quiet night — the model key sat revoked in production
until a human read the call ledger (#832). Each check now appends its verdict, green or red,
to `mart.nightly_verdicts`; `/api/health` reports the newest verdict per check
(`nightly_verdicts`), and this check — a step of the scheduled deploy-freshness workflow,
which escalates a red run to an issue — fails when an expected check's newest verdict is:

  * red (`ok` false): the check ran and failed;
  * stale (older than twice the check's cadence): the schedule stopped ticking, or the job
    stopped reaching its verdict;
  * missing: the environment has never recorded it.

Which checks are expected is declared once, in `tools/nightly_verdicts.json`, and
`libs/runtime/tests/test_nightly_verdicts.py` holds that set equal to what the lanes record.
The set is read AS THE DEPLOYED RELEASE DECLARED IT (`git show <release>:tools/...`): a check
added on main is not demanded of a production release that lags by design and does not run
it yet, and a release that predates the file bounds nothing. When the release cannot be
resolved here, main's set is used — a false red beats a silent miss.

Exit codes:
  0 - every expected check's newest verdict is green and within twice its cadence, or the
      environment serves a release that predates the report
  1 - a verdict is red, stale or missing, the report is unreadable, or the endpoint is
      unreachable
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from infra2_sdk.deploy_health import HttpGet, default_http_get

EXPECTATIONS_PATH = Path(__file__).with_name("nightly_verdicts.json")
#: The same file as a release's tree names it.
RELEASE_EXPECTATIONS_PATH = "tools/nightly_verdicts.json"
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
    ok: bool
    summary: str


@dataclass(frozen=True)
class HealthReport:
    #: None when the release predates the report (no key at all — an older build).
    verdicts: list[Verdict] | None
    #: The release whose data engine writes the verdicts, when the endpoint names one.
    release: str | None


def parse_expectations(text: str) -> dict[str, Expectation]:
    raw = json.loads(text)
    return {
        name: Expectation(name, float(entry["cadence_hours"]), str(entry.get("job", "")))
        for name, entry in raw["checks"].items()
    }


def load_expectations(path: Path = EXPECTATIONS_PATH) -> dict[str, Expectation]:
    return parse_expectations(path.read_text(encoding="utf-8"))


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
    if "nightly_verdicts" not in payload:
        return HealthReport(None, release)
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
            if not isinstance(ok, bool):
                raise TypeError(f"ok is {ok!r}, not a boolean")
            verdicts.append(Verdict(str(entry["check"]), ran_at, ok, str(entry.get("summary", ""))))
        except (KeyError, TypeError, ValueError) as exc:
            raise VerdictCheckFailure(f"{url} reports a malformed verdict entry: {entry!r}") from exc
    return HealthReport(verdicts, release)


#: The expectations file's text at a release ref; None when that release predates the file.
#: Raises `VerdictCheckFailure` when the ref is not a commit here.
ReleaseReader = Callable[[str], str | None]


def git_release_reader(
    repo: str = ".", run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
) -> ReleaseReader:
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
            ["git", "-C", repo, "ls-tree", "--name-only", commit, "--", RELEASE_EXPECTATIONS_PATH],
            capture_output=True,
            text=True,
            check=False,
        )
        if listed.returncode != 0:
            raise VerdictCheckFailure(f"cannot list {ref}'s tree: {listed.stderr.strip()[:120]}")
        if not listed.stdout.strip():
            return None
        shown = run(
            ["git", "-C", repo, "show", f"{commit}:{RELEASE_EXPECTATIONS_PATH}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if shown.returncode != 0:
            raise VerdictCheckFailure(f"cannot read {RELEASE_EXPECTATIONS_PATH} at {ref}: {shown.stderr.strip()[:120]}")
        return shown.stdout

    return read


def expectations_for(release: str | None, reader: ReleaseReader) -> tuple[dict[str, Expectation] | None, str]:
    """(what the deployed release records, where that answer came from)."""
    if release is None:
        return load_expectations(), "the release identity is unreadable, so main's set is bounded"
    try:
        text = reader(release)
    except VerdictCheckFailure as exc:
        return load_expectations(), f"{exc}; main's set is bounded"
    if text is None:
        return None, f"{release} predates {RELEASE_EXPECTATIONS_PATH}"
    return parse_expectations(text), f"as {release} declares them"


def judge(verdicts: Sequence[Verdict], expectations: dict[str, Expectation], now: datetime) -> list[str]:
    """One sentence per expected check that is red, stale or missing."""
    newest: dict[str, Verdict] = {}
    for entry in verdicts:
        if entry.check not in newest or entry.ran_at > newest[entry.check].ran_at:
            newest[entry.check] = entry
    failures: list[str] = []
    for name, expectation in sorted(expectations.items()):
        verdict = newest.get(name)
        if verdict is None:
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
    now: datetime | None = None,
) -> int:
    http_get = http_get or default_http_get()
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
    expectations, source = expectations_for(report.release, reader)
    if expectations is None:
        print(f"{name}: the data engine release {source}; nothing to bound yet")
        return 0
    reference = now or datetime.now(UTC)
    failures = judge(report.verdicts, expectations, reference)
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    return check_nightly_verdicts(
        arguments.url, environment=arguments.environment, reader=git_release_reader(arguments.repo)
    )


if __name__ == "__main__":
    raise SystemExit(main())
