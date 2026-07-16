import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from data_engine.datahub import AttemptLedger, expand_obligations
from truealpha_contracts import UniverseRef
from truealpha_contracts.datahub import FetchAttemptOutcome

ROOT = Path(__file__).parents[1]
CORPUS = ROOT / "fixtures" / "capture_control" / "corpus.v1.json"
AT = datetime(2026, 4, 1, tzinfo=UTC)


def test_frozen_topt_denominator_expands_without_share_class_collapse() -> None:
    corpus = json.loads(CORPUS.read_text())
    denominator = corpus["topt_denominator"]
    listings = tuple(row[2] for row in denominator["instruments"])
    obligations = expand_obligations(
        run_id=f"capture-run:{'a' * 64}",
        universe=UniverseRef(
            universe_id=denominator["universe_id"],
            universe_version="topt-candidate-2026-03-31-v1",
            content_sha256="8b2f885e6161c01603b9d78882d411c7984ff6a3dbf35d636cb11e8c2ecfcf8f",
        ),
        listings=listings,
        semantic_types=tuple(denominator["obligation_expansion"]["semantic_types"]),
        partition=denominator["report_date"],
    )
    assert len(set(listings)) == 21
    assert len({row[0] for row in denominator["instruments"]}) == 20
    assert len(obligations) == denominator["obligation_count"] == 84
    assert {item.subject.id for item in obligations} >= {"listing:xnas:goog", "listing:xnas:googl"}


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
