"""Month-end history for a standard's backfill (#1137).

The backtest reads a fact with `knowable_at <= cutoff`. The weekly backfill runs at one
cutoff, the tick. A cutoff before the first extracted filing finds no fact, so the issuer
drops out of the early months.

The history run calls the same backfill (`backfill.run_standard_backfill`) at each month-end
cutoff of the last N months, oldest first. The cutoffs come from the tick, never from a
literal date. Each call has the planner, the extractor and the model replay of the weekly run:

- The extractor reads the newest annual filing filed at or before the cutoff. It stamps
  `knowable_at` with the filing date. A row therefore never lands after the cutoff that wrote it.
- The planner closes a cell that holds a fact which is not stale. A filled history costs no
  vendor call, except at cutoffs that precede the issuer's first filing.
- The model is asked once per (issuer, filing). Another cutoff that reads the same filing
  replays the stored answer. This holds for a declined answer too.

A cell that never lands (a decline, a filing without a candidate) stays open at every later
cutoff. Each visit costs two SEC calls. The model is not asked again.

The run reports counts only. Every count is per cell visit.
"""

from __future__ import annotations

import calendar
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from truealpha_contracts.ports import RawObjectStore

from data_engine.datahub.standards.backfill import (
    HEALTH_LOG_SOURCE,
    BackfillReport,
    resolve_issuers,
    run_standard_backfill,
)
from data_engine.sources.gateway import SourceGateway

#: Ten years. A larger count in a manual launch is a typing error that would spend the SEC budget.
MAX_HISTORY_MONTHS = 120

#: Outcomes that mean the cell could not be tried. Every other outcome is an answer.
FAILED_STATUSES = ("error", "deferred_capacity")


class HistoryFailure(RuntimeError):
    """Every open cell of a history run failed. The text holds counts only."""


def month_end_cutoffs(tick: datetime, months: int) -> list[datetime]:
    """The last `months` month-end cutoffs at or before `tick`, oldest first.

    A cutoff is 23:59:59 UTC on the last day of the month. The month-end of the tick's own
    month counts only when it is not after the tick.
    """
    if tick.tzinfo is None or tick.utcoffset() is None:
        raise ValueError("tick must be timezone-aware")
    if not 1 <= months <= MAX_HISTORY_MONTHS:
        raise ValueError(f"months must be between 1 and {MAX_HISTORY_MONTHS}, got {months}")
    tick = tick.astimezone(UTC)
    year, month = tick.year, tick.month
    cutoffs: list[datetime] = []
    while len(cutoffs) < months:
        cutoff = datetime(year, month, calendar.monthrange(year, month)[1], 23, 59, 59, tzinfo=UTC)
        if cutoff <= tick:
            cutoffs.append(cutoff)
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    cutoffs.reverse()
    return cutoffs


@dataclass(frozen=True)
class CutoffResult:
    cutoff: datetime
    report: BackfillReport


@dataclass
class HistoryReport:
    universe: str
    standard: str
    tick: datetime
    cutoffs: list[CutoffResult] = field(default_factory=list)

    def _cells(self) -> Iterator[dict[str, Any]]:
        for result in self.cutoffs:
            yield from result.report.cells

    def counts(self) -> dict[str, int]:
        """The run in counts. A cell visit is one open cell at one cutoff.

        `extractions_asked` counts visits where the provider answered a new question.
        `replays` counts visits that reused a stored answer.
        `declines` counts visits that ended with a model refusal.
        `rows_written` counts the facts the run landed.
        """
        cells = list(self._cells())
        return {
            "cutoffs_visited": len(self.cutoffs),
            "cells_attempted": len(cells),
            "cells_failed": sum(1 for cell in cells if cell["status"] in FAILED_STATUSES),
            "extractions_asked": sum(1 for cell in cells if cell["model_replayed"] is False),
            "replays": sum(1 for cell in cells if cell["model_replayed"] is True),
            "declines": sum(1 for cell in cells if cell["status"] == "model_declined"),
            "rows_written": sum(1 for cell in cells if cell["status"] == "resolved" and cell["fact_id"] is not None),
        }


def raise_if_every_attempt_failed(report: HistoryReport) -> None:
    """Fail the run when it tried at least one cell and every try failed.

    A vendor outage or a spent SEC budget would otherwise end as a green run with zero rows.
    A run with no open cell is a filled history, not a failure.
    """
    counts = report.counts()
    attempted, failed = counts["cells_attempted"], counts["cells_failed"]
    if attempted and failed == attempted:
        raise HistoryFailure(
            f"every attempt failed: {failed} of {attempted} open cells over "
            f"{counts['cutoffs_visited']} cutoffs ended in an error or a capacity refusal"
        )


def run_standard_history(
    connection: Any,
    *,
    universe: str,
    standard_name: str,
    tick: datetime,
    months: int,
    http: Any = None,
    gateway: SourceGateway | None = None,
    store: RawObjectStore | None = None,
    log: Callable[[str], None] = print,
) -> HistoryReport:
    """Run the backfill of one standard over one universe at each month-end cutoff.

    One gateway serves every cutoff, so the SEC rate window and the daily budget carry over.
    The issuers resolve once. Each cell commits on its own, as in the weekly run: a run that
    dies keeps what it landed, and the next run finds only the cells that remain open.
    """
    cutoffs = month_end_cutoffs(tick, months)
    gateway = gateway or SourceGateway(connection, caller=f"{HEALTH_LOG_SOURCE}:{universe}:{standard_name}:history")
    issuers = resolve_issuers(connection, universe, gateway=gateway, http=http)
    report = HistoryReport(universe=universe, standard=standard_name, tick=tick)
    for cutoff in cutoffs:
        backfill = run_standard_backfill(
            connection,
            universe=universe,
            standard_name=standard_name,
            cutoff=cutoff,
            mode="backfill",
            http=http,
            gateway=gateway,
            store=store,
            log=log,
            issuers=issuers,
            record_summary=False,
        )
        report.cutoffs.append(CutoffResult(cutoff=cutoff, report=backfill))
    log(f"standard history done: {report.counts()}")
    return report
