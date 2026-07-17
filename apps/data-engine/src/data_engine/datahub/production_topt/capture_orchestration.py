"""TOPT capture orchestration (Phase 3g, ADR A1 / #171).

Wires the executor to the per-semantic source adapters. A `CompositeSourceFetchPort` routes
each work item to the adapter that owns its semantic, and `run_topt_capture` runs the executor
over the planned work items, writing the append-only evidence graph and returning the run
report. The routing table is built from the plan by the caller (each adapter already holds its
own per-work-item targets).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from truealpha_contracts import EvidenceGraphWriter, ObligationReasonCode
from truealpha_contracts.datahub import CaptureWorkItem

from data_engine.datahub.production_topt.executor import (
    FetchFailure,
    FetchOutcome,
    SourceFetchPort,
    ToptCaptureExecutor,
    ToptCaptureRunReport,
)


class CompositeSourceFetchPort:
    """Routes each work item to the adapter that owns it; unmapped items fail closed."""

    def __init__(self, routes: Mapping[str, SourceFetchPort]) -> None:
        self._routes = dict(routes)

    def fetch(self, work_item: CaptureWorkItem) -> FetchOutcome:
        adapter = self._routes.get(work_item.work_item_id)
        if adapter is None:
            return FetchFailure(ObligationReasonCode.CONTRACT_VIOLATION)
        return adapter.fetch(work_item)


def run_topt_capture(
    run_id: str,
    work_items: Sequence[CaptureWorkItem],
    routes: Mapping[str, SourceFetchPort],
    writer: EvidenceGraphWriter,
    *,
    cutoff: datetime,
    recorded_at: datetime,
    max_attempts: int = 3,
) -> ToptCaptureRunReport:
    """Execute the planned TOPT capture through the routed adapters and the evidence writer."""
    port = CompositeSourceFetchPort(routes)
    executor = ToptCaptureExecutor(writer, max_attempts=max_attempts)
    return executor.run(run_id, work_items, port, cutoff=cutoff, recorded_at=recorded_at)
