from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from data_engine.datahub.production_topt.executor import FetchSuccess
from data_engine.datahub.production_topt.release_derived_adapter import (
    ReleaseDerivedAdapter,
    ReleaseDerivedRecord,
)
from truealpha_contracts import ObligationReasonCode
from truealpha_contracts.datahub import CaptureWorkItem

_CUTOFF = date(2026, 3, 31)
_KNOWN = datetime(2026, 3, 1, tzinfo=UTC)


def _work_item(digest: str) -> CaptureWorkItem:
    return CaptureWorkItem(
        campaign_id="capture-campaign:" + "1" * 64,
        source_request_id="source-request:" + digest,
        schedule_policy_id="schedule-policy:" + "2" * 64,
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
