#!/usr/bin/env python3
"""Regenerate `apps/data-engine/tests/recall_census.json` (#772).

The census is what stops a recall rule being fitted to one document and described as
general. `segment_extraction`'s heading pattern, window shape and units rule were all
developed against ONE real 10-K, every test was written from that same document, and the
suite proved the module worked on the document it was fitted to. The first deployed run over
25 issuers resolved one — the same one.

So the counts are committed per filing. A change that scores on a single document and zero
elsewhere shows that in the diff, and a DROP is as visible as a gain (AVGO went 13 -> 12
candidates during one change and nothing said so).

Run: uv run python tools/write_recall_census.py
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from data_engine.datahub.standards.filing_extraction import filing_plain_text
from data_engine.datahub.standards.segment_extraction import (
    SEGMENT_TOLERANCE,
    _windows,
    as_candidates,
    filing_scale,
    segment_candidates,
    windows_of,
)
from factors.shared.extraction import Partition, select_exhaustive_partition

ROOT = Path(__file__).resolve().parent.parent
FILINGS = ROOT / "apps" / "data-engine" / "samples" / "filings"
CENSUS = ROOT / "apps" / "data-engine" / "tests" / "recall_census.json"

#: Consolidated revenue as the CAPTURE PLANE holds it, for the filings whose oracle is known
#: — absolute, read from Production, the same number a run checks the identity against. Only
#: these can have an accepted partition recorded; the rest record recall alone.
TOTALS = {"AVGO_10K_000173016825000121.html": "63887000000"}

COMMENT = (
    "Per-filing recall census over apps/data-engine/samples/filings. Committed so a change to "
    "the extraction patterns shows WHICH filings it moved, in the diff, rather than being "
    "described as general because it scored on the one it was developed against. A drop counts "
    "as much as a gain. Regenerate with: uv run python tools/write_recall_census.py"
)


def census() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in sorted(FILINGS.glob("*.html")):
        if "8K" in path.name:
            continue  # 8-Ks carry no segment table; they are in the corpus for other adapters
        text = filing_plain_text(path.read_bytes())
        recalled = segment_candidates(text)
        scale = filing_scale(text)
        entry: dict = {
            "windows": len(_windows(text)),
            "candidates": len(recalled),
            "filing_scale": str(scale) if scale is not None else None,
        }
        total = TOTALS.get(path.name)
        if total and recalled:
            candidates = as_candidates(recalled)
            accepted = []
            for indices in windows_of(recalled).values():
                verdict = select_exhaustive_partition(
                    candidates,
                    total=Decimal(total),
                    tolerance=SEGMENT_TOLERANCE * recalled[indices[0]].multiplier,
                    indices=indices,
                )
                if isinstance(verdict, Partition):
                    accepted.append([recalled[i].segment_name for i in verdict.candidate_indices])
            entry["accepted_partitions"] = accepted
        out[path.name] = entry
    return out


def main() -> int:
    # Once, and the number printed is the number written. Computing it twice doubles a full
    # parse of every packaged filing and lets the log disagree with the file (review on #813).
    filings = census()
    CENSUS.write_text(json.dumps({"_comment": COMMENT, "filings": filings}, indent=2) + "\n")
    print(f"recall census written: {len(filings)} filings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
