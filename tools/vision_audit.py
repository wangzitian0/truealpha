"""TrueAlpha Vision Audit tool (Issue #28 and Issue #54).

Evaluates the physical state of the TrueAlpha system against the root acceptance
criteria in vision.md, init.md, and Gate Epics (#56, #29, #30, #31, #32, #54).

Usage:
    python tools/vision_audit.py [--env production|staging] [--fail-closed] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from infra2_sdk.deploy import default_http_get

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from nightly_verdicts import HealthReport, read_report  # noqa: E402


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


def audit_vision(
    *,
    env: str = "production",
    health_url: str | None = None,
) -> VisionAuditReport:
    resolved_url = health_url or (
        "https://truealpha.club/api/health"
        if env == "production"
        else "https://truealpha-staging.truealpha.club/api/health"
    )

    try:
        report: HealthReport = read_report(resolved_url, default_http_get())
        verdicts_list = report.verdicts or []
        verdicts_by_check = {v.check: v for v in verdicts_list}
        verdicts_summary = {
            v.check: {"ok": v.ok, "summary": v.summary, "ran_at": v.ran_at.isoformat()} for v in verdicts_list
        }
        release_sha = report.release
    except Exception as exc:  # noqa: BLE001
        verdicts_by_check = {}
        verdicts_summary = {"error": str(exc)}
        release_sha = None

    gates: list[GateAuditResult] = []

    # Gate 0 (#56): Source capture & environment readiness
    fetch_proof = verdicts_by_check.get("release_fetch_proof")
    g0_verified = fetch_proof is not None and fetch_proof.ok is True
    g0_evidence = fetch_proof.summary if fetch_proof else "No release_fetch_proof recorded in nightly verdicts."
    gates.append(
        GateAuditResult(
            gate_id="#56",
            title="Gate 0: Capture & Environment Readiness",
            status="OPEN" if not g0_verified else "VERIFIED",
            verified=g0_verified,
            evidence=g0_evidence,
        )
    )

    # Gate 1 (#29): Wide Row & Factor Engine Integrity
    surface_proof = verdicts_by_check.get("report_surface_proof")
    invariants = verdicts_by_check.get("output_invariants")
    g1_verified = (
        surface_proof is not None and surface_proof.ok is True and invariants is not None and invariants.ok is True
    )
    g1_evidence = (
        f"report_surface_proof: {surface_proof.summary if surface_proof else 'missing'}; "
        f"output_invariants: {invariants.summary if invariants else 'missing'}"
    )
    gates.append(
        GateAuditResult(
            gate_id="#29",
            title="Gate 1: Wide Row and Output Invariants",
            status="OPEN" if not g1_verified else "VERIFIED",
            verified=g1_verified,
            evidence=g1_evidence,
        )
    )

    # Gate 2 (#30): Research Questions & Entity Coverage
    cov_qqq = verdicts_by_check.get("question_coverage@universe-list:qqq")
    cov_topt = verdicts_by_check.get("question_coverage@topt")
    g2_verified = cov_qqq is not None and cov_qqq.ok is True and cov_topt is not None and cov_topt.ok is True
    g2_evidence = (
        f"qqq coverage: {cov_qqq.summary if cov_qqq else 'missing'}; "
        f"topt coverage: {cov_topt.summary if cov_topt else 'missing'}"
    )
    gates.append(
        GateAuditResult(
            gate_id="#30",
            title="Gate 2: Research Question Coverage",
            status="OPEN" if not g2_verified else "VERIFIED",
            verified=g2_verified,
            evidence=g2_evidence,
        )
    )

    # Gate 3 (#31): Governed Strategy & Backtest Simulation
    bt_verdict = verdicts_by_check.get("nightly_backtest")
    g3_verified = bt_verdict is not None and bt_verdict.ok is True
    g3_evidence = (
        bt_verdict.summary if bt_verdict else "nightly_backtest: no verdict recorded at all in target environment."
    )
    gates.append(
        GateAuditResult(
            gate_id="#31",
            title="Gate 3: Backtest Engine and Simulation Integrity",
            status="OPEN" if not g3_verified else "VERIFIED",
            verified=g3_verified,
            evidence=g3_evidence,
        )
    )

    # Gate 4 (#32 & #54): Production Graduation
    market_fresh = verdicts_by_check.get("market_data_freshness")
    g4_verified = all(g.verified for g in gates) and market_fresh is not None and market_fresh.ok is True
    g4_evidence = (
        f"market_data_freshness: {market_fresh.summary if market_fresh else 'missing'}. "
        "Production graduation requires all prior gates VERIFIED and human owner sign-off."
    )
    gates.append(
        GateAuditResult(
            gate_id="#54",
            title="Gate 4: Production Graduation & Vision Completion",
            status="OPEN" if not g4_verified else "VERIFIED",
            verified=g4_verified,
            evidence=g4_evidence,
        )
    )

    all_passed = all(g.verified for g in gates)

    return VisionAuditReport(
        target_env=env,
        health_url=resolved_url,
        release=release_sha,
        all_passed=all_passed,
        gates=gates,
        nightly_verdicts_summary=verdicts_summary,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit TrueAlpha Vision & Gate Epics")
    parser.add_argument(
        "--env",
        choices=["production", "staging"],
        default="production",
        help="Target environment to audit (default: production)",
    )
    parser.add_argument(
        "--health-url",
        default=None,
        help="Explicit health endpoint URL override",
    )
    parser.add_argument(
        "--fail-closed",
        action="store_true",
        help="Exit with non-zero status if any gate criterion is not verified",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON summary",
    )
    args = parser.parse_args()

    report = audit_vision(env=args.env, health_url=args.health_url)

    if args.json:
        print(
            json.dumps(
                {
                    "target_env": report.target_env,
                    "health_url": report.health_url,
                    "release": report.release,
                    "all_passed": report.all_passed,
                    "gates": [asdict(g) for g in report.gates],
                    "nightly_verdicts_summary": report.nightly_verdicts_summary,
                },
                indent=2,
            )
        )
    else:
        print(f"=== TrueAlpha Vision Audit [{report.target_env.upper()}] ===")
        print(f"Health Endpoint: {report.health_url}")
        print(f"Deployed Release: {report.release or 'unknown'}")
        print("")
        for g in report.gates:
            badge = "PASS" if g.verified else "FAIL"
            print(f"[{badge}] {g.gate_id} {g.title}: {g.status}")
            print(f"       Evidence: {g.evidence}")
        print("")
        if report.all_passed:
            print("Status: All Gate Epics physically verified.")
        else:
            print("Status: System contains open gates and unfulfilled physical criteria.")

    if args.fail_closed and not report.all_passed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
