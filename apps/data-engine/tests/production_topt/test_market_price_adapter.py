from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from data_engine.datahub.production_topt.executor import (
    FetchFailure,
    FetchSuccess,
    ToptCaptureExecutor,
)
from data_engine.datahub.production_topt.market_price_adapter import (
    CorroboratingOrigin,
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


# -- failover to the next registered origin (#862) -------------------------------------------


class _Recorder:
    """A sink that keeps what the executor handed it."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def record_outcome(self, work_item, *, attempt_reasons, terminal_state, success) -> None:
        self.calls.append(
            {"attempt_reasons": tuple(attempt_reasons), "terminal_state": terminal_state, "success": success}
        )


class _CountingFetch:
    """An origin fetcher that answers from a script and counts every call."""

    def __init__(self, answer) -> None:
        self._answer = answer
        self.calls = 0

    def __call__(self, symbol: str, cutoff: date) -> MarketPriceQuote | None:
        self.calls += 1
        return self._answer(symbol, cutoff) if callable(self._answer) else self._answer


def _twelve_data(fetch) -> CorroboratingOrigin:
    from data_engine.datahub.production_topt import twelve_data_origin as twelve

    return CorroboratingOrigin(
        origin=twelve.ORIGIN,
        parser_version=twelve.PARSER_VERSION,
        mapping_version=twelve.MAPPING_VERSION,
        value_key=twelve.VALUE_KEY,
        confidence=Decimal("0.85"),
        fetch=fetch,
    )


def _moomoo_kline(fetch) -> CorroboratingOrigin:
    from data_engine.datahub.production_topt.source_registrations import (
        MOOMOO_KLINE_MAPPING_VERSION,
        MOOMOO_KLINE_ORIGIN,
        MOOMOO_KLINE_PARSER_VERSION,
        MOOMOO_KLINE_VALUE_KEY,
    )
    from truealpha_contracts.models import DataSource

    return CorroboratingOrigin(
        origin=MOOMOO_KLINE_ORIGIN,
        parser_version=MOOMOO_KLINE_PARSER_VERSION,
        mapping_version=MOOMOO_KLINE_MAPPING_VERSION,
        value_key=MOOMOO_KLINE_VALUE_KEY,
        confidence=Decimal("0.80"),
        fetch=fetch,
        raw_source=DataSource.MOOMOO,
    )


def _vendor_quote(vendor: str, day: date, close: str, knowable: datetime | None = None) -> MarketPriceQuote:
    return MarketPriceQuote(
        raw_bytes=f"{vendor}:GOOG:{day}:{close}".encode(),
        close=Decimal(close),
        as_of=day,
        knowable_at=knowable or datetime.combine(day, datetime.min.time(), tzinfo=UTC),
        open=Decimal(close),
        high=Decimal(close) + 1,
        low=Decimal(close) - 1,
        volume=Decimal("1000"),
    )


_RUN_CUTOFF = datetime(2026, 3, 31, 22, 15, tzinfo=UTC)


def _failover_adapter(item: CaptureWorkItem, primary, *origins: CorroboratingOrigin) -> MarketPriceAdapter:
    return MarketPriceAdapter(
        {
            item.work_item_id: MarketPriceTarget(
                "GOOG", _CUTOFF, "issuer:lei:X", "security:cusip:Y", "listing:xnas:goog", run_cutoff=_RUN_CUTOFF
            )
        },
        primary,
        corroborating_origins=origins,
    )


def _capture(item: CaptureWorkItem, adapter: MarketPriceAdapter):
    sink = _Recorder()
    report = ToptCaptureExecutor(_FakeWriter(), sink=sink).run(
        "capture-run:" + "c" * 64,
        [item],
        adapter,
        cutoff=_RUN_CUTOFF,
        recorded_at=_RUN_CUTOFF,
    )
    return report, sink


def _unavailable(symbol: str, cutoff: date) -> MarketPriceQuote:
    raise SourceUnavailableError("502 from the chart endpoint")


def test_an_unavailable_primary_is_served_by_twelve_data_graded_one_step_lower(caplog) -> None:
    """#862 acceptance 1: Yahoo raises `SourceUnavailableError` on every retry and Twelve
    Data holds the settled close. The cell is served — by Twelve Data, under ITS parser
    vintage, raw source and record id (never disguised as the primary), with the marker
    in the payload and 0.75 (Twelve Data's 0.85 one grade down). The attempt ledger the
    sink receives still reads TRANSIENT_NETWORK on every attempt, and the confidence
    report grades the cell LOW: one origin asserted it."""
    import logging

    from data_engine.datahub import confidence_report
    from data_engine.datahub.production_topt import twelve_data_origin as twelve
    from data_engine.datahub.production_topt.source_registrations import SOURCE_BY_PARSER
    from truealpha_contracts.models import DataSource

    item = _work_item("c" * 64)
    twelve_fetch = _CountingFetch(_vendor_quote("td", _CUTOFF, "150.30"))
    with caplog.at_level(logging.WARNING):
        report, sink = _capture(item, _failover_adapter(item, _unavailable, _twelve_data(twelve_fetch)))

    [outcome] = report.outcomes
    assert outcome.terminal_state is ObligationTerminalState.SUCCESS
    assert outcome.reason_code is ObligationReasonCode.TRANSIENT_NETWORK
    assert outcome.served_by_failover == twelve.ORIGIN
    [call] = sink.calls
    assert call["attempt_reasons"] == (ObligationReasonCode.TRANSIENT_NETWORK,) * 3
    served = call["success"]
    assert isinstance(served, FetchSuccess)
    assert served.served_by_failover == twelve.ORIGIN
    assert served.raw.source is DataSource.TWELVE_DATA
    assert served.raw.record_id == f"{twelve.ORIGIN}:GOOG:2026-03-31"
    assert served.record is not None
    assert (served.record.parser_version, served.record.mapping_version) == (
        twelve.PARSER_VERSION,
        twelve.MAPPING_VERSION,
    )
    assert served.record.payload["close"] == "150.30"
    assert served.record.payload["served_by_failover"] == twelve.ORIGIN
    assert served.confidence == Decimal("0.75")
    assert served.transaction_time == datetime(2026, 3, 31, tzinfo=UTC)
    assert served.corroborations == ()
    assert twelve_fetch.calls == 1, "the failover origin is asked once, not once per primary retry"
    assert any("twelve-data" in record.getMessage() and "GOOG" in record.getMessage() for record in caplog.records)

    # The served observation is what the mart validates, marker included.
    from data_engine.datahub.production_topt.materialization import MarketPricePayload

    assert MarketPricePayload.model_validate(dict(served.record.payload)).close == Decimal("150.30")
    # Graded from what the observation asserts: one origin, LOW, named as a failover.
    origins = confidence_report.bar_origins(
        SOURCE_BY_PARSER[served.record.parser_version],
        dict(served.record.payload),
        knowable_at=served.transaction_time,
        observation_id="normalized-observation:" + served.normalized_sha256,
    )
    grade = confidence_report.classify_cell(
        confidence_report.family_policy("close"), "listing:xnas:goog", [origins["close"]], _RUN_CUTOFF
    )
    assert (grade.band, grade.reason, grade.independent_origins) == (
        confidence_report.Band.LOW,
        "served_by_failover",
        1,
    )


def test_a_null_primary_fails_over_in_the_fusion_policy_order() -> None:
    """#862 acceptance 2: the primary has nothing (the overnight null-close shape, #622).
    The origins are handed over in the WRONG order; the adapter asks them in
    `RECONCILIATION_POLICY.source_priority` order anyway, so Twelve Data serves and
    moomoo — still asked — corroborates the served session at its normal grade."""
    item = _work_item("d" * 64)
    twelve_fetch = _CountingFetch(_vendor_quote("td", _CUTOFF, "150.30"))
    moomoo_fetch = _CountingFetch(_vendor_quote("mm", _CUTOFF, "150.28"))
    adapter = _failover_adapter(
        item, lambda symbol, cutoff: None, _moomoo_kline(moomoo_fetch), _twelve_data(twelve_fetch)
    )
    report, sink = _capture(item, adapter)

    [outcome] = report.outcomes
    assert (outcome.terminal_state, outcome.reason_code, outcome.attempts) == (
        ObligationTerminalState.SUCCESS,
        ObligationReasonCode.FIELD_UNAVAILABLE,
        1,
    )
    served = sink.calls[0]["success"]
    assert served.served_by_failover == "twelve-data"
    assert [corroboration.origin for corroboration in served.corroborations] == ["moomoo-kline"]
    assert served.corroborations[0].confidence == Decimal("0.80")
    assert "served_by_failover" not in served.corroborations[0].record.payload
    assert (twelve_fetch.calls, moomoo_fetch.calls) == (1, 1)


def test_the_next_origin_serves_when_the_second_has_nothing() -> None:
    """Twelve Data has no settled close; moomoo does. moomoo serves at 0.70 (its 0.80 one
    grade down), and Twelve Data, already asked, is not asked again for a corroboration."""
    from truealpha_contracts.models import DataSource

    item = _work_item("e" * 64)
    twelve_fetch = _CountingFetch(None)
    moomoo_fetch = _CountingFetch(_vendor_quote("mm", _CUTOFF, "150.28"))
    adapter = _failover_adapter(item, lambda s, c: None, _twelve_data(twelve_fetch), _moomoo_kline(moomoo_fetch))
    _report, sink = _capture(item, adapter)

    served = sink.calls[0]["success"]
    assert served.served_by_failover == "moomoo-kline"
    assert served.raw.source is DataSource.MOOMOO
    assert served.confidence == Decimal("0.70")
    assert served.corroborations == ()
    assert (twelve_fetch.calls, moomoo_fetch.calls) == (1, 1)


def test_every_origin_failing_leaves_the_primary_failure_unchanged(caplog) -> None:
    """#862 acceptance 3: the primary times out, Twelve Data raises and moomoo has
    nothing. The obligation resolves exactly as it did before failover existed:
    UNAVAILABLE after three TIMEOUT attempts, nothing handed to the sink, and the
    raising origin is logged, not swallowed (#885)."""
    import logging

    def _timeout(symbol: str, cutoff: date) -> MarketPriceQuote:
        raise TimeoutError

    def _revoked(symbol: str, cutoff: date) -> MarketPriceQuote:
        raise PermissionError("api key revoked")

    item = _work_item("f" * 64)
    adapter = _failover_adapter(item, _timeout, _twelve_data(_revoked), _moomoo_kline(_CountingFetch(None)))
    with caplog.at_level(logging.WARNING):
        report, sink = _capture(item, adapter)
    baseline_report, baseline_sink = _capture(item, _failover_adapter(item, _timeout))

    for run, recorded in ((report, sink), (baseline_report, baseline_sink)):
        [outcome] = run.outcomes
        assert (outcome.terminal_state, outcome.reason_code, outcome.attempts, outcome.served_by_failover) == (
            ObligationTerminalState.UNAVAILABLE,
            ObligationReasonCode.TIMEOUT,
            3,
            None,
        )
        assert recorded.calls == [
            {
                "attempt_reasons": (ObligationReasonCode.TIMEOUT,) * 3,
                "terminal_state": ObligationTerminalState.UNAVAILABLE,
                "success": None,
            }
        ]
    assert any("twelve-data" in r.getMessage() and "PermissionError" in r.getMessage() for r in caplog.records)
    # And the adapter's own fetch still answers the primary's classified failure.
    assert isinstance(adapter.fetch(item), FetchFailure)


def test_a_failover_origin_on_another_session_is_not_used() -> None:
    """#862: session alignment is the target's settled session. A Twelve Data close from
    the session before is a real close of a different day — it cannot serve this cell,
    so the next origin is asked; when no origin has the session the cell stays failed."""
    item = _work_item("1" * 64)
    previous_session = date(2026, 3, 30)
    twelve_fetch = _CountingFetch(_vendor_quote("td", previous_session, "149.00"))
    report, sink = _capture(item, _failover_adapter(item, lambda s, c: None, _twelve_data(twelve_fetch)))
    assert report.outcomes[0].terminal_state is ObligationTerminalState.UNAVAILABLE
    assert sink.calls[0]["success"] is None

    moomoo_fetch = _CountingFetch(_vendor_quote("mm", _CUTOFF, "150.28"))
    adapter = _failover_adapter(item, lambda s, c: None, _twelve_data(twelve_fetch), _moomoo_kline(moomoo_fetch))
    _report, sink = _capture(item, adapter)
    served = sink.calls[0]["success"]
    assert served.served_by_failover == "moomoo-kline"
    # The other-day close is still an assertion the reports narrow away (#622): it rides
    # along as a corroboration, and the fusion engine grades it on its own day.
    assert [(c.origin, c.transaction_time.date()) for c in served.corroborations] == [("twelve-data", previous_session)]


def test_a_failover_close_knowable_after_the_run_cutoff_is_not_used() -> None:
    """No look-ahead: the failover observation is what the snapshot binds, and the
    snapshot admits only what was knowable at the run's cutoff INSTANT. A close stamped
    on the right session date but knowable after the cutoff cannot serve."""
    item = _work_item("2" * 64)
    late = _vendor_quote("td", _CUTOFF, "150.30", knowable=_RUN_CUTOFF + timedelta(minutes=5))
    report, sink = _capture(item, _failover_adapter(item, lambda s, c: None, _twelve_data(_CountingFetch(late))))
    assert report.outcomes[0].terminal_state is ObligationTerminalState.UNAVAILABLE
    assert sink.calls[0]["success"] is None


def test_an_origin_the_fusion_policy_does_not_rank_never_serves() -> None:
    """An origin the policy does not list would be `unregistered` to the fusion engine and
    its cell would vanish from the report; one that writes its close under another key
    (Twelve Data v1's `price`) cannot satisfy the mart's payload contract. Neither may
    serve a cell; both may still corroborate a primary success."""
    item = _work_item("3" * 64)
    unranked = CorroboratingOrigin(
        origin="late-origin",
        parser_version="late-origin-parser:v1",
        mapping_version="late-origin-map:v1",
        value_key="close",
        confidence=Decimal("0.80"),
        fetch=_CountingFetch(_vendor_quote("lo", _CUTOFF, "150.30")),
    )
    legacy = CorroboratingOrigin(
        origin="twelve-data",
        parser_version="twelve-data-parser:v1",
        mapping_version="twelve-data-map:v1",
        value_key="price",
        confidence=Decimal("0.85"),
        fetch=_CountingFetch(_vendor_quote("td", _CUTOFF, "150.30")),
    )
    report, _sink = _capture(item, _failover_adapter(item, lambda s, c: None, unranked, legacy))
    assert report.outcomes[0].terminal_state is ObligationTerminalState.UNAVAILABLE
    success = _failover_adapter(item, lambda s, c: _quote(_CUTOFF, "150.25"), unranked, legacy).fetch(item)
    assert isinstance(success, FetchSuccess)
    assert [c.origin for c in success.corroborations] == ["late-origin", "twelve-data"]


def test_a_stop_reason_is_never_served_by_failover() -> None:
    """The adapter itself refuses to fail over a reason that is not "the primary had
    nothing": a contract violation or a look-ahead is a broken run, not a gap."""
    item = _work_item("4" * 64)
    adapter = _failover_adapter(
        item, lambda s, c: None, _twelve_data(_CountingFetch(_vendor_quote("td", _CUTOFF, "1")))
    )
    for reason in (
        ObligationReasonCode.LOOK_AHEAD_VIOLATION,
        ObligationReasonCode.CONTRACT_VIOLATION,
        ObligationReasonCode.NOT_YET_KNOWABLE,
    ):
        assert adapter.failover(item, reason) is None
    assert adapter.failover(_work_item("5" * 64), ObligationReasonCode.FIELD_UNAVAILABLE) is None
    assert adapter.failover(item, ObligationReasonCode.RATE_LIMITED) is not None


def test_the_deployed_route_fails_over_through_the_executor(monkeypatch) -> None:
    """Asserted through the deployed entry point (AGENTS.md rule 7): `build_route` — what
    the composition root calls — wires the registered origins as failovers, stamps the
    run's cutoff on every target, and a Yahoo outage is served by Twelve Data through
    the same executor the tick runs."""
    from data_engine.datahub.production_topt import market_price_adapter as module
    from data_engine.datahub.production_topt import moomoo_origin, twelve_data_origin
    from data_engine.datahub.production_topt.source_registrations import RouteCell, RouteContext

    twelve_fetch = _CountingFetch(_vendor_quote("td", _CUTOFF, "150.30"))
    moomoo_fetch = _CountingFetch(_vendor_quote("mm", _CUTOFF, "150.31"))
    monkeypatch.setattr(twelve_data_origin, "twelve_data_origin", lambda: _twelve_data(twelve_fetch))
    monkeypatch.setattr(moomoo_origin, "moomoo_kline_origin", lambda: _moomoo_kline(moomoo_fetch))
    monkeypatch.setattr(module, "yahoo_quote_fetcher", _unavailable)
    item = _work_item("6" * 64)
    context = RouteContext(
        cutoff=_RUN_CUTOFF,
        cutoff_date=_CUTOFF,
        price_cutoff_date=_CUTOFF,
        partition_start=datetime(2026, 3, 31, tzinfo=UTC),
        universe_published_at=None,
        coordinates={},
        connection=None,
    )
    cells = [
        RouteCell(item.work_item_id, "market-price", "issuer:lei:X", "security:cusip:Y", "listing:xnas:goog", "GOOG")
    ]
    adapter = module.build_route(context, cells)
    assert adapter._targets[item.work_item_id].run_cutoff == _RUN_CUTOFF

    _report, sink = _capture(item, adapter)
    served = sink.calls[0]["success"]
    assert served.served_by_failover == "twelve-data"
    assert [c.origin for c in served.corroborations] == ["moomoo-kline"]


def test_an_exhausted_budget_defers_the_cell_through_the_deployed_fetcher(call_ledger, monkeypatch) -> None:
    """Rule 6 (#729): once this environment's Yahoo budget is spent, the deployed fetcher's
    request is refused before it is sent, and the cell is `deferred_capacity` — not
    `field_unavailable`, which would read as "Yahoo has no bar"."""
    from data_engine.datahub.production_topt.market_price_adapter import yahoo_quote_fetcher
    from data_engine.sources import gateway, yahoo

    now = datetime.now(UTC)
    call_ledger.extend([gateway.CallRecord(source="yahoo", endpoint="chart", caller="x", called_at=now, ok=True)] * 3)

    class _NeverAsked:
        def __init__(self, **_kwargs: object) -> None: ...

        def __enter__(self):
            return self

        def __exit__(self, *_exc: object) -> None: ...

        def get(self, *args, **kwargs):
            raise AssertionError("a refused request reached Yahoo")

    monkeypatch.setattr(yahoo.httpx, "Client", _NeverAsked)
    gate = gateway.CapacityGate(capacities={"yahoo": gateway.SourceCapacity("yahoo", 1.0, 5, 3)}, environment="staging")
    item = _work_item("9" * 64)
    with gateway.capacity_scope(gate):
        result = _adapter(item, yahoo_quote_fetcher).fetch(item)
    assert isinstance(result, FetchFailure)
    assert result.reason_code is ObligationReasonCode.DEFERRED_CAPACITY
    assert len(call_ledger) == 3
