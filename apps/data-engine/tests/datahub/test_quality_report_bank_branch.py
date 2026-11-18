"""#1664: the complete-listing query reads the branch from the snapshot payload member shape."""

from __future__ import annotations

from data_engine.datahub.capture_core.materialization import SnapshotMember
from data_engine.datahub.quality_report import _CORE_COMPLETE_LISTINGS_SQL


def test_complete_listing_sql_reads_operating_branch_from_the_payload_member_key() -> None:
    assert "operating_branch" in SnapshotMember.model_fields
    assert "factor_input" not in SnapshotMember.model_fields
    assert "member->>'operating_branch'" in _CORE_COMPLETE_LISTINGS_SQL
    assert "factor_input" not in _CORE_COMPLETE_LISTINGS_SQL
