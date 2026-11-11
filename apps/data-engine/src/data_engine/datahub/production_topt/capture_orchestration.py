"""TOPT capture orchestration across routed semantic source adapters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from truealpha_contracts.datahub import CaptureWorkItem
from truealpha_contracts.evidence_graph import EvidenceGraphWriter
from truealpha_contracts.obligation_reason_codes import ObligationReasonCode

from data_engine.datahub.production_topt.executor import (
    FailoverFetchPort,
    FetchFailure,
    FetchOutcome,
    FetchSuccess,
    ObligationSink,
    SourceFetchPort,
    ToptCaptureExecutor,
    ToptCaptureRunReport,
)


class CompositeSourceFetchPort:
    def __init__(self, routes: Mapping[str, SourceFetchPort]) -> None:
        self._routes = dict(routes)

    def fetch(self, work_item: CaptureWorkItem) -> FetchOutcome:
        adapter = self._routes.get(work_item.work_item_id)
        if adapter is None:
            return FetchFailure(ObligationReasonCode.CONTRACT_VIOLATION)
        return adapter.fetch(work_item)

    def failover(self, work_item: CaptureWorkItem, primary_reason: ObligationReasonCode) -> FetchSuccess | None:
        adapter = self._routes.get(work_item.work_item_id)
        if not isinstance(adapter, FailoverFetchPort):
            return None
        return adapter.failover(work_item, primary_reason)


def run_topt_capture(
    run_id: str,
    work_items: Sequence[CaptureWorkItem],
    routes: Mapping[str, SourceFetchPort],
    writer: EvidenceGraphWriter,
    *,
    sink: ObligationSink | None = None,
    cutoff: datetime,
    recorded_at: datetime,
    max_attempts: int = 3,
) -> ToptCaptureRunReport:
    port = CompositeSourceFetchPort(routes)
    executor = ToptCaptureExecutor(writer, sink=sink, max_attempts=max_attempts)
    return executor.run(run_id, work_items, port, cutoff=cutoff, recorded_at=recorded_at)
