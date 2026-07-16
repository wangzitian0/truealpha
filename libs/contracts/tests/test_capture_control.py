from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from truealpha_contracts import SubjectKind, SubjectRef, UniverseRef
from truealpha_contracts.capture_control import CaptureCheckpoint, CaptureListVersion, CheckpointPhase

SHA = "a" * 64
AT = datetime(2026, 4, 1, tzinfo=UTC)


def _universe() -> UniverseRef:
    return UniverseRef(universe_id="universe:topt-us-2026-03-31", universe_version="v1", content_sha256=SHA)


def test_list_version_is_deterministic_and_keeps_share_classes_distinct() -> None:
    members = (
        SubjectRef(kind=SubjectKind.LISTING, id="listing:xnas:googl"),
        SubjectRef(kind=SubjectKind.LISTING, id="listing:xnas:goog"),
    )
    first = CaptureListVersion(universe=_universe(), members=members, effective_at=AT)
    replay = CaptureListVersion(universe=_universe(), members=tuple(reversed(members)), effective_at=AT)
    assert first == replay
    assert len(first.members) == 2


def test_list_version_rejects_duplicates_and_identity_drift() -> None:
    member = SubjectRef(kind=SubjectKind.LISTING, id="listing:xnas:nvda")
    with pytest.raises(ValidationError, match="duplicates"):
        CaptureListVersion(universe=_universe(), members=(member, member), effective_at=AT)
    with pytest.raises(ValidationError, match="canonical identity"):
        CaptureListVersion(
            list_version_id=f"list-version:{'b' * 64}", universe=_universe(), members=(member,), effective_at=AT
        )


def test_checkpoint_identity_is_append_only_sequence_grain() -> None:
    run_id = f"capture-run:{SHA}"
    obligation_id = f"list-obligation:{'b' * 64}"
    first = CaptureCheckpoint(
        run_id=run_id, sequence=1, phase=CheckpointPhase.PLANNED, completed_obligation_ids=(), recorded_at=AT
    )
    second = CaptureCheckpoint(
        run_id=run_id,
        sequence=2,
        phase=CheckpointPhase.RAW_LANDED,
        completed_obligation_ids=(obligation_id,),
        recorded_at=AT,
    )
    assert first.checkpoint_id != second.checkpoint_id
    with pytest.raises(ValidationError, match="timezone-aware"):
        CaptureCheckpoint(
            run_id=run_id,
            sequence=1,
            phase=CheckpointPhase.PLANNED,
            completed_obligation_ids=(),
            recorded_at=datetime(2026, 4, 1),
        )
