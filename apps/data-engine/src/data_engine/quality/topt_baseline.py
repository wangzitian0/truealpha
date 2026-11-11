"""Does TOPT meet the accepted baseline in this environment? The nightly verdict `topt_baseline` (#1664).

The owner decided on 2026-11-11: TOPT is below the baseline, so no other universe goes forward until it
meets the baseline. #1664 lists the checks. This module runs each check that one environment can run on
its own data, from the primary tables. It never reads a number from a report that a dashboard also shows,
except to compare the two.

What each row reads, and what makes it red:

* Q1 (GPPE availability). The served TOPT head: the issuers of `mart.topt_gppe_results` with
  `availability = 'available'` against all issuers of the run. Red below `minimum_availability` of
  `ACCEPTED_SERVICE_OBJECTIVES` (95%). The stored coverage report of the same run must show the same two
  numbers. When it shows others, or no report exists, the row is red: a dashboard that disagrees with its own
  tables is a defect.
* R1 (quota). The calls of the Twelve Data seat in `staging.api_call_ledger` on each of the last 7 UTC days
  against the share of this environment (`account_share`, the window of one day). Red above
  `UTILIZATION_CEILING` (70%) on any day. A ledger row counts, failed or not: the vendor counts it.
* C1 (server memory). `VmHWM` of the first process of the code server container against `server_mib` of the
  capacity contract. A server below the budget that started less than `MATURE_SERVER_HOURS` ago is not
  measured: the high-water mark only grows, and the contract states its reading at 13 hours after the start.

Q3, S1, S2 and S3 of #1664 cannot run inside one environment today. Q3 compares two environments, and no
database sees the other. S1 to S3 compare a catalog with the migration chain; the reference needs a second
database next to the target, and `tools/schema_drift.py` refuses to build it on a remote server. Each is
listed as not measured. A verdict with a row that is not measured is never green: unknown is not OK.

A summary is public (`/api/health`). It holds row names, counts and percentages. Never a research value.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from truealpha_contracts.environment_contract import UTILIZATION_CEILING, EnvironmentContract

from data_engine.datahub.a1_evidence import ACCEPTED_SERVICE_OBJECTIVES

MIB = 1024 * 1024
PROC_ROOT = Path("/proc")

#: The seat of Twelve Data in the call ledger, and the days that R1 reads (today included).
QUOTA_SOURCE = "twelvedata"
QUOTA_DAYS = 7
DAY_SECONDS = 86_400

#: The code server holds its reading of `grpc_server_hwm_mib` in the contract 13 hours after its start
#: (`code_server_capacity.measured.method`). A younger server has not shown its size yet.
MATURE_SERVER_HOURS = 13

#: Rows of #1664 that no environment can run alone, with the reason. The summary names them.
NOT_MEASURED: Mapping[str, str] = {
    "Q3": "compares two environments; no database sees the other",
    "S1": "schema_drift needs a second database next to the target",
    "S2": "schema_drift needs a second database next to the target",
    "S3": "schema_drift needs a second database next to the target",
}

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
        parts: list[str] = []
        if self.failed:
            parts.append("failed: " + "; ".join(row.text for row in self.failed))
        if self.unmeasured:
            parts.append("not measured: " + ", ".join(row.key for row in self.unmeasured))
        if not parts:
            return "ok: " + "; ".join(row.text for row in self.rows)
        return ". ".join(parts)


def _percent(ratio: Decimal) -> str:
    return f"{ratio * 100:.0f}%"


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
    text = f"Q1 GPPE {available}/{total} ({_percent(ratio)}) against {_percent(minimum)}"
    return Row("Q1", "pass" if ratio >= minimum else "fail", text)


def judge_quota(*, source: str, daily_calls: Mapping[date, int], share: int | None) -> Row:
    """R1: the busiest of the last days against the share of one day, at `UTILIZATION_CEILING`."""
    if share is None or share < 1:
        return Row("R1", "fail", f"R1 {source} has no daily share to judge against")
    ceiling = Decimal(str(UTILIZATION_CEILING))
    if not daily_calls:
        return Row("R1", "pass", f"R1 {source} 0/{share} in {QUOTA_DAYS} days")
    day, peak = max(daily_calls.items(), key=lambda item: (item[1], item[0]))
    ratio = Decimal(peak) / Decimal(share)
    text = f"R1 {source} {peak}/{share} on {day:%m-%d} ({_percent(ratio)}) against {_percent(ceiling)}"
    return Row("R1", "pass" if ratio <= ceiling else "fail", text)


def judge_server(*, hwm_bytes: int | None, age_seconds: float | None, budget_mib: int) -> Row:
    """C1: the code server's high-water mark against its budget. A young server is not measured yet."""
    if hwm_bytes is None:
        return Row("C1", "fail", "C1 the size of the code server cannot be read")
    size = hwm_bytes / MIB
    age = "age unknown" if age_seconds is None else f"age {age_seconds / 3600:.0f} h"
    if hwm_bytes > budget_mib * MIB:
        return Row("C1", "fail", f"C1 code server {size:.1f}/{budget_mib} MiB, {age}")
    if age_seconds is None or age_seconds < MATURE_SERVER_HOURS * 3600:
        text = f"C1 code server {size:.1f}/{budget_mib} MiB, {age}, measured at {MATURE_SERVER_HOURS} h"
        return Row("C1", "unmeasured", text)
    return Row("C1", "pass", f"C1 code server {size:.1f}/{budget_mib} MiB, {age}")


def judge(*, gppe: Row, quota: Row, server: Row) -> Judgement:
    """The rows in the order of #1664, then the rows that no environment can run alone."""
    waiting = tuple(Row(key, "unmeasured", f"{key} {reason}") for key, reason in NOT_MEASURED.items())
    return Judgement((gppe, quota, server, *waiting))


# --- readers ------------------------------------------------------------------------------------------------

GPPE_SQL = """
select count(distinct issuer_id)::int,
       (count(distinct issuer_id) filter (where availability = 'available'))::int
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
where source = %s and called_at >= %s
group by 1
"""


def read_gppe(connection: Any, run_id: str) -> Row:
    available_total = connection.execute(GPPE_SQL, (run_id,)).fetchone()
    total, available = (0, 0) if available_total is None else (int(available_total[0]), int(available_total[1]))
    reported_row = connection.execute(REPORTED_Q1_SQL, (run_id,)).fetchone()
    reported = None
    if reported_row is not None and reported_row[0] is not None and reported_row[1] is not None:
        reported = (int(reported_row[0]), int(reported_row[1]))
    return judge_gppe(available=available, total=total, reported=reported)


def window_start(now: datetime) -> datetime:
    """Midnight UTC of the first of the last `QUOTA_DAYS` days, today included."""
    today = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=QUOTA_DAYS - 1)


def read_quota(connection: Any, *, now: datetime, environment: str, contract: EnvironmentContract) -> Row:
    account = contract.account_of_seat(QUOTA_SOURCE)
    share = contract.account_share(account, environment).share_in_window(DAY_SECONDS)
    rows = connection.execute(DAILY_CALLS_SQL, (QUOTA_SOURCE, window_start(now))).fetchall()
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
