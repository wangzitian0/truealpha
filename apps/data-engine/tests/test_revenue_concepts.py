"""One revenue concept list, and the CRWD revenue that it did not include (#1379).

CRWD tags its revenue only as `RevenueFromContractWithCustomerIncludingAssessedTax`. The concept
ruleset listed only the Excluding form and `Revenues`. So the capture stored no CRWD revenue, and
the core result was `missing_revenue`. The segment reader and the vendor oracle each held a copy
of the list that did include the Including form. Three lists disagreed.

The checks below:

1. The trimmed CRWD companyfacts, read through the deployed SEC route, give revenue 4,812,005,000.
2. An issuer that reports two declared forms for one period keeps the value of the earlier form.
3. The segment reader tries the ruleset's concepts in the ruleset's order. It holds no copy.
4. The vendor oracle reads each ruleset concept. It reads other concepts only where this file
   states the reason.

The fixture rows are verbatim SEC rows. The fixture names its source document and that document's
sha256. No test here makes a network call.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from data_engine.datahub.production_topt.concept_mapping import DEFAULT_RULESET
from data_engine.datahub.production_topt.parser_identity import MAPPING_VERSION, PARSER_VERSION
from data_engine.datahub.production_topt.sec_financial_adapter import (
    annual_values_by_period_end,
    build_bundle,
)
from data_engine.datahub.standards import segment_extraction
from data_engine.quality import vendor_oracle
from factors.production_topt import OperatingBranch

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
from sec_route import served_financial_record  # noqa: E402

_TESTS = Path(__file__).resolve().parent
_CRWD_FIXTURE = _TESTS / "fixtures" / "sec" / "CRWD_CIK0001535527.trimmed.json"
_SAMPLES = _TESTS.parent / "samples" / "sec"
_CRWD_CIK = 1535527
#: The day that production recorded the CRWD payload with no revenue (#1379).
_CUTOFF = date(2026, 10, 15)

#: The concepts that the vendor oracle reads beyond the ruleset's `revenue` field, and why.
#: The oracle re-derives revenue by an independent route, so a wider list is its purpose (#429).
_ORACLE_ONLY = {
    "SalesRevenueNet": "A total from before ASC 606. The ruleset leaves it out: see the sample test below.",
    "SalesRevenueGoodsNet": "The goods part of SalesRevenueNet. NICE and PLUG tag it last for FY2017.",
    "SalesRevenueServicesNet": "The services part of SalesRevenueNet. NICE, PLUG and SHOP tag it last for FY2017.",
    "RevenuesNetOfInterestExpense": "A bank's top line. The ruleset declares it under `bank_revenue`.",
}


def _ruleset_names(field: str) -> list[str]:
    mapping = DEFAULT_RULESET.mapping_for(field)
    assert mapping is not None, f"the ruleset declares no {field!r} field"
    return [f"{item.taxonomy}:{item.concept}" for item in mapping.concepts]


def _annual(end: str, start: str, val: int, filed: str) -> dict[str, Any]:
    return {"start": start, "end": end, "val": val, "filed": filed, "form": "10-K", "fy": int(end[:4]), "fp": "FY"}


def test_the_deployed_route_reads_crwd_revenue_from_the_including_form(monkeypatch) -> None:
    """Criterion 1: production stored gross profit, assets and shares for CRWD, and no revenue.

    The route is the one the capture runs (`build_route`), with no database. So the ruleset is the
    shipped default, as in production with no governed head. Only the two SEC reads are replaced.
    """
    outcome = served_financial_record(
        monkeypatch, _CRWD_FIXTURE.read_bytes(), cik=_CRWD_CIK, ticker="CRWD", cutoff_date=_CUTOFF
    )
    assert outcome.record is not None
    payload = outcome.record.payload
    # Gross profit, total assets and shares equal the stored production payload (#1379).
    # The revenue is the value that the stored payload did not have.
    expected = {
        "revenue": "4812005000",
        "revenue_period_end": "2026-01-31",
        "gross_profit": "3593076000",
        "operating_period_end": "2026-01-31",
        "total_assets": "12025001000",
        "shares_outstanding": "1023934842",
    }
    assert {key: payload[key] for key in expected} == expected
    assert payload["vintage"]["revenue"]["accession"] == "0001535527-26-000010"
    # The observation names its rules by the ruleset hash, so a ruleset edit is a new mapping
    # version with no new parser vintage (parser_identity, #1379).
    assert outcome.record.parser_version == PARSER_VERSION
    assert outcome.record.mapping_version == f"{MAPPING_VERSION}+{DEFAULT_RULESET.content_sha256[:12]}"


@pytest.mark.parametrize(
    "preferred",
    ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"],
)
def test_an_issuer_that_reports_two_forms_keeps_the_earlier_form(preferred: str) -> None:
    """Criterion 3: the Including form is the last synonym, so it never replaces a form before it.

    Both concepts report the same year in the same 10-K, with different values.
    """
    facts = {
        "facts": {
            "us-gaap": {
                preferred: {"units": {"USD": [_annual("2025-12-31", "2025-01-01", 900, "2026-02-10")]}},
                "RevenueFromContractWithCustomerIncludingAssessedTax": {
                    "units": {"USD": [_annual("2025-12-31", "2025-01-01", 950, "2026-02-10")]}
                },
            }
        }
    }

    bundle = build_bundle(facts, _CUTOFF, OperatingBranch.NON_FINANCIAL, ruleset=DEFAULT_RULESET)

    assert (bundle.revenue, bundle.revenue_period_end) == (900, date(2025, 12, 31))
    assert bundle.vintages["revenue"]["period_end"] == "2025-12-31"


def test_the_segment_reader_tries_the_ruleset_revenue_concepts_in_order() -> None:
    """Criterion 2, segment half. The segment oracle is the capture's `revenue`. So the reader tries
    the concepts that figure resolves from, in the same order, then the bank top line."""
    expected = list(dict.fromkeys(_ruleset_names("revenue") + _ruleset_names("bank_revenue")))

    assert list(segment_extraction._REVENUE_CONCEPTS) == expected


def test_the_vendor_oracle_differs_from_the_ruleset_only_where_stated() -> None:
    """Criterion 2, oracle half. The oracle must read each ruleset concept, or it cannot audit it.
    Each other concept that it reads must have a reason in `_ORACLE_ONLY`."""
    ruleset = _ruleset_names("revenue")
    assert all(name.startswith("us-gaap:") for name in ruleset), "the oracle reads only the us-gaap taxonomy"
    oracle = {f"us-gaap:{name}" for name in vendor_oracle._REVENUE_CONCEPTS}

    assert [name for name in ruleset if name not in oracle] == []
    assert sorted(name.removeprefix("us-gaap:") for name in oracle - set(ruleset)) == sorted(_ORACLE_ONLY)
    assert "us-gaap:RevenuesNetOfInterestExpense" in _ruleset_names("bank_revenue")


@pytest.mark.parametrize("sample", ["NICE_CIK0001003935.json", "PLUG_CIK0001093691.json"])
def test_sales_revenue_net_holds_only_years_that_a_later_concept_replaces(sample: str) -> None:
    """The reason that the ruleset leaves `SalesRevenueNet` out, on real companyfacts.

    Its last annual year is FY2017. The ruleset resolves a later year from `Revenues`. So the concept
    could add only stale figures, and none of these issuers needs it.
    """
    facts = json.loads((_SAMPLES / sample).read_bytes())

    legacy = annual_values_by_period_end(facts, "us-gaap", "SalesRevenueNet", "USD", _CUTOFF)
    bundle = build_bundle(facts, _CUTOFF, OperatingBranch.NON_FINANCIAL, ruleset=DEFAULT_RULESET)

    assert legacy, f"{sample} carries no annual SalesRevenueNet"
    assert max(legacy) == date(2017, 12, 31)
    assert bundle.revenue_period_end == date(2025, 12, 31)
    assert bundle.vintages["revenue"]["period_end"] == "2025-12-31"
