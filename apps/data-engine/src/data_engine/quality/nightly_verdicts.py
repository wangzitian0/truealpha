# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import logging; import re; from collections.abc import Collection, Iterator; from contextlib import contextmanager; from dataclasses import dataclass; from datetime import UTC, datetime; from typing import Any; import psycopg; from truealpha_contracts.environment_contract import load_environment_contract; from data_engine.config import settings; from data_engine.environment_universes import rule_environment; log = logging.getLogger(__name__); TICK_TAG = "truealpha/tick"; SUMMARY_LIMIT = 300; _NAME = re.compile(r"[a-z0-9_]+(@[a-z0-9_.:-]+)?"); INSERT_SQL = ( "insert into mart.nightly_verdicts (check_name, ran_at, ok, summary, dagster_run_id) values (%s, %s, %s, %s, %s)" ); NEWEST_SQL = "select ok from mart.nightly_verdicts where check_name = %s order by ran_at desc, recorded_at desc limit 1"
def newest_is_red(name: str) -> bool:
    with psycopg.connect(settings.database_url, autocommit=True) as connection: row = connection.execute(NEWEST_SQL, (name,)).fetchone()
    return row is not None and row[0] is False
def check_name(check: str, universe: str | None = None) -> str: return f"{check}@{universe}" if universe else check
def is_registered(name: str, declared: Collection[str]) -> bool: return name in declared and load_environment_contract().expects_verdict(name, rule_environment())
def is_valid_name(name: str) -> bool: return _NAME.fullmatch(name) is not None
def tick_of(context: Any) -> datetime | None:
    value = (getattr(context, "run_tags", None) or {}).get(TICK_TAG)
    if not value: return None
    try: return _aware(datetime.fromisoformat(value))
    except (TypeError, ValueError): log.warning("run tag %s=%r is not an ISO 8601 time; dating the verdict by its completion", TICK_TAG, value); return None
def tick_from_config(executed_at: str) -> datetime: return _aware(datetime.fromisoformat(executed_at))
def _aware(value: datetime) -> datetime: return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
def bounded(summary: str) -> str: line = " ".join(summary.split()) or "no summary"; return line if len(line) <= SUMMARY_LIMIT else line[: SUMMARY_LIMIT - 1] + "…"
def record(name: str, *, ran_at: datetime, ok: bool | None, summary: str, run_id: str) -> None:
    if not is_valid_name(name): raise ValueError(f"not a verdict name: {name!r}")
    with psycopg.connect(settings.database_url, autocommit=True) as connection: connection.execute(INSERT_SQL, (name, ran_at, ok, bounded(summary), run_id[:64] or "unknown"))
@dataclass
class Outcome: summary: str = ""; pending: bool = False
@contextmanager
def verdict(name: str, *, registered: Collection[str], run_id: str, tick: datetime | None) -> Iterator[Outcome]:
    outcome = Outcome()
    if not is_registered(name, registered): log.info("%s is not a nightly check of this environment; no verdict recorded", name); yield outcome; return
    try: yield outcome
    except Exception as exc:
        summary = outcome.summary or f"{type(exc).__name__} — see Dagster run {run_id[:8]}"
        try: record(name, ran_at=tick or datetime.now(UTC), ok=False, summary=f"failed: {summary}", run_id=run_id)
        except Exception: log.exception("could not record the red verdict of %s", name)
        raise
    record( name, ran_at=tick or datetime.now(UTC), ok=None if outcome.pending else True, summary=outcome.summary or ("pending" if outcome.pending else "ok"), run_id=run_id, )
