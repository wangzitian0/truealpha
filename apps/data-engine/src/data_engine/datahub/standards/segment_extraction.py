"""Segment revenue from a filing's own tags (#772, q6).

The SEC-filing adapter for `segment_revenue`, beside `filing_extraction` rather than inside it:
it reuses that module's fetch contract and owns only what is different, which is how a filing
states its segments and the shape of the answer.

**What decides is what the filer tags, never what a pattern reads.** Two structured statements
in the filing's inline XBRL carry the answer: the count of segments it declares, and the revenue
it tags on the business-segment axis. A printed-table reader — heading, row and units regexes —
answered this question until #833 and accepted 3 partitions across the 106 filings the lane
walks, one of them a geography table taken for segments; the tags balance 54.

**The identity owns correctness.** `factors.shared.extraction.select_exhaustive_partition`
accepts a set only when its parts account for the issuer's consolidated revenue — a number this
module never computes and cannot influence. A missed segment silently raises every remaining
segment's share, so q6's "purest name under a theme" would invert while every number on the page
still looked like a number; a set that does not balance is refused, with the reason on the
outcome.

What this module does NOT do: decide which parts are right (the partition rule decides), or
plan (the standard's backfill does). It reads the filing's tags and lands what the identity
accepts, with the evidence each part was read from.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from functools import partial
from typing import Any

from factors.shared.extraction import (
    RULE_SINGLE_SEGMENT,
    Candidate,
    PartitionRefusal,
    select_exhaustive_partition,
)
from truealpha_contracts.standards import MetricStandard, confidence_for

from data_engine.datahub.standards.filing_extraction import (
    ExtractionOutcome,
    fetch_annual_filing,
    filing_plain_text,
)
from data_engine.datahub.standards.inline_xbrl import InlineXbrl, TaggedFact

#: How many segments the issuer says it has, in the form it files for machines: the inline
#: XBRL tag on the count in its own segment note. This — never a sentence — is what decides
#: that an issuer has ONE segment.
#:
#: A single-segment issuer is a DETERMINATE answer, and for init.md question 6 the most
#: interesting one: a pure-play is the purest name under its theme. But its one-part partition
#: balances by construction, so nothing downstream can catch a wrong one, and the evidence it
#: rests on has to be the strongest the filing offers. Prose decided until #822, and measured
#: over the latest annual filing of every issuer the standards lane walks (106 filings,
#: 2026-09-15), a pattern for "one/single operating/reportable segment" fired on 48 — at least
#: five of them issuers with SEVERAL segments. Berkshire Hathaway was landed as one segment on
#: "expenses considered significant for one operating segment may not be significant in
#: others"; AMD on "we combined the Client and Gaming segments into one reportable segment";
#: AEP and Exelon on sentences about a subsidiary registrant. 97 of the 106 filings tag the
#: count, and each of those five tags two or more, or one only under a subsidiary's dimension.
_SEGMENT_COUNT_CONCEPTS = ("us-gaap:NumberOfReportableSegments", "us-gaap:NumberOfOperatingSegments")
#: Enough of the sentence around the statement to be worth reading back. The filing usually
#: says what the one segment DOES right there ("one reportable segment, Payment Services"),
#: which is the only description a classifier will get.
_SINGLE_SEGMENT_SPAN = 240
_STATEMENT_LEAD = 90


@dataclass(frozen=True)
class DeclaredSegmentCount:
    """The filer's own count of its segments (one, two, seven …), at the latest period it tags a count for."""

    concept: str
    #: None when the tags at that period disagree or cannot be read — which refuses the
    #: single-segment path exactly as firmly as a count of several does.
    value: int | None
    period_end: date
    #: The printed sentence the count is tagged in, when it is printed at all.
    statement: str | None

    @property
    def evidence(self) -> str:
        return f"{self.concept}={self.value}@{self.period_end.isoformat()}"


def declared_segment_count(filing: bytes | InlineXbrl) -> DeclaredSegmentCount | None:
    """The segment count the filing's inline XBRL states, or None when it tags none.

    Three rules, each taken from a filing that breaks the simpler one:

    - Only a fact about the whole filer counts. A dimensional context is about a part of it:
      a subsidiary registrant in a combined filing (AEP, Exelon), or one segment (CSX's
      railroad).
    - The latest period decides. Western Digital tags `two` for the day before its Flash
      separation and `one` for the fiscal year.
    - The reportable count outranks the operating count. Ross and Booking aggregate several
      operating segments into ONE reportable segment, and a reportable segment is what the
      standard measures; the operating count answers only when no reportable count is tagged.
    """
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


#: Segment revenue as the filer TAGS it (#830): a revenue fact whose context names one member of
#: the business-segment axis. Since ASU 2023-07 a 10-K tags its segment note, and this is the
#: part of the filing that says, in a form nothing has to interpret, which segment earned what.
#:
#: Measured over the latest annual filing of every issuer the lane walks (106 filings,
#: 2026-09-15): the tagged segments balance the filing's own consolidated revenue in 54, against
#: 6 for the heading/row/units table reader this replaced (#833) — and one of that reader's six
#: was Comcast's GEOGRAPHY table ("United States, United Kingdom, Other") taken for its segments.
#:
#: Tried in this order; the first concept whose members balance the oracle is the answer.
_REVENUE_CONCEPTS = (
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    "us-gaap:Revenues",
    "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
    "us-gaap:SalesRevenueNet",
    # A bank's top line (JPM): net revenue is what its segments report and what the oracle holds.
    "us-gaap:RevenuesNetOfInterestExpense",
)
_SEGMENT_AXIS = "us-gaap:StatementBusinessSegmentsAxis"
#: The one second dimension a segment fact may carry and still be the segment's revenue: the
#: filer marking the value as the operating segment's own.
_OPERATING_SEGMENTS = ("srt:ConsolidationItemsAxis", "us-gaap:OperatingSegmentsMember")
#: A fiscal year, including 52/53-week years (AAPL's runs 364 days, some run 371).
_ANNUAL_DAYS = range(340, 381)
_CAMEL_CASE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
#: CamelCase capitalizes every word; a printed segment name does not ("Rest of Asia Pacific").
_MINOR_WORDS = frozenset({"And", "Of", "The", "For", "In", "To", "On"})


@dataclass(frozen=True)
class TaggedSegments:
    """One revenue concept's segment members, as tagged for the filing's latest annual period."""

    concept: str
    period_end: date
    #: (axis member, value), member-sorted. Empty when the set is refused before any sum.
    parts: tuple[tuple[str, Decimal], ...]
    scale: int
    #: Why the set cannot be offered to the identity at all, or None.
    refusal: str | None = None


def tagged_segment_revenues(filing: bytes | InlineXbrl) -> tuple[TaggedSegments, ...]:
    """Every revenue concept the filing tags on the business-segment axis, for its latest year.

    A set is refused rather than summed when a member carries two different values (two
    claims about one segment), a value the reader cannot parse, or a negative value: an
    elimination is a reconciling item, not a segment anyone is the purest name under.
    """
    document = filing if isinstance(filing, InlineXbrl) else InlineXbrl(filing)
    facts = [
        fact
        for fact in document.facts(_REVENUE_CONCEPTS)
        if fact.context.days is not None and fact.context.days in _ANNUAL_DAYS
    ]
    if not facts:
        return ()
    latest = max(fact.context.end for fact in facts)
    found = []
    for concept in _REVENUE_CONCEPTS:
        members: dict[str, set[Decimal | None]] = {}
        scale = 0
        for fact in facts:
            if fact.concept != concept or fact.context.end != latest:
                continue
            dimensions = dict(fact.context.dimensions)
            member = dimensions.pop(_SEGMENT_AXIS, None)
            if member is None or (dimensions and tuple(dimensions.items()) != (_OPERATING_SEGMENTS,)):
                continue
            members.setdefault(member, set()).add(fact.value)
            scale = max(scale, fact.scale)
        if not members:
            continue
        if any(None in tagged for tagged in members.values()):
            found.append(TaggedSegments(concept, latest, (), scale, "unreadable_value"))
        elif any(len(tagged) > 1 for tagged in members.values()):
            found.append(TaggedSegments(concept, latest, (), scale, "member_tagged_twice"))
        else:
            parts = tuple(sorted((member, value) for member, (value,) in members.items() if value is not None))
            if any(value < 0 for _, value in parts):
                found.append(TaggedSegments(concept, latest, (), scale, "negative_part"))
            else:
                found.append(TaggedSegments(concept, latest, parts, scale))
    return tuple(found)


def segment_name_for(member: str) -> str:
    """The segment's name from its axis member: `msft:IntelligentCloudMember` -> "Intelligent Cloud".

    The filer's printed label lives in a separate label linkbase; the member's own words are the
    same words, CamelCased, and they are enough for a reader and for the classifier.
    """
    local = member.split(":", 1)[-1]
    local = re.sub(r"(?:Segments?)?Member$", "", local) or local
    words = _CAMEL_CASE.sub(" ", local).split()
    return " ".join([words[0], *(word.lower() if word in _MINOR_WORDS else word for word in words[1:])])


#: The prose that USED to decide (see `_SEGMENT_COUNT_CONCEPTS` for why it no longer does).
#: It survives for one job: when a filer tags its count only in the hidden header (DDOG, LLY,
#: TMUS), there is no tagged sentence to read back, and this finds the sentence a classifier is
#: shown instead. Choosing a poor description costs a declined classification, never a
#: partition.
_SINGLE_SEGMENT = re.compile(
    r"\b(?:one|a\s+single|single)\s+(?:operating|reportable)"
    r"(?:\s+and\s+(?:operating|reportable))?\s+segment\b",
    re.IGNORECASE,
)


def single_segment_statement(text: str) -> str | None:
    """A sentence in the filing text that talks about one segment, or None. Description only."""
    match = _SINGLE_SEGMENT.search(text)
    if match is None:
        return None
    start = max(0, match.start() - _STATEMENT_LEAD)
    return " ".join(text[start : match.end() + _SINGLE_SEGMENT_SPAN].split())


#: In the units the filer tagged, not in currency: parts each rounded to the tag's own last
#: digit can miss their total by a few of them, and more than that is a missing or double-counted
#: part rather than rounding. The caller multiplies by the tags' declared scale, so five means
#: "five of whatever the filer counts in".
#:
#: Declared here rather than taken from the standard because it is a property of how filings
#: round, and it moves with evidence rather than with a metric definition.
SEGMENT_TOLERANCE = Decimal("5")


#: The issuer's consolidated revenue as the capture plane holds it: the SEC company-facts
#: observation the wide row is built from, addressed by the vintage's own source record id.
#:
#: Not `staging.financial_facts`, which the first version of this adapter read. init.md §3
#: retires that table — "it holds 0 rows in Production", confirmed against prod on
#: 2026-09-10 — so every extraction would have refused `no_total` forever while the query
#: itself looked correct. The plane below holds 3,354 revenue observations, and AVGO's reads
#: 63,887,000,000 for the filing that prints 63,887.
#:
#: The id is zero-padded to ten digits because that is how the capture layer writes it
#: (`companyfacts:CIK0001730168`); an unpadded id matches nothing (review on #805).
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
    """The oracle, with the period it describes.

    The period is not decoration and it is not read from the filing's own tags. The
    identity's whole premise is that these parts and this total describe the SAME period, and
    the only way to hold that without asserting it is to take the period from the number the
    parts are checked against. A total whose period the source never stated cannot certify any
    period's partition, so the query above requires it and this is never None.
    """

    value: Decimal
    period_end: date


def consolidated_revenue(connection: Any, cik: int, *, cutoff: datetime) -> ConsolidatedRevenue | None:
    """The issuer's own consolidated revenue at this cutoff — the partition's oracle.

    Read from the SAME plane the wide row is built from, so the number the parts are checked
    against is the number a reader sees beside them. Point-in-time by construction: only
    observations knowable at or before the cutoff are eligible, and the newest of those wins.

    ABSOLUTE, in the issuer's reporting currency. The tagged parts carry their own `scale`
    and are read absolute too, so the identity compares two numbers on the same scale rather
    than one that is right and one that is off by 10^6.

    Returns None when the issuer has no revenue observation — or has one whose period the
    source never stated — which the caller turns into a `no_total` refusal rather than a
    partition it cannot check or cannot file under a period.
    """
    row = connection.execute(_CONSOLIDATED_REVENUE_SQL, (f"companyfacts:CIK{cik:010d}", cutoff)).fetchone()
    if row is None or row[0] is None or row[1] is None:
        return None
    return ConsolidatedRevenue(value=Decimal(str(row[0])), period_end=date.fromisoformat(str(row[1])))


#: What the plane records as the origin of these rows. Matches the standard's
#: `evidence_bearing_sources` and its plane's `source_priority`, which is what makes a later
#: source able to supersede this one without a code change.
SEGMENT_SOURCE = "10k-segment-extraction"


def partition_id_for(cik: int, period_end: date, parts: Sequence[tuple[str, Decimal]], *, extractor: str) -> str:
    """The identity of one accepted set, addressed by its content.

    Same filing extracted twice lands the same id, so a re-run collapses onto the rows it
    already wrote instead of duplicating a segment set — and a RESTATEMENT, which changes a
    part, is a different set with a different id rather than an update to history.

    `(cik, period_end, parts, extractor)` and nothing else: not the accession, because the same
    segments restated in a later filing are the same claim about the same period; not the
    residual, because a set is what its parts are.

    The extractor IS part of it, because the rule that accepted a set is part of what the set
    claims. Without it a withdrawn rule's rows would block their own replacement: the plane
    stops admitting them, the cell reopens, the new rule accepts the same parts — and lands
    nothing, because the id already exists (#822).
    """
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
    """Land one accepted partition: one row per segment, all carrying the set's identity.

    Deliberately not an upsert, like every other PIT plane here: a corrected breakdown is a
    NEW partition with a later `knowable_at`, so history stays readable and a replay of an
    older cutoff is unaffected.

    The whole set is written or none of it is — the caller runs inside the backfill's
    transaction, and a half-written partition is exactly the "missed segment" this design
    refuses to produce from a filing. A partition landed with one row missing would pass
    every check the plane makes and raise every remaining segment's share.
    """
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


#: What a one-part partition is named. Not the issuer's ticker and not a guess at its
#: business: the classifier is handed `evidence_ref`'s statement as the description, and this
#: is the row's label for a reader scanning the plane. REPORTABLE, not operating: Booking tags
#: five operating segments aggregated into one reportable segment, and the reportable segment
#: is what the standard measures.
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
    """Land — or, in a probe, report — one accepted set. Every path that accepts a partition
    (a declared single segment, tagged segment revenue) ends here, so what a landed set
    carries cannot depend on how it was found."""
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
        # The same filing re-extracted addresses the same set. Saying so is the point: a second
        # identical run must be visible as a no-op rather than as a fresh landing, or an operator
        # cannot tell a backfill that worked from one that ran twice.
        return outcome("already_recorded", detail=f"{partition_id} already holds {summary}")
    record_segment_partition(
        connection,
        cik=record_cik,
        partition_id=partition_id,
        period_end=oracle.period_end,
        parts=parts,
        partition_total=oracle.value,
        partition_residual=residual,
        # WHEN the breakdown became knowable: the filing's own date, never now(). An insertion
        # clock here is look-ahead for every historical cutoff.
        knowable_at=datetime.combine(document.filing_date, time.min, tzinfo=UTC),
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
    """Offer each tagged concept's segments to the identity; land the first that balances.

    When none does, the answer is a refusal naming each concept's reason. A filer that tags its
    segments has said what they are; the retired table reader balanced a table where Comcast's
    five tagged segments did not, and the table was its geography (#830, #833).
    """
    refusals: list[str] = []
    for segments in tagged:
        concept = segments.concept.split(":", 1)[-1]
        if segments.refusal is not None:
            refusals.append(f"{concept}: {segments.refusal}")
            continue
        if oracle is None:
            refusals.append(f"{concept}: {PartitionRefusal.NO_TOTAL.value}")
            continue
        if segments.period_end != oracle.period_end:
            refusals.append(f"{concept}: tagged for {segments.period_end}, revenue on file is for {oracle.period_end}")
            continue
        candidates = [Candidate(float(value), member) for member, value in segments.parts]
        # In the units the facts were tagged in — five of whatever the filer rounds to.
        tolerance = SEGMENT_TOLERANCE * Decimal(10) ** segments.scale
        verdict = select_exhaustive_partition(candidates, total=oracle.value, tolerance=tolerance)
        if isinstance(verdict, PartitionRefusal):
            refusals.append(f"{concept}: {verdict.value} ({len(segments.parts)} members)")
            continue
        parts = [(segment_name_for(member), value) for member, value in segments.parts]
        named = ", ".join(f"{name}={value}" for name, value in parts)
        return _land_partition(
            connection,
            record_cik=record_cik,
            document=document,
            oracle=oracle,
            parts=parts,
            residual=Decimal(str(verdict.residual)),
            extractor=verdict.extractor,
            # `recall=xbrl` and the members: a reader can find every part's tag in the filing.
            evidence_ref=(
                f"accession={document.accession} form={document.form} recall=xbrl "
                f"concept={segments.concept} members={','.join(member for member, _ in segments.parts)}"
            ),
            standard=standard,
            write=write,
            summary=(
                f"{len(parts)} segments accounting for {verdict.total} "
                f"(residual {verdict.residual}) for {oracle.period_end}: {named}"
            ),
        )
    return ExtractionOutcome(
        cik,
        "no_candidate",
        accession=document.accession,
        form=document.form,
        filing_date=document.filing_date,
        detail=f"tagged segment revenue accounts for no consolidated revenue ({'; '.join(refusals)})",
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
    """The standard's adapter: fetch, read what the filer tags, and land what balances.

    Same signature as `extract_headcount` because `backfill._resolve` calls whichever adapter
    the standard declares (#800) — the loop no longer knows which is which.

    Two statements answer, in order: a declared single segment (#822), then the revenue tagged
    on the business-segment axis (#830). Anything else is a refusal that says which half is
    missing. In write mode the accepted set is landed as one row per segment, all sharing a
    content-addressed `partition_id`, inside the caller's transaction — the rows are admissible
    only together, so they commit together or not at all.
    """
    del issuer_label  # accepted for signature parity; not used by this adapter
    # #496, the same split `extract_headcount` makes: `cik` is where the FILING is fetched
    # from, `record_cik` (default: the same) is the issuer the fact is RECORDED under. They
    # differ for a post-reorganization holding company whose filings still sit under the
    # predecessor CIK — and the backfill's fallback passes it, so discarding it would file
    # XOM's segments under the CIK it no longer trades as.
    record_cik = cik if record_cik is None else record_cik
    # One fetch contract for every adapter (`fetch_annual_filing`): a backfill reports the
    # failure per CELL and keeps going. This module shipped its own copy and #805's review
    # caught what it had drifted from; there is one now.
    document = fetch_annual_filing(cik, http=http, gateway=gateway, cutoff=cutoff)
    if isinstance(document, ExtractionOutcome):
        return document
    if document is None:
        return ExtractionOutcome(cik, "no_annual_filing", detail="no 10-K/20-F at or before the cutoff")

    # By `record_cik`, not `cik`: the total must be the ISSUER's, even when the filing the
    # parts were read from sits under a predecessor. Checking a holdco's segments against the
    # predecessor's revenue would balance two different entities against each other.
    oracle = consolidated_revenue(connection, record_cik, cutoff=cutoff)

    # A single-segment issuer has no segment TABLE, and that is an answer rather than a
    # miss: the one segment IS the company, so the partition is the consolidated revenue in
    # one part. It satisfies the accounting identity by construction — which is exactly why
    # it has to say so on the row: the identity gives these no independent check, and the
    # filer's own declaration is the whole of the evidence.
    #
    # Checked FIRST: an issuer that declares one segment is answered by the declaration, and a
    # geography or product breakdown it also tags must not be accepted as its reportable
    # segments.
    xbrl = InlineXbrl(document.body)
    declared = declared_segment_count(xbrl)
    if declared is not None and declared.value == 1 and oracle is not None:
        if declared.period_end != oracle.period_end:
            # The partition is filed under the ORACLE's period, so a count declared for a
            # different one is not evidence about it: a filer that reorganized between the two
            # would be landed as one segment for a year it had several.
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
        # The prose is read only here, and only when the count was tagged where no sentence is printed.
        statement = declared.statement or single_segment_statement(filing_plain_text(document.body))
        return _land_partition(
            connection,
            record_cik=record_cik,
            document=document,
            oracle=oracle,
            parts=[(SINGLE_SEGMENT_NAME, oracle.value)],
            residual=Decimal(0),
            extractor=RULE_SINGLE_SEGMENT,
            # The declaration travels ON the row, and so does the sentence. Everything else here
            # balances by construction, so these are the only things a reader can check the claim
            # against. The statement goes LAST: the theme-purity reader takes everything after
            # its marker as the description.
            evidence_ref=" ".join(
                [f"accession={document.accession}", f"form={document.form}", f"segment_count={declared.evidence}"]
                + ([f"single_segment_statement={statement[:400]}"] if statement else [])
            ),
            standard=standard,
            write=write,
            summary=(
                f"1 segment (the whole issuer) accounting for {oracle.value} for {oracle.period_end}, "
                f"declared as {declared.evidence}"
            ),
            note=f": {(statement or '')[:160]}",
        )

    # Next, the revenue the filer tags on the segment axis (#830): answered from the tags, or
    # refused with each concept's reason.
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

    # Neither statement: the filing does not say, in a form this adapter trusts, how its revenue
    # divides. Two different things, with two different owners, can leave it there — a single
    # segment declared where this environment has no revenue to file it against, and a filing
    # that tags no segment revenue at all (an IFRS filer, a custom axis) — and the refusal says
    # which.
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
