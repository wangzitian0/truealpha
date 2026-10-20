"""The plausibility gate on a tick (#544): judge this run's published rows against the
plausibility policy (v4 since #1443), in the tick's own transaction, before the pointer advances.

`lanes/capture.py` calls `judge_issuers` right after materialization, before anything consumes
the run. An issuer refusal that is not exempted names one listing. A tick that runs the strategy
excludes that issuer's decision with the refusal's reason and publishes the others (#1323). A tick
without a strategy logs the refusal, and the refused core row stays published (#1456). A run
refusal (more than the policy's share of listings refused, or `judge_strategy`'s empty eligible
set) fails the Dagster run. The transaction rolls back with it, so nothing of the refused run is
materialised and `mart.current_pointer` stays on the previous accepted run. Exemptions come from the same file the
nightly suite reads (`tools/output_invariant_exemptions.json`, issue + expiry): `node-sign-policy`
is judged under the suite's entry of the same id because it is the same question judged earlier.
An expired exemption is no exemption.

The independent reference of a row is the run's own corroborating financial-fact observation for
the listing: an origin other than the primary (`quality_report.classify_financial_fact_entry`), in
the primary's currency. The previous accepted run is read only for the policy's change detectors:
a review line on the tick, never a refusal (#1443).

A negative value whose forest node declares `sign-is-signal` is not a violation, and it is
not silent either: `Verdict.signals` carries it and `lines()` prints it on every tick (#528).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from factors.composite.plausibility_policy import (
    EXCLUSION_REASON,
    POLICY_VERSION,
    RULE_NODE_SIGN_POLICY,
    IndependentReference,
    Row,
    Violation,
    empty_eligible_set,
    evaluate_issuers,
    judged_listings,
    policy_sha256,
    refused_share_of_run,
    reviews,
    sign_signals,
)
from truealpha_contracts.strategy import ExclusionReason

from data_engine.datahub.a1_evidence import POINTER_FACTOR_ID
from data_engine.datahub.quality_report import classify_financial_fact_entry, financial_fact_unit

#: The nightly suite's exemption file, as the image carries it (Dockerfile) or in the repo.
_EXEMPTIONS_IMAGE = Path("/app/tools/output_invariant_exemptions.json")
_EXEMPTIONS_REPO = Path(__file__).resolve().parents[6] / "tools" / "output_invariant_exemptions.json"
#: Gate rule -> nightly invariant whose exemption also defers the gate.
_SHARED_EXEMPTION = {RULE_NODE_SIGN_POLICY: "node-sign-policy"}
#: The figures an independent reference reports to the policy (`plausibility_policy._served_figures`).
_REFERENCE_FIGURES = ("revenue", "gross_profit")

# `last_close` is the price the run's own core result was computed from: the market-price
# observation among the result's `input_observation_ids` for the result's own listing (a
# dual-class issuer's result carries one per class). It used to be joined from
# `staging.strategy_backtest_inputs` on (issuer_id, cutoff), which has no run at all
# (#877): a forced tick (#874) or a re-published tick seeds a second vintage at the same
# cutoff and every row came back once per vintage, with either run's price; and a QQQ or
# canary result read TOPT's seeded price for an issuer the two universes key alike. The
# run's own observation is what `current_ps` was computed from, so a forced correction of
# a price is judged against the price it corrected, not against the run it replaced.
# `fact` is the financial-fact observation the same row was computed from, found the same way
# (#1443): the served revenue and gross profit the independent reference judges.
_ROWS_SQL = """
    select r.listing_id, r.operating_branch, r.availability, r.operating_efficiency, r.current_ps,
           price.last_close, r.revenue_period_end, fact.normalized_payload
    from mart.topt_core_results r
    left join lateral (
        select (payload.normalized_payload->>'close')::numeric as last_close
        from staging.capture_normalized_observations observation
        join staging.capture_observation_payloads payload using (observation_id)
        where observation.observation_id = any(r.input_observation_ids)
          and observation.semantic_type = 'market-price'
          and payload.normalized_payload->>'listing_id' = r.listing_id
    ) price on true
    left join lateral (
        select payload.normalized_payload
        from staging.capture_normalized_observations observation
        join staging.capture_observation_payloads payload using (observation_id)
        where observation.observation_id = any(r.input_observation_ids)
          and observation.semantic_type = 'financial-fact'
          and payload.normalized_payload->>'listing_id' = r.listing_id
    ) fact on true
    where r.run_id = %s
    order by r.listing_id
"""

_ISSUERS_SQL = "select listing_id, issuer_id from mart.topt_core_results where run_id = %s"

# Every financial-fact observation the capture run holds, the corroborating origins included: the
# rows the quality report fuses (`quality_report._reconcile_financial_fact_cells`).
_RUN_FINANCIAL_FACTS_SQL = """
    select o.parser_version, o.knowable_at, o.confidence, o.normalized_payload_sha256, o.observation_id,
           v.source_vintage_id, v.raw_object_id, p.normalized_payload
    from raw.capture_obligations ob
    join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
    join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
    join staging.capture_observation_payloads p on p.observation_id = o.observation_id
    join raw.capture_source_vintages v on v.source_vintage_id = o.source_vintage_id
    where ob.run_id = %s and o.semantic_type = 'financial-fact'
    order by o.observation_id
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
    where head.environment = (select environment from mart.environment_identity)
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
    #: Issuer refusals in force: the tick excludes each refused issuer and publishes the others.
    refusals: tuple[Violation, ...]
    #: Run refusals: the tick fails and its transaction rolls back.
    run_refusals: tuple[Violation, ...]
    deferred: tuple[tuple[Violation, str], ...]
    #: Change detectors against the previous accepted run: printed for review, never a refusal.
    reviews: tuple[Violation, ...] = ()
    #: Negative values their node declares `sign-is-signal`: accepted, and printed by name.
    signals: tuple[Violation, ...] = ()
    #: listing id -> issuer id of every judged row, so a refusal reaches the issuer's decision.
    issuers: Mapping[str, str] = field(default_factory=dict)
    #: Listings a reference judged, of all listings of the run. 0 judged means no reference arrived.
    judged: int = 0
    listings: int = 0
    policy_sha256: str = field(default_factory=policy_sha256)

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

    def excluded_lines(self) -> list[str]:
        """One `<rule> <listing>` entry per refused listing and rule, for the op metadata."""
        return sorted({f"{violation.rule} {violation.listing_id}" for violation in self.refusals})

    def lines(self) -> list[str]:
        out = [
            f"plausibility policy {self.policy_version} ({self.policy_sha256[:12]}): previous accepted run "
            f"{self.previous_run_id or '(none)'}; {self.judged} of {self.listings} listings judged against an "
            "independent reference"
        ]
        for signal in self.signals:
            out.append(f"  SIGNAL   {signal.rule} {signal.listing_id or ''}: {signal.detail} — a declared low signal")
        for review in self.reviews:
            out.append(
                f"  REVIEW   {review.rule} {review.listing_id or ''}: {review.detail} — a change against our own "
                "previous run, not a refusal"
            )
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
            out.append(f"  ok: no refusal; {self.judged} of {self.listings} listings judged")
        return out


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _date(value: object) -> date | None:
    return date.fromisoformat(value) if isinstance(value, str) else None


def _reference(entry_payload: Mapping[str, Any], reference_id: str) -> IndependentReference:
    periods = entry_payload.get("by_period_end")
    figures: dict[date, dict[str, Decimal | None]] = {}
    if isinstance(periods, Mapping):
        for end, values in periods.items():
            if isinstance(end, str) and isinstance(values, Mapping):
                figures[date.fromisoformat(end)] = {name: _decimal(values.get(name)) for name in _REFERENCE_FIGURES}
    return IndependentReference(reference_id=reference_id, figures=figures)


def _references(connection: Any, run_id: str) -> dict[str, list[tuple[str, IndependentReference]]]:
    """listing id -> (currency, reference) for every corroborating financial-fact observation of the run."""
    found: dict[str, list[tuple[str, IndependentReference]]] = {}
    for (
        parser,
        knowable_at,
        confidence,
        payload_sha,
        observation_id,
        vintage_id,
        raw_object,
        payload,
    ) in connection.execute(_RUN_FINANCIAL_FACTS_SQL, (run_id,)).fetchall():
        entry = classify_financial_fact_entry(
            parser, knowable_at, Decimal(str(confidence)), payload_sha, observation_id, vintage_id, raw_object, payload
        )
        listing_id = payload.get("listing_id") if isinstance(payload, Mapping) else None
        if entry is None or entry.is_primary or not isinstance(listing_id, str):
            continue
        found.setdefault(listing_id, []).append((financial_fact_unit(payload), _reference(payload, observation_id)))
    return found


def _rows(connection: Any, run_id: str, *, with_references: bool = True) -> list[Row]:
    """The run's published rows with their served figures and, unless `with_references` is False,
    their independent references. The change detectors read the previous run without them."""
    references = _references(connection, run_id) if with_references else {}
    rows: list[Row] = []
    for listing_id, branch, availability, op, ps, close, revenue_period_end, fact in connection.execute(
        _ROWS_SQL, (run_id,)
    ).fetchall():
        listing = str(listing_id)
        served: Mapping[str, Any] = fact if isinstance(fact, Mapping) else {}
        unit = financial_fact_unit(served)
        vintage = served.get("vintage")
        gross_profit_vintage = vintage.get("gross_profit") if isinstance(vintage, Mapping) else None
        rows.append(
            Row(
                listing_id=listing,
                operating_branch=str(branch),
                availability=str(availability),
                operating_efficiency=_decimal(op),
                current_ps=_decimal(ps),
                last_close=_decimal(close),
                revenue_period_end=revenue_period_end,
                revenue=_decimal(served.get("revenue")),
                gross_profit=_decimal(served.get("gross_profit")),
                operating_period_end=_date(served.get("operating_period_end")),
                gross_profit_is_revenue_proxy=isinstance(gross_profit_vintage, Mapping)
                and gross_profit_vintage.get("is_revenue_proxy") is True,
                # A figure in another currency is not the same number (the fusion's rule).
                references=tuple(
                    reference for currency, reference in references.get(listing, ()) if fact and currency == unit
                ),
            )
        )
    return rows


def _issuers(connection: Any, run_id: str) -> dict[str, str]:
    """listing id -> issuer id of every published row of the run."""
    return {str(listing): str(issuer) for listing, issuer in connection.execute(_ISSUERS_SQL, (run_id,)).fetchall()}


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


def judge_issuers(
    connection: Any,
    *,
    run_id: str,
    exemptions_path: Path | None = None,
    today: date | None = None,
) -> Verdict:
    """The policy's issuer rules over this run's published rows, the refused share of the run, and
    the change detectors against the previous accepted run of the same universe. Never excludes,
    advances or refuses anything itself: the caller excludes `Verdict.excluded_issuers()` and turns
    `Verdict.refused` into the Dagster failure that rolls the tick back."""
    current = _rows(connection, run_id)
    universe = connection.execute(_RUN_UNIVERSE_SQL, (run_id,)).fetchone()
    if universe is None:
        raise RuntimeError(f"run {run_id} has no frozen snapshot; the gate judges materialized runs only")
    universe_id, universe_version = str(universe[0]), str(universe[1])
    previous_row = connection.execute(_PREVIOUS_HEAD_SQL, (POINTER_FACTOR_ID, universe_id, universe_version)).fetchone()
    previous_run_id = str(previous_row[0]) if previous_row and previous_row[0] != run_id else None
    previous = _rows(connection, previous_run_id, with_references=False) if previous_run_id else []
    live = _exemptions(exemptions_path, today)
    refusals: list[Violation] = []
    deferred: list[tuple[Violation, str]] = []
    for violation in evaluate_issuers(current):
        shared = _SHARED_EXEMPTION.get(violation.rule)
        note = live.get(shared) if shared else None
        if note:
            deferred.append((violation, note))
        else:
            refusals.append(violation)
    refused_listings = {violation.listing_id for violation in refusals if violation.listing_id is not None}
    return Verdict(
        policy_version=POLICY_VERSION,
        previous_run_id=previous_run_id,
        refusals=tuple(refusals),
        run_refusals=tuple(refused_share_of_run(current, refused_listings)),
        deferred=tuple(deferred),
        reviews=tuple(reviews(current, previous)),
        signals=tuple(sign_signals(current)),
        issuers=_issuers(connection, run_id),
        judged=len(judged_listings(current)),
        listings=len({row.listing_id for row in current}),
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
