"""Plausibility gate on published TOPT core and strategy rows."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from factors.composite.plausibility_policy import (
    EXCLUSION_REASON,
    POLICY_VERSION,
    RULE_NODE_SIGN_POLICY,
    Row,
    Violation,
    empty_eligible_set,
    node_sign_policy,
    sign_signals,
)
from factors.panel import GPPE_BANKING_TCE_SPEC, GPPE_V0_SPEC
from truealpha_contracts.strategy import ExclusionReason

_EXEMPTIONS_IMAGE = Path("/app/tools/output_invariant_exemptions.json")
_EXEMPTIONS_REPO = Path(__file__).resolve().parents[6] / "tools" / "output_invariant_exemptions.json"
_SHARED_EXEMPTION = {RULE_NODE_SIGN_POLICY: "node-sign-policy"}

_ROWS_SQL = """
    select distinct on (m.listing_id)
           m.listing_id,
           mv.issuer_id,
           coalesce(m.factor_input ->> 'operating_branch', 'non_financial') as operating_branch,
           case when mv.availability_status in ('available', 'low_confidence') then 'available' else 'unavailable' end as availability,
           mv.value as operating_efficiency
    from mart.metric_node_values mv
    join staging.core_snapshots s on s.run_id = mv.run_id
    join staging.core_snapshot_members m on m.snapshot_id = s.snapshot_id and m.issuer_id = mv.issuer_id
    where mv.run_id = %(run_id)s
      and (
          (coalesce(m.factor_input ->> 'operating_branch', 'non_financial') = 'financial'
           and mv.node_key = %(bank_key)s)
          or mv.node_key = %(base_key)s
      )
    order by m.listing_id, (mv.node_key = %(bank_key)s) desc
"""

_OUTCOMES_SQL = """
    select d.outcome, count(*)::int
    from mart.strategy_decisions d
    where d.strategy_run_id = %s
    group by d.outcome
"""


@dataclass(frozen=True)
class Verdict:
    policy_version: str
    refusals: tuple[Violation, ...]
    run_refusals: tuple[Violation, ...]
    deferred: tuple[tuple[Violation, str], ...]
    signals: tuple[Violation, ...] = ()
    issuers: Mapping[str, str] = field(default_factory=dict)

    @property
    def refused(self) -> bool:
        return bool(self.run_refusals)

    def excluded_issuers(self) -> dict[str, ExclusionReason]:
        excluded: dict[str, ExclusionReason] = {}
        for violation in self.refusals:
            if violation.listing_id is None:
                raise ValueError(f"issuer refusal without a listing: {violation}")
            excluded.setdefault(self.issuers[violation.listing_id], EXCLUSION_REASON[violation.rule])
        return excluded

    def lines(self) -> list[str]:
        out = [f"plausibility policy {self.policy_version}"]
        for signal in self.signals:
            out.append(f"  SIGNAL   {signal.rule} {signal.listing_id or ''}: {signal.detail} — a declared low signal")
        for violation, note in self.deferred:
            out.append(f"  DEFERRED {violation.rule} {violation.listing_id or ''}: {violation.detail} — {note}")
        for violation in self.refusals:
            out.append(
                f"  EXCLUDED {violation.rule} {violation.listing_id or ''}: {violation.detail} — refused as "
                f"{EXCLUSION_REASON[violation.rule].value}; the run publishes the other listings"
            )
        for violation in self.run_refusals:
            out.append(f"  REFUSED  {violation.rule}: {violation.detail}")
        if not self.refusals and not self.run_refusals and not self.deferred:
            out.append("  ok: every published row respects its definition")
        return out


def _rows(connection: Any, run_id: str) -> tuple[list[Row], dict[str, str]]:
    rows: list[Row] = []
    issuers: dict[str, str] = {}
    for listing_id, issuer_id, branch, availability, op in connection.execute(
        _ROWS_SQL,
        {"run_id": run_id, "bank_key": GPPE_BANKING_TCE_SPEC.root, "base_key": GPPE_V0_SPEC.root},
    ).fetchall():
        rows.append(
            Row(
                listing_id=str(listing_id),
                operating_branch=str(branch),
                availability=str(availability),
                operating_efficiency=Decimal(str(op)) if op is not None else None,
            )
        )
        issuers[str(listing_id)] = str(issuer_id)
    return rows, issuers


def _exemptions(path: Path | None = None, today: date | None = None) -> dict[str, str]:
    candidates = [path] if path is not None else [_EXEMPTIONS_IMAGE, _EXEMPTIONS_REPO]
    for candidate in candidates:
        if candidate is not None and candidate.exists():
            payload = json.loads(candidate.read_text())
            break
    else:
        return {}
    today = today or datetime.now(UTC).date()
    return {
        str(entry["invariant"]): f"exempt until {expires} under {entry['issue']}"
        for entry in payload.get("exemptions", [])
        if (expires := date.fromisoformat(str(entry["expires"]))) >= today
    }


def judge_issuers(
    connection: Any,
    *,
    run_id: str,
    exemptions_path: Path | None = None,
    today: date | None = None,
) -> Verdict:
    current, issuers = _rows(connection, run_id)
    live = _exemptions(exemptions_path, today)
    refusals: list[Violation] = []
    deferred: list[tuple[Violation, str]] = []
    for violation in node_sign_policy(current):
        shared = _SHARED_EXEMPTION.get(violation.rule)
        note = live.get(shared) if shared else None
        if note:
            deferred.append((violation, note))
        else:
            refusals.append(violation)
    return Verdict(
        policy_version=POLICY_VERSION,
        refusals=tuple(refusals),
        run_refusals=(),
        deferred=tuple(deferred),
        signals=tuple(sign_signals(current)),
        issuers=issuers,
    )


def judge_strategy(
    connection: Any, verdict: Verdict, *, strategy_run_id: str | None, l2_complete: int | None
) -> Verdict:
    outcomes = (
        {
            str(outcome): int(count)
            for outcome, count in connection.execute(_OUTCOMES_SQL, (strategy_run_id,)).fetchall()
        }
        if strategy_run_id
        else None
    )
    return replace(verdict, run_refusals=(*verdict.run_refusals, *empty_eligible_set(outcomes, l2_complete)))
