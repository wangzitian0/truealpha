"""#1416: Environment contract SSOT, generated TypeScript module, and sentinel known answers."""

from __future__ import annotations

import runpy
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from truealpha_contracts.environment_contract import (
    CodeServerCapacity,
    EnvironmentContract,
    JobRunTimeBasis,
    load_environment_contract,
    load_sentinel_known_answers,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
_EXPORT_SCRIPT = Path(__file__).resolve().parents[1] / "conformance" / "export_environment_contract.py"
_GENERATED_TS = REPO_ROOT / "apps" / "app-web" / "src" / "contracts" / "environmentContract.generated.ts"
_SERVICE_OBJECTIVES_TS = REPO_ROOT / "apps" / "app-web" / "src" / "contracts" / "serviceObjectives.ts"


def test_environment_contract_loads_ten_sentinel_listings_across_nine_issuers() -> None:
    contract = load_environment_contract()
    assert len(contract.sentinels) == 10
    issuers = {member.issuer_id for member in contract.sentinels}
    assert len(issuers) == 9
    tickers = [member.ticker for member in contract.sentinels]
    assert tickers == [
        "AAPL",
        "GOOGL",
        "GOOG",
        "ASML",
        "HBAN",
        "CINF",
        "JPM",
        "NVDA",
        "BRK.B",
        "CRWD",
    ]


@pytest.mark.parametrize(
    ("universe_id", "environment", "expected"),
    [
        ("universe:topt-us-2026-03-31", "production", True),
        ("universe:topt-us-2026-03-31", "staging", True),
        ("universe:qqq-us-2026-09-30", "production", True),
        ("universe:qqq-us-2026-09-30", "staging", False),
        ("universe:sp500-us-2026-09-30", "production", False),
        ("universe:sp500-us-2026-09-30", "staging", False),
        ("universe:canary-us-2026-09-30", "production", True),
        ("universe:canary-us-2026-09-30", "staging", True),
        ("universe:unknown-us-2026-09-30", "staging", True),
        ("universe:qqq-us-2026-09-30", "unknown-env", True),
    ],
)
def test_contract_is_head_scheduled_matches_declared_ticks(universe_id: str, environment: str, expected: bool) -> None:
    contract = load_environment_contract()
    assert contract.is_head_scheduled(universe_id, environment) is expected


def test_generated_typescript_environment_contract_equals_fresh_render(monkeypatch: pytest.MonkeyPatch) -> None:
    exporter = runpy.run_path(str(_EXPORT_SCRIPT))
    rendered = exporter["render"]()
    assert _GENERATED_TS.read_text(encoding="utf-8") == rendered
    monkeypatch.setattr("sys.argv", [str(_EXPORT_SCRIPT), "--check"])
    assert exporter["main"]() == 0

    objectives_text = _SERVICE_OBJECTIVES_TS.read_text(encoding="utf-8")
    assert "isHeadScheduledFromContract" in objectives_text
    assert 'universeId.startsWith("universe:qqq-")' not in objectives_text


def test_derived_vendor_shares_sum_within_hundred_percent_for_every_shared_seat() -> None:
    contract = load_environment_contract()
    assert set(contract.shared_physical_seats) == {"twelvedata", "sec", "yahoo", "moomoo"}
    for seat in contract.shared_physical_seats:
        shares = contract.derived_environment_shares(seat)
        assert shares, f"{seat} must derive non-empty shares"
        share_map = dict(shares)
        assert set(share_map) == {"production", "staging"}
        assert all(0 < pct <= 100 for pct in share_map.values())
        assert sum(share_map.values()) <= 100
    assert dict(contract.derived_environment_shares("twelvedata")) == {"production": 60, "staging": 40}


def test_sentinel_known_answers_cover_all_nine_sentinel_issuers_with_accessions() -> None:
    contract = load_environment_contract()
    catalog = load_sentinel_known_answers()
    sentinel_issuers = {member.issuer_id for member in contract.sentinels}
    answered_issuers = {answer.issuer_id for answer in catalog.answers}
    assert sentinel_issuers <= answered_issuers, (
        f"every sentinel issuer must have at least one known answer; missing {sentinel_issuers - answered_issuers}"
    )
    for answer in catalog.answers:
        assert answer.accession, f"{answer.answer_id} lacks an accession"
        assert answer.source_citation, f"{answer.answer_id} lacks a citation"
    neg_tickers = {entry.ticker for entry in catalog.negative_theme_classifications}
    assert {"SBUX", "PANW", "INTU"} <= neg_tickers
    for entry in catalog.negative_theme_classifications:
        assert Decimal(entry.max_theme_share) == Decimal("0.00")


def test_docs_point_to_environment_contract_and_drop_stale_copies() -> None:
    init_text = (REPO_ROOT / "init.md").read_text(encoding="utf-8")
    vision_text = (REPO_ROOT / "vision.md").read_text(encoding="utf-8")
    release_text = (REPO_ROOT / "docs" / "release-protocol.md").read_text(encoding="utf-8")

    assert "environment_contract.v1.json" in init_text
    assert "| Local | `local_dev`, `local_test` |" not in init_text
    assert "TWELVE_DATA_ENVIRONMENT_SHARES" not in init_text

    assert "environment_contract.v1.json" in vision_text
    assert "S&P 500" in vision_text

    assert "waits\n20 minutes" not in release_text
    assert "daily cap (4," not in release_text


def _contract_with_jobs(jobs: dict[str, dict[str, object]]) -> EnvironmentContract:
    """The real contract with margin 2, a 10-minute floor and the given `run_time_bounds.jobs` (#1442)."""
    contract = load_environment_contract()
    parsed = {name: JobRunTimeBasis.model_validate(basis) for name, basis in jobs.items()}
    bounds = contract.run_time_bounds.model_copy(update={"jobs": parsed, "margin": 2.0, "floor_minutes": 10.0})
    return contract.model_copy(update={"run_time_bounds": bounds})


def test_max_runtime_is_margin_times_the_longest_success_over_all_environments() -> None:
    contract = _contract_with_jobs(
        {
            "both": {
                "measured": {
                    "production": {"successes": 90, "longest_minutes": 16.3},
                    "staging": {"successes": 98, "longest_minutes": 22.5},
                }
            },
            "short": {"measured": {"production": {"successes": 4, "longest_minutes": 0.5}}},
            "never_ran": {"estimate_tick": "sp500"},
            "new": {"unmeasured": "no run yet"},
        }
    )
    assert contract.max_runtime_seconds("both") == 2 * 22.5 * 60
    assert contract.max_runtime_seconds("short") == 10 * 60
    # The largest declared sp500 listing count is 503: (2.1 + 0.434 x 503) x 2 minutes, rounded up.
    assert contract.max_runtime_seconds("never_ran") == 26449
    assert contract.max_runtime_seconds("new") is None
    assert contract.max_runtime_seconds("absent") is None


@pytest.mark.parametrize(
    "basis",
    [
        {},
        {"estimate_tick": "sp500", "unmeasured": "no run yet"},
        {"measured": {"staging": {"successes": 1, "longest_minutes": 1.0}}, "estimate_tick": "sp500"},
        {"measured": {"staging": {"successes": 0, "longest_minutes": 1.0}}},
        {"measured": {"staging": {"successes": 1, "longest_minutes": 1.0}}, "unmeasured": ""},
        {"measured": {}, "estimate_tick": "sp500"},
        {"unmeasured": ""},
    ],
)
def test_a_job_declares_exactly_one_valid_run_time_basis(basis: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        JobRunTimeBasis.model_validate(basis)


def _capacity(**changes: object) -> CodeServerCapacity:
    """The real code server capacity with `changes` applied, validated again (#1467)."""
    declared = load_environment_contract().code_server_capacity.model_dump()
    return CodeServerCapacity.model_validate({**declared, **changes})


def test_the_run_limit_is_the_runs_that_fit_beside_the_server_inside_the_utilization_limit() -> None:
    """1536 MiB x 0.7 = 1075 MiB. The server holds 240 MiB. Three runs of 240 MiB fit in the other 835 MiB."""
    capacity = load_environment_contract().code_server_capacity

    assert capacity.usable_mib == 1075
    assert capacity.max_concurrent_runs == 3
    assert capacity.max_waiting_runs == 2
    held = (
        capacity.server_processes + capacity.max_concurrent_runs * capacity.processes_per_run
    ) * capacity.process_mib
    assert held <= capacity.usable_mib < held + capacity.processes_per_run * capacity.process_mib, (
        "the limit is the largest one that fits: one more run would pass the utilization limit"
    )


@pytest.mark.parametrize(
    ("changes", "runs"),
    [
        ({"ceiling_mib": 2048}, 4),
        ({"ceiling_mib": 1280}, 2),
        ({"process_mib": 300, "ceiling_mib": 2048}, 3),
        ({"processes_per_run": 2, "ceiling_mib": 2048}, 2),
        ({"server_processes": 2, "ceiling_mib": 2048}, 3),
        ({"utilization_limit": 0.5, "ceiling_mib": 2048}, 3),
    ],
)
def test_the_run_limit_follows_each_input_of_the_derivation(changes: dict[str, object], runs: int) -> None:
    assert _capacity(**changes).max_concurrent_runs == runs


@pytest.mark.parametrize(
    "changes",
    [
        {"utilization_limit": 0.71},
        {"utilization_limit": 1.0},
        {"process_mib": 229},
        {"ceiling_mib": 768},
        {"ceiling_mib": 0},
    ],
    ids=[
        "above-the-70-percent-rule",
        "full-utilization",
        "below-the-measured-peak",
        "ceiling-admits-one-run",
        "no-ceiling",
    ],
)
def test_a_capacity_that_breaks_a_rule_is_refused(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _capacity(**changes)
