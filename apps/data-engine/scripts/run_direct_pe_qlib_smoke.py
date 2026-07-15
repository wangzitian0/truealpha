#!/usr/bin/env python3
"""Project the frozen sample corpus and run the direct-P/E Qlib smoke."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from factors.batches.direct_pe_qlib_smoke import (
    CORPUS_SHA256,
    CurrentPegInput,
    DirectPeFeature,
    DirectPeSmokeActivation,
    DirectPeSmokeRequest,
    PriceBar,
    build_earnings_yield_definition,
    render_markdown,
    run_direct_pe_qlib_smoke,
)
from truealpha_contracts.qlib_expression import QlibOperatorRegistry

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CORPUS_PATH = Path("libs/factors/tests/batches/direct_pe_qlib_smoke/fixtures/corpus.v1.json")
OPERATOR_REGISTRY_PATH = Path("libs/contracts/tests/fixtures/qlib_expression.v1.json")
OUTPUT_JSON = "direct_pe_qlib_smoke.json"
OUTPUT_MARKDOWN = "direct_pe_qlib_smoke.md"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"


def _opaque_input_id(payload: object) -> str:
    digest = hashlib.sha256(_canonical_json(payload).encode()).hexdigest()
    return f"sample-input:{digest}"


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(), parse_float=Decimal)
    if not isinstance(payload, dict):
        raise ValueError(f"json_object_required:{path}")
    return payload


def load_frozen_corpus(repository_root: Path = REPOSITORY_ROOT) -> dict[str, Any]:
    """Load the immutable corpus after validating its content identity."""

    path = repository_root / CORPUS_PATH
    if _sha256(path) != CORPUS_SHA256:
        raise ValueError("corpus_identity_drift")
    return _load_json(path)


def verify_frozen_artifacts(corpus: dict[str, Any], repository_root: Path = REPOSITORY_ROOT) -> None:
    """Fail closed if any registered sample or engine artifact has drifted."""

    for evidence in corpus["contract_refs"].values():
        path = repository_root / evidence["path"]
        if _sha256(path) != evidence["sha256"]:
            raise ValueError("contract_artifact_identity_drift")
    for field in (
        "runtime_project",
        "runtime_lock",
        "selection_adapter",
        "expression_compiler",
        "expression_fixture",
    ):
        path = repository_root / corpus["engine_binding"][f"{field}_path"]
        if _sha256(path) != corpus["engine_binding"][f"{field}_sha256"]:
            raise ValueError("qlib_execution_identity_drift")
    for artifacts in corpus["source_artifacts"].values():
        for evidence in artifacts.values():
            path = repository_root / evidence["path"]
            if _sha256(path) != evidence["sha256"]:
                raise ValueError("source_artifact_identity_drift")


def _project_direct_pe(
    symbol: str,
    source: dict[str, Any],
    artifact_sha256: str,
) -> tuple[tuple[DirectPeFeature, ...], CurrentPegInput]:
    valuation = source["valuation_pe"]
    if valuation.get("ok") is not True:
        raise ValueError("direct_pe_source_not_ready")
    data = valuation["data"]
    trend = data["trend"]
    rows = []
    for item in trend["historical_items"]:
        observation_date = date.fromisoformat(item["time_str"])
        direct_pe = Decimal(str(item["value"]))
        rows.append(
            DirectPeFeature(
                instrument_id=symbol,
                observation_date=observation_date,
                as_of=observation_date,
                direct_pe=direct_pe,
                confidence=Decimal(1),
                input_id=_opaque_input_id(
                    {
                        "artifact_sha256": artifact_sha256,
                        "instrument_id": symbol,
                        "observation_date": observation_date.isoformat(),
                        "value": str(direct_pe),
                    }
                ),
            )
        )

    captured = date.fromisoformat(data["last_update_time_str"].split(" ", maxsplit=1)[0])
    current_pe = Decimal(str(trend["current_value"]))
    growth = Decimal(str(data["profit_growth_rate"]["financial_ttm_multiple"]))
    current = CurrentPegInput(
        instrument_id=symbol,
        as_captured_date=captured,
        current_pe=current_pe,
        financial_ttm_multiple=growth,
        confidence=Decimal(1),
        pe_input_id=_opaque_input_id(
            {
                "artifact_sha256": artifact_sha256,
                "as_captured_date": captured.isoformat(),
                "instrument_id": symbol,
                "kind": "current_pe",
                "value": str(current_pe),
            }
        ),
        growth_input_id=_opaque_input_id(
            {
                "artifact_sha256": artifact_sha256,
                "as_captured_date": captured.isoformat(),
                "instrument_id": symbol,
                "kind": "current_financial_ttm_multiple",
                "value": str(growth),
            }
        ),
    )
    return tuple(rows), current


def _project_prices(
    symbol: str,
    path: Path,
    artifact_sha256: str,
    *,
    close_field: str = "Close",
) -> tuple[PriceBar, ...]:
    if close_field != "Close":
        raise ValueError("adjusted_price_forbidden")
    rows = []
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"Date", "Open", close_field}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError("price_projection_fields_missing")
        for item in reader:
            session = date.fromisoformat(item["Date"])
            unadjusted_open = Decimal(item["Open"])
            unadjusted_close = Decimal(item[close_field])
            if unadjusted_open <= 0 or unadjusted_close <= 0:
                raise ValueError("nonpositive_execution_price")
            rows.append(
                PriceBar(
                    instrument_id=symbol,
                    session_date=session,
                    unadjusted_open=unadjusted_open,
                    unadjusted_close=unadjusted_close,
                    input_id=_opaque_input_id(
                        {
                            "artifact_sha256": artifact_sha256,
                            "instrument_id": symbol,
                            "session_date": session.isoformat(),
                            "unadjusted_close": str(unadjusted_close),
                            "unadjusted_open": str(unadjusted_open),
                        }
                    ),
                )
            )
    return tuple(rows)


def project_frozen_inputs(
    corpus: dict[str, Any],
    repository_root: Path = REPOSITORY_ROOT,
    *,
    close_field: str = "Close",
) -> tuple[tuple[DirectPeFeature, ...], tuple[PriceBar, ...], tuple[CurrentPegInput, ...]]:
    """Perform only deterministic source-to-contract projection."""

    features: list[DirectPeFeature] = []
    prices: list[PriceBar] = []
    current_peg: list[CurrentPegInput] = []
    for symbol in corpus["universe"]["symbols"]:
        artifacts = corpus["source_artifacts"][symbol]
        pe_evidence = artifacts["direct_pe"]
        pe_rows, current = _project_direct_pe(
            symbol,
            _load_json(repository_root / pe_evidence["path"]),
            pe_evidence["sha256"],
        )
        price_evidence = artifacts["unadjusted_prices"]
        features.extend(pe_rows)
        current_peg.append(current)
        prices.extend(
            _project_prices(
                symbol,
                repository_root / price_evidence["path"],
                price_evidence["sha256"],
                close_field=close_field,
            )
        )
    return tuple(features), tuple(prices), tuple(current_peg)


def _operator_registry(repository_root: Path) -> QlibOperatorRegistry:
    fixture = _load_json(repository_root / OPERATOR_REGISTRY_PATH)
    return QlibOperatorRegistry.model_validate(fixture["operator_registry"])


def build_request(
    corpus: dict[str, Any],
    repository_root: Path = REPOSITORY_ROOT,
    *,
    environment: str = "local",
    close_field: str = "Close",
) -> DirectPeSmokeRequest:
    features, prices, current_peg = project_frozen_inputs(corpus, repository_root, close_field=close_field)
    registry = _operator_registry(repository_root)
    return DirectPeSmokeRequest(
        activation=DirectPeSmokeActivation(environment=environment),
        expression_definition=build_earnings_yield_definition(registry),
        operator_registry=registry,
        pe_features=features,
        price_bars=prices,
        current_peg_inputs=current_peg,
    )


def verify_frozen_oracles(corpus: dict[str, Any], report: object) -> None:
    """Compare factor-engine output with the independently frozen expected rows."""

    decisions = report.decisions
    expected = corpus["expected_monthly_decisions"]
    if len(decisions) != len(expected):
        raise ValueError("decision_oracle_mismatch")
    for actual, oracle in zip(decisions, expected, strict=True):
        selected = next(row for row in actual.scores if row.instrument_id == actual.selected_instrument_id)
        positive_count = sum(row.availability == "available" for row in actual.scores)
        observed = {
            "decision_date": actual.decision_date.isoformat(),
            "execution_date": actual.execution_date.isoformat(),
            "selected_symbol": actual.selected_instrument_id,
            "selected_pe_date": selected.feature_date.isoformat(),
            "selected_pe": str(selected.direct_pe),
            "positive_candidate_count": positive_count,
        }
        if observed != oracle:
            raise ValueError("decision_oracle_mismatch")

    peg_by_symbol = {row.instrument_id: row for row in report.current_peg_snapshot}
    for oracle in corpus["current_peg_snapshot"]["rows"]:
        actual = peg_by_symbol[oracle["symbol"]]
        if (
            str(actual.current_pe) != oracle["current_pe"]
            or str(actual.financial_ttm_multiple) != oracle["financial_ttm_multiple"]
            or str(actual.peg) != oracle["expected_current_peg"]
            or actual.historical_decision_input
        ):
            raise ValueError("current_peg_oracle_mismatch")


def _input_artifacts(corpus: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"instrument_id": symbol, "kind": kind, "path": evidence["path"], "sha256": evidence["sha256"]}
        for symbol, artifacts in sorted(corpus["source_artifacts"].items())
        for kind, evidence in sorted(artifacts.items())
    ]


def build_report_envelope(corpus: dict[str, Any], report: object) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "input_artifacts": _input_artifacts(corpus),
        "factor_report": report.model_dump(mode="json"),
    }
    content_sha256 = hashlib.sha256(_canonical_json(payload).encode()).hexdigest()
    return {
        **payload,
        "content_sha256": content_sha256,
        "report_id": f"direct-pe-qlib-smoke-envelope:{content_sha256}",
    }


def _render_envelope_markdown(corpus: dict[str, Any], report: object) -> str:
    artifacts = ["## Input Artifacts", "", "| Symbol | Kind | SHA-256 |", "|---|---|---|"]
    artifacts.extend(
        f"| {row['instrument_id']} | {row['kind']} | `{row['sha256']}` |" for row in _input_artifacts(corpus)
    )
    return render_markdown(report).rstrip() + "\n\n" + "\n".join(artifacts) + "\n"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def run(output_dir: Path, *, repository_root: Path = REPOSITORY_ROOT, environment: str = "local") -> dict[str, Any]:
    corpus = load_frozen_corpus(repository_root)
    verify_frozen_artifacts(corpus, repository_root)
    request = build_request(corpus, repository_root, environment=environment)
    report = run_direct_pe_qlib_smoke(request)
    verify_frozen_oracles(corpus, report)
    envelope = build_report_envelope(corpus, report)
    _atomic_write(output_dir / OUTPUT_JSON, _canonical_json(envelope))
    _atomic_write(output_dir / OUTPUT_MARKDOWN, _render_envelope_markdown(corpus, report))
    return envelope


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--environment", choices=("local", "ci"), default="local")
    args = parser.parse_args()
    envelope = run(args.output_dir, environment=args.environment)
    print(envelope["report_id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
