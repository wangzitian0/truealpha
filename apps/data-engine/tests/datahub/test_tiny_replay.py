import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from data_engine.datahub import (
    build_recapture_plan,
    execute_recapture,
    materialize_shared_provider_work,
    reject_out_of_order_attempt,
    replay_resume_scenarios,
    run_tiny_replay,
    select_recapture,
)

ROOT = Path(__file__).parents[1]
REPOSITORY_ROOT = ROOT.parents[2]
CORPUS = ROOT / "fixtures" / "capture_control" / "corpus.v1.json"
SCRIPT = REPOSITORY_ROOT / "apps" / "data-engine" / "scripts" / "run_capture_control.py"


def load_corpus() -> dict[str, object]:
    return json.loads(CORPUS.read_text())


def test_tiny_replay_is_deterministic_and_covers_the_frozen_strata() -> None:
    corpus = load_corpus()
    first = run_tiny_replay(corpus)
    replay = run_tiny_replay(corpus)

    assert first == replay
    assert first.list_count == 2
    assert first.obligation_count == 5
    assert first.shared_obligation_count == 2
    assert first.shared_provider_work_item_count == 1
    assert dict(first.attempt_counts) == {
        "bounded-terminal-failure": 3,
        "interrupted-then-success": 2,
        "rate-limit-then-success": 2,
        "success-first-attempt": 1,
    }
    assert first.raw_object_count == 1
    assert first.observation_event_count == 2
    assert first.terminal_obligation_count == first.obligation_count == 5
    assert set(first.terminal_states) == set(corpus["tiny_lists"][0]["expected_terminal_states"])
    assert first.source_calls == 0
    assert len(first.report_sha256) == 64


def test_shared_provider_work_keeps_both_obligation_bindings() -> None:
    corpus = load_corpus()
    shared = materialize_shared_provider_work(corpus)

    assert len(shared.bindings) == 2
    assert len({binding.obligation_id for binding in shared.bindings}) == 2
    assert {binding.work_item_id for binding in shared.bindings} == {shared.work_item.work_item_id}


def test_out_of_order_attempt_after_terminal_fails_closed() -> None:
    with pytest.raises(ValueError, match="terminal"):
        reject_out_of_order_attempt(load_corpus())


def test_every_resume_checkpoint_replays_without_an_extra_append() -> None:
    corpus = load_corpus()
    results = replay_resume_scenarios(corpus)
    assert [result.scenario_id for result in results] == [row["scenario_id"] for row in corpus["resume_scenarios"]]
    assert [result.expected_resume for result in results] == [
        row["expected_resume"] for row in corpus["resume_scenarios"]
    ]
    assert all(result.append_count > 0 for result in results)
    assert all(result.replay_append_count == 0 for result in results)
    assert len({result.checkpoint_id for result in results}) == len(results)


def test_resume_requires_persisted_checkpoint_and_artifact_records() -> None:
    corpus = deepcopy(load_corpus())
    del corpus["resume_scenarios"][0]["persisted_records"]
    with pytest.raises(ValueError, match="persisted checkpoint and artifact records"):
        replay_resume_scenarios(corpus)


def test_recapture_execution_equals_the_frozen_dry_run() -> None:
    corpus = load_corpus()
    plan = build_recapture_plan(corpus)
    assert dict(plan.predicates) == corpus["recapture_scenarios"][0]["predicates"]
    assert (
        len(plan.contract_plan.predicate.model_dump(exclude={"predicate_id", "content_sha256"}, exclude_defaults=True))
        == 10
    )
    assert execute_recapture(plan, plan.selected_obligation_ids) == plan.selected_obligation_ids
    with pytest.raises(ValueError, match="differs"):
        execute_recapture(plan, ())


def test_recapture_selection_uses_state_as_of_the_frozen_cutoff() -> None:
    corpus = load_corpus()
    scenario = corpus["recapture_scenarios"][0]
    cutoff = datetime.fromisoformat(scenario["selection_cutoff"].replace("Z", "+00:00"))

    selected = select_recapture(corpus, scenario["predicates"], selection_cutoff=cutoff)
    assert tuple(item.obligation_id for item in selected) == tuple(
        item["obligation_id"] for item in scenario["expected_selected_obligations"]
    )
    with pytest.raises(ValueError, match="empty_recapture_selection"):
        select_recapture(corpus, scenario["predicates"], selection_cutoff=cutoff + timedelta(seconds=2))
    with pytest.raises(ValueError, match="timezone-aware"):
        select_recapture(corpus, scenario["predicates"], selection_cutoff=datetime(2026, 4, 1))


def test_recapture_empty_or_mutable_selection_fails_closed() -> None:
    corpus = load_corpus()
    with pytest.raises(ValueError, match="empty_recapture_selection"):
        select_recapture(corpus, corpus["recapture_scenarios"][1]["predicates"])
    with pytest.raises(ValueError, match="unbounded_or_mutable"):
        select_recapture(corpus, corpus["recapture_scenarios"][2]["predicates"])


def test_cli_requires_the_exact_frozen_corpus_hash() -> None:
    expected_sha256 = hashlib.sha256(CORPUS.read_bytes()).hexdigest()
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--corpus",
            str(CORPUS),
            "--expected-sha256",
            expected_sha256,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    output = json.loads(completed.stdout)
    assert output == run_tiny_replay(load_corpus()).as_dict()

    rejected = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--corpus",
            str(CORPUS),
            "--expected-sha256",
            "0" * 64,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "SHA-256 mismatch" in rejected.stderr
