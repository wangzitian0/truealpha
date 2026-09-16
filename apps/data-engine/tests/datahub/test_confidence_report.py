"""The datahub confidence & accuracy report (owner standard, 2026-09-15).

"HIGH confidence = multiple sources agree, MEDIUM = multiple sources present, LOW = covered
by one source." The classifier is pure and every band is driven here by a synthetic cell
shaped like the persisted observations; the independence rule (two producers of one lineage
are not two sources) has its own test because it is the rule most likely to be quietly
relaxed. The families the report grades are asserted against `source_registrations`, so a
registered semantic cannot go ungraded and the report cannot grade a semantic nothing
captures. Every field of the bar is its own family (#865), driven here from payloads shaped
like the persisted observations, so a field one origin never wrote is a missing assertion
rather than a conflict, and the per-field accuracy cross-check is driven from the shape the
quality report persists. The fused fundamentals (#866) are driven from payloads shaped like
the primary's and the statements origin's, so period alignment and the currency gate are
exercised where they live.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, date, datetime
from decimal import Decimal

import dagster as dg
import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub import confidence_report as cr
from data_engine.datahub.confidence_report import (
    CLOSE_FAMILY,
    FAMILIES,
    INDEX_MEMBERSHIP_FAMILY,
    INDEX_MEMBERSHIP_POLICY,
    Band,
    OriginValue,
    aggregate,
    classify_cell,
    content_address,
    family_policy,
    persist,
    sec_oracle_section,
    stored_confidence_metadata,
)
from data_engine.datahub.production_topt import source_registrations as registry
from data_engine.datahub.quality_report import (
    FIELD_RECONCILIATION_POLICIES,
    FIELD_UNITS,
    FINANCIAL_FACT_FUSION_FIELDS,
    FINANCIAL_FACT_RECONCILIATION_POLICY,
    PRICE_BAR_FIELDS,
    RECONCILIATION_POLICY,
    VOLUME_RECONCILIATION_POLICY,
)
from truealpha_contracts.reconciliation import ReconciliationOutcome

CUTOFF = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)
DAY = datetime(2026, 9, 14, 0, 0, tzinfo=UTC)


def _yahoo(value: str | None, at: datetime = DAY) -> OriginValue:
    return OriginValue(
        origin_id="origin:yahoo:v1", source_id="yahoo-chart:v1", lineage="yahoo", value=value, knowable_at=at
    )


def _twelve(value: str | None, at: datetime = DAY) -> OriginValue:
    return OriginValue(
        origin_id="origin:twelve-data:v1",
        source_id="twelve-data:v1",
        lineage="twelve-data",
        value=value,
        knowable_at=at,
    )


# -- the four bands, each driven by its own synthetic cell ----------------------------------


def test_two_independent_origins_agreeing_within_tolerance_are_high() -> None:
    # AAPL on the 2026-09-14 QQQ head: 333.08 (yahoo) vs 333.079987 (twelve-data).
    grade = classify_cell(
        family_policy(CLOSE_FAMILY), "listing:xnas:aapl", (_yahoo("333.08"), _twelve("333.079987")), CUTOFF
    )
    assert grade.band is Band.HIGH
    assert grade.outcome == ReconciliationOutcome.AGREED.value
    assert grade.independent_origins == 2
    assert grade.tolerance == RECONCILIATION_POLICY.policy_id
    assert grade.delta == "0.000013" and Decimal(grade.relative_delta) < Decimal("0.000001")


def test_two_independent_origins_disagreeing_beyond_tolerance_are_medium() -> None:
    # 1% apart: outside the deployed 30bp policy — present, not agreed.
    grade = classify_cell(family_policy(CLOSE_FAMILY), "listing:xnas:bad", (_yahoo("300"), _twelve("303")), CUTOFF)
    assert grade.band is Band.MEDIUM
    assert grade.reason == "not_agreed_within_tolerance"
    assert grade.outcome == ReconciliationOutcome.CONFLICT_ABSTAINED.value
    assert grade.delta == "3" and grade.relative_delta == "0.009901"


def test_two_origins_with_no_agreement_policy_are_medium() -> None:
    """Multiple sources present, nothing declared to compare them: MEDIUM by the standard,
    never HIGH by accident. Shares outstanding is a fundamental no policy fuses (#866 fuses
    revenue, gross profit, net income and total assets)."""
    shares = family_policy("shares_outstanding")
    assert shares.reconciliation is None
    sec = OriginValue("origin:sec-company-facts:v1", "sec-company-facts:v1", "sec-company-facts", "15000000000")
    other = OriginValue("origin:moomoo:v1", "moomoo:v1", "moomoo", "15000000000")
    grade = classify_cell(shares, "listing:xnas:aapl", (sec, other), CUTOFF)
    assert grade.band is Band.MEDIUM and grade.reason == "no_agreement_policy"


def test_a_single_origin_is_low() -> None:
    grade = classify_cell(
        family_policy("revenue"),
        "listing:xnas:aapl",
        (OriginValue("origin:sec-company-facts:v1", "sec-company-facts:v1", "sec-company-facts", "416161000000"),),
        CUTOFF,
    )
    assert grade.band is Band.LOW and grade.reason == "single_origin"
    assert grade.origins == ("origin:sec-company-facts:v1",)


def test_no_origin_value_is_missing() -> None:
    grade = classify_cell(family_policy(CLOSE_FAMILY), "listing:xnas:x", (_yahoo(None), _twelve(None)), CUTOFF)
    assert grade.band is Band.MISSING and grade.reason == "no_origin_value"
    assert grade.origins == ()
    assert classify_cell(family_policy("revenue"), "listing:xnas:x", (), CUTOFF).band is Band.MISSING


# -- the independence rule -----------------------------------------------------------------


def test_two_producers_of_one_lineage_are_not_two_sources() -> None:
    """The headcount plane has two producers (10-K extraction and a reviewed manual entry)
    that both read the issuer's 10-K. Equal values from one lineage are MEDIUM at most —
    a mirror never corroborates its original (docs/datahub-quality-report.md step 3)."""
    extracted = OriginValue("origin:headcount:10k-extraction", "10k-extraction", "sec-10k", "166000")
    reviewed = OriginValue("origin:headcount:manual-review", "manual-review", "sec-10k", "166000")
    grade = classify_cell(family_policy("headcount"), "listing:xnas:aapl", (extracted, reviewed), CUTOFF)
    assert grade.band is Band.MEDIUM and grade.reason == "same_lineage"
    assert grade.independent_origins == 1
    assert grade.origins == ("origin:headcount:10k-extraction", "origin:headcount:manual-review")


def test_a_second_origin_from_another_day_does_not_count_for_the_served_day() -> None:
    """#622's shape: Yahoo fell back to Friday, Twelve Data published Monday. The served day
    has one origin; the pair is not a conflict and not a corroboration."""
    friday, monday = datetime(2026, 8, 14, tzinfo=UTC), datetime(2026, 8, 17, tzinfo=UTC)
    grade = classify_cell(
        family_policy(CLOSE_FAMILY), "listing:xnas:hon", (_yahoo("233.96", friday), _twelve("229.45", monday)), CUTOFF
    )
    assert grade.band is Band.LOW and grade.reason == "second_origin_other_day"
    assert grade.origins == ("origin:yahoo:v1",)


def test_a_dated_primary_row_without_a_value_does_not_move_the_served_day() -> None:
    """A primary row from Yahoo's overnight null-close window is dated but asserts nothing;
    the served day is the day an origin actually priced, so the other origin's value is
    graded rather than excluded behind an empty anchor."""
    friday, monday = datetime(2026, 8, 14, tzinfo=UTC), datetime(2026, 8, 17, tzinfo=UTC)
    grade = classify_cell(
        family_policy(CLOSE_FAMILY), "listing:xnas:hon", (_yahoo(None, monday), _twelve("229.45", friday)), CUTOFF
    )
    assert grade.band is Band.LOW and grade.reason == "single_origin"
    assert grade.origins == ("origin:twelve-data:v1",) and grade.excluded == ()


# -- every field of the bar is its own family (#865) -------------------------------------------

_YAHOO = ("yahoo-chart:v1", "origin:yahoo:v1", "close")
_TWELVE = ("twelve-data:v1", "origin:twelve-data:v1", "close")
# The settled 2026-08-14 AAPL bars of the cassette pair (test_quality_report): every field agrees.
_YAHOO_BAR = {"open": "306.00", "high": "307.49", "low": "304.30", "close": "305.93", "volume": "28186700"}
_TWELVE_BAR = {"open": "306", "high": "307.48999", "low": "304.29999", "close": "305.92999", "volume": "28186700"}
_QUALITY_REPORT_ID = "datahub-quality-report:" + "c" * 64


def _bar_grades(*bars: tuple[tuple[str, str, str], dict]) -> dict[str, cr.CellGrade]:
    """One listing's grade per bar family, from what each origin's payload carries."""
    origins: dict[str, list[OriginValue]] = {}
    for coordinate, payload in bars:
        for family, origin in cr.bar_origins(coordinate, payload, knowable_at=DAY).items():
            origins.setdefault(family, []).append(origin)
    return {name: classify_cell(family_policy(name), "listing:xnas:aapl", origins[name], CUTOFF) for name in origins}


def test_every_bar_field_is_its_own_family_under_the_quality_reports_policy() -> None:
    """open/high/low/close under the price policy, volume under its own, each in the unit the
    quality report's cell declares, all session-bound like the close: a HIGH here is what
    `field_reconciliation[<field>]` calls agreed."""
    assert [policy.family for policy in FAMILIES[: len(PRICE_BAR_FIELDS)]] == list(PRICE_BAR_FIELDS)
    for name in PRICE_BAR_FIELDS:
        policy = family_policy(name)
        assert policy.semantic_type == "market-price" and policy.session_bound, name
        assert policy.reconciliation is FIELD_RECONCILIATION_POLICIES[name], name
        assert policy.unit == FIELD_UNITS[name], name
        if name != CLOSE_FAMILY:
            assert policy.value_keys == (name,), name
    assert family_policy("volume").reconciliation is VOLUME_RECONCILIATION_POLICY
    assert family_policy("volume").unit == "shares" and family_policy("open").unit == "USD"


def test_a_two_origin_agreed_bar_is_high_on_all_five_fields() -> None:
    grades = _bar_grades((_YAHOO, _YAHOO_BAR), (_TWELVE, _TWELVE_BAR))
    assert set(grades) == set(PRICE_BAR_FIELDS)
    assert {name: grade.band for name, grade in grades.items()} == dict.fromkeys(PRICE_BAR_FIELDS, Band.HIGH)
    assert all(
        grade.reason == "independent_origins_agree" and grade.independent_origins == 2 for grade in grades.values()
    )
    assert grades["volume"].tolerance == VOLUME_RECONCILIATION_POLICY.policy_id
    assert grades["open"].tolerance == RECONCILIATION_POLICY.policy_id
    assert grades["volume"].delta == "0" and grades["high"].delta == "0.00001"


def test_a_close_only_second_origin_corroborates_the_close_alone() -> None:
    """A Twelve Data v2 observation carries the close and no bar keys: it asserts nothing for
    open/high/low/volume — present with no value, never a conflict — so those families are
    honestly single-origin while the close is HIGH."""
    grades = _bar_grades((_YAHOO, _YAHOO_BAR), (_TWELVE, {"close": "305.92999"}))
    assert grades[CLOSE_FAMILY].band is Band.HIGH
    for name in ("open", "high", "low", "volume"):
        assert grades[name].band is Band.LOW and grades[name].reason == "single_origin", name
        assert grades[name].origins == ("origin:yahoo:v1",) and grades[name].outcome is None, name
        assert grades[name].values == {"origin:twelve-data:v1": None, "origin:yahoo:v1": _YAHOO_BAR[name]}, name


def test_a_volume_conflict_is_the_volume_familys_finding_alone() -> None:
    """A primary-listing-only count (roughly half the consolidated tape) disagrees on volume
    under the 2% policy while every price field, the close included, stays HIGH."""
    grades = _bar_grades((_YAHOO, _YAHOO_BAR), (_TWELVE, {**_TWELVE_BAR, "volume": "14500000"}))
    volume = grades["volume"]
    assert volume.band is Band.MEDIUM and volume.reason == "not_agreed_within_tolerance"
    assert volume.outcome == ReconciliationOutcome.CONFLICT_ABSTAINED.value
    assert volume.tolerance == VOLUME_RECONCILIATION_POLICY.policy_id
    assert volume.delta == "13686700" and volume.relative_delta == "0.485573"
    prices = ("open", "high", "low", "close")
    assert {name: grades[name].band for name in prices} == dict.fromkeys(prices, Band.HIGH)


def test_a_bar_without_a_close_asserts_nothing_and_the_v1_close_is_read_under_price() -> None:
    """The quality report skips an observation whose close is null; this report asserts no
    field from it either, so the two grade the same assertions. The v1 second origin wrote
    its close under `price`, and the registry's value key is what reads it."""
    nulled = cr.bar_origins(_YAHOO, {**_YAHOO_BAR, "close": None}, knowable_at=DAY)
    assert set(nulled) == set(PRICE_BAR_FIELDS) and all(origin.value is None for origin in nulled.values())
    v1 = cr.bar_origins(("twelve-data:v1", "origin:twelve-data:v1", "price"), {"price": "305.92999", "open": "306"})
    assert v1[CLOSE_FAMILY].value == "305.92999" and v1["open"].value == "306" and v1["volume"].value is None
    assert all(origin.lineage == "twelve-data" and origin.source_id == "twelve-data:v1" for origin in v1.values())


def test_accuracy_compares_each_bar_field_with_the_quality_reports_grade_of_that_field() -> None:
    """The cross-check reads `reconciliation_cells[*].fields[<field>].outcome` (#850) per field:
    a volume conflict the quality report also recorded matches; a persisted `agreed` this
    report graded otherwise — or never compared at all — is a mismatch for that field alone."""
    grades = _bar_grades((_YAHOO, _YAHOO_BAR), (_TWELVE, {**_TWELVE_BAR, "volume": "14500000"}))
    fields = {name: {"outcome": grades[name].outcome, "origin_groups": 2} for name in PRICE_BAR_FIELDS}
    persisted = cr.quality_report_field_outcomes(
        {"reconciliation_cells": {"listing:xnas:aapl": {"outcome": "agreed", "fields": fields}}}
    )
    assert persisted["volume"] == {"listing:xnas:aapl": "conflict_abstained"}
    assert persisted["close"] == {"listing:xnas:aapl": "agreed"}
    cells = {name: {"listing:xnas:aapl": grades[name]} for name in PRICE_BAR_FIELDS}
    entries = {
        name: cr.field_accuracy(
            family_policy(name),
            aggregate(family_policy(name), cells[name].values()),
            cells[name],
            quality_report_id=_QUALITY_REPORT_ID,
            persisted=persisted[name],
        )
        for name in PRICE_BAR_FIELDS
    }
    for name, entry in entries.items():
        assert entry["matches_quality_report"] is True and entry["quality_report_mismatches"] == [], name
        assert entry["quality_report_id"] == _QUALITY_REPORT_ID and entry["quality_report_cells"] == 1
        assert entry["tolerance_policy"]["policy_id"] == FIELD_RECONCILIATION_POLICIES[name].policy_id
        assert entry["origins"] == ["origin:twelve-data:v1", "origin:yahoo:v1"] and entry["compared"] == 1
    assert (entries["close"]["agreed"], entries["volume"]["agreed"]) == (1, 0)
    assert (entries["close"]["agreement_rate"], entries["volume"]["agreement_rate"]) == ("1.0000", "0.0000")
    # The quality report claiming the volume agreed when this report graded a conflict.
    claimed = cr.field_accuracy(
        family_policy("volume"),
        {},
        cells["volume"],
        quality_report_id=_QUALITY_REPORT_ID,
        persisted={"listing:xnas:aapl": "agreed"},
    )
    assert claimed["matches_quality_report"] is False and claimed["quality_report_mismatches"] == ["listing:xnas:aapl"]
    # A persisted comparison over a cell this report saw one origin for is a mismatch too;
    # the quality report's own single-origin grade for it is not.
    single = _bar_grades((_YAHOO, _YAHOO_BAR))["open"]
    assert single.band is Band.LOW and single.outcome is None
    for outcome, matches in (
        ("agreed", False),
        ("conflict_abstained", False),
        ("insufficient_independent_origins", True),
    ):
        entry = cr.field_accuracy(
            family_policy("open"),
            {},
            {"listing:xnas:aapl": single},
            quality_report_id=_QUALITY_REPORT_ID,
            persisted={"listing:xnas:aapl": outcome},
        )
        assert entry["matches_quality_report"] is matches, outcome


def test_a_quality_report_from_before_per_field_fusion_is_compared_on_the_close_alone() -> None:
    """Reports persisted before #850 carry the close's grade under the headline keys and no
    `fields`: the close is still cross-checked, and the other fields report nothing to
    compare rather than a vacuous match."""
    persisted = cr.quality_report_field_outcomes({"reconciliation_cells": {"listing:xnas:aapl": {"outcome": "agreed"}}})
    assert persisted["close"] == {"listing:xnas:aapl": "agreed"}
    assert all(persisted[name] == {} for name in ("open", "high", "low", "volume"))
    grades = _bar_grades((_YAHOO, _YAHOO_BAR), (_TWELVE, _TWELVE_BAR))
    entry = cr.field_accuracy(
        family_policy("open"),
        {},
        {"listing:xnas:aapl": grades["open"]},
        quality_report_id=_QUALITY_REPORT_ID,
        persisted=persisted["open"],
    )
    assert entry["matches_quality_report"] is None and entry["quality_report_cells"] == 0
    none = cr.field_accuracy(family_policy("close"), {}, {}, quality_report_id=None, persisted={})
    assert none["matches_quality_report"] is None and none["quality_report_id"] is None
    assert cr.quality_report_field_outcomes({}) == {name: {} for name in cr.CROSS_CHECKED_FAMILIES}


# -- the fused fundamentals reconcile under the quality report's policy (#866) ----------------

_SEC = ("sec-company-facts:v1", "origin:sec-company-facts:v1")
_MOOMOO = ("moomoo-financials:v1", "origin:moomoo-financials:v1")
FILED = datetime(2026, 2, 18, tzinfo=UTC)
# The fixture pair of test_quality_report's financial fusion: every dated field agrees at
# 2025-12-31, net income by the measured 0.67% ProfitLoss-vs-NetIncomeLoss gap, inside 1%.
_PRIMARY_FACT = {
    "revenue": "100000000",
    "revenue_period_end": "2025-12-31",
    "gross_profit": "40000000",
    "operating_period_end": "2025-12-31",
    "net_income": "9000000",
    "total_assets": "500000000",
    "vintage": {"net_income": {"period_end": "2025-12-31"}, "total_assets": {"period_end": "2025-12-31"}},
}
_MOOMOO_FACT = {
    "origin": "moomoo-financials",
    "period_end": "2025-12-31",
    "revenue": "100000000",
    "gross_profit": "40000000",
    "net_income": "9060000",
    "total_assets": "500000000",
    "by_period_end": {
        "2024-12-31": {"revenue": "80000000", "gross_profit": "30000000", "net_income": "5000000"},
        "2025-12-31": {
            "revenue": "100000000",
            "gross_profit": "40000000",
            "net_income": "9060000",
            "total_assets": "500000000",
        },
    },
}


def _financial(coordinate: tuple[str, str], payload: dict, *, primary: bool | None = None) -> cr.FinancialObservation:
    source, origin = coordinate
    return cr.FinancialObservation(
        primary=coordinate is _SEC if primary is None else primary,
        origin_source=source,
        origin_id=origin,
        payload=payload,
        knowable_at=FILED,
        observation_id="normalized-observation:" + hashlib.sha256(origin.encode()).hexdigest(),
    )


def _financial_grades(*observations: cr.FinancialObservation) -> dict[str, cr.CellGrade]:
    """One issuer's grade per financial family from what its observations carry."""
    origins = cr.financial_origins(observations)
    return {name: classify_cell(family_policy(name), "listing:xnas:t", origins[name], CUTOFF) for name in origins}


def test_the_fused_fundamentals_carry_the_quality_reports_policy_and_the_others_none() -> None:
    assert set(FINANCIAL_FACT_FUSION_FIELDS) == {"revenue", "gross_profit", "net_income", "total_assets"}
    for name in FINANCIAL_FACT_FUSION_FIELDS:
        policy = family_policy(name)
        assert policy.reconciliation is FINANCIAL_FACT_RECONCILIATION_POLICY and policy.period_bound, name
        assert policy.semantic_type == "financial-fact" and not policy.session_bound, name
    for name in ("headcount", "pre_provision_profit", "shares_outstanding"):
        assert family_policy(name).reconciliation is None and not family_policy(name).period_bound, name
    assert cr.CROSS_CHECKED_FAMILIES == (*PRICE_BAR_FIELDS, *FINANCIAL_FACT_FUSION_FIELDS)


def test_two_lineages_agreeing_at_the_primarys_period_are_high() -> None:
    """SEC company-facts and moomoo's statements agree on every fused field at the primary's
    fiscal period end: HIGH under `financial-fact-fusion:v1`, the net income gap inside 1%."""
    grades = _financial_grades(_financial(_SEC, _PRIMARY_FACT), _financial(_MOOMOO, _MOOMOO_FACT))
    for name in FINANCIAL_FACT_FUSION_FIELDS:
        grade = grades[name]
        assert grade.band is Band.HIGH and grade.reason == "independent_origins_agree", name
        assert grade.outcome == ReconciliationOutcome.AGREED.value and grade.independent_origins == 2, name
        assert grade.tolerance == FINANCIAL_FACT_RECONCILIATION_POLICY.policy_id, name
        assert grade.origins == ("origin:moomoo-financials:v1", "origin:sec-company-facts:v1"), name
        assert grade.excluded == (), name
    assert grades["net_income"].values == {
        "origin:moomoo-financials:v1": "9060000",
        "origin:sec-company-facts:v1": "9000000",
    }
    assert grades["net_income"].delta == "60000" and grades["net_income"].relative_delta == "0.006623"
    # The fields no policy fuses are unchanged: the statements origin asserts none of them.
    assert grades["shares_outstanding"].band is Band.MISSING
    assert grades["pre_provision_profit"].band is Band.MISSING


def test_two_lineages_disagreeing_beyond_tolerance_are_medium() -> None:
    conflicting = {
        **_MOOMOO_FACT,
        "by_period_end": {"2025-12-31": {**_MOOMOO_FACT["by_period_end"]["2025-12-31"], "revenue": "103000000"}},
    }
    grades = _financial_grades(_financial(_SEC, _PRIMARY_FACT), _financial(_MOOMOO, conflicting))
    revenue = grades["revenue"]
    assert revenue.band is Band.MEDIUM and revenue.reason == "not_agreed_within_tolerance"
    assert revenue.outcome == ReconciliationOutcome.CONFLICT_ABSTAINED.value
    assert revenue.delta == "3000000" and revenue.relative_delta == "0.029126"
    assert grades["gross_profit"].band is Band.HIGH, "one field's disagreement is that field's finding"


def test_a_second_origin_without_the_primarys_period_is_low_other_period() -> None:
    """The financial analogue of #622: a vendor that has not published the primary's fiscal
    period has not corroborated it and has not disagreed with it. Its newest figure is
    recorded, dated, and excluded — never compared."""
    stale = {**_MOOMOO_FACT, "by_period_end": {"2024-12-31": _MOOMOO_FACT["by_period_end"]["2024-12-31"]}}
    grades = _financial_grades(_financial(_SEC, _PRIMARY_FACT), _financial(_MOOMOO, stale))
    for name in ("revenue", "gross_profit", "net_income"):
        grade = grades[name]
        assert grade.band is Band.LOW and grade.reason == "second_origin_other_period", name
        assert grade.origins == ("origin:sec-company-facts:v1",) and grade.outcome is None, name
        assert grade.excluded == ("origin:moomoo-financials:v1",), name
    assert grades["revenue"].values == {
        "origin:moomoo-financials:v1": "80000000",
        "origin:sec-company-facts:v1": "100000000",
    }
    # The stale vintage never carried total assets at all: nothing to exclude, plain single origin.
    assert grades["total_assets"].band is Band.LOW and grades["total_assets"].reason == "single_origin"
    assert grades["total_assets"].excluded == ()


def test_a_single_financial_origin_is_low_whichever_origin_it_is() -> None:
    primary_only = _financial_grades(_financial(_SEC, _PRIMARY_FACT))
    assert all(primary_only[name].band is Band.LOW for name in FINANCIAL_FACT_FUSION_FIELDS)
    assert all(primary_only[name].reason == "single_origin" for name in FINANCIAL_FACT_FUSION_FIELDS)
    # Without a primary there is no period to align on: the statements origin's headline
    # figure covers the field alone, as the quality report's `unavailable` says.
    second_only = _financial_grades(_financial(_MOOMOO, _MOOMOO_FACT))
    assert second_only["revenue"].band is Band.LOW and second_only["revenue"].reason == "single_origin"
    assert second_only["revenue"].origins == ("origin:moomoo-financials:v1",)
    assert second_only["revenue"].values == {"origin:moomoo-financials:v1": "100000000"}


def test_a_second_origin_in_another_currency_never_corroborates() -> None:
    """The cell's unit is the primary's reporting currency: a figure in another currency is
    not the same number, so the origin is present with no comparable value. The same
    currency on both sides compares as usual; a payload without one reads as USD."""
    eur_primary = {**_PRIMARY_FACT, "currency": "EUR"}
    mismatched = _financial_grades(
        _financial(_SEC, eur_primary), _financial(_MOOMOO, {**_MOOMOO_FACT, "currency": "HKD"})
    )
    for name in FINANCIAL_FACT_FUSION_FIELDS:
        assert mismatched[name].band is Band.LOW and mismatched[name].reason == "single_origin", name
        assert mismatched[name].values["origin:moomoo-financials:v1"] is None, name
    agreed = _financial_grades(_financial(_SEC, eur_primary), _financial(_MOOMOO, {**_MOOMOO_FACT, "currency": "EUR"}))
    assert all(agreed[name].band is Band.HIGH for name in FINANCIAL_FACT_FUSION_FIELDS)
    legacy = _financial_grades(
        _financial(_SEC, _PRIMARY_FACT), _financial(_MOOMOO, {**_MOOMOO_FACT, "currency": "USD"})
    )
    assert all(legacy[name].band is Band.HIGH for name in FINANCIAL_FACT_FUSION_FIELDS)


def test_an_undated_primary_figure_is_not_corroborated_and_an_absent_one_leaves_the_second_alone() -> None:
    """A primary figure without a fiscal period cannot be aligned, so nothing corroborates it
    (the quality report compares it no more); a field the primary does not assert at all
    is covered by the second origin's headline figure alone."""
    undated = {name: value for name, value in _PRIMARY_FACT.items() if name != "vintage"}
    grades = _financial_grades(_financial(_SEC, undated), _financial(_MOOMOO, _MOOMOO_FACT))
    assert grades["revenue"].band is Band.HIGH and grades["gross_profit"].band is Band.HIGH
    for name in ("net_income", "total_assets"):
        assert grades[name].band is Band.LOW and grades[name].reason == "single_origin", name
        assert grades[name].values["origin:moomoo-financials:v1"] is None, name
    absent = _financial_grades(
        _financial(_SEC, {**_PRIMARY_FACT, "net_income": None}), _financial(_MOOMOO, _MOOMOO_FACT)
    )
    assert absent["net_income"].band is Band.LOW and absent["net_income"].origins == ("origin:moomoo-financials:v1",)
    assert absent["net_income"].values["origin:moomoo-financials:v1"] == "9060000"


def test_two_readers_of_one_lineage_stay_medium_under_the_financial_policy() -> None:
    """The independence rule survives the policy: a mirror of the SEC facts agreeing to the
    cent is one source, MEDIUM at most, never HIGH."""
    sec = OriginValue(
        "origin:sec-company-facts:v1",
        "sec-company-facts:v1",
        "sec-company-facts",
        "100000000",
        period_end=date(2025, 12, 31),
    )
    mirror = OriginValue(
        "origin:sec-mirror:v1", "sec-mirror:v1", "sec-company-facts", "100000000", period_end=date(2025, 12, 31)
    )
    grade = classify_cell(family_policy("revenue"), "listing:xnas:t", (sec, mirror), CUTOFF)
    assert grade.band is Band.MEDIUM and grade.reason == "same_lineage" and grade.independent_origins == 1


def test_accuracy_compares_each_fused_fundamental_with_the_quality_reports_grade() -> None:
    """The cross-check reads `financial_fact_reconciliation_cells[*].fields[<field>].outcome`
    (#854) per field; a subject the quality report graded no field for (no primary, or an
    undated one) is not compared."""
    grades = _financial_grades(_financial(_SEC, _PRIMARY_FACT), _financial(_MOOMOO, _MOOMOO_FACT))
    fields = {
        name: {"outcome": "agreed", "period_end": "2025-12-31", "unit": "USD"} for name in FINANCIAL_FACT_FUSION_FIELDS
    }
    persisted = cr.quality_report_field_outcomes(
        {
            "financial_fact_reconciliation_cells": {
                "listing:xnas:t": {"outcome": "agreed", "fields": fields},
                "listing:xnas:u": {"outcome": "unavailable", "fields": {}, "origin_groups": 0},
            }
        }
    )
    assert all(persisted[name] == {"listing:xnas:t": "agreed"} for name in FINANCIAL_FACT_FUSION_FIELDS)
    assert all(persisted[name] == {} for name in PRICE_BAR_FIELDS)
    for name in FINANCIAL_FACT_FUSION_FIELDS:
        cells = {"listing:xnas:t": grades[name]}
        entry = cr.field_accuracy(
            family_policy(name),
            aggregate(family_policy(name), cells.values()),
            cells,
            quality_report_id=_QUALITY_REPORT_ID,
            persisted=persisted[name],
        )
        assert entry["matches_quality_report"] is True and entry["quality_report_cells"] == 1, name
        assert entry["tolerance_policy"]["policy_id"] == FINANCIAL_FACT_RECONCILIATION_POLICY.policy_id, name
        assert (entry["compared"], entry["agreed"], entry["agreement_rate"]) == (1, 1, "1.0000"), name
    conflicting = _financial_grades(
        _financial(_SEC, _PRIMARY_FACT),
        _financial(_MOOMOO, {**_MOOMOO_FACT, "by_period_end": {"2025-12-31": {"revenue": "103000000"}}}),
    )
    claimed = cr.field_accuracy(
        family_policy("revenue"),
        {},
        {"listing:xnas:t": conflicting["revenue"]},
        quality_report_id=_QUALITY_REPORT_ID,
        persisted={"listing:xnas:t": "agreed"},
    )
    assert claimed["matches_quality_report"] is False and claimed["quality_report_mismatches"] == ["listing:xnas:t"]


# -- index membership: the policy this report adds ------------------------------------------


def _route(origin: str, value: str | None) -> OriginValue:
    source, lineage = {
        "origin:nasdaq-index:v1": ("nasdaq-index:v1", "nasdaq-index"),
        "origin:nport:v1": ("nport:v1", "sec-nport"),
    }[origin]
    return OriginValue(origin, source, lineage, value)


def test_each_family_declares_its_own_unit_for_the_cell_identity() -> None:
    """The unit is part of the content-addressed reconciliation cell: a close is dollars, a
    fund weight is a percent of net assets, membership is presence — never USD for all."""
    assert family_policy(CLOSE_FAMILY).unit == "USD"
    assert family_policy(cr.ETF_WEIGHT_FAMILY).unit == "percent_of_net_assets"
    assert family_policy(INDEX_MEMBERSHIP_FAMILY).unit == "membership"
    assert all(policy.unit for policy in cr.FAMILIES)


def test_both_moomoo_origins_share_one_vendor_lineage() -> None:
    """K-line and statements come from one vendor: independent of Yahoo/Twelve Data and of
    SEC, but one connected source, so the roll-up counts moomoo once."""
    assert cr.lineage_of("origin:moomoo-kline:v1") == "moomoo"
    assert cr.lineage_of("origin:moomoo-financials:v1") == "moomoo"
    assert cr.lineage_of("origin:twelve-data:v1") == "twelve-data"


def test_index_membership_listed_by_both_routes_is_high() -> None:
    """Membership is presence: the operator lists the name and the fund files it held."""
    grade = classify_cell(
        family_policy(INDEX_MEMBERSHIP_FAMILY),
        "listing:xnas:nvda",
        (_route("origin:nasdaq-index:v1", cr.MEMBER), _route("origin:nport:v1", cr.MEMBER)),
        CUTOFF,
    )
    assert grade.band is Band.HIGH and grade.outcome == ReconciliationOutcome.AGREED.value
    assert grade.tolerance == INDEX_MEMBERSHIP_POLICY.policy_id
    assert grade.comparison == "membership"


def test_etf_weight_is_low_while_only_the_fund_files_one() -> None:
    """The operator route carries no weight today (`staging.etf_constituent_facts.weight` is
    NULL by design): membership can be HIGH while the weight is honestly single-origin."""
    grade = classify_cell(
        family_policy(cr.ETF_WEIGHT_FAMILY),
        "listing:xnas:nvda",
        (_route("origin:nasdaq-index:v1", None), _route("origin:nport:v1", "7.596756593966")),
        CUTOFF,
    )
    assert grade.band is Band.LOW and grade.reason == "single_origin"
    assert grade.origins == ("origin:nport:v1",)


def test_etf_weights_are_compared_at_the_stated_tolerance_when_both_routes_carry_one() -> None:
    within = classify_cell(
        family_policy(cr.ETF_WEIGHT_FAMILY),
        "listing:xnas:nvda",
        (_route("origin:nasdaq-index:v1", "7.70"), _route("origin:nport:v1", "7.596756593966")),
        CUTOFF,
    )
    assert within.band is Band.HIGH and within.comparison == "numeric"
    assert within.tolerance == INDEX_MEMBERSHIP_POLICY.policy_id
    apart = classify_cell(
        family_policy(cr.ETF_WEIGHT_FAMILY),
        "listing:xnas:nvda",
        (_route("origin:nasdaq-index:v1", "0.30"), _route("origin:nport:v1", "7.596756593966")),
        CUTOFF,
    )
    assert apart.band is Band.MEDIUM and apart.reason == "not_agreed_within_tolerance"


def test_a_listing_in_one_route_only_is_low() -> None:
    grade = classify_cell(
        family_policy(INDEX_MEMBERSHIP_FAMILY),
        "listing:xnas:new",
        (_route("origin:nasdaq-index:v1", cr.MEMBER),),
        CUTOFF,
    )
    assert grade.band is Band.LOW and grade.reason == "single_origin"


# -- aggregation -------------------------------------------------------------------------------


def test_aggregate_counts_shares_and_agreement_rate_over_compared_cells() -> None:
    policy = family_policy(CLOSE_FAMILY)
    grades = [
        classify_cell(policy, "listing:xnas:a", (_yahoo("100"), _twelve("100.1")), CUTOFF),  # high
        classify_cell(policy, "listing:xnas:b", (_yahoo("100"), _twelve("103")), CUTOFF),  # medium
        classify_cell(policy, "listing:xnas:c", (_yahoo("100"),), CUTOFF),  # low
        classify_cell(policy, "listing:xnas:d", (_yahoo(None),), CUTOFF),  # missing
    ]
    summary = aggregate(policy, grades)
    assert summary["cells"] == 4
    assert (summary["high"], summary["medium"], summary["low"], summary["missing"]) == (1, 1, 1, 1)
    assert summary["share"] == {"high": "0.2500", "medium": "0.2500", "low": "0.2500", "missing": "0.2500"}
    # Agreement is judged only where two independent origins were compared: 1 of 2.
    assert summary["compared"] == 2 and summary["agreement_rate"] == "0.5000"
    assert summary["origins"] == ["origin:twelve-data:v1", "origin:yahoo:v1"]
    assert summary["tolerance"] == RECONCILIATION_POLICY.policy_id
    assert summary["reasons"] == {
        "independent_origins_agree": 1,
        "no_origin_value": 1,
        "not_agreed_within_tolerance": 1,
        "single_origin": 1,
    }


def test_aggregate_with_nothing_compared_has_no_agreement_rate() -> None:
    policy = family_policy("shares_outstanding")
    sec = OriginValue("origin:sec-company-facts:v1", "sec-company-facts:v1", "sec-company-facts", "1")
    summary = aggregate(policy, [classify_cell(policy, "listing:xnas:a", (sec,), CUTOFF)])
    assert summary["compared"] == 0 and summary["agreement_rate"] is None and summary["tolerance"] is None


# -- the families are the registry's semantics -----------------------------------------------


def test_every_family_names_a_registered_semantic_or_the_membership_plane() -> None:
    registered = set(registry.registered_semantic_types())
    for family in FAMILIES:
        assert family.semantic_type in registered or family.semantic_type == cr.INDEX_MEMBERSHIP_SEMANTIC, family


def test_every_registered_semantic_with_a_value_is_graded() -> None:
    """A registered origin declares the payload key that carries its value; the family
    graded under that semantic must read exactly that key. Identity semantics carry no
    measured value and are deliberately not families."""
    families_by_semantic: dict[str, set[str]] = {}
    for family in FAMILIES:
        families_by_semantic.setdefault(family.semantic_type, set()).add(family.family)
    for registration in registry.REGISTRATIONS:
        for semantic in registration.semantic_types:
            if semantic in registry.RELEASE_SEMANTICS:
                assert semantic not in families_by_semantic
                continue
            assert semantic in families_by_semantic, f"{semantic} is registered but no family grades it"
    close = family_policy(CLOSE_FAMILY)
    assert close.semantic_type == "market-price"
    assert set(close.value_keys) == {origin.value_key for origin in registry.registration_for("market-price").origins}


def test_close_origins_are_the_registrations_origins_and_the_deployed_policy() -> None:
    close = family_policy(CLOSE_FAMILY)
    assert close.reconciliation is RECONCILIATION_POLICY
    assert set(cr.CLOSE_ORIGINS) == {origin.origin_id for origin in registry.registration_for("market-price").origins}
    assert set(cr.CLOSE_ORIGINS.values()) <= set(registry.source_by_parser().values())


# -- content addressing --------------------------------------------------------------------------


def test_the_report_is_content_addressed_like_the_quality_report() -> None:
    payload = {"b": 1, "a": {"y": "2", "x": [3]}}
    report_id, digest = content_address(payload)
    assert report_id == f"datahub-confidence-report:{digest}" and len(digest) == 64
    assert content_address({"a": {"x": [3], "y": "2"}, "b": 1}) == (report_id, digest)
    assert content_address({**payload, "b": 2})[1] != digest


# -- metadata: the stored confidence column is a constant and is not used ---------------------


def test_stored_confidence_is_reported_as_measured_and_marked_unused() -> None:
    rows = [
        ("market-price", Decimal("0.85"), 204),
        ("financial-fact", Decimal("0.92"), 102),
        ("listing-identity", Decimal("1.0"), 102),
    ]
    metadata = stored_confidence_metadata(rows)
    assert metadata["used_for_bands"] is False
    assert metadata["values_by_semantic"] == {
        "financial-fact": {"0.92": 102},
        "listing-identity": {"1.0": 102},
        "market-price": {"0.85": 204},
    }
    assert metadata["constant_per_semantic"] is True
    varied = stored_confidence_metadata(rows + [("market-price", Decimal("0.75"), 3)])
    assert varied["constant_per_semantic"] is False


# -- accuracy: the SEC oracle's independent re-derivation -----------------------------------------


def _facts(revenue: str, cogs: str, end: str = "2025-09-27") -> dict:
    row = {"end": end, "start": "2024-09-29", "filed": "2025-10-31"}
    return {
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [{**row, "val": revenue}]}},
                "CostOfGoodsAndServicesSold": {"units": {"USD": [{**row, "val": cogs}]}},
            }
        }
    }


def test_sec_oracle_reports_agreement_per_field_over_compared_issuers() -> None:
    issuers = [
        ("listing:xnas:aapl", "AAPL", Decimal("416161000000"), Decimal("195201000000")),  # agrees
        ("listing:xnas:stale", "STAL", Decimal("100"), Decimal("40")),  # revenue stale, GP agrees
        ("listing:xnas:none", "NONE", None, None),  # mart has nothing
        ("listing:xnas:unk", "UNKN", Decimal("1"), Decimal("1")),  # no CIK -> not compared
    ]
    facts = {
        1: _facts("416161000000", "220960000000"),
        2: _facts("120", "80"),
        3: _facts("5", "1"),
    }
    section = sec_oracle_section(
        issuers,
        cutoff=CUTOFF.date(),
        ticker_index=lambda: {"AAPL": 1, "STAL": 2, "NONE": 3},
        facts_for=lambda cik: facts[cik],
    )
    assert section["oracle"] == "quality.vendor_oracle"
    assert section["fields"] == ["revenue", "gross_profit"]
    assert section["issuers_requested"] == 4 and section["issuers_compared"] == 3
    assert section["skipped"] == {"listing:xnas:unk": "no_cik"}
    by_subject = {row["subject_id"]: row for row in section["rows"]}
    aapl = by_subject["listing:xnas:aapl"]
    assert aapl["revenue"]["agrees"] is True and aapl["revenue"]["vendor_value"] == "416161000000"
    assert aapl["gross_profit"]["agrees"] is True and aapl["gross_profit"]["vendor_concept"] == "Revenues-CostOfRevenue"
    stale = by_subject["listing:xnas:stale"]
    assert stale["revenue"]["agrees"] is False and stale["revenue"]["delta"] == "20"
    none = by_subject["listing:xnas:none"]
    assert none["revenue"]["agrees"] is False and none["revenue"]["mart_value"] is None
    # Agreement per field counts only cells where the vendor asserts a value.
    assert section["per_field"] == {
        "revenue": {"compared": 3, "agreed": 1, "agreement_rate": "0.3333"},
        "gross_profit": {"compared": 3, "agreed": 2, "agreement_rate": "0.6667"},
    }


def test_sec_oracle_without_a_fetcher_says_so_instead_of_guessing() -> None:
    section = sec_oracle_section([("listing:xnas:aapl", "AAPL", Decimal("1"), Decimal("1"))], cutoff=CUTOFF.date())
    assert section["issuers_compared"] == 0 and section["reason"] == "no_sec_user_agent"


# -- the deployed wiring ---------------------------------------------------------------------------


def test_the_confidence_job_is_scheduled_after_the_invariants_and_runs_per_universe() -> None:
    from data_engine.lanes import quality

    assert quality.defs.get_job_def(quality.CONFIDENCE_REPORT_JOB_NAME) is not None
    schedule = quality.defs.get_schedule_def("datahub_confidence_report_schedule")
    invariants_minute, invariants_hour = quality.OUTPUT_INVARIANTS_CRON.split()[:2]
    report_minute, report_hour = schedule.cron_schedule.split()[:2]
    assert (int(report_hour), int(report_minute)) > (int(invariants_hour), int(invariants_minute))
    tick = datetime(2026, 9, 16, 0, 45, tzinfo=UTC)
    requests = list(schedule(dg.build_schedule_context(scheduled_execution_time=tick)))
    assert [r.run_key for r in requests] == [f"{tick.isoformat()}:{u}" for u in quality.CONFIDENCE_REPORT_UNIVERSES]
    for request in requests:
        assert request.run_config["ops"]["run_confidence_report"]["config"]["executed_at"] == tick.isoformat()


def test_default_sample_subjects_are_five_qqq_names_and_five_topt_issuers() -> None:
    assert len(cr.DEFAULT_SAMPLE_SUBJECTS["universe-list:qqq"]) == 5
    assert len(cr.DEFAULT_SAMPLE_SUBJECTS["topt"]) == 5


# -- persistence (real schema; skips without a local Postgres) --------------------------------------


def _connection():
    try:
        return psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            raise
        pytest.skip("no local Postgres")


def test_the_report_persists_append_only_and_reads_back() -> None:
    connection = _connection()
    try:
        report = {
            "universe": "topt",
            "universe_id": "universe:topt-us-2026-03-31",
            "run_id": "capture-run:" + "0" * 64,
            "cutoff": CUTOFF.isoformat(),
            "families": {"close": {"high": 21}},
        }
        report_id = persist(connection, report)
        assert persist(connection, report) == report_id
        row = connection.execute(
            "select universe_id, run_id, payload->'families'->'close'->>'high' from mart.datahub_confidence_report where report_id = %s",
            (report_id,),
        ).fetchone()
        assert row == ("universe:topt-us-2026-03-31", "capture-run:" + "0" * 64, "21")
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute("delete from mart.datahub_confidence_report where report_id = %s", (report_id,))
    finally:
        connection.rollback()
        connection.close()
