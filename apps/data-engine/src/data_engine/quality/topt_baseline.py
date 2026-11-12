"""Does TOPT meet the accepted baseline in this environment? The nightly verdict `topt_baseline` (#1664).

The owner decided on 2026-11-11: TOPT is below the baseline, so no other universe goes forward until it
meets the baseline. #1664 lists the checks. This module runs each check that one environment can run on
its own data, from the primary tables. It never reads a number from a report that a dashboard also shows,
except to compare the two.

What each row reads, and what makes it red:

* Q1 (GPPE availability). The served TOPT head of `mart.served_head`, the head that the dashboard shows. A head
  that is withheld or not fresh is red. For a fresh head: the issuers of `mart.topt_gppe_results` that the coverage
  report would count as answered (`coalesce(availability_status, availability) = 'available'` and a GPPE value)
  against all issuers of the run. Red below `minimum_availability` of `ACCEPTED_SERVICE_OBJECTIVES` (95%). The
  stored coverage report of the same run must show the same two numbers. When it shows others, or no report
  exists, the row is red: a dashboard that disagrees with its own tables is a defect.
* R1 (quota). The calls of the Twelve Data seat in `staging.api_call_ledger` on each of the last 7 UTC days
  against the share of this environment (`account_share`, the window of one day). Red above
  `UTILIZATION_CEILING` (70%) on any day. A ledger row counts, failed or not: the vendor counts it. A share of 0
  is inside the share when the seat made no call, and red when it made one.
* C1 (server memory). `VmHWM` of the first process of the code server container against `server_mib` of the
  capacity contract. A server below the budget that started less than `MATURE_SERVER_HOURS` ago is not
  measured: the high-water mark only grows, and the contract states its reading at 13 hours after the start.

* S (S1 to S3, schema). The live catalog of this environment's database against a reference database that ran the
  chain (`truealpha_tools.schema_drift`). The reference sits on the same server, is built by the one applier
  `db/apply_migrations.sh` from `/app/db`, and is dropped at the end. The number of differences is the row. Green
  only at 0. The comparison covers relations, columns, constraints, indexes, triggers, functions, views and
  sequences. It does not cover row-level security policies, grants, function settings, index validity, trigger
  state, enum types or comments: a green S does not prove those. A reference that cannot be built or compared is
  red, never green. A scratch database that a killed process left behind is dropped by the next run (older than
  1 h). Each environment runs its own check, so the row is S1 in production and S2 in staging, and the scheduled
  verdict is S3.

Q3 of #1664 cannot run inside one environment today. It compares two environments, and no database sees the other.
It is listed as not measured. A verdict with a row that is not measured is never green: unknown is not OK.

A shown percentage or size is rounded in the direction that keeps the text true: up for a red row, down for a
row that passes. A text never says "70.0% against 70.0%" for a row that failed.

A summary starts with the texts of the rows that keep the verdict red: the failed rows or, when no row failed,
the rows that are not measured. Then it lists the rows that passed (their names), and, after a failed row, the rows
that are not measured (their names). A green row is visible and differs from a row that did not run.

A summary is public (`/api/health`). It holds row names, counts and percentages. Never a research value.
`verdict()` puts "failed: " before a red summary, so the first text of a summary says why the verdict is red.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, Literal

from truealpha_contracts.environment_contract import DAY_SECONDS, UTILIZATION_CEILING, EnvironmentContract
from truealpha_contracts.universes import record

from data_engine.datahub.a1_evidence import ACCEPTED_SERVICE_OBJECTIVES
from data_engine.datahub.market_prices import TWELVE_DATA_SEAT

log = logging.getLogger(__name__)

MIB = 1024 * 1024
PROC_ROOT = Path("/proc")

#: The seat of Twelve Data in the call ledger (the constant of the writer), and the days that R1 reads.
QUOTA_SOURCE = TWELVE_DATA_SEAT
QUOTA_DAYS = 7

#: The code server holds its reading of `grpc_server_hwm_mib` in the contract 13 hours after its start
#: (`code_server_capacity.measured.method`). A younger server has not shown its size yet.
MATURE_SERVER_HOURS = 13

#: Rows of #1664 that no environment can run alone, with the reason. The summary names them.
NOT_MEASURED: Mapping[str, str] = {
    "Q3": "compares two environments; no database sees the other",
}

#: Where the data-engine image holds the chain for the reference database (`apps/data-engine/Dockerfile`).
IMAGE_DB_DIR = Path("/app/db")
#: `db/roles.sql` sets the password of the app login when this variable is set. The reference build must never
#: carry it: the check reads the schema and changes no credential.
MASKED_ENVIRONMENT = ("APP_SERVICE_DB_PASSWORD",)

State = Literal["pass", "fail", "unmeasured"]


@dataclass(frozen=True)
class Row:
    """One check of #1664 and what it found. `text` is public: names and counts only."""

    key: str
    state: State
    text: str


@dataclass(frozen=True)
class Judgement:
    rows: tuple[Row, ...]

    @property
    def failed(self) -> tuple[Row, ...]:
        return tuple(row for row in self.rows if row.state == "fail")

    @property
    def unmeasured(self) -> tuple[Row, ...]:
        return tuple(row for row in self.rows if row.state == "unmeasured")

    @property
    def ok(self) -> bool:
        """Green only when every row passed. A row that is not measured is never a pass."""
        return all(row.state == "pass" for row in self.rows)

    @property
    def summary(self) -> str:
        """The rows that keep the verdict red come first: the failed rows, or, when none failed, the rows that are
        not measured. `verdict()` puts "failed: " before a red summary, so that first part must say why."""
        lead = self.failed or self.unmeasured
        if not lead:
            return "ok: " + "; ".join(row.text for row in self.rows)
        parts = ["; ".join(row.text for row in lead)]
        passed = [row.key for row in self.rows if row.state == "pass"]
        if passed:
            parts.append("passed: " + ", ".join(passed))
        if self.failed and self.unmeasured:
            parts.append("not measured: " + ", ".join(row.key for row in self.unmeasured))
        return ". ".join(parts)


def _one_decimal(value: Decimal, *, up: bool) -> Decimal:
    return value.quantize(Decimal("0.1"), rounding=ROUND_CEILING if up else ROUND_FLOOR)


def _percent(ratio: Decimal, *, up: bool) -> str:
    return f"{_one_decimal(ratio * 100, up=up)}%"


def judge_head(*, run_id: str | None, freshness: str, availability: str) -> Row | None:
    """Q1, first step: a head that is withheld or not fresh is red before any count. None when it is served."""
    if run_id is None or availability != "available":
        return Row("Q1", "fail", f"Q1 the served TOPT head is withheld ({availability})")
    if freshness != "fresh":
        return Row("Q1", "fail", f"Q1 the served TOPT head is {freshness}")
    return None


def judge_gppe(*, available: int, total: int, reported: tuple[int, int] | None) -> Row:
    """Q1: the share of issuers of the served TOPT head with a GPPE, and the stored report's own numbers."""
    if total <= 0:
        return Row("Q1", "fail", "Q1 the served TOPT head holds no GPPE row")
    if reported is None:
        return Row("Q1", "fail", f"Q1 GPPE {available}/{total}: no coverage report names this head")
    if reported != (available, total):
        return Row(
            "Q1", "fail", f"Q1 the report says {reported[0]}/{reported[1]}, the GPPE rows say {available}/{total}"
        )
    ratio = Decimal(available) / Decimal(total)
    minimum = ACCEPTED_SERVICE_OBJECTIVES.minimum_availability
    passed = ratio >= minimum
    text = f"Q1 GPPE {available}/{total} ({_percent(ratio, up=not passed)}) against {_percent(minimum, up=False)}"
    return Row("Q1", "pass" if passed else "fail", text)


def judge_quota(*, source: str, daily_calls: Mapping[date, int], share: int | None) -> Row:
    """R1: the busiest of the last days against the share of one day, at `UTILIZATION_CEILING`."""
    if share is None:
        return Row("R1", "fail", f"R1 {source} has no daily limit to judge against")
    total = sum(daily_calls.values())
    if share < 1:
        text = f"R1 {source} {total} calls in {QUOTA_DAYS} days against a share of 0"
        return Row("R1", "pass" if total == 0 else "fail", text)
    ceiling = Decimal(str(UTILIZATION_CEILING))
    if not daily_calls:
        return Row("R1", "pass", f"R1 {source} 0/{share} in {QUOTA_DAYS} days")
    day, peak = max(daily_calls.items(), key=lambda item: (item[1], item[0]))
    ratio = Decimal(peak) / Decimal(share)
    passed = ratio <= ceiling
    text = f"R1 {source} {peak}/{share} on {day:%m-%d} ({_percent(ratio, up=not passed)}) against {_percent(ceiling, up=False)}"
    return Row("R1", "pass" if passed else "fail", text)


def judge_server(*, hwm_bytes: int | None, age_seconds: float | None, budget_mib: int) -> Row:
    """C1: the code server's high-water mark against its budget. A young server is not measured yet."""
    if hwm_bytes is None:
        return Row("C1", "fail", "C1 the size of the code server cannot be read")
    size = Decimal(hwm_bytes) / MIB
    over = hwm_bytes > budget_mib * MIB
    age = "age unknown" if age_seconds is None else f"age {_one_decimal(Decimal(str(age_seconds)) / 3600, up=False)} h"
    text = f"C1 code server {_one_decimal(size, up=over)}/{budget_mib} MiB, {age}"
    if over:
        return Row("C1", "fail", text)
    if age_seconds is None or age_seconds < MATURE_SERVER_HOURS * 3600:
        return Row("C1", "unmeasured", f"{text}, measured at {MATURE_SERVER_HOURS} h")
    return Row("C1", "pass", text)


def judge_schema(*, differences: int | None, failure: str = "") -> Row:
    """S: the places where the live catalog differs from the chain. None means that the comparison did not run."""
    if differences is None:
        return Row("S", "fail", f"S the schema could not be compared ({failure})")
    if differences == 0:
        return Row("S", "pass", "S catalog matches the chain")
    return Row("S", "fail", f"S schema differs from the chain in {differences} places")


def judge(*, gppe: Row, quota: Row, server: Row, schema: Row) -> Judgement:
    """The rows in the order of #1664, then the rows that no environment can run alone."""
    waiting = tuple(Row(key, "unmeasured", f"{key} not measured: {reason}") for key, reason in NOT_MEASURED.items())
    return Judgement((gppe, quota, server, schema, *waiting))


# --- readers ------------------------------------------------------------------------------------------------

#: The head that the dashboard shows: the newest TOPT pointer of `mart.served_head`. `run_id` is null while the
#: head is withheld.
HEAD_SQL = """
select run_id, freshness, availability
from mart.served_head
where universe_id like %s
order by advanced_at desc
limit 1
"""

#: The issuers of the run, and those that the coverage report counts as answered
#: (`question_coverage._classify_cell`: an available status with a value).
GPPE_SQL = """
select count(distinct issuer_id)::int,
       (count(distinct issuer_id)
          filter (where coalesce(availability_status, availability) = 'available' and gppe is not null))::int
from mart.topt_gppe_results
where run_id = %s
"""

#: The numbers of q1 in the newest stored coverage report of the run. The dashboard shows the same report.
REPORTED_Q1_SQL = """
select (payload -> 'questions' -> 'q1' ->> 'answered')::int,
       (payload -> 'questions' -> 'q1' ->> 'denominator')::int
from mart.question_coverage_report
where run_id = %s
order by created_at desc
limit 1
"""

DAILY_CALLS_SQL = """
select (called_at at time zone 'utc')::date, count(*)::int
from staging.api_call_ledger
where source = %s and called_at >= %s and called_at <= %s
group by 1
"""


def topt_universe_like() -> str:
    return f"{record('topt').id_prefix}%"


def read_gppe(connection: Any, run_id: str) -> Row:
    counts = connection.execute(GPPE_SQL, (run_id,)).fetchone()
    total, available = (0, 0) if counts is None else (int(counts[0]), int(counts[1]))
    reported_row = connection.execute(REPORTED_Q1_SQL, (run_id,)).fetchone()
    reported = None
    if reported_row is not None and reported_row[0] is not None and reported_row[1] is not None:
        reported = (int(reported_row[0]), int(reported_row[1]))
    return judge_gppe(available=available, total=total, reported=reported)


def read_q1(connection: Any) -> Row:
    """Q1 for the head that is served now."""
    head = connection.execute(HEAD_SQL, (topt_universe_like(),)).fetchone()
    if head is None:
        return Row("Q1", "fail", "Q1 no served TOPT head")
    run_id, freshness, availability = head
    refused = judge_head(run_id=run_id, freshness=str(freshness), availability=str(availability))
    return refused if refused is not None else read_gppe(connection, str(run_id))


def window_start(now: datetime) -> datetime:
    """Midnight UTC of the first of the last `QUOTA_DAYS` days, today included."""
    today = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=QUOTA_DAYS - 1)


def read_quota(connection: Any, *, now: datetime, environment: str, contract: EnvironmentContract) -> Row:
    account = contract.account_of_seat(QUOTA_SOURCE)
    share = contract.account_share(account, environment).share_in_window(DAY_SECONDS)
    rows = connection.execute(DAILY_CALLS_SQL, (QUOTA_SOURCE, window_start(now), now)).fetchall()
    return judge_quota(source=QUOTA_SOURCE, daily_calls={day: int(count) for day, count in rows}, share=share)


def server_age_seconds(proc_root: Path = PROC_ROOT, ticks_per_second: int | None = None) -> float | None:
    """Seconds since the first process started: the uptime of the host minus the start of the process (ticks)."""
    try:
        stat = (proc_root / "1" / "stat").read_text()
        started_ticks = int(stat[stat.rindex(")") + 2 :].split()[19])
        uptime = float((proc_root / "uptime").read_text().split()[0])
        hertz = ticks_per_second or os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None
    return uptime - started_ticks / hertz


def read_server(
    *,
    budget_mib: int,
    proc_root: Path = PROC_ROOT,
    reader: Callable[[Path], int] | None = None,
) -> Row:
    """The code server of this container. `reader` is the `VmHWM` reader of `runtime_shape` unless a test gives one."""
    from data_engine.quality import runtime_shape

    read_hwm = reader or runtime_shape._server_process
    try:
        hwm: int | None = read_hwm(proc_root)
    except (OSError, ValueError):
        hwm = None
    return judge_server(hwm_bytes=hwm, age_seconds=server_age_seconds(proc_root), budget_mib=budget_mib)


@contextmanager
def _without_environment(names: tuple[str, ...]) -> Iterator[None]:
    """Mask variables of this process for the block, then put them back."""
    held = {name: os.environ.pop(name) for name in names if name in os.environ}
    try:
        yield
    finally:
        os.environ.update(held)


def read_schema(database_url: str, *, db_dir: Path | None = None) -> Row:
    """S: build the reference on the server of `database_url`, compare, drop it.

    Any failure is a red row. The row names the exception type and nothing else, because a connection error names its
    host and the row is public. The log of the run keeps the full error."""
    chain_dir = IMAGE_DB_DIR if db_dir is None else db_dir
    try:
        from truealpha_tools import schema_drift

        try:
            schema_drift.drop_stale_references(database_url)  # logs each database that it drops
        except Exception:  # noqa: BLE001 - a failed sweep never hides the measurement
            log.warning("could not sweep stale scratch reference databases", exc_info=True)
        with _without_environment(MASKED_ENVIRONMENT):
            with schema_drift.reference_database(database_url, db_dir=chain_dir, allow_remote=True) as reference_url:
                report, _counts = schema_drift.compare(database_url, reference_url)
    except Exception as failure:  # noqa: BLE001 - every failure is a red row, never a crash of the verdict
        log.warning("the schema comparison failed", exc_info=True)
        return judge_schema(differences=None, failure=type(failure).__name__)
    return judge_schema(differences=len(report))
