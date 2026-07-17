from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

from data_engine.datahub.production_topt.capture_orchestration import (
    CompositeSourceFetchPort,
    run_topt_capture,
)
from data_engine.datahub.production_topt.executor import FetchFailure, FetchSuccess
from data_engine.datahub.production_topt.release_derived_adapter import (
    ReleaseDerivedAdapter,
    ReleaseDerivedRecord,
)
from data_engine.datahub.production_topt.yahoo_market_price_adapter import (
    MarketPriceQuote,
    MarketPriceTarget,
    YahooMarketPriceAdapter,
)
from truealpha_contracts import EvidenceEdge, EvidenceNode, ObligationReasonCode
from truealpha_contracts.datahub import CaptureWorkItem, ObligationTerminalState

_CUTOFF_D = date(2026, 3, 31)
_CUTOFF = datetime(2026, 4, 1, tzinfo=UTC)
_RECORDED = datetime(2026, 4, 1, 12, tzinfo=UTC)
_KNOWN = datetime(2026, 3, 1, tzinfo=UTC)


def _work_item(digest: str) -> CaptureWorkItem:
    return CaptureWorkItem(
        campaign_id="capture-campaign:" + "1" * 64,
        source_request_id="source-request:" + digest,
        schedule_policy_id="schedule-policy:" + "2" * 64,
    )


class _FakeWriter:
    def __init__(self) -> None:
        self.nodes: list[EvidenceNode] = []
        self.edges: list[EvidenceEdge] = []

    def append(self, nodes: Sequence[EvidenceNode], edges: Sequence[EvidenceEdge]) -> None:
        self.nodes.extend(nodes)
        self.edges.extend(edges)


def test_composite_routes_and_fails_closed_on_unmapped() -> None:
    known = _work_item("3" * 64)
    unmapped = _work_item("4" * 64)
    price = YahooMarketPriceAdapter(
        {known.work_item_id: MarketPriceTarget("GOOG", _CUTOFF_D)},
        lambda s, c: MarketPriceQuote(b"raw", Decimal("150.0"), date(2026, 3, 31), _KNOWN),
    )
    port = CompositeSourceFetchPort({known.work_item_id: price})
    assert isinstance(port.fetch(known), FetchSuccess)
    miss = port.fetch(unmapped)
    assert isinstance(miss, FetchFailure)
    assert miss.reason_code is ObligationReasonCode.CONTRACT_VIOLATION


def test_run_topt_capture_across_two_semantics() -> None:
    price_item = _work_item("5" * 64)
    member_item = _work_item("6" * 64)
    price = YahooMarketPriceAdapter(
        {price_item.work_item_id: MarketPriceTarget("GOOG", _CUTOFF_D)},
        lambda s, c: MarketPriceQuote(b"raw", Decimal("150.0"), date(2026, 3, 31), _KNOWN),
    )
    membership = ReleaseDerivedAdapter(
        {
            member_item.work_item_id: ReleaseDerivedRecord(
                "universe-membership", "listing:goog", {"member": True}, _KNOWN
            )
        },
        cutoff=_CUTOFF_D,
    )
    routes = {price_item.work_item_id: price, member_item.work_item_id: membership}
    writer = _FakeWriter()
    report = run_topt_capture(
        "capture-run:" + "a" * 64,
        [price_item, member_item],
        routes,
        writer,
        cutoff=_CUTOFF,
        recorded_at=_RECORDED,
    )
    assert report.available == 2
    assert all(o.terminal_state is ObligationTerminalState.SUCCESS for o in report.outcomes)
    # capture_run once + two raw + two normalized nodes.
    kinds = sorted(n.ref.kind.value for n in writer.nodes)
    assert kinds == [
        "capture_run",
        "normalized_observation",
        "normalized_observation",
        "raw_fetch",
        "raw_fetch",
    ]
