"""Audit Gate Epics against /api/health verdicts (#28, #54)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from data_engine.environment_universes import scheduled_universe_names
from truealpha_contracts.environment_contract import load_environment_contract
from truealpha_contracts.goal_criteria import load_goal_criteria

from truealpha_tools.nightly_verdicts import EXPECTATIONS_PATH, HealthReport, parse_expectations, read_report

_AUDIT_USER_AGENT = "truealpha-ops-audit/1.0"


def _health_url_for(env: str, *, base_url: str | None = None, domain: str | None = None) -> str:
    resolved_base = (base_url or os.environ.get("TRUEALPHA_BASE_URL") or "").strip().rstrip("/")
    if resolved_base:
        return f"{resolved_base}/api/health"
    resolved_domain = (domain or os.environ.get("TRUEALPHA_DOMAIN") or "").strip().strip(".")
    if not resolved_domain:
        raise ValueError("vision_audit requires --health-url, --base-url, TRUEALPHA_BASE_URL, or TRUEALPHA_DOMAIN")
    tier = env.strip().lower()
    if tier in {"prod", "production"}:
        host = resolved_domain
    elif tier == "staging":
        host = f"truealpha-staging.{resolved_domain}"
    else:
        host = f"{tier}.{resolved_domain}"
    return f"https://{host}/api/health"


def _http_get(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": _AUDIT_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15.0) as resp:  # noqa: S310
            return int(resp.status), resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        return int(exc.code), body.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class GateAuditResult:
    gate_id: str
    title: str
    status: str
    verified: bool
    evidence: str


@dataclass(frozen=True)
class CriterionAuditResult:
    """Verification outcome for one goal criterion."""

    criterion_id: str
    status: str
    verified: bool
    evidence: str
    outcome: str = "pending"


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
    live_health_ok: bool = False
    criteria_summary: dict[str, int] = field(default_factory=dict)
    criteria: list[CriterionAuditResult] = field(default_factory=list)


def _assess_served_head_freshness(report: Any, *, environment: str = "production") -> tuple[bool, str]:
    pointers = getattr(report, "governed_pointers", None)
    if not isinstance(pointers, list) or not pointers:
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
        freshness, availability = str(entry.get("freshness", "fresh")), str(entry.get("availability", "available"))
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


def _audit_goal_criteria(
    verdicts_by_check: Mapping[str, Any],
    *,
    env: str,
    now: datetime | None = None,
) -> tuple[list[CriterionAuditResult], list[str], dict[str, int]]:
    scheduled = set(scheduled_universe_names(env))
    check_now = now or datetime.now(UTC)
    max_age_by_check = {
        name: exp.max_age_hours
        for name, exp in parse_expectations(EXPECTATIONS_PATH.read_text(encoding="utf-8"), sorted(scheduled)).items()
    }
    results: list[CriterionAuditResult] = []
    blockers: list[str] = []
    counts = {"total": 0, "verified": 0, "partial": 0, "pending": 0, "failed_or_stale": 0}
    for criterion in load_goal_criteria().criteria:
        counts["total"] += 1
        applicable = [key for key in criterion.verdict_keys if "@" not in key or key.split("@", 1)[1] in scheduled]
        if criterion.verdict_keys and not applicable:
            counts["verified"] += 1
            results.append(
                CriterionAuditResult(
                    criterion_id=criterion.id,
                    status=criterion.status,
                    verified=True,
                    evidence=f"not scheduled in {env}",
                    outcome="not_applicable",
                )
            )
            continue
        if not applicable:
            if criterion.status == "backed":
                counts["verified"] += 1
                results.append(
                    CriterionAuditResult(
                        criterion_id=criterion.id,
                        status=criterion.status,
                        verified=True,
                        evidence="backed by contract/CI suite",
                        outcome="verified",
                    )
                )
            else:
                outcome_kind = "partial" if criterion.status == "partial" else "pending"
                counts[outcome_kind] += 1
                detail = f"awaiting {criterion.pending_on}" if criterion.pending_on else "unwired"
                blockers.append(f"{criterion.id}: {detail}")
                results.append(
                    CriterionAuditResult(
                        criterion_id=criterion.id,
                        status=criterion.status,
                        verified=False,
                        evidence=detail,
                        outcome=outcome_kind,
                    )
                )
            continue

        parts: list[str] = []
        failed_present = False
        stale_present = False
        missing_any = False
        for key in applicable:
            verdict = verdicts_by_check.get(key)
            if verdict is None:
                missing_any = True
                parts.append(f"{key}=missing")
                continue
            age_hours = (check_now - verdict.ran_at).total_seconds() / 3600.0
            limit_hours = max_age_by_check.get(key, 48.0)
            if age_hours > limit_hours:
                stale_present = True
                parts.append(f"{key}=STALE({age_hours:.1f}h>{limit_hours:.0f}h)")
            elif verdict.ok is True:
                parts.append(f"{key}=ok({verdict.summary})")
            elif verdict.ok is None:
                missing_any = True
                parts.append(f"{key}=pending({verdict.summary})")
            else:
                failed_present = True
                parts.append(f"{key}=FAIL({verdict.summary})")

        if failed_present or stale_present or missing_any:
            counts["failed_or_stale"] += 1
            verified = False
            outcome_kind = "failed_or_stale"
            blockers.append(f"{criterion.id}: {'; '.join(parts)}")
        elif criterion.status == "partial":
            counts["partial"] += 1
            verified = False
            outcome_kind = "partial"
            if criterion.pending_on:
                parts.append(f"partial({criterion.pending_on})")
            blockers.append(f"{criterion.id}: {'; '.join(parts)}")
        elif criterion.status == "pending":
            counts["pending"] += 1
            verified = False
            outcome_kind = "pending"
            if criterion.pending_on:
                parts.append(f"pending({criterion.pending_on})")
            blockers.append(f"{criterion.id}: {'; '.join(parts)}")
        else:
            counts["verified"] += 1
            verified = True
            outcome_kind = "verified"
        results.append(
            CriterionAuditResult(
                criterion_id=criterion.id,
                status=criterion.status,
                verified=verified,
                evidence="; ".join(parts),
                outcome=outcome_kind,
            )
        )
    return results, blockers, counts


def audit_vision(
    *,
    env: str = "production",
    health_url: str | None = None,
    base_url: str | None = None,
    oracle_outcomes: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> VisionAuditReport:
    resolved_url = health_url or _health_url_for(env, base_url=base_url)
    check_now = now or datetime.now(UTC)
    try:
        report: HealthReport = read_report(resolved_url, _http_get)
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

    criteria_results, criteria_blockers, criteria_summary = _audit_goal_criteria(
        verdicts_by_check, env=env, now=check_now
    )
    market_fresh = verdicts_by_check.get("market_data_freshness")
    core_gates = [
        _verdict_gate(
            "#56",
            "Gate 0: Capture & Environment Readiness",
            verdicts_by_check.get("job_run_health"),
            "No job_run_health recorded in nightly verdicts.",
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
    live_health_ok = bool(
        core_gates[0].verified and heads_fresh and market_fresh is not None and market_fresh.ok is True
    )
    gates = [
        *core_gates,
        _gate(
            "#54",
            "Gate 4: Production Graduation & Vision Completion",
            all(g.verified for g in core_gates)
            and heads_fresh
            and market_fresh is not None
            and market_fresh.ok is True
            and not criteria_blockers,
            f"market_data_freshness: {market_fresh.summary if market_fresh else 'missing'}. "
            + f"criteria: {criteria_summary['verified']}/{criteria_summary['total']} verified. "
            + (f"criteria_blockers: {len(criteria_blockers)}. " if criteria_blockers else "")
            + "Production graduation requires all prior gates VERIFIED and human owner sign-off.",
        ),
    ]
    if not heads_fresh and gates:
        last = gates[-1]
        gates[-1] = GateAuditResult(last.gate_id, last.title, "OPEN", False, f"{last.evidence} [{freshness_detail}]")
    all_passed = all(g.verified for g in gates) and heads_fresh and not criteria_blockers
    return VisionAuditReport(
        target_env=env,
        health_url=resolved_url,
        release=release_sha,
        all_passed=all_passed,
        gates=gates,
        nightly_verdicts_summary=verdicts_summary,
        status="pass" if all_passed else "fail",
        served_head_freshness="fresh" if heads_fresh else freshness_detail,
        live_health_ok=live_health_ok,
        criteria_summary=criteria_summary,
        criteria=criteria_results,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit TrueAlpha Vision & Gate Epics")
    parser.add_argument("--env", choices=["production", "staging"], default="production")
    parser.add_argument("--health-url", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--fail-closed", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = audit_vision(env=args.env, health_url=args.health_url, base_url=args.base_url)
    exit_code = 0 if (not args.fail_closed or report.all_passed) else 1
    if args.json:
        print(json.dumps(asdict(report), indent=2))
        return exit_code
    print(f"=== TrueAlpha Vision Audit [{report.target_env.upper()}] ===")
    print(f"Health Endpoint: {report.health_url}\nDeployed Release: {report.release or 'unknown'}")
    print(f"Live Runtime Health: {'PASS' if report.live_health_ok else 'FAIL'} ({report.served_head_freshness})")
    if report.criteria_summary:
        cs = report.criteria_summary
        print(
            f"Vision Criteria: {cs.get('verified', 0)}/{cs.get('total', 0)} verified "
            f"(partial={cs.get('partial', 0)}, pending={cs.get('pending', 0)}, failed_or_stale={cs.get('failed_or_stale', 0)})"
        )
    print()
    for g in report.gates:
        print(f"[{'PASS' if g.verified else 'FAIL'}] {g.gate_id} {g.title}: {g.status}\n       Evidence: {g.evidence}")
    for c in report.criteria:
        tag = c.outcome.upper()
        print(f"[{tag:15s}] {c.criterion_id} ({c.status}): {c.evidence}")
    print(
        "\nStatus: All Gate Epics physically verified."
        if report.all_passed
        else "\nStatus: System contains open gates and unfulfilled physical criteria."
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
