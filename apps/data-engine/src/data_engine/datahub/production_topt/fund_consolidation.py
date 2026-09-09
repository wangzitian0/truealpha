"""Materialize the module-5 ETF virtual-company row for a governed run (#727, #36).

The producer half of #727's first acceptance option: the fund-level weighted valuation
gap is computed by `factors.base.etf_virtual_company` and written to
`mart.fund_virtual_company` by the tick that produced the core rows it consumed, so the
App can read a column instead of aggregating in its own SQL (init.md §1 rule 2).

Vintage selection is explicit and PIT: the fund's newest holdings filing whose
`transaction_time` (the filing date — when the weights became publicly knowable) is at or
before the run's cutoff. `mart.fund_holdings_valuation` selects newest-per-fund
unconditionally, which is right for a live page and wrong for a replay, so this reads the
vintage-bearing `mart.fund_holdings` and picks the vintage itself (#36: "never applies a
later filing retroactively").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from factors.base.etf_virtual_company import FundConsolidation, HoldingLine, consolidate_fund
from psycopg import Connection
from truealpha_contracts.etf_virtual_company import ETF_CONSOLIDATION_V0, EtfConsolidationDefinition
from truealpha_contracts.execution import AvailabilityStatus, FactorValidationStatus, InputEvidenceStatus

from data_engine.datahub.production_topt.status_dimensions import LOW_CONFIDENCE_FLOOR

#: One fund's lines at the vintage KNOWABLE AT THE CUTOFF, joined to this run's core rows.
#: The vintage is chosen per fund (funds file on their own quarterly cycles), and it is
#: chosen here rather than taken from `mart.fund_holdings_valuation`, which pins the newest
#: vintage unconditionally — correct for a live page, a look-ahead for a replay (#36).
_LINES_SQL = """
with vintage as (
    select distinct on (fund_id) fund_id, report_period, transaction_time
    from mart.fund_holdings_resolved
    where transaction_time <= %(cutoff)s
    order by fund_id, transaction_time desc, report_period desc
)
select h.fund_id,
       coalesce(nullif(h.fund_name, ''), h.fund_id) as fund_name,
       h.report_period,
       h.transaction_time,
       h.holding_name,
       h.percent_of_net_assets,
       h.listing_id,
       core.valuation_gap,
       core.availability,
       core.confidence
from mart.fund_holdings_resolved h
join vintage using (fund_id, report_period, transaction_time)
left join mart.topt_core_result_read core
  on core.listing_id = h.listing_id and core.run_id = %(run_id)s
order by h.fund_id, h.percent_of_net_assets desc nulls last, h.holding_name
"""


@dataclass(frozen=True)
class FundVintage:
    fund_id: str
    fund_name: str
    report_period: Any
    transaction_time: datetime
    lines: tuple[HoldingLine, ...]


def load_fund_vintages(connection: Connection[Any], *, run_id: str, cutoff: datetime) -> tuple[FundVintage, ...]:
    """Every fund with a holdings vintage knowable at `cutoff`, joined to `run_id`'s rows."""
    rows = connection.execute(_LINES_SQL, {"run_id": run_id, "cutoff": cutoff}).fetchall()
    by_fund: dict[str, list[Any]] = {}
    meta: dict[str, tuple[str, Any, datetime]] = {}
    for fund_id, fund_name, report_period, transaction_time, *rest in rows:
        meta.setdefault(str(fund_id), (str(fund_name), report_period, transaction_time))
        by_fund.setdefault(str(fund_id), []).append(rest)
    vintages = []
    for fund_id, line_rows in by_fund.items():
        fund_name, report_period, transaction_time = meta[fund_id]
        lines = tuple(
            HoldingLine(
                holding_name=str(holding_name),
                weight=weight,
                listing_id=None if listing_id is None else str(listing_id),
                valuation_gap=valuation_gap,
                availability=None if availability is None else str(availability),
                confidence=confidence,
            )
            for holding_name, weight, listing_id, valuation_gap, availability, confidence in line_rows
        )
        vintages.append(
            FundVintage(
                fund_id=fund_id,
                fund_name=fund_name,
                report_period=report_period,
                transaction_time=transaction_time,
                lines=lines,
            )
        )
    return tuple(vintages)


def _status_dimensions(
    consolidation: FundConsolidation,
) -> tuple[AvailabilityStatus, InputEvidenceStatus, FactorValidationStatus]:
    """The three §8 dimensions, derived from what this aggregate actually consumed.

    A refused aggregate is `unavailable` — the reason travels in `reason_codes`, which is
    the flag the factor refused on. A published aggregate whose valued mass is partial is
    `degraded` source evidence: every line it used carries evidence, but the fund it
    claims to describe is only partly represented, and that is exactly the "an asserted
    input has no evidence on the row" shape #747 grades down.
    """
    if consolidation.result.value is None:
        return (AvailabilityStatus.UNAVAILABLE, InputEvidenceStatus.DEGRADED, FactorValidationStatus.NOT_EVALUATED)
    availability = (
        AvailabilityStatus.LOW_CONFIDENCE
        if consolidation.result.confidence < LOW_CONFIDENCE_FLOOR
        else AvailabilityStatus.AVAILABLE
    )
    evidence = (
        InputEvidenceStatus.VERIFIED
        if consolidation.valued_weight >= consolidation.total_weight
        else InputEvidenceStatus.DEGRADED
    )
    # #65 owns the holdout verdict; module 5 has no sealed record, so it is not_evaluated
    # rather than a claim this factor is not entitled to make.
    return (availability, evidence, FactorValidationStatus.NOT_EVALUATED)


def materialize_fund_consolidation(
    connection: Connection[Any],
    *,
    run_id: str,
    cutoff: datetime,
    definition: EtfConsolidationDefinition = ETF_CONSOLIDATION_V0,
) -> tuple[FundConsolidation, ...]:
    """Write one `mart.fund_virtual_company` row per fund with a vintage at `cutoff`.

    Idempotent per (run_id, fund_id): a re-run of the same tick replaces its own row
    rather than accumulating, since the row is a function of the run and the definition.
    """
    written: list[FundConsolidation] = []
    for vintage in load_fund_vintages(connection, run_id=run_id, cutoff=cutoff):
        consolidation = consolidate_fund(vintage.lines, fund_id=vintage.fund_id, as_of=cutoff, definition=definition)
        availability, evidence, validation = _status_dimensions(consolidation)
        connection.execute(
            """
            insert into mart.fund_virtual_company
                (run_id, fund_id, fund_name, report_period, transaction_time, cutoff,
                 definition_version, definition_sha256, weighted_valuation_gap,
                 total_weight_pct, resolved_weight_pct, valued_weight_pct,
                 lines, valued_lines, confidence, reason_codes,
                 availability_status, source_evidence_status, factor_validation_status)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (run_id, fund_id) do update set
                fund_name = excluded.fund_name,
                report_period = excluded.report_period,
                transaction_time = excluded.transaction_time,
                cutoff = excluded.cutoff,
                definition_version = excluded.definition_version,
                definition_sha256 = excluded.definition_sha256,
                weighted_valuation_gap = excluded.weighted_valuation_gap,
                total_weight_pct = excluded.total_weight_pct,
                resolved_weight_pct = excluded.resolved_weight_pct,
                valued_weight_pct = excluded.valued_weight_pct,
                lines = excluded.lines,
                valued_lines = excluded.valued_lines,
                confidence = excluded.confidence,
                reason_codes = excluded.reason_codes,
                availability_status = excluded.availability_status,
                source_evidence_status = excluded.source_evidence_status,
                factor_validation_status = excluded.factor_validation_status
            """,
            (
                run_id,
                vintage.fund_id,
                vintage.fund_name,
                vintage.report_period,
                vintage.transaction_time,
                cutoff,
                definition.factor_version,
                definition.content_sha256,
                consolidation.result.value,
                consolidation.total_weight,
                consolidation.resolved_weight,
                consolidation.valued_weight,
                consolidation.lines,
                consolidation.valued_lines,
                consolidation.result.confidence,
                list(consolidation.result.flags),
                availability.value,
                evidence.value,
                validation.value,
            ),
        )
        written.append(consolidation)
    return tuple(written)


def summary_line(consolidations: tuple[FundConsolidation, ...]) -> str:
    if not consolidations:
        return "fund consolidation: no fund has a holdings vintage at this cutoff"
    parts = [
        f"{c.fund_id}={'refused' if c.result.value is None else f'{c.result.value:.4f}'}"
        f" (valued {c.valued_weight:.2f}/{c.resolved_weight:.2f}/{c.total_weight:.2f}%)"
        for c in consolidations
    ]
    return "fund consolidation: " + "; ".join(parts)
