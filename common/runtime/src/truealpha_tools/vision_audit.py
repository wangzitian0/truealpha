"""Audit physical Gate Epics (#28, #54) against /api/health verdicts and pointers."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from data_engine.environment_universes import scheduled_universe_names
from infra2_sdk.deploy import default_http_get
from infra2_sdk.routing import resolve_app_hostname
from truealpha_contracts.environment_contract import load_environment_contract

from truealpha_tools.nightly_verdicts import HealthReport, read_report


def _health_url_for(env: str) -> str:
    host = resolve_app_hostname("truealpha", env, base_domain="truealpha.club", custom_prod_domain="truealpha.club")
    return f"https://{host}/api/health"


@dataclass(frozen=True)
class GateAuditResult:
    gate_id: str
    title: str
    status: str
    verified: bool
    evidence: str


@dataclass(frozen=True)
class VisionAuditReport:
    target_env: str
    health_url: str
    release: str | None
    all_passed: bool
    gates: list[GateAuditResult]
    nightly_verdicts_summary: dict[str, Any]
    status: str = "fail"
    served_head_freshness: str = "fresh"


def _assess_served_head_freshness(report: Any, *, environment: str = "production") -> tuple[bool, str]:
    pointers = getattr(report, "governed_pointers", None)
    if pointers is None or pointers == "unknown" or not isinstance(pointers, list) or not pointers:
        return False, "mart.served_head.freshness=unknown (no governed pointers)"
    stale: list[str] = []
    scheduled_seen = 0
    contract = load_environment_contract()
    for entry in pointers:
        if not isinstance(entry, dict):
            stale.append(str(entry))
            continue
        universe = str(entry.get("universe_id", "?"))
        if not contract.is_head_scheduled(universe, environment):
            continue
        scheduled_seen += 1
        freshness = str(entry.get("freshness", "fresh"))
        availability = str(entry.get("availability", "available"))
        if freshness != "fresh" or availability != "available":
            stale.append(f"{universe}:{freshness}/{availability}")
    if stale:
        return False, f"mart.served_head.freshness!=fresh ({', '.join(stale)})"
    if scheduled_seen == 0:
        return False, "mart.served_head.freshness=unknown (no scheduled governed pointers)"
    return True, "mart.served_head.freshness == 'fresh'"


def _count_oracle_agrees(oracle_outcomes: Any) -> int:
    if oracle_outcomes is None:
        return 0
    if isinstance(oracle_outcomes, Mapping):
        return int(oracle_outcomes.get("agree", 0))
    if isinstance(oracle_outcomes, Sequence) and not isinstance(oracle_outcomes, (str, bytes)):
        return sum(
            1
            for item in oracle_outcomes
            if (item.get("result") if isinstance(item, Mapping) else getattr(item, "result", None)) == "agree"
        )
    return 0


def _gate(gate_id: str, title: str, verified: bool, evidence: str) -> GateAuditResult:
    return GateAuditResult(gate_id, title, "VERIFIED" if verified else "OPEN", verified, evidence)


def _audit_gate_1(
    verdicts_by_check: Mapping[str, Any], oracle_outcomes: Any, *, env: str = "production"
) -> GateAuditResult:
    surface_proof = verdicts_by_check.get("report_surface_proof")
    invariants = verdicts_by_check.get("output_invariants")
    if oracle_outcomes is not None:
        agree_count = _count_oracle_agrees(oracle_outcomes)
        oracle_ok = agree_count > 0
        oracle_label = f"oracle_outcomes: {agree_count} agree" if oracle_ok else "no comparator"
    else:
        conf_verdicts = {
            u: verdicts_by_check.get(f"datahub_confidence_report@{u}") for u in scheduled_universe_names(env)
        }
        oracle_ok = bool(conf_verdicts) and all(v is not None and v.ok is True for v in conf_verdicts.values())
        oracle_label = (
            "; ".join(f"datahub_confidence_report@{u}: {v.summary}" for u, v in conf_verdicts.items() if v is not None)
            if oracle_ok
            else "no comparator"
        )
    verified = bool(oracle_ok and surface_proof and surface_proof.ok is True and invariants and invariants.ok is True)
    evidence = (
        f"{oracle_label}; "
        f"report_surface_proof: {surface_proof.summary if surface_proof else 'missing'}; "
        f"output_invariants: {invariants.summary if invariants else 'missing'}"
    )
    return _gate("#29", "Gate 1: Wide Row and Output Invariants", verified, evidence)


def _verdict_gate(gate_id: str, title: str, verdict: Any, missing: str) -> GateAuditResult:
    return _gate(gate_id, title, verdict is not None and verdict.ok is True, verdict.summary if verdict else missing)


def _coverage_gate(verdicts_by_check: Mapping[str, Any], env: str) -> GateAuditResult:
    coverage = {u: verdicts_by_check.get(f"question_coverage@{u}") for u in scheduled_universe_names(env)}
    ok = bool(coverage) and all(v is not None and v.ok is True for v in coverage.values())
    detail = "; ".join(f"{u} coverage: {v.summary if v else 'missing'}" for u, v in coverage.items())
    return _gate(
        "#30", "Gate 2: Research Question Coverage", ok, detail or f"the contract schedules no universe in {env}"
    )


def audit_vision(
    *,
    env: str = "production",
    health_url: str | None = None,
    oracle_outcomes: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None = None,
) -> VisionAuditReport:
    resolved_url = health_url or _health_url_for(env)
    try:
        report: HealthReport = read_report(resolved_url, default_http_get())
        verdicts_list = report.verdicts or []
        verdicts_by_check = {v.check: v for v in verdicts_list}
        verdicts_summary: dict[str, Any] = {
            v.check: {"ok": v.ok, "summary": v.summary, "ran_at": v.ran_at.isoformat()} for v in verdicts_list
        }
        release_sha = report.release
        heads_fresh, freshness_detail = _assess_served_head_freshness(report, environment=env)
        effective_outcomes = getattr(report, "oracle_outcomes", None) if oracle_outcomes is None else oracle_outcomes
    except Exception as exc:  # noqa: BLE001
        verdicts_by_check, verdicts_summary, release_sha = {}, {"error": str(exc)}, None
        heads_fresh, freshness_detail = False, "mart.served_head.freshness=unknown"
        effective_outcomes = oracle_outcomes

    market_fresh = verdicts_by_check.get("market_data_freshness")
    gates = [
        _verdict_gate(
            "#56",
            "Gate 0: Capture & Environment Readiness",
            verdicts_by_check.get("release_fetch_proof"),
            "No release_fetch_proof recorded in nightly verdicts.",
        ),
        _audit_gate_1(verdicts_by_check, effective_outcomes, env=env),
        _coverage_gate(verdicts_by_check, env),
        _verdict_gate(
            "#31",
            "Gate 3: Backtest Engine and Simulation Integrity",
            verdicts_by_check.get("nightly_backtest"),
            "nightly_backtest: no verdict recorded at all in target environment.",
        ),
    ]
    gates.append(
        _gate(
            "#54",
            "Gate 4: Production Graduation & Vision Completion",
            all(g.verified for g in gates) and market_fresh is not None and market_fresh.ok is True,
            f"market_data_freshness: {market_fresh.summary if market_fresh else 'missing'}. "
            "Production graduation requires all prior gates VERIFIED and human owner sign-off.",
        )
    )
    if not heads_fresh and gates:
        last = gates[-1]
        gates[-1] = GateAuditResult(last.gate_id, last.title, "OPEN", False, f"{last.evidence} [{freshness_detail}]")
    all_passed = all(g.verified for g in gates) and heads_fresh
    return VisionAuditReport(
        target_env=env,
        health_url=resolved_url,
        release=release_sha,
        all_passed=all_passed,
        gates=gates,
        nightly_verdicts_summary=verdicts_summary,
        status="pass" if all_passed else "fail",
        served_head_freshness="fresh" if heads_fresh else freshness_detail,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit TrueAlpha Vision & Gate Epics")
    parser.add_argument("--env", choices=["production", "staging"], default="production")
    parser.add_argument("--health-url", default=None)
    parser.add_argument("--fail-closed", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = audit_vision(env=args.env, health_url=args.health_url)
    if args.json:
        print(json.dumps(asdict(report), indent=2))
        return 0
    print(f"=== TrueAlpha Vision Audit [{report.target_env.upper()}] ===")
    print(f"Health Endpoint: {report.health_url}\nDeployed Release: {report.release or 'unknown'}\n")
    for g in report.gates:
        print(f"[{'PASS' if g.verified else 'FAIL'}] {g.gate_id} {g.title}: {g.status}\n       Evidence: {g.evidence}")
    print(
        "\nStatus: All Gate Epics physically verified."
        if report.all_passed
        else "\nStatus: System contains open gates and unfulfilled physical criteria."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
