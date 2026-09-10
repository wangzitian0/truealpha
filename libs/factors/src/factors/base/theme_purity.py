"""Module 6: theme purity — what share of an issuer's revenue is the theme (#772).

init.md §0 question 6: *"Who's the purest name under a given theme?"* — revenue-share
ranking. The answer for one issuer is the fraction of its revenue that the theme accounts
for, and the ranking is that fraction across a universe.

Two things this factor deliberately does NOT do, because each is somebody else's job and
mixing them in is how a ranking becomes unfalsifiable:

1. **It does not classify.** Whether a segment belongs to a theme is a judgement — init.md
   §7 module 6 is "LLM-assisted semantic classification of segment revenue" — so the caller
   supplies the classification and this function does deterministic arithmetic over it. A
   factor that both judged and computed could not be checked against a hand calculation.
2. **It does not decide the set is complete.** The segments arrive as an accepted partition
   (the exhaustive-partition rule owned by `factors.shared.extraction`, #803): their parts
   already account for the issuer's
   consolidated revenue within a stated tolerance. This factor consumes that guarantee and
   carries its residual through, rather than re-deriving it.

Why the second matters more than it reads: a missed segment silently RAISES every remaining
segment's share. If the denominator were `sum(segments I happen to have)`, an incomplete
extraction would produce a *higher* purity for the issuer whose data is worst — exactly
backwards, and the number would look ordinary. The denominator is therefore the CONSOLIDATED
total the partition was accepted against, never the sum of the classified parts.

`unclassified_share` exists for the same reason: a theme share of 0.6 with 0.35
unclassified is a different claim from 0.6 with 0.0 unclassified, and a ranking that cannot
tell them apart is ranking its own coverage.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from factors.registry import factor
from factors.types import DataAvailability, FactorResult, UnitFamily

_ZERO = Decimal(0)


@dataclass(frozen=True)
class ThemeSegment:
    """One segment of an accepted partition, with the caller's theme verdict.

    `in_theme` is `None` when the classifier declined — distinct from `False`. A declined
    segment is unclassified revenue, which lowers confidence in the share; a segment judged
    NOT in the theme is classified, and lowers the share itself. Collapsing the two would
    let a silent classifier look like a confident "no".
    """

    segment_name: str
    revenue: Decimal
    in_theme: bool | None


@dataclass(frozen=True)
class ThemePurity:
    """The share, and everything a reader needs to distrust it.

    `FactorResult` carries one scalar; these masses ride alongside for the same reason the
    ETF consolidation's do — a share without its coverage is a number without a claim.
    """

    entity_id: str
    theme: str
    result: FactorResult
    consolidated_revenue: Decimal
    in_theme_revenue: Decimal
    out_of_theme_revenue: Decimal
    unclassified_revenue: Decimal
    partition_residual: Decimal
    segments: int

    @property
    def theme_share(self) -> Decimal | None:
        return self.result.value

    @property
    def unclassified_share(self) -> Decimal:
        if self.consolidated_revenue <= _ZERO:
            return _ZERO
        return self.unclassified_revenue / self.consolidated_revenue

    @property
    def classified_share(self) -> Decimal:
        """How much of the denominator carries a judgement either way.

        Not `1 - unclassified_share`: the parts may miss the consolidated total by
        `partition_residual`, and revenue that was never extracted was never classified
        either. Both gaps count against the share's standing.
        """
        if self.consolidated_revenue <= _ZERO:
            return _ZERO
        return (self.in_theme_revenue + self.out_of_theme_revenue) / self.consolidated_revenue


@factor("theme_purity", kind="base", module=6)
def theme_purity(
    segments: Sequence[ThemeSegment],
    *,
    entity_id: str,
    theme: str,
    as_of: datetime,
    consolidated_revenue: Decimal | None,
    partition_residual: Decimal = _ZERO,
    confidence: Decimal = _ZERO,
    minimum_classified_share: Decimal = _ZERO,
) -> ThemePurity:
    """The theme's share of CONSOLIDATED revenue, with the coverage that qualifies it.

    `consolidated_revenue` is the denominator and is required: it is the total the partition
    was accepted against, and using the classified parts instead would reward an incomplete
    extraction with a higher purity. `None` (an issuer with no revenue fact) refuses rather
    than divides.

    `partition_residual` travels from the accepted partition so a consumer can see how much
    of the whole the parts missed; it does not change the share, because the denominator is
    already the whole.

    `minimum_classified_share` is the caller's governed floor (module 5's
    `EtfConsolidationDefinition` states its coverage floors the same way). Below it the share
    is REFUSED rather than published: a purity of 0.58 computed while 42% of the issuer's
    revenue was never judged is not a purity, and publishing it ranks an issuer the
    classifier could not read against issuers it could. The masses stay on the record so a
    reader can see why. Default zero: a caller that states no floor gets no floor, rather
    than one this function invented.
    """

    def refusal(flag: str, total: Decimal) -> ThemePurity:
        return ThemePurity(
            entity_id=entity_id,
            theme=theme,
            result=FactorResult(
                factor="theme_purity",
                entity_id=entity_id,
                value=None,
                unit_family=UnitFamily.RATIO,
                confidence=_ZERO,
                as_of=as_of,
                data_availability="unverified",
                flags=[flag],
            ),
            consolidated_revenue=total,
            in_theme_revenue=_ZERO,
            out_of_theme_revenue=_ZERO,
            unclassified_revenue=_ZERO,
            partition_residual=partition_residual,
            segments=len(segments),
        )

    if consolidated_revenue is None:
        return refusal("no_consolidated_revenue", _ZERO)
    if consolidated_revenue <= _ZERO:
        # A non-positive denominator is not a small share, it is no share at all.
        return refusal("nonpositive_consolidated_revenue", consolidated_revenue)
    if not segments:
        return refusal("no_segments", consolidated_revenue)

    in_theme = sum((s.revenue for s in segments if s.in_theme is True), _ZERO)
    out_of_theme = sum((s.revenue for s in segments if s.in_theme is False), _ZERO)
    unclassified = sum((s.revenue for s in segments if s.in_theme is None), _ZERO)

    flags: list[str] = []
    if unclassified > _ZERO:
        flags.append("unclassified_revenue")
    if partition_residual != _ZERO:
        flags.append("partition_residual")
    # `verified` requires every segment classified AND the partition exact. Anything else is
    # a share whose denominator is right but whose numerator may not be complete.
    availability: DataAvailability = (
        "verified" if unclassified == _ZERO and partition_residual == _ZERO else "unverified"
    )

    classified = (in_theme + out_of_theme) / consolidated_revenue
    refused = classified < minimum_classified_share
    if refused:
        flags.append("below_minimum_classified_share")

    return ThemePurity(
        entity_id=entity_id,
        theme=theme,
        result=FactorResult(
            factor="theme_purity",
            entity_id=entity_id,
            value=None if refused else in_theme / consolidated_revenue,
            unit_family=UnitFamily.RATIO,
            confidence=_ZERO if refused else confidence,
            as_of=as_of,
            data_availability="unverified" if refused else availability,
            flags=flags,
        ),
        consolidated_revenue=consolidated_revenue,
        in_theme_revenue=in_theme,
        out_of_theme_revenue=out_of_theme,
        unclassified_revenue=unclassified,
        partition_residual=partition_residual,
        segments=len(segments),
    )
