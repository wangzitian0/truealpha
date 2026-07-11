"""Audit the checked-in sample corpus against strategy data requirements."""

from __future__ import annotations

import csv
import json
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from html import unescape
from pathlib import Path
from xml.etree import ElementTree

from truealpha_contracts import (
    STRATEGY_DATA_REQUIREMENTS,
    DataQualityReport,
    EvidenceCase,
    EvidenceKind,
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


def _valid_price_inventory(paths: list[Path]) -> tuple[dict[str, int], dict[str, int], list[str]]:
    observations: dict[str, dict[date, tuple[Decimal, Decimal, Decimal, Decimal, Decimal, int]]] = {}
    errors: list[str] = []
    for path in paths:
        symbol = path.name.split("_", 1)[0]
        prior_date: date | None = None
        symbol_rows = observations.setdefault(symbol, {})
        with path.open(newline="", encoding="utf-8") as handle:
            for line_number, row in enumerate(csv.DictReader(handle), start=2):
                try:
                    trading_date = date.fromisoformat(row["Date"])
                    open_price = Decimal(row["Open"])
                    high = Decimal(row["High"])
                    low = Decimal(row["Low"])
                    close = Decimal(row["Close"])
                    adjusted_close = Decimal(row["Adj Close"])
                    volume = int(row["Volume"])
                    if high < max(open_price, low, close) or low > min(open_price, high, close) or volume < 0:
                        raise ValueError("invalid OHLCV bounds")
                    if prior_date is not None and trading_date <= prior_date:
                        raise ValueError("dates are not strictly increasing")
                except (InvalidOperation, KeyError, TypeError, ValueError) as error:
                    errors.append(f"{path.name}:{line_number}: {error}")
                    continue
                values = (open_price, high, low, close, adjusted_close, volume)
                # Different immutable captures may revise adjusted history. Each
                # file is validated independently; later captures remain a new
                # point-in-time vintage rather than a duplicate-row failure.
                symbol_rows[trading_date] = values
                prior_date = trading_date
    rows_by_symbol = {symbol: len(rows) for symbol, rows in observations.items()}
    spans_by_symbol = {symbol: (max(rows) - min(rows)).days if rows else 0 for symbol, rows in observations.items()}
    return rows_by_symbol, spans_by_symbol, errors


def _artifact_text(sample_root: Path, evidence: EvidenceCase) -> str:
    return "\n".join(
        unescape((sample_root / relative_path).read_text(encoding="utf-8", errors="ignore"))
        for relative_path in evidence.artifact_paths
    )


def _assert_ddog_headcount_ambiguity(sample_root: Path, evidence: EvidenceCase) -> bool:
    text = _artifact_text(sample_root, evidence)
    return all(token in text for token in ("3,600 employees", "3,900 employees", "8,100 employees"))


def _assert_ddog_supply_chain_candidates(sample_root: Path, evidence: EvidenceCase) -> bool:
    text = _artifact_text(sample_root, evidence)
    return all(token in text for token in ("Amazon Web Services", "Microsoft Azure", "Google Cloud Platform"))


def _artifact_with_name(sample_root: Path, evidence: EvidenceCase, name: str) -> Path:
    return next(sample_root / path for path in evidence.artifact_paths if name in path)


def _price_rows(path: Path) -> dict[date, dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return {date.fromisoformat(row["Date"]): row for row in csv.DictReader(handle)}


def _assert_nvda_split_replay(sample_root: Path, evidence: EvidenceCase) -> bool:
    filing = unescape(_artifact_with_name(sample_root, evidence, "NVDA_8K_SPLIT").read_text(errors="ignore"))
    events = _load_json(_artifact_with_name(sample_root, evidence, "NVDA_YAHOO_EVENTS"))
    splits = events["chart"]["result"][0]["events"]["splits"].values()
    prices = _price_rows(_artifact_with_name(sample_root, evidence, "NVDA_prices"))
    before = Decimal(prices[date(2024, 6, 7)]["Close"])
    after = Decimal(prices[date(2024, 6, 10)]["Close"])
    return (
        "ten-for-one forward stock split" in filing.lower()
        and "June 10, 2024" in filing
        and any(split.get("splitRatio") == "10:1" for split in splits)
        and Decimal("0.5") < after / before < Decimal("2")
    )


def _assert_jpm_dividend_replay(sample_root: Path, evidence: EvidenceCase) -> bool:
    announcement = _load_json(_artifact_with_name(sample_root, evidence, "JPM_DIVIDEND"))
    events = _load_json(_artifact_with_name(sample_root, evidence, "JPM_YAHOO_EVENTS"))
    dividends = events["chart"]["result"][0]["events"]["dividends"].values()
    prices = _price_rows(_artifact_with_name(sample_root, evidence, "JPM_prices"))
    return (
        announcement.get("artifact_kind") == "extracted_public_statement"
        and announcement.get("source") == "jpmorgan_investor_relations"
        and announcement.get("currency") == "USD"
        and Decimal(str(announcement.get("amount_per_share"))) == Decimal("1.40")
        and announcement.get("record_date") == "2025-07-03"
        and announcement.get("pay_date") == "2025-07-31"
        and any(Decimal(str(item.get("amount"))) == Decimal("1.4") for item in dividends)
        and date(2025, 7, 2) in prices
        and date(2025, 7, 3) in prices
    )


def _assert_qqq_membership_replay(sample_root: Path, evidence: EvidenceCase) -> bool:
    roots = [ElementTree.parse(sample_root / path).getroot() for path in evidence.artifact_paths]
    periods = [_xml_texts(root, "repPdDate")[0] for root in roots]
    holdings = [set(_xml_texts(root, "name")) for root in roots]
    return (
        periods == ["2025-12-31", "2026-03-31"]
        and "AstraZeneca PLC" in holdings[0] - holdings[1]
        and "Walmart Inc." in holdings[1] - holdings[0]
    )


def _assert_meta_symbol_replay(sample_root: Path, evidence: EvidenceCase) -> bool:
    filing = _artifact_text(sample_root, evidence)
    filing_lower = filing.lower()
    prices = _price_rows(_artifact_with_name(sample_root, evidence, "META_prices"))
    return (
        "ticker symbol to 'meta'" in filing_lower
        and "June 9" in filing
        and "current ticker symbol 'fb'" in filing_lower
        and date(2022, 6, 8) in prices
        and date(2022, 6, 9) in prices
    )


def _assert_plug_restatement_replay(sample_root: Path, evidence: EvidenceCase) -> bool:
    facts = _load_json(_artifact_with_name(sample_root, evidence, "PLUG_CIK"))
    observations = facts["facts"]["us-gaap"]["FinanceLeaseRightOfUseAssetAccumulatedAmortization"]["units"]["USD"]
    rows = {
        row["accn"]: row
        for row in observations
        if row.get("end") == "2020-12-31" and row.get("accn") in {"0001558370-21-007147", "0001558370-22-003577"}
    }
    amended = _artifact_with_name(sample_root, evidence, "PLUG_10KA").read_text(errors="ignore").lower()
    return (
        rows["0001558370-21-007147"]["val"] == 102000
        and rows["0001558370-22-003577"]["val"] == 102000000
        and rows["0001558370-21-007147"]["filed"] < rows["0001558370-22-003577"]["filed"]
        and "restated" in amended
    )


def _assert_ddog_rating_knowability(sample_root: Path, evidence: EvidenceCase) -> bool:
    moomoo = _load_json(_artifact_with_name(sample_root, evidence, "DDOG.json"))
    rating_items = [
        item
        for analyst in moomoo["rating_summary"]["data"]["analyst_rating_summary_list"]
        for item in analyst["rating_item_list"]
    ]
    matching = [
        item
        for item in rating_items
        if item.get("recommendation_date_str") == "2026-07-02" and item.get("target_price") == 330.0
    ]
    corroboration = _artifact_with_name(sample_root, evidence, "RATING_CORROBORATION").read_text(errors="ignore")
    return (
        len(matching) == 1
        and matching[0]["update_time_str"] == "2026-07-09"
        and "2026-07-02T11:40:41.000Z" in corroboration
        and "Datadog price target raised to $330 from $260 at Benchmark" in corroboration
    )


def _assert_ddog_supply_chain_pit(sample_root: Path, evidence: EvidenceCase) -> bool:
    golden = _load_json(_artifact_with_name(sample_root, evidence, "DDOG_supply_chain_edges"))
    filing = (sample_root / golden["source_artifact"]).read_text(errors="ignore")
    edges = golden["edges"]
    return len(edges) == 3 and all(
        edge["relation_type"] == "supplies_to"
        and edge["knowable_at"] == "2026-02-18T00:00:00Z"
        and Decimal(edge["confidence"]) == Decimal("0.80")
        and edge["evidence_token"] in filing
        for edge in edges
    )


def _assert_jpm_financial_semantics(sample_root: Path, evidence: EvidenceCase) -> bool:
    text = _artifact_text(sample_root, evidence)
    return "financial holding company" in text and "financial services firm" in text


def _assert_adm_traditional_semantics(sample_root: Path, evidence: EvidenceCase) -> bool:
    text = _artifact_text(sample_root, evidence)
    return "global agricultural supply chain manager and processor" in text


def _assert_nvda_company_guidance(sample_root: Path, evidence: EvidenceCase) -> bool:
    text = _artifact_text(sample_root, evidence)
    return (
        "Second Quarter of Fiscal 2025 Outlook" in text
        and "Revenue is expected to be $28.0 billion, plus or minus 2%." in text
        and "gross margins are expected" in text
    )


EVIDENCE_ASSERTIONS = {
    "analyst.knowability-corroborated": _assert_ddog_rating_knowability,
    "corporate-action.dividend-replay": _assert_jpm_dividend_replay,
    "corporate-action.split-replay": _assert_nvda_split_replay,
    "filing.adm.traditional-semantics": _assert_adm_traditional_semantics,
    "filing.ddog.headcount-ambiguity": _assert_ddog_headcount_ambiguity,
    "filing.ddog.supply-chain-candidates": _assert_ddog_supply_chain_candidates,
    "filing.jpm.financial-semantics": _assert_jpm_financial_semantics,
    "financial.company-guidance": _assert_nvda_company_guidance,
    "financial.restatement-before-after": _assert_plug_restatement_replay,
    "graph.supply-chain-pit-replay": _assert_ddog_supply_chain_pit,
    "universe.membership-replay": _assert_qqq_membership_replay,
    "universe.symbol-change-or-delisting-replay": _assert_meta_symbol_replay,
}


def _verified_evidence(coverage: dict, sample_root: Path) -> tuple[dict[str, tuple[EvidenceCase, ...]], list[str]]:
    errors: list[str] = []
    cases: dict[str, EvidenceCase] = {}
    for raw_case in coverage.get("evidence_cases", []):
        case = EvidenceCase.model_validate(raw_case)
        if case.evidence_id in cases:
            errors.append(f"duplicate evidence ID: {case.evidence_id}")
        cases[case.evidence_id] = case

    verified: dict[str, tuple[EvidenceCase, ...]] = {}
    for requirement_id, evidence_ids in coverage.get("requirement_evidence", {}).items():
        valid_cases: list[EvidenceCase] = []
        for evidence_id in evidence_ids:
            case = cases.get(evidence_id)
            if case is None:
                errors.append(f"{requirement_id}: missing evidence case {evidence_id}")
                continue
            if case.requirement_id != requirement_id:
                errors.append(f"{evidence_id}: requirement mismatch")
                continue
            if case.kind is not EvidenceKind.REAL:
                errors.append(f"{evidence_id}: synthetic evidence cannot satisfy readiness")
                continue
            artifacts_valid = True
            for relative_path, expected_hash in zip(case.artifact_paths, case.artifact_sha256, strict=True):
                path = sample_root / relative_path
                if not path.is_file():
                    errors.append(f"{evidence_id}: missing artifact {relative_path}")
                    artifacts_valid = False
                    continue
                actual_hash = sha256(path.read_bytes()).hexdigest()
                if actual_hash != expected_hash:
                    errors.append(f"{evidence_id}: hash mismatch for {relative_path}")
                    artifacts_valid = False
            assertions_valid = True
            for assertion_id in case.assertion_ids:
                assertion = EVIDENCE_ASSERTIONS.get(assertion_id)
                if assertion is None:
                    errors.append(f"{evidence_id}: unknown assertion {assertion_id}")
                    assertions_valid = False
                elif artifacts_valid:
                    try:
                        assertion_passed = assertion(sample_root, case)
                    except (IndexError, KeyError, TypeError, ValueError) as error:
                        errors.append(f"{evidence_id}: assertion error {assertion_id}: {error}")
                        assertions_valid = False
                    else:
                        if not assertion_passed:
                            errors.append(f"{evidence_id}: assertion failed {assertion_id}")
                            assertions_valid = False
            if artifacts_valid and assertions_valid:
                valid_cases.append(case)
        verified[requirement_id] = tuple(valid_cases)
    return verified, errors


def _verify_capture_manifest(sample_root: Path) -> list[str]:
    errors: list[str] = []
    manifests = sorted(sample_root.glob("capture_manifest_*.json"))
    if not manifests:
        return ["missing targeted evidence capture manifest"]
    for manifest_path in manifests:
        manifest = _load_json(manifest_path)
        for artifact in manifest.get("artifacts", []):
            relative_path = artifact.get("path", "")
            if relative_path.startswith("/") or ".." in relative_path.split("/"):
                errors.append(f"{manifest_path.name}: unsafe artifact path {relative_path}")
                continue
            path = sample_root / relative_path
            if not path.is_file():
                errors.append(f"{manifest_path.name}: missing artifact {relative_path}")
                continue
            body = path.read_bytes()
            if len(body) != artifact.get("byte_length"):
                errors.append(f"{manifest_path.name}: byte length mismatch for {relative_path}")
            if sha256(body).hexdigest() != artifact.get("sha256"):
                errors.append(f"{manifest_path.name}: hash mismatch for {relative_path}")
    return errors


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
    traits_by_company = coverage.get("company_traits", {})
    evidence, evidence_errors = _verified_evidence(coverage, sample_root)
    evidence_errors.extend(_verify_capture_manifest(sample_root))

    sec_paths = sorted((sample_root / "sec").glob("*.json"))
    moomoo_paths = sorted(path for path in (sample_root / "moomoo").glob("*.json") if path.stem != "owner_plate")
    price_paths = sorted((sample_root / "prices").glob("*.csv"))
    filing_paths = sorted((sample_root / "filings").glob("*.html"))
    nport_paths = sorted((sample_root / "nport").glob("*.xml"))

    sec_symbols = {path.name.split("_", 1)[0] for path in sec_paths}
    moomoo_symbols = {path.stem for path in moomoo_paths}
    filing_symbols = {path.name.split("_", 1)[0] for path in filing_paths}
    price_rows, price_spans, price_errors = _valid_price_inventory(price_paths)
    price_symbols = set(price_rows)
    common_companies = sec_symbols & moomoo_symbols & filing_symbols & price_symbols
    strategy_companies = set(traits_by_company) & sec_symbols & filing_symbols & price_symbols

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
    company_count = len(strategy_companies)
    cross_source_count = len(common_companies)
    checks: list[QualityCheckResult] = []

    def has_evidence(requirement_id: str, *assertion_ids: str) -> bool:
        cases = evidence.get(requirement_id, ())
        observed_assertions = {assertion_id for case in cases for assertion_id in case.assertion_ids}
        return bool(cases) and set(assertion_ids).issubset(observed_assertions)

    def add_all(requirement_id: str, predicate: bool, observed: str, expected: str) -> None:
        requirement = next(item for item in STRATEGY_DATA_REQUIREMENTS if item.id == requirement_id)
        checks.extend(
            _check(requirement_id, level, predicate, observed, expected)
            for level in ReadinessLevel
            if level in requirement.required_for
        )

    add_all(
        "identity.point_in_time",
        cross_source_count >= 4 and len(nport_paths) >= 2 and identifier_fallbacks > 0,
        f"{cross_source_count} cross-source companies, {len(nport_paths)} funds, {identifier_fallbacks} identifier fallbacks",
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
        {"10-K", "20-F"}.issubset(filing_forms)
        and has_evidence("filings.edge_cases", "filing.ddog.headcount-ambiguity"),
        f"forms={sorted(filing_forms)}, verified_cases={len(evidence.get('filings.edge_cases', ()))}",
        "10-K, 20-F, and an explicitly documented ambiguity case",
    )
    add_all(
        "prices.adjusted_ohlcv",
        company_count >= 7 and not price_errors and all(price_rows.get(symbol, 0) > 0 for symbol in strategy_companies),
        f"rows={price_rows}, validation_errors={len(price_errors)}",
        "valid ordered adjusted OHLCV rows for every cross-source company",
    )
    checks.append(
        _check(
            "prices.history",
            ReadinessLevel.LOCAL_BACKTEST,
            bool(strategy_companies) and all(price_spans.get(symbol, 0) >= 3 * 365 for symbol in strategy_companies),
            f"per_symbol_days={{{', '.join(f'{symbol}: {price_spans.get(symbol, 0)}' for symbol in sorted(strategy_companies))}}}",
            "at least 1095 calendar days",
        )
    )
    checks.append(
        _check(
            "prices.history",
            ReadinessLevel.STRATEGY_EVALUATION,
            bool(strategy_companies) and all(price_spans.get(symbol, 0) >= 5 * 365 for symbol in strategy_companies),
            f"per_symbol_days={{{', '.join(f'{symbol}: {price_spans.get(symbol, 0)}' for symbol in sorted(strategy_companies))}}}",
            "at least 1825 calendar days",
        )
    )
    add_all(
        "corporate_actions.total_return",
        has_evidence(
            "corporate_actions.total_return",
            "corporate-action.split-replay",
            "corporate-action.dividend-replay",
        ),
        f"verified_cases={len(evidence.get('corporate_actions.total_return', ()))}",
        "one split and one dividend golden case",
    )
    add_all(
        "universe.membership_history",
        has_evidence(
            "universe.membership_history",
            "universe.membership-replay",
            "universe.symbol-change-or-delisting-replay",
        ),
        f"verified_cases={len(evidence.get('universe.membership_history', ()))}",
        "membership intervals and a symbol-change or delisting case",
    )
    add_all(
        "financial.restatement_vintages",
        has_evidence("financial.restatement_vintages", "financial.restatement-before-after"),
        f"verified_cases={len(evidence.get('financial.restatement_vintages', ()))}",
        "one independently replayable original/restated golden pair",
    )
    add_all(
        "financial.company_guidance",
        has_evidence("financial.company_guidance", "financial.company-guidance"),
        f"verified_cases={len(evidence.get('financial.company_guidance', ()))}",
        "one filed, dated company-guidance case",
    )
    add_all(
        "graph.supply_chain_evidence",
        has_evidence("graph.supply_chain_evidence", "filing.ddog.supply-chain-candidates") and bool(filing_paths),
        f"verified_cases={len(evidence.get('graph.supply_chain_evidence', ()))}, filings={len(filing_paths)}",
        "named relationship candidates backed by filing fixtures",
    )
    add_all(
        "graph.supply_chain_history",
        has_evidence("graph.supply_chain_history", "graph.supply-chain-pit-replay"),
        f"verified_cases={len(evidence.get('graph.supply_chain_history', ()))}",
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
        has_evidence("analyst.knowability", "analyst.knowability-corroborated"),
        f"verified_cases={len(evidence.get('analyst.knowability', ()))}",
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
        company_count >= 7
        and REQUIRED_TRAITS.issubset(observed_traits)
        and has_evidence(
            "universe.strategy_diversity",
            "filing.jpm.financial-semantics",
            "filing.adm.traditional-semantics",
        ),
        f"{company_count} companies, traits={sorted(observed_traits)}",
        f"at least 7 companies with traits={sorted(REQUIRED_TRAITS)}",
    )
    add_all(
        "prices.source_reconciliation",
        has_evidence("prices.source_reconciliation", "prices.independent-source-reconciliation"),
        f"verified_cases={len(evidence.get('prices.source_reconciliation', ()))}",
        "primary source reconciled against an independent fallback",
    )
    add_all(
        "factors.point_in_time_outputs",
        has_evidence("factors.point_in_time_outputs", "factors.composite-pit-replay"),
        f"verified_cases={len(evidence.get('factors.point_in_time_outputs', ()))}",
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
    if evidence_errors:
        raise ValueError("invalid strategy evidence:\n" + "\n".join(evidence_errors))

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
