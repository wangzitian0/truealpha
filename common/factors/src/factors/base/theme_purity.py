"""Module 6: theme purity — share of consolidated revenue attributable to a theme (#772)."""

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
        return _ZERO if self.consolidated_revenue <= _ZERO else self.unclassified_revenue / self.consolidated_revenue

    @property
    def classified_share(self) -> Decimal:
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
    refused = (in_theme + out_of_theme) / consolidated_revenue < minimum_classified_share
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
