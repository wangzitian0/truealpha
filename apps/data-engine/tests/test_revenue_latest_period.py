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

import os
import sys
from datetime import date
from pathlib import Path

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.production_topt.concept_mapping import DEFAULT_RULESET, publish_ruleset
from truealpha_contracts.concept_mapping import ConceptMappingRuleset

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
from sec_route import served_financial_record  # noqa: E402

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sec"
#: After the latest 10-K of both issuers. XEL filed its 10-K for FY2025 on 2026-02-25. CPRT filed its
#: 10-K for FY2026 on 2026-09-29. A cutoff before the second date would not show the CPRT period.
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


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        active.execute("select 1")
        yield active
    finally:
        active.rollback()
        active.close()


def _ruleset_v3() -> ConceptMappingRuleset:
    """The ruleset of the images before this change: the default without the utility total."""
    payload = DEFAULT_RULESET.model_dump(mode="json", exclude={"ruleset_id", "content_sha256"})
    payload["version"] = "production-topt-concepts:v3"
    for mapping in payload["mappings"]:
        if mapping["field"] == "revenue":
            mapping["concepts"] = [
                item for item in mapping["concepts"] if item["concept"] != "RegulatedAndUnregulatedOperatingRevenue"
            ]
    return ConceptMappingRuleset.model_validate(payload)


def test_a_published_head_governs_the_route_until_the_default_is_published(connection, monkeypatch) -> None:
    """`resolve_ruleset` reads `staging.accepted_ruleset_head` first, and `DEFAULT_RULESET` only when no
    head is published. So an environment with a head that pins v3 keeps the 2018 revenue of XEL after
    the deploy. It follows the default once the default is published as the head (`publish_ruleset`).

    The route below is the one the capture runs, with a connection, so the head is read."""
    v3 = _ruleset_v3()
    assert v3.content_sha256 != DEFAULT_RULESET.content_sha256
    body = (_FIXTURES / "XEL_CIK0000072903.trimmed.json").read_bytes()

    def served() -> tuple[str, str, str]:
        record = served_financial_record(
            monkeypatch, body, cik=72903, ticker="XEL", cutoff_date=_CUTOFF, connection=connection
        ).record
        assert record is not None
        return record.payload["revenue"], record.payload["revenue_period_end"], record.mapping_version

    publish_ruleset(connection, v3, note="pin the ruleset of the images before #1457")
    pinned = served()
    assert pinned[:2] == ("11537000000", "2018-12-31"), "a published head wins over the default"
    assert pinned[2].endswith(f"+{v3.content_sha256[:12]}"), "the row names the rules of the head"

    publish_ruleset(connection, DEFAULT_RULESET, note="follow the default after #1457")
    followed = served()
    assert followed[:2] == ("14669000000", "2025-12-31")
    assert followed[2].endswith(f"+{DEFAULT_RULESET.content_sha256[:12]}")
