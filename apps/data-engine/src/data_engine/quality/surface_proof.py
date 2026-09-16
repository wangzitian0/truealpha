"""Every report surface serves the governed head — proven against the environment's own
database, nightly (#855 C1).

The deploy's Playwright walk proves each route renders; nothing proved that the numbers on
it belong to the run the governed pointer names. Each App reader picks its run with its own
SQL, copied here verbatim so the proof asks exactly what the page asks, and the proof is
that every answer is the head: a page serving an older run than its neighbours contradicts
the App's own head with every gate green. The coverage report is held to more than its run:
it is recomputed from the same tables, and a stored report that no longer matches what the
tables say is stale even when its run id is right.

Not a fixture check. Like `quality.invariants`, this only means something against a database
real ticks wrote, so it runs as an op of the nightly quality job and its verdict is a red or
green Dagster run (init.md rule 9).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import Connection
from truealpha_contracts.common import CaptureEnvironment

from data_engine.datahub.question_coverage import UNIVERSE_PREFIXES, GovernedHead, compile_report, governed_head

#: `apps/app-web/src/server/mart/strategy-run-repository.ts` resolves the governed strategy
#: run through this view (`db/migrations/20260907T0630_bt_governed_strategy_run.sql`):
#: /research/rankings, /strategy, /compare, /trace and /coverage all read it.
_STRATEGY_HEAD_SQL = "select target_run_id, strategy_run_id from mart.governed_strategy_run"
#: `apps/app-web/src/server/mart/theme-purity.ts` `LATEST_RUN_SQL`: the newest run in the
#: table, deliberately not the pointer — which is exactly why it has to be checked against it.
_THEMES_HEAD_SQL = """
select run_id, max(cutoff) as cutoff
from mart.issuer_theme_purity
group by run_id
order by max(cutoff) desc
limit 1
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
#: A universe this environment never ticks: nothing to serve and nothing served.
_NOT_RUN_HERE = "not-run-in-this-environment"


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

    @property
    def ok(self) -> bool:
        return self.expected_run is not None and self.served_run == self.expected_run and not self.stale

    @property
    def line(self) -> str:
        verdict = "MATCH" if self.ok else "MISMATCH"
        served = self.served_run or "nothing"
        expected = self.expected_run or "no governed head"
        suffix = f" — {self.detail}" if self.detail else ""
        return f"{verdict} {self.surface}: serves {served[:24]} vs head {expected[:24]}{suffix}"


def prove(connection: Connection[Any], *, executed_at: datetime) -> tuple[SurfaceVerdict, ...]:
    """Every surface's served run against the governed head, plus the coverage report against
    its own recomputation. Read-only."""
    environment = CaptureEnvironment.PRODUCTION.value
    heads = {
        universe: governed_head(connection, universe_prefix=prefix, environment=environment)
        for universe, prefix in UNIVERSE_PREFIXES.items()
    }
    topt, qqq = heads.get("topt"), heads.get("universe-list:qqq")
    verdicts: list[SurfaceVerdict] = []

    row = connection.execute(_STRATEGY_HEAD_SQL).fetchone()
    verdicts.append(
        SurfaceVerdict(
            surface="/research/rankings, /strategy, /compare, /trace, /coverage",
            reader="mart.governed_strategy_run",
            expected_run=_run(topt),
            served_run=str(row[0]) if row else None,
            detail=f"strategy run {row[1]}" if row else "the view is empty: no strategy run at the head's cutoff",
        )
    )

    row = connection.execute(_THEMES_HEAD_SQL).fetchone()
    verdicts.append(
        SurfaceVerdict(
            surface="/research/themes",
            reader="theme-purity.ts LATEST_RUN_SQL",
            expected_run=_run(topt),
            served_run=str(row[0]) if row else None,
            detail="" if row else "no theme purity rows at all",
        )
    )

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
            )
        )

    stored = {
        str(universe_id): (str(run_id), payload)
        for universe_id, run_id, payload in connection.execute(_COVERAGE_HEAD_SQL).fetchall()
    }
    for universe, prefix in UNIVERSE_PREFIXES.items():
        head = heads[universe]
        match = next((entry for universe_id, entry in stored.items() if universe_id.startswith(prefix)), None)
        if head is None and match is None:
            verdicts.append(
                SurfaceVerdict(
                    surface=f"/admin/datahub coverage [{universe}]",
                    reader="datahub-stats.ts QUESTION_COVERAGE_SQL",
                    expected_run=_NOT_RUN_HERE,
                    served_run=_NOT_RUN_HERE,
                    detail="no head and no report for this universe in this environment",
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
            )
        )
    return tuple(verdicts)


def _run(head: GovernedHead | None) -> str | None:
    return head.run_id if head else None


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
    failed = sum(1 for verdict in verdicts if not verdict.ok)
    lines.append(
        f"report surface proof: {len(verdicts) - failed}/{len(verdicts)} surfaces serve the governed head"
        + (f"; {failed} do not" if failed else "")
    )
    return lines
