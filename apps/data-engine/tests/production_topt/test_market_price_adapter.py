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
from truealpha_contracts.datahub import CaptureWorkItem, ObligationTerminalState
from truealpha_contracts.evidence_graph import EvidenceEdge, EvidenceNode
from truealpha_contracts.obligation_reason_codes import ObligationReasonCode

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


def test_price_confidence_grades_by_session_lag() -> None:
    """#641 D6: price confidence is a grade, not a constant — a bar that lags
    the settled session says so."""
    from datetime import date as _date
    from decimal import Decimal as _D

    from data_engine.datahub.production_topt.market_price_adapter import graded_price_confidence

    monday, tuesday = _date(2026, 8, 17), _date(2026, 8, 18)
    assert graded_price_confidence(as_of=tuesday, expected_session=tuesday) == _D("0.85")
    assert graded_price_confidence(as_of=monday, expected_session=tuesday) == _D("0.75")
    # Friday bar against Monday's settled session: the weekend is not a lag.
    friday, next_monday = _date(2026, 8, 14), _date(2026, 8, 17)
    assert graded_price_confidence(as_of=friday, expected_session=next_monday) == _D("0.75")
    # Deep staleness floors at 0.50 rather than going negative.
    assert graded_price_confidence(as_of=_date(2026, 7, 1), expected_session=tuesday) == _D("0.50")


def test_success_payload_carries_the_full_ohlcv_bar_from_both_origins() -> None:
    """Only `close` reached the payload until now, so the confidence report could grade
    exactly one price metric HIGH. Both vendors send the whole daily bar; the primary AND
    the corroborating assertion must carry open/high/low/volume as base-10 strings so the
    fusion engine can reconcile each field on its own."""
    from data_engine.datahub.production_topt.market_price_adapter import CorroboratingOrigin

    item = _work_item("9" * 64)
    bar = MarketPriceQuote(
        raw_bytes=b"GOOG:2026-03-31:bar",
        close=Decimal("150.25"),
        as_of=date(2026, 3, 31),
        knowable_at=datetime(2026, 3, 31, tzinfo=UTC),
        open=Decimal("149.10"),
        high=Decimal("151.00"),
        low=Decimal("148.75"),
        volume=Decimal("28186700"),
    )
    second = CorroboratingOrigin(
        origin="twelve-data",
        parser_version="twelve-data-parser:v3",
        mapping_version="twelve-data-map:v3",
        value_key="close",
        confidence=Decimal("0.85"),
        fetch=lambda symbol, cutoff: bar,
    )
    adapter = MarketPriceAdapter(
        {
            item.work_item_id: MarketPriceTarget(
                "GOOG", _CUTOFF, "issuer:lei:X", "security:cusip:Y", "listing:xnas:goog"
            )
        },
        lambda symbol, cutoff: bar,
        corroborating_origins=(second,),
    )
    result = adapter.fetch(item)
    assert isinstance(result, FetchSuccess)
    assert result.record is not None
    expected = {"open": "149.10", "high": "151.00", "low": "148.75", "close": "150.25", "volume": "28186700"}
    assert {key: result.record.payload[key] for key in expected} == expected
    assert result.corroborations, "the second origin must corroborate the same bar"
    assert {key: result.corroborations[0].record.payload[key] for key in expected} == expected
    for payload in (result.record.payload, result.corroborations[0].record.payload):
        assert not any(isinstance(value, float) for value in payload.values()), "binary float reached a payload"


def test_a_bar_field_the_vendor_did_not_send_is_an_explicit_null() -> None:
    """A key that is absent and a key that is null are different claims: the payload
    always carries the four bar keys, so a null under this vintage means "the source
    asserted nothing" rather than "nobody asked" (the v6/v7 lesson in parser_identity)."""
    item = _work_item("a" * 64)
    adapter = _adapter(item, lambda symbol, cutoff: _quote(date(2026, 3, 31), "150.25"))
    result = adapter.fetch(item)
    assert isinstance(result, FetchSuccess)
    assert result.record is not None
    assert {key: result.record.payload[key] for key in ("open", "high", "low", "volume")} == {
        "open": None,
        "high": None,
        "low": None,
        "volume": None,
    }


def test_an_origin_that_raises_is_absent_logged_and_counted(caplog) -> None:
    """#885: a second origin never fails the primary capture, and it is never silent
    either. The raising origin is absent from the success; the one that answered still
    corroborates; the warning names the origin and the exception type; the tick's tally
    counts one lost fetch."""
    import logging

    from data_engine.datahub.production_topt.corroboration_audit import corroboration_tally
    from data_engine.datahub.production_topt.market_price_adapter import CorroboratingOrigin

    def revoked(symbol: str, cutoff: date) -> MarketPriceQuote:
        raise PermissionError("api key revoked")

    def origin(name: str, fetch) -> CorroboratingOrigin:
        return CorroboratingOrigin(
            origin=name,
            parser_version=f"{name}-parser:v1",
            mapping_version=f"{name}-map:v1",
            value_key="close",
            confidence=Decimal("0.85"),
            fetch=fetch,
        )

    item = _work_item("b" * 64)
    quote = _quote(date(2026, 3, 31), "150.25")
    adapter = MarketPriceAdapter(
        {
            item.work_item_id: MarketPriceTarget(
                "GOOG", _CUTOFF, "issuer:lei:X", "security:cusip:Y", "listing:xnas:goog"
            )
        },
        lambda symbol, cutoff: quote,
        corroborating_origins=(origin("twelve-data", revoked), origin("moomoo-kline", lambda s, c: quote)),
    )
    with caplog.at_level(logging.WARNING), corroboration_tally() as tally:
        result = adapter.fetch(item)

    assert isinstance(result, FetchSuccess)
    assert [corroboration.origin for corroboration in result.corroborations] == ["moomoo-kline"]
    assert tally.total == 1 and tally.summary() == "corroborations refused 1 (twelve-data fetch 1)"
    [record] = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert "twelve-data" in record.getMessage() and "PermissionError" in record.getMessage()
    assert "GOOG" in record.getMessage() and record.exc_info is not None
