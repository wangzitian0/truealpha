from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from data_engine.datahub.production_topt.executor import FetchFailure, FetchSuccess
from data_engine.datahub.production_topt.issuer_registry import (
    operating_branch_for_sic,
    revenue_proxy_allowed_for_sic,
)
from data_engine.datahub.production_topt.sec_financial_adapter import (
    FinancialFactsBundle,
    HeadcountFact,
    SecFinancialFactAdapter,
    SecTarget,
    SourceUnavailableError,
    annual_values_by_period_end,
    build_bundle,
    gross_profit,
    insurance_pre_claims_profit,
    pre_provision_profit,
)
from factors.production_topt import OperatingBranch
from truealpha_contracts import ObligationReasonCode
from truealpha_contracts.datahub import CaptureWorkItem

_CUTOFF = date(2026, 3, 31)


def _work_item(digest: str) -> CaptureWorkItem:
    return CaptureWorkItem(
        campaign_id="capture-campaign:" + "1" * 64,
        source_request_id="source-request:" + digest,
        schedule_policy_id="schedule-policy:" + "2" * 64,
    )


def _target(cik: int = 1, branch: OperatingBranch = OperatingBranch.NON_FINANCIAL) -> SecTarget:
    return SecTarget(
        cik=cik,
        cutoff=_CUTOFF,
        issuer_id="issuer:lei:X",
        instrument_id="security:cusip:Y",
        listing_id="listing:xnas:goog",
        operating_branch=branch,
    )


def _annual(end: str, start: str, val: object, filed: str) -> dict:
    return {"end": end, "start": start, "val": val, "filed": filed}


def _facts() -> dict:
    return {
        "facts": {
            "us-gaap": {
                "GrossProfit": {
                    "units": {
                        "USD": [
                            _annual("2024-12-31", "2024-01-01", 100, "2025-02-01"),
                            _annual("2025-12-31", "2025-01-01", 120, "2026-02-01"),
                            # Filed after the cutoff — must be excluded (not knowable yet).
                            _annual("2026-03-31", "2025-04-01", 999, "2026-04-20"),
                            # A quarter, not a year: must never win over the annual figure.
                            _annual("2025-12-31", "2025-10-01", 30, "2026-02-01"),
                        ]
                    }
                },
                "Assets": {"units": {"USD": [{"end": "2025-12-31", "val": 500, "filed": "2026-02-01"}]}},
                "CommonStockSharesOutstanding": {
                    "units": {"shares": [{"end": "2026-01-31", "val": 10, "filed": "2026-02-10"}]}
                },
            }
        }
    }


def test_pit_excludes_future_filings_and_quarters() -> None:
    values = annual_values_by_period_end(_facts(), "us-gaap", "GrossProfit", "USD", _CUTOFF)
    assert set(values) == {date(2024, 12, 31), date(2025, 12, 31)}  # the 2026-04-20 filing is not knowable
    assert values[date(2025, 12, 31)].value == Decimal("120")  # the 30 is a quarter, dropped
    assert values[date(2025, 12, 31)].filed == date(2026, 2, 1)


def test_build_bundle_extracts_the_core_fields() -> None:
    bundle = build_bundle(_facts(), _CUTOFF, OperatingBranch.NON_FINANCIAL)
    assert bundle.gross_profit == Decimal("120")
    assert bundle.total_assets == Decimal("500")
    assert bundle.shares_outstanding == Decimal("10")
    assert bundle.pre_provision_profit is None
    assert bundle.knowable_at is not None
    assert bundle.knowable_at.date() <= _CUTOFF


def _no_cogs_facts() -> dict:
    """An issuer tagging neither GrossProfit nor any COGS concept, but reporting revenue.

    Shared by a payment network (whose cost of revenue really is ~zero) and an oil major
    (whose cost of revenue is enormous but untagged) — the payload cannot tell them apart,
    which is the whole point of #533.
    """
    facts = _facts()
    del facts["facts"]["us-gaap"]["GrossProfit"]
    facts["facts"]["us-gaap"]["Revenues"] = {
        "units": {"USD": [_annual("2025-12-31", "2025-01-01", 332238000000, "2026-02-01")]}
    }
    return facts


def test_the_revenue_proxy_is_refused_for_an_industry_it_was_not_approved_for() -> None:
    # XOM's real shape: no GrossProfit, no COGS family, $332B revenue. Petroleum Refining
    # is not a ~zero-COGS industry, so the substitution must not happen (#533).
    item = _work_item("e" * 64)
    target = SecTarget(
        cik=2115436,
        cutoff=_CUTOFF,
        issuer_id="issuer:lei:J3WHBG0MTS7O8ZVMDC91",
        instrument_id="security:cusip:30231G102",
        listing_id="listing:xnys:xom",
        operating_branch=OperatingBranch.NON_FINANCIAL,
        revenue_proxy_allowed=False,
    )
    adapter = SecFinancialFactAdapter(
        {item.work_item_id: target}, lambda cik, cutoff, branch: build_bundle(_no_cogs_facts(), cutoff, branch)
    )
    result = adapter.fetch(item)
    assert isinstance(result, FetchSuccess)
    assert result.record.payload["gross_profit"] is None, "an oil major must not publish revenue as gross profit"
    # Revenue itself is untouched: the issuer really does report it.
    assert result.record.payload["revenue"] == "332238000000"


def test_the_revenue_proxy_still_applies_to_an_approved_industry() -> None:
    item = _work_item("f" * 64)
    target = SecTarget(
        cik=1403161,
        cutoff=_CUTOFF,
        issuer_id="issuer:lei:549300JZ4OKEHW3DPJ59",
        instrument_id="security:cusip:92826C839",
        listing_id="listing:xnys:v",
        operating_branch=OperatingBranch.NON_FINANCIAL,
        revenue_proxy_allowed=True,
    )
    adapter = SecFinancialFactAdapter(
        {item.work_item_id: target}, lambda cik, cutoff, branch: build_bundle(_no_cogs_facts(), cutoff, branch)
    )
    result = adapter.fetch(item)
    assert isinstance(result, FetchSuccess)
    assert result.record.payload["gross_profit"] == "332238000000"


def test_the_bundle_reports_whether_gross_profit_is_the_proxy() -> None:
    assert build_bundle(_no_cogs_facts(), _CUTOFF, OperatingBranch.NON_FINANCIAL).gross_profit_is_revenue_proxy
    # A real reported figure is never flagged, so the adapter cannot refuse it.
    assert not build_bundle(_facts(), _CUTOFF, OperatingBranch.NON_FINANCIAL).gross_profit_is_revenue_proxy


def _multi_class_facts(shares_end: str, shares_filed: str, shares_val: object) -> dict:
    """A multi-class issuer's shape: current profit, and the only surviving share fact old.

    company-facts drops the dimensional per-class share facts, so what remains for V and
    BRK.B is the cover-page figure from before dimensional tagging. The concept is present
    and parses, which is exactly why nothing refused it (#529).
    """
    facts = _facts()
    del facts["facts"]["us-gaap"]["CommonStockSharesOutstanding"]
    facts["facts"]["dei"] = {
        "EntityCommonStockSharesOutstanding": {
            "units": {"shares": [{"end": shares_end, "val": shares_val, "filed": shares_filed}]}
        }
    }
    return facts


def _issuer_shape(
    *,
    sic: str,
    shares: tuple[str, str, object] | None,
    reports_gross_profit: bool,
) -> tuple[dict, bool]:
    """One issuer's company-facts shape plus the registry verdict its SIC produces.

    `shares=None` means the issuer tags a current `CommonStockSharesOutstanding` (the
    ordinary case); a tuple is the multi-class shape where only a `dei` cover-page fact
    survives.
    """
    facts = _facts() if shares is None else _multi_class_facts(*shares)
    if not reports_gross_profit:
        del facts["facts"]["us-gaap"]["GrossProfit"]
        facts["facts"]["us-gaap"]["Revenues"] = {
            "units": {"USD": [_annual("2025-12-31", "2025-01-01", 332238000000, "2026-02-01")]}
        }
    return facts, revenue_proxy_allowed_for_sic(sic)


# The universe's real shapes, so a regression in either guard turns this red rather than
# being caught only by re-deriving from the vendor by hand (AGENTS.md rule 7). Each row is
# (label, SIC, surviving dei share fact or None, tags GrossProfit, expect gross profit,
# expect shares). The dates and SICs are the live values verified on 2026-07-30.
_UNIVERSE_SHAPES = [
    # Ordinary issuers: real gross profit, current share count. Stands for the sixteen
    # whose values this change must leave untouched.
    ("AAPL-like non_financial", "3571", None, True, True, True),
    # SIC 7389, tags neither GrossProfit nor COGS: the proxy is owner-approved here.
    ("V-like payment network", "7389", None, False, True, True),
    # Petroleum Refining with the same untagged shape: the proxy must NOT apply (#533).
    ("XOM-like oil major", "2911", None, False, False, True),
    # Multi-class issuer whose only surviving cover-page fact is from 2010 (#529).
    ("V-like multi-class shares", "7389", ("2010-01-27", "2010-02-03", 469280842), True, True, False),
    # Berkshire's Class-A-scale figure from 2011, refused for the same reason. Its gross
    # profit is absent too, because SIC 6331 routes to the insurance numerator and the
    # claims concept it needs stops at FY2016 in company-facts — the open residual on #496,
    # and the reason this landmine has not yet detonated in a decision.
    ("BRK.B-like multi-class shares", "6331", ("2011-04-29", "2011-05-06", 941481), True, False, False),
    # A current cover-page fact is kept: the bound must not reject a compliant filer.
    ("MA-like current cover page", "7389", ("2026-01-31", "2026-02-10", 905000000), True, True, True),
]


def test_both_guards_hold_across_every_shape_in_the_universe() -> None:
    for label, sic, shares, reports_gp, expect_gp, expect_shares in _UNIVERSE_SHAPES:
        facts, proxy_allowed = _issuer_shape(sic=sic, shares=shares, reports_gross_profit=reports_gp)
        branch = operating_branch_for_sic(sic)
        item = _work_item("1" * 64)
        target = SecTarget(
            cik=1,
            cutoff=_CUTOFF,
            issuer_id="issuer:lei:X",
            instrument_id="security:cusip:Y",
            listing_id="listing:xnas:x",
            operating_branch=branch,
            revenue_proxy_allowed=proxy_allowed,
        )
        adapter = SecFinancialFactAdapter(
            {item.work_item_id: target}, lambda cik, cutoff, b, _f=facts: build_bundle(_f, cutoff, b)
        )
        result = adapter.fetch(item)
        assert isinstance(result, FetchSuccess), label
        payload = result.record.payload
        assert (payload["gross_profit"] is not None) is expect_gp, f"{label}: gross_profit"
        assert (payload["shares_outstanding"] is not None) is expect_shares, f"{label}: shares_outstanding"


def test_a_share_count_older_than_the_staleness_bound_is_refused() -> None:
    # V's real shape: shares measured 2010-01-27 against fresh current-period profit.
    bundle = build_bundle(
        _multi_class_facts("2010-01-27", "2010-02-03", 469280842), _CUTOFF, OperatingBranch.NON_FINANCIAL
    )
    assert bundle.shares_outstanding is None, "a 2010 share count must not build a market capitalisation"
    # The measurement date survives the refusal so the gap is diagnosable in SQL, not just
    # reproducible by re-deriving from the vendor.
    assert bundle.shares_period_end == date(2010, 1, 27)
    # Refusing shares must not disturb the other inputs.
    assert bundle.gross_profit == Decimal("120")
    assert bundle.total_assets == Decimal("500")


def test_a_current_share_count_is_kept_and_dated() -> None:
    bundle = build_bundle(_multi_class_facts("2026-01-31", "2026-02-10", 1000), _CUTOFF, OperatingBranch.NON_FINANCIAL)
    assert bundle.shares_outstanding == Decimal("1000")
    assert bundle.shares_period_end == date(2026, 1, 31)


def test_the_staleness_bound_does_not_reject_a_normal_quarterly_cadence() -> None:
    # A filer whose latest cover page is a year old is late, not broken: the bound exists to
    # catch a fifteen-year gap, and must not start excluding compliant issuers.
    bundle = build_bundle(_multi_class_facts("2025-03-31", "2025-04-15", 1000), _CUTOFF, OperatingBranch.NON_FINANCIAL)
    assert bundle.shares_outstanding == Decimal("1000")


def test_a_refused_share_count_lowers_the_recorded_confidence() -> None:
    # `_confidence` counts present fields, so the refusal has to show up as a real gap
    # rather than a full-confidence record with a null in it.
    item = _work_item("d" * 64)

    def _fetched(shares_end: str, shares_filed: str, val: object) -> FetchSuccess | FetchFailure:
        adapter = SecFinancialFactAdapter(
            {item.work_item_id: _target()},
            lambda cik, cutoff, branch: build_bundle(
                _multi_class_facts(shares_end, shares_filed, val), cutoff, OperatingBranch.NON_FINANCIAL
            ),
        )
        return adapter.fetch(item)

    stale = _fetched("2010-01-27", "2010-02-03", 469280842)
    fresh = _fetched("2026-01-31", "2026-02-10", 1000)
    assert isinstance(stale, FetchSuccess) and isinstance(fresh, FetchSuccess)
    assert stale.confidence < fresh.confidence
    assert stale.record.payload["shares_outstanding"] is None
    assert stale.record.payload["shares_period_end"] == "2010-01-27"


def test_gross_profit_falls_back_to_revenue_minus_cost_over_a_shared_period() -> None:
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": [_annual("2025-12-31", "2025-01-01", 900, "2026-02-01")]}},
                "CostOfGoodsAndServicesSold": {
                    "units": {"USD": [_annual("2025-12-31", "2025-01-01", 400, "2026-02-01")]}
                },
            }
        }
    }
    datum = gross_profit(facts, _CUTOFF)
    assert datum is not None
    assert datum.value == Decimal("500")


def test_gross_profit_never_mixes_periods() -> None:
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": [_annual("2025-12-31", "2025-01-01", 900, "2026-02-01")]}},
                "CostOfRevenue": {"units": {"USD": [_annual("2024-12-31", "2024-01-01", 400, "2025-02-01")]}},
            }
        }
    }
    assert gross_profit(facts, _CUTOFF) is None


def _bank_facts() -> dict:
    return {
        "facts": {
            "us-gaap": {
                "RevenuesNetOfInterestExpense": {
                    "units": {"USD": [_annual("2025-12-31", "2025-01-01", 180, "2026-02-01")]}
                },
                "NoninterestExpense": {"units": {"USD": [_annual("2025-12-31", "2025-01-01", 95, "2026-02-01")]}},
                "Assets": {"units": {"USD": [{"end": "2025-12-31", "val": 4900, "filed": "2026-02-01"}]}},
            }
        }
    }


def test_depository_branch_uses_pre_provision_profit_as_the_operating_numerator() -> None:
    datum = pre_provision_profit(_bank_facts(), _CUTOFF)
    assert datum is not None
    assert datum.value == Decimal("85")

    bundle = build_bundle(_bank_facts(), _CUTOFF, OperatingBranch.FINANCIAL)
    # A bank files no GrossProfit; without the branch it would be missing_gross_profit.
    assert bundle.gross_profit == Decimal("85")
    assert bundle.pre_provision_profit == Decimal("85")


def test_non_financial_branch_never_derives_a_bank_proxy() -> None:
    bundle = build_bundle(_bank_facts(), _CUTOFF, OperatingBranch.NON_FINANCIAL)
    assert bundle.gross_profit is None
    assert bundle.pre_provision_profit is None


def test_a_payload_with_no_eligible_fact_is_captured_with_nulls() -> None:
    """A source that publishes nothing is a real, captured state — not a failure.

    XOM's post-reorganization CIK serves a company-facts document with no us-gaap
    taxonomy; the cell must record honest nulls so the factor names the gap.
    """
    item = _work_item("a" * 64)
    empty = {"facts": {"ffd": {}}}
    adapter = SecFinancialFactAdapter(
        {item.work_item_id: _target()}, lambda cik, cutoff, branch: build_bundle(empty, cutoff, branch)
    )
    result = adapter.fetch(item)
    assert isinstance(result, FetchSuccess)
    assert result.record is not None
    assert result.record.payload["gross_profit"] is None
    assert result.record.payload["total_assets"] is None
    assert result.confidence == Decimal("0.50")


def _bundle(**overrides: object) -> FinancialFactsBundle:
    fields: dict = {
        "gross_profit": Decimal("120"),
        "total_assets": Decimal("500"),
        "shares_outstanding": Decimal("10"),
        "revenue": Decimal("900"),
        "pre_provision_profit": None,
        "raw_bytes": b"{}",
        "knowable_at": datetime(2026, 2, 1, tzinfo=UTC),
    }
    fields.update(overrides)
    return FinancialFactsBundle(**fields)  # type: ignore[arg-type]


def test_adapter_success_carries_the_identity_and_branch() -> None:
    item = _work_item("3" * 64)
    adapter = SecFinancialFactAdapter({item.work_item_id: _target(320193)}, lambda cik, cutoff, branch: _bundle())
    result = adapter.fetch(item)
    assert isinstance(result, FetchSuccess)
    assert result.confidence == Decimal("0.92")  # all three core fields present
    assert result.record is not None
    assert result.record.payload["issuer_id"] == "issuer:lei:X"
    assert result.record.payload["operating_branch"] == "non_financial"
    assert result.record.payload["currency"] == "USD"


def test_adapter_no_bundle_is_field_unavailable() -> None:
    item = _work_item("4" * 64)
    adapter = SecFinancialFactAdapter({item.work_item_id: _target()}, lambda cik, cutoff, branch: None)
    result = adapter.fetch(item)
    assert isinstance(result, FetchFailure)
    assert result.reason_code is ObligationReasonCode.FIELD_UNAVAILABLE


def test_adapter_transient_and_timeout() -> None:
    item = _work_item("5" * 64)

    def _boom(cik, cutoff, branch):
        raise SourceUnavailableError("503")

    def _timeout(cik, cutoff, branch):
        raise TimeoutError

    a1 = SecFinancialFactAdapter({item.work_item_id: _target()}, _boom)
    a2 = SecFinancialFactAdapter({item.work_item_id: _target()}, _timeout)
    assert a1.fetch(item).reason_code is ObligationReasonCode.TRANSIENT_NETWORK
    assert a2.fetch(item).reason_code is ObligationReasonCode.TIMEOUT


def test_unknown_work_item_is_contract_violation() -> None:
    item = _work_item("6" * 64)
    other = _work_item("7" * 64)
    adapter = SecFinancialFactAdapter({item.work_item_id: _target()}, lambda cik, cutoff, branch: None)
    assert adapter.fetch(other).reason_code is ObligationReasonCode.CONTRACT_VIOLATION


def test_headcount_extractor_changes_the_normalized_identity() -> None:
    item = _work_item("8" * 64)
    without = SecFinancialFactAdapter({item.work_item_id: _target()}, lambda c, cut, b: _bundle())
    fact = HeadcountFact(value=Decimal("1800"), knowable_at=datetime(2026, 3, 1, tzinfo=UTC))
    with_hc = SecFinancialFactAdapter(
        {item.work_item_id: _target()},
        lambda c, cut, b: _bundle(),
        headcount_extractor=lambda c, cut: fact,
    )
    r0 = without.fetch(item)
    r1 = with_hc.fetch(item)
    assert isinstance(r0, FetchSuccess) and isinstance(r1, FetchSuccess)
    # Merging headcount changes the normalized observation identity and advances knowable_at.
    assert r1.normalized_sha256 != r0.normalized_sha256
    assert r1.transaction_time == datetime(2026, 3, 1, tzinfo=UTC)
    assert r1.record is not None
    assert r1.record.payload["headcount"] == "1800"


def test_headcount_after_cutoff_is_ignored() -> None:
    item = _work_item("9" * 64)
    late = HeadcountFact(value=Decimal("1"), knowable_at=datetime(2026, 4, 20, tzinfo=UTC))
    adapter = SecFinancialFactAdapter(
        {item.work_item_id: _target()},
        lambda c, cut, b: _bundle(),
        headcount_extractor=lambda c, cut: late,
    )
    baseline = SecFinancialFactAdapter({item.work_item_id: _target()}, lambda c, cut, b: _bundle())
    # A post-cutoff headcount is excluded, so the identity equals the no-headcount case.
    assert adapter.fetch(item).normalized_sha256 == baseline.fetch(item).normalized_sha256


def test_look_ahead_bundle_is_rejected() -> None:
    item = _work_item("b" * 64)
    adapter = SecFinancialFactAdapter(
        {item.work_item_id: _target()},
        lambda c, cut, b: _bundle(knowable_at=datetime(2026, 4, 20, tzinfo=UTC)),
    )
    assert adapter.fetch(item).reason_code is ObligationReasonCode.LOOK_AHEAD_VIOLATION


# -- concept-variant recency (#496) ----------------------------------------------------
#
# Every fixture above declares exactly one variant per field. That is the premise
# production data violates: an issuer that switched tags keeps the abandoned one's history
# forever, so both are present and only the period end says which is current. The cases
# below are the real shapes, reduced.


def test_a_current_variant_beats_an_abandoned_one_regardless_of_declaration_order() -> None:
    """AAPL: `Revenues` stops at FY2018, `RevenueFromContract…` carries FY2025.

    The abandoned tag is declared first, so a first-non-empty rule returns the 2018 figure
    — which is what production mart held for seven years.
    """
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": [_annual("2018-09-29", "2017-10-01", 265595000000, "2018-11-05")]}},
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {"USD": [_annual("2025-09-27", "2024-09-29", 416161000000, "2025-10-31")]}
                },
            }
        }
    }
    bundle = build_bundle(facts, _CUTOFF, OperatingBranch.NON_FINANCIAL)
    assert bundle.revenue == Decimal("416161000000")
    assert bundle.revenue_period_end == date(2025, 9, 27)


def test_a_stale_reported_gross_profit_loses_to_a_current_derivation() -> None:
    """AMZN: `GrossProfit` last tagged FY2009; FY2025 revenue and cost are both present."""
    facts = {
        "facts": {
            "us-gaap": {
                "GrossProfit": {"units": {"USD": [_annual("2009-12-31", "2009-01-01", 5531000000, "2010-01-29")]}},
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {"USD": [_annual("2025-12-31", "2025-01-01", 716924000000, "2026-02-06")]}
                },
                "CostOfGoodsAndServicesSold": {
                    "units": {"USD": [_annual("2025-12-31", "2025-01-01", 356414000000, "2026-02-06")]}
                },
            }
        }
    }
    datum = gross_profit(facts, _CUTOFF)
    assert datum is not None
    assert datum.value == Decimal("360510000000")
    assert datum.period_end == date(2025, 12, 31)


def test_a_reported_figure_still_wins_its_own_period() -> None:
    """The rule is 'later period wins', not 'derivation always wins': for a shared period
    the issuer's own assertion is preferred over our arithmetic."""
    facts = {
        "facts": {
            "us-gaap": {
                "GrossProfit": {"units": {"USD": [_annual("2025-12-31", "2025-01-01", 500, "2026-02-01")]}},
                "Revenues": {"units": {"USD": [_annual("2025-12-31", "2025-01-01", 900, "2026-02-01")]}},
                "CostOfRevenue": {"units": {"USD": [_annual("2025-12-31", "2025-01-01", 380, "2026-02-01")]}},
            }
        }
    }
    datum = gross_profit(facts, _CUTOFF)
    assert datum is not None
    assert datum.value == Decimal("500")  # not 520


def test_variants_are_never_cross_paired_across_periods() -> None:
    """A legacy revenue tag and a current cost tag must not be subtracted from each other
    just because that pair is reached first."""
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": [_annual("2018-12-31", "2018-01-01", 100, "2019-02-01")]}},
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {"USD": [_annual("2025-12-31", "2025-01-01", 900, "2026-02-01")]}
                },
                "CostOfRevenue": {"units": {"USD": [_annual("2025-12-31", "2025-01-01", 400, "2026-02-01")]}},
            }
        }
    }
    datum = gross_profit(facts, _CUTOFF)
    assert datum is not None
    assert datum.value == Decimal("500")  # 900 - 400, both FY2025 — never 100 - 400


def test_a_restatement_wins_by_filing_date_not_array_position() -> None:
    """Company-facts repeats one period end across every document that reported it. The
    latest FILING must win; taking whatever sits last in the array makes restatement
    handling depend on the vendor's serialization order."""
    facts = {
        "facts": {
            "us-gaap": {
                "GrossProfit": {
                    "units": {
                        "USD": [
                            _annual("2025-12-31", "2025-01-01", 120, "2026-03-01"),  # restated, later filing
                            _annual("2025-12-31", "2025-01-01", 100, "2026-02-01"),  # original, later in array
                        ]
                    }
                }
            }
        }
    }
    values = annual_values_by_period_end(facts, "us-gaap", "GrossProfit", "USD", _CUTOFF)
    assert values[date(2025, 12, 31)].value == Decimal("120")
    assert values[date(2025, 12, 31)].filed == date(2026, 3, 1)


def test_share_count_resolves_through_the_dei_taxonomy() -> None:
    """META/ABBV/JNJ/LLY reported no us-gaap share count at all; the figure large filers
    actually publish lives in `dei`."""
    facts = {
        "facts": {
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {"shares": [{"end": "2026-01-31", "val": 2500000000, "filed": "2026-02-10"}]}
                }
            }
        }
    }
    bundle = build_bundle(facts, _CUTOFF, OperatingBranch.NON_FINANCIAL)
    assert bundle.shares_outstanding == Decimal("2500000000")


def test_the_normalized_payload_carries_the_fiscal_periods() -> None:
    """Without these the warehouse cannot answer how old its own numbers are — the
    seven-year-stale revenue was only detectable by re-deriving from the vendor (#429)."""
    adapter = SecFinancialFactAdapter(
        {_work_item("d" * 64).work_item_id: _target()},
        lambda *_: build_bundle(_facts(), _CUTOFF, OperatingBranch.NON_FINANCIAL),
    )
    result = adapter.fetch(_work_item("d" * 64))
    assert isinstance(result, FetchSuccess)
    assert result.record is not None
    assert result.record.payload["operating_period_end"] == "2025-12-31"


# -- synonym variants merge; stand-in variants do not ----------------------------------


def test_treasury_inflated_share_counts_are_never_substituted() -> None:
    """`CommonStockSharesIssued` includes treasury stock: JNJ reports 3,119,843,000 issued
    against 2,409,898,597 outstanding. Ranking share concepts purely by period end would
    hand market cap a 29% error on whichever filing cycle carried the later date."""
    facts = {
        "facts": {
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {"shares": [{"end": "2026-01-31", "val": 2409898597, "filed": "2026-02-10"}]}
                }
            },
            "us-gaap": {
                "CommonStockSharesIssued": {  # later period, larger number, different quantity
                    "units": {"shares": [{"end": "2026-03-31", "val": 3119843000, "filed": "2026-04-20"}]}
                }
            },
        }
    }
    bundle = build_bundle(facts, _CUTOFF, OperatingBranch.NON_FINANCIAL)
    assert bundle.shares_outstanding == Decimal("2409898597")


def test_a_bank_never_takes_gross_revenue_just_because_it_is_more_recent() -> None:
    """For a bank, plain `Revenues` is gross of interest expense. Subtracting noninterest
    expense from it is not pre-provision NET revenue, however recent it is."""
    facts = {
        "facts": {
            "us-gaap": {
                "RevenuesNetOfInterestExpense": {
                    "units": {"USD": [_annual("2024-12-31", "2024-01-01", 180, "2025-02-01")]}
                },
                "Revenues": {"units": {"USD": [_annual("2025-12-31", "2025-01-01", 400, "2026-02-01")]}},
                "NoninterestExpense": {
                    "units": {
                        "USD": [
                            _annual("2024-12-31", "2024-01-01", 95, "2025-02-01"),
                            _annual("2025-12-31", "2025-01-01", 100, "2026-02-01"),
                        ]
                    }
                },
            }
        }
    }
    datum = pre_provision_profit(facts, _CUTOFF)
    assert datum is not None
    assert datum.value == Decimal("85")  # 180 - 95, the net-of-interest pair — never 400 - 100
    assert datum.period_end == date(2024, 12, 31)


def test_the_bank_fallback_is_still_reachable_when_the_exact_concept_is_absent() -> None:
    """Preference must not become prohibition: an issuer publishing no net-of-interest
    total still resolves, it just is not overridden by recency when it does publish one."""
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": [_annual("2025-12-31", "2025-01-01", 400, "2026-02-01")]}},
                "NoninterestExpense": {"units": {"USD": [_annual("2025-12-31", "2025-01-01", 100, "2026-02-01")]}},
            }
        }
    }
    datum = pre_provision_profit(facts, _CUTOFF)
    assert datum is not None
    assert datum.value == Decimal("300")


# --- #496 numerator policies (mapping v3, owner decision 2026-07-28) ---------------


def _units(entries: list[dict], unit: str = "USD") -> dict:
    return {"units": {unit: entries}}


def test_no_cogs_filer_uses_revenue_as_gross_profit_proxy() -> None:
    """V/MA/XOM: no GrossProfit and no COGS-family concept AT ALL -> revenue proxy."""
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": _units([_annual("2025-12-31", "2025-01-01", 300, "2026-02-01")]),
            }
        }
    }
    datum = gross_profit(facts, _CUTOFF)
    assert datum is not None and datum.value == Decimal("300")


def test_cogs_filer_with_period_mismatch_still_resolves_none() -> None:
    """Concept-level absence only: a real COGS filer whose periods never align
    must NOT silently fall to the revenue proxy."""
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": _units([_annual("2025-12-31", "2025-01-01", 300, "2026-02-01")]),
                "CostOfRevenue": _units([_annual("2024-12-31", "2024-01-01", 120, "2025-02-01")]),
            }
        }
    }
    assert gross_profit(facts, _CUTOFF) is None


def test_insurance_branch_subtracts_claims_at_the_latest_revenue_period() -> None:
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": _units([_annual("2025-12-31", "2025-01-01", 400, "2026-02-01")]),
                "PolicyholderBenefitsAndClaimsIncurredNet": _units(
                    [_annual("2025-12-31", "2025-01-01", 250, "2026-02-01")]
                ),
            }
        }
    }
    datum = insurance_pre_claims_profit(facts, _CUTOFF)
    assert datum is not None and datum.value == Decimal("150")


def test_insurance_branch_refuses_a_stale_claims_series() -> None:
    """BRK.B reality: the only API-visible claims series stops years before the
    latest revenue period — a stale difference paired with current headcount is
    the Amazon-2009 lesson, so the numerator resolves None (honest absence)."""
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": _units(
                    [
                        _annual("2016-12-31", "2016-01-01", 200, "2017-02-01"),
                        _annual("2025-12-31", "2025-01-01", 400, "2026-02-01"),
                    ]
                ),
                "IncurredClaimsPropertyCasualtyAndLiability": _units(
                    [_annual("2016-12-31", "2016-01-01", 120, "2017-02-01")]
                ),
            }
        }
    }
    assert insurance_pre_claims_profit(facts, _CUTOFF) is None


def test_weighted_average_shares_is_a_last_resort_and_never_shadows_point_in_time() -> None:
    both = {
        "facts": {
            "dei": {
                "EntityCommonStockSharesOutstanding": _units(
                    [{"end": "2026-01-31", "val": 10, "filed": "2026-02-10"}], unit="shares"
                )
            },
            "us-gaap": {
                "WeightedAverageNumberOfSharesOutstandingBasic": _units(
                    [_annual("2026-01-31", "2025-02-01", 999, "2026-02-10")], unit="shares"
                )
            },
        }
    }
    dual_class_only = {
        "facts": {
            "us-gaap": {
                "WeightedAverageNumberOfSharesOutstandingBasic": _units(
                    [_annual("2025-12-31", "2025-01-01", 2500, "2026-02-01")], unit="shares"
                )
            }
        }
    }
    # Same period end: preference decides, and the point-in-time count wins.
    bundle = build_bundle(both, _CUTOFF, OperatingBranch.NON_FINANCIAL)
    assert bundle.shares_outstanding == Decimal("10"), "point-in-time count must win its period"

    # No point-in-time tag at all: the dual-class filer gets the weighted average.
    bundle = build_bundle(dual_class_only, _CUTOFF, OperatingBranch.NON_FINANCIAL)
    assert bundle.shares_outstanding == Decimal("2500"), "META-style filers get the weighted average"


def test_a_weighted_average_never_wins_on_recency_alone() -> None:
    """The case a shared-period fixture cannot reach.

    A weighted average is a period figure and a cover-page count is an instant, so their
    dates routinely differ. Whenever the cover page predates the latest fiscal year end,
    ranking both in one synonym list hands the share count to the average — a different
    quantity, promoted purely by carrying a later date. Declaring it as a separate
    last-resort field is what makes that impossible rather than merely unlikely.
    """
    stale_cover_page = {
        "facts": {
            "dei": {
                "EntityCommonStockSharesOutstanding": _units(
                    [{"end": "2025-02-01", "val": 10, "filed": "2025-02-10"}], unit="shares"
                )
            },
            "us-gaap": {
                "WeightedAverageNumberOfSharesOutstandingBasic": _units(
                    [_annual("2025-12-31", "2025-01-01", 999, "2026-02-01")], unit="shares"
                )
            },
        }
    }
    bundle = build_bundle(stale_cover_page, _CUTOFF, OperatingBranch.NON_FINANCIAL)
    assert bundle.shares_outstanding == Decimal("10"), "a period average must not displace a real count"


def test_operating_branch_for_sic_maps_insurers() -> None:
    from data_engine.datahub.production_topt.issuer_registry import operating_branch_for_sic

    assert operating_branch_for_sic("6331") is OperatingBranch.INSURANCE
    assert operating_branch_for_sic("6022") is OperatingBranch.FINANCIAL
    assert operating_branch_for_sic("7389") is OperatingBranch.NON_FINANCIAL


def test_predecessor_cik_fallback_fires_only_on_an_empty_taxonomy() -> None:
    """#496: the mapped CIK's document exists but asserts nothing (knowable_at
    None) -> the adapter refetches through the SAME injected fetcher with the
    lineage-resolved predecessor. A payload with ANY eligible fact never falls
    back, and no predecessor means honest nulls."""
    calls: list[int] = []
    empty = FinancialFactsBundle(
        gross_profit=None,
        total_assets=None,
        shares_outstanding=None,
        revenue=None,
        pre_provision_profit=None,
        raw_bytes=b"{}",
        knowable_at=None,
    )
    full = FinancialFactsBundle(
        gross_profit=Decimal("5"),
        total_assets=Decimal("9"),
        shares_outstanding=Decimal("3"),
        revenue=Decimal("7"),
        pre_provision_profit=None,
        raw_bytes=b"{}",
        knowable_at=datetime(2026, 3, 1, tzinfo=UTC),
    )

    def fetcher(cik: int, cutoff: date, branch: OperatingBranch) -> FinancialFactsBundle:
        calls.append(cik)
        return empty if cik == 2115436 else full

    item = _work_item("a" * 64)
    target = SecTarget(
        cik=2115436,
        cutoff=_CUTOFF,
        issuer_id="issuer:lei:X",
        instrument_id="security:cusip:Y",
        listing_id="listing:xnys:xom",
        operating_branch=OperatingBranch.NON_FINANCIAL,
        predecessor_cik=34088,
    )
    adapter = SecFinancialFactAdapter({item.work_item_id: target}, fetcher)
    outcome = adapter.fetch(item)
    assert calls == [2115436, 34088], "fallback must reuse the injected fetcher with the predecessor"
    assert isinstance(outcome, FetchSuccess)

    calls.clear()
    item2 = _work_item("b" * 64)
    target2 = SecTarget(
        cik=2115436,
        cutoff=_CUTOFF,
        issuer_id="issuer:lei:X",
        instrument_id="security:cusip:Y",
        listing_id="listing:xnys:xom",
        operating_branch=OperatingBranch.NON_FINANCIAL,
    )
    adapter_no_pred = SecFinancialFactAdapter({item2.work_item_id: target2}, fetcher)
    adapter_no_pred.fetch(item2)
    assert calls == [2115436], "no predecessor -> no second fetch, honest nulls stand"


# -- earnings CAGR for module 1 (PEG) -------------------------------------------------


def _earnings_facts(*pairs: tuple[str, str, object]) -> dict:
    """company-facts carrying annual NetIncomeLoss periods: (start, end, value)."""
    facts = _facts()
    facts["facts"]["us-gaap"]["NetIncomeLoss"] = {
        "units": {"USD": [_annual(end, start, value, f"{end[:4]}-02-15") for start, end, value in pairs]}
    }
    return facts


def test_the_bundle_derives_an_earnings_cagr_from_the_payload_it_already_fetched() -> None:
    """PEG needs multi-period earnings, and company-facts already carries them.

    `annual_values_by_period_end` builds every annual period keyed by period end and
    the bundle then throws all but the latest away. Deriving the CAGR here means module
    1 needs no new data plane: no `staging.financial_facts`, which is empty in
    Production with no reachable writer (#530).
    """
    facts = _earnings_facts(
        ("2022-01-01", "2022-12-31", 100),
        ("2023-01-01", "2023-12-31", 120),
        ("2024-01-01", "2024-12-31", 150),
        ("2025-01-01", "2025-12-31", 200),
    )
    bundle = build_bundle(facts, date(2026, 3, 31), OperatingBranch.NON_FINANCIAL, earnings_cagr_years=3)
    assert bundle.earnings_cagr is not None
    # Yearly rates 20%, 25%, 33.33% weighted 1:2:3 -> (20 + 50 + 100) / 6 = 28.33%.
    # An endpoint CAGR would have said 25.99%; the owner-chosen weighting favours the
    # accelerating recent years.
    assert bundle.earnings_cagr.quantize(Decimal("0.0001")) == Decimal("0.2833")
    # Both endpoints are recorded so the window is auditable rather than implied.
    assert bundle.earnings_cagr_base_period_end == date(2022, 12, 31)
    assert bundle.earnings_cagr_latest_period_end == date(2025, 12, 31)


def test_the_cagr_is_never_knowable_before_the_filings_it_consumed() -> None:
    """The PIT obligation strategy participation adds (#284).

    `strategy_backtest_inputs_pit` only asserts `knowable_at <= cutoff_at`, so it would
    accept a CAGR built from a restatement that was not public at the historical cutoff
    — which makes a backtest look better for free. The bundle's `knowable_at` must be at
    or after the latest filing behind the window.
    """
    facts = _earnings_facts(
        ("2022-01-01", "2022-12-31", 100),
        ("2023-01-01", "2023-12-31", 120),
        ("2024-01-01", "2024-12-31", 150),
        ("2025-01-01", "2025-12-31", 200),
    )
    bundle = build_bundle(facts, date(2026, 3, 31), OperatingBranch.NON_FINANCIAL, earnings_cagr_years=3)
    assert bundle.earnings_cagr is not None
    assert bundle.knowable_at is not None
    # The 2025 period was filed 2025-02-15 in this fixture; nothing may claim the CAGR
    # was knowable before then.
    assert bundle.knowable_at.date() >= date(2025, 2, 15)


def test_a_missing_base_period_yields_no_cagr_rather_than_a_shorter_window() -> None:
    # Only 2024 and 2025 on file; a three-year window has no 2022 base. Silently
    # computing a one-year rate would make issuers incomparable in one ranking.
    facts = _earnings_facts(
        ("2024-01-01", "2024-12-31", 150),
        ("2025-01-01", "2025-12-31", 200),
    )
    bundle = build_bundle(facts, date(2026, 3, 31), OperatingBranch.NON_FINANCIAL, earnings_cagr_years=3)
    assert bundle.earnings_cagr is None


def test_no_cagr_is_derived_when_the_issuer_reports_no_net_income() -> None:
    bundle = build_bundle(_facts(), date(2026, 3, 31), OperatingBranch.NON_FINANCIAL, earnings_cagr_years=3)
    assert bundle.earnings_cagr is None
    assert bundle.earnings_cagr_base_period_end is None


def test_the_adapter_publishes_the_cagr_and_its_window_in_the_payload() -> None:
    item = _work_item("a" * 64)
    facts = _earnings_facts(
        ("2022-01-01", "2022-12-31", 100),
        ("2023-01-01", "2023-12-31", 120),
        ("2024-01-01", "2024-12-31", 150),
        ("2025-01-01", "2025-12-31", 200),
    )
    adapter = SecFinancialFactAdapter(
        {item.work_item_id: _target()},
        lambda cik, cutoff, branch: build_bundle(facts, cutoff, branch, earnings_cagr_years=3),
    )
    result = adapter.fetch(item)
    assert isinstance(result, FetchSuccess)
    payload = result.record.payload
    assert payload["earnings_cagr_3y"] is not None
    assert payload["earnings_cagr_base_period_end"] == "2022-12-31"
    assert payload["earnings_cagr_latest_period_end"] == "2025-12-31"


def test_the_window_is_measured_in_elapsed_time_not_calendar_years() -> None:
    """JNJ's real shape: a fiscal year ending 2022-01-02 is FY2021, not FY2022.

    Selecting the base period by `end.year == latest.year - years` made a 3.99-year span
    read as a three-year one, overstating the rate. JNJ's periods end 2022-01-02,
    2023-01-01, 2023-12-31, 2024-12-29 and 2025-12-28 — from 2025-12-28 a three-year
    window is 2023-01-01 (1092 days), not 2022-01-02 (1456 days).
    """
    facts = _earnings_facts(
        ("2021-01-04", "2022-01-02", 20_880),
        ("2022-01-03", "2023-01-01", 17_940),
        ("2023-01-02", "2023-12-31", 35_150),
        ("2024-01-01", "2024-12-29", 14_070),
        ("2024-12-30", "2025-12-28", 26_800),
    )
    bundle = build_bundle(facts, date(2026, 8, 17), OperatingBranch.NON_FINANCIAL, earnings_cagr_years=3)
    assert bundle.earnings_cagr_latest_period_end == date(2025, 12, 28)
    assert bundle.earnings_cagr_base_period_end == date(2023, 1, 1), (
        "the base must be the period ~3 years back in elapsed time, not the one whose "
        "calendar year happens to be latest.year - 3"
    )


def test_a_period_that_is_not_close_enough_to_the_window_is_refused() -> None:
    # Only a 5-year-old base exists; stretching to it would report a 3-year rate for a
    # 5-year span, which is a different number.
    facts = _earnings_facts(
        ("2020-01-01", "2020-12-31", 100),
        ("2025-01-01", "2025-12-31", 200),
    )
    bundle = build_bundle(facts, date(2026, 8, 17), OperatingBranch.NON_FINANCIAL, earnings_cagr_years=3)
    assert bundle.earnings_cagr is None


def test_the_growth_rate_weights_recent_years_more_heavily() -> None:
    """Owner decision (2026-08-17): three years, but not equal weights — recent higher.

    A plain endpoint CAGR is not equal-weighted, it is ZERO-weighted on the middle: only
    the base and the latest observation enter it, so a collapse and recovery in between is
    invisible. The rate is now the recency-weighted mean of the year-over-year rates inside
    the window, weights rising linearly toward the present (1:2:3 over three steps).

    Here the yearly rates are +10%, +20% and +50%. Equal weighting gives 26.67%; weighting
    1:2:3 gives (1*10 + 2*20 + 3*50) / 6 = 33.33%, and an endpoint CAGR would give 24.6%.
    """
    facts = _earnings_facts(
        ("2022-01-01", "2022-12-31", 100),
        ("2023-01-01", "2023-12-31", 110),
        ("2024-01-01", "2024-12-31", 132),
        ("2025-01-01", "2025-12-31", 198),
    )
    bundle = build_bundle(facts, date(2026, 3, 31), OperatingBranch.NON_FINANCIAL, earnings_cagr_years=3)
    assert bundle.earnings_cagr is not None
    assert bundle.earnings_cagr.quantize(Decimal("0.0001")) == Decimal("0.3333")


def test_a_gap_inside_the_window_refuses_rather_than_skipping_a_year() -> None:
    """Recency weighting needs every year in the window, not just the endpoints.

    Silently dropping a missing year would change the weights without saying so — the
    remaining rates would be reweighted and the number would no longer be the thing its
    version claims. 2024 is absent here.
    """
    facts = _earnings_facts(
        ("2022-01-01", "2022-12-31", 100),
        ("2023-01-01", "2023-12-31", 110),
        ("2025-01-01", "2025-12-31", 198),
    )
    bundle = build_bundle(facts, date(2026, 3, 31), OperatingBranch.NON_FINANCIAL, earnings_cagr_years=3)
    assert bundle.earnings_cagr is None


def test_a_loss_year_inside_the_window_makes_the_rate_undefined() -> None:
    # A year-over-year rate across a sign change is not a growth rate, and it would
    # dominate a weighted mean. Endpoint-only logic could not see this.
    facts = _earnings_facts(
        ("2022-01-01", "2022-12-31", 100),
        ("2023-01-01", "2023-12-31", -50),
        ("2024-01-01", "2024-12-31", 132),
        ("2025-01-01", "2025-12-31", 198),
    )
    bundle = build_bundle(facts, date(2026, 3, 31), OperatingBranch.NON_FINANCIAL, earnings_cagr_years=3)
    assert bundle.earnings_cagr is None
