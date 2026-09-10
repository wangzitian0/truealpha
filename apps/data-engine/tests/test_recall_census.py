"""The corpus-wide recall census (#772).

This exists because of a specific mistake, made three times in one day, in three places:

1. `segment_extraction`'s heading pattern, window shape and units rule were all developed
   against ONE real 10-K, and every test was written from that same document — so the suite
   proved the module worked on the document it was fitted to. The first deployed run over 25
   issuers resolved one: the same one. Measured across the packaged corpus at the time: 13
   candidates on AVGO, **zero on eight other filings**.
2. A pull request's body predicted that the backward window would recover ADP's six tables.
   The deployed run returned an identical refusal distribution.
3. A test asserted "SHOP declares no scale anywhere". SHOP writes `(in US $ millions)` — the
   assertion was about the regex, not the document.

Every one of those is the same shape: something was measured, and reported as a measurement
of something else. The fix is not a better regex, it is a cheaper measurement — run the rule
over every filing already in the repository and commit the per-file result, so a change that
scores on one document and zero elsewhere says so in the diff.

A DROP counts as much as a gain. AVGO went 13 -> 12 candidates during one change and nothing
said so; that is exactly the silent movement this pins.

The census is not a quality bar. Zeros are in it on purpose: DDOG and DUOL are single-segment
issuers with no table to find, and COST and ADM lay their tables out vertically or nest
sub-totals, which needs a table model this module does not have. What the census asserts is
that those numbers are *known*, not that they are good.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
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

REPO_ROOT = Path(__file__).resolve().parents[3]
FILINGS = REPO_ROOT / "apps" / "data-engine" / "samples" / "filings"
CENSUS = Path(__file__).with_name("recall_census.json")
#: Mirrors tools/write_recall_census.py. Kept here rather than imported so the test does not
#: depend on the generator it is checking.
TOTALS = {"AVGO_10K_000173016825000121.html": "63887000000"}


def _measure(path: Path) -> dict:
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
    return entry


@pytest.fixture(scope="module")
def committed() -> dict:
    assert CENSUS.exists(), f"the census is missing: {CENSUS}"
    return json.loads(CENSUS.read_text())["filings"]


def test_the_census_covers_every_packaged_annual_filing(committed) -> None:
    """A filing added to the corpus and left out of the census would be a document nobody
    measured against — which is the state this whole file exists to end."""
    packaged = {p.name for p in FILINGS.glob("*.html") if "8K" not in p.name}
    assert set(committed) == packaged, (
        "regenerate: uv run python tools/write_recall_census.py "
        f"(missing {sorted(packaged - set(committed))}, stale {sorted(set(committed) - packaged)})"
    )


def test_recall_on_every_filing_matches_the_committed_census(committed) -> None:
    """The whole point. A pattern change that moves one filing and not the others shows
    exactly that, per file, in the diff of this JSON — and has to be explained in the same
    pull request rather than described as general."""
    drift = {}
    for name, expected in sorted(committed.items()):
        actual = _measure(FILINGS / name)
        if actual != expected:
            drift[name] = {"expected": expected, "actual": actual}
    assert drift == {}, (
        "recall moved on these filings. If the change is intended, regenerate the census in "
        "the SAME pull request (uv run python tools/write_recall_census.py) and say in the "
        f"body which filings moved and why:\n{json.dumps(drift, indent=2)}"
    )


def test_the_one_filing_with_a_known_oracle_still_balances(committed) -> None:
    """Coverage can move for many reasons; this cannot move quietly. AVGO's two segments are
    the only accepted partition in the corpus, and they are checked against the consolidated
    revenue the capture plane actually holds."""
    avgo = committed["AVGO_10K_000173016825000121.html"]
    assert avgo["accepted_partitions"] == [["Semiconductor solutions", "Infrastructure software"]]


def test_a_filing_with_no_table_is_recorded_as_zero_rather_than_omitted(committed) -> None:
    """Zeros are the census's most useful entries: they are the filings a future pattern is
    supposed to move, and a census that listed only the successes would hide them.

    DDOG and DUOL state they operate as a single segment — they have no table to find, and
    the single-segment path answers for them elsewhere. COST and ADM have tables this module
    cannot read yet.
    """
    zeros = {name for name, e in committed.items() if e["candidates"] == 0}
    assert "DDOG_10K_000162828026008819.html" in zeros
    assert "DUOL_10K_000162828026012494.html" in zeros
    assert len(zeros) >= 4, "the corpus still holds filings recall does not reach, and says so"
