"""Inline-XBRL segment revenue extraction and consolidated-revenue balancing (#772, #830)."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from functools import partial
from typing import Any

from factors.shared.extraction import (
    RULE_SINGLE_SEGMENT,
    Candidate,
    Partition,
    PartitionRefusal,
    SetPartition,
    select_first_balancing_set,
)
from truealpha_contracts.standards import MetricStandard, confidence_for

from data_engine.datahub.production_topt.concept_mapping import DEFAULT_RULESET
from data_engine.datahub.production_topt.source_registrations import AsOfAxis, knowable_instant
from data_engine.datahub.standards.filing_extraction import (
    ExtractionOutcome,
    fetch_annual_filing,
    filing_plain_text,
)
from data_engine.datahub.standards.inline_xbrl import InlineXbrl, TaggedFact

_SEGMENT_COUNT_CONCEPTS = ("us-gaap:NumberOfReportableSegments", "us-gaap:NumberOfOperatingSegments")
_SINGLE_SEGMENT_SPAN = 240
_STATEMENT_LEAD = 90


@dataclass(frozen=True)
class DeclaredSegmentCount:
    concept: str
    value: int | None
    period_end: date
    statement: str | None

    @property
    def evidence(self) -> str:
        return f"{self.concept}={self.value}@{self.period_end.isoformat()}"


def declared_segment_count(filing: bytes | InlineXbrl) -> DeclaredSegmentCount | None:
    """Return the whole-filer segment count at the latest tagged period, preferring reportable over operating."""
    document = filing if isinstance(filing, InlineXbrl) else InlineXbrl(filing)
    stated: dict[str, list[TaggedFact]] = {}
    for fact in document.facts(_SEGMENT_COUNT_CONCEPTS):
        if not fact.context.dimensions:
            stated.setdefault(fact.concept, []).append(fact)
    for concept in _SEGMENT_COUNT_CONCEPTS:
        facts = stated.get(concept)
        if not facts:
            continue
        latest = max(fact.context.end for fact in facts)
        at_latest = [fact for fact in facts if fact.context.end == latest]
        values = {_whole(fact.value) for fact in at_latest}
        value = values.pop() if len(values) == 1 else None
        printed = [fact.offset for fact in at_latest if not fact.hidden]
        statement = (
            document.sentence_at(printed[0], lead=_STATEMENT_LEAD, span=_SINGLE_SEGMENT_SPAN)
            if value is not None and printed
            else None
        )
        return DeclaredSegmentCount(concept, value, latest, statement)
    return None


def _whole(value: Decimal | None) -> int | None:
    return int(value) if value is not None and value == value.to_integral_value() else None


def _ruleset_concepts(*fields: str) -> tuple[str, ...]:
    names: dict[str, None] = {}
    for field in fields:
        mapping = DEFAULT_RULESET.mapping_for(field)
        if mapping is None:
            raise LookupError(f"the concept ruleset declares no {field!r} field")
        for item in mapping.concepts:
            names.setdefault(f"{item.taxonomy}:{item.concept}", None)
    return tuple(names)


_REVENUE_CONCEPTS = _ruleset_concepts("revenue", "bank_revenue")
_SEGMENT_AXIS = "us-gaap:StatementBusinessSegmentsAxis"
_PRODUCT_AXIS = "srt:ProductOrServiceAxis"
_CONSOLIDATION_AXIS = "srt:ConsolidationItemsAxis"
_OPERATING_SEGMENTS = (_CONSOLIDATION_AXIS, "us-gaap:OperatingSegmentsMember")
_ANNUAL_DAYS = range(340, 381)
_CAMEL_CASE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_MINOR_WORDS = frozenset({"And", "Of", "The", "For", "In", "To", "On"})
_GEOGRAPHIC_REGION_NAMES = frozenset(
    {
        "americas",
        "the americas",
        "north america",
        "latin america",
        "south america",
        "central america",
        "united states",
        "u s",
        "us",
        "usa",
        "u s a",
        "united kingdom",
        "uk",
        "u k",
        "international",
        "europe",
        "emea",
        "europe middle east and africa",
        "europe middle east africa",
        "greater china",
        "china",
        "mainland china",
        "prc",
        "japan",
        "asia",
        "asia pacific",
        "apac",
        "rest of asia pacific",
        "other asia pacific",
        "all other asia",
        "rest of world",
        "rest of the world",
        "other countries",
        "other regions",
        "other international",
        "other foreign",
        "foreign",
        "domestic",
    }
)


def _is_geographic_member(member: str) -> bool:
    clean = member.strip()
    if clean.lower().startswith("country:"):
        return True
    normalized = re.sub(r"[^a-z0-9]+", " ", segment_name_for(clean).lower()).strip()
    return normalized in _GEOGRAPHIC_REGION_NAMES


def _prune_coarse_product_rollup(
    members: dict[str, set[Decimal | None]],
) -> dict[str, set[Decimal | None]]:
    if "us-gaap:ProductMember" not in members:
        return members
    specific = [m for m in members if m not in ("us-gaap:ProductMember", "us-gaap:ServiceMember")]
    if not specific:
        return members
    filtered = dict(members)
    filtered.pop("us-gaap:ProductMember", None)
    return filtered


_SHAPES = (("segment-axis", ()), ("operating-segments", (_OPERATING_SEGMENTS,)))
_RECONCILING_LABELS = {
    "CorporateNonSegmentMember": "Corporate and other",
    "CorporateAndReconcilingItemsMember": "Corporate and reconciling items",
    "CorporateReconcilingItemsAndEliminationsMember": "Corporate items",
    "MaterialReconcilingItemsMember": "Reconciling items",
    "SegmentReconcilingItemsMember": "Reconciling items",
}


@dataclass(frozen=True)
class TaggedSegments:
    concept: str
    shape: str
    period_end: date
    parts: tuple[tuple[str, Decimal], ...]
    scale: int
    refusal: str | None = None
    reconciling: tuple[tuple[str, Decimal], ...] = ()

    @property
    def label(self) -> str:
        concept = self.concept.split(":", 1)[-1]
        return concept if self.shape == "all" else f"{concept} ({self.shape.replace('-', ' ')} only)"


def _product_axis_candidate(
    concept: str,
    current: Sequence[TaggedFact],
    period_end: date,
    base_scale: int,
    reconciling: tuple[tuple[str, Decimal], ...],
) -> TaggedSegments | None:
    prod_members: dict[str, set[Decimal | None]] = {}
    prod_scale = base_scale
    for fact in current:
        dimensions = dict(fact.context.dimensions)
        member = dimensions.pop(_PRODUCT_AXIS, "")
        if not member or dimensions:
            continue
        prod_members.setdefault(member, set()).add(fact.value)
        prod_scale = max(prod_scale, fact.scale)
    prod_members = _prune_coarse_product_rollup(prod_members)
    if not prod_members:
        return None
    prod_set = _tagged_set(
        concept,
        "product-axis",
        period_end,
        prod_members,
        prod_scale,
        reconciling,
        check_geographic=False,
    )
    return prod_set if prod_set.refusal is None and prod_set.parts else None


def _reconciling_items(current: Sequence[TaggedFact]) -> tuple[tuple[str, Decimal], ...]:
    items: dict[str, set[Decimal]] = {}
    for fact in current:
        if len(fact.context.dimensions) != 1 or fact.value is None or fact.value <= 0:
            continue
        ((axis, member),) = fact.context.dimensions
        if axis == _CONSOLIDATION_AXIS and member.split(":", 1)[-1] in _RECONCILING_LABELS:
            items.setdefault(member, set()).add(fact.value)
    return tuple(sorted((member, min(values)) for member, values in items.items() if len(values) == 1))


def _segment_axis_candidates(
    concept: str,
    current: Sequence[TaggedFact],
    period_end: date,
    reconciling: tuple[tuple[str, Decimal], ...],
) -> tuple[list[TaggedSegments], int]:
    by_shape: dict[str, dict[str, set[Decimal | None]]] = {}
    scale = 0
    for shape, extra in _SHAPES:
        for fact in current:
            dimensions = dict(fact.context.dimensions)
            member = dimensions.pop(_SEGMENT_AXIS, "")
            if not member or tuple(dimensions.items()) != extra:
                continue
            by_shape.setdefault(shape, {}).setdefault(member, set()).add(fact.value)
            scale = max(scale, fact.scale)
    union: dict[str, set[Decimal | None]] = {}
    for members in by_shape.values():
        for member, values in members.items():
            union.setdefault(member, set()).update(values)
    candidates = [("all", union)] if union else []
    candidates += [(shape, members) for shape, members in by_shape.items() if members != union]
    concept_sets = [
        _tagged_set(concept, shape, period_end, members, scale, reconciling) for shape, members in candidates
    ]
    return concept_sets, scale


def tagged_segment_revenues(filing: bytes | InlineXbrl) -> tuple[TaggedSegments, ...]:
    """Return candidate annual segment revenue sets ordered by concept and context shape."""
    document = filing if isinstance(filing, InlineXbrl) else InlineXbrl(filing)
    facts = [
        fact
        for fact in document.facts(_REVENUE_CONCEPTS)
        if fact.context.days is not None and fact.context.days in _ANNUAL_DAYS
    ]
    if not facts:
        return ()
    latest = max(fact.context.end for fact in facts)
    found: list[TaggedSegments] = []
    for concept in _REVENUE_CONCEPTS:
        current = [fact for fact in facts if fact.concept == concept and fact.context.end == latest]
        reconciling = _reconciling_items(current)
        concept_sets, scale = _segment_axis_candidates(concept, current, latest, reconciling)
        found.extend(concept_sets)
        if concept_sets and all(seg.refusal == "geographic_axis_members" for seg in concept_sets):
            if (prod_set := _product_axis_candidate(concept, current, latest, scale, reconciling)) is not None:
                found.append(prod_set)
    return tuple(found)


def _tagged_set(
    concept: str,
    shape: str,
    period_end: date,
    members: dict[str, set[Decimal | None]],
    scale: int,
    reconciling: tuple[tuple[str, Decimal], ...],
    *,
    check_geographic: bool = True,
) -> TaggedSegments:
    if any(None in tagged for tagged in members.values()):
        return TaggedSegments(concept, shape, period_end, (), scale, "unreadable_value")
    if any(len(tagged) > 1 for tagged in members.values()):
        return TaggedSegments(concept, shape, period_end, (), scale, "member_tagged_twice")
    parts = tuple(sorted((member, value) for member, (value,) in members.items() if value is not None))
    if any(value < 0 for _, value in parts):
        return TaggedSegments(concept, shape, period_end, (), scale, "negative_part")
    if check_geographic and parts and all(_is_geographic_member(member) for member, _ in parts):
        return TaggedSegments(concept, shape, period_end, (), scale, "geographic_axis_members")
    return TaggedSegments(concept, shape, period_end, parts, scale, None, reconciling)


@dataclass(frozen=True)
class TaggedPartition:
    segments: TaggedSegments
    parts: tuple[tuple[str, Decimal], ...]
    reconciling: str | None
    verdict: Partition


def accepted_tagged_partition(
    tagged: Sequence[TaggedSegments], *, total: Decimal, period_end: date | None
) -> TaggedPartition | list[str]:
    """Select the first balancing tagged segment set against consolidated `total`."""
    refusals: list[str] = []
    eligible: list[TaggedSegments] = []
    for segments in tagged:
        if segments.refusal is not None:
            refusals.append(f"{segments.label}: {segments.refusal}")
        elif period_end is not None and segments.period_end != period_end:
            refusals.append(f"{segments.label}: tagged for {segments.period_end}, revenue on file is for {period_end}")
        else:
            eligible.append(segments)
    result = select_first_balancing_set(
        [[Candidate(float(value), member) for member, value in segments.parts] for segments in eligible],
        total=total,
        tolerances=[SEGMENT_TOLERANCE * Decimal(10) ** segments.scale for segments in eligible],
        completions=[
            [Candidate(float(value), member) for member, value in segments.reconciling] for segments in eligible
        ],
    )
    if isinstance(result, SetPartition):
        segments = eligible[result.set_index]
        parts = segments.parts
        completion = None
        if result.completion_index is not None:
            completion = segments.reconciling[result.completion_index]
            parts = (*parts, completion)
        return TaggedPartition(segments, parts, completion[0] if completion else None, result.partition)
    for segments, refusal in zip(eligible, result, strict=False):
        refusals.append(f"{segments.label}: {refusal.value} ({len(segments.parts)} members)")
        if refusal is PartitionRefusal.SHORT and segments.reconciling:
            refusals.append(f"{segments.label}: no single reconciling item closes it")
    return refusals


def segment_name_for(member: str) -> str:
    local = member.split(":", 1)[-1]
    if local in _RECONCILING_LABELS:
        return _RECONCILING_LABELS[local]
    local = re.sub(r"(?:Segments?)?Member$", "", local) or local
    words = _CAMEL_CASE.sub(" ", local).split()
    return " ".join([words[0], *(word.lower() if word in _MINOR_WORDS else word for word in words[1:])])


_SINGLE_SEGMENT = re.compile(
    r"\b(?:one|a\s+single|single)\s+(?:operating|reportable)"
    r"(?:\s+and\s+(?:operating|reportable))?\s+segment\b",
    re.IGNORECASE,
)


def single_segment_statement(text: str) -> str | None:
    match = _SINGLE_SEGMENT.search(text)
    if match is None:
        return None
    start = max(0, match.start() - _STATEMENT_LEAD)
    return " ".join(text[start : match.end() + _SINGLE_SEGMENT_SPAN].split())


_SEGMENT_NOTE = "us-gaap:SegmentReportingDisclosureTextBlock"
_SEGMENT_NOTE_LIMIT = 6000
_BUSINESS_CONCEPTS = (
    "us-gaap:NatureOfOperations",
    "us-gaap:BusinessDescriptionAndBasisOfPresentationTextBlock",
    "us-gaap:OrganizationConsolidationAndPresentationOfFinancialStatementsDisclosureTextBlock",
    "us-gaap:OrganizationConsolidationAndPresentationOfFinancialStatementsDisclosureAndSignificantAccountingPoliciesTextBlock",
    "us-gaap:OrganizationConsolidationBasisOfPresentationBusinessDescriptionAndAccountingPoliciesTextBlock",
)
_BUSINESS_SPAN = 320
_BUSINESS_MARKER = " Business: "
_DESCRIPTION_LIMIT = 720


def single_segment_description(document: InlineXbrl, declared: DeclaredSegmentCount, body: bytes) -> str | None:
    """Assemble the classifier description for a declared single-segment issuer."""
    note = document.text_block(_SEGMENT_NOTE, limit=_SEGMENT_NOTE_LIMIT)
    if note:
        declaration: str | None = single_segment_statement(note) or note[: _STATEMENT_LEAD + _SINGLE_SEGMENT_SPAN]
    else:
        declaration = declared.statement or single_segment_statement(filing_plain_text(body))
    business = next(
        (found for concept in _BUSINESS_CONCEPTS if (found := document.text_block(concept, limit=_BUSINESS_SPAN))),
        None,
    )
    if business is None:
        return declaration
    return f"{declaration or ''}{_BUSINESS_MARKER}{business}".strip()


SEGMENT_TOLERANCE = Decimal("5")

_CONSOLIDATED_REVENUE_SQL = """
select p.normalized_payload->>'revenue',
       p.normalized_payload->>'revenue_period_end'
from staging.capture_normalized_observations o
join staging.capture_observation_payloads p on p.observation_id = o.observation_id
join raw.capture_source_vintages v on v.source_vintage_id = o.source_vintage_id
where o.semantic_type = 'financial-fact'
  and v.source_record_id = %s
  and o.knowable_at <= %s
  and p.normalized_payload->>'revenue' is not null
  and p.normalized_payload->>'revenue_period_end' is not null
order by o.knowable_at desc
limit 1
"""


@dataclass(frozen=True)
class ConsolidatedRevenue:
    value: Decimal
    period_end: date


def consolidated_revenue(connection: Any, cik: int, *, cutoff: datetime) -> ConsolidatedRevenue | None:
    row = connection.execute(_CONSOLIDATED_REVENUE_SQL, (f"companyfacts:CIK{cik:010d}", cutoff)).fetchone()
    if row is None or row[0] is None or row[1] is None:
        return None
    return ConsolidatedRevenue(value=Decimal(str(row[0])), period_end=date.fromisoformat(str(row[1])))


SEGMENT_SOURCE = "10k-segment-extraction"


def partition_id_for(cik: int, period_end: date, parts: Sequence[tuple[str, Decimal]], *, extractor: str) -> str:
    canonical = json.dumps(
        {
            "cik": cik,
            "period_end": period_end.isoformat(),
            "parts": sorted((name, str(value)) for name, value in parts),
            "extractor": extractor,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "segment-partition:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def partition_already_recorded(connection: Any, partition_id: str) -> bool:
    row = connection.execute(
        "select 1 from staging.issuer_segment_revenue_facts where partition_id = %s limit 1",
        (partition_id,),
    ).fetchone()
    return row is not None


def record_segment_partition(
    connection: Any,
    *,
    cik: int,
    partition_id: str,
    period_end: date,
    parts: Sequence[tuple[str, Decimal]],
    partition_total: Decimal,
    partition_residual: Decimal,
    knowable_at: datetime,
    evidence_ref: str,
    extractor: str,
    confidence: Decimal,
) -> int:
    for segment_name, revenue in parts:
        connection.execute(
            """
            insert into staging.issuer_segment_revenue_facts
                (cik, segment_name, segment_revenue, partition_id, partition_total,
                 partition_residual, knowable_at, period_end, source, evidence_ref,
                 extractor, confidence)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                cik,
                segment_name,
                revenue,
                partition_id,
                partition_total,
                partition_residual,
                knowable_at,
                period_end,
                SEGMENT_SOURCE,
                evidence_ref,
                extractor,
                confidence,
            ),
        )
    return len(parts)


SINGLE_SEGMENT_NAME = "Single reportable segment"


def _land_partition(
    connection: Any,
    *,
    record_cik: int,
    document: Any,
    oracle: ConsolidatedRevenue,
    parts: Sequence[tuple[str, Decimal]],
    residual: Decimal,
    extractor: str,
    evidence_ref: str,
    standard: MetricStandard,
    write: bool,
    summary: str,
    note: str = "",
) -> ExtractionOutcome:
    partition_id = partition_id_for(record_cik, oracle.period_end, parts, extractor=extractor)
    outcome = partial(
        ExtractionOutcome,
        record_cik,
        extractor=extractor,
        accession=document.accession,
        form=document.form,
        filing_date=document.filing_date,
    )
    if not write:
        return outcome("resolved", detail=f"would land {summary}{note}")
    if partition_already_recorded(connection, partition_id):
        return outcome("already_recorded", detail=f"{partition_id} already holds {summary}")
    record_segment_partition(
        connection,
        cik=record_cik,
        partition_id=partition_id,
        period_end=oracle.period_end,
        parts=parts,
        partition_total=oracle.value,
        partition_residual=residual,
        knowable_at=knowable_instant(AsOfAxis.ACCESSION, document.filing_date),
        evidence_ref=evidence_ref,
        extractor=extractor,
        confidence=confidence_for(standard.confidence_policy_id, extractor),
    )
    return outcome("resolved", detail=f"{partition_id} landed {summary}{note}")


def _tagged_segments_outcome(
    connection: Any,
    *,
    cik: int,
    record_cik: int,
    document: Any,
    oracle: ConsolidatedRevenue | None,
    tagged: Sequence[TaggedSegments],
    standard: MetricStandard,
    write: bool,
) -> ExtractionOutcome:
    refused = partial(
        ExtractionOutcome,
        cik,
        "no_candidate",
        accession=document.accession,
        form=document.form,
        filing_date=document.filing_date,
    )
    if oracle is None:
        labels = "; ".join(f"{segments.label}: {PartitionRefusal.NO_TOTAL.value}" for segments in tagged)
        return refused(detail=f"tagged segment revenue accounts for no consolidated revenue ({labels})")
    accepted = accepted_tagged_partition(tagged, total=oracle.value, period_end=oracle.period_end)
    if isinstance(accepted, list):
        return refused(detail=f"tagged segment revenue accounts for no consolidated revenue ({'; '.join(accepted)})")
    parts = [(segment_name_for(member), value) for member, value in accepted.parts]
    named = ", ".join(f"{name}={value}" for name, value in parts)
    verdict = accepted.verdict
    return _land_partition(
        connection,
        record_cik=record_cik,
        document=document,
        oracle=oracle,
        parts=parts,
        residual=Decimal(str(verdict.residual)),
        extractor=verdict.extractor,
        evidence_ref=" ".join(
            [
                f"accession={document.accession}",
                f"form={document.form}",
                "recall=xbrl",
                f"concept={accepted.segments.concept}",
                f"shape={accepted.segments.shape}",
                f"members={','.join(member for member, _ in accepted.segments.parts)}",
            ]
            + ([f"reconciling={accepted.reconciling}"] if accepted.reconciling else [])
        ),
        standard=standard,
        write=write,
        summary=(
            f"{len(parts)} parts accounting for {verdict.total} (residual {verdict.residual}) "
            f"for {oracle.period_end}: {named}"
        ),
    )


def extract_segment_revenue(
    cik: int,
    *,
    connection: Any,
    http: Any,
    gateway: Any,
    standard: MetricStandard,
    cutoff: datetime,
    write: bool,
    store: Any = None,
    record_cik: int | None = None,
    issuer_label: str | None = None,
    **_unused: Any,
) -> ExtractionOutcome:
    """Fetch the latest annual filing, verify inline-XBRL segment counts/revenues against consolidated revenue, and land."""
    del issuer_label, store
    record_cik = cik if record_cik is None else record_cik
    document = fetch_annual_filing(cik, http=http, gateway=gateway, cutoff=cutoff)
    if isinstance(document, ExtractionOutcome):
        return document
    if document is None:
        return ExtractionOutcome(cik, "no_annual_filing", detail="no 10-K/20-F at or before the cutoff")

    oracle = consolidated_revenue(connection, record_cik, cutoff=cutoff)
    xbrl = InlineXbrl(document.body)
    declared = declared_segment_count(xbrl)
    if declared is not None and declared.value == 1 and oracle is not None:
        if declared.period_end != oracle.period_end:
            return ExtractionOutcome(
                cik,
                "no_candidate",
                accession=document.accession,
                form=document.form,
                filing_date=document.filing_date,
                detail=(
                    f"declares a single segment for {declared.period_end} ({declared.concept}), but "
                    f"the consolidated revenue on file is for {oracle.period_end}"
                ),
            )
        statement = single_segment_description(xbrl, declared, document.body)
        return _land_partition(
            connection,
            record_cik=record_cik,
            document=document,
            oracle=oracle,
            parts=[(SINGLE_SEGMENT_NAME, oracle.value)],
            residual=Decimal(0),
            extractor=RULE_SINGLE_SEGMENT,
            evidence_ref=" ".join(
                [f"accession={document.accession}", f"form={document.form}", f"segment_count={declared.evidence}"]
                + ([f"single_segment_statement={statement[:_DESCRIPTION_LIMIT]}"] if statement else [])
            ),
            standard=standard,
            write=write,
            summary=(
                f"1 segment (the whole issuer) accounting for {oracle.value} for {oracle.period_end}, "
                f"declared as {declared.evidence}"
            ),
            note=f": {(statement or '')[:160]}",
        )

    tagged = tagged_segment_revenues(xbrl)
    if tagged:
        return _tagged_segments_outcome(
            connection,
            cik=cik,
            record_cik=record_cik,
            document=document,
            oracle=oracle,
            tagged=tagged,
            standard=standard,
            write=write,
        )

    if declared is not None and declared.value == 1:
        detail = (
            f"declares a single segment ({declared.evidence}), but this environment holds no "
            "consolidated revenue to file the partition against"
        )
    else:
        detail = "the filing tags no segment revenue on the business-segment axis for its latest year"
    return ExtractionOutcome(
        cik,
        "no_candidate",
        accession=document.accession,
        form=document.form,
        filing_date=document.filing_date,
        detail=detail,
    )
