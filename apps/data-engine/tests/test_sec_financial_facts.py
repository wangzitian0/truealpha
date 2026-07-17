import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from data_engine.sec_financial_facts import extract_gross_profit, extract_revenue, extract_total_assets

SAMPLES_DIR = Path(__file__).resolve().parents[1] / "samples" / "sec"

_SAMPLE_FILES = {
    "adm": "ADM_CIK0000007084.json",
    "ddog": "DDOG_CIK0001561550.json",
    "duol": "DUOL_CIK0001562088.json",
    "jpm": "JPM_CIK0000019617.json",
    "meta": "META_CIK0001326801.json",
    "nice": "NICE_CIK0001003935.json",
    "nvda": "NVDA_CIK0001045810.json",
    "plug": "PLUG_CIK0001093691.json",
    "shop": "SHOP_CIK0001594805.json",
}


def _company_facts(ticker: str) -> dict:
    return json.loads((SAMPLES_DIR / _SAMPLE_FILES[ticker]).read_text())


_FAR_FUTURE_CUTOFF = datetime(2027, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize("ticker", sorted(_SAMPLE_FILES))
def test_total_assets_extracts_for_every_sample_issuer(ticker: str) -> None:
    observation = extract_total_assets(_company_facts(ticker), entity_id=f"issuer.{ticker}", cutoff=_FAR_FUTURE_CUTOFF)
    assert observation is not None
    assert observation.metric == "total_assets"
    assert observation.entity_id == f"issuer.{ticker}"
    assert observation.value > 0
    assert observation.confidence == Decimal("0.98")


@pytest.mark.parametrize("ticker", ["adm", "ddog", "duol", "nice", "nvda", "plug", "shop"])
def test_gross_profit_extracts_for_issuers_that_report_it(ticker: str) -> None:
    observation = extract_gross_profit(_company_facts(ticker), entity_id=f"issuer.{ticker}", cutoff=_FAR_FUTURE_CUTOFF)
    assert observation is not None
    assert observation.metric == "gross_profit"
    # PLUG (loss-making) genuinely reports negative gross profit -- a real
    # value, not a bug -- so this only checks a value was resolved at all.
    assert observation.value != 0


@pytest.mark.parametrize("ticker", ["jpm", "meta"])
def test_gross_profit_is_none_not_fabricated_when_tag_is_absent(ticker: str) -> None:
    # JPM: financial issuer, no GrossProfit tag. META: non-financial but also
    # doesn't report GrossProfit — both must surface as a real gap, not a
    # guessed fallback (e.g. revenue - cost_of_revenue).
    observation = extract_gross_profit(_company_facts(ticker), entity_id=f"issuer.{ticker}", cutoff=_FAR_FUTURE_CUTOFF)
    assert observation is None


def test_pit_cutoff_excludes_facts_not_yet_filed() -> None:
    # DDOG's FY2025 10-K (value 2,740,201,000) was filed 2026-02-18. A cutoff
    # the day before must not see it.
    before_filing = datetime(2026, 2, 17, tzinfo=UTC)
    observation = extract_gross_profit(_company_facts("ddog"), entity_id="issuer.ddog", cutoff=before_filing)
    assert observation is not None
    assert observation.value != Decimal("2740201000")
    assert observation.knowable_at <= before_filing


def test_pit_cutoff_sees_the_fact_once_filed() -> None:
    after_filing = datetime(2026, 2, 18, tzinfo=UTC)
    observation = extract_gross_profit(_company_facts("ddog"), entity_id="issuer.ddog", cutoff=after_filing)
    assert observation is not None
    assert observation.value == Decimal("2740201000")
    assert observation.fiscal_period == "FY2025"
    assert observation.accession == "0001628280-26-008819"


def test_selects_latest_by_filed_end_accession_value_not_first_match() -> None:
    # DDOG's most recent 10-K (accn 0001628280-26-008819) restates prior years
    # too (FY2023, FY2024, FY2025 all appear with the same filed date) --
    # selection must land on the FY matching `end`, not just the max filed date.
    observation = extract_total_assets(_company_facts("ddog"), entity_id="issuer.ddog", cutoff=_FAR_FUTURE_CUTOFF)
    assert observation is not None
    assert observation.fiscal_period == "FY2025"
    assert observation.value == Decimal("6643844000")


def test_revenue_prefers_the_tag_the_issuer_actively_files_under() -> None:
    # NVDA's RevenueFromContractWithCustomerExcludingAssessedTax data stops
    # in 2022 ($26.9B); Revenues continues through 2026 ($215.9B). A fixed
    # tag priority would silently return the four-year-stale figure.
    observation = extract_revenue(_company_facts("nvda"), entity_id="issuer.nvda", cutoff=_FAR_FUTURE_CUTOFF)
    assert observation is not None
    assert observation.value == Decimal("215938000000")


def test_revenue_switches_preferred_tag_per_issuer_not_a_global_priority() -> None:
    # META is the mirror image of NVDA: Revenues is stale (last filed value
    # is from fiscal 2017), RevenueFromContractWithCustomerExcludingAssessedTax
    # is current. Proves selection is per-issuer recency, not "always prefer
    # tag X".
    observation = extract_revenue(_company_facts("meta"), entity_id="issuer.meta", cutoff=_FAR_FUTURE_CUTOFF)
    assert observation is not None
    assert observation.value == Decimal("200966000000")


def test_revenue_is_none_when_equally_current_tags_disagree() -> None:
    # ADM files both Revenues ($80.3B) and
    # RevenueFromContractWithCustomerExcludingAssessedTax ($25.0B) in the
    # same 10-K -- evidently different revenue concepts for a commodities
    # issuer, not the same number twice. Neither is safe to guess.
    observation = extract_revenue(_company_facts("adm"), entity_id="issuer.adm", cutoff=_FAR_FUTURE_CUTOFF)
    assert observation is None


def test_revenue_extracts_when_only_one_candidate_tag_exists() -> None:
    observation = extract_revenue(_company_facts("shop"), entity_id="issuer.shop", cutoff=_FAR_FUTURE_CUTOFF)
    assert observation is not None
    assert observation.metric == "revenue"


def test_unregistered_tag_returns_none_not_an_error() -> None:
    from data_engine.sec_financial_facts import extract_annual_metric

    result = extract_annual_metric(
        _company_facts("ddog"),
        tag="SomeTagThatDoesNotExist",
        metric="not_a_real_metric",
        entity_id="issuer.ddog",
        cutoff=_FAR_FUTURE_CUTOFF,
    )
    assert result is None
