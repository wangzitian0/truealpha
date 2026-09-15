"""The corpus-wide census of what the segment adapter reads (#772, #822, #830, #833).

This exists because of a specific mistake, made three times in one day, in three places:

1. The adapter's first reader — heading, row and units regexes — was developed against ONE real
   10-K, and every test was written from that same document, so the suite proved it worked on
   the document it was fitted to. The first deployed run over 25 issuers resolved one: the same
   one. Measured across the packaged corpus at the time: 13 candidates on AVGO, **zero on eight
   other filings**.
2. A pull request's body predicted that a backward window would recover ADP's six tables. The
   deployed run returned an identical refusal distribution.
3. A test asserted "SHOP declares no scale anywhere". SHOP writes `(in US $ millions)` — the
   assertion was about the regex, not the document.

Every one of those is the same shape: something was measured, and reported as a measurement of
something else. The fix is not a better pattern, it is a cheaper measurement — run the reader
over every filing already in the repository and commit the per-file result, so a change that
scores on one document and nowhere else says so in the diff.

That reader is retired (#833): the filing's own tags answer instead. The census outlived it on
purpose, because the lesson is about measurement, not about regexes — the tag readers are pinned
per filing the same way.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from data_engine.datahub.standards.segment_extraction import (
    accepted_tagged_partition,
    declared_segment_count,
    segment_name_for,
    tagged_segment_revenues,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
FILINGS = REPO_ROOT / "apps" / "data-engine" / "samples" / "filings"
CENSUS = Path(__file__).with_name("recall_census.json")
#: Mirrors tools/write_recall_census.py. Kept here rather than imported so the test does not
#: depend on the generator it is checking.
TOTALS = {
    "AVGO_10K_000173016825000121.html": "63887000000",
    "AAPL_10K_000032019325000079.html": "416161000000",
}


def _measure(path: Path) -> dict:
    body = path.read_bytes()
    declared = declared_segment_count(body)
    tagged = tagged_segment_revenues(body)
    entry: dict = {
        "declared_segments": declared.evidence if declared is not None else None,
        "tagged_segments": [
            {
                "concept": segments.concept,
                "shape": segments.shape,
                "members": [member for member, _ in segments.parts],
                "reconciling": [member for member, _ in segments.reconciling],
                "sum": str(sum((value for _, value in segments.parts), Decimal(0))),
                "refusal": segments.refusal,
            }
            for segments in tagged
        ],
    }
    total = TOTALS.get(path.name)
    if total:
        # The adapter's own selection, not a copy of it: the census measures what a run would accept.
        accepted = accepted_tagged_partition(tagged, total=Decimal(total), period_end=None)
        entry["accepted_partition"] = (
            None if isinstance(accepted, list) else [segment_name_for(member) for member, _ in accepted.parts]
        )
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


def test_every_filing_reads_as_the_committed_census_says(committed) -> None:
    """The whole point. A reader change that moves one filing and not the others shows exactly
    that, per file, in the diff of this JSON — and has to be explained in the same pull request
    rather than described as general."""
    drift = {}
    for name, expected in sorted(committed.items()):
        actual = _measure(FILINGS / name)
        if actual != expected:
            drift[name] = {"expected": expected, "actual": actual}
    assert drift == {}, (
        "the readers moved on these filings. If the change is intended, regenerate the census in "
        "the SAME pull request (uv run python tools/write_recall_census.py) and say in the body "
        f"which filings moved and why:\n{json.dumps(drift, indent=2)}"
    )


def test_the_filings_with_a_known_oracle_still_balance(committed) -> None:
    """Coverage can move for many reasons; an acceptance cannot move quietly. Both are checked
    against the consolidated revenue the capture plane actually holds. AAPL matters most: it is
    a filing the DEPLOYED run meets, and its five tagged geographies balance to the dollar."""
    avgo = committed["AVGO_10K_000173016825000121.html"]
    assert avgo["accepted_partition"] == ["Infrastructure Software", "Semiconductor Solutions"]

    aapl = committed["AAPL_10K_000032019325000079.html"]
    assert aapl["accepted_partition"] == ["Americas", "Europe", "Greater China", "Japan", "Rest of Asia Pacific"]


def test_the_corpus_holds_filings_the_deployed_run_actually_meets(committed) -> None:
    """The census's own blind spot, closed and pinned: AAPL and ADP are in the universe the
    standards lane walks, and their behaviour was measured through the deployed gateway before
    either was packaged."""
    assert "AAPL_10K_000032019325000079.html" in committed
    adp = committed["ADP_10K_000000867026000030.html"]
    assert adp["tagged_segments"], "ADP tags its two segments; that they exceed its revenue is the identity's call"


def test_the_single_segment_declaration_is_measured_per_filing(committed) -> None:
    """#822: the path that lands a partition no identity can check is decided by the count the
    filer tags, so that count is pinned per filing. PLUG is the case to watch — its prose says
    one segment and it tags nothing, so it must stay undeclared."""
    declared = {name.split("_")[0]: entry["declared_segments"] for name, entry in committed.items()}
    assert declared["SHOP"] == "us-gaap:NumberOfReportableSegments=1@2025-12-31"
    assert declared["AVGO"] == "us-gaap:NumberOfReportableSegments=2@2025-11-02"
    assert declared["PLUG"] is None


def test_the_tagged_segment_revenue_is_measured_per_filing(committed) -> None:
    """#830: segment revenue the filer tags is what answers, so what the reader makes of each
    packaged filing is pinned. AAPL and AVGO balance their oracles; ADM's three tagged segments
    are short of its revenue by the off-axis "Other Business"."""
    by_ticker = {name.split("_")[0]: entry["tagged_segments"] for name, entry in committed.items()}
    (aapl,) = by_ticker["AAPL"]
    assert aapl["sum"] == TOTALS["AAPL_10K_000032019325000079.html"]
    (avgo,) = by_ticker["AVGO"]
    assert avgo["sum"] == TOTALS["AVGO_10K_000173016825000121.html"]
    assert {entry["concept"] for entry in by_ticker["ADM"]} == {
        "us-gaap:Revenues",
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    }
    assert all(entry["reconciling"] == ["us-gaap:CorporateNonSegmentMember"] for entry in by_ticker["ADM"]), (
        "the off-axis corporate revenue a short set may be completed by (#835)"
    )


def test_a_filing_that_tags_nothing_is_recorded_as_empty_rather_than_omitted(committed) -> None:
    """Empty entries are the census's most useful ones: they are the filings the adapter answers
    only by declaration (DDOG, DUOL, SHOP) or not at all (PLUG's 2021 filings predate tagging),
    and a census that listed only the successes would hide them."""
    empty = {name.split("_")[0] for name, entry in committed.items() if not entry["tagged_segments"]}
    assert {"DDOG", "DUOL", "SHOP", "PLUG"} <= empty
