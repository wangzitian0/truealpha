"""The nightly verdict ``fetch_keys_once`` (#1411 K7, #1418 R2.B2).

Owner requirement (2026-10-20): one fetch key is fetched exactly once. A fetch key is
(seat, endpoint, canonical entity, as-of). The source gateway serves a key that already landed
from stored bytes (`sources.landed_keys`). This check proves it from the environment's own call
ledger, the only record of what was asked of a vendor.

The check reads the UTC day that has just ended. A key is a duplicate when the ledger holds two
successful rows with that key on that day. A failed attempt (a 429, a timeout) is not a fetch, so
a retry never counts. The ledger of an environment is its own database, so the count is per
environment.

The verdict is red when:

- a key has two or more successful rows on the day. The summary names the keys.
- the day holds successful vendor calls and none carries a key. Nothing could be counted, and a
  green verdict would be green while empty.

The verdict is pending when the day holds no successful vendor call. It judges nothing.

Calls without a key are counted and named by seat and endpoint (``unkeyed``). They are not judged:
a caller that does not name its key cannot prove it fetched once. The number is the work left.
A request whose answer is not final (a window that ends today) is made without a key, so it is
listed as unkeyed and never counted as a duplicate.

A forced tick (`force_fetch`) is a probe: it asks the vendors again, its rows carry no key, and it
neither reads nor writes the index. A request whose answer the fetch refuses to keep (a chart whose
session has not closed, an answer without the session's row) is asked again on purpose, so its row carries
no key either. Both are listed as unkeyed. A key that must be fetched again is deleted from
`staging.landed_fetch_keys` by an operator.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

#: Keys named in the one-line summary. The run log and the output metadata hold all of them.
SUMMARY_KEYS = 3

DUPLICATES_SQL = """
select fetch_key, count(*)
from staging.api_call_ledger
where called_at >= %s and called_at < %s and fetch_key is not null and ok
group by fetch_key
having count(*) > 1
order by count(*) desc, fetch_key
"""

#: Successful vendor calls that spent quota (``cost > 0``). A model call records ``cost = 0``, and
#: none of its requests is a resource that can be fetched twice.
COVERAGE_SQL = """
select count(*) filter (where fetch_key is not null), count(*) filter (where fetch_key is null)
from staging.api_call_ledger
where called_at >= %s and called_at < %s and ok and cost > 0
"""

UNKEYED_SQL = """
select source, endpoint, count(*)
from staging.api_call_ledger
where called_at >= %s and called_at < %s and ok and cost > 0 and fetch_key is null
group by source, endpoint
order by count(*) desc, source, endpoint
"""


@dataclass(frozen=True)
class Judgement:
    """What the ledger of one UTC day says about fetch keys."""

    day: date
    keyed: int
    unkeyed: int
    #: ``(ledger key, successful rows)`` for every key with more than one.
    duplicates: tuple[tuple[str, int], ...]
    #: ``(seat, endpoint, rows)`` of the successful calls that carry no key.
    unkeyed_by_endpoint: tuple[tuple[str, str, int], ...]

    @property
    def pending(self) -> bool:
        return self.keyed + self.unkeyed == 0

    @property
    def red(self) -> bool:
        return bool(self.duplicates) or (self.keyed == 0 and self.unkeyed > 0)

    @property
    def summary(self) -> str:
        counts = f"{self.keyed} keyed and {self.unkeyed} unkeyed calls"
        if self.pending:
            return f"{self.day}: no successful vendor call in the ledger; nothing to judge"
        if self.duplicates:
            named = ", ".join(f"{key} x{rows}" for key, rows in self.duplicates[:SUMMARY_KEYS])
            more = len(self.duplicates) - SUMMARY_KEYS
            tail = f" and {more} more" if more > 0 else ""
            return f"{self.day}: {len(self.duplicates)} key(s) fetched twice: {named}{tail}; {counts}"
        if self.keyed == 0:
            return f"{self.day}: {counts}; no call carries a fetch key, so none can be counted"
        return f"{self.day}: no key fetched twice; {counts}"


def judged_day(config_day: str | None, tick: datetime | None, *, now: datetime) -> date:
    """The UTC day to judge: the configured day, else the day before the schedule tick.

    A manual run without a day judges the day before ``now``. The tick of the nightly schedule falls
    after the last tick of the previous day, so that day is complete."""
    if config_day:
        return date.fromisoformat(config_day)
    reference = tick if tick is not None else now
    return reference.astimezone(UTC).date() - timedelta(days=1)


def evaluate(connection: Any, *, day: date) -> Judgement:
    """Judge the keys of ``day`` (UTC) on the environment's own ledger."""
    since = datetime.combine(day, time.min, tzinfo=UTC)
    window = (since, since + timedelta(days=1))
    duplicates = tuple((str(key), int(rows)) for key, rows in connection.execute(DUPLICATES_SQL, window).fetchall())
    keyed, unkeyed = connection.execute(COVERAGE_SQL, window).fetchone()
    by_endpoint = tuple(
        (str(source), str(endpoint), int(rows)) for source, endpoint, rows in connection.execute(UNKEYED_SQL, window)
    )
    return Judgement(day, int(keyed), int(unkeyed), duplicates, by_endpoint)
