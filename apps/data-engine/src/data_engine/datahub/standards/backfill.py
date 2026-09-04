"""Run the loop for one standard over one universe (#733): plan the open cells, resolve
each through the standard's evidence path, and report what happened per cell.

Two modes, one code path. `backfill` lands cited facts; `probe` runs the same enumeration
and selection without writing a fact or a raw object — the report is the answer to "which
source can fill this field for this universe", the data-source research instrument.
The ledger records the vendor calls in both modes: capacity spent is capacity spent.

Each cell commits on its own. A backfill that dies on issuer 60 of 101 keeps the 59
cited facts it landed; the next run's planner finds only the remaining cells.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from truealpha_contracts import RawObjectStore
from truealpha_contracts.standards import STANDARDS, EvidenceRequirement, MetricStandard

from data_engine.datahub.standards.filing_extraction import ExtractionOutcome, extract_headcount
from data_engine.datahub.standards.planner import (
    OpenCell,
    open_cells,
    resolve_missing_ciks,
    universe_issuers,
)
from data_engine.sources import sec
from data_engine.sources.gateway import SourceGateway

Mode = Literal["backfill", "probe"]
HEALTH_LOG_SOURCE = "standard-backfill"


@dataclass
class BackfillReport:
    universe: str
    standard: str
    mode: Mode
    cutoff: datetime
    issuers: int = 0
    open: int = 0
    open_by_reason: Counter[str] = field(default_factory=Counter)
    outcomes: Counter[str] = field(default_factory=Counter)
    cells: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "universe": self.universe,
            "standard": self.standard,
            "mode": self.mode,
            "cutoff": self.cutoff.isoformat(),
            "issuers": self.issuers,
            "open_cells": self.open,
            "open_by_reason": dict(self.open_by_reason),
            "outcomes": dict(self.outcomes),
        }


def run_standard_backfill(
    connection: Any,
    *,
    universe: str,
    standard_name: str,
    cutoff: datetime,
    mode: Mode,
    max_issuers: int = 0,
    http: Any = None,
    gateway: SourceGateway | None = None,
    store: RawObjectStore | None = None,
    log: Callable[[str], None] = print,
) -> BackfillReport:
    standard = STANDARDS[standard_name]
    report = BackfillReport(universe=universe, standard=standard_name, mode=mode, cutoff=cutoff)
    gateway = gateway or SourceGateway(connection, caller=f"{HEALTH_LOG_SOURCE}:{universe}:{standard_name}")
    issuers = universe_issuers(connection, universe)
    if any(issuer.cik is None for issuer in issuers):
        index = gateway.call("sec", "company_tickers", lambda: sec.ticker_cik_index(http))
        issuers = resolve_missing_ciks(issuers, index)
    report.issuers = len(issuers)
    cells = open_cells(connection, issuers, standard=standard, cutoff=cutoff)
    report.open = len(cells)
    report.open_by_reason.update(cell.reason for cell in cells)
    log(
        f"standard backfill {standard_name} over {universe} at {cutoff.isoformat()} [{mode}]: "
        f"{report.open}/{report.issuers} cells open ({dict(report.open_by_reason)})"
    )
    if max_issuers > 0:
        cells = cells[:max_issuers]

    own_http = http is None and cells and standard.evidence is EvidenceRequirement.FILING_SPAN
    client = sec.client() if own_http else http
    try:
        for cell in cells:
            outcome = _resolve(cell, standard, connection, client, gateway, cutoff, mode, store)
            report.outcomes[outcome.status] += 1
            report.cells.append(_cell_record(cell, outcome))
            log(_cell_line(cell, outcome))
            connection.commit()
    finally:
        if own_http and client is not None:
            client.close()

    _persist_summary(connection, report)
    connection.commit()
    log(f"standard backfill done: {json.dumps(report.summary(), sort_keys=True)}")
    return report


def _resolve(
    cell: OpenCell,
    standard: MetricStandard,
    connection: Any,
    http: Any,
    gateway: SourceGateway,
    cutoff: datetime,
    mode: Mode,
    store: RawObjectStore | None,
) -> ExtractionOutcome:
    if cell.issuer.cik is None:
        return ExtractionOutcome(0, "error", detail="issuer has no CIK in the universe plane or the SEC crosswalk")
    if standard.evidence is not EvidenceRequirement.FILING_SPAN:
        return ExtractionOutcome(cell.issuer.cik, "error", detail=f"no adapter for evidence {standard.evidence}")
    return extract_headcount(
        cell.issuer.cik,
        connection=connection,
        http=http,
        gateway=gateway,
        standard=standard,
        cutoff=cutoff,
        write=mode == "backfill",
        store=store,
    )


def _cell_record(cell: OpenCell, outcome: ExtractionOutcome) -> dict[str, Any]:
    return {
        "issuer_id": cell.issuer.issuer_id,
        "ticker": cell.issuer.ticker,
        "cik": cell.issuer.cik,
        "open_reason": cell.reason,
        "status": outcome.status,
        "value": outcome.value,
        "as_of": outcome.as_of.isoformat() if outcome.as_of else None,
        "accession": outcome.accession,
        "filing_date": outcome.filing_date.isoformat() if outcome.filing_date else None,
        "candidates": [c.value for c in outcome.candidates],
        "fact_id": outcome.fact_id,
        "detail": outcome.detail,
    }


def _cell_line(cell: OpenCell, outcome: ExtractionOutcome) -> str:
    head = f"  {cell.issuer.ticker:<6} cik={cell.issuer.cik} open={cell.reason:<9} -> {outcome.status}"
    if outcome.status in ("resolved", "already_recorded"):
        return f"{head} value={outcome.value} accession={outcome.accession} filed={outcome.filing_date} fact_id={outcome.fact_id}"
    if outcome.status == "needs_model_selection":
        return f"{head} candidates={[c.value for c in outcome.candidates]} accession={outcome.accession}"
    return f"{head} {outcome.detail}"


def _persist_summary(connection: Any, report: BackfillReport) -> None:
    """The run's per-status counts, in the existing health log: a probe is an answer only
    if it survives the run that produced it (init.md §6, `ingestion_health_log`)."""
    note = json.dumps(report.summary(), sort_keys=True)
    for status, count in sorted(report.outcomes.items()):
        connection.execute(
            "insert into staging.ingestion_health_log (source, metric, value, note) values (%s, %s, %s, %s)",
            (HEALTH_LOG_SOURCE, f"{report.standard}:{report.universe}:{report.mode}:{status}", count, note),
        )
    connection.execute(
        "insert into staging.ingestion_health_log (source, metric, value, note) values (%s, %s, %s, %s)",
        (HEALTH_LOG_SOURCE, f"{report.standard}:{report.universe}:{report.mode}:open_cells", report.open, note),
    )
