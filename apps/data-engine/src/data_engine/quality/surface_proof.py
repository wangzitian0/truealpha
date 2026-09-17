"""Every report surface serves the governed head — proven against the environment's own
database, nightly (#855 C1).

The deploy's Playwright walk proves each route renders; nothing proved that the numbers on
it belong to the run the governed pointer names. Each App reader picks its run with its own
SQL, copied here verbatim so the proof asks exactly what the page asks, and the proof is
that every answer is the head: a page serving an older run than its neighbours contradicts
the App's own head with every gate green. "The head" is the one the reader itself would pick
once every universe's reports are current: the theme reader is universe-blind, so its head is
the newest governed head among the universes the theme lane covers, not TOPT's (#910). The
coverage report is held to more than its run:
it is recomputed from the same tables, and a stored report that no longer matches what the
tables say is stale even when its run id is right.

Not a fixture check. Like `quality.invariants`, this only means something against a database
real ticks wrote, so it runs as an op of the nightly quality job and its verdict is a red or
green Dagster run (init.md rule 9).

Three states per surface, not two. MATCH and MISMATCH as above; IN-PROGRESS for a surface
that does not match while its universe is demonstrably settling — a tick or a head-reports
run for it has not finished, or its head advanced minutes ago and the reports that follow it
are being written (`fresh_heads_without_reports`). The job waits for a universe to settle
before proving (`lanes.quality`), so IN-PROGRESS is what is left when the wait ran out; it
does not fail the run, and it is never granted to a surface whose universe is quiet.

A surface whose lane is deliberately off here is `not-run-in-this-environment`, as holdings
is on an environment with no QQQ tick: the theme lane is off where no model provider is
seated (`llm.is_configured()`), because nothing can judge a segment there. A lane that is on
and produced nothing is a MISMATCH, and the line says what the plane under it holds.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from psycopg import Connection
from truealpha_contracts.common import CaptureEnvironment

from data_engine.datahub.question_coverage import (
    GOVERNING_FACTOR,
    UNIVERSE_PREFIXES,
    GovernedHead,
    compile_report,
    governed_head,
    stored_report_run,
)
from data_engine.sources import llm

TOPT = "topt"
QQQ = "universe-list:qqq"

#: `apps/app-web/src/server/mart/strategy-run-repository.ts` resolves the governed strategy
#: run through this view (`db/migrations/20260907T0630_bt_governed_strategy_run.sql`):
#: /research/rankings, /strategy, /compare, /trace and /coverage all read it.
_STRATEGY_HEAD_SQL = "select target_run_id, strategy_run_id from mart.governed_strategy_run"
#: `apps/app-web/src/server/mart/theme-purity.ts` `LATEST_RUN_SQL`: the newest run in the
#: table, deliberately not the pointer — which is exactly why it has to be checked against it.
#: No universe filter: every universe's head gets purity rows, and the page names a run and a
#: cutoff, never a universe, so it serves whichever head cut off last (`themes_verdict`).
_THEMES_HEAD_SQL = """
select run_id, max(cutoff) as cutoff
from mart.issuer_theme_purity
group by run_id
order by max(cutoff) desc
limit 1
"""
#: Whether a run has purity rows, i.e. whether the theme reader could select it.
_THEME_ROWS_SQL = "select exists (select 1 from mart.issuer_theme_purity where run_id = %s)"
#: Whether any run a universe's pointer ever named has purity rows: whether the theme lane
#: covers that universe in this environment.
_THEME_UNIVERSE_SQL = """
select exists (
    select 1 from mart.current_pointer p
    join mart.issuer_theme_purity t on t.run_id = p.target_run_id
    where p.environment = %s and p.factor_id = %s and p.universe_id like %s
)
"""
#: `apps/app-web/src/server/mart/fund-valuation.ts` `QQQ_POINTER_HEAD_SQL`.
_HOLDINGS_HEAD_SQL = """
select target_run_id as run_id from mart.current_pointer_head
where environment = 'production' and factor_id = 'gross_profit_per_employee'
  and universe_id like 'universe:qqq-us-%%'
order by advanced_at desc limit 1
"""
_HOLDINGS_ROWS_SQL = "select count(*) from mart.fund_virtual_company where run_id = %s"
#: `apps/app-web/src/server/admin/datahub-stats.ts` `QUESTION_COVERAGE_SQL`.
_COVERAGE_HEAD_SQL = """
select distinct on (universe_id) universe_id, run_id, payload
from mart.question_coverage_report
order by universe_id, created_at desc
"""
#: What the theme lane had to work with, for a MISMATCH line on an empty table: the accepted
#: segment partitions knowable at the head's cutoff (`theme_purity._PARTITION_SQL`'s vintage
#: rule, without the member join).
_SEGMENT_PARTITIONS_SQL = """
select count(distinct partition_id) from staging.issuer_segment_revenue_facts
where knowable_at <= %s
"""
#: When the pointer row naming `target_run_id` was written: `advanced_at` is the tick's
#: cutoff, which a tick that runs for an hour passes long before it commits.
_POINTER_RECORDED_SQL = """
select max(created_at) from mart.current_pointer
where environment = %s and factor_id = %s and universe_id = %s and target_run_id = %s
"""
#: A universe this environment never ticks: nothing to serve and nothing served.
_NOT_RUN_HERE = "not-run-in-this-environment"
_THEMES_SURFACE = "/research/themes"
_THEMES_READER = "theme-purity.ts LATEST_RUN_SQL"


@dataclass(frozen=True)
class SurfaceVerdict:
    """One surface: the run its reader serves, against the run the pointer names."""

    surface: str
    reader: str
    expected_run: str | None
    served_run: str | None
    #: Why a surface whose run is right is still wrong (a stored report the tables no
    #: longer agree with), or context for a right one.
    detail: str = ""
    stale: bool = False
    #: The universe whose head this surface must serve.
    universe: str | None = None
    #: Why this surface's universe is still settling, when it is; set only on a surface that
    #: does not match.
    in_progress: str = ""

    @property
    def ok(self) -> bool:
        return self.expected_run is not None and self.served_run == self.expected_run and not self.stale

    @property
    def state(self) -> str:
        if self.ok:
            return "MATCH"
        return "IN-PROGRESS" if self.in_progress else "MISMATCH"

    @property
    def mismatched(self) -> bool:
        return self.state == "MISMATCH"

    @property
    def line(self) -> str:
        served = self.served_run or "nothing"
        expected = self.expected_run or "no governed head"
        notes = [note for note in (self.detail, self.in_progress) if note]
        suffix = f" — {'; '.join(notes)}" if notes else ""
        return f"{self.state} {self.surface}: serves {served[:24]} vs head {expected[:24]}{suffix}"


def _heads(connection: Connection[Any]) -> dict[str, GovernedHead | None]:
    environment = CaptureEnvironment.PRODUCTION.value
    return {
        universe: governed_head(connection, universe_prefix=prefix, environment=environment)
        for universe, prefix in UNIVERSE_PREFIXES.items()
    }


def fresh_heads_without_reports(connection: Connection[Any], *, now: datetime, grace: timedelta) -> dict[str, str]:
    """universe -> why, for a head whose pointer row is younger than `grace` and whose reports
    do not name it yet: the window between a tick's commit and the head-reports run the
    pointer sensor launches for it. Older than `grace`, the same state is a MISMATCH — the
    sensor did not follow the head. Read-only."""
    settling: dict[str, str] = {}
    for universe, head in _heads(connection).items():
        if head is None or stored_report_run(connection, head.universe_id) == head.run_id:
            continue
        row = connection.execute(
            _POINTER_RECORDED_SQL,
            (CaptureEnvironment.PRODUCTION.value, GOVERNING_FACTOR, head.universe_id, head.run_id),
        ).fetchone()
        recorded = row[0] if row else None
        if recorded is not None and now - recorded < grace:
            minutes = max(0, int((now - recorded).total_seconds() // 60))
            settling[universe] = f"head {head.run_id[:24]} recorded {minutes} min ago; its reports are not written yet"
    return settling


def prove(
    connection: Connection[Any], *, executed_at: datetime, settling: Mapping[str, str] | None = None
) -> tuple[SurfaceVerdict, ...]:
    """Every surface's served run against the governed head, plus the coverage report against
    its own recomputation. Read-only.

    `settling` names the universes that have not settled (universe -> why): a surface of one
    that does not match is IN-PROGRESS rather than MISMATCH."""
    environment = CaptureEnvironment.PRODUCTION.value
    heads = _heads(connection)
    topt, qqq = heads.get(TOPT), heads.get(QQQ)
    verdicts: list[SurfaceVerdict] = []

    row = connection.execute(_STRATEGY_HEAD_SQL).fetchone()
    verdicts.append(
        SurfaceVerdict(
            surface="/research/rankings, /strategy, /compare, /trace, /coverage",
            reader="mart.governed_strategy_run",
            expected_run=_run(topt),
            served_run=str(row[0]) if row else None,
            detail=f"strategy run {row[1]}" if row else "the view is empty: no strategy run at the head's cutoff",
            universe=TOPT,
        )
    )

    row = connection.execute(_THEMES_HEAD_SQL).fetchone()
    if row is None and not llm.is_configured():
        # No provider seated: the lane cannot judge a segment here, so it has nothing to serve
        # and nothing to serve wrongly. An unseated key where one belongs is the model-key
        # probe's red verdict (#876 W2), not this surface's.
        verdicts.append(
            SurfaceVerdict(
                surface=_THEMES_SURFACE,
                reader=_THEMES_READER,
                expected_run=_NOT_RUN_HERE,
                served_run=_NOT_RUN_HERE,
                detail="no model provider seated in this environment; the theme lane is off",
                universe=TOPT,
            )
        )
    else:
        verdicts.append(themes_verdict(connection, heads, str(row[0]) if row else None))

    row = connection.execute(_HOLDINGS_HEAD_SQL).fetchone()
    served = str(row[0]) if row else None
    counted = connection.execute(_HOLDINGS_ROWS_SQL, (served,)).fetchone() if served else None
    funds = int(counted[0]) if counted else 0
    if qqq is None and served is None:
        # An environment that never captures QQQ (staging today) has no holdings head to
        # serve and none to serve wrongly; the page renders its filed weights unvalued. Not
        # a mismatch — a surface with no run behind it in a place that runs no such tick.
        verdicts.append(
            SurfaceVerdict(
                surface="/research/holdings",
                reader="fund-valuation.ts QQQ_POINTER_HEAD_SQL",
                expected_run=_NOT_RUN_HERE,
                served_run=_NOT_RUN_HERE,
                detail="no QQQ head in this environment; the page renders filed weights unvalued",
                universe=QQQ,
            )
        )
    else:
        verdicts.append(
            SurfaceVerdict(
                surface="/research/holdings",
                reader="fund-valuation.ts QQQ_POINTER_HEAD_SQL",
                expected_run=_run(qqq),
                served_run=served,
                detail=f"{funds} consolidated fund row(s)",
                stale=served is not None and funds == 0,
                universe=QQQ,
            )
        )

    stored = {
        str(universe_id): (str(run_id), payload)
        for universe_id, run_id, payload in connection.execute(_COVERAGE_HEAD_SQL).fetchall()
    }
    for universe, prefix in UNIVERSE_PREFIXES.items():
        head = heads[universe]
        # The report for the head's OWN universe id when there is a head (two TOPT partitions
        # would share a prefix; review on #859); by prefix only to say "nothing stored" for a
        # universe with no head.
        if head is not None:
            match = stored.get(head.universe_id)
        else:
            match = next((entry for universe_id, entry in stored.items() if universe_id.startswith(prefix)), None)
        if head is None and match is None:
            verdicts.append(
                SurfaceVerdict(
                    surface=f"/admin/datahub coverage [{universe}]",
                    reader="datahub-stats.ts QUESTION_COVERAGE_SQL",
                    expected_run=_NOT_RUN_HERE,
                    served_run=_NOT_RUN_HERE,
                    detail="no head and no report for this universe in this environment",
                    universe=universe,
                )
            )
            continue
        fresh = compile_report(connection, universe=universe, executed_at=executed_at, environment=environment)
        drift = _drift(match[1] if match else None, fresh)
        verdicts.append(
            SurfaceVerdict(
                surface=f"/admin/datahub coverage [{universe}]",
                reader="datahub-stats.ts QUESTION_COVERAGE_SQL",
                expected_run=_run(head),
                served_run=match[0] if match else None,
                detail=drift or "recomputed report agrees",
                stale=bool(drift),
                universe=universe,
            )
        )
    pending = settling or {}
    return tuple(_settle(verdict, pending) for verdict in verdicts)


def _settle(verdict: SurfaceVerdict, pending: Mapping[str, str]) -> SurfaceVerdict:
    """A surface that does not match, of a universe that has not settled, is in progress."""
    why = pending.get(verdict.universe) if verdict.universe is not None else None
    return replace(verdict, in_progress=why) if why and not verdict.ok else verdict


def _run(head: GovernedHead | None) -> str | None:
    return head.run_id if head else None


def _exists(connection: Connection[Any], sql: str, params: tuple[Any, ...]) -> bool:
    row = connection.execute(sql, params).fetchone()
    return bool(row and row[0])


def themes_verdict(
    connection: Connection[Any], heads: Mapping[str, GovernedHead | None], served: str | None
) -> SurfaceVerdict:
    """/research/themes against the head its own reader picks once every universe's reports
    are current.

    `LATEST_RUN_SQL` has no universe filter: it serves the run with the newest cutoff in the
    table, and the head reports write purity rows for every universe's head. So the head it
    must serve is the newest-cutoff head among the universes the theme lane covers here (any
    run their pointer ever named has rows). In production that is the QQQ head, whose tick
    cuts off after TOPT's every day; expecting TOPT's head failed every nightly proof there
    (#910). Equal cutoffs leave the reader's order undefined, so either tied head is its head.

    The verdict names that head's universe, so its settling state decides IN-PROGRESS. When
    the newest head has no rows yet while an older run is served, the page changes when that
    universe's head reports land, whichever universe's run is served meanwhile; a quiet one in
    that state is a MISMATCH, because partitions only accumulate and a covered universe's new
    head should have rows again. A universe the lane has never covered cannot be selected, so
    its newer head is not expected. When no run any pointer named has rows, every head is a
    candidate and the newest is named, with what the plane under it holds.
    """
    environment = CaptureEnvironment.PRODUCTION.value
    present = {universe: head for universe, head in heads.items() if head is not None}
    covered = {
        universe: head
        for universe, head in present.items()
        if _exists(connection, _THEME_UNIVERSE_SQL, (environment, GOVERNING_FACTOR, UNIVERSE_PREFIXES[universe] + "%"))
    }
    candidates = covered or present
    if not candidates:
        return SurfaceVerdict(
            surface=_THEMES_SURFACE,
            reader=_THEMES_READER,
            expected_run=None,
            served_run=served,
            detail="" if served else _no_theme_rows(connection, None),
            universe=TOPT,
        )
    newest = max(head.cutoff for head in candidates.values())
    tied = sorted(universe for universe, head in candidates.items() if head.cutoff == newest)
    universe = next((name for name in tied if candidates[name].run_id == served), tied[0])
    head = candidates[universe]
    if served is None:
        detail = _no_theme_rows(connection, head)
    elif served == head.run_id:
        detail = f"the {universe} head, the newest of a universe with theme rows"
    else:
        notes: list[str] = []
        if not covered:
            notes.append("no run a governed pointer named has theme rows")
        elif not _exists(connection, _THEME_ROWS_SQL, (head.run_id,)):
            notes.append(f"the {universe} head, the newest of a universe with theme rows, has none")
        owner = next((name for name, other in present.items() if other.run_id == served), None)
        notes.append(
            f"the served run is the {owner} head" if owner else "the served run is no universe's governed head"
        )
        detail = "; ".join(notes)
    return SurfaceVerdict(
        surface=_THEMES_SURFACE,
        reader=_THEMES_READER,
        expected_run=head.run_id,
        served_run=served,
        detail=detail,
        universe=universe,
    )


def _no_theme_rows(connection: Connection[Any], head: GovernedHead | None) -> str:
    """The MISMATCH detail for an empty theme table: what the plane under it holds."""
    if head is None:
        return "no theme purity rows at all"
    row = connection.execute(_SEGMENT_PARTITIONS_SQL, (head.cutoff,)).fetchone()
    partitions = int(row[0]) if row else 0
    if partitions == 0:
        return (
            "no theme purity rows at all: no segment partition is knowable at the head's cutoff "
            "(the segment_revenue backfill has landed nothing here)"
        )
    return f"no theme purity rows at all, though {partitions} segment partition(s) are knowable at the head's cutoff"


def _drift(stored: Any, fresh: dict[str, Any] | None) -> str:
    """How the stored report differs from one compiled now over the same tables, per question."""
    if stored is None:
        return "no stored report"
    if fresh is None:
        return "no governed head to recompute against"
    stored_questions = (stored or {}).get("questions", {})
    changed = []
    for question, entry in fresh["questions"].items():
        before = stored_questions.get(question)
        if before is None:
            changed.append(f"{question}: absent from the stored report")
        elif (before.get("answered"), before.get("unavailable"), before.get("missing")) != (
            entry["answered"],
            entry["unavailable"],
            entry["missing"],
        ):
            changed.append(f"{question}: stored {before.get('answered')} answered, tables say {entry['answered']}")
    return "; ".join(changed)


def summary_lines(verdicts: Sequence[SurfaceVerdict]) -> list[str]:
    lines = [verdict.line for verdict in verdicts]
    lines.append(f"report surface proof: {verdict_counts(verdicts)}")
    return lines


def verdict_counts(verdicts: Sequence[SurfaceVerdict]) -> str:
    """`m/n surfaces serve the governed head`, then how many are in progress and how many do not."""
    matched = sum(1 for verdict in verdicts if verdict.ok)
    settling = sum(1 for verdict in verdicts if verdict.state == "IN-PROGRESS")
    failed = sum(1 for verdict in verdicts if verdict.mismatched)
    return (
        f"{matched}/{len(verdicts)} surfaces serve the governed head"
        + (f"; {settling} in progress" if settling else "")
        + (f"; {failed} do not" if failed else "")
    )
