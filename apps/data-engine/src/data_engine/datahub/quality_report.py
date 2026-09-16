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

Every field of the price bar is its own reconciliation cell (`reconcile_price_bar`):
open, high, low and close under the price policy, volume under its own. A cell's
headline outcome stays the close's (the served value, what the a1 pointer gate and
the admin page read); the per-field grades sit under `fields` and are summarised in
`field_reconciliation`, so the report can say how many metrics — not how many
cells — reached two agreeing origins.

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
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

import psycopg
from factors.production_topt import OperatingBranch
from psycopg.types.json import Jsonb
from pydantic import ValidationError
from truealpha_contracts.common import canonical_sha256
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
from data_engine.datahub.production_topt.source_registrations import (
    RELEASE_SEMANTICS,
    SOURCE_BY_PARSER,
    registration_for,
)

# Declared fusion policy for the dual-origin market-price cells (init.md rule 12):
# yahoo-chart is the pinned primary, twelve-data the independent second origin;
# disagreement beyond tolerance abstains and reports rather than letting either
# source win silently.
#
# v1 -> v2 (#719): tolerance 0.1% -> 0.3%. The 0.1% calibration dated from the
# era when cross-run reuse fed BOTH origins the same bound observation, so
# "agreement" was byte-identity and the tolerance was never exercised. #691
# (identity-coordinate reuse) made TOPT capture genuinely twice, and the honest
# consolidated-tape vs primary-close spread surfaced immediately: 2026-08-31
# AAPL 317.23/316.85 (0.12%), AVGO 370.82/370.34 (0.13%), MU 956.68/958.73
# (0.21%) — three abstentions, corroborated share 18/21 < 0.95, and the TOPT
# governed pointer refused for four days on vendor microstructure noise. 30bp
# clears the observed spread with margin while a real error (splits, 2x, fat
# fingers) still lands orders of magnitude outside it; the plausibility oracle
# and the #705 recompute oracle own that regime.
#
# v2 -> v3: a third origin, moomoo's daily K-line close (`moomoo_origin`), joins the
# priority list last. The tolerance is unchanged; conflict behaviour is unchanged (any
# disagreeing representative abstains the cell — three origins do not out-vote). A source
# absent from the priority list is `unregistered` to the engine and silently excluded,
# which is why the policy has to change for the origin to count at all.
RECONCILIATION_POLICY = ReconciliationPolicy(
    policy_version="market-price-fusion:v3",
    source_priority=("yahoo-chart:v1", "twelve-data:v1", "moomoo-kline:v1"),
    absolute_tolerance=Decimal("0"),
    relative_tolerance=Decimal("0.003"),
    minimum_independent_origin_groups=2,
)
# Volume is not a price. It is each vendor's own aggregation of the consolidated tape,
# and it settles later than the prices do (late prints, corrections), so it gets its
# own policy rather than the 30bp one. The number is measured where it can be: the
# settled 2026-08-14 AAPL bars on the cassette pair agree EXACTLY on volume
# (28,186,700 from both vendors, `test_real_vendor_bytes`), so the tolerance exists
# for the same-evening capture, where the primary's consolidated figure is still
# absorbing late prints. 2% (200bp) covers that regime with margin, while a
# primary-listing-only count — roughly half the consolidated tape, the different
# quantity a volume mix-up produces — stays a conflict by an order of magnitude.
# Provisional the way the price policy's v1 was: the staging soak measures the real
# spread and this number moves from that measurement, in a version (#719's lesson).
# A volume conflict never touches the close's grade, so miscalibration here cannot
# freeze the governed pointer the way #719's did.
VOLUME_RECONCILIATION_POLICY = ReconciliationPolicy(
    policy_version="market-volume-fusion:v1",
    source_priority=RECONCILIATION_POLICY.source_priority,
    absolute_tolerance=Decimal("0"),
    relative_tolerance=Decimal("0.02"),
    minimum_independent_origin_groups=2,
)
# The served value: its grade is the cell's headline outcome.
_HEADLINE_FIELD = "close"
# Every field of the session's bar, each reconciled as its own cell under its own
# policy. Open/high/low are the same quantity class as close, from the same two
# tapes, and share its policy.
PRICE_BAR_FIELDS: tuple[str, ...] = ("open", "high", "low", "close", "volume")
FIELD_RECONCILIATION_POLICIES: dict[str, ReconciliationPolicy] = {
    "open": RECONCILIATION_POLICY,
    "high": RECONCILIATION_POLICY,
    "low": RECONCILIATION_POLICY,
    "close": RECONCILIATION_POLICY,
    "volume": VOLUME_RECONCILIATION_POLICY,
}
_FIELD_UNITS: dict[str, str] = {"open": "USD", "high": "USD", "low": "USD", "close": "USD", "volume": "shares"}


# Declared fusion policy for the dual-origin financial-fact cells: SEC company-facts is
# the pinned primary, moomoo's vendor-normalized statements the independent second origin,
# reconciled PER FIELD at the primary's fiscal period end. A vendor-normalized statement
# and an XBRL fact differ by definition, not only by rounding: measured on the four
# captured issuers (DDOG, DUOL, NICE, SHOP; FY2023-FY2025) revenue, gross profit, total
# assets and EPS are byte-equal and net income differs by at most 0.70% (NICE: moomoo
# reports ProfitLoss before minority interest, the primary NetIncomeLoss after it). 1%
# clears that with margin while a wrong period, a currency, or a units error still lands
# orders of magnitude outside; disagreement abstains and reports, as for prices.
FINANCIAL_FACT_RECONCILIATION_POLICY = ReconciliationPolicy(
    policy_version="financial-fact-fusion:v1",
    source_priority=("sec-company-facts:v1", "moomoo-financials:v1"),
    absolute_tolerance=Decimal("0"),
    relative_tolerance=Decimal("0.01"),
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
# still listed. Since #72 the derivation lives with the source registrations: every
# registered origin declares its vintages, and this map is built from all of them.
_SOURCE_BY_PARSER = SOURCE_BY_PARSER


# What "a usable value" means for each requested semantic. The payload contracts are the
# ones the mart itself parses (`materialization._snapshot_member`), and the financial-fact
# requirement is the one the factor itself consumes
# (`factors.production_topt.core.compute_topt_gppe`: the capital charge, the denominator,
# and the branch's operating numerator). Reading the same fields is what stops the report
# and the page disagreeing about one run — the report used to answer 84/84 for a tick the
# mart scored 19 available / 1 unavailable.
_IDENTITY_SEMANTICS = RELEASE_SEMANTICS
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


# Which semantics each SERVED factor needs usable, per subject, before the factor
# can produce a value for that subject (#641 D4). The headline `availability` is an
# equal-weight average over ALL semantics, so 89 missing financial-fact cells hide
# behind identity/membership/price at 4:1 — 0.78 while only 12 of 101 issuers were
# factor-computable. This block states the consumer-relevant number.
_FACTOR_REQUIRED_SEMANTICS: dict[str, tuple[str, ...]] = {
    "gross_profit_per_employee": ("financial-fact",),
}


def _factor_availability(usable_by_subject: dict[str, dict[str, bool]]) -> dict[str, dict[str, Any]]:
    """Per-factor availability over subjects: a subject counts only when EVERY
    semantic the factor requires is usable for it. Subjects lacking any required
    obligation count in the denominator — absence is a shortfall, not an exemption."""
    out: dict[str, dict[str, Any]] = {}
    for factor_id, required in _FACTOR_REQUIRED_SEMANTICS.items():
        # EVERY graded subject is in the denominator — a subject captured with no
        # required-semantic obligation at all is a shortfall, not an exemption
        # (review on #644: filtering to subjects that HAVE the semantic key
        # contradicted exactly that promise).
        universe = list(usable_by_subject)
        complete = [
            subject for subject in universe if all(usable_by_subject[subject].get(sem, False) for sem in required)
        ]
        ratio = (
            (Decimal(len(complete)) / Decimal(len(universe))).quantize(Decimal("0.0001")) if universe else Decimal(0)
        )
        out[factor_id] = {
            "required_semantics": list(required),
            "complete_subjects": len(complete),
            "universe_subjects": len(universe),
            "ratio": str(ratio),
        }
    return out


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
               ob.subject_id                                              as obligation_subject,
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
    usable_by_subject: dict[str, dict[str, bool]] = {}
    for (
        obligation_id,
        semantic_type,
        obligation_subject,
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
        subject_semantics = usable_by_subject.setdefault(str(obligation_subject), {})
        subject_semantics.setdefault(semantic_type, False)
        if observation_id is None:
            continue
        cell.fresh = cell.fresh or freshness_state == "fresh"
        if confidence is not None:
            cell.confidence = confidence if cell.confidence is None else max(cell.confidence, confidence)
        if not cell.available:
            cell.available = _has_usable_value(semantic_type, payload)
        if cell.available:
            subject_semantics[semantic_type] = True
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
    field_reconciliation = _field_reconciliation(reconciliation)


    financial_reconciliation = _reconcile_financial_fact_cells(conn, run_id)
    independent = sum(
        1 for cell in reconciliation.values() if cell["outcome"] == ReconciliationOutcome.AGREED.value
    ) + sum(1 for cell in financial_reconciliation.values() if cell["outcome"] == ReconciliationOutcome.AGREED.value)
    confidences = [cell.confidence for cell in cells.values() if cell.confidence is not None]
    mean_conf = (sum(confidences) / requested) if requested else Decimal(0)

    def ratio(n: int) -> str:
        return str((Decimal(n) / Decimal(requested)).quantize(Decimal("0.0001"))) if requested else "0"

    return {
        "reconciliation_policy_id": RECONCILIATION_POLICY.policy_id,
        "reconciliation_cells": reconciliation,
        "field_reconciliation": field_reconciliation,


        # Financial-fact cells under their own policy, per subject with per-field detail;
        # `independently_reconciled_count` counts a subject whose every compared field agreed.
        "financial_fact_reconciliation_policy_id": FINANCIAL_FACT_RECONCILIATION_POLICY.policy_id,
        "financial_fact_reconciliation_cells": financial_reconciliation,
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
        "factor_availability": _factor_availability(usable_by_subject),
        "complete": bool(status[7]),
    }


# The policy's first priority IS the primary; deriving it here means a re-prioritized
# policy re-anchors served-day narrowing automatically (Copilot on #625).
_PRIMARY_PRICE_SOURCE = RECONCILIATION_POLICY.source_priority[0]


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


def _field_reconciliation(cells: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per bar field: how many graded market-price cells reached AGREED, over every
    graded cell — the figure that answers "how many metrics are HIGH". The headline
    `independent_reconciliation` stays the close's, over the full requested denominator."""
    graded = len(cells)
    out: dict[str, dict[str, Any]] = {}
    for field in PRICE_BAR_FIELDS:
        agreed = sum(
            1 for cell in cells.values() if cell["fields"][field]["outcome"] == ReconciliationOutcome.AGREED.value
        )
        share = (Decimal(agreed) / Decimal(graded)).quantize(Decimal("0.0001")) if graded else Decimal(0)
        out[field] = {
            "agreed": agreed,
            "cells": graded,
            "share": str(share),
            "policy_id": FIELD_RECONCILIATION_POLICIES[field].policy_id,
        }
    return out


def reconcile_price_bar(
    listing_id: str,
    entries: list[tuple[str, Any, Decimal, str, str, str, str, dict]],
    *,
    partition: date,
    cutoff: datetime,
) -> dict[str, Any]:
    """One listing's served-day assertions, reconciled one bar field at a time.

    Each field is its own `ReconciliationCell` (its own semantics id, unit and policy)
    and an origin contributes an assertion to a field only when its payload carries a
    value for it: a field one origin left null grades `insufficient_independent_origins`
    for that field alone; a field no origin asserted grades `unavailable`; two nulls
    never agree. The returned cell keeps the close's grade under the headline keys the
    a1 gate and the admin page read, with every field's grade under `fields`.
    """
    fields: dict[str, dict[str, Any]] = {}
    for field in PRICE_BAR_FIELDS:
        policy = FIELD_RECONCILIATION_POLICIES[field]
        cell = ReconciliationCell(
            requirement_id=f"data-requirement:{canonical_sha256({'requirement': 'market-price:v1'})}",
            subject=SubjectRef(kind=SubjectKind.LISTING, id=listing_id),
            field_name=field,
            field_semantics_id=f"field-semantics:{canonical_sha256({'field': f'market-price-{field}:v1'})}",
            unit=_FIELD_UNITS[field],
            valid_from=partition,
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
                numeric_value=Decimal(str(extra["values"][field])),
                confidence_assessment_id=f"confidence-assessment:{payload_sha}",
                confidence_score=confidence,
                lineage_node_ids=(vintage_id, raw_object),
                lineage_complete=True,
            )
            for source_id, knowable_at, confidence, payload_sha, obs_id, vintage_id, raw_object, extra in entries
            if extra["values"].get(field) is not None
        )
        result = reconcile_source_assertions(cell=cell, assertions=assertions, policy=policy, cutoff=cutoff)
        fields[field] = {
            "outcome": result.outcome.value,
            "origin_groups": len(result.origin_group_ids),
            "selected_source": next(
                (a.source_id for a in assertions if a.assertion_id == result.selected_assertion_id), None
            ),
            "selected_value": None if result.selected_numeric_value is None else str(result.selected_numeric_value),
            "conflicting": len(result.conflicting_assertion_ids),
            "policy_id": policy.policy_id,
        }
    headline = {key: value for key, value in fields[_HEADLINE_FIELD].items() if key != "policy_id"}
    return {**headline, "fields": fields}


def _reconcile_market_price_cells(conn: psycopg.Connection[Any], run_id: str) -> dict[str, dict[str, Any]]:
    """Run the accepted fusion engine over every market-price cell's assertions.

    Each observation (Yahoo primary + Twelve Data second origin) becomes one
    SourceAssertion per bar field it carries; the field's declared policy reconciles
    them (`reconcile_price_bar`). Returns per-listing outcomes for the report payload.
    Single-assertion cells honestly resolve INSUFFICIENT_INDEPENDENT_ORIGINS —
    counting origins never reconciles values. Assertions are first narrowed to the
    served bar's trading day (`_served_day_assertions`) so a publication lag grades
    as a missing second origin, never as a value conflict (#622).
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
        # The close under the origin's declared key (v1 of the second origin wrote
        # `price`); every other bar field under its own name, absent on payloads
        # written before parser v10 / twelve-data v3.
        values = {field: (value if field == _HEADLINE_FIELD else payload.get(field)) for field in PRICE_BAR_FIELDS}
        by_listing.setdefault(subject_id, []).append(
            (
                source_id,
                knowable_at,
                Decimal(str(confidence)),
                payload_sha,
                obs_id,
                vintage_id,
                raw_object,
                {"origin_group": origin_group, "values": values},
            )
        )

    outcomes: dict[str, dict[str, Any]] = {}
    for listing_id, entries in sorted(by_listing.items()):
        outcomes[listing_id] = reconcile_price_bar(
            listing_id,
            _served_day_assertions(entries),
            partition=partition or cutoff.date(),
            cutoff=cutoff,
        )
    return outcomes


# --- financial-fact fusion (second origin: moomoo statements) ------------------------------
#
# The primary's parser vintage is the shared primary identity (`PARSER_VERSION_HISTORY`),
# which the registry maps to the market-price primary; within the financial-fact semantic
# it is the owning registration's identity instead. Corroborating vintages resolve through
# the registry like every other origin.
_FINANCIAL_FACT_REGISTRATION = registration_for("financial-fact")
_PRIMARY_FINANCIAL_SOURCE = f"{_FINANCIAL_FACT_REGISTRATION.source_id}:{_FINANCIAL_FACT_REGISTRATION.version}"
_PRIMARY_FINANCIAL_ORIGIN_GROUP = (
    f"origin:{_FINANCIAL_FACT_REGISTRATION.source_id}:{_FINANCIAL_FACT_REGISTRATION.version}"
)
_PRIMARY_FINANCIAL_VINTAGES = frozenset(PARSER_VERSION_HISTORY)
# Per reconciled field: which primary-payload key carries the value and which carries
# the fiscal period end it describes (a dotted path into `vintage` where the payload
# has no dedicated column). A second origin's payload carries the same field names
# inside `by_period_end[<period_end>]`, so alignment is by the primary's period.
_FINANCIAL_FACT_FUSION_FIELDS: dict[str, tuple[str, str]] = {
    "revenue": ("revenue", "revenue_period_end"),
    "gross_profit": ("gross_profit", "operating_period_end"),
    "net_income": ("net_income", "vintage.net_income.period_end"),
    "total_assets": ("total_assets", "vintage.total_assets.period_end"),
}
# The unit of a primary payload that names no currency (vintages before the adapter wrote one).
_FINANCIAL_FACT_UNIT = "USD"
_ISO_CURRENCY = re.compile(r"[A-Z]{3}")


def financial_fact_unit(payload: Mapping[str, Any]) -> str:
    """The currency a financial-fact payload reports in: `sec_financial_adapter` and every
    corroborating origin write `currency` per payload; a payload without one is USD."""
    currency = payload.get("currency")
    return currency if isinstance(currency, str) and _ISO_CURRENCY.fullmatch(currency) else _FINANCIAL_FACT_UNIT


def _payload_path(payload: Mapping[str, Any], path: str) -> Any:
    node: Any = payload
    for key in path.split("."):
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def primary_financial_fields(payload: Mapping[str, Any]) -> dict[str, tuple[Decimal, date]]:
    """field -> (value, period_end) for every fusion field the primary payload asserts
    WITH a dated period. An undated value cannot be aligned and is not compared."""
    out: dict[str, tuple[Decimal, date]] = {}
    for field_name, (value_key, period_path) in _FINANCIAL_FACT_FUSION_FIELDS.items():
        value = payload.get(value_key)
        period = _payload_path(payload, period_path)
        if value is None or not isinstance(period, str):
            continue
        try:
            out[field_name] = (Decimal(str(value)), date.fromisoformat(period))
        except (ValueError, ArithmeticError):
            continue
    return out


def corroborating_financial_value(payload: Mapping[str, Any], field_name: str, period_end: date) -> Decimal | None:
    """The second origin's value for `field_name` at exactly the primary's period end."""
    periods = payload.get("by_period_end")
    if not isinstance(periods, Mapping):
        return None
    values = periods.get(period_end.isoformat())
    if not isinstance(values, Mapping) or values.get(field_name) is None:
        return None
    try:
        return Decimal(str(values[field_name]))
    except ArithmeticError:
        return None


@dataclass(frozen=True)
class FinancialFactEntry:
    """One financial-fact observation as the fusion sees it."""

    source_id: str
    origin_group: str
    knowable_at: datetime
    confidence: Decimal
    payload_sha: str
    observation_id: str
    vintage_id: str
    raw_object: str
    payload: Mapping[str, Any]

    @property
    def is_primary(self) -> bool:
        return self.source_id == _PRIMARY_FINANCIAL_SOURCE


def classify_financial_fact_entry(
    parser_version: str,
    knowable_at: datetime,
    confidence: Decimal,
    payload_sha: str,
    observation_id: str,
    vintage_id: str,
    raw_object: str,
    payload: Mapping[str, Any],
) -> FinancialFactEntry | None:
    """Resolve an observation's origin from its parser vintage: the shared primary vintage
    is the owning registration; anything else must be a registered corroborating origin."""
    if parser_version in _PRIMARY_FINANCIAL_VINTAGES:
        source_id, origin_group = _PRIMARY_FINANCIAL_SOURCE, _PRIMARY_FINANCIAL_ORIGIN_GROUP
    elif parser_version in _SOURCE_BY_PARSER:
        source_id, origin_group, _value_key = _SOURCE_BY_PARSER[parser_version]
    else:
        return None
    return FinancialFactEntry(
        source_id, origin_group, knowable_at, confidence, payload_sha, observation_id, vintage_id, raw_object, payload
    )


def reconcile_financial_fact_entries(
    listing_id: str, entries: Sequence[FinancialFactEntry], *, cutoff: datetime
) -> dict[str, Any]:
    """Run the fusion engine per field over one subject's financial-fact observations.

    Alignment is on the PRIMARY's fiscal period for each field: a second origin asserting
    a different period has not corroborated the served number, so it is absent for that
    field (honest `insufficient_independent_origins`), never a value conflict. The cell's
    unit is the primary's reporting currency, and a second origin reporting in another
    currency is absent the same way — a figure in another unit is not the same number.
    The subject's outcome is AGREED only when every compared field agreed and at least
    one was compared; any conflicting field abstains the subject.
    """
    primaries = [entry for entry in entries if entry.is_primary]
    if not primaries:
        return {"outcome": ReconciliationOutcome.UNAVAILABLE.value, "fields": {}, "origin_groups": 0}
    primary = max(primaries, key=lambda entry: (entry.knowable_at, entry.observation_id))
    unit = financial_fact_unit(primary.payload)
    fields: dict[str, dict[str, Any]] = {}
    outcomes: list[str] = []
    for field_name, (value, period_end) in sorted(primary_financial_fields(primary.payload).items()):
        cell = ReconciliationCell(
            requirement_id=f"data-requirement:{canonical_sha256({'requirement': 'financial-fact:v1'})}",
            subject=SubjectRef(kind=SubjectKind.LISTING, id=listing_id),
            field_name=field_name,
            field_semantics_id=f"field-semantics:{canonical_sha256({'field': f'financial-fact-{field_name}:v1'})}",
            unit=unit,
            valid_from=period_end,
            valid_to=max(period_end, cutoff.date()),
        )
        assertions = [_financial_assertion(cell, primary, value)]
        for entry in entries:
            if entry.is_primary or financial_fact_unit(entry.payload) != unit:
                continue
            other = corroborating_financial_value(entry.payload, field_name, period_end)
            if other is not None:
                assertions.append(_financial_assertion(cell, entry, other))
        result = reconcile_source_assertions(
            cell=cell, assertions=tuple(assertions), policy=FINANCIAL_FACT_RECONCILIATION_POLICY, cutoff=cutoff
        )
        fields[field_name] = {
            "outcome": result.outcome.value,
            "period_end": period_end.isoformat(),
            "unit": unit,
            "origin_groups": len(result.origin_group_ids),
            "selected_source": next(
                (a.source_id for a in assertions if a.assertion_id == result.selected_assertion_id), None
            ),
            "selected_value": None if result.selected_numeric_value is None else str(result.selected_numeric_value),
            "conflicting": len(result.conflicting_assertion_ids),
        }
        outcomes.append(result.outcome.value)
    if any(outcome == ReconciliationOutcome.CONFLICT_ABSTAINED.value for outcome in outcomes):
        overall = ReconciliationOutcome.CONFLICT_ABSTAINED.value
    elif outcomes and all(outcome == ReconciliationOutcome.AGREED.value for outcome in outcomes):
        overall = ReconciliationOutcome.AGREED.value
    elif outcomes:
        overall = ReconciliationOutcome.INSUFFICIENT_INDEPENDENT_ORIGINS.value
    else:
        overall = ReconciliationOutcome.UNAVAILABLE.value
    return {
        "outcome": overall,
        "origin_groups": len({entry.origin_group for entry in entries}),
        "fields": fields,
    }


def _financial_assertion(cell: ReconciliationCell, entry: FinancialFactEntry, value: Decimal) -> SourceAssertion:
    return SourceAssertion(
        cell_id=cell.cell_id,
        observation_id=entry.observation_id,
        source_id=entry.source_id,
        origin_group_id=entry.origin_group,
        knowable_at=entry.knowable_at,
        normalized_value_sha256=entry.payload_sha,
        numeric_value=value,
        confidence_assessment_id=f"confidence-assessment:{entry.payload_sha}",
        confidence_score=entry.confidence,
        lineage_node_ids=(entry.vintage_id, entry.raw_object),
        lineage_complete=True,
    )


def _reconcile_financial_fact_cells(conn: psycopg.Connection[Any], run_id: str) -> dict[str, dict[str, Any]]:
    """Every financial-fact cell's assertions through the fusion engine, per field."""
    status = conn.execute("select cutoff from mart.topt_capture_status where run_id = %s", (run_id,)).fetchone()
    if status is None:
        return {}
    cutoff = status[0]
    rows = conn.execute(
        """
        select o.subject_id, o.parser_version, o.knowable_at, o.confidence,
               o.normalized_payload_sha256, o.observation_id,
               v.source_vintage_id, v.raw_object_id, p.normalized_payload
        from raw.capture_obligations ob
        join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
        join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
        join staging.capture_observation_payloads p on p.observation_id = o.observation_id
        join raw.capture_source_vintages v on v.source_vintage_id = o.source_vintage_id
        where ob.run_id = %s and o.semantic_type = 'financial-fact'
        order by o.subject_id
        """,
        (run_id,),
    ).fetchall()
    by_listing: dict[str, list[FinancialFactEntry]] = {}
    for subject_id, parser, knowable_at, confidence, payload_sha, obs_id, vintage_id, raw_object, payload in rows:
        entry = classify_financial_fact_entry(
            parser, knowable_at, Decimal(str(confidence)), payload_sha, obs_id, vintage_id, raw_object, payload
        )
        if entry is not None:
            by_listing.setdefault(subject_id, []).append(entry)
    return {
        listing_id: reconcile_financial_fact_entries(listing_id, entries, cutoff=cutoff)
        for listing_id, entries in sorted(by_listing.items())
    }


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
