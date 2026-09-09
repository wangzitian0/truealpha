"""Module 5: the ETF-as-virtual-company consolidation definition (#36 first slice, #727).

init.md §0 question 5 asks whether an ETF "looks like" a healthy company when treated as
one. The answer is a fund-level aggregate of per-issuer factor outputs weighted by the
fund's OWN filed weights (`staging.fund_holding_facts.percent_of_net_assets`, captured
from N-PORT by #63 tranche 1) — never a market-cap ratio the operator computed.

Why this is a definition and not a SQL expression: init.md §1 rule 2 confines the App
layer to deterministic within-row reformatting, and a weight-weighted mean over a fund's
holdings spans two tables and aggregates across rows. #727 recorded the drift — the
weighted gap was computed in `apps/app-web/src/server/mart/fund-valuation.ts` against
that file's own header — and its first acceptance option is this: the aggregate becomes a
module-5 factor output with a `mart` column, and the App reads the column.

Everything the aggregate depends on is named here rather than defaulted in code, so two
runs are comparable only under the same `content_sha256`:

- `weighting` — whose number the weight is. `filed_net_asset_percent` is the fund's own
  filed `pctVal`; nothing else is admissible under init.md §5's confirmed-source rule.
- `renormalization` — a weighted mean over a subset of the fund needs a stated
  denominator. `valued_mass` divides by the weight that actually carried a value, so the
  number reads as "the average over what we could value", and the masses below say how
  much of the fund that was.
- `minimum_resolved_weight` / `minimum_valued_weight` — #36's refusal rule: below these,
  the aggregate is REFUSED rather than published as though it described the fund. A
  thinly-covered fund must not present a confident-looking number.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from truealpha_contracts.common import canonical_sha256


class EtfConsolidationDefinition(BaseModel):
    """A versioned, content-addressed statement of how a fund's holdings consolidate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    factor_key: Literal["etf_virtual_company"] = "etf_virtual_company"
    factor_version: str = Field(pattern=r"^v[0-9]+$")
    #: The fund's own filed percent-of-net-assets. A market-cap ratio is not a fund
    #: weight; `staging.etf_constituent_facts.weight` stays NULL for exactly this reason.
    weighting: Literal["filed_net_asset_percent"] = "filed_net_asset_percent"
    #: The denominator of the weighted mean: the mass that actually carried a value.
    renormalization: Literal["valued_mass"] = "valued_mass"
    #: Percent of net assets (0-100), not a fraction — the same unit the filing states.
    minimum_resolved_weight: Decimal = Field(ge=0, le=100)
    minimum_valued_weight: Decimal = Field(ge=0, le=100)

    @property
    def content_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


#: v0 thresholds. Deliberately low and stated, not tuned: QQQ's valued mass is bounded by
#: how much of the fund has a governed core row on the tick, and the honest first slice
#: publishes the number WITH its coverage mass rather than withholding it until coverage
#: is high. Raising these is a versioned change that a new content_sha256 records — the
#: point of the floors is that a fund with almost nothing valued cannot publish an
#: aggregate at all (#36: "runs below minimum_resolved_weight reject the affected
#: aggregate instead of presenting it as complete").
ETF_CONSOLIDATION_V0 = EtfConsolidationDefinition(
    factor_version="v0",
    minimum_resolved_weight=Decimal("50"),
    minimum_valued_weight=Decimal("20"),
)
