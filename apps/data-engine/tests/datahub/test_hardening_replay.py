import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from data_engine.datahub import run_topt_hardening_replay

ROOT = Path(__file__).parents[1]
REPOSITORY_ROOT = Path(__file__).parents[4]
CORPUS = ROOT / "fixtures" / "capture_control" / "corpus.v1.json"
SCRIPT = REPOSITORY_ROOT / "apps" / "data-engine" / "scripts" / "run_capture_control.py"


def load_corpus() -> dict[str, object]:
    return json.loads(CORPUS.read_text())


def test_hardening_replay_closes_the_exact_topt_control_plane() -> None:
    first = run_topt_hardening_replay(load_corpus())
    replay = run_topt_hardening_replay(load_corpus())

    assert first.identity_dict() == replay.identity_dict()
    assert first.identity_sha256 == replay.identity_sha256
    assert (first.issuer_count, first.instrument_count, first.obligation_count_per_run) == (20, 21, 84)
    assert first.total_terminal_obligation_count == 252
    assert first.denominator_completeness_ppm == 1_000_000
    assert first.recapture_selection_count > 0
    assert first.resume_checkpoint_count == 4
    assert first.negative_controls == (
        "collision_rejected",
        "denominator_shrink_rejected",
        "out_of_order_attempt_rejected",
        "parser_failure_terminalized",
        "partial_write_rejected",
        "rate_limit_recovered_within_retry_budget",
        "recapture_overreach_rejected",
        "resume_is_idempotent",
    )
    assert first.source_calls == 0
    assert len(first.resource_metric_semantics) == 3
    assert [metric.scope_kind for metric in first.scope_metrics] == ["list", "campaign", "campaign", "campaign"]
    assert all(metric.denominator_completeness_ppm == 1_000_000 for metric in first.scope_metrics)
    assert all(metric.retry_amplification_ppm == 1_000_000 for metric in first.scope_metrics)
    assert all(
        metric.overfetch_count == metric.provider_calls == metric.source_cost_microunits == 0
        for metric in first.scope_metrics
    )
    assert [metric.freshness_age_seconds for metric in first.scope_metrics] == [86_400, 0, 0, 86_400]


def test_hardening_resource_observation_stays_inside_every_frozen_ceiling() -> None:
    report = run_topt_hardening_replay(load_corpus())
    observation = report.resource_observation.as_dict()
    ceilings = report.resource_ceilings.as_dict()

    for field, ceiling in ceilings.items():
        assert observation[field] <= ceiling
    assert observation["throughput_milli_obligations_per_second"] > 0
    assert observation["overfetch_count"] == 0


def test_hardening_replay_fails_closed_on_partial_persisted_state() -> None:
    corpus = deepcopy(load_corpus())
    corpus["resume_scenarios"][2]["persisted_records"]["observation_obligation_ordinals"] = []

    with pytest.raises(ValueError, match="normalized checkpoint is missing"):
        run_topt_hardening_replay(corpus)


def test_cli_exposes_hardening_without_changing_its_identity() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--corpus",
            str(CORPUS),
            "--expected-sha256",
            hashlib.sha256(CORPUS.read_bytes()).hexdigest(),
            "--replay",
            "hardening",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    output = json.loads(completed.stdout)
    expected = run_topt_hardening_replay(load_corpus())

    assert output["identity_sha256"] == expected.identity_sha256
    assert output["resource_ceilings"] == expected.resource_ceilings.as_dict()
    assert output["source_calls"] == 0
