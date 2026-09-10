"""Recall of segment revenue from a filing's own text (#772, q6).

The SEC-filing adapter for `segment_revenue`, beside `filing_extraction` rather than inside
it: it reuses that module's `latest_annual_filing` and `filing_plain_text` unchanged (both
carry no headcount semantics — measured, not assumed) and owns only what is different, which
is recall and the shape of the answer.

**Recall is deliberately not clever, and that is the design.** Measured on the packaged AVGO
10-K, a plain sweep of "revenue by segment" windows returns the real table AND a per-segment
income statement 140k characters later, whose cost, R&D and operating-income rows sum to far
more than the issuer earned. That is not filtered by cleverness here.
`factors.shared.extraction.select_exhaustive_partition` refuses a set whose parts do not
account for the issuer's consolidated revenue — a number this module never computes and
cannot influence — so a greedy recall pass produces a REFUSAL, not a wrong answer.

One class IS excluded before the identity sees it, and it is not a judgement about which
table is right: a window that states no monetary scale (AVGO restates the same segments as
percentages, under "(As a percentage of net revenue)") holds numbers that cannot be compared
to an absolute oracle at all. Reading the units the table declares is the difference between
a part and a number that merely looks like one.

That division matters more than it sounds. A missed segment silently raises every remaining
segment's share, so q6's "purest name under a theme" inverts while every number on the page
still looks like a number. Recall owns coverage; the identity owns correctness.

What this module does NOT do: fetch (the caller passes a `FilingDocument`), decide (the
partition rule decides), or write (the standard's backfill writes). It turns filing text into
candidates with the evidence each was read from.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from factors.shared.extraction import (
    RULE_SINGLE_SEGMENT,
    Candidate,
    Partition,
    PartitionRefusal,
    select_exhaustive_partition,
)
from truealpha_contracts.standards import MetricStandard, confidence_for

from data_engine.datahub.standards.filing_extraction import (
    ExtractionOutcome,
    filing_plain_text,
    latest_annual_filing,
)
from data_engine.sources.gateway import CapacityExceeded

#: The sentence that introduces a segment revenue table. Every phrasing seen in the packaged
#: filings AND in the ones the deployed run refused; a heading this misses costs a
#: `no_candidates` refusal, never a wrong number.
#:
#: `sales` is here because the word is not always "revenue": Apple writes "net sales by
#: reportable segment for 2025, 2024 and 2023 (dollars in millions)" and Costco reports
#: segment "revenue" under a sentence this still does not match. Measured on the filings the
#: staging run refused, not invented — 15 of 25 issuers refused with "no segment table
#: matched", which is a statement about this pattern and not about the filings.
_TABLE_HEADING = re.compile(
    r"(?:net\s+)?(?:revenue|sales)\s+by\s+(?:reportable\s+|operating\s+)?segment"
    r"|segment\s+(?:net\s+)?(?:revenue|sales)"
    r"|(?:revenues?|net\s+sales)\s+from\s+external\s+customers\s+by\s+(?:reportable\s+)?segment",
    re.IGNORECASE,
)

#: One table row: a label followed by its first numeric column, DECIMALS INCLUDED. Without
#: the decimal, ADP is invisible: it reports millions to one place ("segment revenues 14,831.4
#: 7,128.1 21,959.5"), so every row of its segment table failed to match and six located
#: tables produced nothing. The SECOND column is deliberately not read — it is the prior fiscal year, and mixing the years is the trap that
#: makes a partition sum to neither. Which column belongs to which year is stated in the
#: header the window starts with, so the first is this year's by position within the window.
_ROW = re.compile(r"([A-Z][A-Za-z&/,\.\-' ]{3,60}?)\s+\$?\s*([\d,]{2,15}(?:\.\d+)?)(?:\s|$)")

#: A row label that is really the TAIL of a total. `_NOT_A_SEGMENT` rejects `Total Segment
#: Revenues`, and then the row regex matches again from `Segment Revenues` — so the aggregate
#: enters the set as a part named after the group it totals. Measured on ADM, whose grand
#: total is literally "Total Segment Revenues 79,820".
#:
#: Checked against the text BEFORE the label rather than the label itself, which is the only
#: place the word survives. Rejecting the whole WINDOW instead was tried and measured: it
#: takes ADM from 32 candidates to 6 and loses every real leaf (Crushing 10,353, Refined
#: Products and Other 10,855, Starches and Sweeteners 7,982 ...), because that same match is
#: the anchor the backward window uses to find the table at all. The row is the thing to
#: reject; the window is not.
_TAIL_OF_A_TOTAL = re.compile(r"\btotal\s+(?:\w+\s+){0,2}$", re.IGNORECASE)

#: A window wide enough for a two-to-six segment table plus its header. An upper bound only
#: — the window really ends at the table's total row, below.
_WINDOW = 700

#: The total row ENDS a segment table. Measured on the packaged AVGO 10-K: the amounts table
#: runs `... Infrastructure software 27,029 ... Total net revenue $ 63,887 ...` and the very
#: next characters are the caption of a SECOND table stating the same segments as
#: percentages. A fixed-width window swallows both, and their parts sum to neither the total
#: nor anything else — the identity refuses, correctly, and the extraction fails when it
#: should have succeeded. Cutting at the total is not a heuristic about which numbers look
#: right; it is where the table itself says it is finished.
_TABLE_END = re.compile(r"\btotal\b[^\n]{0,40}?[\d,]{2,15}", re.IGNORECASE)

#: The scale the table states for itself: filings print "(In millions)" or "(in thousands)"
#: in the caption between the heading and the first row. The oracle this set is checked
#: against is ABSOLUTE (the observation plane stores 63,887,000,000 for the same issuer the
#: filing prints as 63,887), so an unscaled part is short by six orders of magnitude and the
#: identity refuses every real table.
#:
#: Read rather than assumed. A default of "millions" would be right for most large issuers
#: and silently wrong for the rest — and wrong in the direction that MATTERS, because a set
#: scaled by 1,000x too little refuses (visible) while one scaled too much can only land if
#: it balances, which it cannot. A window that does not state its units is skipped, and
#: `unitless_windows` counts it so the refusal says so instead of claiming recall found
#: nothing.
#: Every shape the packaged corpus actually uses, measured rather than guessed:
#: `(In millions, except percentages)`, `(dollars in millions)`, `(in US $ millions, except
#: share and per share amounts)`, `(in thousands)`. The first version required `(` to be
#: followed immediately by `in`, and SHOP writes `(in US $ millions)` — so the filing read as
#: declaring no scale ANYWHERE, and a test of mine asserted that as a property of the
#: DOCUMENT when it was a property of this regex. Widening it takes SHOP from nothing to
#: millions x70 and changes no other filing's dominant answer.
_UNITS = re.compile(
    r"\(\s*(?:[^()]{0,40}?\b)?in\s+(?:U\.?S\.?\s*\$\s*|\$\s*)?(thousands|millions|billions)\b",
    re.IGNORECASE,
)
#: A table that says it is stated in percentages. Not cleverness about which table is right
#: — the same document-stated fact the units are, read for the same reason.
#:
#: This became load-bearing when a table could inherit the FILING's scale: AVGO restates its
#: segments as `58 / 42` under `(As a percentage of net revenue)`, and 58 + 42 inherited as
#: millions is 100,000,000. Against the issuer this is harmless — 100M is nowhere near
#: 63,887M — but an issuer whose consolidated revenue happens to be about $100 million would
#: have had its PERCENTAGE table accepted as its segment revenues, balanced to the cent. A
#: plausible-looking wrong answer is the one failure this module is built to make impossible,
#: so the percentage table is excluded by what it says about itself.
_PERCENTAGE_TABLE = re.compile(r"\(\s*(?:as\s+a\s+)?percentages?\b|\bas\s+a\s+percentage\s+of\b", re.IGNORECASE)

_MULTIPLIERS = {
    "thousands": Decimal("1000"),
    "millions": Decimal("1000000"),
    "billions": Decimal("1000000000"),
}


#: Labels that are never a segment. `total` is the identity's own oracle restated inside the
#: table; a month name is a header date fragment the row pattern picks up.
_NOT_A_SEGMENT = re.compile(
    r"^total\b"
    # Column headers, whose "value" is a year. Measured on Apple: its table is headed
    # `... 2025 Change 2024 Change 2023`, and reading `Change 2024` as a 2,024-unit segment
    # put two junk parts in the set — the five real geographies sum to the consolidated
    # revenue exactly, and did not balance until these were out.
    r"|^(?:change|fiscal|year|period|quarter)\b"
    r"|^(?:january|february|march|april|may|june|july|august|september|october|november|december)\b"
    r"|\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\b",
    re.IGNORECASE,
)


#: An issuer stating, in its own words, that it has ONE segment. Measured against the
#: packaged corpus, not invented: it catches DDOG ("a single operating and reportable
#: segment"), SHOP ("one single operating and reportable segment"), DUOL and PLUG ("a single
#: operating segment"), and fires on none of ADM, AVGO, JPM or NICE.
#:
#: This is not a recall fallback. A single-segment issuer is a DETERMINATE answer, and for
#: init.md question 6 it is the most interesting one — a pure-play is the purest name under
#: its theme. Refusing them, which this module did until now, would make the ranking
#: systematically exclude exactly the companies it exists to find.
_SINGLE_SEGMENT = re.compile(
    r"\b(?:one|a\s+single|single)\s+(?:operating|reportable)"
    r"(?:\s+and\s+(?:operating|reportable))?\s+segment\b",
    re.IGNORECASE,
)
#: Enough of the sentence around the statement to be worth reading back. The filing usually
#: says what the one segment DOES right there ("providing an observability and security
#: platform for cloud applications"), which is the only description a classifier will get.
_SINGLE_SEGMENT_SPAN = 240


def single_segment_statement(text: str) -> str | None:
    """The filing's own sentence saying it operates as one segment, or None.

    Returned verbatim because it is the evidence AND the description: a single-segment
    issuer has no segment table, so this sentence is the whole of what the filing says about
    the thing being classified.
    """
    match = _SINGLE_SEGMENT.search(text)
    if match is None:
        return None
    start = max(0, match.start() - 90)
    return " ".join(text[start : match.end() + _SINGLE_SEGMENT_SPAN].split())


@dataclass(frozen=True)
class SegmentCandidate:
    """One stated segment revenue, with what it was read from.

    `sentence` is the row verbatim — init.md §9's anchor: the landed fact points at the text
    that stated it, so a number is always re-readable against its source.
    """

    segment_name: str
    #: ABSOLUTE, in the filing's currency — the units its own caption declared, applied. This
    #: is what the identity checks and what lands, so no consumer has to know which filing
    #: used which scale.
    value: Decimal
    #: The number as the row printed it (36,858), kept beside the scaled one so the evidence
    #: span and the value a reader sees in the filing still agree.
    stated_value: Decimal
    #: The scale applied to this row. Per window where the table states one — an issuer may
    #: print its segment table in millions and a supplementary table in thousands — and the
    #: filing's own dominant declaration where it does not.
    multiplier: Decimal
    #: `"table"` when the window declared the scale, `"filing"` when it was inherited. Kept
    #: because the two are different evidence: the first is stated beside the numbers, the
    #: second is stated elsewhere and only survives because the identity would have refused
    #: the set had it been wrong.
    scale_source: str
    sentence: str
    window_start: int


def segment_candidates(text: str) -> list[SegmentCandidate]:
    """Every segment-looking row under every segment-table heading, first column only.

    Duplicates across windows are collapsed on (name, value): the same table is commonly
    introduced twice (a lead-in sentence and the table's own caption both match the
    heading), and one segment stated twice would double-count into every share.

    Order is discovery order, which the partition rule preserves in `candidate_indices` — an
    adapter mapping indices back to these candidates gets the row it expected.
    """
    seen: set[tuple[str, Decimal]] = set()
    found: list[SegmentCandidate] = []
    inherited = filing_scale(text)
    for start, window in _windows(text):
        if _PERCENTAGE_TABLE.search(window):
            # The table states it holds percentages. Nothing here is a revenue.
            continue
        units = _UNITS.search(window)
        if units is not None:
            multiplier, scale_source = _MULTIPLIERS[units.group(1).lower()], "table"
        elif inherited is not None:
            multiplier, scale_source = inherited, "filing"
        else:
            # Not a judgement about whether this table is the right one — that is the
            # identity's job. A number whose scale the document states NOWHERE cannot be
            # compared to an absolute oracle at all.
            continue
        for row in _ROW.finditer(window):
            name = row.group(1).strip()
            if _NOT_A_SEGMENT.search(name) or len(name.split()) > 6:
                continue
            # Against the FULL text, not the window: when the phrase matched inside the total
            # itself the window BEGINS at the aggregate's name, so the word "Total" is behind
            # the window's own start and a window-local look-back sees nothing.
            at = start + row.start(1)
            if _TAIL_OF_A_TOTAL.search(text[max(0, at - 12) : at]):
                continue
            stated = Decimal(row.group(2).replace(",", ""))
            key = (name, stated)
            if key in seen:
                continue
            seen.add(key)
            found.append(
                SegmentCandidate(
                    segment_name=name,
                    value=stated * multiplier,
                    stated_value=stated,
                    multiplier=multiplier,
                    scale_source=scale_source,
                    sentence=row.group(0).strip(),
                    window_start=start,
                )
            )
    return found


#: How far back to look for a units caption when the forward window has none. Measured, not
#: guessed: ADM's caption sits ~700 characters before the phrase this module matches on.
_LOOKBACK = 900


def _rows_in(window: str) -> int:
    """How many segment-looking rows a span holds. The window's direction is chosen by this
    rather than by where a units caption sits, because a caption is optional and the rows are
    the thing being looked for.

    Window-local on purpose, and so slightly looser than the filter in `segment_candidates`:
    this only compares two spans of the SAME document to pick a direction, and a row that will
    later be rejected as a total's tail counts the same on both sides of that comparison.
    """
    return sum(
        1
        for row in _ROW.finditer(window)
        if not _NOT_A_SEGMENT.search(row.group(1).strip())
        and len(row.group(1).split()) <= 6
        and not _TAIL_OF_A_TOTAL.search(window[max(0, row.start(1) - 12) : row.start(1)])
    )


def filing_scale(text: str) -> Decimal | None:
    """The scale this filing declares most often, or None if it declares none.

    Measured on the packaged corpus, where the dominant declaration is never close: ADM says
    millions 51 times and thousands twice, DDOG says thousands 39 times and millions once,
    JPM says millions 253 times. A filing declares its units once at the top of the financial
    statements and every table below inherits them, so a table that states no scale of its own
    is not scaleless — it is using the filing's.

    Safe to fall back to precisely because it CANNOT hide an error. A scale wrong by 1000x
    makes the parts miss the consolidated total by 1000x, and
    `select_exhaustive_partition` refuses the set. Unlike a classification, a wrong guess here
    produces a refusal rather than a plausible number — which is what earns this a fallback
    instead of a refusal (measured on ADP: its segment table states no scale within 900
    characters in either direction, and the backward window recovered nothing).
    """
    counts = Counter(match.group(1).lower() for match in _UNITS.finditer(text))
    if not counts:
        return None
    return _MULTIPLIERS[counts.most_common(1)[0][0]]


def _windows(text: str) -> list[tuple[int, str]]:
    """Each segment table, as a span that contains both its rows and its declared scale.

    A segment table is bounded by a units caption on one side and a total row on the other,
    and the phrase this module matches on can be at EITHER end — which is the thing the first
    version got wrong by assuming one shape:

    - AVGO prints `Net Revenue by Segment ... (In millions, except percentages) ... Total net
      revenue $ 63,887`. The match is the caption's heading; the window runs forward to the
      total.
    - ADM's total row is itself called `Segment Revenues 79,820 85,099`, so the match lands on
      the table's LAST line. Everything this module wants — the caption and every segment row
      — is BEHIND the match, and a forward-only window captured none of it. Six tables on that
      one filing, found and thrown away.

    So: try forward first, and when the forward span states no scale, fall back to the span
    from the nearest preceding caption up to the match. A window is still offered to the
    identity as a whole; this only changes where its edges are.
    """
    windows = []
    for heading in _TABLE_HEADING.finditer(text):
        forward = text[heading.start() : heading.start() + _WINDOW]
        # Stop at the table's own total row, so a second table under the same heading (the
        # percentage restatement) is a SEPARATE window rather than extra parts in this one.
        end = _TABLE_END.search(forward)
        if end:
            forward = forward[: end.start()]
        # The caption may be behind the match — take the NEAREST preceding one, never an
        # earlier table's; without one, the whole look-back span is the candidate.
        back_start = max(0, heading.start() - _LOOKBACK)
        behind = text[back_start : heading.start()]
        captions = list(_UNITS.finditer(behind))
        backward = behind[captions[-1].start() :] if captions else behind
        backward_start = back_start + (captions[-1].start() if captions else 0)

        if _UNITS.search(forward) and _rows_in(forward):
            windows.append((heading.start(), forward))
        elif _rows_in(backward) > _rows_in(forward):
            # The direction follows the ROWS, not the caption. ADP states no scale in either
            # direction, so a caption-driven choice sent it forward — into the half of the
            # table with nothing in it. A match that lands on a table's TOTAL row has
            # everything worth reading behind it, whether or not a caption is there too.
            windows.append((backward_start, backward))
        else:
            windows.append((heading.start(), forward))
    return windows


def unitless_windows(text: str) -> int:
    """How many segment tables were found in a filing that declares no scale ANYWHERE.

    Zero once the filing declares one, because every window then inherits it. Reported so a
    filing whose tables were all skipped refuses with that reason instead of "no segment table
    matched" — two different problems with two different fixes (a heading this module cannot
    read vs. a document that never states its units).
    """
    if filing_scale(text) is not None:
        return 0
    return sum(1 for _, window in _windows(text) if _UNITS.search(window) is None)


def as_candidates(segments: list[SegmentCandidate]) -> list[Candidate]:
    """The primitive's provenance-neutral view of the same rows.

    `Candidate.value` is `int | float` by that module's contract; the Decimal above is what
    LANDS, and `select_exhaustive_partition` re-reads through `str()` so no binary error
    reaches the acceptance.
    """
    return [Candidate(float(item.value), item.sentence) for item in segments]


def windows_of(segments: list[SegmentCandidate]) -> dict[int, list[int]]:
    """Candidate indices grouped by the table they came from, discovery order preserved.

    A filing states its segments in one table; a second table under a matching heading is a
    different statement of them (percentages, a prior-year-only breakout, a geography split).
    Offering each window to the identity separately is what lets the right table be accepted
    without the wrong one having to be recognised as wrong.
    """
    grouped: dict[int, list[int]] = {}
    for index, item in enumerate(segments):
        grouped.setdefault(item.window_start, []).append(index)
    return grouped


#: In the units the TABLE states, not in currency: a table rounding each part to its own last
#: printed digit can miss its own total by a few of them, and more than that is a missing or
#: double-counted part rather than rounding. The caller multiplies by the window's declared
#: scale, so five means "five of whatever this table counts in".
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

    The period is not decoration and it is not read from the filing's table header. The
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

    ABSOLUTE, in the issuer's reporting currency. The parts are scaled to match at recall
    (`segment_candidates`) from the units each table declares, so the identity compares two
    numbers on the same scale rather than one that is right and one that is off by 10^6.

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


def partition_id_for(cik: int, period_end: date, parts: Sequence[tuple[str, Decimal]]) -> str:
    """The identity of one accepted set, addressed by its content.

    Same filing extracted twice lands the same id, so a re-run collapses onto the rows it
    already wrote instead of duplicating a segment set — and a RESTATEMENT, which changes a
    part, is a different set with a different id rather than an update to history.

    `(cik, period_end, parts)` and nothing else: not the accession, because the same segments
    restated in a later filing are the same claim about the same period; not the residual,
    because a set is what its parts are.
    """
    canonical = json.dumps(
        {
            "cik": cik,
            "period_end": period_end.isoformat(),
            "parts": sorted((name, str(value)) for name, value in parts),
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
#: is the row's label for a reader scanning the plane.
SINGLE_SEGMENT_NAME = "Single operating segment"


def _single_segment_outcome(
    connection: Any,
    *,
    cik: int,
    record_cik: int,
    document: Any,
    oracle: ConsolidatedRevenue,
    statement: str,
    standard: MetricStandard,
    write: bool,
) -> ExtractionOutcome:
    """Land (or report) the determinate partition of an issuer that states one segment."""
    parts = [(SINGLE_SEGMENT_NAME, oracle.value)]
    partition_id = partition_id_for(record_cik, oracle.period_end, parts)
    summary = f"1 segment (the whole issuer) accounting for {oracle.value} for {oracle.period_end}"
    if not write:
        return ExtractionOutcome(
            record_cik,
            "resolved",
            extractor=RULE_SINGLE_SEGMENT,
            accession=document.accession,
            form=document.form,
            filing_date=document.filing_date,
            detail=f"would land {summary}: {statement[:160]}",
        )
    if partition_already_recorded(connection, partition_id):
        return ExtractionOutcome(
            record_cik,
            "already_recorded",
            extractor=RULE_SINGLE_SEGMENT,
            accession=document.accession,
            form=document.form,
            filing_date=document.filing_date,
            detail=f"{partition_id} already holds {summary}",
        )
    record_segment_partition(
        connection,
        cik=record_cik,
        partition_id=partition_id,
        period_end=oracle.period_end,
        parts=parts,
        partition_total=oracle.value,
        partition_residual=Decimal(0),
        knowable_at=datetime.combine(document.filing_date, time.min, tzinfo=UTC),
        # The statement travels ON the row. Everything else here balances by construction, so
        # this sentence is the only thing a reader can check the claim against.
        evidence_ref=(
            f"accession={document.accession} form={document.form} single_segment_statement={statement[:400]}"
        ),
        extractor=RULE_SINGLE_SEGMENT,
        confidence=confidence_for(standard.confidence_policy_id, RULE_SINGLE_SEGMENT),
    )
    del cik  # the filing's CIK; the fact is recorded under record_cik
    return ExtractionOutcome(
        record_cik,
        "resolved",
        extractor=RULE_SINGLE_SEGMENT,
        accession=document.accession,
        form=document.form,
        filing_date=document.filing_date,
        detail=f"{partition_id} landed {summary}: {statement[:160]}",
    )


def _no_candidate_detail(text: str) -> str:
    """Which of THREE things happened, because they have three different owners.

    A fourth state appeared the moment a table could inherit the filing's scale, and it was
    reported as the first: ADP matches six headings and parses no rows from them, and the
    message said "no segment table matched". That sent the next reader to the heading pattern
    when the fault was in the row pattern — its numbers carry a decimal ("14,831.4") and the
    row regex could not read one.

    Collapsing two problems into one refusal string is the thing this module keeps being
    caught by. Three states, three sentences.
    """
    found = len(_windows(text))
    if not found:
        return "no segment table matched in the filing text"
    unitless = unitless_windows(text)
    if unitless:
        return f"{unitless} segment table(s) state no scale, so no part could be compared"
    return f"{found} segment table(s) matched but no row parsed as a segment"


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
    """The standard's adapter: fetch, recall, and accept the ONE table that balances.

    Same signature as `extract_headcount` because `backfill._resolve` calls whichever adapter
    the standard declares (#800) — the loop no longer knows which is which.

    Every window recall found is offered to the identity separately, and the FIRST that
    balances wins. Two balancing windows would be two answers to one question; the tests on
    the packaged filing assert exactly one balances, and if a filing ever produces two the
    honest outcome is the refusal below rather than a silent pick.

    In write mode the accepted set is landed as one row per segment, all sharing a
    content-addressed `partition_id`, inside the caller's transaction — the rows are
    admissible only together, so they commit together or not at all.
    """
    del issuer_label  # accepted for signature parity; not used by this adapter
    # #496, the same split `extract_headcount` makes: `cik` is where the FILING is fetched
    # from, `record_cik` (default: the same) is the issuer the fact is RECORDED under. They
    # differ for a post-reorganization holding company whose filings still sit under the
    # predecessor CIK — and the backfill's fallback passes it, so discarding it would file
    # XOM's segments under the CIK it no longer trades as.
    record_cik = cik if record_cik is None else record_cik
    # Same contract as `extract_headcount`: a backfill reports the failure per CELL and
    # keeps going. Letting `CapacityExceeded` escape here would abandon every issuer after
    # the first throttled one, which is the opposite of what a capacity signal means
    # (review on #805).
    try:
        document = latest_annual_filing(cik, http=http, gateway=gateway, cutoff=cutoff)
    except CapacityExceeded as error:
        return ExtractionOutcome(cik, "deferred_capacity", detail=str(error))
    except Exception as error:  # noqa: BLE001 - a backfill reports the failure per cell and continues
        return ExtractionOutcome(cik, "error", detail=f"{type(error).__name__}: {error}")
    if document is None:
        return ExtractionOutcome(cik, "no_annual_filing", detail="no 10-K/20-F at or before the cutoff")

    # By `record_cik`, not `cik`: the total must be the ISSUER's, even when the filing the
    # parts were read from sits under a predecessor. Checking a holdco's segments against the
    # predecessor's revenue would balance two different entities against each other.
    oracle = consolidated_revenue(connection, record_cik, cutoff=cutoff)
    total = None if oracle is None else oracle.value
    text = filing_plain_text(document.body)

    # A single-segment issuer has no segment TABLE, and that is an answer rather than a
    # miss: the one segment IS the company, so the partition is the consolidated revenue in
    # one part. It satisfies the accounting identity by construction — which is exactly why
    # it has to say so on the row: the identity gives these no independent check, and the
    # filing's own sentence is the whole of the evidence.
    #
    # Checked BEFORE the tables, not as a fallback after them: an issuer that states one
    # segment and also prints a geography or product breakdown must not have that breakdown
    # accepted as its reportable segments.
    statement = single_segment_statement(text)
    if statement is not None and oracle is not None:
        return _single_segment_outcome(
            connection,
            cik=cik,
            record_cik=record_cik,
            document=document,
            oracle=oracle,
            statement=statement,
            standard=standard,
            write=write,
        )

    # Recall runs only now, so "before the tables" is the code's order and not just a claim
    # about it — a filing that states one segment never pays for a sweep whose result is
    # already known to be unused (review on #808).
    recalled = segment_candidates(text)
    if not recalled:
        return ExtractionOutcome(
            cik,
            "no_candidate",
            accession=document.accession,
            form=document.form,
            filing_date=document.filing_date,
            detail=_no_candidate_detail(text),
        )

    candidates = as_candidates(recalled)
    refusals: list[PartitionRefusal] = []
    for indices in windows_of(recalled).values():
        # In the units THIS table declared. A tolerance fixed in absolute currency would be
        # a thousand times too tight for a table stated in thousands and a thousand times
        # too loose for one stated in billions.
        tolerance = SEGMENT_TOLERANCE * recalled[indices[0]].multiplier
        verdict = select_exhaustive_partition(candidates, total=total, tolerance=tolerance, indices=indices)
        if isinstance(verdict, Partition):
            assert oracle is not None  # a Partition cannot be returned without a total
            parts = [(recalled[i].segment_name, recalled[i].value) for i in verdict.candidate_indices]
            named = ", ".join(
                f"{recalled[i].segment_name}={recalled[i].stated_value}" for i in verdict.candidate_indices
            )
            partition_id = partition_id_for(record_cik, oracle.period_end, parts)
            summary = (
                f"{len(parts)} segments accounting for {verdict.total} "
                f"(residual {verdict.residual}) for {oracle.period_end}: {named}"
            )
            if not write:
                return ExtractionOutcome(
                    record_cik,
                    "resolved",
                    extractor=verdict.extractor,
                    accession=document.accession,
                    form=document.form,
                    filing_date=document.filing_date,
                    detail=f"would land {summary}",
                )
            if partition_already_recorded(connection, partition_id):
                # The same filing re-extracted addresses the same set. Saying so is the
                # point: a second identical run must be visible as a no-op rather than as a
                # fresh landing, or an operator cannot tell a backfill that worked from one
                # that ran twice.
                return ExtractionOutcome(
                    record_cik,
                    "already_recorded",
                    extractor=verdict.extractor,
                    accession=document.accession,
                    form=document.form,
                    filing_date=document.filing_date,
                    detail=f"{partition_id} already holds {summary}",
                )
            record_segment_partition(
                connection,
                cik=record_cik,
                partition_id=partition_id,
                period_end=oracle.period_end,
                parts=parts,
                partition_total=oracle.value,
                partition_residual=Decimal(str(verdict.residual)),
                # WHEN the breakdown became knowable: the filing's own date, never now().
                # An insertion clock here is look-ahead for every historical cutoff.
                knowable_at=datetime.combine(document.filing_date, time.min, tzinfo=UTC),
                # `scale=` is not decoration: a scale printed beside the numbers and one
                # inherited from the filing are different evidence, and a row that does not
                # say which leaves a reader unable to tell them apart. It is the honest cost
                # of letting a table inherit — the identity proves the scale was RIGHT, and
                # this says where it came from.
                evidence_ref=(
                    f"accession={document.accession} form={document.form} "
                    f"scale={recalled[verdict.candidate_indices[0]].scale_source}"
                ),
                extractor=verdict.extractor,
                confidence=confidence_for(standard.confidence_policy_id, verdict.extractor),
            )
            return ExtractionOutcome(
                record_cik,
                "resolved",
                extractor=verdict.extractor,
                accession=document.accession,
                form=document.form,
                filing_date=document.filing_date,
                detail=f"{partition_id} landed {summary}",
            )
        refusals.append(verdict)

    # No window balanced. The refusals say WHY, and they differ: `no_total` is a missing
    # consolidated revenue (this issuer has no wide-row number to check against), while
    # short/over means recall missed or over-collected. Reporting the set rather than the
    # first keeps that distinction visible.
    reasons = ", ".join(sorted({refusal.value for refusal in refusals}))
    return ExtractionOutcome(
        cik,
        "no_candidate",
        accession=document.accession,
        form=document.form,
        filing_date=document.filing_date,
        detail=f"no segment table accounts for the consolidated revenue ({reasons})",
    )
