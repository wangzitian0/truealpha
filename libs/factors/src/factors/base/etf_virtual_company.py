"""Module 5: ETF as a virtual company — the fund-level consolidation of per-issuer
factor outputs (#36 first slice, #727 acceptance option 1).

A BASE factor, per init.md §7 ("modules 1-6 are base factors ... module 7 is a composite
factor"), even though the valuation gap it weights is another factor's materialized
output. The distinction is who loads: a composite reloads mart itself, while this receives
provenance-neutral `(weight, gap, availability, confidence)` tuples projected by the runner
(`data_engine.datahub.production_topt.fund_consolidation`) and cannot see where any of them
came from. Confidence is still bounded by the minimum consumed — a rule composites owe and
this factor keeps anyway, because an aggregate is no better than its weakest line.

One fund, one row. Each holding line arrives with the fund's own filed weight and the
governed run's core-factor output for the listing it resolves to; this function returns
the weighted aggregate plus the three coverage masses that say how much of the fund the
aggregate actually describes.

The three masses are nested, and the arithmetic is stated rather than implied:

    total_weight     every filed line, resolved or not (~100 for a fully-filed N-PORT)
    resolved_weight  the subset whose ISIN resolved to a listing (<= total)
    valued_weight    the subset of THAT whose core row was available (<= resolved)

`unvalued_weight = resolved - valued` and `unresolved_weight = total - resolved` are
returned too, so a reader never has to subtract to discover what was dropped: #36's
"resolved, unresolved, missing-fact, and renormalized weights are explicit and sum
consistently" is a property of the return shape, not of a caller's care.

The weighted mean divides by `valued_weight`, so it reads as "the average across the part
of the fund we could value". Below the definition's floors the value is REFUSED (None
with a flag), never published as if it described the whole fund — a fund with 3% valued
must not render a confident-looking number next to one with 95%.

Confidence is the minimum consumed confidence: an aggregate cannot be more trustworthy
than the weakest line it consumed. The valued mass is reported beside it rather than
folded into it, so a reader can discount by coverage explicitly.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from truealpha_contracts.etf_virtual_company import EtfConsolidationDefinition

from factors.registry import factor
from factors.types import DataAvailability, FactorResult, UnitFamily

#: Below this the mean is arithmetically meaningless, whatever the definition says.
_ZERO = Decimal(0)


@dataclass(frozen=True)
class HoldingLine:
    """One filed N-PORT line, joined to the governed run's core row for its listing.

    `listing_id` is None when the line's ISIN did not resolve — a foreign line, a share
    class the crosswalk has not covered, or a non-equity line. `valuation_gap` is None
    when the listing resolved but the run produced no available core row for it. The two
    cases are distinct on purpose: one is an identity gap, the other a data gap, and #36
    requires both to stay visible instead of collapsing into "missing".
    """

    holding_name: str
    weight: Decimal | None
    listing_id: str | None
    valuation_gap: Decimal | None
    availability: str | None
    confidence: Decimal | None = None

    @property
    def resolved(self) -> bool:
        return self.listing_id is not None

    @property
    def valued(self) -> bool:
        """A line is valued only if it can actually enter the aggregate.

        `weight is not None` is part of that: a filed line whose `pctVal` did not parse
        contributes to neither numerator nor denominator, so counting it as valued would
        overstate `valued_lines` and let its confidence pull `min()` down for a line the
        number does not depend on (review on #727).
        """
        return (
            self.weight is not None
            and self.resolved
            and self.availability == "available"
            and self.valuation_gap is not None
        )


@dataclass(frozen=True)
class FundConsolidation:
    """The fund-level answer plus the coverage that qualifies it.

    `FactorResult` carries one scalar, so the masses ride alongside it here rather than
    being squeezed into flags — the same reason `three_tier_valuation` sends its band
    lookup back to the caller instead of widening the result type.
    """

    fund_id: str
    result: FactorResult
    total_weight: Decimal
    resolved_weight: Decimal
    valued_weight: Decimal
    lines: int
    valued_lines: int

    @property
    def unresolved_weight(self) -> Decimal:
        return self.total_weight - self.resolved_weight

    @property
    def unvalued_weight(self) -> Decimal:
        return self.resolved_weight - self.valued_weight

    @property
    def weighted_valuation_gap(self) -> Decimal | None:
        return self.result.value


def _mass(lines: Sequence[HoldingLine], predicate) -> Decimal:
    return sum((line.weight for line in lines if line.weight is not None and predicate(line)), _ZERO)


@factor("etf_virtual_company", kind="base", module=5)
def consolidate_fund(
    lines: Sequence[HoldingLine],
    *,
    fund_id: str,
    as_of: datetime,
    definition: EtfConsolidationDefinition,
) -> FundConsolidation:
    """Consolidate one fund's filed holdings into a virtual-company valuation gap.

    `as_of` is the cutoff of the governed run whose core rows the lines carry — the
    valuation's time, not the filing's. The holdings' own vintage (report period and
    filing date) is the caller's to record; a line's weight is only admissible here if
    the caller already selected a vintage knowable at this cutoff (#36: "never applies a
    later filing retroactively").
    """
    total = _mass(lines, lambda _: True)
    resolved = _mass(lines, lambda line: line.resolved)
    valued = _mass(lines, lambda line: line.valued)
    valued_lines = [line for line in lines if line.valued]

    def refusal(flag: str) -> FundConsolidation:
        return FundConsolidation(
            fund_id=fund_id,
            result=FactorResult(
                factor="etf_virtual_company",
                entity_id=fund_id,
                value=None,
                unit_family=UnitFamily.RATIO,
                confidence=_ZERO,
                as_of=as_of,
                data_availability="unverified",
                flags=[flag],
            ),
            total_weight=total,
            resolved_weight=resolved,
            valued_weight=valued,
            lines=len(lines),
            valued_lines=len(valued_lines),
        )

    if resolved < definition.minimum_resolved_weight:
        return refusal("resolved_weight_below_minimum")
    if valued < definition.minimum_valued_weight:
        return refusal("valued_weight_below_minimum")
    if valued <= _ZERO:
        # Unreachable while minimum_valued_weight > 0, kept because a future definition
        # may set the floor to zero and a zero denominator is not a policy question.
        return refusal("no_valued_mass")

    weighted = sum(
        (line.weight * line.valuation_gap for line in valued_lines if line.weight is not None),  # type: ignore[operator]
        _ZERO,
    )
    # Confidence: the weakest line that contributed, scaled by nothing else. The valued
    # mass is reported separately rather than folded in — a reader who wants to discount
    # by coverage can, and a number that silently mixes the two explains neither.
    confidences = [line.confidence for line in valued_lines if line.confidence is not None]
    confidence = min(confidences) if confidences else _ZERO
    # Measured against the WHOLE filed mass, not against the resolved subset: a fund with
    # unresolved holdings is described only in part, whatever share of the resolved half
    # was valued. `_status_dimensions` grades source evidence by the same comparison, and
    # the two must not disagree about one fact (review on #727).
    availability: DataAvailability = "verified" if valued >= total else "unverified"
    flags: list[str] = []
    if valued < resolved:
        flags.append("partial_valued_mass")
    if resolved < total:
        flags.append("unresolved_holdings")

    return FundConsolidation(
        fund_id=fund_id,
        result=FactorResult(
            factor="etf_virtual_company",
            entity_id=fund_id,
            value=weighted / valued,
            unit_family=UnitFamily.RATIO,
            confidence=confidence,
            as_of=as_of,
            data_availability=availability,
            flags=flags,
        ),
        total_weight=total,
        resolved_weight=resolved,
        valued_weight=valued,
        lines=len(lines),
        valued_lines=len(valued_lines),
    )
