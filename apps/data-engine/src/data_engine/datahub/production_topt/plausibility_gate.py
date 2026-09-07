"""The plausibility gate on a tick (#544): judge this run's published rows against
policy v1 and the previous accepted run, in the tick's own transaction, before the
pointer advances.

`lanes/capture.py` calls `judge_run` after materialization (and after the strategy
replay when the tick runs one). A violation that is not exempted fails the Dagster run;
the transaction rolls back with it, so nothing of the refused run is materialised and
`mart.current_pointer` stays on the previous accepted run — the behaviour the pointer
design already implies. Exemptions come from the same file the nightly suite reads
(`tools/output_invariant_exemptions.json`, issue + expiry): `sign-per-branch` is judged
under the suite's `gppe-not-negative` entry because it is the same defect judged
earlier. An expired exemption is no exemption.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from factors.composite.plausibility_policy import (
    POLICY_VERSION,
    RULE_SIGN_PER_BRANCH,
    Row,
    Violation,
    evaluate,
)

from data_engine.datahub.a1_evidence import POINTER_FACTOR_ID

#: The nightly suite's exemption file, as the image carries it (Dockerfile) or in the repo.
_EXEMPTIONS_IMAGE = Path("/app/tools/output_invariant_exemptions.json")
_EXEMPTIONS_REPO = Path(__file__).resolve().parents[6] / "tools" / "output_invariant_exemptions.json"
#: Gate rule -> nightly invariant whose exemption also defers the gate.
_SHARED_EXEMPTION = {RULE_SIGN_PER_BRANCH: "gppe-not-negative"}

_ROWS_SQL = """
    select r.listing_id, r.operating_branch, r.availability, r.operating_efficiency, r.current_ps,
           p.value as last_close
    from mart.topt_core_results r
    left join staging.strategy_backtest_inputs p
      on p.issuer_id = r.issuer_id and p.cutoff_at = r.cutoff and p.input_key = 'last_close'
    where r.run_id = %s
    order by r.listing_id
"""

# The previous accepted run for the SAME universe: the head is keyed by universe
# (check_factor_contract), and a canary head must never stand in for the core's.
# The full governed key (review on #764): (environment, universe_id, universe_version,
# factor_id). The environment literal is the one every head consumer carries today —
# #756 measures it; until then this stays in step with POINTER_HEAD_SQL and
# tools/output_invariants.GOVERNED_HEAD rather than diverging from them.
_PREVIOUS_HEAD_SQL = """
    select head.target_run_id
    from mart.current_pointer_head head
    where head.environment = 'production'
      and head.factor_id = %s
      and head.universe_id = %s
      and head.universe_version = %s
    order by head.advanced_at desc
    limit 1
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
    previous_run_id: str | None
    violations: tuple[Violation, ...]
    deferred: tuple[tuple[Violation, str], ...]

    @property
    def refused(self) -> bool:
        return bool(self.violations)

    def lines(self) -> list[str]:
        out = [f"plausibility policy {self.policy_version}: previous accepted run {self.previous_run_id or '(none)'}"]
        for violation, note in self.deferred:
            out.append(f"  DEFERRED {violation.rule} {violation.listing_id or ''}: {violation.detail} — {note}")
        for violation in self.violations:
            out.append(f"  REFUSED  {violation.rule} {violation.listing_id or ''}: {violation.detail}")
        if not self.violations and not self.deferred:
            out.append("  ok: every published row is physically possible against the previous accepted run")
        return out


def _rows(connection: Any, run_id: str) -> list[Row]:
    return [
        Row(
            listing_id=str(listing_id),
            operating_branch=str(branch),
            availability=str(availability),
            operating_efficiency=Decimal(str(op)) if op is not None else None,
            current_ps=Decimal(str(ps)) if ps is not None else None,
            last_close=Decimal(str(close)) if close is not None else None,
        )
        for listing_id, branch, availability, op, ps, close in connection.execute(_ROWS_SQL, (run_id,)).fetchall()
    ]


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


_RUN_UNIVERSE_SQL = "select universe_id, universe_version from staging.topt_core_snapshots where run_id = %s"


def judge_run(
    connection: Any,
    *,
    run_id: str,
    strategy_run_id: str | None = None,
    l2_complete: int | None = None,
    exemptions_path: Path | None = None,
    today: date | None = None,
) -> Verdict:
    """Policy v1 over this run's published rows against the previous accepted run of the
    same universe. Never advances or refuses anything itself: the caller turns
    `Verdict.refused` into the Dagster failure that rolls the tick back."""
    current = _rows(connection, run_id)
    universe = connection.execute(_RUN_UNIVERSE_SQL, (run_id,)).fetchone()
    if universe is None:
        raise RuntimeError(f"run {run_id} has no frozen snapshot; the gate judges materialized runs only")
    universe_id, universe_version = str(universe[0]), str(universe[1])
    previous_row = connection.execute(_PREVIOUS_HEAD_SQL, (POINTER_FACTOR_ID, universe_id, universe_version)).fetchone()
    previous_run_id = str(previous_row[0]) if previous_row and previous_row[0] != run_id else None
    previous = _rows(connection, previous_run_id) if previous_run_id else []
    outcomes = (
        {
            str(outcome): int(count)
            for outcome, count in connection.execute(_OUTCOMES_SQL, (strategy_run_id,)).fetchall()
        }
        if strategy_run_id
        else None
    )
    found = evaluate(current, previous, outcomes=outcomes, l2_complete=l2_complete)
    live = _exemptions(exemptions_path, today)
    violations: list[Violation] = []
    deferred: list[tuple[Violation, str]] = []
    for violation in found:
        shared = _SHARED_EXEMPTION.get(violation.rule)
        note = live.get(shared) if shared else None
        if note:
            deferred.append((violation, note))
        else:
            violations.append(violation)
    return Verdict(POLICY_VERSION, previous_run_id, tuple(violations), tuple(deferred))
