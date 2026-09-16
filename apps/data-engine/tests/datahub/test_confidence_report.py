"""The datahub confidence & accuracy report (owner standard, 2026-09-15).

"HIGH confidence = multiple sources agree, MEDIUM = multiple sources present, LOW = covered
by one source." The classifier is pure and every band is driven here by a synthetic cell
shaped like the persisted observations; the independence rule (two producers of one lineage
are not two sources) has its own test because it is the rule most likely to be quietly
relaxed. The families the report grades are asserted against `source_registrations`, so a
registered semantic cannot go ungraded and the report cannot grade a semantic nothing
captures.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
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
from data_engine.datahub.quality_report import RECONCILIATION_POLICY
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
    never HIGH by accident."""
    revenue = family_policy("revenue")
    assert revenue.reconciliation is None
    sec = OriginValue("origin:sec-company-facts:v1", "sec-company-facts:v1", "sec-company-facts", "416161000000")
    other = OriginValue("origin:moomoo:v1", "moomoo:v1", "moomoo", "416161000000")
    grade = classify_cell(revenue, "listing:xnas:aapl", (sec, other), CUTOFF)
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
    policy = family_policy("revenue")
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
