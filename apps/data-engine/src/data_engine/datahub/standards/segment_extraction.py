"""Recall of segment revenue from a filing's own text (#772, q6).

The SEC-filing adapter for `segment_revenue`, beside `filing_extraction` rather than inside
it: it reuses that module's `latest_annual_filing` and `filing_plain_text` unchanged (both
carry no headcount semantics — measured, not assumed) and owns only what is different, which
is recall and the shape of the answer.

**Recall is deliberately not clever, and that is the design.** Measured on the packaged AVGO
10-K, a plain sweep of "revenue by segment" windows returns the real table AND a percentage
table (58 / 42) AND a gross-margin figure from the next paragraph. None of that is filtered
by cleverness here. `factors.shared.extraction.select_exhaustive_partition` refuses a set
whose parts do not account for the issuer's consolidated revenue — a number this module
never computes and cannot influence — so a greedy recall pass produces a REFUSAL, not a
wrong answer.

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
from decimal import Decimal

from factors.shared.extraction import Candidate

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
    value: Decimal
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
    for heading in _TABLE_HEADING.finditer(text):
        window = text[heading.start() : heading.start() + _WINDOW]
        # Stop at the table's own total row, so a second table under the same heading (the
        # percentage restatement) is a SEPARATE window rather than extra parts in this one.
        end = _TABLE_END.search(window)
        if end:
            window = window[: end.start()]
        for row in _ROW.finditer(window):
            name = row.group(1).strip()
            if _NOT_A_SEGMENT.search(name) or len(name.split()) > 6:
                continue
            value = Decimal(row.group(2).replace(",", ""))
            key = (name, value)
            if key in seen:
                continue
            seen.add(key)
            found.append(SegmentCandidate(name, value, row.group(0).strip(), heading.start()))
    return found


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
