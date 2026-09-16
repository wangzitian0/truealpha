"""Every nightly in-environment check leaves a verdict row, green or red (#876 W1).

The quality and standards lanes run inside the environment's own Dagster daemon, and until
this module a red run was a row in `dagster.runs` that nobody was paged for; a daemon that
stopped ticking was indistinguishable from a quiet night. Each check now appends one row to
`mart.nightly_verdicts` per run, the service's `/health` reports the newest row per check,
and `tools/nightly_verdicts.py` (deploy-freshness, daily) goes red on a verdict that is red,
stale, or missing.

`verdict()` wraps a check's body. On a normal exit it records `ok = true` with the summary
the body set; on ANY exception — the check's own `dg.Failure` or a crash — it records
`ok = false` and re-raises the original, so the Dagster run stays red exactly as before.

What a verdict is named: the check, plus the universe for per-universe checks
(`theme_purity@topt`). A lane declares every name it can record in its module-level
`NIGHTLY_VERDICTS`; `data_engine.lanes.nightly_verdict_names()` is their union, and
`libs/runtime/tests/test_nightly_verdicts.py` holds that union equal to the set the tool
watches (`tools/nightly_verdicts.json`), so a check cannot be recorded and go unwatched. A
run whose name the lane does not declare — a manual run over a universe no schedule ticks —
is not a nightly check and records nothing.

What a summary may say: it is published on the public health endpoint. Counts, check and
surface names, the universe — never a research value, a credential, a host, or an
exception's text (a connection error names its server). A crash is summarized by its
exception type and the Dagster run to open for the rest.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg

from data_engine.config import settings

log = logging.getLogger(__name__)

#: The run tag every nightly schedule without run config stamps its tick into (ISO 8601).
#: Ours, not Dagster's hidden `.dagster/scheduled_execution_time`, which is an internal tag.
TICK_TAG = "truealpha/tick"

#: The column's own bound (`db/migrations/20260916T0848_factors_nightly_verdicts.sql`).
SUMMARY_LIMIT = 300

_NAME = re.compile(r"[a-z0-9_]+(@[a-z0-9_.:-]+)?")

INSERT_SQL = """
insert into mart.nightly_verdicts (check_name, ran_at, ok, summary, dagster_run_id)
values (%s, %s, %s, %s, %s)
"""


def check_name(check: str, universe: str | None = None) -> str:
    """`check`, or `check@universe` for a per-universe check. Not validated here: a manual
    run may name any universe, and `verdict()` records only declared names."""
    return f"{check}@{universe}" if universe else check


def is_valid_name(name: str) -> bool:
    """The column's own shape (`mart.nightly_verdicts.check_name`)."""
    return _NAME.fullmatch(name) is not None


def tick_of(context: Any) -> datetime | None:
    """The schedule tick a run carries in its tags, or None for a manual launch."""
    value = (getattr(context, "run_tags", None) or {}).get(TICK_TAG)
    return _aware(datetime.fromisoformat(value)) if value else None


def tick_from_config(executed_at: str) -> datetime:
    """A lane config's `executed_at` (the schedule's tick), as an aware timestamp."""
    return _aware(datetime.fromisoformat(executed_at))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def bounded(summary: str) -> str:
    """One line, within the column's bound, never empty."""
    line = " ".join(summary.split()) or "no summary"
    return line if len(line) <= SUMMARY_LIMIT else line[: SUMMARY_LIMIT - 1] + "…"


def record(name: str, *, ran_at: datetime, ok: bool, summary: str, run_id: str) -> None:
    """Append one verdict on its own autocommit connection: the row must survive whatever
    the check's own transaction does, exactly like the call ledger's (`sources.gateway`)."""
    if not is_valid_name(name):
        raise ValueError(f"not a verdict name: {name!r}")
    with psycopg.connect(settings.database_url, autocommit=True) as connection:
        connection.execute(INSERT_SQL, (name, ran_at, ok, bounded(summary), run_id[:64] or "unknown"))


@dataclass
class Outcome:
    """What the check body reports: the summary of its verdict (green, or the red it is
    about to raise). Left empty, a green run says `ok` and a crash names its exception."""

    summary: str = ""


@contextmanager
def verdict(name: str, *, registered: Collection[str], run_id: str, tick: datetime | None) -> Iterator[Outcome]:
    """Record the enclosed check's verdict — `ok = false` on any exception, which is then
    re-raised unchanged. `tick` is the run's schedule tick; a manual run (None) is dated by
    the wall clock when it completes, which is when its verdict became true."""
    outcome = Outcome()
    if name not in registered:
        log.info("%s is not a declared nightly check; no verdict recorded", name)
        yield outcome
        return
    try:
        yield outcome
    except Exception as exc:
        summary = outcome.summary or f"{type(exc).__name__} — see Dagster run {run_id[:8]}"
        try:
            record(name, ran_at=tick or datetime.now(UTC), ok=False, summary=f"failed: {summary}", run_id=run_id)
        except Exception:  # noqa: BLE001 - the check's own failure is the one to raise
            log.exception("could not record the red verdict of %s", name)
        raise
    # A green check whose verdict cannot be written raises: the watchdog would otherwise
    # read the previous night's row, and the run is where that loss becomes visible.
    record(name, ran_at=tick or datetime.now(UTC), ok=True, summary=outcome.summary or "ok", run_id=run_id)
