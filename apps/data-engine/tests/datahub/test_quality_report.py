"""Coverage for `quality_report` (truealpha#462 AC4, #537).

Two layers. The falsifiability harness for `availability` and `lineage_completeness`
lives in `tests/production_topt/test_persistence.py`, because falsifying them needs a
real 84-cell capture run with one deliberately broken cell. What is here is the
Postgres-free half: the two predicates those metrics are built on, pinned directly, so
"usable value" and "the pointer dereferences" cannot quietly widen back into
"a row exists".

`persist` is the one function in this module genuinely testable standalone: it
inserts a caller-supplied report dict straight into `mart.datahub_quality_report`,
an append-only table with no foreign-key dependency on the capture-control tables.
`latest_run`/`build_report`/`_reconcile_market_price_cells` all read from
`mart.topt_capture_status`, a view over the full capture-control pipeline (campaigns,
obligations, attempts) -- exercising those against a real Postgres needs that whole
chain seeded first, which is out of scope here; this file closes the narrower,
achievable slice of the coverage gap #462 found (this module had zero tests
referencing it anywhere in the repo), not the whole thing.
"""

from __future__ import annotations

import hashlib
import os

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.production_topt.materialization import FinancialFactPayload
from data_engine.datahub.quality_report import (
    _FINANCIAL_FACT_OPERATING_NUMERATOR,
    _has_usable_value,
    _PointerDereferencer,
    persist,
)
from factors.production_topt import OperatingBranch
from truealpha_contracts.models import RawObjectRef


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=True)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        yield active
    finally:
        active.close()


def _report(run_id: str, **overrides: object) -> dict[str, object]:
    report: dict[str, object] = {
        "run_id": run_id,
        "requested_count": 84,
        "complete": True,
        "freshness": "1.0000",
    }
    report.update(overrides)
    return report


_IDENTITY = {"issuer_id": "i", "instrument_id": "n", "listing_id": "l", "ticker": "T"}
_MARKET_PRICE = {"issuer_id": "i", "instrument_id": "n", "listing_id": "l", "currency": "USD", "close": "40"}
_FINANCIAL = {
    "issuer_id": "i",
    "instrument_id": "n",
    "listing_id": "l",
    "operating_branch": "non_financial",
    "currency": "USD",
    "gross_profit": "210000000",
    "total_assets": "200000000",
    "headcount": "164000",
    "revenue": "100000000",
    "shares_outstanding": "10000000",
    "pre_provision_profit": None,
}


def test_a_complete_payload_is_usable_for_every_requested_semantic() -> None:
    assert _has_usable_value("listing-identity", _IDENTITY)
    assert _has_usable_value("universe-membership", _IDENTITY)
    assert _has_usable_value("market-price", _MARKET_PRICE)
    assert _has_usable_value("financial-fact", _FINANCIAL)


def test_a_payload_that_yields_no_number_is_not_usable() -> None:
    """The Staging 13:01 shape: the row is there, the number is not."""
    assert not _has_usable_value("market-price", {**_MARKET_PRICE, "close": None})
    assert not _has_usable_value("financial-fact", {**_FINANCIAL, "gross_profit": None})
    assert not _has_usable_value("financial-fact", {**_FINANCIAL, "headcount": None})
    assert not _has_usable_value("financial-fact", {**_FINANCIAL, "total_assets": None})


def test_an_unparseable_or_absent_payload_is_not_usable() -> None:
    assert not _has_usable_value("listing-identity", {k: v for k, v in _IDENTITY.items() if k != "ticker"})
    assert not _has_usable_value("financial-fact", {"unexpected": "shape"})
    assert not _has_usable_value("market-price", None)


def test_an_undeclared_semantic_is_not_usable() -> None:
    """A new semantic must declare what usable means for it; defaulting to yes is the bug."""
    assert not _has_usable_value("employee-headcount", _IDENTITY)


class _Bucket:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.reads = 0

    def get(self, ref: RawObjectRef) -> bytes:
        self.reads += 1
        return self.objects[ref.key]


def _uri(key: str) -> str:
    return f"s3://truealpha-raw/{key}"


def test_a_pointer_dereferences_only_when_the_bytes_are_there_and_match() -> None:
    body = b'{"facts":{}}'
    digest = hashlib.sha256(body).hexdigest()
    store = _Bucket({"raw/sec/ab/object": body})
    pointers = _PointerDereferencer(store)

    assert pointers.dereferences(
        object_uri=_uri("raw/sec/ab/object"), sha256=digest, byte_length=len(body), content_type="application/json"
    )
    # Absent object, and present object whose digest is not the one claimed.
    assert not pointers.dereferences(
        object_uri=_uri("raw/sec/cd/gone"), sha256=digest, byte_length=len(body), content_type="application/json"
    )
    store.objects["raw/sec/ef/tampered"] = b"other bytes"
    assert not pointers.dereferences(
        object_uri=_uri("raw/sec/ef/tampered"), sha256=digest, byte_length=len(body), content_type="application/json"
    )


def test_repeated_pointers_cost_one_read() -> None:
    """84 cells per tick collapse onto far fewer content-addressed objects."""
    body = b"shared"
    digest = hashlib.sha256(body).hexdigest()
    store = _Bucket({"raw/release/aa/shared": body})
    pointers = _PointerDereferencer(store)

    for _ in range(5):
        assert pointers.dereferences(
            object_uri=_uri("raw/release/aa/shared"), sha256=digest, byte_length=len(body), content_type="text/plain"
        )
    assert store.reads == 1


def test_persist_writes_a_content_addressed_row(connection) -> None:
    run_id = "capture-run:" + "e" * 64
    report_id = persist(connection, _report(run_id))

    row = connection.execute(
        "select run_id, requested_count, payload from mart.datahub_quality_report where report_id = %s",
        (report_id,),
    ).fetchone()

    assert row is not None
    assert row[0] == run_id
    assert row[1] == 84
    assert row[2]["run_id"] == run_id
    assert report_id.startswith("datahub-quality-report:")


def test_persist_is_idempotent_for_identical_content(connection) -> None:
    run_id = "capture-run:" + "f" * 64
    report = _report(run_id, note="idempotency-check")

    first_id = persist(connection, report)
    second_id = persist(connection, report)

    assert first_id == second_id
    count = connection.execute(
        "select count(*) from mart.datahub_quality_report where report_id = %s", (first_id,)
    ).fetchone()[0]
    assert count == 1


def test_persist_gives_distinct_reports_distinct_ids(connection) -> None:
    run_id = "capture-run:" + "1" * 64
    first_id = persist(connection, _report(run_id, freshness="1.0000"))
    second_id = persist(connection, _report(run_id, freshness="0.9000"))

    assert first_id != second_id
    rows = connection.execute(
        "select report_id from mart.datahub_quality_report where run_id = %s", (run_id,)
    ).fetchall()
    assert {r[0] for r in rows} == {first_id, second_id}


def test_the_numerator_map_is_total_over_operating_branches() -> None:
    """Every `OperatingBranch` member must resolve a numerator attribute.

    The first deployed tick after INSURANCE was added (#534) crashed on BRK.B's
    financial-fact cell with a KeyError in `_has_usable_value`, aborting the whole
    run — CI stayed green because its fixture emitted only the two branches the map
    then covered. A branch added without a row here must fail THIS test, not the
    next production tick.
    """
    assert set(_FINANCIAL_FACT_OPERATING_NUMERATOR) == set(OperatingBranch)
    for branch, attribute in _FINANCIAL_FACT_OPERATING_NUMERATOR.items():
        assert attribute in FinancialFactPayload.model_fields, (branch, attribute)


def test_an_insurance_branch_payload_grades_available() -> None:
    """The exact payload shape that crashed the 2026-08-14 05:41 staging tick."""
    payload = {
        "issuer_id": "issuer:lei:5493000C01ZX7D35SD85",
        "instrument_id": "security:cusip:084670702",
        "listing_id": "listing:xnys:brk.b",
        "operating_branch": "insurance",
        "currency": "USD",
        "gross_profit": "80000000",
        "total_assets": "200000000",
        "headcount": "392400",
        "revenue": "100000000",
        "shares_outstanding": "10000000",
        "pre_provision_profit": None,
    }
    assert _has_usable_value("financial-fact", payload) is True
    assert _has_usable_value("financial-fact", {**payload, "gross_profit": None}) is False


def _fact(**overrides):
    from data_engine.datahub.production_topt.materialization import FinancialFactPayload

    base = {
        "issuer_id": "issuer:lei:TEST00000000000000000",
        "instrument_id": "security:cusip:tst000000",
        "listing_id": "listing:xnas:tst",
        "operating_branch": "non_financial",
        "currency": "USD",
        "gross_profit": "80000000",
        "total_assets": "200000000",
        "headcount": "40000",
        "revenue": "100000000",
        "shares_outstanding": "10000000",
        "pre_provision_profit": None,
    }
    return FinancialFactPayload.model_validate({**base, **overrides})


def test_every_plausibility_rule_fires_on_its_own_violation() -> None:
    """D8 for the oracle (#578): a rule that cannot fire measures nothing, so each
    rule is driven by the exact shape it exists to refuse — including the two
    incidents that reached the served page (revenue-as-gross-profit inflation and
    a stale share count driving per-employee off the domain)."""
    from data_engine.datahub.quality_report import _plausibility_violations

    assert _plausibility_violations(_fact()) == []
    # A numerator strictly above revenue is impossible accounting.
    assert _plausibility_violations(_fact(gross_profit="150000000", revenue="100000000")) == [
        "gross_profit_exceeds_revenue"
    ]
    # Division of labor, stated where it can be read: the REAL XOM incident was
    # gross_profit == revenue (the proxy substitution), and equality passes this
    # oracle BY DESIGN because payment networks earn it legitimately. The guard
    # for the equality case is the adapter's industry bound (#533) — the oracle's
    # job is the strictly-impossible and the out-of-domain, not re-litigating
    # industry approval.
    assert _plausibility_violations(_fact(gross_profit="100000000")) == []
    assert "pre_provision_profit_exceeds_revenue" in _plausibility_violations(
        _fact(operating_branch="financial", gross_profit=None, pre_provision_profit="200000000")
    )
    assert _plausibility_violations(_fact(headcount="0")) == ["nonpositive_headcount"]
    assert _plausibility_violations(_fact(total_assets="-1")) == ["nonpositive_total_assets"]
    assert _plausibility_violations(_fact(shares_outstanding="0")) == ["nonpositive_shares_outstanding"]
    # Domain bound, both sides: $500/employee and $50M/employee are outside any
    # legitimate issuer's range.
    assert _plausibility_violations(_fact(headcount="160000000")) == ["per_employee_outside_domain"]
    assert _plausibility_violations(_fact(headcount="1")) == ["per_employee_outside_domain"]


def test_the_boundary_values_are_inside_the_domain() -> None:
    from data_engine.datahub.quality_report import _plausibility_violations

    exactly_floor = _fact(gross_profit="40000000", headcount="40000")  # $1,000/employee
    assert _plausibility_violations(exactly_floor) == []


def test_financial_branch_uses_its_own_numerator_for_the_domain_bound() -> None:
    """The per-employee bound judges the branch's OWN numerator: a FINANCIAL payload
    carrying a plausible gross_profit must still be graded on pre_provision_profit
    (Copilot on #599)."""
    from data_engine.datahub.quality_report import _plausibility_violations

    fact = _fact(
        operating_branch="financial",
        gross_profit="80000000",  # $2,000/employee — inside the domain
        pre_provision_profit="1000000",  # $25/employee — outside it
    )
    assert _plausibility_violations(fact) == ["per_employee_outside_domain"]


def _price_entry(source_id: str, day: str, value: str):
    from datetime import UTC, datetime
    from decimal import Decimal

    origin = "origin:yahoo:v1" if source_id == "yahoo-chart:v1" else "origin:twelve-data:v1"
    return (
        source_id,
        datetime.fromisoformat(day).replace(tzinfo=UTC),
        Decimal("0.85"),
        hashlib.sha256(f"{source_id}:{day}:{value}".encode()).hexdigest(),
        "normalized-observation:" + hashlib.sha256(f"{source_id}{day}".encode()).hexdigest(),
        "source-vintage:" + "a" * 64,
        "raw-object:" + "b" * 64,
        {"origin_group": origin, "value": value},
    )


def test_served_day_pairing_keeps_same_day_assertions_together() -> None:
    from data_engine.datahub.quality_report import _served_day_assertions

    same_day = [
        _price_entry("yahoo-chart:v1", "2026-08-17", "1183.16"),
        _price_entry("twelve-data:v1", "2026-08-17", "1184.91"),
    ]
    assert _served_day_assertions(same_day) == same_day


def test_served_day_pairing_drops_the_other_days_assertion() -> None:
    """#622: Yahoo's overnight rebuild nulls the latest close, its cell falls back to
    Friday, and Friday-vs-Monday must grade as a missing second origin for the served
    day — not as a value conflict. 78/102 QQQ cells on 2026-08-18."""
    from data_engine.datahub.quality_report import _served_day_assertions

    friday_yahoo = _price_entry("yahoo-chart:v1", "2026-08-14", "233.96")
    monday_td = _price_entry("twelve-data:v1", "2026-08-17", "229.45")
    assert _served_day_assertions([friday_yahoo, monday_td]) == [friday_yahoo]


def test_served_day_pairing_anchors_on_newest_primary_day() -> None:
    from data_engine.datahub.quality_report import _served_day_assertions

    old_yahoo = _price_entry("yahoo-chart:v1", "2026-08-14", "233.96")
    new_yahoo = _price_entry("yahoo-chart:v1", "2026-08-17", "234.10")
    td = _price_entry("twelve-data:v1", "2026-08-17", "234.12")
    assert _served_day_assertions([old_yahoo, new_yahoo, td]) == [new_yahoo, td]


def test_served_day_pairing_without_primary_uses_newest_day() -> None:
    from data_engine.datahub.quality_report import _served_day_assertions

    stale_td = _price_entry("twelve-data:v1", "2026-08-14", "229.00")
    fresh_td = _price_entry("twelve-data:v1", "2026-08-17", "229.45")
    assert _served_day_assertions([stale_td, fresh_td]) == [fresh_td]


def test_cross_day_pair_reconciles_insufficient_not_conflict() -> None:
    """End-to-end through the real fusion engine: the narrowed single-origin cell
    grades INSUFFICIENT_INDEPENDENT_ORIGINS, never CONFLICT_ABSTAINED."""
    from datetime import UTC, datetime

    from data_engine.datahub.quality_report import RECONCILIATION_POLICY, _served_day_assertions
    from truealpha_contracts import canonical_sha256
    from truealpha_contracts.reconciliation import (
        ReconciliationCell,
        ReconciliationOutcome,
        SourceAssertion,
        reconcile_source_assertions,
    )
    from truealpha_contracts.universe import SubjectKind, SubjectRef

    entries = _served_day_assertions(
        [
            _price_entry("yahoo-chart:v1", "2026-08-14", "233.96"),
            _price_entry("twelve-data:v1", "2026-08-17", "229.45"),
        ]
    )
    from decimal import Decimal

    cell = ReconciliationCell(
        requirement_id=f"data-requirement:{canonical_sha256({'requirement': 'market-price:v1'})}",
        subject=SubjectRef(kind=SubjectKind.LISTING, id="listing:xnas:hon"),
        field_name="close",
        field_semantics_id=f"field-semantics:{canonical_sha256({'field': 'market-price-close:v1'})}",
        unit="USD",
        valid_from=datetime(2026, 8, 14, tzinfo=UTC).date(),
        valid_to=datetime(2026, 8, 18, tzinfo=UTC).date(),
    )
    assertions = tuple(
        SourceAssertion(
            cell_id=cell.cell_id,
            observation_id=obs_id,
            source_id=source_id,
            origin_group_id=extra["origin_group"],
            knowable_at=knowable_at,
            normalized_value_sha256=payload_sha,
            numeric_value=Decimal(str(extra["value"])),
            confidence_assessment_id=f"confidence-assessment:{payload_sha}",
            confidence_score=confidence,
            lineage_node_ids=(vintage_id, raw_object),
            lineage_complete=True,
        )
        for source_id, knowable_at, confidence, payload_sha, obs_id, vintage_id, raw_object, extra in entries
    )
    result = reconcile_source_assertions(
        cell=cell,
        assertions=assertions,
        policy=RECONCILIATION_POLICY,
        cutoff=datetime(2026, 8, 18, 3, 51, tzinfo=UTC),
    )
    assert result.outcome == ReconciliationOutcome.INSUFFICIENT_INDEPENDENT_ORIGINS


def test_factor_availability_counts_subjects_with_complete_input_sets() -> None:
    """#641 D4: the headline availability equal-weights all semantics (89 missing
    financial-fact cells diluted 4:1 read as 0.78 while 12/101 issuers were
    factor-computable). The factor-level view judges each subject by its factor's
    OWN required semantics — and only those."""
    from data_engine.datahub.quality_report import _factor_availability

    usable = {
        "listing:a": {"financial-fact": True, "market-price": True},
        "listing:b": {"financial-fact": False, "market-price": True},  # headcount hole
        "listing:c": {"financial-fact": True},  # price missing — irrelevant to GPPE
        "listing:d": {"market-price": True},  # NO financial-fact obligation at all: still in the denominator
    }
    out = _factor_availability(usable)
    gppe = out["gross_profit_per_employee"]
    assert gppe["required_semantics"] == ["financial-fact"]
    assert (gppe["complete_subjects"], gppe["universe_subjects"]) == (2, 4)
    assert gppe["ratio"] == "0.5000"


def test_factor_availability_with_no_graded_subjects_is_zero() -> None:
    from data_engine.datahub.quality_report import _factor_availability

    out = _factor_availability({})
    assert out["gross_profit_per_employee"]["ratio"] == "0"
