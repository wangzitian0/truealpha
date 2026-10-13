"""Split events derived from two stored price series (#758, M3).

No corporate-action source exists. ``staging.market_prices_daily`` holds two series for each
symbol: unadjusted bars (``adjust = 'none'``) and split-adjusted bars (``adjust = 'splits'``).
The split-adjusted series is expressed on the share basis of its fetch. So on each session,
the ratio ``unadjusted close / split-adjusted close`` is the product of the factors of every
split with a later ex-date. The ratio is constant between two splits. It steps down on each
ex-date. A step from ``r0`` to ``r1`` is one split with the factor ``r0 / r1``: ``r0 / r1``
new shares for each old share.

Rules this module enforces:

* A step is a split only when its factor is a simple ratio ``p/q`` with ``q <= 20``. A step
  that does not round to such a ratio stops the run. A slow drift of the ratio (for example a
  series that is also adjusted for dividends) has no simple ratio, so it also stops the run.
* The unadjusted series must confirm each split. On the ex-date, the unadjusted price drops by
  the factor. Multiplied by the factor, the move from the previous close to the ex-date open
  must be smaller than half the factor in log terms. A step that the unadjusted series does
  not show (for example two vintages of the split-adjusted series fetched before and after a
  split) stops the run. Nothing guesses a corporate action.
* The ratio on the latest session must be 1. The split-adjusted series is on the share basis
  of its fetch, so a latest ratio other than 1 means the two series disagree.
* Both series must cover the same span. A split outside the span of the split-adjusted series
  leaves no step in the ratio, so an unadjusted session before the first or after the last
  session of both series stops the run.

Dividends have no source. A return computed with these events is a price return only.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction

#: The largest relative difference between two ratios that is rounding noise, not a split.
RATIO_TOLERANCE = Decimal("0.005")
#: The largest denominator of a split factor (a 3:2 split has the denominator 2).
MAX_FACTOR_DENOMINATOR = 20


class SplitDerivationError(ValueError):
    """The two price series do not identify a split without ambiguity."""


@dataclass(frozen=True)
class SeriesBar:
    """One session of one symbol, read from both stored series."""

    session: date
    unadjusted_open: Decimal | None
    unadjusted_close: Decimal | None
    split_adjusted_close: Decimal | None


@dataclass(frozen=True)
class SplitEvent:
    """One split: ``numerator`` new shares for each ``denominator`` old shares from ``ex_date``."""

    symbol: str
    ex_date: date
    numerator: int
    denominator: int

    @property
    def factor(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)


def _positive(value: Decimal | None) -> bool:
    return value is not None and value > 0


def _snap(symbol: str, session: date, step: Decimal) -> Fraction:
    """The simple ratio ``p/q`` a ratio step rounds to. Raise when there is none."""
    exact = Fraction(step)
    if exact >= 1:
        candidate = exact.limit_denominator(MAX_FACTOR_DENOMINATOR)
    else:
        candidate = 1 / (1 / exact).limit_denominator(MAX_FACTOR_DENOMINATOR)
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
    """Raise unless the unadjusted series shows the split on its ex-date."""
    first_price = bar.unadjusted_open if _positive(bar.unadjusted_open) else bar.unadjusted_close
    if first_price is None or first_price <= 0:
        raise SplitDerivationError(f"{symbol} {session.isoformat()}: no unadjusted price confirms the split")
    factor_decimal = Decimal(factor.numerator) / Decimal(factor.denominator)
    adjusted_move = (first_price * factor_decimal / previous_close).ln()
    if abs(adjusted_move) >= abs(factor_decimal.ln()) / 2:
        raise SplitDerivationError(
            f"{symbol} {session.isoformat()}: the price ratio shows a {factor} split, "
            f"but the unadjusted series does not drop by that factor "
            f"(previous close {previous_close}, first price {first_price})"
        )


def derive_split_events(symbol: str, bars: Sequence[SeriesBar]) -> tuple[SplitEvent, ...]:
    """The splits of one symbol, oldest first. Raise ``SplitDerivationError`` on ambiguity."""
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
            if anchor is not None:
                step = anchor / ratio
                if abs(step - 1) > RATIO_TOLERANCE:
                    factor = _snap(symbol, bar.session, step)
                    if previous_close is None:
                        raise SplitDerivationError(
                            f"{symbol} {bar.session.isoformat()}: no unadjusted close before the split"
                        )
                    _confirm(symbol, bar.session, factor, previous_close, bar)
                    events.append(SplitEvent(symbol, bar.session, factor.numerator, factor.denominator))
                    anchor = ratio
            else:
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


__all__ = [
    "MAX_FACTOR_DENOMINATOR",
    "RATIO_TOLERANCE",
    "SeriesBar",
    "SplitDerivationError",
    "SplitEvent",
    "derive_split_events",
]
