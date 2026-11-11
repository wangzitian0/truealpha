"""Month-end multi-cutoff history runner for standard backfills (#1137)."""

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

MAX_HISTORY_MONTHS = 120
FAILED_STATUSES = ("error", "deferred_capacity")


class HistoryFailure(RuntimeError):
    """Raised when at least one open cell was attempted and every attempt failed."""


def month_end_cutoffs(tick: datetime, months: int) -> list[datetime]:
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
