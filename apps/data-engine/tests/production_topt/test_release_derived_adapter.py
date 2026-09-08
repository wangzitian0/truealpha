from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from data_engine.datahub.production_topt.executor import FetchSuccess
from data_engine.datahub.production_topt.release_derived_adapter import (
    ReleaseDerivedAdapter,
    ReleaseDerivedRecord,
    build_route,
)
from data_engine.datahub.production_topt.source_registrations import RouteCell, RouteContext
from truealpha_contracts.datahub import CaptureWorkItem
from truealpha_contracts.obligation_reason_codes import ObligationReasonCode

_CUTOFF = date(2026, 3, 31)
_KNOWN = datetime(2026, 3, 1, tzinfo=UTC)


def _work_item(digest: str) -> CaptureWorkItem:
    return CaptureWorkItem(
        campaign_id="capture-campaign:" + "1" * 64,
        source_request_id="source-request:" + digest,
        schedule_policy_id="schedule-policy:" + "2" * 64,
    )


def _context(*, universe_published_at: datetime | None, cutoff_date: date = _CUTOFF) -> RouteContext:
    return RouteContext(
        cutoff=datetime.combine(cutoff_date, datetime.min.time(), tzinfo=UTC),
        cutoff_date=cutoff_date,
        price_cutoff_date=cutoff_date,
        partition_start=datetime(2026, 1, 1, tzinfo=UTC),
        universe_published_at=universe_published_at,
        coordinates={},
        connection=None,
    )


def _cell(work_item_id: str, *, semantic_type: str = "listing-identity") -> RouteCell:
    return RouteCell(
        work_item_id=work_item_id,
        semantic_type=semantic_type,
        issuer_id="issuer:cik:1",
        instrument_id="instrument:1",
        listing_id="listing:xnas:aaa",
        ticker="AAA",
    )


def test_listing_identity_success_is_exact() -> None:
    item = _work_item("3" * 64)
    record = ReleaseDerivedRecord(
        semantic_type="listing-identity",
        subject_id="listing:goog",
        payload={"cik": 1652044, "ticker": "GOOG"},
        knowable_at=_KNOWN,
    )
    adapter = ReleaseDerivedAdapter({item.work_item_id: record}, cutoff=_CUTOFF)
    result = adapter.fetch(item)
    assert isinstance(result, FetchSuccess)
    assert result.confidence == Decimal("1.0")
    assert adapter.fetch(item).normalized_sha256 == result.normalized_sha256


def test_universe_membership_success() -> None:
    item = _work_item("4" * 64)
    record = ReleaseDerivedRecord(
        semantic_type="universe-membership",
        subject_id="listing:goog",
        payload={"universe": "topt-us-2026-03-31", "member": True},
        knowable_at=_KNOWN,
    )
    adapter = ReleaseDerivedAdapter({item.work_item_id: record}, cutoff=_CUTOFF)
    assert isinstance(adapter.fetch(item), FetchSuccess)


def test_unknown_work_item_is_contract_violation() -> None:
    item = _work_item("5" * 64)
    other = _work_item("6" * 64)
    record = ReleaseDerivedRecord("listing-identity", "listing:x", {}, _KNOWN)
    adapter = ReleaseDerivedAdapter({item.work_item_id: record}, cutoff=_CUTOFF)
    assert adapter.fetch(other).reason_code is ObligationReasonCode.CONTRACT_VIOLATION


def test_look_ahead_is_rejected() -> None:
    item = _work_item("7" * 64)
    late = datetime(2026, 4, 10, tzinfo=UTC)
    record = ReleaseDerivedRecord("listing-identity", "listing:x", {}, late)
    adapter = ReleaseDerivedAdapter({item.work_item_id: record}, cutoff=_CUTOFF)
    assert adapter.fetch(item).reason_code is ObligationReasonCode.LOOK_AHEAD_VIOLATION


def test_unsupported_semantic_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="unsupported release-derived semantic"):
        ReleaseDerivedRecord("market-price", "listing:x", {}, _KNOWN)


# -- build_route: knowable_at basis (#530 item 2) -----------------------------------------


def test_build_route_uses_the_universe_heads_published_at_when_present() -> None:
    """A governed universe (QQQ, canary) carries its head's own recorded_at; that is the
    real knowable-at for its identity/membership rows, not the partition coordinate."""
    published_at = datetime(2026, 2, 10, 9, 30, tzinfo=UTC)
    context = _context(universe_published_at=published_at)
    adapter = build_route(context, [_cell("wi-1")])
    record = adapter.targets["wi-1"]
    assert record.knowable_at == published_at
    assert record.knowable_at_basis == "universe-head"
    # The basis is provenance, carried in the raw bytes only -- never inside the
    # strictly-validated normalized payload (IdentityPayload forbids extra keys).
    assert "knowable_at_basis" not in record.payload


def test_build_route_falls_back_to_the_partition_start_without_a_universe_head() -> None:
    """The hand-curated TOPT corpus has no publication event; its only freshness signal
    is `report_date`, which the plan already equates with the partition start."""
    context = _context(universe_published_at=None)
    adapter = build_route(context, [_cell("wi-1", semantic_type="universe-membership")])
    record = adapter.targets["wi-1"]
    assert record.knowable_at == context.partition_start
    assert record.knowable_at_basis == "report-date"
    assert "knowable_at_basis" not in record.payload


def test_build_route_refuses_a_universe_head_published_after_the_cutoff() -> None:
    """The look-ahead guard stays intact on the new basis: a head published after the
    tick's own cutoff must be refused exactly like any other future fact."""
    item = _work_item("8" * 64)
    late_publish = datetime(2026, 4, 10, tzinfo=UTC)  # after _CUTOFF (2026-03-31)
    context = _context(universe_published_at=late_publish)
    adapter = build_route(context, [_cell(item.work_item_id)])
    result = adapter.fetch(item)
    assert result.reason_code is ObligationReasonCode.LOOK_AHEAD_VIOLATION


def test_build_routes_payload_still_satisfies_the_strict_identity_model() -> None:
    """Regression for the Copilot finding on #775: `knowable_at_basis` must never land
    inside `payload` -- `IdentityPayload` (materialization.py) validates that exact dict
    with `extra="forbid"`, and a stray key there would break snapshot materialization for
    every listing-identity/universe-membership row, not just this test's fixture."""
    from data_engine.datahub.production_topt.materialization import IdentityPayload

    for published_at in (datetime(2026, 2, 10, tzinfo=UTC), None):
        context = _context(universe_published_at=published_at)
        adapter = build_route(context, [_cell("wi-1")])
        IdentityPayload.model_validate(adapter.targets["wi-1"].payload)


def test_an_unknown_knowable_at_basis_is_refused_before_it_can_land() -> None:
    import pytest

    from data_engine.datahub.production_topt.release_derived_adapter import KNOWABLE_AT_BASES, ReleaseDerivedRecord

    assert KNOWABLE_AT_BASES == ("universe-head", "report-date")
    with pytest.raises(ValueError, match="knowable_at basis"):
        ReleaseDerivedRecord(
            semantic_type="listing-identity",
            subject_id="listing:xnas:aapl",
            payload={},
            knowable_at=datetime(2026, 3, 31, tzinfo=UTC),
            knowable_at_basis="universe-heaf",
        )
