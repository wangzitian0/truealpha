"""Split events derived from unadjusted and split-adjusted price series (#758, M3)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction

RATIO_TOLERANCE = Decimal("0.005")
MAX_FACTOR_DENOMINATOR = 20
MAX_FORWARD_SPLIT_DENOMINATOR = 3


class SplitDerivationError(ValueError):
    """The two price series do not identify a split without ambiguity."""


@dataclass(frozen=True)
class SeriesBar:
    session: date
    unadjusted_open: Decimal | None
    unadjusted_close: Decimal | None
    split_adjusted_close: Decimal | None


@dataclass(frozen=True)
class SplitEvent:
    symbol: str
    ex_date: date
    numerator: int
    denominator: int

    @property
    def factor(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)


def _positive(value: Decimal | None) -> bool:
    return value is not None and value > 0


def _first_unadjusted(bar: SeriesBar) -> Decimal | None:
    price = bar.unadjusted_open if _positive(bar.unadjusted_open) else bar.unadjusted_close
    return price if _positive(price) else None


def _snap(symbol: str, session: date, step: Decimal) -> Fraction:
    exact = Fraction(step)
    candidate = (
        exact.limit_denominator(MAX_FACTOR_DENOMINATOR)
        if exact >= 1
        else 1 / (1 / exact).limit_denominator(MAX_FACTOR_DENOMINATOR)
    )
    if candidate == 1:
        raise SplitDerivationError(
            f"{symbol} {session.isoformat()}: the price ratio moved by {step} without a split factor; "
            "the split-adjusted series is not adjusted for splits only"
        )
    error = abs(Decimal(candidate.numerator) / Decimal(candidate.denominator) / step - 1)
    if error > RATIO_TOLERANCE:
        raise SplitDerivationError(
            f"{symbol} {session.isoformat()}: the price ratio stepped by {step}, which is not a simple split ratio"
        )
    return candidate


def _confirm(symbol: str, session: date, factor: Fraction, previous_close: Decimal, bar: SeriesBar) -> None:
    first_price = _first_unadjusted(bar)
    if first_price is None:
        raise SplitDerivationError(f"{symbol} {session.isoformat()}: no unadjusted price confirms the split")
    factor_decimal = Decimal(factor.numerator) / Decimal(factor.denominator)
    if abs((first_price * factor_decimal / previous_close).ln()) >= abs(factor_decimal.ln()) / 2:
        raise SplitDerivationError(
            f"{symbol} {session.isoformat()}: the price ratio shows a {factor} split, "
            f"but the unadjusted series does not drop by that factor "
            f"(previous close {previous_close}, first price {first_price})"
        )


def _is_non_split_price_adjustment(step: Decimal, previous_close: Decimal | None, bar: SeriesBar) -> bool:
    if previous_close is None or previous_close <= 0:
        return False
    first_price = _first_unadjusted(bar)
    if first_price is None or first_price >= previous_close:
        return False
    if step < 1:
        return abs((first_price / (previous_close * step)).ln()) < abs(step.ln()) / 2
    if step > 1:
        fwd = Fraction(step).limit_denominator(MAX_FORWARD_SPLIT_DENOMINATOR)
        if fwd > 1 and abs(Decimal(fwd.numerator) / Decimal(fwd.denominator) / step - 1) <= RATIO_TOLERANCE:
            return False
        return abs((first_price * step / previous_close).ln()) < abs(step.ln()) / 2
    return False


def _resolve_ratio_step(
    symbol: str, session: date, step: Decimal, previous_close: Decimal | None, bar: SeriesBar
) -> SplitEvent | None:
    if _is_non_split_price_adjustment(step, previous_close, bar):
        return None
    factor = _snap(symbol, session, step)
    if previous_close is None:
        raise SplitDerivationError(f"{symbol} {session.isoformat()}: no unadjusted close before the split")
    _confirm(symbol, session, factor, previous_close, bar)
    return SplitEvent(symbol, session, factor.numerator, factor.denominator)


def derive_split_events(symbol: str, bars: Sequence[SeriesBar]) -> tuple[SplitEvent, ...]:
    ordered = sorted(bars, key=lambda bar: bar.session)
    if len({bar.session for bar in ordered}) != len(ordered):
        raise SplitDerivationError(f"{symbol}: two bars for one session")
    events: list[SplitEvent] = []
    anchor: Decimal | None = None
    latest_ratio: Decimal | None = None
    previous_close: Decimal | None = None
    first_paired: date | None = None
    last_paired: date | None = None
    for bar in ordered:
        if _positive(bar.unadjusted_close) and _positive(bar.split_adjusted_close):
            assert bar.unadjusted_close is not None and bar.split_adjusted_close is not None
            ratio = bar.unadjusted_close / bar.split_adjusted_close
            if anchor is not None and abs(anchor / ratio - 1) > RATIO_TOLERANCE:
                if (event := _resolve_ratio_step(symbol, bar.session, anchor / ratio, previous_close, bar)) is not None:
                    events.append(event)
                anchor = ratio
            elif anchor is None:
                anchor = ratio
            latest_ratio = ratio
            first_paired = first_paired or bar.session
            last_paired = bar.session
        if _positive(bar.unadjusted_close):
            previous_close = bar.unadjusted_close
    unadjusted = [bar.session for bar in ordered if _positive(bar.unadjusted_close)]
    if unadjusted and (
        first_paired is None or last_paired is None or unadjusted[0] < first_paired or unadjusted[-1] > last_paired
    ):
        raise SplitDerivationError(
            f"{symbol}: the unadjusted series spans {unadjusted[0].isoformat()}..{unadjusted[-1].isoformat()}, "
            f"the sessions with both series span {first_paired}..{last_paired}; "
            "a split outside the second span cannot be derived"
        )
    if latest_ratio is not None and abs(latest_ratio - 1) > RATIO_TOLERANCE:
        raise SplitDerivationError(
            f"{symbol}: the latest unadjusted/split-adjusted ratio is {latest_ratio}, not 1; "
            "the two series are not on one share basis"
        )
    return tuple(events)


def split_factor_between(events: Sequence[SplitEvent], *, after: date, through: date) -> Fraction:
    factor = Fraction(1)
    for event in events:
        if after < event.ex_date <= through:
            factor *= event.factor
    return factor


__all__ = [
    "MAX_FACTOR_DENOMINATOR",
    "RATIO_TOLERANCE",
    "SeriesBar",
    "SplitDerivationError",
    "SplitEvent",
    "derive_split_events",
    "split_factor_between",
]
