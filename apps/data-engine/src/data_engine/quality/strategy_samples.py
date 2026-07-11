"""Audit the checked-in sample corpus against strategy data requirements."""

from __future__ import annotations

import csv
import json
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree

from truealpha_contracts import (
    STRATEGY_DATA_REQUIREMENTS,
    DataQualityReport,
    QualityCheckResult,
    QualityStatus,
    ReadinessAssessment,
    ReadinessLevel,
)

REQUIRED_TRAITS = frozenset({"software", "financial", "traditional", "loss_making"})


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_texts(root: ElementTree.Element, name: str) -> list[str]:
    return [element.text.strip() for element in root.iter() if _local_name(element.tag) == name and element.text]


def _load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected object in {path}")
    return value


def _sec_lineage_count(paths: list[Path]) -> int:
    count = 0
    for path in paths:
        facts = _load_json(path).get("facts", {})
        for taxonomy in facts.values():
            for fact in taxonomy.values():
                for observations in fact.get("units", {}).values():
                    count += sum(
                        all(key in observation for key in ("val", "end", "filed", "accn", "form"))
                        for observation in observations
                    )
    return count


def _rating_event_count(paths: list[Path]) -> int:
    count = 0
    for path in paths:
        data = _load_json(path).get("rating_summary", {}).get("data") or {}
        for analyst in data.get("analyst_rating_summary_list", []):
            count += len(analyst.get("rating_item_list", []))
    return count


def _valid_price_inventory(paths: list[Path]) -> tuple[dict[str, int], int, list[str]]:
    rows_by_symbol: dict[str, int] = {}
    dates: list[date] = []
    errors: list[str] = []
    for path in paths:
        symbol = path.name.split("_", 1)[0]
        prior_date: date | None = None
        count = 0
        with path.open(newline="", encoding="utf-8") as handle:
            for line_number, row in enumerate(csv.DictReader(handle), start=2):
                try:
                    trading_date = date.fromisoformat(row["Date"])
                    open_price = Decimal(row["Open"])
                    high = Decimal(row["High"])
                    low = Decimal(row["Low"])
                    close = Decimal(row["Close"])
                    Decimal(row["Adj Close"])
                    volume = int(row["Volume"])
                    if high < max(open_price, low, close) or low > min(open_price, high, close) or volume < 0:
                        raise ValueError("invalid OHLCV bounds")
                    if prior_date is not None and trading_date <= prior_date:
                        raise ValueError("dates are not strictly increasing")
                except (InvalidOperation, KeyError, TypeError, ValueError) as error:
                    errors.append(f"{path.name}:{line_number}: {error}")
                    continue
                prior_date = trading_date
                dates.append(trading_date)
                count += 1
        rows_by_symbol[symbol] = count
    span_days = (max(dates) - min(dates)).days if dates else 0
    return rows_by_symbol, span_days, errors


def _check(
    requirement_id: str,
    level: ReadinessLevel,
    passed: bool,
    observed: str,
    expected: str,
) -> QualityCheckResult:
    return QualityCheckResult(
        requirement_id=requirement_id,
        level=level,
        status=QualityStatus.PASS if passed else QualityStatus.FAIL,
        observed=observed,
        expected=expected,
    )


def audit_strategy_samples(sample_root: Path) -> DataQualityReport:
    """Return deterministic readiness results for the repository sample corpus."""

    sample_root = sample_root.resolve()
    coverage = _load_json(sample_root / "strategy_coverage.json")
    evidence = coverage.get("evidence", {})
    traits_by_company = coverage.get("company_traits", {})

    sec_paths = sorted((sample_root / "sec").glob("*.json"))
    moomoo_paths = sorted(path for path in (sample_root / "moomoo").glob("*.json") if path.stem != "owner_plate")
    price_paths = sorted((sample_root / "prices").glob("*.csv"))
    filing_paths = sorted((sample_root / "filings").glob("*.html"))
    nport_paths = sorted((sample_root / "nport").glob("*.xml"))

    sec_symbols = {path.name.split("_", 1)[0] for path in sec_paths}
    moomoo_symbols = {path.stem for path in moomoo_paths}
    filing_symbols = {path.name.split("_", 1)[0] for path in filing_paths}
    price_rows, price_span_days, price_errors = _valid_price_inventory(price_paths)
    price_symbols = set(price_rows)
    common_companies = sec_symbols & moomoo_symbols & filing_symbols & price_symbols

    lineage_count = _sec_lineage_count(sec_paths)
    rating_count = _rating_event_count(moomoo_paths)
    filing_forms = {
        "20-F" if "_20F_" in path.name else "10-K"
        for path in filing_paths
        if "_10K_" in path.name or "_20F_" in path.name
    }
    revenue_breakdowns = sum(
        bool(_load_json(path).get("financials_revenue_breakdown", {}).get("data")) for path in moomoo_paths
    )

    nport_roots = [ElementTree.parse(path).getroot() for path in nport_paths]
    nport_weights = sum(len(_xml_texts(root, "pctVal")) for root in nport_roots)
    identifier_fallbacks = sum(
        1
        for root in nport_roots
        for holding in (element for element in root.iter() if _local_name(element.tag) == "invstOrSec")
        if "000000000" in _xml_texts(holding, "cusip")
        and bool(_xml_texts(holding, "isin") or _xml_texts(holding, "lei"))
    )

    observed_traits = {trait for traits in traits_by_company.values() for trait in traits}
    company_count = len(common_companies)
    checks: list[QualityCheckResult] = []

    def add_all(requirement_id: str, predicate: bool, observed: str, expected: str) -> None:
        requirement = next(item for item in STRATEGY_DATA_REQUIREMENTS if item.id == requirement_id)
        checks.extend(
            _check(requirement_id, level, predicate, observed, expected)
            for level in ReadinessLevel
            if level in requirement.required_for
        )

    add_all(
        "identity.point_in_time",
        company_count >= 4 and len(nport_paths) >= 2 and identifier_fallbacks > 0,
        f"{company_count} cross-source companies, {len(nport_paths)} funds, {identifier_fallbacks} identifier fallbacks",
        "at least 4 cross-source companies, 2 funds, and 1 non-CUSIP fallback",
    )
    add_all(
        "financial.lineage",
        lineage_count > 0,
        f"{lineage_count} SEC observations with value/period/filed/accession/form",
        "at least one complete point-in-time financial observation",
    )
    add_all(
        "filings.edge_cases",
        {"10-K", "20-F"}.issubset(filing_forms) and bool(evidence.get("ambiguous_disclosure_case")),
        f"forms={sorted(filing_forms)}, ambiguous_case={bool(evidence.get('ambiguous_disclosure_case'))}",
        "10-K, 20-F, and an explicitly documented ambiguity case",
    )
    add_all(
        "prices.adjusted_ohlcv",
        company_count >= 4 and not price_errors and all(count > 0 for count in price_rows.values()),
        f"rows={price_rows}, validation_errors={len(price_errors)}",
        "valid ordered adjusted OHLCV rows for every cross-source company",
    )
    checks.append(
        _check(
            "prices.history",
            ReadinessLevel.LOCAL_BACKTEST,
            price_span_days >= 3 * 365,
            f"{price_span_days} calendar days",
            "at least 1095 calendar days",
        )
    )
    checks.append(
        _check(
            "prices.history",
            ReadinessLevel.STRATEGY_EVALUATION,
            price_span_days >= 5 * 365,
            f"{price_span_days} calendar days",
            "at least 1825 calendar days",
        )
    )
    add_all(
        "corporate_actions.total_return",
        bool(evidence.get("split_golden_case")) and bool(evidence.get("dividend_golden_case")),
        f"split={bool(evidence.get('split_golden_case'))}, dividend={bool(evidence.get('dividend_golden_case'))}",
        "one split and one dividend golden case",
    )
    add_all(
        "universe.membership_history",
        bool(evidence.get("universe_membership_intervals")) and bool(evidence.get("symbol_change_or_delisting_case")),
        (
            f"intervals={bool(evidence.get('universe_membership_intervals'))}, "
            f"symbol_change_or_delisting={bool(evidence.get('symbol_change_or_delisting_case'))}"
        ),
        "membership intervals and a symbol-change or delisting case",
    )
    add_all(
        "financial.restatement_vintages",
        bool(evidence.get("restatement_golden_pair")),
        f"golden_pair={bool(evidence.get('restatement_golden_pair'))}",
        "one independently replayable original/restated golden pair",
    )
    add_all(
        "graph.supply_chain_evidence",
        bool(evidence.get("supply_chain_edge_candidates")) and bool(filing_paths),
        f"candidate_case={bool(evidence.get('supply_chain_edge_candidates'))}, filings={len(filing_paths)}",
        "named relationship candidates backed by filing fixtures",
    )
    add_all(
        "graph.supply_chain_history",
        bool(evidence.get("supply_chain_pit_golden_edges")),
        f"pit_golden_edges={bool(evidence.get('supply_chain_pit_golden_edges'))}",
        "golden edges with validity, knowability, confidence, and evidence",
    )
    add_all(
        "analyst.event_history",
        rating_count >= 20,
        f"{rating_count} historical rating events",
        "at least 20 historical rating events",
    )
    add_all(
        "analyst.knowability",
        bool(evidence.get("rating_knowability_corroborated")),
        f"corroborated={bool(evidence.get('rating_knowability_corroborated'))}",
        "at least one externally corroborated public availability timestamp",
    )
    add_all(
        "holdings.point_in_time",
        len(nport_paths) >= 2 and nport_weights > 0 and identifier_fallbacks > 0,
        f"{len(nport_paths)} N-PORT filings, {nport_weights} weights, {identifier_fallbacks} fallbacks",
        "at least 2 funds with weights and a non-CUSIP fallback",
    )
    add_all(
        "segments.revenue_taxonomy",
        revenue_breakdowns == len(moomoo_paths) and bool(filing_paths),
        f"{revenue_breakdowns}/{len(moomoo_paths)} structured breakdowns plus {len(filing_paths)} filings",
        "structured revenue breakdown and filing fallback for every sampled company",
    )
    add_all(
        "universe.strategy_diversity",
        company_count >= 7 and REQUIRED_TRAITS.issubset(observed_traits),
        f"{company_count} companies, traits={sorted(observed_traits)}",
        f"at least 7 companies with traits={sorted(REQUIRED_TRAITS)}",
    )
    add_all(
        "prices.source_reconciliation",
        bool(evidence.get("primary_price_source")) and bool(evidence.get("independent_price_reconciliation")),
        (
            f"primary={bool(evidence.get('primary_price_source'))}, "
            f"reconciled={bool(evidence.get('independent_price_reconciliation'))}"
        ),
        "primary source reconciled against an independent fallback",
    )
    add_all(
        "factors.point_in_time_outputs",
        bool(evidence.get("factor_output_replay_case")),
        f"replay_case={bool(evidence.get('factor_output_replay_case'))}",
        "composite replay fixture with timestamp and confidence assertions",
    )

    assessments = tuple(
        ReadinessAssessment(
            level=level,
            ready=not (
                blockers := tuple(
                    check.requirement_id
                    for check in checks
                    if check.level == level and check.status == QualityStatus.FAIL
                )
            ),
            blockers=blockers,
        )
        for level in ReadinessLevel
    )
    return DataQualityReport(
        generated_at=datetime.now(UTC),
        sample_root=str(sample_root),
        checks=tuple(checks),
        assessments=assessments,
    )


def render_markdown(report: DataQualityReport) -> str:
    lines = ["# Strategy sample readiness", ""]
    for assessment in report.assessments:
        state = "READY" if assessment.ready else "NOT READY"
        lines.append(f"- **{assessment.level.value}**: {state}")
    lines.extend(["", "| Level | Requirement | Status | Observed | Expected |", "|---|---|---|---|---|"])
    for check in report.checks:
        lines.append(
            f"| {check.level.value} | `{check.requirement_id}` | {check.status.value.upper()} | "
            f"{check.observed} | {check.expected} |"
        )
    return "\n".join(lines)
