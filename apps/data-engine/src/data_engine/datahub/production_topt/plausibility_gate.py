"""The plausibility gate on a tick (#544): judge this run's published rows against the
plausibility policy (v4 since #1443), in the tick's own transaction, before the pointer advances.

`lanes/capture.py` calls `judge_issuers` right after materialization, before anything consumes
the run. An issuer refusal that is not exempted names one listing: a tick that runs the strategy
excludes that issuer's decision with the refusal's reason, and every tick publishes the others
(#1323). The only run refusal is `judge_strategy`'s empty eligible set: it fails the Dagster run,
the transaction rolls back with it, and `mart.current_pointer` stays on the previous accepted run.
The gate reads no other run (#1443). Exemptions come from the same file the nightly suite reads
(`tools/output_invariant_exemptions.json`, issue + expiry): `node-sign-policy` is judged under the
suite's entry of the same id because it is the same question judged earlier. An expired
exemption is no exemption.

A negative value whose forest node declares `sign-is-signal` is not a violation, and it is
not silent either: `Verdict.signals` carries it and `lines()` prints it on every tick (#528).
"""

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
from truealpha_contracts.strategy import ExclusionReason

#: The nightly suite's exemption file, as the image carries it (Dockerfile) or in the repo.
_EXEMPTIONS_IMAGE = Path("/app/tools/output_invariant_exemptions.json")
_EXEMPTIONS_REPO = Path(__file__).resolve().parents[6] / "tools" / "output_invariant_exemptions.json"
#: Gate rule -> nightly invariant whose exemption also defers the gate.
_SHARED_EXEMPTION = {RULE_NODE_SIGN_POLICY: "node-sign-policy"}

_ROWS_SQL = """
    select r.listing_id, r.issuer_id, r.operating_branch, r.availability, r.operating_efficiency
    from mart.topt_core_results r
    where r.run_id = %s
    order by r.listing_id
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
    #: Issuer refusals in force: the tick excludes each refused issuer and publishes the others.
    refusals: tuple[Violation, ...]
    #: Run refusals: the tick fails and its transaction rolls back.
    run_refusals: tuple[Violation, ...]
    deferred: tuple[tuple[Violation, str], ...]
    #: Negative values their node declares `sign-is-signal`: accepted, and printed by name.
    signals: tuple[Violation, ...] = ()
    #: listing id -> issuer id of every judged row, so a refusal reaches the issuer's decision.
    issuers: Mapping[str, str] = field(default_factory=dict)

    @property
    def refused(self) -> bool:
        """True when the whole run is refused. An issuer refusal alone never refuses the run."""
        return bool(self.run_refusals)

    def excluded_issuers(self) -> dict[str, ExclusionReason]:
        """issuer id -> exclusion reason for every refused listing, first refusal first."""
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
    """The run's published rows, and the issuer id of each listing."""
    rows: list[Row] = []
    issuers: dict[str, str] = {}
    for listing_id, issuer_id, branch, availability, op in connection.execute(_ROWS_SQL, (run_id,)).fetchall():
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
    """invariant id -> note, for exemptions that are still in force today."""
    candidates = [path] if path is not None else [_EXEMPTIONS_IMAGE, _EXEMPTIONS_REPO]
    for candidate in candidates:
        if candidate is not None and candidate.exists():
            payload = json.loads(candidate.read_text())
            break
    else:
        return {}
    today = today or datetime.now(UTC).date()
    live: dict[str, str] = {}
    for entry in payload.get("exemptions", []):
        expires = date.fromisoformat(str(entry["expires"]))
        if expires >= today:
            live[str(entry["invariant"])] = f"exempt until {expires} under {entry['issue']}"
    return live


def judge_issuers(
    connection: Any,
    *,
    run_id: str,
    exemptions_path: Path | None = None,
    today: date | None = None,
) -> Verdict:
    """The policy's issuer rules over this run's published rows. Never excludes, advances or
    refuses anything itself: the caller excludes `Verdict.excluded_issuers()` and turns
    `Verdict.refused` into the Dagster failure that rolls the tick back."""
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
    """`verdict` plus the empty-eligible-set rule over the strategy run the tick wrote with the
    verdict's exclusions (#527). None for both means the tick ran no strategy."""
    outcomes = (
        {
            str(outcome): int(count)
            for outcome, count in connection.execute(_OUTCOMES_SQL, (strategy_run_id,)).fetchall()
        }
        if strategy_run_id
        else None
    )
    return replace(verdict, run_refusals=(*verdict.run_refusals, *empty_eligible_set(outcomes, l2_complete)))


def judge_run(
    connection: Any,
    *,
    run_id: str,
    strategy_run_id: str | None = None,
    l2_complete: int | None = None,
    exemptions_path: Path | None = None,
    today: date | None = None,
) -> Verdict:
    """`judge_issuers` and `judge_strategy` in one call, for a run whose strategy is already written."""
    verdict = judge_issuers(connection, run_id=run_id, exemptions_path=exemptions_path, today=today)
    return judge_strategy(connection, verdict, strategy_run_id=strategy_run_id, l2_complete=l2_complete)
