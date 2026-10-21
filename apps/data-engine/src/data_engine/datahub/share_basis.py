"""The share basis of a filed share count, and the close it multiplies (#1257, #1262).

A filed share count is on the share basis of one date, its basis date. A close after a stock
split is on the post-split basis. Their product is a market value only when both are on one
basis. Two writers multiply a filed count by a close: the history projector
(``datahub.strategy_history``) and the live strategy seed (``datahub.strategy_bridge``). Both
use this module, so the two cannot disagree on what a basis date is or how a split applies.

* ``share_count_as_of`` classes a count by the stored entry that carries it. The basis date of
  a cover-page count is its own date. The basis date of a statement count is its filing date,
  because the filing restates it for an earlier split (SAB Topic 4C).
* ``on_close_basis`` multiplies a count by every split with an ex-date after its basis date
  and on or before the close's session. The product is knowable once both the count and the
  close are.

The projector reads its close from the stored unadjusted series, so the series always hold its
session. The live close comes from the capture. ``live_on_close_basis`` therefore checks what
the stored series can prove, and it fails closed. An issuer whose splits are unknown between
the basis date and the close's session gets no market value, with a named reason. The live
path never falls back to the filed count.

The stored series may end one session before the close's session (``MAX_BAR_LAG_SESSIONS``):
the market-data refresh can run before the vendor lists that day's bar. Then the live close must
stay within a factor of ``CLOSE_JUMP_LIMIT`` (1.5) of the last stored unadjusted close. The
smallest common split, 3-for-2, moves a close by 1.5. A one-day move to x1.5 or more, or to x0.667
or less, is a split or an event, and the stored series cannot tell which. Then the seed excludes
the issuer for that night.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, NamedTuple

from factors.backtest.corporate_actions import SplitDerivationError, SplitEvent, split_factor_between
from truealpha_contracts.concept_mapping import ConceptMappingRuleset, ConceptRef

from data_engine.datahub.market_prices import oldest_session_within_lag
from data_engine.datahub.price_series import DailySeries, split_events_of

#: The input key of the share count that multiplies a close into a market value.
SHARES_KEY = "shares_outstanding"
#: The ruleset fields whose concepts can produce the share count (``build_bundle``).
_SHARE_COUNT_FIELDS = (SHARES_KEY, "shares_outstanding_last_resort")
#: How a share count was classed (``ShareBasis.kind``).
COVER, STATEMENT, UNRECOGNISED = "cover", "statement", "unrecognised"


class ShareBasis(NamedTuple):
    """The date whose share basis a filed share count is on, and how the count was classed."""

    as_of: date
    kind: str


def share_count_concepts(ruleset: ConceptMappingRuleset) -> tuple[tuple[ConceptRef, ...], tuple[ConceptRef, ...]]:
    """(cover concepts, statement concepts) of the share count. Cover is the ``dei`` (Document and
    Entity Information) taxonomy; every other concept of the share-count fields is a statement figure."""
    concepts = [
        concept
        for field in _SHARE_COUNT_FIELDS
        if (mapping := ruleset.mapping_for(field)) is not None
        for concept in mapping.concepts
    ]
    return (
        tuple(concept for concept in concepts if concept.taxonomy == "dei"),
        tuple(concept for concept in concepts if concept.taxonomy != "dei"),
    )


def _carries(
    documents: Sequence[Mapping[str, Any]],
    concepts: Sequence[ConceptRef],
    *,
    accession: str,
    period_end: str,
    value: Decimal,
) -> bool:
    """True when a stored entry of one of ``concepts`` has this accession, period end and value."""
    for facts in documents:
        for concept in concepts:
            units = facts.get("facts", {}).get(concept.taxonomy, {}).get(concept.concept, {}).get("units", {})
            for entries in units.values():
                for item in entries:
                    if item.get("accn") != accession or item.get("end") != period_end or item.get("val") is None:
                        continue
                    try:
                        if Decimal(str(item["val"])) == value:
                            return True
                    except InvalidOperation:
                        continue
    return False


def share_count_as_of(
    entry: object,
    value: Decimal,
    documents: Sequence[Mapping[str, Any]],
    *,
    cover_concepts: Sequence[ConceptRef],
    statement_concepts: Sequence[ConceptRef],
    filed_fallback: date,
) -> ShareBasis:
    """The share basis of a filed share count, from the stored entry that carries it.

    A cover-page count is a count as of its own date, its ``period_end``. A statement count is a
    balance-sheet count or a weighted average. Its filing restates it for an earlier split (SAB
    Topic 4C), so it is on the basis of the filing date. The vintage names the filing and the
    period end, not the concept, so the stored document decides. Only an entry with the same
    accession, the same period end AND the same value classifies the count. A 20-F can hold a
    cover count and a restated balance-sheet count of one date under one accession.

    A count that no stored entry carries is UNRECOGNISED. It falls back to its filing date, which
    under-adjusts a cover count; the projector counts each case, so the summary shows how often.
    """
    if not isinstance(entry, Mapping) or not entry.get("filed"):
        return ShareBasis(filed_fallback, UNRECOGNISED)
    filed = date.fromisoformat(str(entry["filed"]))
    accession, period_end = entry.get("accession"), entry.get("period_end")
    if not accession or not period_end:
        return ShareBasis(filed, UNRECOGNISED)
    if _carries(documents, cover_concepts, accession=str(accession), period_end=str(period_end), value=value):
        return ShareBasis(date.fromisoformat(str(period_end)), COVER)
    if _carries(documents, statement_concepts, accession=str(accession), period_end=str(period_end), value=value):
        return ShareBasis(filed, STATEMENT)
    return ShareBasis(filed, UNRECOGNISED)


class OnCloseBasis(NamedTuple):
    """A share count on the share basis of a close, and the factor that put it there."""

    value: Decimal
    knowable_at: datetime
    #: The product of the split factors applied. 1 means the count is unchanged.
    factor: Fraction


def on_close_basis(
    value: Decimal,
    knowable_at: datetime,
    *,
    basis_date: date,
    close_session: date,
    close_knowable_at: datetime,
    splits: Sequence[SplitEvent],
) -> OnCloseBasis:
    """The count on the share basis of the close of ``close_session``.

    The filed count is on the basis of ``basis_date`` (``share_count_as_of``). A split with an
    ex-date after that date and on or before the close's session multiplies it. The product is
    knowable once both the count and the close are. Without such a split, the count and its
    knowable time are returned unchanged.
    """
    factor = split_factor_between(splits, after=basis_date, through=close_session)
    if factor == 1:
        return OnCloseBasis(value, knowable_at, factor)
    adjusted = value * Decimal(factor.numerator) / Decimal(factor.denominator)
    return OnCloseBasis(adjusted, max(knowable_at, close_knowable_at), factor)


# -- the live path: a close that the stored series did not supply ------------------------------

#: The reasons the live path names when it cannot put a share count on the basis of its close.
NO_SPLIT_ADJUSTED_SERIES = "no_split_adjusted_series"
NO_UNADJUSTED_SERIES = "no_unadjusted_series"
SPLIT_DERIVATION_FAILED = "split_derivation_failed"
SERIES_DO_NOT_COVER = "split_series_do_not_cover_the_window"
LISTINGS_DISAGREE = "listings_disagree_on_splits"
SHARE_COUNT_UNDATED = "share_count_undated"
SHARE_BASIS_AMBIGUOUS = "share_basis_ambiguous"
CLOSE_JUMP_BEYOND_SERIES = "close_jump_beyond_series"
#: Every reason above. `factors.production_topt.ToptCoreReasonCode` registers the same strings
#: (#1277), so a core result names the cause that the live path found. A test holds the two equal.
LIVE_UNAVAILABLE_REASONS = (
    NO_SPLIT_ADJUSTED_SERIES,
    NO_UNADJUSTED_SERIES,
    SPLIT_DERIVATION_FAILED,
    SERIES_DO_NOT_COVER,
    LISTINGS_DISAGREE,
    SHARE_COUNT_UNDATED,
    SHARE_BASIS_AMBIGUOUS,
    CLOSE_JUMP_BEYOND_SERIES,
)

#: The smallest common split, 3-for-2, moves a close by the factor 1.5. When the stored series end
#: before the close's session, a close at x1.5 or more, or at x0.667 or less, of the last stored
#: close can follow a split that the series do not show yet. Both limits exclude the issuer.
CLOSE_JUMP_LIMIT = Decimal("1.5")


class Unavailable(NamedTuple):
    """Why a share count cannot be put on the basis of its close. The issuer gets no market value."""

    reason: str
    detail: str


@dataclass(frozen=True)
class SplitHistory:
    """The splits that one symbol's two stored daily series show.

    A split with an ex-date in ``(after, through]`` shows as a step of the price ratio. It shows
    only when both series hold a session at or before ``after`` and one at or after ``through``.
    """

    symbol: str
    events: tuple[SplitEvent, ...]
    first_session: date
    last_session: date
    #: The unadjusted close of ``last_session``: the last close the stored series know.
    last_unadjusted_close: Decimal


def _positive_close(bar: tuple[Decimal | None, Decimal | None] | None) -> bool:
    return bar is not None and bar[1] is not None and bar[1] > 0


def split_history(
    symbol: str, unadjusted: DailySeries, split_adjusted: DailySeries
) -> SplitHistory | Unavailable | None:
    """The split history of one symbol, an ``Unavailable`` when its series cannot prove one, or None.

    None means the store holds no bar of the symbol in either series: the symbol adds nothing.
    A symbol with only one series cannot show its splits. A ``SplitDerivationError`` (an
    ambiguous ratio step) makes this symbol unavailable and leaves every other symbol alone.
    """
    raw, adjusted = unadjusted.get(symbol, {}), split_adjusted.get(symbol, {})
    if not raw and not adjusted:
        return None
    if not adjusted:
        return Unavailable(NO_SPLIT_ADJUSTED_SERIES, f"{symbol}: unadjusted bars without split-adjusted bars")
    if not raw:
        return Unavailable(NO_UNADJUSTED_SERIES, f"{symbol}: split-adjusted bars without unadjusted bars")
    try:
        events = split_events_of(symbol, unadjusted, split_adjusted)
    except SplitDerivationError as error:
        return Unavailable(SPLIT_DERIVATION_FAILED, str(error))
    paired = sorted(day for day in raw if _positive_close(raw[day]) and _positive_close(adjusted.get(day)))
    if not paired:
        return Unavailable(SPLIT_DERIVATION_FAILED, f"{symbol}: no session holds a close in both series")
    last_close = raw[paired[-1]][1]
    assert last_close is not None  # `_positive_close` admitted the session
    return SplitHistory(symbol, events, paired[0], paired[-1], last_close)


@dataclass(frozen=True)
class LiveShareCount:
    """One issuer's filed share count and the captured close it multiplies."""

    shares: Decimal
    knowable_at: datetime
    #: The payload's vintage entry of the count: its filing date, its own date and its accession.
    entry: object
    close_session: date
    close_knowable_at: datetime
    #: The captured close the count multiplies.
    close: Decimal


def _entry_date(entry: object, key: str) -> date | None:
    """The ISO date that one key of the vintage entry holds, or None when it holds no valid date."""
    if not isinstance(entry, Mapping) or not entry.get(key):
        return None
    try:
        return date.fromisoformat(str(entry[key]))
    except ValueError:
        return None


def _close_side_gap(history: SplitHistory, count: LiveShareCount) -> Unavailable | None:
    """None when the series of one listing speak for the close's session, else the named reason.

    Series that hold the close's session speak for it. Series that end at most
    ``MAX_BAR_LAG_SESSIONS`` sessions before it speak for it only when the live close stayed
    within a factor of ``CLOSE_JUMP_LIMIT`` of the last stored close. A close on either limit is
    excluded: a 3-for-2 split lands on it exactly.
    """
    close = count.close_session
    if history.last_session >= close:
        return None
    if history.last_session < oldest_session_within_lag(close):
        return Unavailable(
            SERIES_DO_NOT_COVER,
            f"{history.symbol}: both series hold {history.first_session}..{history.last_session}; "
            f"the close is of {close}",
        )
    last = history.last_unadjusted_close
    if count.close * CLOSE_JUMP_LIMIT > last and count.close < last * CLOSE_JUMP_LIMIT:
        return None
    return Unavailable(
        CLOSE_JUMP_BEYOND_SERIES,
        f"{history.symbol}: the close of {close} is {count.close} and the last stored close, of "
        f"{history.last_session}, is {last}: ratio {count.close / last:.4f}, not inside the limit "
        f"{CLOSE_JUMP_LIMIT}; a split or an event, and the stored series cannot tell which",
    )


def live_on_close_basis(
    count: LiveShareCount,
    histories: Sequence[SplitHistory | Unavailable | None],
    *,
    documents: Callable[[], Sequence[Mapping[str, Any]]],
    ruleset: Callable[[], ConceptMappingRuleset],
) -> OnCloseBasis | Unavailable:
    """The live count on the basis of its close, or the named reason why the issuer has no market value.

    ``histories`` holds one entry per listing of the issuer. A listing without stored bars adds
    nothing, and the listings with stored bars must agree. The basis date is the count's own
    date for a cover count and its filing date for a statement count. Only a split between those
    two dates makes the class matter. Then the stored document classes the count
    (``share_count_as_of``); ``documents`` and ``ruleset`` are read only in that case.
    """
    gaps = [history for history in histories if isinstance(history, Unavailable)]
    if gaps:
        return Unavailable(gaps[0].reason, "; ".join(gap.detail for gap in gaps))
    known = [history for history in histories if isinstance(history, SplitHistory)]
    if not known:
        return Unavailable(NO_SPLIT_ADJUSTED_SERIES, "no listing of the issuer holds stored daily series")
    own_date, filed = _entry_date(count.entry, "period_end"), _entry_date(count.entry, "filed")
    if own_date is None or filed is None:
        named = dict(count.entry) if isinstance(count.entry, Mapping) else {}
        return Unavailable(
            SHARE_COUNT_UNDATED,
            f"the share count needs a valid own date and filing date; the payload names "
            f"period_end {named.get('period_end')!r} and filed {named.get('filed')!r}",
        )
    earliest, latest = min(own_date, filed), max(own_date, filed)
    close = count.close_session
    for history in known:
        if history.first_session > earliest:
            return Unavailable(
                SERIES_DO_NOT_COVER,
                f"{history.symbol}: both series start on {history.first_session}; the count's basis can be {earliest}",
            )
        gap = _close_side_gap(history, count)
        if gap is not None:
            return gap
    factors = {
        (
            split_factor_between(h.events, after=earliest, through=close),
            split_factor_between(h.events, after=latest, through=close),
        )
        for h in known
    }
    if len(factors) > 1:
        return Unavailable(LISTINGS_DISAGREE, ", ".join(sorted(h.symbol for h in known)) + " show different splits")
    ((from_earliest, from_latest),) = factors
    basis_date = latest
    if from_earliest != from_latest:
        cover_concepts, statement_concepts = share_count_concepts(ruleset())
        basis = share_count_as_of(
            count.entry,
            count.shares,
            documents(),
            cover_concepts=cover_concepts,
            statement_concepts=statement_concepts,
            filed_fallback=filed,
        )
        if basis.kind == UNRECOGNISED:
            return Unavailable(
                SHARE_BASIS_AMBIGUOUS,
                f"a split lies between the count's date {own_date} and its filing {filed}, "
                "and no stored document classes the count as a cover or a statement figure",
            )
        basis_date = basis.as_of
    return on_close_basis(
        count.shares,
        count.knowable_at,
        basis_date=basis_date,
        close_session=close,
        close_knowable_at=count.close_knowable_at,
        splits=known[0].events,
    )


__all__ = [
    "CLOSE_JUMP_BEYOND_SERIES",
    "CLOSE_JUMP_LIMIT",
    "COVER",
    "LISTINGS_DISAGREE",
    "LIVE_UNAVAILABLE_REASONS",
    "NO_SPLIT_ADJUSTED_SERIES",
    "NO_UNADJUSTED_SERIES",
    "SERIES_DO_NOT_COVER",
    "SHARES_KEY",
    "SHARE_BASIS_AMBIGUOUS",
    "SHARE_COUNT_UNDATED",
    "SPLIT_DERIVATION_FAILED",
    "STATEMENT",
    "UNRECOGNISED",
    "LiveShareCount",
    "OnCloseBasis",
    "ShareBasis",
    "SplitHistory",
    "Unavailable",
    "live_on_close_basis",
    "on_close_basis",
    "share_count_as_of",
    "share_count_concepts",
    "split_history",
]
