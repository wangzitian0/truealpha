"""SEC company-facts XBRL extraction, generalized across issuers and tags.

The only prior XBRL-tag lookups in this repo were single-tag, single-issuer
probes buried in frozen batch fixtures (`batches/mvp_capture_tiny/e0_slice.py`
hardcodes `GrossProfit` for NVDA only; `batches/mvp_medium_validation/e1_slice.py`
hardcodes one lease-amortization tag for a PLUG restatement test). Neither is a
reusable mapper, and `staging.financial_facts` has no production writer yet
(`db/migrations/0006_bitemporal_lineage.sql`: "empty everywhere: staging
writers land after this"). This module is that mapper's first real slice.

Known pitfall (init.md Section 9, `apps/data-engine/samples/README.md`): XBRL
tags vary by issuer — e.g. some non-financial issuers don't report `GrossProfit`
at all (verified against the sample corpus: META reports no `GrossProfit`
fact, JPM none either but for the documented financial-issuer reason). This
module reports `None` for a genuinely absent tag rather than inventing a
fallback computation (e.g. revenue - cost_of_revenue) that hasn't been
verified correct for this pass — a caller sees an honest gap, not a guess.

Scope: this is the parsing/selection layer only — company-facts JSON in,
a typed point-in-time observation out. It deliberately does not yet write
`staging.financial_facts` rows, since that requires `raw_ref` pointing at a
real `raw.fetches` capture, and no Dagster asset here captures SEC bytes
through that ledger yet (same boundary the existing single-tag probes stop
at). Wiring raw capture + a staging writer + a Dagster asset is separate,
larger follow-up work, not attempted here.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# SEC structured financial data is high-confidence relative to text extraction
# (e.g. the H0 headcount pipeline's filing-text confidence), but not a
# calibrated probability — matches the confidence level used for other
# structured SEC facts in the checked-in golden fixtures.
_STRUCTURED_TAG_CONFIDENCE = Decimal("0.98")


class SecFinancialObservation(BaseModel):
    """One point-in-time XBRL fact, selected and visible as of `knowable_at`.

    Mirrors the shape `truealpha_contracts.models.FinancialFact` will need,
    minus the raw/mapping-version provenance fields a real capture pipeline
    supplies — this type is the parsing-layer output, not the staging row.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_id: str = Field(pattern=r"^issuer\.[a-z0-9]+$")
    metric: str
    value: Decimal
    unit: Literal["USD"] = "USD"
    fiscal_period: str
    accession: str
    form: str
    valid_from: date
    valid_to: date
    knowable_at: datetime
    confidence: Decimal = Field(ge=0, le=1)


def _annual_usd_rows(company_facts: dict[str, Any], tag: str) -> list[dict[str, Any]]:
    try:
        rows = company_facts["facts"]["us-gaap"][tag]["units"]["USD"]
    except KeyError:
        return []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"company-facts schema drifted for tag {tag!r}: USD units must be a list of objects")
    return rows


#: Annual-report forms across domestic and foreign private issuers, plus
#: their amendments (verified against the sample corpus: NICE files 20-F, not
#: 10-K; PLUG has a genuine 10-K/A restatement pair). `filed` already governs
#: PIT visibility, so including amendments is correct, not a look-ahead risk —
#: an amendment's own later `filed` date keeps it invisible before it existed.
_ANNUAL_REPORT_FORMS = frozenset({"10-K", "10-K/A", "20-F", "20-F/A"})


def extract_annual_metric(
    company_facts: dict[str, Any],
    *,
    tag: str,
    metric: str,
    entity_id: str,
    cutoff: datetime,
) -> SecFinancialObservation | None:
    """Select the latest annual (10-K/20-F, full fiscal year) value for `tag`
    visible at `cutoff`, or None if the tag is absent or nothing is visible
    yet — never a fabricated or silently substituted value.

    Selection mirrors the proven pattern in
    `batches.mvp_capture_tiny.e0_slice._latest_annual_gross_profit`
    (filed <= cutoff, annual form, full fiscal year; ties broken by
    (filed, end, accession, value) so replay is deterministic), generalized
    to any tag/metric/issuer instead of one hardcoded triple.
    """

    rows = _annual_usd_rows(company_facts, tag)
    candidates = [
        row
        for row in rows
        if row.get("form") in _ANNUAL_REPORT_FORMS
        and row.get("fp") == "FY"
        and date.fromisoformat(row["filed"]) <= cutoff.date()
    ]
    if not candidates:
        return None
    try:
        selected = max(
            candidates,
            key=lambda row: (
                date.fromisoformat(row["filed"]),
                date.fromisoformat(row["end"]),
                str(row["accn"]),
                Decimal(str(row["val"])),
            ),
        )
        filed = date.fromisoformat(selected["filed"])
        # Duration facts (e.g. GrossProfit) carry "start"; instant/balance-sheet
        # facts (e.g. Assets) don't — they're valid as of a single date.
        valid_from = (
            date.fromisoformat(selected["start"]) if "start" in selected else date.fromisoformat(selected["end"])
        )
        return SecFinancialObservation(
            entity_id=entity_id,
            metric=metric,
            value=Decimal(str(selected["val"])),
            fiscal_period=f"FY{selected['fy']}",
            accession=str(selected["accn"]),
            form=str(selected["form"]),
            valid_from=valid_from,
            valid_to=date.fromisoformat(selected["end"]),
            knowable_at=datetime.combine(filed, time.min, UTC),
            confidence=_STRUCTURED_TAG_CONFIDENCE,
        )
    except (InvalidOperation, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"company-facts schema drifted for tag {tag!r}") from error


def extract_total_assets(
    company_facts: dict[str, Any], *, entity_id: str, cutoff: datetime
) -> SecFinancialObservation | None:
    """`total_assets` per `truealpha_contracts.metrics.METRICS` — the `Assets`
    XBRL tag, present for all issuers in the sample corpus regardless of
    financial/non-financial branch (unlike `GrossProfit`)."""

    return extract_annual_metric(company_facts, tag="Assets", metric="total_assets", entity_id=entity_id, cutoff=cutoff)


def extract_gross_profit(
    company_facts: dict[str, Any], *, entity_id: str, cutoff: datetime
) -> SecFinancialObservation | None:
    """`gross_profit` per `truealpha_contracts.metrics.METRICS` — the
    `GrossProfit` tag. Returns None (not a fallback computation) for issuers
    that don't report it, financial or otherwise; #24's factor is the layer
    that decides what an absent value means for eligibility."""

    return extract_annual_metric(
        company_facts, tag="GrossProfit", metric="gross_profit", entity_id=entity_id, cutoff=cutoff
    )


#: Priority order matters only as a tie-break preference, not a blind first
#: match — see extract_revenue's docstring for why a fixed priority alone is
#: unsafe here.
_REVENUE_TAGS = ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues")


def extract_revenue(
    company_facts: dict[str, Any], *, entity_id: str, cutoff: datetime
) -> SecFinancialObservation | None:
    """`revenue` per `truealpha_contracts.metrics.METRICS`.

    Issuers use different XBRL tags for top-line revenue, and verified
    against the sample corpus, several **stop actively filing under one tag
    and switch to another over time**: NVDA's last
    `RevenueFromContractWithCustomerExcludingAssessedTax` filing is dated
    2022-03-18 (val $26.9B), while its `Revenues` tag continues through
    2026-02-25 (val $215.9B) — a fixed tag priority would silently return
    NVDA's four-year-stale figure. Selection instead prefers whichever
    candidate tag has the most recently *filed* annual value.

    When two candidate tags are equally current but materially disagree —
    verified against ADM, where `Revenues` ($80.3B) and
    `RevenueFromContractWithCustomerExcludingAssessedTax` ($25.0B) are both
    actively filed in the same 10-K, evidently different revenue concepts
    for a commodities-trading issuer, not the same number reported twice —
    this returns `None` rather than guessing which one is "the" revenue.
    An unresolved tag conflict is a real, reportable gap, not a coin flip.
    """

    observations = [
        obs
        for tag in _REVENUE_TAGS
        if (obs := extract_annual_metric(company_facts, tag=tag, metric="revenue", entity_id=entity_id, cutoff=cutoff))
        is not None
    ]
    if not observations:
        return None
    latest_knowable_at = max(obs.knowable_at for obs in observations)
    most_recent = [obs for obs in observations if obs.knowable_at == latest_knowable_at]
    if len({obs.value for obs in most_recent}) == 1:
        return most_recent[0]
    return None
