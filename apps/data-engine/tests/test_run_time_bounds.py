"""Every deployed Dagster job derives its maximum runtime bound from dagster.runs history (#1442, #1522).

The environment contract holds the policy parameters (margin, floor_minutes, window_days).
The run launcher queries SUCCESS runs in dagster.runs over the window and sets margin x longest
successful run (at least floor_minutes). When a job has no successful run in the window, no tag
is written and the instance limit (3 h, dagster.yaml) applies. An operator's bound is preserved.
"""

from __future__ import annotations

import sys
from pathlib import Path

import dagster as dg
from dagster._core.storage.tags import MAX_RUNTIME_SECONDS_TAG
from data_engine.run_launcher import CONTRACT_BOUND_TAG, MAX_RUNTIME_ALIAS_TAG, bound_tags_to_write, max_runtime_tags

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
from run_seed import seed_success_run  # noqa: E402
from truealpha_contracts.environment_contract import load_environment_contract


def test_job_with_no_success_run_writes_no_tag() -> None:
    """A job without a SUCCESS run in the window writes no tag; the instance limit applies (#1522)."""
    instance = dg.DagsterInstance.ephemeral()
    job_name = "brand_new_pipeline"

    assert max_runtime_tags(job_name, instance=instance) == {}
    assert bound_tags_to_write(job_name, {}, instance=instance) == {}


def test_tag_written_is_margin_times_longest_success_respecting_floor() -> None:
    """Bound is margin x longest SUCCESS run, and never below floor_minutes (#1442, #1522)."""
    instance = dg.DagsterInstance.ephemeral()
    policy = load_environment_contract().job_runtime_bounds
    job_name = "test_pipeline"

    # Short run of 60s: 60 x margin (2) = 120s < floor (10 min = 600s) -> floor applies
    seed_success_run(instance, job_name, 60.0)
    floor_seconds = int(policy.floor_minutes * 60)
    tags = max_runtime_tags(job_name, instance=instance)
    assert tags == {
        MAX_RUNTIME_SECONDS_TAG: str(floor_seconds),
        CONTRACT_BOUND_TAG: str(floor_seconds),
    }

    # Longer run of 500s: 500 x 2 = 1000s > 600s -> margin x duration applies
    seed_success_run(instance, job_name, 500.0)
    expected_bound = int(policy.margin * 500.0)
    tags = max_runtime_tags(job_name, instance=instance)
    assert tags == {
        MAX_RUNTIME_SECONDS_TAG: str(expected_bound),
        CONTRACT_BOUND_TAG: str(expected_bound),
    }


def test_slower_success_run_raises_the_bound() -> None:
    """A slower SUCCESS run raises the derived runtime bound (#1522)."""
    instance = dg.DagsterInstance.ephemeral()
    policy = load_environment_contract().job_runtime_bounds
    job_name = "scaling_pipeline"

    seed_success_run(instance, job_name, 400.0)
    bound_1 = int(max_runtime_tags(job_name, instance=instance)[MAX_RUNTIME_SECONDS_TAG])
    assert bound_1 == int(policy.margin * 400.0)

    # A faster subsequent run does not lower the bound
    seed_success_run(instance, job_name, 200.0)
    bound_2 = int(max_runtime_tags(job_name, instance=instance)[MAX_RUNTIME_SECONDS_TAG])
    assert bound_2 == bound_1

    # A slower subsequent run raises the bound
    seed_success_run(instance, job_name, 700.0)
    bound_3 = int(max_runtime_tags(job_name, instance=instance)[MAX_RUNTIME_SECONDS_TAG])
    assert bound_3 == int(policy.margin * 700.0)
    assert bound_3 > bound_1


def test_operator_explicit_max_runtime_tag_is_preserved() -> None:
    """An operator's explicit tag (or alias) is kept and never overwritten (#1442, #1522)."""
    instance = dg.DagsterInstance.ephemeral()
    job_name = "operator_pipeline"
    seed_success_run(instance, job_name, 500.0)

    # Standard explicit operator tag
    operator_tags = {MAX_RUNTIME_SECONDS_TAG: "86400"}
    assert bound_tags_to_write(job_name, operator_tags, instance=instance) == {}

    # Alias operator tag
    alias_tags = {MAX_RUNTIME_ALIAS_TAG: "86400"}
    assert bound_tags_to_write(job_name, alias_tags, instance=instance) == {}

    # Re-execution with previously generated contract bound gets renewed
    contract_bound = max_runtime_tags(job_name, instance=instance)[MAX_RUNTIME_SECONDS_TAG]
    re_exec_tags = {MAX_RUNTIME_SECONDS_TAG: contract_bound, CONTRACT_BOUND_TAG: contract_bound}
    assert bound_tags_to_write(job_name, re_exec_tags, instance=instance) == {
        MAX_RUNTIME_SECONDS_TAG: contract_bound,
        CONTRACT_BOUND_TAG: contract_bound,
    }
