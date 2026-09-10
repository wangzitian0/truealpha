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

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from factors.shared.extraction import (
    Candidate,
    Partition,
    PartitionRefusal,
    select_exhaustive_partition,
)
from truealpha_contracts.standards import MetricStandard

from data_engine.datahub.standards.filing_extraction import (
    ExtractionOutcome,
    filing_plain_text,
    latest_annual_filing,
)
from data_engine.sources.gateway import CapacityExceeded

#: The sentence that introduces a segment revenue table. Every phrasing seen in the packaged
#: filings; a heading this misses costs a `no_candidates` refusal, never a wrong number.
_TABLE_HEADING = re.compile(
    r"(?:net\s+)?revenue\s+by\s+(?:reportable\s+)?segment"
    r"|segment\s+(?:net\s+)?revenue"
    r"|revenues?\s+from\s+external\s+customers\s+by\s+(?:reportable\s+)?segment",
    re.IGNORECASE,
)

#: One table row: a label followed by its first numeric column. The SECOND column is
#: deliberately not read — it is the prior fiscal year, and mixing the years is the trap that
#: makes a partition sum to neither. Which column belongs to which year is stated in the
#: header the window starts with, so the first is this year's by position within the window.
_ROW = re.compile(r"([A-Z][A-Za-z&/,\.\-' ]{3,60}?)\s+\$?\s*([\d,]{2,15})(?:\s|$)")

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
_UNITS = re.compile(r"\(\s*in\s+(thousands|millions|billions)\b", re.IGNORECASE)
_MULTIPLIERS = {
    "thousands": Decimal("1000"),
    "millions": Decimal("1000000"),
    "billions": Decimal("1000000000"),
}


#: Labels that are never a segment. `total` is the identity's own oracle restated inside the
#: table; a month name is a header date fragment the row pattern picks up.
_NOT_A_SEGMENT = re.compile(
    r"^total\b"
    r"|^(?:january|february|march|april|may|june|july|august|september|october|november|december)\b"
    r"|\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\b",
    re.IGNORECASE,
)


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
    #: The scale this window declared. Per window, not per filing: an issuer may state its
    #: segment table in millions and a supplementary table in thousands.
    multiplier: Decimal
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
    for start, window in _windows(text):
        units = _UNITS.search(window)
        if units is None:
            # Not a judgement about whether this table is the right one — that is the
            # identity's job. A number whose scale the document never states cannot be
            # compared to an absolute oracle at all.
            continue
        multiplier = _MULTIPLIERS[units.group(1).lower()]
        for row in _ROW.finditer(window):
            name = row.group(1).strip()
            if _NOT_A_SEGMENT.search(name) or len(name.split()) > 6:
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
                    sentence=row.group(0).strip(),
                    window_start=start,
                )
            )
    return found


def _windows(text: str) -> list[tuple[int, str]]:
    """Each segment-table heading and the text from it to that table's own total row."""
    windows = []
    for heading in _TABLE_HEADING.finditer(text):
        window = text[heading.start() : heading.start() + _WINDOW]
        # Stop at the table's own total row, so a second table under the same heading (the
        # percentage restatement) is a SEPARATE window rather than extra parts in this one.
        end = _TABLE_END.search(window)
        if end:
            window = window[: end.start()]
        windows.append((heading.start(), window))
    return windows


def unitless_windows(text: str) -> int:
    """How many segment tables were found but state no scale.

    Reported so a filing whose tables were all skipped for missing units refuses with that
    reason instead of "no segment table matched" — two different problems with two different
    fixes (a heading this module cannot read vs. a caption shape it cannot read).
    """
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
select p.normalized_payload->>'revenue'
from staging.capture_normalized_observations o
join staging.capture_observation_payloads p on p.observation_id = o.observation_id
join raw.capture_source_vintages v on v.source_vintage_id = o.source_vintage_id
where o.semantic_type = 'financial-fact'
  and v.source_record_id = %s
  and o.knowable_at <= %s
  and p.normalized_payload->>'revenue' is not null
order by o.knowable_at desc
limit 1
"""


def consolidated_revenue(connection: Any, cik: int, *, cutoff: datetime) -> Decimal | None:
    """The issuer's own consolidated revenue at this cutoff — the partition's oracle.

    Read from the SAME plane the wide row is built from, so the number the parts are checked
    against is the number a reader sees beside them. Point-in-time by construction: only
    observations knowable at or before the cutoff are eligible, and the newest of those wins.

    ABSOLUTE, in the issuer's reporting currency. The parts are scaled to match at recall
    (`segment_candidates`) from the units each table declares, so the identity compares two
    numbers on the same scale rather than one that is right and one that is off by 10^6.

    Returns None when the issuer has no revenue observation, which the caller turns into a
    `no_total` refusal rather than a partition it cannot check.
    """
    row = connection.execute(_CONSOLIDATED_REVENUE_SQL, (f"companyfacts:CIK{cik:010d}", cutoff)).fetchone()
    return None if row is None or row[0] is None else Decimal(str(row[0]))


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

    Write mode is not implemented in this slice — the fact writer lands with the standard's
    registration. `write=True` therefore reports what it WOULD land rather than pretending
    to have landed it.
    """
    del record_cik, issuer_label  # accepted for signature parity; not used by this adapter
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

    total = consolidated_revenue(connection, cik, cutoff=cutoff)
    text = filing_plain_text(document.body)
    recalled = segment_candidates(text)
    if not recalled:
        skipped = unitless_windows(text)
        return ExtractionOutcome(
            cik,
            "no_candidate",
            accession=document.accession,
            form=document.form,
            filing_date=document.filing_date,
            detail=(
                f"{skipped} segment table(s) state no scale, so no part could be compared"
                if skipped
                else "no segment table matched in the filing text"
            ),
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
            named = ", ".join(
                f"{recalled[i].segment_name}={recalled[i].stated_value}" for i in verdict.candidate_indices
            )
            return ExtractionOutcome(
                cik,
                "resolved",
                extractor=verdict.extractor,
                accession=document.accession,
                form=document.form,
                filing_date=document.filing_date,
                # `write` changes only what this SAYS, because the writer does not exist yet.
                # It used to change nothing at all (`"resolved" if not write else "resolved"`)
                # while the docstring claimed write mode reported what it would land — so an
                # operator running the standard in write mode read the same sentence a probe
                # produces and had no way to tell no fact was written.
                detail=(
                    f"{'would land ' if write else ''}{len(verdict.candidate_indices)} segments "
                    f"accounting for {verdict.total} (residual {verdict.residual}): {named}"
                ),
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
