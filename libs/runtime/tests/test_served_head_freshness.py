"""The served head ages at read time (#1062).

`mart.head_freshness` turns a refresh time and a cadence family into an age, a limit, a
label, an availability and a reason code. `mart.served_head` applies it to every governed
head. The limits live in `mart.freshness_limit`. Git is their authority: a replay of the
migration restores the shipped values.

Every test builds a scratch database from the real migration chain, so a change to the
migration shows in the result. Rows are seeded inside a transaction that is rolled back.
Each time is relative to one fixed instant, so no test depends on the wall clock.

The owner limits (2026-10-06): daily data 3 days, weekly data 14 days, quarterly data
30 days, and no limit above 30 days. Past its limit a value reads stale. Past 30 days it
is withheld.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import errors, sql
from truealpha_runtime.testing import apply_migration_chain, read_seed_rows, skip_or_fail

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION = REPO_ROOT / "db" / "migrations" / "20261006T1020_datahub_served_head_freshness.sql"
_DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/truealpha"

AS_OF = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


def _named(database: str) -> str:
    base = urlsplit(os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_URL))
    return urlunsplit((base.scheme, base.netloc, f"/{database}", base.query, ""))


@pytest.fixture(scope="module")
def database() -> Iterator[str]:
    """One database with the declared chain applied. Tests never commit to it."""
    name = f"truealpha_served_head_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    try:
        with psycopg.connect(_named("postgres"), connect_timeout=3, autocommit=True) as admin:
            admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        skip_or_fail(f"no local Postgres; CI runs the required integration coverage ({error})")
    try:
        apply_migration_chain(_named(name))
        yield _named(name)
    finally:
        with psycopg.connect(_named("postgres"), autocommit=True) as admin:
            admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))


@pytest.fixture
def conn(database: str) -> Iterator[psycopg.Connection]:
    """A connection in a transaction that is always rolled back."""
    with psycopg.connect(database) as connection:
        try:
            yield connection
        finally:
            connection.rollback()


@dataclass(frozen=True)
class Reading:
    age_hours: Decimal | None
    limit_hours: int
    freshness: str
    availability: str
    staleness_reason: str | None


def read(conn: psycopg.Connection, age: timedelta, family: str | None) -> Reading:
    """`head_freshness` for a head refreshed `age` before the fixed instant."""
    row = conn.execute(
        "select age_hours, limit_hours, freshness, availability, staleness_reason from mart.head_freshness(%s, %s, %s)",
        (AS_OF - age, family, AS_OF),
    ).fetchone()
    assert row is not None
    return Reading(*row)


def hours(value: str) -> Decimal:
    return Decimal(value)


# --- T1: the daily limit, at both sides of every boundary ---------------------------------


@pytest.mark.parametrize(
    ("age", "age_hours", "freshness", "availability", "reason"),
    [
        (timedelta(hours=71.9), "71.9", "fresh", "available", None),
        (timedelta(hours=72), "72", "fresh", "available", None),
        (timedelta(hours=72.1), "72.1", "stale", "available", "older_than_3d"),
        (2 * DAY, "48", "fresh", "available", None),
        (4 * DAY, "96", "stale", "available", "older_than_3d"),
        (timedelta(hours=720), "720", "stale", "available", "older_than_3d"),
        (timedelta(hours=720.1), "720.1", "stale", "unavailable", "older_than_30d"),
        (31 * DAY, "744", "stale", "unavailable", "older_than_30d"),
    ],
    ids=["71.9h", "72h-is-fresh", "72.1h", "2d", "4d", "30d-is-served", "30d+6min", "31d"],
)
def test_the_daily_limit_is_three_days_and_the_cap_is_thirty(
    conn: psycopg.Connection,
    age: timedelta,
    age_hours: str,
    freshness: str,
    availability: str,
    reason: str | None,
) -> None:
    got = read(conn, age, "daily")
    assert got == Reading(hours(age_hours), 72, freshness, availability, reason)


# --- T2: the same age reads differently by cadence ----------------------------------------


def test_the_same_ten_day_age_is_stale_for_daily_data_and_fresh_for_weekly_data(conn: psycopg.Connection) -> None:
    daily = read(conn, 10 * DAY, "daily")
    weekly = read(conn, 10 * DAY, "weekly")
    assert (daily.freshness, daily.limit_hours, daily.staleness_reason) == ("stale", 72, "older_than_3d")
    assert (weekly.freshness, weekly.limit_hours, weekly.staleness_reason) == ("fresh", 336, None)
    assert daily.age_hours == weekly.age_hours == hours("240")


def test_the_weekly_limit_is_fourteen_days(conn: psycopg.Connection) -> None:
    assert read(conn, 14 * DAY, "weekly") == Reading(hours("336"), 336, "fresh", "available", None)
    assert read(conn, 14 * DAY + HOUR, "weekly") == Reading(hours("337"), 336, "stale", "available", "older_than_14d")


# --- T3: a quarterly value goes from fresh straight to withheld ---------------------------


def test_a_quarterly_value_is_fresh_for_thirty_days_and_then_withheld(conn: psycopg.Connection) -> None:
    assert read(conn, 29 * DAY, "quarterly") == Reading(hours("696"), 720, "fresh", "available", None)
    assert read(conn, 30 * DAY, "quarterly") == Reading(hours("720"), 720, "fresh", "available", None)
    withheld = read(conn, 31 * DAY, "quarterly")
    assert (withheld.availability, withheld.staleness_reason) == ("unavailable", "older_than_30d")


def _sweep(conn: psycopg.Connection, family: str) -> list[tuple[Decimal, Reading]]:
    """Readings for ages from 0 to 1000 hours in steps of 0.7 hour, in one query."""
    rows = conn.execute(
        "select h, f.age_hours, f.limit_hours, f.freshness, f.availability, f.staleness_reason "
        "from generate_series(0, 1000, 0.7) as h, "
        "lateral mart.head_freshness(%s::timestamptz - h * interval '1 hour', %s, %s) as f",
        (AS_OF, family, AS_OF),
    ).fetchall()
    assert len(rows) > 1400, "the sweep must cover the whole range"
    return [(Decimal(h), Reading(*rest)) for h, *rest in rows]


@pytest.mark.parametrize("family", ["daily", "weekly", "quarterly"])
def test_no_family_serves_a_value_older_than_thirty_days(conn: psycopg.Connection, family: str) -> None:
    for age, got in _sweep(conn, family):
        assert (got.availability == "available") == (age <= 720), (family, age, got)
        # A value is fresh exactly when it is served and not older than its limit.
        assert (got.freshness == "fresh") == (age <= got.limit_hours and age <= 720), (family, age, got)


def test_a_quarterly_value_is_never_served_stale(conn: psycopg.Connection) -> None:
    """Quarterly equals the cap (720 hours), so no age is both served and past the limit."""
    sweep = _sweep(conn, "quarterly")
    assert [age for age, got in sweep if got.freshness == "stale" and got.availability == "available"] == []
    assert {got.freshness for _, got in sweep if got.availability == "available"} == {"fresh"}
    assert {got.availability for _, got in sweep if got.freshness == "stale"} == {"unavailable"}


# --- T4: an unknown family gets the strictest limit ---------------------------------------


@pytest.mark.parametrize("family", [None, "no-such-family", "withhold", ""])
def test_an_unknown_or_missing_family_reads_with_the_strictest_limit(
    conn: psycopg.Connection, family: str | None
) -> None:
    assert read(conn, 4 * DAY, family) == Reading(hours("96"), 72, "stale", "available", "older_than_3d")


def test_a_missing_refresh_time_fails_closed(conn: psycopg.Connection) -> None:
    row = conn.execute(
        "select age_hours, limit_hours, freshness, availability, staleness_reason "
        "from mart.head_freshness(null, 'daily', %s)",
        (AS_OF,),
    ).fetchone()
    assert row == (None, 72, "unknown", "unavailable", "refresh_time_unknown")


def test_a_refresh_time_in_the_future_counts_as_age_zero(conn: psycopg.Connection) -> None:
    assert read(conn, -2 * DAY, "daily") == Reading(hours("0"), 72, "fresh", "available", None)


# --- the limit never exceeds the cap, and the reason names the real limit ------------------


def test_no_limit_is_longer_than_the_withhold_limit(conn: psycopg.Connection) -> None:
    conn.execute("update mart.freshness_limit set hours = 400 where limit_key = 'withhold'")
    got = read(conn, 17 * DAY, "quarterly")  # 408 hours
    assert (got.limit_hours, got.availability, got.staleness_reason) == (400, "unavailable", "older_than_400h")


def test_a_limit_that_is_not_whole_days_is_named_in_hours(conn: psycopg.Connection) -> None:
    conn.execute("update mart.freshness_limit set hours = 36 where limit_key = 'daily'")
    assert read(conn, 2 * DAY, "daily").staleness_reason == "older_than_36h"


# --- T5: the served head ------------------------------------------------------------------


def _environment(conn: psycopg.Connection) -> str:
    row = conn.execute("select environment from mart.environment_identity").fetchone()
    assert row is not None
    return str(row[0])


def seed_head(
    conn: psycopg.Connection,
    universe_id: str,
    age: timedelta,
    *,
    environment: str | None = None,
    earlier_age: timedelta | None = None,
) -> tuple[str, str]:
    """Seed one governed head `age` before the transaction time.

    With `earlier_age` it seeds two advances; the newer one is the head. Returns the head run
    id and the run id of the first advance.
    """
    environment = environment or _environment(conn)

    def node() -> str:
        digest = uuid.uuid4().hex * 2
        run_id = f"capture-run:{digest}"
        conn.execute(
            "insert into staging.evidence_nodes (node_id, kind, content_sha256, valid_from, transaction_time, "
            "recorded_at) values (%s, 'capture_run', %s, '2026-03-31', now(), now())",
            (run_id, digest),
        )
        return run_id

    def advance(run_id: str, sequence: int, previous: str | None, when: timedelta) -> None:
        digest = uuid.uuid4().hex * 2
        conn.execute(
            "insert into mart.current_pointer (pointer_id, content_sha256, environment, universe_id, "
            "universe_version, factor_id, target_run_id, sequence, previous_run_id, advanced_at) "
            "values (%s, %s, %s, %s, 'v1', 'f', %s, %s, %s, now() - %s)",
            (f"current-pointer:{digest}", digest, environment, universe_id, run_id, sequence, previous, when),
        )

    first = node()
    advance(first, 0, None, earlier_age if earlier_age is not None else age)
    if earlier_age is None:
        return first, first
    head = node()
    advance(head, 1, first, age)
    return head, first


def served(conn: psycopg.Connection, universe_id: str) -> list[dict[str, object]]:
    cursor = conn.execute("select * from mart.served_head where universe_id = %s", (universe_id,))
    names = [column.name for column in cursor.description or []]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def test_the_served_head_ages_each_governed_head_by_its_own_age(conn: psycopg.Connection) -> None:
    stale_run, _ = seed_head(conn, "universe:topt-us-t5-4d", 4 * DAY)
    fresh_run, _ = seed_head(conn, "universe:topt-us-t5-2d", 2 * DAY)
    withheld_run, _ = seed_head(conn, "universe:topt-us-t5-31d", 31 * DAY)

    (stale,) = served(conn, "universe:topt-us-t5-4d")
    assert (stale["freshness"], stale["availability"], stale["staleness_reason"]) == (
        "stale",
        "available",
        "older_than_3d",
    )
    assert (stale["age_hours"], stale["limit_hours"]) == (hours("96"), 72)
    assert stale["run_id"] == stale["head_run_id"] == stale_run

    (fresh,) = served(conn, "universe:topt-us-t5-2d")
    assert (fresh["freshness"], fresh["availability"], fresh["staleness_reason"]) == ("fresh", "available", None)
    assert fresh["run_id"] == fresh_run

    (withheld,) = served(conn, "universe:topt-us-t5-31d")
    assert (withheld["freshness"], withheld["availability"], withheld["staleness_reason"]) == (
        "stale",
        "unavailable",
        "older_than_30d",
    )
    assert withheld["run_id"] is None, "a withheld head must not hand out its run"
    assert withheld["head_run_id"] == withheld_run, "the head run stays visible for audit"


def test_the_served_head_carries_the_registry_row_and_the_pointer_columns(conn: psycopg.Connection) -> None:
    seed_head(conn, "universe:qqq-us-t5", DAY)
    (row,) = served(conn, "universe:qqq-us-t5")
    assert (row["artifact_key"], row["family"]) == ("head:qqq", "daily")
    assert (row["environment"], row["universe_version"], row["factor_id"], row["sequence"]) == (
        _environment(conn),
        "v1",
        "f",
        0,
    )
    assert isinstance(row["advanced_at"], datetime)


def test_a_head_of_another_environment_is_invisible(conn: psycopg.Connection) -> None:
    seed_head(conn, "universe:topt-us-t5-other", DAY, environment="not-this-environment")
    assert served(conn, "universe:topt-us-t5-other") == []
    # The pointer view still holds it, so the filter is what hides it.
    assert conn.execute(
        "select count(*) from mart.current_pointer_head where universe_id = 'universe:topt-us-t5-other'"
    ).fetchone() == (1,)


def test_the_served_head_ages_the_newest_advance(conn: psycopg.Connection) -> None:
    head_run, first_run = seed_head(conn, "universe:topt-us-t5-two", DAY, earlier_age=10 * DAY)
    (row,) = served(conn, "universe:topt-us-t5-two")
    assert (row["head_run_id"], row["sequence"], row["age_hours"], row["freshness"]) == (
        head_run,
        1,
        hours("24"),
        "fresh",
    )
    assert head_run != first_run


def test_two_registered_cadences_give_the_same_ten_day_head_two_readings(conn: psycopg.Connection) -> None:
    conn.execute(
        "insert into mart.served_artifact (artifact_key, lane, universe_like, family, served_to, wired) "
        "values ('t2:weekly-head', 'capture', 'universe:weekly-%', 'weekly', 'consumers', false)"
    )
    seed_head(conn, "universe:topt-us-t2", 10 * DAY)
    seed_head(conn, "universe:weekly-us-t2", 10 * DAY)
    (daily,) = served(conn, "universe:topt-us-t2")
    (weekly,) = served(conn, "universe:weekly-us-t2")
    assert (daily["freshness"], weekly["freshness"]) == ("stale", "fresh")
    assert daily["age_hours"] == weekly["age_hours"]


def test_a_head_with_no_registry_row_gets_the_strictest_limit(conn: psycopg.Connection) -> None:
    seed_head(conn, "universe:unregistered-us", 4 * DAY)
    (row,) = served(conn, "universe:unregistered-us")
    assert (row["artifact_key"], row["family"]) == (None, None)
    assert (row["limit_hours"], row["freshness"], row["staleness_reason"]) == (72, "stale", "older_than_3d")


def test_a_head_that_matches_two_registry_rows_takes_the_strictest_and_appears_once(conn: psycopg.Connection) -> None:
    conn.execute(
        "insert into mart.served_artifact (artifact_key, lane, universe_like, family, served_to, wired) "
        "values ('t5:overlap', 'capture', 'universe:topt-us-overlap%', 'weekly', 'consumers', false)"
    )
    seed_head(conn, "universe:topt-us-overlap-1", 10 * DAY)
    (row,) = served(conn, "universe:topt-us-overlap-1")
    assert (row["artifact_key"], row["limit_hours"], row["freshness"]) == ("head:topt", 72, "stale")


# --- the reader roles ---------------------------------------------------------------------


@pytest.mark.parametrize("role", ["mart_readonly", "app_ops_reader"])
def test_the_reader_roles_can_read_the_served_head_and_its_tables(conn: psycopg.Connection, role: str) -> None:
    """`db/roles.sql` grants the served head and the two tables behind it to the roles that read
    the pointer head today: the Web App (`mart_readonly`) and the admin pages (`app_ops_reader`)."""
    run_id, _ = seed_head(conn, "universe:topt-us-roles", 4 * DAY)
    conn.execute(sql.SQL("set local role {}").format(sql.Identifier(role)))
    (row,) = served(conn, "universe:topt-us-roles")
    assert (row["head_run_id"], row["freshness"]) == (run_id, "stale")
    registry_rows = len(read_seed_rows(MIGRATION, "mart.served_artifact"))
    limit_rows = len(read_seed_rows(MIGRATION, "mart.freshness_limit"))
    assert conn.execute("select count(*) from mart.served_artifact").fetchone() == (registry_rows,)
    assert conn.execute("select count(*) from mart.freshness_limit").fetchone() == (limit_rows,)
    assert conn.execute("select freshness from mart.head_freshness(now(), 'daily')").fetchone() == ("fresh",)


# --- T6: the limits are shipped state, and git is their authority --------------------------


def test_a_changed_limit_changes_the_reading(conn: psycopg.Connection) -> None:
    """The function reads the table. No literal stands in for a limit."""
    seed_head(conn, "universe:topt-us-t6", 4 * DAY)
    assert served(conn, "universe:topt-us-t6")[0]["freshness"] == "stale"
    conn.execute("update mart.freshness_limit set hours = 96 where limit_key = 'daily'")
    (row,) = served(conn, "universe:topt-us-t6")
    assert (row["limit_hours"], row["freshness"], row["staleness_reason"]) == (96, "fresh", None)
    assert read(conn, 4 * DAY + HOUR, "daily").staleness_reason == "older_than_4d"


@pytest.mark.parametrize("hours_value", [721, 1440, 0, -1])
def test_a_limit_above_thirty_days_or_below_one_hour_is_refused(conn: psycopg.Connection, hours_value: int) -> None:
    with pytest.raises(errors.CheckViolation), conn.transaction():
        conn.execute("update mart.freshness_limit set hours = %s where limit_key = 'weekly'", (hours_value,))
    with pytest.raises(errors.CheckViolation), conn.transaction():
        conn.execute("insert into mart.freshness_limit (limit_key, hours) values ('new', %s)", (hours_value,))


def test_a_registry_row_cannot_use_the_withhold_limit_or_an_unknown_family(conn: psycopg.Connection) -> None:
    insert = (
        "insert into mart.served_artifact (artifact_key, lane, family, served_to, wired) "
        "values ('t6:bad', 'capture', %s, 'internal', false)"
    )
    with pytest.raises(errors.CheckViolation), conn.transaction():
        conn.execute(insert, ("withhold",))
    with pytest.raises(errors.ForeignKeyViolation), conn.transaction():
        conn.execute(insert, ("monthly",))


def test_a_replay_of_the_migration_restores_the_shipped_limits(conn: psycopg.Connection) -> None:
    """Git is the authority: an edit outside a reviewed migration lasts until the next boot."""
    seed_head(conn, "universe:topt-us-t6-replay", 4 * DAY)
    conn.execute("update mart.freshness_limit set hours = 96 where limit_key = 'daily'")
    assert served(conn, "universe:topt-us-t6-replay")[0]["freshness"] == "fresh"

    conn.execute(MIGRATION.read_text(encoding="utf-8"))

    assert conn.execute("select hours from mart.freshness_limit where limit_key = 'daily'").fetchone() == (72,)
    assert served(conn, "universe:topt-us-t6-replay")[0]["freshness"] == "stale"


def test_a_replay_restores_a_deleted_limit_and_a_changed_registry_row(conn: psycopg.Connection) -> None:
    conn.execute("update mart.served_artifact set family = 'weekly', wired = false where artifact_key = 'head:topt'")
    conn.execute("update mart.served_artifact set wired = true where artifact_key = 'market-data'")
    conn.execute("delete from mart.served_artifact where artifact_key = 'head:qqq'")

    conn.execute(MIGRATION.read_text(encoding="utf-8"))

    rows = dict(
        conn.execute(
            "select artifact_key, family || ':' || wired::text from mart.served_artifact "
            "where artifact_key in ('head:topt', 'market-data', 'head:qqq')"
        ).fetchall()
    )
    assert rows == {"head:topt": "daily:true", "market-data": "weekly:false", "head:qqq": "daily:true"}


def test_a_replay_with_no_change_takes_no_strong_lock(conn: psycopg.Connection) -> None:
    """Boot-lock rule: a replay on a settled database holds only ACCESS SHARE, ROW EXCLUSIVE
    and the SHARE UPDATE EXCLUSIVE of a comment. A strong lock on a mart relation would queue
    the boot behind every open reader."""
    conn.execute(MIGRATION.read_text(encoding="utf-8"))
    held = conn.execute(
        """
        select format('%s.%s', n.nspname, c.relname), l.mode
        from pg_locks l
        join pg_class c on c.oid = l.relation
        join pg_namespace n on n.oid = c.relnamespace
        where l.pid = pg_backend_pid() and l.locktype = 'relation' and l.granted and n.nspname = 'mart'
        """
    ).fetchall()
    strong = sorted(
        (name, mode)
        for name, mode in held
        if mode not in {"AccessShareLock", "RowExclusiveLock", "ShareUpdateExclusiveLock"}
    )
    assert held, "the probe saw no lock at all, so it cannot say anything"
    assert strong == []


def test_the_database_holds_exactly_the_seed_in_the_migration(conn: psycopg.Connection) -> None:
    """The text reader that other tests use must agree with what the migration really stores."""
    limits = conn.execute("select limit_key, hours from mart.freshness_limit").fetchall()
    assert sorted(limits) == sorted(
        (row["limit_key"], row["hours"]) for row in read_seed_rows(MIGRATION, "mart.freshness_limit")
    )
    columns = "artifact_key, lane, schedule_name, universe_like, family, served_to, wired"
    registry = conn.execute(f"select {columns} from mart.served_artifact").fetchall()
    seeded = [tuple(row.values()) for row in read_seed_rows(MIGRATION, "mart.served_artifact")]
    assert sorted(registry, key=lambda row: row[0]) == sorted(seeded, key=lambda row: row[0])
    assert len(registry) >= 14
