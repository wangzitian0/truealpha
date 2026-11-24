"""Module 6: theme revenue share (#772)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from factors.registry import factor
from factors.types import DataAvailability, FactorResult, UnitFamily, refused_factor_result

_ZERO = Decimal(0)


@dataclass(frozen=True)
class ThemeSegment:
    segment_name: str
    revenue: Decimal
    in_theme: bool | None


@dataclass(frozen=True)
class ThemePurity:
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
        return _ZERO if self.accounted_revenue <= _ZERO else self.unclassified_revenue / self.accounted_revenue

    @property
    def classified_share(self) -> Decimal:
        if self.accounted_revenue <= _ZERO:
            return _ZERO
        return (self.in_theme_revenue + self.out_of_theme_revenue) / self.accounted_revenue

    @property
    def accounted_revenue(self) -> Decimal:
        """The revenue the shares count against: consolidated revenue, or the gross segment sum
        when the segments land gross with an intersegment elimination as the residual (#1851)."""
        return self.consolidated_revenue - min(self.partition_residual, _ZERO)


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
    gross_segments: bool = False,
) -> ThemePurity:
    """`gross_segments`: the partition rule landed the segments gross, with the intersegment
    elimination as the residual. A negative residual of a plain partition is rounding."""

    def refusal(flag: str, total: Decimal) -> ThemePurity:
        return ThemePurity(
            entity_id=entity_id,
            theme=theme,
            result=refused_factor_result("theme_purity", entity_id, as_of, [flag]),
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
        return refusal("nonpositive_consolidated_revenue", consolidated_revenue)
    if not segments:
        return refusal("no_segments", consolidated_revenue)

    in_theme = sum((s.revenue for s in segments if s.in_theme is True), _ZERO)
    out_of_theme = sum((s.revenue for s in segments if s.in_theme is False), _ZERO)
    unclassified = sum((s.revenue for s in segments if s.in_theme is None), _ZERO)

    flags = [
        *(["unclassified_revenue"] if unclassified > _ZERO else []),
        *(["partition_residual"] if partition_residual != _ZERO else []),
    ]
    availability: DataAvailability = (
        "verified" if unclassified == _ZERO and partition_residual == _ZERO else "unverified"
    )
    accounted = consolidated_revenue - min(partition_residual, _ZERO)
    refused = (in_theme + out_of_theme) / accounted < minimum_classified_share
    if refused:
        flags.append("below_minimum_classified_share")
    # Gross segments with an intersegment elimination as the residual (#1851): the elimination
    # belongs to no known segment. The share is a fact only when every segment is in the theme (1)
    # or none is (0); otherwise the row keeps its revenue fields and serves no value.
    gross = gross_segments and partition_residual < _ZERO
    whole = gross and in_theme > _ZERO and out_of_theme == _ZERO and unclassified == _ZERO
    if gross and in_theme > _ZERO and not whole:
        refused = True
        flags.append("elimination_not_allocated")

    return ThemePurity(
        entity_id=entity_id,
        theme=theme,
        result=FactorResult(
            factor="theme_purity",
            entity_id=entity_id,
            value=None if refused else (Decimal(1) if whole else in_theme / consolidated_revenue),
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
