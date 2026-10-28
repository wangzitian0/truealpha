"""TrueAlpha Vision Audit tool (Issue #28 and Issue #54).

Evaluates the physical state of the TrueAlpha system against the root acceptance
criteria in init.md section 0 and Gate Epics (#56, #29, #30, #31, #32, #54).

Usage:
    python tools/vision_audit.py [--env production|staging] [--fail-closed] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from data_engine.environment_universes import scheduled_universe_names
from infra2_sdk.deploy import default_http_get
from truealpha_contracts.environment_contract import load_environment_contract

from truealpha_tools.nightly_verdicts import HealthReport, read_report


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


def _assess_served_head_freshness(
    report: Any,
    *,
    environment: str = "production",
) -> tuple[bool, str]:
    """Verify that mart.served_head.freshness == 'fresh' across scheduled governed pointers (#54, #1299).

    Every scheduled pointer in `report.governed_pointers` must carry `freshness == 'fresh'` and
    `availability == 'available'`. Manual-only lanes in an environment (#1256) do not fail the
    check. An empty list, missing `governed_pointers`, or 'unknown' pointer state fails closed.
    """
    pointers = getattr(report, "governed_pointers", None)
    if pointers is None or pointers == "unknown" or not isinstance(pointers, list) or not pointers:
        return False, "mart.served_head.freshness=unknown (no governed pointers)"
    stale: list[str] = []
    scheduled_seen = 0
    for entry in pointers:
        if not isinstance(entry, dict):
            stale.append(str(entry))
            continue
        universe = str(entry.get("universe_id", "?"))
        if not load_environment_contract().is_head_scheduled(universe, environment):
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


def _finalize_vision_report(
    *,
    env: str,
    resolved_url: str,
    release_sha: str | None,
    gates: list[GateAuditResult],
    verdicts_summary: dict[str, Any],
    heads_fresh: bool,
    freshness_detail: str,
) -> VisionAuditReport:
    if not heads_fresh and gates:
        last = gates[-1]
        gates[-1] = GateAuditResult(
            gate_id=last.gate_id,
            title=last.title,
            status="OPEN",
            verified=False,
            evidence=f"{last.evidence} [{freshness_detail}]",
        )
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


def _count_oracle_agrees(oracle_outcomes: Any) -> int:
    """Return the count of `agree` rows from an oracle outcomes rollup or sequence (#1523 G19)."""
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


def _audit_gate_1(verdicts_by_check: Mapping[str, Any], oracle_outcomes: Any) -> GateAuditResult:
    """Evaluate Gate 1 (#29) from surface proof, output invariants, and oracle agrees (#1523 G19)."""
    surface_proof = verdicts_by_check.get("report_surface_proof")
    invariants = verdicts_by_check.get("output_invariants")
    agree_count = _count_oracle_agrees(oracle_outcomes)
    verified = (
        agree_count > 0
        and surface_proof is not None
        and surface_proof.ok is True
        and invariants is not None
        and invariants.ok is True
    )
    oracle_label = f"oracle_outcomes: {agree_count} agree" if agree_count > 0 else "no comparator"
    evidence = (
        f"{oracle_label}; "
        f"report_surface_proof: {surface_proof.summary if surface_proof else 'missing'}; "
        f"output_invariants: {invariants.summary if invariants else 'missing'}"
    )
    return GateAuditResult(
        gate_id="#29",
        title="Gate 1: Wide Row and Output Invariants",
        status="OPEN" if not verified else "VERIFIED",
        verified=verified,
        evidence=evidence,
    )


def audit_vision(
    *,
    env: str = "production",
    health_url: str | None = None,
    oracle_outcomes: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None = None,
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
        verdicts_summary: dict[str, Any] = {
            v.check: {"ok": v.ok, "summary": v.summary, "ran_at": v.ran_at.isoformat()} for v in verdicts_list
        }
        release_sha = report.release
        heads_fresh, freshness_detail = _assess_served_head_freshness(report, environment=env)
        effective_outcomes = getattr(report, "oracle_outcomes", None) if oracle_outcomes is None else oracle_outcomes
    except Exception as exc:  # noqa: BLE001
        verdicts_by_check = {}
        verdicts_summary = {"error": str(exc)}
        release_sha = None
        heads_fresh, freshness_detail = False, "mart.served_head.freshness=unknown"
        effective_outcomes = oracle_outcomes

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

    gates.append(_audit_gate_1(verdicts_by_check, effective_outcomes))

    # Gate 2 (#30): Research Questions & Entity Coverage. The environment contract lists the universes
    # that the environment runs (staging runs TOPT only), and each of them needs a green coverage verdict.
    coverage = {
        universe: verdicts_by_check.get(f"question_coverage@{universe}") for universe in scheduled_universe_names(env)
    }
    g2_verified = bool(coverage) and all(verdict is not None and verdict.ok is True for verdict in coverage.values())
    g2_evidence = (
        "; ".join(
            f"{universe} coverage: {verdict.summary if verdict else 'missing'}"
            for universe, verdict in coverage.items()
        )
        or f"the contract schedules no universe in {env}"
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

    return _finalize_vision_report(
        env=env,
        resolved_url=resolved_url,
        release_sha=release_sha,
        gates=gates,
        verdicts_summary=verdicts_summary,
        heads_fresh=heads_fresh,
        freshness_detail=freshness_detail,
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
                    "status": report.status,
                    "target_env": report.target_env,
                    "health_url": report.health_url,
                    "release": report.release,
                    "all_passed": report.all_passed,
                    "served_head_freshness": report.served_head_freshness,
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

    return 0


if __name__ == "__main__":
    sys.exit(main())
