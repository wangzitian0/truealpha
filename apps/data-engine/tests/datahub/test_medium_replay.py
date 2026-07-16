import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from data_engine.datahub import run_topt_medium_replay

ROOT = Path(__file__).parents[1]
REPOSITORY_ROOT = Path(__file__).parents[4]
CORPUS = ROOT / "fixtures" / "capture_control" / "corpus.v1.json"
SCRIPT = REPOSITORY_ROOT / "apps" / "data-engine" / "scripts" / "run_capture_control.py"


def load_corpus() -> dict[str, object]:
    return json.loads(CORPUS.read_text())


def test_topt_medium_replay_is_deterministic_and_complete() -> None:
    first = run_topt_medium_replay(load_corpus())
    replay = run_topt_medium_replay(load_corpus())

    assert first == replay
    assert (first.issuer_count, first.instrument_count, first.obligation_count_per_run) == (20, 21, 84)
    assert (first.run_count, first.cutoff_count, first.source_vintage_count) == (3, 3, 168)
    assert first.total_obligation_count == first.total_binding_count == first.total_terminal_obligation_count == 252
    assert first.total_work_item_count == first.total_attempt_count == 252
    assert dict(first.semantic_obligation_counts) == {
        "financial-fact": 63,
        "listing-identity": 63,
        "market-price": 63,
        "universe-membership": 63,
    }
    assert dict(first.terminal_state_counts) == {"success": 168, "unchanged": 84}
    assert first.goog_instrument_id != first.googl_instrument_id
    assert first.source_calls == 0
    assert len(first.report_sha256) == 64


def test_every_medium_run_uses_the_exact_topt_denominator() -> None:
    report = run_topt_medium_replay(load_corpus())

    assert len({summary.campaign_id for summary in report.run_summaries}) == 3
    assert len({summary.run_id for summary in report.run_summaries}) == 3
    assert all(summary.obligation_count == 84 for summary in report.run_summaries)
    assert all(summary.work_item_count == 84 for summary in report.run_summaries)
    assert all(summary.binding_count == 84 for summary in report.run_summaries)
    assert all(summary.attempt_count == 84 for summary in report.run_summaries)
    assert all(summary.terminal_obligation_count == 84 for summary in report.run_summaries)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda corpus: corpus["topt_denominator"]["instruments"].pop(), "instrument denominator shrink"),
        (
            lambda corpus: corpus["topt_denominator"]["instruments"].append(
                corpus["topt_denominator"]["instruments"][0]
            ),
            "instrument denominator shrink",
        ),
        (
            lambda corpus: corpus["topt_denominator"]["obligation_expansion"]["semantic_types"].append("market-price"),
            "semantic denominator drift",
        ),
    ),
)
def test_topt_medium_replay_rejects_denominator_drift(
    mutation: Callable[[dict[str, Any]], object], message: str
) -> None:
    corpus = deepcopy(load_corpus())
    mutation(corpus)
    with pytest.raises(ValueError, match=message):
        run_topt_medium_replay(corpus)


def test_cli_runs_the_medium_replay_against_exact_corpus_bytes() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--corpus",
            str(CORPUS),
            "--expected-sha256",
            hashlib.sha256(CORPUS.read_bytes()).hexdigest(),
            "--replay",
            "medium",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == run_topt_medium_replay(load_corpus()).as_dict()
