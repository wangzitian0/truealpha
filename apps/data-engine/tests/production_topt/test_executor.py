from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

from data_engine.datahub.production_topt.executor import (
    FetchFailure,
    FetchOutcome,
    FetchSuccess,
    ToptCaptureExecutor,
)
from truealpha_contracts import EvidenceEdge, EvidenceNode, ObligationReasonCode
from truealpha_contracts.datahub import CaptureWorkItem, ObligationTerminalState

_RUN = "capture-run:" + "a" * 64
_CUTOFF = datetime(2026, 4, 1, tzinfo=UTC)
_RECORDED = datetime(2026, 4, 1, 12, tzinfo=UTC)


class _FakeWriter:
    def __init__(self) -> None:
        self.nodes: list[EvidenceNode] = []
        self.edges: list[EvidenceEdge] = []

    def append(self, nodes: Sequence[EvidenceNode], edges: Sequence[EvidenceEdge]) -> None:
        self.nodes.extend(nodes)
        self.edges.extend(edges)


class _ScriptedFetch:
    def __init__(self, script: dict[str, list[FetchOutcome]]) -> None:
        self._script = script

    def fetch(self, work_item: CaptureWorkItem) -> FetchOutcome:
        return self._script[work_item.work_item_id].pop(0)


def _work_item(digest: str) -> CaptureWorkItem:
    return CaptureWorkItem(
        campaign_id="capture-campaign:" + "1" * 64,
        source_request_id="source-request:" + digest,
        schedule_policy_id="schedule-policy:" + "2" * 64,
    )


def _success(digest: str) -> FetchSuccess:
    return FetchSuccess(
        raw_sha256="a" + digest[1:],
        object_uri="s3://raw/obj",
        normalized_sha256="b" + digest[1:],
        confidence=Decimal("0.9"),
        valid_from=date(2026, 3, 31),
        transaction_time=datetime(2026, 3, 31, tzinfo=UTC),
    )


def _run(script: dict[str, list[FetchOutcome]], items: list[CaptureWorkItem], *, max_attempts: int = 3):
    writer = _FakeWriter()
    executor = ToptCaptureExecutor(writer, max_attempts=max_attempts)
    report = executor.run(_RUN, items, _ScriptedFetch(script), cutoff=_CUTOFF, recorded_at=_RECORDED)
    return report, writer


def test_success_writes_evidence_and_reports_available() -> None:
    item = _work_item("3" * 64)
    report, writer = _run({item.work_item_id: [_success("3" * 64)]}, [item])
    assert not report.halted and report.available == 1 and report.total == 1
    # capture_run + raw_fetch + normalized_observation nodes, and derived_from + member_of edges.
    kinds = sorted(n.ref.kind.value for n in writer.nodes)
    assert kinds == ["capture_run", "normalized_observation", "raw_fetch"]
    assert {e.relation.value for e in writer.edges} == {"derived_from", "member_of"}


def test_retry_then_success() -> None:
    item = _work_item("4" * 64)
    script = {item.work_item_id: [FetchFailure(ObligationReasonCode.TIMEOUT), _success("4" * 64)]}
    report, _ = _run(script, [item])
    assert report.available == 1
    assert report.outcomes[0].attempts == 2


def test_retry_exhausted_is_unavailable() -> None:
    item = _work_item("5" * 64)
    script = {item.work_item_id: [FetchFailure(ObligationReasonCode.RATE_LIMITED)] * 3}
    report, _ = _run(script, [item], max_attempts=3)
    assert report.unavailable == 1
    assert report.outcomes[0].terminal_state is ObligationTerminalState.UNAVAILABLE
    assert report.outcomes[0].attempts == 3


def test_trace_only_records_and_continues() -> None:
    first = _work_item("6" * 64)
    second = _work_item("7" * 64)
    script = {
        first.work_item_id: [FetchFailure(ObligationReasonCode.FIELD_UNAVAILABLE)],
        second.work_item_id: [_success("7" * 64)],
    }
    report, _ = _run(script, [first, second])
    assert not report.halted
    assert report.unavailable == 1 and report.available == 1


def test_stop_halts_the_run() -> None:
    first = _work_item("8" * 64)
    second = _work_item("9" * 64)
    script = {
        first.work_item_id: [FetchFailure(ObligationReasonCode.CHECKSUM_MISMATCH)],
        second.work_item_id: [_success("9" * 64)],
    }
    report, _ = _run(script, [first, second])
    assert report.halted
    assert report.halt_reason is ObligationReasonCode.CHECKSUM_MISMATCH
    assert report.failed == 1
    # The run stopped before the second obligation.
    assert report.total == 1
