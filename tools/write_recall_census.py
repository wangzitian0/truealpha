#!/usr/bin/env python3
"""Regenerate `apps/data-engine/tests/recall_census.json` (#772, #822, #830, #833).

The census is what stops an extraction rule being fitted to one document and described as
general. The segment adapter's first reader — heading, row and units regexes — was developed
against ONE real 10-K, every test was written from that same document, and the suite proved it
worked on the document it was fitted to: the first deployed run over 25 issuers resolved one.

So what the adapter reads is committed per filing: the segment count each filing declares and
the segment revenue it tags. A change to either reader shows WHICH filings it moved, in the
diff, and a drop is as visible as a gain.

Run: uv run python tools/write_recall_census.py
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from data_engine.datahub.standards.segment_extraction import (
    SEGMENT_TOLERANCE,
    declared_segment_count,
    segment_name_for,
    tagged_segment_revenues,
)
from factors.shared.extraction import Candidate, Partition, select_exhaustive_partition

ROOT = Path(__file__).resolve().parent.parent
FILINGS = ROOT / "apps" / "data-engine" / "samples" / "filings"
CENSUS = ROOT / "apps" / "data-engine" / "tests" / "recall_census.json"

#: Consolidated revenue as the CAPTURE PLANE holds it, for the filings whose oracle is known —
#: absolute, read from Production, the same number a run checks the identity against. Only these
#: can have an accepted partition recorded; the rest record what the tags say alone.
TOTALS = {
    "AVGO_10K_000173016825000121.html": "63887000000",
    "AAPL_10K_000032019325000079.html": "416161000000",
}

COMMENT = (
    "Per-filing census of what the segment adapter reads from apps/data-engine/samples/filings: "
    "the declared segment count and the segment revenue each filing tags. Committed so a change "
    "to either reader shows WHICH filings it moved, in the diff, rather than being described as "
    "general because it scored on the one it was developed against. A drop counts as much as a "
    "gain. Regenerate with: uv run python tools/write_recall_census.py"
)


def census() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in sorted(FILINGS.glob("*.html")):
        if "8K" in path.name:
            continue  # 8-Ks carry no segment note; they are in the corpus for other adapters
        body = path.read_bytes()
        declared = declared_segment_count(body)
        tagged = tagged_segment_revenues(body)
        entry: dict = {
            "declared_segments": declared.evidence if declared is not None else None,
            "tagged_segments": [
                {
                    "concept": segments.concept,
                    "members": [member for member, _ in segments.parts],
                    "sum": str(sum((value for _, value in segments.parts), Decimal(0))),
                    "refusal": segments.refusal,
                }
                for segments in tagged
            ],
        }
        total = TOTALS.get(path.name)
        if total:
            accepted = []
            for segments in tagged:
                if segments.refusal is not None:
                    continue
                verdict = select_exhaustive_partition(
                    [Candidate(float(value), member) for member, value in segments.parts],
                    total=Decimal(total),
                    tolerance=SEGMENT_TOLERANCE * Decimal(10) ** segments.scale,
                )
                if isinstance(verdict, Partition):
                    accepted.append([segment_name_for(member) for member, _ in segments.parts])
            entry["accepted_partitions"] = accepted
        out[path.name] = entry
    return out


def main() -> int:
    # Once, and the number printed is the number written (review on #813).
    filings = census()
    CENSUS.write_text(json.dumps({"_comment": COMMENT, "filings": filings}, indent=2) + "\n")
    print(f"recall census written: {len(filings)} filings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
