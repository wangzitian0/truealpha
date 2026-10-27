"""init.md states the owner decisions of 2026-10-15 (#1333), and no sentence contradicts them.

Each forbidden phrase is a sentence that blocked a source, a module or a gate on a missing
oracle or a low score. Each required phrase names the replacement. No database is needed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
INIT = (ROOT / "init.md").read_text()
A3 = (ROOT / "docs" / "architecture-decisions" / "A3-source-governance.md").read_text()

FORBIDDEN = {
    "oracle blocks supply-chain edges": "The blocker is not recall but the absence of an oracle",
    "public availability oracle blocks moomoo ratings": "independently defensible public availability may enter PIT scoring",
    "moomoo has never run": "zero moomoo rows",
    "yfinance is fallback only": "Unofficial, no SLA, fallback only",
    "source-count tiers": "三个源且误差小于千分之一",
    "low score removes coverage": "`low_confidence` remains distinct from evidence verification and cannot count toward usable coverage",
    "sealed holdout gates Gate 2": "and pass #65 under the custody rule above",
    "custodian rule 18": "A named custodian controls labels",
    "restore drill gates graduation": "A destructive clean restore must meet declared database and raw-object RPO/RTO",
    "yfinance cannot be critical path": "can't be the sole dependency on a critical path",
    "gate fail closed": "(fail closed)",
    "RPO gate": "recovery within RPO/RTO",
    "two-column rule 17": "Every issuer decomposes into an operating component and a financial component; both are computed",
}

REQUIRED = {
    "uniform metric named": "gppe_uniform_charge_v0",
    "banking metric named": "gppe_banking_tce_v1",
    "continuous score": "Confidence is one continuous score from 0 to 1",
    "source prior order": "SEC filings, then moomoo, then Twelve Data, then yfinance",
    "vendor timestamp accepted": "`vendor_timestamp`",
    "listing session key": "`(listing, session_date)`",
    "no score blocks a row": "**No score blocks a row.**",
}


@pytest.mark.parametrize(("name", "phrase"), FORBIDDEN.items())
def test_init_md_has_no_contradicting_sentence(name: str, phrase: str) -> None:
    assert phrase not in INIT, f"init.md still says: {name}"


@pytest.mark.parametrize(("name", "phrase"), REQUIRED.items())
def test_init_md_states_the_decision(name: str, phrase: str) -> None:
    assert phrase in INIT, f"init.md lacks: {name}"


def test_the_success_section_counts_a_low_score_value_as_produced() -> None:
    assert "stale, unresolved, or low-confidence placeholder" not in INIT
    assert "A computed value with a low confidence score" in INIT


def test_a3_does_not_block_a_source_without_a_falsifier() -> None:
    assert "is inventory, not data" not in A3
    assert "lowest prior and a dashboard flag" in A3


def test_the_band_thresholds_in_init_md_equal_the_json_file_that_holds_them() -> None:
    """Rule 29: init.md copies the two thresholds, and this test holds the copy equal to its source."""
    bands = (ROOT / "common/contracts/src/truealpha_contracts/data/confidence_bands.v1.json").read_text()
    thresholds = json.loads(bands)
    high, medium = thresholds["high_from"], thresholds["medium_from"]
    sentence = f"High is a confidence of {high} or more. Medium is {medium} to below {high}. Low is below {medium}."
    assert sentence in INIT, "init.md and confidence_bands.v1.json disagree on the band thresholds"
