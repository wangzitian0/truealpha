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

SHARES_KEY = "shares_outstanding"

_SHARE_COUNT_FIELDS = (SHARES_KEY, "shares_outstanding_last_resort")

COVER, STATEMENT, UNRECOGNISED = "cover", "statement", "unrecognised"


class ShareBasis(NamedTuple):
    as_of: date
    kind: str


def share_count_concepts(ruleset: ConceptMappingRuleset) -> tuple[tuple[ConceptRef, ...], tuple[ConceptRef, ...]]:
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
    value: Decimal
    knowable_at: datetime

    factor: Fraction


def on_close_basis(
    value: Decimal,
    knowable_at: datetime,
    *,
    basis_date: date,
    close_session: date,
    close_knowable_at: datetime,
    splits: Sequence[SplitEvent],
    listing_units_per_share: Decimal | int | None,
    shares_basis: str | None = None,
) -> OnCloseBasis | Unavailable:
    if shares_basis == MISSING_CLASS_RATIO:
        return Unavailable(
            MISSING_CLASS_RATIO,
            "the multi-class share count has no proven class conversion ratio",
        )
    if listing_units_per_share is None or isinstance(listing_units_per_share, bool):
        return Unavailable(
            MISSING_LISTING_UNIT_RATIO,
            "the share count has no proven listing unit ratio",
        )
    ratio = Decimal(listing_units_per_share)
    if ratio <= 0:
        return Unavailable(
            MISSING_LISTING_UNIT_RATIO,
            f"the listing unit ratio must be positive; got {listing_units_per_share!r}",
        )
    in_listed_units = value if ratio == 1 else value / ratio
    factor = split_factor_between(splits, after=basis_date, through=close_session)
    if factor == 1:
        return OnCloseBasis(in_listed_units, knowable_at, factor)
    adjusted = in_listed_units * Decimal(factor.numerator) / Decimal(factor.denominator)
    return OnCloseBasis(adjusted, max(knowable_at, close_knowable_at), factor)


def listing_units_per_share_of(payload: Mapping[str, Any]) -> Decimal | None:
    raw = payload.get("listing_units_per_share")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        ratio = Decimal(str(raw))
    except Exception:  # noqa: BLE001
        return None
    return ratio if ratio > 0 else None


NO_SPLIT_ADJUSTED_SERIES = "no_split_adjusted_series"
NO_UNADJUSTED_SERIES = "no_unadjusted_series"
SPLIT_DERIVATION_FAILED = "split_derivation_failed"
SERIES_DO_NOT_COVER = "split_series_do_not_cover_the_window"
LISTINGS_DISAGREE = "listings_disagree_on_splits"
SHARE_COUNT_UNDATED = "share_count_undated"
SHARE_BASIS_AMBIGUOUS = "share_basis_ambiguous"
CLOSE_JUMP_BEYOND_SERIES = "close_jump_beyond_series"
MISSING_LISTING_UNIT_RATIO = "missing_listing_unit_ratio"
MISSING_CLASS_RATIO = "missing_class_ratio"


CLOSE_JUMP_LIMIT = Decimal("1.5")


class Unavailable(NamedTuple):
    reason: str
    detail: str


@dataclass(frozen=True)
class SplitHistory:
    symbol: str
    events: tuple[SplitEvent, ...]
    first_session: date
    last_session: date

    last_unadjusted_close: Decimal


def _positive_close(bar: tuple[Decimal | None, Decimal | None] | None) -> bool:
    return bar is not None and bar[1] is not None and bar[1] > 0


def split_history(
    symbol: str, unadjusted: DailySeries, split_adjusted: DailySeries
) -> SplitHistory | Unavailable | None:
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
    assert last_close is not None
    return SplitHistory(symbol, events, paired[0], paired[-1], last_close)


@dataclass(frozen=True)
class LiveShareCount:
    shares: Decimal
    knowable_at: datetime

    entry: object
    close_session: date
    close_knowable_at: datetime

    close: Decimal


def _entry_date(entry: object, key: str) -> date | None:
    if not isinstance(entry, Mapping) or not entry.get(key):
        return None
    try:
        return date.fromisoformat(str(entry[key]))
    except ValueError:
        return None


def _close_side_gap(history: SplitHistory, count: LiveShareCount) -> Unavailable | None:
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
    listing_units_per_share: Decimal | int | None,
    shares_basis: str | None = None,
    documents: Callable[[], Sequence[Mapping[str, Any]]],
    ruleset: Callable[[], ConceptMappingRuleset],
) -> OnCloseBasis | Unavailable:
    if shares_basis == MISSING_CLASS_RATIO:
        return Unavailable(
            MISSING_CLASS_RATIO,
            "the multi-class share count has no proven class conversion ratio",
        )
    if (
        listing_units_per_share is None
        or isinstance(listing_units_per_share, bool)
        or Decimal(listing_units_per_share) <= 0
    ):
        return Unavailable(
            MISSING_LISTING_UNIT_RATIO,
            "the share count has no proven listing unit ratio",
        )
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
        listing_units_per_share=listing_units_per_share,
        shares_basis=shares_basis,
    )


__all__ = [
    "CLOSE_JUMP_BEYOND_SERIES",
    "CLOSE_JUMP_LIMIT",
    "COVER",
    "LISTINGS_DISAGREE",
    "MISSING_CLASS_RATIO",
    "MISSING_LISTING_UNIT_RATIO",
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
    "listing_units_per_share_of",
    "live_on_close_basis",
    "on_close_basis",
    "share_count_as_of",
    "share_count_concepts",
    "split_history",
]
