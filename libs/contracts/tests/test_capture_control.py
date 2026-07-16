from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from truealpha_contracts import SubjectKind, SubjectRef, UniverseRef
from truealpha_contracts.capture_control import (
    CaptureCheckpoint,
    CaptureListObligation,
    CaptureListVersion,
    CaptureObligationWorkBinding,
    CaptureRecapturePlan,
    CheckpointPhase,
)
from truealpha_contracts.datahub import ListObligation, RecapturePredicate

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


def test_list_version_preserves_six_digit_fractional_timestamp_identity() -> None:
    version = CaptureListVersion(
        universe=UniverseRef(
            universe_id="universe:topt-us-2026-03-31",
            universe_version="topt-sql-contract-v1",
            content_sha256="8" * 64,
        ),
        members=(SubjectRef(kind=SubjectKind.LISTING, id="listing:xnas:goog"),),
        effective_at=datetime(2026, 4, 1, 0, 0, 0, 123000, tzinfo=UTC),
    )
    assert version.list_version_id == ("list-version:0d312e5b25aa8a450ffefa2c039a1286a035093c3f9a7ef5dc4f47f31cd971a8")


def test_list_version_preserves_non_utc_offset_identity() -> None:
    version = CaptureListVersion(
        universe=UniverseRef(
            universe_id="universe:topt-us-2026-03-31",
            universe_version="topt-sql-contract-v1",
            content_sha256="8" * 64,
        ),
        members=(SubjectRef(kind=SubjectKind.LISTING, id="listing:xnas:goog"),),
        effective_at=datetime(2026, 4, 1, 8, tzinfo=timezone(timedelta(hours=8))),
    )
    assert version.list_version_id == ("list-version:503cdf7ca54bc8f7873993cc1dd1ad6ce9105ade759640c041eb145130bff3ef")


def test_d5_recapture_plan_preserves_non_utc_offset_identity() -> None:
    plan = CaptureRecapturePlan(
        selection_cutoff=datetime(2026, 4, 1, 8, tzinfo=timezone(timedelta(hours=8))),
        predicate=RecapturePredicate(subject_ids=("listing:xnas:goog",)),
        selected_obligation_ids=(
            "capture-list-obligation:261a5d6ccd4e326894c240a932c9fdb0f892bdbebfd991012c4243b0210294e6",
        ),
        planner_version="capture-planner:v1",
    )
    assert plan.plan_id == (
        "capture-list-recapture-plan:8ed9769976a46f9ed782c34372ce36977db207c0d343381390d39faabfb3eb33"
    )


@pytest.mark.parametrize(
    "planner_version", ("latest", "planner:default", "planner:stable", "planner:main", "capture_planner_latest_v1")
)
def test_d5_recapture_plan_rejects_mutable_planner_versions(planner_version: str) -> None:
    with pytest.raises(ValidationError, match="must not be mutable"):
        CaptureRecapturePlan(
            selection_cutoff=AT,
            predicate=RecapturePredicate(subject_ids=("listing:xnas:goog",)),
            selected_obligation_ids=(f"capture-list-obligation:{'b' * 64}",),
            planner_version=planner_version,
        )


def test_capture_obligation_identity_preserves_list_version() -> None:
    obligation = ListObligation(
        run_id=f"capture-run:{SHA}",
        universe_ref=_universe(),
        subject=SubjectRef(kind=SubjectKind.SECURITY, id="security:cusip:67066G104"),
        capture_requirement_id="market-price:v1",
        partition="2026-03-31",
    )
    primary = CaptureListObligation(list_version_id=f"list-version:{'b' * 64}", obligation=obligation)
    overlap = CaptureListObligation(list_version_id=f"list-version:{'c' * 64}", obligation=obligation)
    assert primary.obligation == overlap.obligation
    assert primary.obligation_id != overlap.obligation_id


def test_capture_binding_uses_the_list_bound_obligation_namespace() -> None:
    binding = CaptureObligationWorkBinding(
        obligation_id=f"capture-list-obligation:{'b' * 64}",
        work_item_id=f"capture-work-item:{'c' * 64}",
    )
    assert binding.binding_id.startswith("capture-obligation-work-binding:")
    with pytest.raises(ValidationError):
        CaptureObligationWorkBinding(
            obligation_id=f"list-obligation:{'b' * 64}",
            work_item_id=f"capture-work-item:{'c' * 64}",
        )


def test_checkpoint_identity_is_append_only_sequence_grain() -> None:
    run_id = f"capture-run:{SHA}"
    obligation_id = f"capture-list-obligation:{'b' * 64}"
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
    with pytest.raises(ValidationError, match="canonical identities"):
        CaptureCheckpoint(
            run_id=run_id,
            sequence=3,
            phase=CheckpointPhase.NORMALIZED,
            completed_obligation_ids=("capture-list-obligation:not-a-hash",),
            recorded_at=AT,
        )
