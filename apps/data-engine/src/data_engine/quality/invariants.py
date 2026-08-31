"""Self-evident invariants over the warehouse, checked against a real database (#429).

Every other check in this repository compares the system against something this
repository also authored — a fixture, a schema shape, a manifest, a grep list. Those
catch a deviation from the author's intent; they cannot catch the intent being wrong.
The 2026-07-27 chain audit is the proof: 13 gates green, 57 tests passing, and AAPL's
revenue in production mart was from FY2018.

The invariants here are different in kind. Each one is true regardless of what anyone
believes about this system — "gross profit cannot exceed revenue" needs no knowledge of
XBRL tags, capture spines, or point-in-time semantics to evaluate. That is the property
that makes them able to fail when the code and its tests agree with each other and are
both wrong.

They are deliberately NOT run against a seeded CI database. A database this repository
populated is self-consistent by construction; the signal only exists against a database
real captures wrote. `scripts/assert_data_invariants.py` is the operational entry point.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from psycopg import Connection


@dataclass(frozen=True)
class Invariant:
    """One statement that must hold, with the reason it needs no domain knowledge.

    `sql` returns exactly one row with one integer column: the violation count. It never
    returns the offending rows — `sample_sql` does that, and only when something failed,
    so a green run costs one cheap aggregate per invariant.
    """

    id: str
    statement: str
    self_evident_because: str
    sql: str
    sample_sql: str | None = None


@dataclass(frozen=True)
class InvariantResult:
    invariant: Invariant
    violations: int
    samples: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.violations == 0


# Ordered by how directly a violation corrupts a consumer-visible number.
INVARIANTS: tuple[Invariant, ...] = (
    Invariant(
        id="I1",
        statement="A normalized financial fact never reports gross profit above revenue",
        self_evident_because=(
            "gross profit is revenue minus cost of revenue, so it cannot exceed revenue for any issuer "
            "in any industry under any accounting standard"
        ),
        sql="""
        select count(*) from staging.capture_observation_payloads
        where normalized_payload->>'gross_profit' is not null
          and normalized_payload->>'revenue' is not null
          and (normalized_payload->>'gross_profit')::numeric > (normalized_payload->>'revenue')::numeric
        """,
        sample_sql="""
        select distinct o.subject_id || ': gross_profit=' || (p.normalized_payload->>'gross_profit')
               || ' > revenue=' || (p.normalized_payload->>'revenue')
        from staging.capture_observation_payloads p
        join staging.capture_normalized_observations o using (observation_id)
        where (p.normalized_payload->>'gross_profit')::numeric > (p.normalized_payload->>'revenue')::numeric
        limit 10
        """,
    ),
    Invariant(
        id="I2",
        statement="Every raw.fetches row points into the configured raw object bucket",
        self_evident_because=(
            "a pointer row whose bucket does not exist cannot be dereferenced, so the bytes it claims "
            "to describe are unreachable no matter what the row says"
        ),
        sql="select count(*) from raw.fetches where object_uri not like 's3://truealpha-raw/%%'",
        sample_sql="select distinct object_uri from raw.fetches where object_uri not like 's3://truealpha-raw/%%' limit 5",
    ),
    Invariant(
        id="I3",
        statement="A JSON source response is never 1 byte or fewer",
        self_evident_because="the shortest syntactically valid JSON document is 2 bytes ('{}')",
        sql="select count(*) from raw.fetches where content_type = 'application/json' and byte_length <= 1",
        sample_sql=(
            "select source || ':' || source_record_id || ' byte_length=' || byte_length from raw.fetches "
            "where content_type = 'application/json' and byte_length <= 1 limit 5"
        ),
    ),
    Invariant(
        id="I4",
        statement="A captured market price carries at most 4 decimal places",
        self_evident_because=(
            "no exchange quotes a US equity beyond four decimals, so extra digits are an artifact "
            "introduced after the vendor's response, not price information"
        ),
        sql="""
        select count(*) from staging.capture_observation_payloads
        where normalized_payload->>'close' is not null
          and length(split_part(normalized_payload->>'close', '.', 2)) > 4
        """,
        sample_sql="""
        select distinct normalized_payload->>'close' from staging.capture_observation_payloads
        where normalized_payload->>'close' is not null
          and length(split_part(normalized_payload->>'close', '.', 2)) > 4
        limit 5
        """,
    ),
    Invariant(
        id="I5",
        statement="Shares outstanding, when asserted, is positive",
        self_evident_because="a listed issuer with zero or negative shares outstanding is not a listed issuer",
        sql="""
        select count(*) from staging.capture_observation_payloads
        where normalized_payload->>'shares_outstanding' is not null
          and (normalized_payload->>'shares_outstanding')::numeric <= 0
        """,
    ),
    Invariant(
        id="I6",
        statement="The governed current pointer resolves to a run that has mart results",
        self_evident_because=(
            "a head pointer with nothing behind it makes every consumer read fail, independently of "
            "whether the numbers behind it are right"
        ),
        # Deliberately spans EVERY universe: a head with nothing behind it breaks its
        # consumers whichever pipeline advanced it, so canary counts here exactly as the
        # core does. The universe is projected rather than filtered, so a violation names
        # which pipeline is dangling instead of leaving the reader to guess.
        sql="""
        select count(*) from mart.current_pointer_head h
        where not exists (select 1 from mart.topt_core_results r where r.run_id = h.target_run_id)
        """,
        sample_sql=("select universe_id, target_run_id from mart.current_pointer_head order by universe_id limit 5"),
    ),
)


def check(connection: Connection[Any], invariants: Sequence[Invariant] = INVARIANTS) -> tuple[InvariantResult, ...]:
    """Evaluate every invariant against `connection`, collecting samples only for failures."""
    results: list[InvariantResult] = []
    for invariant in invariants:
        row = connection.execute(invariant.sql).fetchone()
        violations = int(row[0]) if row is not None else 0
        samples: tuple[str, ...] = ()
        if violations and invariant.sample_sql:
            samples = tuple(str(item[0]) for item in connection.execute(invariant.sample_sql).fetchall())
        results.append(InvariantResult(invariant=invariant, violations=violations, samples=samples))
    return tuple(results)
