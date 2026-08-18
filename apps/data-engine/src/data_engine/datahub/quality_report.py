"""Row-complete DataHub quality report for a capture run (#61 / #404).

Library form of the report the one-shot script produced, so the deployed Dagster
pipeline (#27) can persist it inside the same transaction as the capture it
grades. Computes, over the exact requested denominator, the terminal/coverage/
availability/freshness/independent-reconciliation/lineage/mean-confidence
figures from the capture tables, and persists one append-only
`mart.datahub_quality_report` row.

`independent_reconciliation` is computed by the accepted fusion engine
`reconcile_source_assertions` (#343): every multi-source market-price cell's
assertions are reconciled under a declared tolerance/priority policy, the
per-cell outcome is persisted in the report payload, and only AGREED cells
count as independently reconciled — a raw origin count never does.

`availability` and `lineage_completeness` are falsifiable (#537): each is
computed from the thing a row claims rather than from the row existing. Both
metrics used to read `1.0000` no matter what the run actually produced —
availability counted observation rows (Staging's 2026-07-30 13:01 tick reported
84/84 for a run with zero complete strategy inputs) and lineage_completeness
verified a `raw.fetches` join while Production's bucket held exactly one object.
`tests/production_topt/test_persistence.py` arms that property: one deliberately
broken cell per failure mode must drive the corresponding metric below 1.0.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

import psycopg
from factors.production_topt import OperatingBranch
from psycopg.types.json import Jsonb
from pydantic import ValidationError
from truealpha_contracts import canonical_sha256
from truealpha_contracts.models import RawObjectRef
from truealpha_contracts.reconciliation import (
    ReconciliationCell,
    ReconciliationOutcome,
    ReconciliationPolicy,
    SourceAssertion,
    reconcile_source_assertions,
)
from truealpha_contracts.universe import SubjectKind, SubjectRef
from truealpha_runtime import S3RawObjectStore

from data_engine.datahub.production_topt.materialization import (
    FinancialFactPayload,
    IdentityPayload,
    MarketPricePayload,
)
from data_engine.datahub.production_topt.parser_identity import PARSER_VERSION_HISTORY
from data_engine.datahub.production_topt.twelve_data_origin import PARSER_VERSION as TWELVE_DATA_PARSER_VERSION
from data_engine.datahub.production_topt.twelve_data_origin import VALUE_KEY as TWELVE_DATA_VALUE_KEY

# Declared fusion policy for the dual-origin market-price cells (init.md rule 12):
# yahoo-chart is the pinned primary, twelve-data the independent second origin; a
# 0.1% relative tolerance absorbs vendor rounding, and disagreement beyond it
# abstains and reports rather than letting either source win silently.
RECONCILIATION_POLICY = ReconciliationPolicy(
    policy_version="market-price-fusion:v1",
    source_priority=("yahoo-chart:v1", "twelve-data:v1"),
    absolute_tolerance=Decimal("0"),
    relative_tolerance=Decimal("0.001"),
    minimum_independent_origin_groups=2,
)
# Which origin group each parser vintage's observations belong to.
#
# EVERY primary vintage is enumerated, not just the current one, and the enumeration comes
# from `PARSER_VERSION_HISTORY` rather than being written out here. Two earlier attempts at
# this were both half-right: a literal copy of the current version drifted the moment it was
# bumped, and importing the current version fixed only the current version — when v4 shipped,
# every observation already in the warehouse (all v3) fell out of the map and a report over a
# historical run resolved `insufficient_independent_origins` for all 21 cells, silently, for
# runs that had agreed 21/21 across two origins (#543).
#
# Deriving from the history makes both failures unreachable: a vintage cannot be current
# without being in the tuple, and cannot leave the tuple once shipped. A report over a
# historical run keeps resolving its origin because the vintage it was captured under is
# still listed.
_SOURCE_BY_PARSER = {
    **{vintage: ("yahoo-chart:v1", "origin:yahoo:v1", "close") for vintage in PARSER_VERSION_HISTORY},
    TWELVE_DATA_PARSER_VERSION: ("twelve-data:v1", "origin:twelve-data:v1", TWELVE_DATA_VALUE_KEY),
    # v1 asserted `price` — Twelve Data's last trade, extended hours included — against the
    # primary's regular-session close, so every tick inside a session abstained (#535). Listed
    # by hand because the second origin has no version history to derive from yet; the primary
    # side above shows the shape that would make this maintenance unnecessary.
    "twelve-data-parser:v1": ("twelve-data:v1", "origin:twelve-data:v1", "price"),
}


# What "a usable value" means for each requested semantic. The payload contracts are the
# ones the mart itself parses (`materialization._snapshot_member`), and the financial-fact
# requirement is the one the factor itself consumes
# (`factors.production_topt.core.compute_topt_gppe`: the capital charge, the denominator,
# and the branch's operating numerator). Reading the same fields is what stops the report
# and the page disagreeing about one run — the report used to answer 84/84 for a tick the
# mart scored 19 available / 1 unavailable.
_IDENTITY_SEMANTICS = frozenset({"listing-identity", "universe-membership"})
# Mirrors `compute_topt_gppe`'s dispatch exactly: FINANCIAL scores through
# pre-provision profit, every other branch through gross profit (the insurance
# parse lands revenue-minus-claims INTO gross_profit). This map must stay total
# over `OperatingBranch` — the first deployed tick after INSURANCE was added
# (#534) crashed on BRK.B's cell with a KeyError here, aborting the whole run,
# because CI's fixture emitted only the two branches this map then covered.
# `test_the_numerator_map_is_total_over_operating_branches` turns red on the
# next branch added without a row here.
_FINANCIAL_FACT_OPERATING_NUMERATOR = {
    OperatingBranch.FINANCIAL: "pre_provision_profit",
    OperatingBranch.NON_FINANCIAL: "gross_profit",
    OperatingBranch.INSURANCE: "gross_profit",
}


def _has_usable_value(semantic_type: str, payload: dict[str, Any] | None) -> bool:
    """Does this observation's normalized payload carry the value its cell was requested for?

    An unparseable payload and a payload whose headline value is null are both "no": the
    obligation is terminally resolved and the row is there, but nothing downstream can use
    it. An unknown semantic type is also "no" — a new semantic must declare what usable
    means for it, because defaulting to yes is exactly how this metric got pinned at
    1.0000 in the first place.
    """
    if payload is None:
        return False
    try:
        if semantic_type in _IDENTITY_SEMANTICS:
            IdentityPayload.model_validate(payload)
            return True
        if semantic_type == "market-price":
            return MarketPricePayload.model_validate(payload).close is not None
        if semantic_type == "financial-fact":
            fact = FinancialFactPayload.model_validate(payload)
            numerator = getattr(fact, _FINANCIAL_FACT_OPERATING_NUMERATOR[fact.operating_branch])
            return all(value is not None for value in (fact.total_assets, fact.headcount, numerator))
    except ValidationError:
        return False
    return False


# ---- Class-B plausibility: single-source semantics get domain falsifiers (#578) ----
# A financial-fact cell has no second vendor to disagree with it, so its
# corroboration is accounting identity and domain bounds. Each rule returns its
# name when VIOLATED; every rule has a fixture that triggers it (D8 — a rule
# that cannot fire measures nothing). Rule names are report vocabulary.
_PER_EMPLOYEE_FLOOR = Decimal("1000")
_PER_EMPLOYEE_CEILING = Decimal("20000000")


def _plausibility_violations(fact: FinancialFactPayload) -> list[str]:
    violated: list[str] = []
    if fact.gross_profit is not None and fact.revenue is not None and fact.gross_profit > fact.revenue:
        # Gross profit strictly above revenue is impossible accounting, not an
        # aggressive margin. Equality is deliberately allowed: payment networks
        # legitimately run at ~zero COGS (#533's approved proxy).
        violated.append("gross_profit_exceeds_revenue")
    if fact.pre_provision_profit is not None and fact.revenue is not None and fact.pre_provision_profit > fact.revenue:
        violated.append("pre_provision_profit_exceeds_revenue")
    for name in ("headcount", "total_assets", "shares_outstanding"):
        value = getattr(fact, name)
        if value is not None and value <= 0:
            violated.append(f"nonpositive_{name}")
    # The branch's own numerator, via the same dispatch the factor and
    # _has_usable_value use — choosing "whichever field is present" diverges the
    # moment a payload carries both (Copilot on #599); totality over the branch
    # enum is guarded by test_the_numerator_map_is_total_over_operating_branches.
    numerator = getattr(fact, _FINANCIAL_FACT_OPERATING_NUMERATOR[fact.operating_branch])
    if numerator is not None and fact.headcount is not None and fact.headcount > 0:
        per_employee = numerator / fact.headcount
        if not (_PER_EMPLOYEE_FLOOR <= per_employee <= _PER_EMPLOYEE_CEILING):
            # The glance a human applies, mechanized: $1K-$20M gross profit per
            # employee brackets every legitimate issuer in the universe with an
            # order of magnitude to spare on both sides; a 2010 share count or a
            # revenue-as-gross-profit substitution lands outside it.
            violated.append("per_employee_outside_domain")
    return violated


class _ObjectReader(Protocol):
    """The one thing lineage verification needs from an object store."""

    def get(self, ref: RawObjectRef) -> bytes: ...


class _PointerDereferencer:
    """Answers whether a `raw.fetches` pointer resolves to the bytes it claims.

    `_insert_fetch`'s docstring (`persistence.py`) names the defect this exists to catch:
    "a pointer nobody can dereference is not evidence". Production held 1016 rows into
    buckets that were never created, one stored object, and `lineage_completeness =
    1.0000` on every report, because the metric only checked that the row joined.

    Results are memoised per (uri, sha256): identical source bytes collapse onto one
    content-addressed object across cells and ticks, so a tick's 84 pointers cost far
    fewer round trips than that. When no store is injected the S3 store is built once and
    its bucket probed once — a dead endpoint then costs one timeout rather than 84.
    """

    def __init__(self, store: _ObjectReader | None = None) -> None:
        self._store = store
        self._resolved = store is not None
        self._cache: dict[tuple[str, str], bool] = {}

    def _reader(self) -> _ObjectReader | None:
        if not self._resolved:
            self._resolved = True
            try:
                store = S3RawObjectStore()
                store.ensure_bucket(create=False)
                self._store = store
            except Exception:
                self._store = None
        return self._store

    def dereferences(self, *, object_uri: str, sha256: str, byte_length: int, content_type: str) -> bool:
        cache_key = (object_uri, sha256)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        reader = self._reader()
        result = False
        bucket, _, key = object_uri.removeprefix("s3://").partition("/")
        if reader is not None and bucket and key:
            try:
                body = reader.get(
                    RawObjectRef(
                        bucket=bucket,
                        key=key,
                        sha256=sha256,
                        byte_length=byte_length,
                        content_type=content_type,
                    )
                )
                # Re-checked here rather than trusted: `S3RawObjectStore.get` verifies the
                # digest, but the port does not oblige every implementation to.
                result = hashlib.sha256(body).hexdigest() == sha256 and len(body) == byte_length
            except Exception:
                result = False
        self._cache[cache_key] = result
        return result


@dataclass
class _Cell:
    """One requested cell's graded facts, folded over the observations bound to it.

    A cell with no observation at all keeps these defaults, which is the honest answer:
    nothing was captured for it.
    """

    available: bool = False
    lineage_complete: bool = False
    fresh: bool = False
    confidence: Decimal | None = None


def latest_run(conn: psycopg.Connection[Any]) -> str:
    row = conn.execute(
        "select run_id from mart.topt_capture_status order by cutoff desc, run_id desc limit 1"
    ).fetchone()
    if row is None:
        raise ValueError("no capture run found")
    return row[0]


def build_report(
    conn: psycopg.Connection[Any], run_id: str, *, object_store: _ObjectReader | None = None
) -> dict[str, Any]:
    """Grade one capture run over its exact requested-cell denominator.

    `object_store` is where lineage pointers are dereferenced; it defaults to the deployed
    S3 store, so the deployed call site needs no argument and tests can inject.
    """
    status = conn.execute(
        """
        select obligation_count, terminal_count, success_count, unchanged_count,
               unavailable_count, skipped_count, failed_count, complete
        from mart.topt_capture_status where run_id = %s
        """,
        (run_id,),
    ).fetchone()
    if status is None:
        raise ValueError(f"no capture status for run {run_id}")
    requested = status[0]

    # One row per (requested cell, bound observation) for this run. Left-joined throughout
    # so a cell that produced nothing still appears — an absent cell must be gradeable, and
    # the payload and the object pointer have to travel with it: they are what
    # `availability` and `lineage_completeness` are read off, rather than the row's mere
    # existence.
    rows = conn.execute(
        """
        select ob.obligation_id,
               regexp_replace(ob.capture_requirement_id, ':v1$', '')      as semantic_type,
               o.subject_id,
               o.observation_id,
               p.normalized_payload,
               o.freshness_state,
               o.confidence,
               f.object_uri,
               f.payload_sha256,
               f.byte_length,
               f.content_type
        from raw.capture_obligations ob
        left join staging.capture_observation_obligations oo
               on oo.capture_obligation_id = ob.obligation_id
        left join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
        left join staging.capture_observation_payloads p on p.observation_id = o.observation_id
        left join raw.capture_source_vintages v on v.source_vintage_id = o.source_vintage_id
        left join raw.fetches f on f.id = v.raw_fetch_id
        where ob.run_id = %s
        order by ob.obligation_id, o.observation_id
        """,
        (run_id,),
    ).fetchall()

    pointers = _PointerDereferencer(object_store)
    cells: dict[str, _Cell] = {}
    plausibility: dict[str, dict[str, Any]] = {}
    for (
        obligation_id,
        semantic_type,
        subject_id,
        observation_id,
        payload,
        freshness_state,
        confidence,
        object_uri,
        payload_sha256,
        byte_length,
        content_type,
    ) in rows:
        cell = cells.setdefault(obligation_id, _Cell())
        if observation_id is None:
            continue
        cell.fresh = cell.fresh or freshness_state == "fresh"
        if confidence is not None:
            cell.confidence = confidence if cell.confidence is None else max(cell.confidence, confidence)
        if not cell.available:
            cell.available = _has_usable_value(semantic_type, payload)
        if semantic_type == "financial-fact" and payload is not None and subject_id is not None:
            try:
                violations = _plausibility_violations(FinancialFactPayload.model_validate(payload))
            except ValidationError:
                pass  # unparseable payloads are availability's finding, not this one's
            else:
                # UNION across a subject's observations: a later plausible parse
                # must never flip an earlier violation off the record (Copilot on
                # #599 — the capture plane can bind several observations to one
                # subject).
                cell_grades = plausibility.setdefault(str(subject_id), {"outcome": "plausible", "violated": []})
                merged = sorted(set(cell_grades["violated"]) | set(violations))
                cell_grades["violated"] = merged
                cell_grades["outcome"] = "implausible" if merged else "plausible"
        if not cell.lineage_complete and payload is not None and object_uri is not None:
            cell.lineage_complete = pointers.dereferences(
                object_uri=object_uri,
                sha256=payload_sha256,
                byte_length=byte_length,
                content_type=content_type,
            )

    available = sum(1 for cell in cells.values() if cell.available)
    lineage_complete = sum(1 for cell in cells.values() if cell.lineage_complete)
    fresh = sum(1 for cell in cells.values() if cell.fresh)
    reconciliation = _reconcile_market_price_cells(conn, run_id)
    independent = sum(1 for cell in reconciliation.values() if cell["outcome"] == ReconciliationOutcome.AGREED.value)
    confidences = [cell.confidence for cell in cells.values() if cell.confidence is not None]
    mean_conf = (sum(confidences) / requested) if requested else Decimal(0)

    def ratio(n: int) -> str:
        return str((Decimal(n) / Decimal(requested)).quantize(Decimal("0.0001"))) if requested else "0"

    return {
        "reconciliation_policy_id": RECONCILIATION_POLICY.policy_id,
        "reconciliation_cells": reconciliation,
        "plausibility_cells": plausibility,
        "implausible_count": sum(1 for cell in plausibility.values() if cell["outcome"] == "implausible"),
        "run_id": run_id,
        "requested_count": requested,
        "terminal_count": status[1],
        "available_count": available,
        "fresh_count": fresh,
        "independently_reconciled_count": independent,
        "lineage_complete_count": lineage_complete,
        "terminal_coverage": ratio(status[1]),
        "availability": ratio(available),
        "freshness": ratio(fresh),
        "independent_reconciliation": ratio(independent),
        "lineage_completeness": ratio(lineage_complete),
        "denominator_mean_confidence": str(Decimal(mean_conf).quantize(Decimal("0.0001"))),
        "complete": bool(status[7]),
    }


_PRIMARY_PRICE_SOURCE = "yahoo-chart:v1"


def _served_day_assertions(
    entries: list[tuple[str, Any, Decimal, str, str, str, str, dict]],
) -> list[tuple[str, Any, Decimal, str, str, str, str, dict]]:
    """Only assertions from the served bar's trading day are comparable.

    The mart serves the primary origin's newest bar, so that bar's day anchors the
    cell. A second origin whose freshest bar is from a DIFFERENT day has not
    published the served day at all — feeding that pair to the fusion engine
    manufactures a value conflict out of a publication lag. 78/102 QQQ cells did
    exactly this on 2026-08-18 (#622): Yahoo's overnight rebuild nulls the latest
    session's close, its cells fell back to Friday, and Friday-vs-Monday graded
    `conflict_abstained` as if the vendors disagreed about a price. Dropping the
    other-day assertions lets the engine grade the day honestly instead:
    single-origin -> INSUFFICIENT_INDEPENDENT_ORIGINS.

    Anchor choice: the primary's newest day when the primary asserted anything
    (that is the day whose value consumers read), else the newest day any origin
    asserted (a primary-less cell is already insufficient; anchoring keeps the
    grade attached to one day rather than a cross-day pair).
    """
    primary_days = [knowable_at for source_id, knowable_at, *_ in entries if source_id == _PRIMARY_PRICE_SOURCE]
    anchor = max(primary_days, default=None) or max(knowable_at for _, knowable_at, *_ in entries)
    return [entry for entry in entries if entry[1].date() == anchor.date()]


def _reconcile_market_price_cells(conn: psycopg.Connection[Any], run_id: str) -> dict[str, dict[str, Any]]:
    """Run the accepted fusion engine over every market-price cell's assertions.

    Each observation (Yahoo primary + Twelve Data second origin) becomes a
    SourceAssertion; the declared policy reconciles them. Returns per-listing
    outcomes for the report payload. Single-assertion cells honestly resolve
    INSUFFICIENT_INDEPENDENT_ORIGINS — counting origins never reconciles values.
    Assertions are first narrowed to the served bar's trading day
    (`_served_day_assertions`) so a publication lag grades as a missing second
    origin, never as a value conflict (#622).
    """
    status = conn.execute("select cutoff from mart.topt_capture_status where run_id = %s", (run_id,)).fetchone()
    if status is None:
        return {}
    cutoff = status[0]
    rows = conn.execute(
        """
        select o.subject_id, o.parser_version, o.knowable_at, o.confidence,
               o.normalized_payload_sha256, o.observation_id,
               v.source_vintage_id, v.raw_object_id, p.normalized_payload,
               (ob.partition_key)::date as partition_date
        from raw.capture_obligations ob
        join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
        join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
        join staging.capture_observation_payloads p on p.observation_id = o.observation_id
        join raw.capture_source_vintages v on v.source_vintage_id = o.source_vintage_id
        where ob.run_id = %s and o.semantic_type = 'market-price'
        order by o.subject_id
        """,
        (run_id,),
    ).fetchall()

    by_listing: dict[str, list[tuple[str, Any, Decimal, str, str, str, str, dict]]] = {}
    partition: date | None = None
    for subject_id, parser, knowable_at, confidence, payload_sha, obs_id, vintage_id, raw_object, payload, part in rows:
        if parser not in _SOURCE_BY_PARSER:
            continue
        partition = partition or part
        source_id, origin_group, value_key = _SOURCE_BY_PARSER[parser]
        value = payload.get(value_key)
        if value is None:
            continue
        by_listing.setdefault(subject_id, []).append(
            (
                source_id,
                knowable_at,
                Decimal(str(confidence)),
                payload_sha,
                obs_id,
                vintage_id,
                raw_object,
                {"origin_group": origin_group, "value": value},
            )
        )

    outcomes: dict[str, dict[str, Any]] = {}
    for listing_id, entries in sorted(by_listing.items()):
        entries = _served_day_assertions(entries)
        cell = ReconciliationCell(
            requirement_id=f"data-requirement:{canonical_sha256({'requirement': 'market-price:v1'})}",
            subject=SubjectRef(kind=SubjectKind.LISTING, id=listing_id),
            field_name="close",
            field_semantics_id=f"field-semantics:{canonical_sha256({'field': 'market-price-close:v1'})}",
            unit="USD",
            valid_from=partition or cutoff.date(),
            valid_to=cutoff.date(),
        )
        assertions = tuple(
            SourceAssertion(
                cell_id=cell.cell_id,
                observation_id=obs_id,
                source_id=source_id,
                origin_group_id=extra["origin_group"],
                knowable_at=knowable_at,
                normalized_value_sha256=payload_sha,
                numeric_value=Decimal(str(extra["value"])),
                confidence_assessment_id=f"confidence-assessment:{payload_sha}",
                confidence_score=confidence,
                lineage_node_ids=(vintage_id, raw_object),
                lineage_complete=True,
            )
            for source_id, knowable_at, confidence, payload_sha, obs_id, vintage_id, raw_object, extra in entries
        )
        result = reconcile_source_assertions(
            cell=cell, assertions=assertions, policy=RECONCILIATION_POLICY, cutoff=cutoff
        )
        outcomes[listing_id] = {
            "outcome": result.outcome.value,
            "origin_groups": len(result.origin_group_ids),
            "selected_source": next(
                (a.source_id for a in assertions if a.assertion_id == result.selected_assertion_id), None
            ),
            "selected_value": None if result.selected_numeric_value is None else str(result.selected_numeric_value),
            "conflicting": len(result.conflicting_assertion_ids),
        }
    return outcomes


def persist(conn: psycopg.Connection[Any], report: dict[str, Any]) -> str:
    content_sha256 = canonical_sha256(report)
    report_id = f"datahub-quality-report:{content_sha256}"
    conn.execute(
        """
        insert into mart.datahub_quality_report (report_id, content_sha256, run_id, requested_count, payload)
        values (%s, %s, %s, %s, %s) on conflict (report_id) do nothing
        """,
        (report_id, content_sha256, report["run_id"], report["requested_count"], Jsonb(report)),
    )
    return report_id
