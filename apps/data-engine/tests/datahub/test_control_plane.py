import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from data_engine.datahub import AttemptLedger, expand_obligations
from truealpha_contracts import SubjectKind, SubjectRef, UniverseRef
from truealpha_contracts.capture_control import CaptureListVersion
from truealpha_contracts.datahub import FetchAttemptOutcome

ROOT = Path(__file__).parents[1]
CORPUS = ROOT / "fixtures" / "capture_control" / "corpus.v1.json"
AT = datetime(2026, 4, 1, tzinfo=UTC)


def test_frozen_topt_denominator_expands_without_share_class_collapse() -> None:
    corpus = json.loads(CORPUS.read_text())
    denominator = corpus["topt_denominator"]
    listings = tuple(row[2] for row in denominator["instruments"])
    list_version = CaptureListVersion(
        universe=UniverseRef(
            universe_id=denominator["universe_id"],
            universe_version="topt-candidate-2026-03-31-v1",
            content_sha256="8b2f885e6161c01603b9d78882d411c7984ff6a3dbf35d636cb11e8c2ecfcf8f",
        ),
        members=tuple(SubjectRef(kind=SubjectKind.LISTING, id=listing) for listing in listings),
        effective_at=AT,
    )
    assert list_version.list_version_id == denominator["list_version_id"]
    obligations = expand_obligations(
        run_id=f"capture-run:{'a' * 64}",
        list_version=list_version,
        semantic_types=tuple(denominator["obligation_expansion"]["semantic_types"]),
        partition=denominator["report_date"],
    )
    assert len(set(listings)) == 21
    assert len({row[0] for row in denominator["instruments"]}) == 20
    assert len(obligations) == denominator["obligation_count"] == 84
    assert {item.subject.id for item in obligations} >= {"listing:xnas:goog", "listing:xnas:googl"}


def test_frozen_tiny_list_ids_reconstruct_from_security_members() -> None:
    corpus = json.loads(CORPUS.read_text())
    universe = UniverseRef(
        universe_id=corpus["topt_denominator"]["universe_id"],
        universe_version="topt-candidate-2026-03-31-v1",
        content_sha256="8b2f885e6161c01603b9d78882d411c7984ff6a3dbf35d636cb11e8c2ecfcf8f",
    )
    for frozen in corpus["tiny_lists"]:
        reconstructed = CaptureListVersion(
            universe=universe,
            members=tuple(SubjectRef(kind=SubjectKind.SECURITY, id=member) for member in frozen["members"]),
            effective_at=AT,
        )
        assert reconstructed.list_version_id == frozen["list_version_id"]


def test_attempts_are_contiguous_bounded_and_stop_at_terminal_outcome() -> None:
    ledger = AttemptLedger(work_item_id=f"capture-work-item:{'b' * 64}", maximum_attempts=3)
    first = ledger.start(started_at=AT)
    ledger.finish(attempt=first, completed_at=AT, outcome=FetchAttemptOutcome.INTERRUPTED, error_code="worker_exit")
    second = ledger.start(started_at=AT)
    ledger.finish(
        attempt=second,
        completed_at=AT,
        outcome=FetchAttemptOutcome.SUCCESS,
        source_vintage_id=f"source-vintage:{'d' * 64}",
    )
    assert [attempt.attempt_number for attempt in ledger.attempts] == [1, 2]
    with pytest.raises(ValueError, match="terminal"):
        ledger.start(started_at=AT)


def test_attempt_result_cannot_be_replaced_or_duplicated() -> None:
    ledger = AttemptLedger(work_item_id=f"capture-work-item:{'c' * 64}", maximum_attempts=1)
    attempt = ledger.start(started_at=AT)
    ledger.finish(attempt=attempt, completed_at=AT, outcome=FetchAttemptOutcome.FAILED, error_code="fixture_failure")
    with pytest.raises(ValueError, match="already has a result"):
        ledger.finish(attempt=attempt, completed_at=AT, outcome=FetchAttemptOutcome.SUCCESS)


def test_attempt_dispatch_waits_for_result_and_completion_is_monotonic() -> None:
    ledger = AttemptLedger(work_item_id=f"capture-work-item:{'e' * 64}", maximum_attempts=2)
    attempt = ledger.start(started_at=AT)
    with pytest.raises(ValueError, match="no result"):
        ledger.start(started_at=AT)
    with pytest.raises(ValueError, match="precedes"):
        ledger.finish(
            attempt=attempt,
            completed_at=datetime(2026, 3, 31, tzinfo=UTC),
            outcome=FetchAttemptOutcome.INTERRUPTED,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        ledger.finish(
            attempt=attempt,
            completed_at=datetime(2026, 4, 1),
            outcome=FetchAttemptOutcome.INTERRUPTED,
        )


def test_attempt_budget_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        AttemptLedger(work_item_id=f"capture-work-item:{'f' * 64}", maximum_attempts=0)
