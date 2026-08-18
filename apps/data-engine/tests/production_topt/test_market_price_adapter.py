from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

from data_engine.datahub.production_topt.executor import (
    FetchFailure,
    FetchSuccess,
    ToptCaptureExecutor,
)
from data_engine.datahub.production_topt.market_price_adapter import (
    MarketPriceAdapter,
    MarketPriceQuote,
    MarketPriceTarget,
    SourceUnavailableError,
)
from truealpha_contracts import EvidenceEdge, EvidenceNode, ObligationReasonCode
from truealpha_contracts.datahub import CaptureWorkItem, ObligationTerminalState

_CUTOFF = date(2026, 3, 31)


def _work_item(digest: str) -> CaptureWorkItem:
    return CaptureWorkItem(
        campaign_id="capture-campaign:" + "1" * 64,
        source_request_id="source-request:" + digest,
        schedule_policy_id="schedule-policy:" + "2" * 64,
    )


def _quote(day: date, close: str, knowable: datetime | None = None) -> MarketPriceQuote:
    return MarketPriceQuote(
        raw_bytes=f"GOOG:{day}:{close}".encode(),
        close=Decimal(close),
        as_of=day,
        knowable_at=knowable or datetime.combine(day, datetime.min.time(), tzinfo=UTC),
    )


def _adapter(item: CaptureWorkItem, fetcher) -> MarketPriceAdapter:
    return MarketPriceAdapter(
        {
            item.work_item_id: MarketPriceTarget(
                "GOOG", _CUTOFF, "issuer:lei:X", "security:cusip:Y", "listing:xnas:goog"
            )
        },
        fetcher,
    )


def test_success_is_decimal_and_deterministic() -> None:
    item = _work_item("3" * 64)
    adapter = _adapter(item, lambda symbol, cutoff: _quote(date(2026, 3, 31), "150.25"))
    result = adapter.fetch(item)
    assert isinstance(result, FetchSuccess)
    assert result.confidence == Decimal("0.85")
    # Same inputs → identical normalized identity.
    assert adapter.fetch(item).normalized_sha256 == result.normalized_sha256


def test_unknown_work_item_is_contract_violation() -> None:
    item = _work_item("4" * 64)
    other = _work_item("5" * 64)
    adapter = _adapter(item, lambda symbol, cutoff: _quote(_CUTOFF, "1.0"))
    result = adapter.fetch(other)
    assert isinstance(result, FetchFailure)
    assert result.reason_code is ObligationReasonCode.CONTRACT_VIOLATION


def test_transient_and_timeout_and_unavailable() -> None:
    item = _work_item("6" * 64)

    def _boom(symbol, cutoff):
        raise SourceUnavailableError("502")

    def _timeout(symbol, cutoff):
        raise TimeoutError

    assert _adapter(item, _boom).fetch(item).reason_code is ObligationReasonCode.TRANSIENT_NETWORK
    assert _adapter(item, _timeout).fetch(item).reason_code is ObligationReasonCode.TIMEOUT
    assert _adapter(item, lambda s, c: None).fetch(item).reason_code is ObligationReasonCode.FIELD_UNAVAILABLE


def test_look_ahead_is_rejected() -> None:
    item = _work_item("7" * 64)
    late = datetime(2026, 4, 5, tzinfo=UTC)  # knowable after the cutoff
    adapter = _adapter(item, lambda s, c: _quote(date(2026, 3, 31), "10.0", knowable=late))
    result = adapter.fetch(item)
    assert isinstance(result, FetchFailure)
    assert result.reason_code is ObligationReasonCode.LOOK_AHEAD_VIOLATION


class _FakeWriter:
    def __init__(self) -> None:
        self.nodes: list[EvidenceNode] = []
        self.edges: list[EvidenceEdge] = []

    def append(self, nodes: Sequence[EvidenceNode], edges: Sequence[EvidenceEdge]) -> None:
        self.nodes.extend(nodes)
        self.edges.extend(edges)


def test_end_to_end_through_executor() -> None:
    item = _work_item("8" * 64)
    adapter = _adapter(item, lambda s, c: _quote(date(2026, 3, 31), "99.99"))
    writer = _FakeWriter()
    report = ToptCaptureExecutor(writer).run(
        "capture-run:" + "a" * 64,
        [item],
        adapter,
        cutoff=datetime(2026, 4, 1, tzinfo=UTC),
        recorded_at=datetime(2026, 4, 1, 12, tzinfo=UTC),
    )
    assert report.available == 1
    assert report.outcomes[0].terminal_state is ObligationTerminalState.SUCCESS
    assert {n.ref.kind.value for n in writer.nodes} == {
        "capture_run",
        "raw_fetch",
        "normalized_observation",
    }


def test_last_settled_session_date_excludes_the_running_session() -> None:
    """#637: a daily close is knowable from 16:00 America/New_York, never during
    the session — the 07:51 ET staging smoke asserted 21 in-progress bars as
    closes and every cell honestly degraded to single-origin."""
    from datetime import UTC, datetime

    from data_engine.datahub.production_topt.market_price_adapter import last_settled_session_date

    pre_market = datetime(2026, 8, 18, 11, 51, tzinfo=UTC)  # 07:51 ET Tuesday
    assert last_settled_session_date(pre_market).isoformat() == "2026-08-17"

    mid_session = datetime(2026, 8, 18, 18, 30, tzinfo=UTC)  # 14:30 ET Tuesday
    assert last_settled_session_date(mid_session).isoformat() == "2026-08-17"

    after_close = datetime(2026, 8, 18, 22, 15, tzinfo=UTC)  # 18:15 ET Tuesday — the prod tick slot
    assert last_settled_session_date(after_close).isoformat() == "2026-08-18"

    at_the_bell = datetime(2026, 8, 18, 20, 0, tzinfo=UTC)  # 16:00 ET exactly
    assert last_settled_session_date(at_the_bell).isoformat() == "2026-08-18"

    # Winter (EST): 22:15 UTC is 17:15 ET — still after the close.
    winter_tick = datetime(2026, 1, 13, 22, 15, tzinfo=UTC)
    assert last_settled_session_date(winter_tick).isoformat() == "2026-01-13"

    # A Saturday run settles to Friday-or-earlier via the fetcher's `<=` pick; the
    # helper itself answers Saturday after any Friday close has long existed.
    saturday_morning = datetime(2026, 8, 22, 11, 0, tzinfo=UTC)  # 07:00 ET Saturday
    assert last_settled_session_date(saturday_morning).isoformat() == "2026-08-21"


def test_last_settled_session_date_never_returns_a_weekend() -> None:
    from datetime import UTC, datetime

    from data_engine.datahub.production_topt.market_price_adapter import last_settled_session_date

    saturday_evening = datetime(2026, 8, 22, 22, 0, tzinfo=UTC)  # 18:00 ET Saturday
    assert last_settled_session_date(saturday_evening).isoformat() == "2026-08-21"
    sunday_evening = datetime(2026, 8, 23, 22, 0, tzinfo=UTC)  # 18:00 ET Sunday
    assert last_settled_session_date(sunday_evening).isoformat() == "2026-08-21"
    monday_pre_market = datetime(2026, 8, 24, 11, 0, tzinfo=UTC)  # 07:00 ET Monday
    assert last_settled_session_date(monday_pre_market).isoformat() == "2026-08-21"
