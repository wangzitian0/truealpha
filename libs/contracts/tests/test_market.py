from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from truealpha_contracts.market import (
    CorporateAction,
    CorporateActionClockTick,
    CorporateActionPhase,
    CorporateActionType,
    CurrencyConversionBinding,
    CurrencyConversionPolicy,
    CurrencyPair,
    ExchangeCalendar,
    FxRate,
    IssuerListingValuationPath,
    ListingPriceBar,
    MarketHoliday,
    MarketSession,
    MarketSessionKind,
    PriceBasis,
    ShareCountBasis,
    SharesOutstanding,
    V1ReturnReplay,
)
from truealpha_contracts.universe import (
    IssuerSecurityLink,
    ListingRole,
    SecurityKind,
    SecurityListingLink,
)

AS_OF = datetime(2024, 7, 20, 0, 0, tzinfo=UTC)
KNOWABLE_AT = datetime(2024, 6, 1, 14, 1, tzinfo=UTC)
RECORDED_AT = datetime(2024, 6, 1, 14, 2, tzinfo=UTC)


def _calendar() -> ExchangeCalendar:
    return ExchangeCalendar.create(
        calendar_id="calendar:us-equities",
        calendar_version="2024.07",
        exchange_mic="XNAS",
        timezone="America/New_York",
        valid_from=date(2024, 7, 2),
        valid_to=date(2024, 7, 5),
        sessions=(
            MarketSession(
                session_date=date(2024, 7, 2),
                opens_at=datetime(2024, 7, 2, 13, 30, tzinfo=UTC),
                closes_at=datetime(2024, 7, 2, 20, 0, tzinfo=UTC),
            ),
            MarketSession(
                session_date=date(2024, 7, 3),
                opens_at=datetime(2024, 7, 3, 13, 30, tzinfo=UTC),
                closes_at=datetime(2024, 7, 3, 17, 0, tzinfo=UTC),
                kind=MarketSessionKind.EARLY_CLOSE,
            ),
            MarketSession(
                session_date=date(2024, 7, 5),
                opens_at=datetime(2024, 7, 5, 13, 30, tzinfo=UTC),
                closes_at=datetime(2024, 7, 5, 20, 0, tzinfo=UTC),
            ),
        ),
        holidays=(MarketHoliday(holiday_date=date(2024, 7, 4), name="Independence Day"),),
    )


def _price_bar(
    *,
    input_id: str = "price:xnas-goog:2024-07-02",
    listing_id: str = "listing:xnas-goog",
    trading_date: date = date(2024, 7, 2),
    session_close_at: datetime = datetime(2024, 7, 2, 20, 0, tzinfo=UTC),
    currency: str = "USD",
    price_basis: PriceBasis = PriceBasis.UNADJUSTED,
) -> ListingPriceBar:
    return ListingPriceBar(
        input_id=input_id,
        listing_id=listing_id,
        calendar_id="calendar:us-equities",
        calendar_version="2024.07",
        trading_date=trading_date,
        session_close_at=session_close_at,
        open=Decimal("175"),
        high=Decimal("178"),
        low=Decimal("174"),
        close=Decimal("177"),
        volume=1_000_000,
        currency=currency,
        price_basis=price_basis,
        knowable_at=session_close_at.replace(minute=1),
        recorded_at=session_close_at.replace(minute=2),
        confidence=Decimal("1"),
        raw_ref="raw.fetches:price-goog",
    )


def _issuer_security_link(
    *,
    security_id: str,
    share_class: str,
    security_kind: SecurityKind = SecurityKind.COMMON_STOCK,
    underlying_security_id: str | None = None,
    ratio: Decimal = Decimal("1"),
) -> IssuerSecurityLink:
    return IssuerSecurityLink(
        input_id=f"identity:{security_id}",
        issuer_id="issuer:alphabet",
        security_id=security_id,
        security_kind=security_kind,
        share_class=share_class,
        underlying_security_id=underlying_security_id,
        underlying_shares_per_security_unit=ratio,
        valid_from=date(2014, 4, 3),
        knowable_at=KNOWABLE_AT,
        recorded_at=RECORDED_AT,
        confidence=Decimal("1"),
        raw_ref="raw.fetches:identity",
    )


def _security_listing_link(
    *,
    security_id: str,
    listing_id: str,
    ticker: str,
    role: ListingRole = ListingRole.PRIMARY,
) -> SecurityListingLink:
    return SecurityListingLink(
        input_id=f"identity:{listing_id}",
        security_id=security_id,
        listing_id=listing_id,
        exchange_mic="XNAS",
        ticker=ticker,
        listing_role=role,
        currency="USD",
        timezone="America/New_York",
        trading_calendar_id="calendar:us-equities",
        trading_calendar_version="2024.07",
        valid_from=date(2014, 4, 3),
        knowable_at=KNOWABLE_AT,
        recorded_at=RECORDED_AT,
        confidence=Decimal("1"),
        raw_ref="raw.fetches:identity",
    )


def _shares(*, security_id: str, share_class: str) -> SharesOutstanding:
    return SharesOutstanding(
        input_id=f"shares:{security_id}:2024q2",
        security_id=security_id,
        share_class=share_class,
        basis=ShareCountBasis.POINT_IN_TIME_OUTSTANDING,
        shares=Decimal("5870000000"),
        valid_at=date(2024, 6, 30),
        knowable_at=datetime(2024, 7, 1, 12, 0, tzinfo=UTC),
        recorded_at=datetime(2024, 7, 1, 12, 1, tzinfo=UTC),
        confidence=Decimal("1"),
        raw_ref="raw.fetches:shares",
    )


def _usd_policy() -> CurrencyConversionPolicy:
    return CurrencyConversionPolicy.create(
        policy_id="currency-policy:usd-only",
        policy_version="v1",
        valuation_currency="USD",
        compatible_currencies=("USD",),
    )


def _currency_binding(
    *,
    source_currency: str = "USD",
    policy: CurrencyConversionPolicy | None = None,
    fx_rate: FxRate | None = None,
) -> CurrencyConversionBinding:
    return CurrencyConversionBinding(
        as_of=AS_OF,
        source_currency=source_currency,
        target_currency="USD",
        policy=policy or _usd_policy(),
        fx_rate=fx_rate,
    )


def _valuation_path(
    *,
    issuer_link: IssuerSecurityLink,
    listing_link: SecurityListingLink,
    shares: SharesOutstanding,
    bar: ListingPriceBar,
    multiplier: Decimal,
    currency_binding: CurrencyConversionBinding | None = None,
) -> IssuerListingValuationPath:
    return IssuerListingValuationPath.create(
        path_id=f"valuation-path:{bar.listing_id}",
        issuer_id="issuer:alphabet",
        as_of=AS_OF,
        issuer_security_link=issuer_link,
        security_listing_link=listing_link,
        shares_outstanding=shares,
        price_bar=bar,
        underlying_shares_per_listed_unit=multiplier,
        currency_binding=currency_binding or _currency_binding(source_currency=bar.currency),
    )


def _action(
    *,
    action_id: str,
    action_type: CorporateActionType,
    **overrides: object,
) -> CorporateAction:
    values: dict[str, object] = {
        "action_id": action_id,
        "action_type": action_type,
        "security_id": "security:alphabet-class-c",
        "share_class": "C",
        "source_instrument_ids": ("security:alphabet-class-c",),
        "declared_at": datetime(2024, 6, 1, 14, 0, tzinfo=UTC),
        "knowable_at": KNOWABLE_AT,
        "recorded_at": RECORDED_AT,
        "confidence": Decimal("1"),
        "raw_ref": "raw.fetches:action",
    }
    values.update(overrides)
    return CorporateAction(**values)


def _clock(
    action: CorporateAction, *, omit: CorporateActionPhase | None = None
) -> tuple[CorporateActionClockTick, ...]:
    lifecycle = [
        (phase, occurred_at)
        for phase, occurred_at in action.lifecycle_times().items()
        if occurred_at <= AS_OF and phase is not omit
    ]
    lifecycle.sort(key=lambda item: (max(item[1], action.knowable_at), item[0].value))
    return tuple(
        CorporateActionClockTick(
            tick_id=f"tick:{action.action_id}:{phase.value}",
            action_id=action.action_id,
            phase=phase,
            occurred_at=occurred_at,
            applied_at=max(occurred_at, action.knowable_at),
            sequence=index,
        )
        for index, (phase, occurred_at) in enumerate(lifecycle, start=1)
    )


def test_exchange_calendar_is_content_addressed_and_blocks_holiday_execution() -> None:
    calendar = _calendar()

    assert calendar.require_session(date(2024, 7, 3)).kind is MarketSessionKind.EARLY_CLOSE
    assert len(calendar.content_sha256) == 64
    with pytest.raises(ValueError, match="market holiday: Independence Day"):
        calendar.require_session(date(2024, 7, 4))
    with pytest.raises(ValidationError, match="frozen"):
        calendar.calendar_version = "changed"  # type: ignore[misc]


def test_exchange_calendar_rejects_session_on_holiday_and_hash_tampering() -> None:
    calendar = _calendar()
    holiday_session = MarketSession(
        session_date=date(2024, 7, 4),
        opens_at=datetime(2024, 7, 4, 13, 30, tzinfo=UTC),
        closes_at=datetime(2024, 7, 4, 20, 0, tzinfo=UTC),
    )

    with pytest.raises(ValidationError, match="holidays cannot also be market sessions"):
        ExchangeCalendar(
            **(calendar.model_dump(exclude={"sessions"}) | {"sessions": (*calendar.sessions, holiday_session)})
        )
    with pytest.raises(ValidationError, match="content_sha256 does not match"):
        ExchangeCalendar(**(calendar.model_dump(exclude={"content_sha256"}) | {"content_sha256": "0" * 64}))


def test_v1_listing_bar_rejects_adjusted_prices() -> None:
    with pytest.raises(ValidationError, match="explicitly unadjusted"):
        _price_bar(price_basis=PriceBasis.ADJUSTED_RECONCILIATION_ONLY)


def test_replay_rejects_a_price_bar_on_a_market_holiday() -> None:
    holiday_bar = _price_bar(
        input_id="price:xnas-goog:2024-07-04",
        trading_date=date(2024, 7, 4),
        session_close_at=datetime(2024, 7, 4, 20, 0, tzinfo=UTC),
    )

    with pytest.raises(ValidationError, match="market holiday"):
        V1ReturnReplay.create(
            replay_id="replay:goog:holiday",
            security_id="security:alphabet-class-c",
            share_class="C",
            listing_id="listing:xnas-goog",
            as_of=AS_OF,
            calendar=_calendar(),
            price_bars=(holiday_bar,),
        )


def test_currency_conversion_fails_closed_outside_scope_or_without_explicit_fx() -> None:
    with pytest.raises(ValidationError, match="outside the accepted compatible-currency scope"):
        _currency_binding(source_currency="EUR")

    policy = CurrencyConversionPolicy.create(
        policy_id="currency-policy:eur-usd",
        policy_version="v1",
        valuation_currency="USD",
        compatible_currencies=("USD", "EUR"),
        permitted_fx_pairs=(CurrencyPair(base_currency="EUR", quote_currency="USD"),),
    )
    with pytest.raises(ValidationError, match="requires an explicit PIT FX input"):
        _currency_binding(source_currency="EUR", policy=policy)


def test_currency_conversion_accepts_only_exact_directed_pit_fx_input() -> None:
    pair = CurrencyPair(base_currency="EUR", quote_currency="USD")
    policy = CurrencyConversionPolicy.create(
        policy_id="currency-policy:eur-usd",
        policy_version="v1",
        valuation_currency="USD",
        compatible_currencies=("EUR", "USD"),
        permitted_fx_pairs=(pair,),
    )
    fx = FxRate(
        input_id="fx:eur-usd:2024-07-02",
        pair=pair,
        quote_per_base=Decimal("1.0735"),
        valid_at=datetime(2024, 7, 2, 20, 0, tzinfo=UTC),
        knowable_at=datetime(2024, 7, 2, 20, 1, tzinfo=UTC),
        recorded_at=datetime(2024, 7, 2, 20, 2, tzinfo=UTC),
        confidence=Decimal("1"),
        raw_ref="raw.fetches:fx",
    )

    binding = _currency_binding(source_currency="EUR", policy=policy, fx_rate=fx)

    assert binding.fx_rate is fx


def test_adr_valuation_path_binds_underlying_shares_and_exact_ratio() -> None:
    issuer_link = _issuer_security_link(
        security_id="security:alphabet-adr",
        share_class="ADR",
        security_kind=SecurityKind.ADR,
        underlying_security_id="security:alphabet-ordinary",
        ratio=Decimal("2"),
    )
    listing_link = _security_listing_link(
        security_id="security:alphabet-adr",
        listing_id="listing:xnas-alph-adr",
        ticker="ALPH",
    )
    bar = _price_bar(listing_id="listing:xnas-alph-adr", input_id="price:xnas-alph-adr:2024-07-02")
    shares = _shares(security_id="security:alphabet-ordinary", share_class="Ordinary")

    path = _valuation_path(
        issuer_link=issuer_link,
        listing_link=listing_link,
        shares=shares,
        bar=bar,
        multiplier=Decimal("2"),
    )

    assert path.underlying_shares_per_listed_unit == Decimal("2")
    assert path.shares_outstanding.security_id == "security:alphabet-ordinary"
    with pytest.raises(ValidationError, match="must equal the identity-link ADR ratio"):
        _valuation_path(
            issuer_link=issuer_link,
            listing_link=listing_link,
            shares=shares,
            bar=bar,
            multiplier=Decimal("1"),
        )


def test_goog_and_googl_share_classes_cannot_be_substituted() -> None:
    goog_link = _issuer_security_link(security_id="security:alphabet-class-c", share_class="C")
    goog_listing = _security_listing_link(
        security_id="security:alphabet-class-c",
        listing_id="listing:xnas-goog",
        ticker="GOOG",
    )
    googl_shares = _shares(security_id="security:alphabet-class-a", share_class="A")

    with pytest.raises(ValidationError, match="cannot substitute another security or share class"):
        _valuation_path(
            issuer_link=goog_link,
            listing_link=goog_listing,
            shares=googl_shares,
            bar=_price_bar(),
            multiplier=Decimal("1"),
        )


def test_valuation_path_requires_exact_listing_instead_of_implicit_primary_lookup() -> None:
    issuer_link = _issuer_security_link(security_id="security:alphabet-class-c", share_class="C")
    explicit_secondary = _security_listing_link(
        security_id="security:alphabet-class-c",
        listing_id="listing:xlon-goog",
        ticker="GOOG",
        role=ListingRole.SECONDARY,
    )

    with pytest.raises(ValidationError, match="exact explicit listing"):
        _valuation_path(
            issuer_link=issuer_link,
            listing_link=explicit_secondary,
            shares=_shares(security_id="security:alphabet-class-c", share_class="C"),
            bar=_price_bar(listing_id="listing:xnas-goog"),
            multiplier=Decimal("1"),
        )


def test_symbol_and_primary_listing_changes_name_both_sides_explicitly() -> None:
    symbol_change = _action(
        action_id="action:goog-symbol-change",
        action_type=CorporateActionType.SYMBOL_CHANGE,
        source_listing_id="listing:xnas-goog-old",
        resulting_listing_id="listing:xnas-goog-old",
        resulting_instrument_ids=("security:alphabet-class-c",),
        effective_at=datetime(2024, 7, 5, 13, 30, tzinfo=UTC),
        old_symbol="GOOGV",
        new_symbol="GOOG",
    )
    primary_change = _action(
        action_id="action:goog-primary-change",
        action_type=CorporateActionType.PRIMARY_LISTING_CHANGE,
        source_listing_id="listing:xnas-goog",
        resulting_listing_id="listing:xnys-goog",
        resulting_instrument_ids=("security:alphabet-class-c",),
        effective_at=datetime(2024, 7, 5, 13, 30, tzinfo=UTC),
    )

    assert (symbol_change.old_symbol, symbol_change.new_symbol) == ("GOOGV", "GOOG")
    assert primary_change.source_listing_id != primary_change.resulting_listing_id


def test_delisting_has_an_explicit_effective_time_and_no_implicit_successor() -> None:
    action = _action(
        action_id="action:goog-delisting",
        action_type=CorporateActionType.DELISTING,
        source_listing_id="listing:xnas-goog",
        effective_at=datetime(2024, 7, 5, 20, 0, tzinfo=UTC),
        delisting_reason="Exchange delisting",
    )

    assert action.effective_at == datetime(2024, 7, 5, 20, 0, tzinfo=UTC)
    assert action.resulting_instrument_ids == ()
    with pytest.raises(ValidationError, match="cannot imply a successor"):
        _action(
            action_id="action:goog-delisting-invalid",
            action_type=CorporateActionType.DELISTING,
            source_listing_id="listing:xnas-goog",
            resulting_listing_id="listing:xnys-goog",
            resulting_instrument_ids=("security:alphabet-successor",),
            effective_at=datetime(2024, 7, 5, 20, 0, tzinfo=UTC),
            delisting_reason="Exchange delisting",
        )


def test_split_lifecycle_is_applied_exactly_once_on_monotonic_clock() -> None:
    split = _action(
        action_id="action:goog-split-20-for-1",
        action_type=CorporateActionType.SPLIT,
        resulting_instrument_ids=("security:alphabet-class-c",),
        ex_at=datetime(2024, 7, 3, 13, 30, tzinfo=UTC),
        effective_at=datetime(2024, 7, 3, 13, 30, tzinfo=UTC),
        split_ratio_after_per_before=Decimal("20"),
    )
    clock = _clock(split)

    replay = V1ReturnReplay.create(
        replay_id="replay:goog:split",
        security_id="security:alphabet-class-c",
        share_class="C",
        listing_id="listing:xnas-goog",
        as_of=AS_OF,
        calendar=_calendar(),
        price_bars=(_price_bar(),),
        corporate_actions=(split,),
        action_clock=clock,
    )

    assert len(replay.action_clock) == len(split.lifecycle_times())
    duplicate = CorporateActionClockTick(
        tick_id="tick:duplicate-split-effective",
        action_id=split.action_id,
        phase=CorporateActionPhase.EFFECTIVE,
        occurred_at=split.effective_at,
        applied_at=split.effective_at,
        sequence=len(clock) + 1,
    )
    with pytest.raises(ValidationError, match="lifecycle phase must be applied exactly once"):
        V1ReturnReplay.create(
            replay_id="replay:goog:split-duplicate",
            security_id="security:alphabet-class-c",
            share_class="C",
            listing_id="listing:xnas-goog",
            as_of=AS_OF,
            calendar=_calendar(),
            price_bars=(_price_bar(),),
            corporate_actions=(split,),
            action_clock=(*clock, duplicate),
        )


def test_dividend_record_and_pay_phases_are_both_required_exactly_once() -> None:
    dividend = _action(
        action_id="action:goog-dividend-2024q2",
        action_type=CorporateActionType.CASH_DIVIDEND,
        ex_at=datetime(2024, 7, 2, 13, 30, tzinfo=UTC),
        record_at=datetime(2024, 7, 3, 20, 0, tzinfo=UTC),
        pay_at=datetime(2024, 7, 10, 14, 0, tzinfo=UTC),
        cash_amount_per_share=Decimal("0.20"),
        cash_currency="USD",
    )
    complete_clock = _clock(dividend)

    replay = V1ReturnReplay.create(
        replay_id="replay:goog:dividend",
        security_id="security:alphabet-class-c",
        share_class="C",
        listing_id="listing:xnas-goog",
        as_of=AS_OF,
        calendar=_calendar(),
        price_bars=(_price_bar(),),
        corporate_actions=(dividend,),
        action_clock=complete_clock,
    )

    assert {tick.phase for tick in replay.action_clock} >= {
        CorporateActionPhase.RECORD,
        CorporateActionPhase.PAY,
    }
    with pytest.raises(ValidationError, match="missing=.*pay"):
        V1ReturnReplay.create(
            replay_id="replay:goog:dividend-missing-pay",
            security_id="security:alphabet-class-c",
            share_class="C",
            listing_id="listing:xnas-goog",
            as_of=AS_OF,
            calendar=_calendar(),
            price_bars=(_price_bar(),),
            corporate_actions=(dividend,),
            action_clock=_clock(dividend, omit=CorporateActionPhase.PAY),
        )


def test_return_replay_is_content_addressed_and_rejects_hash_tampering() -> None:
    replay = V1ReturnReplay.create(
        replay_id="replay:goog:plain",
        security_id="security:alphabet-class-c",
        share_class="C",
        listing_id="listing:xnas-goog",
        as_of=AS_OF,
        calendar=_calendar(),
        price_bars=(_price_bar(),),
    )

    assert len(replay.content_sha256) == 64
    with pytest.raises(ValidationError, match="content_sha256 does not match"):
        V1ReturnReplay(**(replay.model_dump(exclude={"content_sha256"}) | {"content_sha256": "f" * 64}))


def test_market_contracts_forbid_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SharesOutstanding(
            input_id="shares:goog",
            security_id="security:alphabet-class-c",
            share_class="C",
            basis=ShareCountBasis.POINT_IN_TIME_OUTSTANDING,
            shares=Decimal("1"),
            valid_at=date(2024, 6, 30),
            knowable_at=KNOWABLE_AT,
            recorded_at=RECORDED_AT,
            confidence=Decimal("1"),
            raw_ref="raw.fetches:shares",
            issuer_id="issuer:alphabet",
        )
