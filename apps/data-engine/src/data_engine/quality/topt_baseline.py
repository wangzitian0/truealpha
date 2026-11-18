"""Nightly TOPT baseline verdict check (#1664)."""

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

from factors.panel import GPPE_BANKING_TCE_SPEC, GPPE_V0_SPEC
from truealpha_contracts.environment_contract import (
    DAY_SECONDS,
    UTILIZATION_CEILING,
    EnvironmentContract,
)
from truealpha_contracts.universes import record

from data_engine.datahub.a1_evidence import ACCEPTED_SERVICE_OBJECTIVES
from data_engine.datahub.market_prices import TWELVE_DATA_SEAT

log = logging.getLogger(__name__)

MIB = 1024 * 1024
PROC_ROOT = Path("/proc")
QUOTA_SOURCE = TWELVE_DATA_SEAT
QUOTA_DAYS = 7
MATURE_SERVER_HOURS = 13

NOT_MEASURED: Mapping[str, str] = {"Q3": "compares two environments; no database sees the other"}
IMAGE_DB_DIR = Path("/app/db")
MASKED_ENVIRONMENT = ("APP_SERVICE_DB_PASSWORD",)

State = Literal["pass", "fail", "unmeasured"]


@dataclass(frozen=True)
class Row:
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
        return all(row.state == "pass" for row in self.rows)

    @property
    def summary(self) -> str:
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


def _round(value: Decimal, place: str, *, up: bool) -> Decimal:
    return value.quantize(Decimal(place), rounding=ROUND_CEILING if up else ROUND_FLOOR)


def _percent(ratio: Decimal, *, up: bool) -> str:
    return f"{_round(ratio * 100, '0.1', up=up)}%"


def judge_head(*, run_id: str | None, freshness: str, availability: str) -> Row | None:
    if run_id is None or availability != "available":
        return Row("Q1", "fail", f"Q1 the served TOPT head is withheld ({availability})")
    if freshness != "fresh":
        return Row("Q1", "fail", f"Q1 the served TOPT head is {freshness}")
    return None


def judge_gppe(*, available: int, total: int, reported: tuple[int, int] | None) -> Row:
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
    if hwm_bytes is None:
        return Row("C1", "fail", "C1 the size of the code server cannot be read")
    size = Decimal(hwm_bytes) / MIB
    over = hwm_bytes > budget_mib * MIB
    age = "age unknown" if age_seconds is None else f"age {_round(Decimal(str(age_seconds)) / 3600, '0.1', up=False)} h"
    text = f"C1 code server {_round(size, '1', up=over)}/{budget_mib} MiB, {age}"
    if over:
        return Row("C1", "fail", text)
    if age_seconds is None or age_seconds < MATURE_SERVER_HOURS * 3600:
        return Row("C1", "unmeasured", f"{text}, measured at {MATURE_SERVER_HOURS} h")
    return Row("C1", "pass", text)


def judge_schema(*, differences: int | None, failure: str = "") -> Row:
    if differences is None:
        return Row("S", "fail", f"S the schema could not be compared ({failure})")
    if differences == 0:
        return Row("S", "pass", "S catalog matches the chain")
    places = "place" if differences == 1 else "places"
    return Row("S", "fail", f"S schema differs from the chain in {differences} {places}")


def judge(*, gppe: Row, quota: Row, server: Row, schema: Row) -> Judgement:
    waiting = tuple(Row(key, "unmeasured", f"{key} not measured: {reason}") for key, reason in NOT_MEASURED.items())
    return Judgement((gppe, quota, server, schema, *waiting))


HEAD_SQL = "select run_id, freshness, availability from mart.served_head where universe_id like %s order by advanced_at desc limit 1"
GPPE_SQL = (
    "with per_issuer as ("
    "select distinct on (mnv.issuer_id) mnv.issuer_id, mnv.availability_status, mnv.value "
    "from mart.metric_node_values mnv "
    "left join mart.capture_issuer_lineage cil using (run_id, issuer_id) "
    "where mnv.run_id = %(run_id)s "
    "and ((cil.operating_branch = 'financial' and mnv.node_key = %(bank_key)s) "
    "or mnv.node_key = %(base_key)s) "
    "order by mnv.issuer_id, (mnv.node_key = %(bank_key)s) desc"
    ") select (count(*) filter (where availability_status <> 'not_applicable'))::int, "
    "(count(*) filter (where availability_status = 'available' and value is not null))::int "
    "from per_issuer"
)
REPORTED_Q1_SQL = (
    "select (payload -> 'questions' -> 'q1' ->> 'answered')::int, (payload -> 'questions' -> 'q1' ->> 'denominator')::int "
    "from mart.question_coverage_report where run_id = %s order by created_at desc limit 1"
)
DAILY_CALLS_SQL = (
    "select (called_at at time zone 'utc')::date, count(*)::int "
    "from staging.api_call_ledger where source = %s and called_at >= %s and called_at <= %s group by 1"
)


def topt_universe_like() -> str:
    return f"{record('topt').id_prefix}%"


def read_gppe(connection: Any, run_id: str) -> Row:
    counts = connection.execute(
        GPPE_SQL,
        {"run_id": run_id, "bank_key": GPPE_BANKING_TCE_SPEC.root, "base_key": GPPE_V0_SPEC.root},
    ).fetchone()
    total, available = (0, 0) if counts is None else (int(counts[0]), int(counts[1]))
    reported_row = connection.execute(REPORTED_Q1_SQL, (run_id,)).fetchone()
    reported = (
        (int(reported_row[0]), int(reported_row[1]))
        if reported_row is not None and reported_row[0] is not None and reported_row[1] is not None
        else None
    )
    return judge_gppe(available=available, total=total, reported=reported)


def read_q1(connection: Any) -> Row:
    head = connection.execute(HEAD_SQL, (topt_universe_like(),)).fetchone()
    if head is None:
        return Row("Q1", "fail", "Q1 no served TOPT head")
    run_id, freshness, availability = head
    refused = judge_head(run_id=run_id, freshness=str(freshness), availability=str(availability))
    return refused if refused is not None else read_gppe(connection, str(run_id))


def window_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=QUOTA_DAYS - 1)


def read_quota(connection: Any, *, now: datetime, environment: str, contract: EnvironmentContract) -> Row:
    account = contract.account_of_seat(QUOTA_SOURCE)
    share = contract.account_share(account, environment).share_in_window(DAY_SECONDS)
    rows = connection.execute(DAILY_CALLS_SQL, (QUOTA_SOURCE, window_start(now), now)).fetchall()
    return judge_quota(source=QUOTA_SOURCE, daily_calls={day: int(count) for day, count in rows}, share=share)


def server_age_seconds(proc_root: Path = PROC_ROOT, ticks_per_second: int | None = None) -> float | None:
    try:
        stat = (proc_root / "1" / "stat").read_text()
        started_ticks = int(stat[stat.rindex(")") + 2 :].split()[19])
        uptime = float((proc_root / "uptime").read_text().split()[0])
        hertz = ticks_per_second or os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None
    return uptime - started_ticks / hertz


def _hwm_bytes(status: str) -> int | None:
    for line in status.splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1]) * 1024
    return None


def _server_process(proc_root: Path) -> int:
    if b"dagster" not in (proc_root / "1" / "cmdline").read_bytes():
        raise ValueError("the first process is not the code server")
    size = _hwm_bytes((proc_root / "1" / "status").read_bytes().decode("utf-8", errors="replace"))
    if size is None:
        raise ValueError("the first process has no VmHWM")
    return size


def read_server(*, budget_mib: int, proc_root: Path = PROC_ROOT, reader: Callable[[Path], int] | None = None) -> Row:
    try:
        hwm: int | None = (reader or _server_process)(proc_root)
    except (OSError, ValueError):
        hwm = None
    return judge_server(hwm_bytes=hwm, age_seconds=server_age_seconds(proc_root), budget_mib=budget_mib)


@contextmanager
def _without_environment(names: tuple[str, ...]) -> Iterator[None]:
    held = {name: os.environ.pop(name) for name in names if name in os.environ}
    try:
        yield
    finally:
        os.environ.update(held)


def read_schema(database_url: str, *, db_dir: Path | None = None) -> Row:
    chain_dir = IMAGE_DB_DIR if db_dir is None else db_dir
    try:
        import truealpha_tools.schema_drift as schema_drift

        try:
            schema_drift.drop_stale_references(database_url)
        except Exception:  # noqa: BLE001
            log.warning("could not sweep stale scratch reference databases", exc_info=True)
        with _without_environment(MASKED_ENVIRONMENT):
            with schema_drift.reference_database(
                database_url, db_dir=chain_dir, allow_remote=True, skip_roles=True
            ) as reference_url:
                report, _counts = schema_drift.compare(database_url, reference_url)
    except Exception as failure:  # noqa: BLE001 - report error class only in public row
        log.warning("the schema comparison failed", exc_info=True)
        return judge_schema(differences=None, failure=type(failure).__name__)
    return judge_schema(differences=len(report))
