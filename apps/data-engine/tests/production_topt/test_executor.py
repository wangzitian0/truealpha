from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

from data_engine.datahub.production_topt.executor import (
    FetchFailure,
    FetchOutcome,
    FetchSuccess,
    NormalizedRecord,
    RawResponse,
    ToptCaptureExecutor,
)
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.datahub import CaptureWorkItem, ObligationTerminalState
from truealpha_contracts.evidence_graph import EvidenceEdge, EvidenceNode
from truealpha_contracts.models import DataSource
from truealpha_contracts.obligation_reason_codes import ObligationReasonCode

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
        raw=RawResponse(body=f"body:{digest}".encode(), source=DataSource.SEC, record_id=f"rec:{digest}"),
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


# -- failover (#862) ------------------------------------------------------------------------


def _failover_success(digest: str, *, origin: str | None = "twelve-data") -> FetchSuccess:
    payload = {"close": "1.5", **({} if origin is None else {"served_by_failover": origin})}
    return FetchSuccess(
        raw=RawResponse(body=f"failover:{digest}".encode(), source=DataSource.TWELVE_DATA, record_id=f"fo:{digest}"),
        normalized_sha256=canonical_sha256(payload),
        confidence=Decimal("0.75"),
        valid_from=date(2026, 3, 31),
        transaction_time=datetime(2026, 3, 31, tzinfo=UTC),
        record=NormalizedRecord(payload=payload, parser_version="origin-parser:v1", mapping_version="origin-map:v1"),
        served_by_failover=origin,
    )


class _FailoverFetch(_ScriptedFetch):
    """A port whose source declares further origins: `failover` answers once the
    primary could not serve, and records what it was asked."""

    def __init__(self, script: dict[str, list[FetchOutcome]], served: dict[str, FetchSuccess | None]) -> None:
        super().__init__(script)
        self._served = served
        self.asked: list[tuple[str, ObligationReasonCode]] = []

    def failover(self, work_item: CaptureWorkItem, primary_reason: ObligationReasonCode) -> FetchSuccess | None:
        self.asked.append((work_item.work_item_id, primary_reason))
        return self._served.get(work_item.work_item_id)


class _RecordingSink:
    def __init__(self) -> None:
        self.outcomes: list[tuple[str, tuple, ObligationTerminalState, FetchSuccess | None]] = []

    def record_outcome(self, work_item, *, attempt_reasons, terminal_state, success) -> None:
        self.outcomes.append((work_item.work_item_id, tuple(attempt_reasons), terminal_state, success))


def _run_failover(fetch: _FailoverFetch, items: list[CaptureWorkItem]):
    writer, sink = _FakeWriter(), _RecordingSink()
    report = ToptCaptureExecutor(writer, sink=sink, max_attempts=3).run(
        _RUN, items, fetch, cutoff=_CUTOFF, recorded_at=_RECORDED
    )
    return report, writer, sink


def test_an_exhausted_primary_is_served_by_failover_and_the_ledger_keeps_its_failure() -> None:
    """#862: the primary's retries run first (a transient blip is still the primary's to
    win); only once they are exhausted is the source asked for a failover. The obligation
    resolves SUCCESS, its attempts still carry the primary's failure on every attempt —
    the served origin never erases why the primary did not serve — and the failover's
    raw and observation reach the evidence graph like any success."""
    item = _work_item("a" * 64)
    served = _failover_success("a" * 64)
    fetch = _FailoverFetch(
        {item.work_item_id: [FetchFailure(ObligationReasonCode.TRANSIENT_NETWORK)] * 3},
        {item.work_item_id: served},
    )
    report, writer, sink = _run_failover(fetch, [item])

    assert fetch.asked == [(item.work_item_id, ObligationReasonCode.TRANSIENT_NETWORK)]
    [outcome] = report.outcomes
    assert outcome.terminal_state is ObligationTerminalState.SUCCESS
    assert outcome.reason_code is ObligationReasonCode.TRANSIENT_NETWORK
    assert outcome.attempts == 3
    assert outcome.served_by_failover == "twelve-data"
    assert (report.available, report.unavailable, report.served_by_failover) == (1, 0, 1)
    assert sink.outcomes == [
        (item.work_item_id, (ObligationReasonCode.TRANSIENT_NETWORK,) * 3, ObligationTerminalState.SUCCESS, served)
    ]
    assert f"raw-fetch:{served.raw_sha256}" in {node.ref.node_id for node in writer.nodes}


def test_a_trace_only_primary_failure_fails_over_at_once() -> None:
    """FIELD_UNAVAILABLE is not retried, so the failover is asked after the one attempt."""
    item = _work_item("b" * 64)
    served = _failover_success("b" * 64, origin="moomoo-kline")
    fetch = _FailoverFetch(
        {item.work_item_id: [FetchFailure(ObligationReasonCode.FIELD_UNAVAILABLE)]},
        {item.work_item_id: served},
    )
    report, _, sink = _run_failover(fetch, [item])
    assert fetch.asked == [(item.work_item_id, ObligationReasonCode.FIELD_UNAVAILABLE)]
    assert report.outcomes[0].attempts == 1 and report.outcomes[0].served_by_failover == "moomoo-kline"
    assert sink.outcomes[0][1:] == (
        (ObligationReasonCode.FIELD_UNAVAILABLE,),
        ObligationTerminalState.SUCCESS,
        served,
    )


def test_no_failover_leaves_the_failure_exactly_as_it_was() -> None:
    """Every origin failing is today's behaviour, attempt for attempt: UNAVAILABLE, the
    primary's reason, no success handed to the sink."""
    item = _work_item("c" * 64)
    fetch = _FailoverFetch({item.work_item_id: [FetchFailure(ObligationReasonCode.TIMEOUT)] * 3}, {})
    report, writer, sink = _run_failover(fetch, [item])
    [outcome] = report.outcomes
    assert (outcome.terminal_state, outcome.reason_code, outcome.attempts, outcome.served_by_failover) == (
        ObligationTerminalState.UNAVAILABLE,
        ObligationReasonCode.TIMEOUT,
        3,
        None,
    )
    assert sink.outcomes == [
        (item.work_item_id, (ObligationReasonCode.TIMEOUT,) * 3, ObligationTerminalState.UNAVAILABLE, None)
    ]
    assert [node.ref.kind.value for node in writer.nodes] == ["capture_run"]
    assert report.served_by_failover == 0


def test_a_stop_is_never_papered_over_by_a_failover() -> None:
    """A look-ahead or contract violation halts the run; no other origin is asked."""
    item = _work_item("d" * 64)
    fetch = _FailoverFetch(
        {item.work_item_id: [FetchFailure(ObligationReasonCode.LOOK_AHEAD_VIOLATION)]},
        {item.work_item_id: _failover_success("d" * 64)},
    )
    report, _, _ = _run_failover(fetch, [item])
    assert report.halted and report.failed == 1
    assert fetch.asked == []


def test_a_primary_success_never_asks_for_a_failover() -> None:
    item = _work_item("e" * 64)
    fetch = _FailoverFetch({item.work_item_id: [_success("e" * 64)]}, {item.work_item_id: _failover_success("e" * 64)})
    report, _, _ = _run_failover(fetch, [item])
    assert report.available == 1 and report.served_by_failover == 0
    assert fetch.asked == []


def test_a_failover_must_name_the_origin_that_served() -> None:
    """An unmarked substitute would be a silent one: the executor refuses it."""
    import pytest

    item = _work_item("f" * 64)
    fetch = _FailoverFetch(
        {item.work_item_id: [FetchFailure(ObligationReasonCode.FIELD_UNAVAILABLE)]},
        {item.work_item_id: _failover_success("f" * 64, origin=None)},
    )
    with pytest.raises(ValueError, match="name the origin"):
        _run_failover(fetch, [item])


def test_a_failover_success_must_declare_its_origin_in_the_payload() -> None:
    """The marker on the dataclass and the marker in the payload are one claim: the payload
    is what the snapshot binds, so a success that says "failover" only in memory is refused."""
    import pytest

    declared = _failover_success("7" * 64)
    with pytest.raises(ValueError, match="declare its serving origin"):
        FetchSuccess(
            raw=declared.raw,
            normalized_sha256=declared.normalized_sha256,
            confidence=declared.confidence,
            valid_from=declared.valid_from,
            transaction_time=declared.transaction_time,
            record=declared.record,
            served_by_failover="moomoo-kline",
        )
    with pytest.raises(ValueError, match="name the origin"):
        FetchSuccess(
            raw=declared.raw,
            normalized_sha256=declared.normalized_sha256,
            confidence=declared.confidence,
            valid_from=declared.valid_from,
            transaction_time=declared.transaction_time,
            record=declared.record,
            served_by_failover="",
        )


def test_a_routed_port_forwards_the_failover_to_the_owning_adapter() -> None:
    """The deployed executor sees the composite router, never the adapter: the router
    must forward `failover` to the adapter that owns the work item, and answer None for
    an adapter that declares no further origins."""
    from data_engine.datahub.production_topt.capture_orchestration import CompositeSourceFetchPort

    owned, plain = _work_item("1" * 64), _work_item("2" * 64)
    served = _failover_success("1" * 64)
    with_failover = _FailoverFetch({}, {owned.work_item_id: served})
    router = CompositeSourceFetchPort({owned.work_item_id: with_failover, plain.work_item_id: _ScriptedFetch({})})
    assert router.failover(owned, ObligationReasonCode.TIMEOUT) is served
    assert with_failover.asked == [(owned.work_item_id, ObligationReasonCode.TIMEOUT)]
    assert router.failover(plain, ObligationReasonCode.TIMEOUT) is None
    assert router.failover(_work_item("9" * 64), ObligationReasonCode.TIMEOUT) is None
