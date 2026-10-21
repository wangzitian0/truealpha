"""The served revenue of CPRT and XEL is the revenue of the latest annual period (#1457).

Production QQQ served annual revenue of 2018 for two issuers in 2026:

| issuer | served period end | latest annual period | revenue of that period |
|---|---|---|---|
| CPRT | 2018-07-31 | 2026-07-31 | 4,666,209,000 |
| XEL  | 2018-12-31 | 2025-12-31 | 14,669,000,000 |

Both revenue figures of the last column are the figures of the second origin in the issue (the
moomoo financials parser of the same capture run). The SEC company-facts rows in the fixtures are
the first origin. The two origins agree to the dollar, so the expected values below do not come
from this repository's resolver.

Each issuer changed its revenue concept after 2018:

* CPRT moved to `RevenueFromContractWithCustomerIncludingAssessedTax`. The ruleset has had that
  concept since #1379 (`production-topt-concepts:v3`). The head capture run of the issue predates it.
* XEL moved to `RegulatedAndUnregulatedOperatingRevenue`, the utility total. The ruleset did not
  list it. XEL reports no other revenue concept after FY2018. The latest annual period was FY2018.

The route below is the one the capture runs (`build_route`), with the shipped default ruleset. The
fixtures hold verbatim SEC rows and name their source document and its sha256. No test here makes a
network call.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest
from data_engine.datahub.production_topt.concept_mapping import DEFAULT_RULESET

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
from sec_route import served_financial_record  # noqa: E402

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sec"
#: The day after the latest 10-K of both issuers was filed (CPRT filed on 2026-09-29).
_CUTOFF = date(2026, 10, 20)

_CASES = [
    pytest.param(
        "CPRT_CIK0000900075.trimmed.json",
        900075,
        "CPRT",
        # served, expected
        ("1805695000", "2018-07-31"),
        ("4666209000", "2026-07-31"),
        # The newest GrossProfit row of CPRT ends 2019-07-31.
        "2019-07-31",
        id="CPRT",
    ),
    pytest.param(
        "XEL_CIK0000072903.trimmed.json",
        72903,
        "XEL",
        ("11537000000", "2018-12-31"),
        ("14669000000", "2025-12-31"),
        # The newest gross profit that the rows give XEL ends 2018-12-31.
        "2018-12-31",
        id="XEL",
    ),
]


@pytest.mark.parametrize(("fixture", "cik", "ticker", "stale", "latest", "operating_end"), _CASES)
def test_the_deployed_route_serves_the_revenue_of_the_latest_annual_period(
    monkeypatch,
    fixture: str,
    cik: int,
    ticker: str,
    stale: tuple[str, str],
    latest: tuple[str, str],
    operating_end: str,
) -> None:
    """Acceptance 1 of #1457. Red on the main of the issue for XEL: the route served 11,537,000,000 of 2018-12-31."""
    outcome = served_financial_record(
        monkeypatch, (_FIXTURES / fixture).read_bytes(), cik=cik, ticker=ticker, cutoff_date=_CUTOFF
    )
    assert outcome.record is not None
    payload = outcome.record.payload

    assert (payload["revenue"], payload["revenue_period_end"]) == latest, f"stale figure was {stale}"
    assert payload["vintage"]["revenue"]["period_end"] == latest[1]
    # A gross profit of an older period never stands beside this revenue (#1114). The adapter drops it
    # and names the period it dropped. So the P/GP of the issuer is unavailable, not a mix of two years.
    assert payload["gross_profit"] is None
    assert payload["operating_period_stale"] is True
    assert payload["stale_operating_period_end"] == operating_end


def test_the_fixtures_hold_the_old_figures_that_the_route_must_not_serve() -> None:
    """A fixture without the stale year would pass for the wrong reason. Each one holds it, verbatim."""
    import json

    for fixture, concept, stale_value in (
        ("CPRT_CIK0000900075.trimmed.json", "Revenues", 1805695000),
        ("XEL_CIK0000072903.trimmed.json", "Revenues", 11537000000),
    ):
        facts = json.loads((_FIXTURES / fixture).read_bytes())
        rows = facts["facts"]["us-gaap"][concept]["units"]["USD"]
        assert any(row["val"] == stale_value and row["form"] == "10-K" and row["fp"] == "FY" for row in rows), fixture


def test_the_utility_total_never_replaces_the_figure_of_an_earlier_concept_for_the_same_period() -> None:
    """The utility concept is the last synonym. For one period, the earlier concept keeps its value."""
    from data_engine.datahub.production_topt.sec_financial_adapter import build_bundle
    from factors.production_topt import OperatingBranch

    def annual(value: int) -> dict:
        row = {"start": "2025-01-01", "end": "2025-12-31", "val": value, "filed": "2026-02-10"}
        return {"units": {"USD": [{**row, "form": "10-K", "fy": 2025, "fp": "FY"}]}}

    facts = {"facts": {"us-gaap": {"Revenues": annual(900), "RegulatedAndUnregulatedOperatingRevenue": annual(950)}}}

    bundle = build_bundle(facts, _CUTOFF, OperatingBranch.NON_FINANCIAL, ruleset=DEFAULT_RULESET)

    assert (bundle.revenue, bundle.revenue_period_end) == (900, date(2025, 12, 31))
