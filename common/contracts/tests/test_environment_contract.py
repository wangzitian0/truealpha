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


#: A fixed capacity for the tests of the derivation. It does not read the contract, so a new
#: measurement in the contract cannot change what these tests expect of the formula.
_BASE_CAPACITY: dict[str, object] = {
    "description": "test",
    "ceiling_mib": 1536,
    "ceiling_source": "test",
    "utilization_limit": 0.7,
    "server_mib": 240,
    "concurrent_runs": 2,
    "server_processes": 1,
    "processes_per_run": 1,
    "measured": {
        "at": "2026-10-21",
        "environment": "staging",
        "method": "test",
        "grpc_server_hwm_mib": 236.0,
        "fresh_import_hwm_mib": 180.0,
        "working_run_hwm_mib": 380.0,
        "container_peak_mib": 1186.0,
        "runs_at_peak": 3,
        "waiting_runs_at_peak": 2,
        "source": "test",
    },
}


def _capacity(**changes: object) -> CodeServerCapacity:
    """The fixed capacity with `changes` applied, validated again (#1467)."""
    return CodeServerCapacity.model_validate({**_BASE_CAPACITY, **changes})


def test_the_declared_slots_leave_each_run_a_budget_that_fits_inside_the_utilization_limit() -> None:
    """Whatever the contract declares: the server and all slots at the budget fit; one more MiB per slot does not."""
    capacity = load_environment_contract().code_server_capacity
    slots = capacity.concurrent_runs * capacity.processes_per_run
    held = capacity.server_processes * capacity.server_mib + slots * capacity.run_mib

    assert capacity.usable_mib <= capacity.ceiling_mib * capacity.utilization_limit
    assert held <= capacity.usable_mib < held + slots, "the budget is the largest whole MiB per slot"
    assert capacity.max_concurrent_runs == capacity.concurrent_runs
    assert capacity.max_waiting_runs == capacity.concurrent_runs - 1 >= 1


def test_the_declared_server_and_budget_cover_what_was_measured() -> None:
    """The server cannot be smaller than it was read. A run cannot be smaller than a fresh import."""
    capacity = load_environment_contract().code_server_capacity
    measured = capacity.measured

    assert capacity.server_mib >= measured.grpc_server_hwm_mib
    assert capacity.run_mib >= measured.working_run_hwm_mib >= measured.fresh_import_hwm_mib
    assert measured.waiting_runs_at_peak < measured.runs_at_peak, "a peak with only waiting runs would prove no size"


def test_the_fixed_capacity_gives_two_slots_and_a_budget_of_417_mib() -> None:
    """1536 MiB x 0.7 = 1075 MiB. The server holds 240 MiB. The other 835 MiB go to two slots: 417 MiB each."""
    capacity = _capacity()

    assert (capacity.usable_mib, capacity.run_mib) == (1075, 417)
    assert (capacity.max_concurrent_runs, capacity.max_waiting_runs) == (2, 1)


@pytest.mark.parametrize(
    ("changes", "budget"),
    [
        ({"ceiling_mib": 2048}, 596),
        ({"ceiling_mib": 1429}, 380),
        ({"concurrent_runs": 3, "ceiling_mib": 2048}, 397),
        ({"concurrent_runs": 4, "ceiling_mib": 4096}, 656),
        ({"processes_per_run": 2, "ceiling_mib": 4096}, 656),
        ({"server_processes": 2, "ceiling_mib": 2048}, 476),
        ({"utilization_limit": 0.5, "ceiling_mib": 2048}, 392),
    ],
)
def test_the_run_budget_follows_each_input_of_the_derivation(changes: dict[str, object], budget: int) -> None:
    assert _capacity(**changes).run_mib == budget


@pytest.mark.parametrize(
    "changes",
    [
        {"utilization_limit": 0.71},
        {"utilization_limit": 1.0},
        {"concurrent_runs": 1},
        {"concurrent_runs": 3},
        {"concurrent_runs": 5},
        {"ceiling_mib": 1426},
        {"server_mib": 235},
        {"ceiling_mib": 768},
        {"ceiling_mib": 0},
    ],
    ids=[
        "above-the-70-percent-rule",
        "full-utilization",
        "one-slot-cannot-wait-for-another-run",
        "three-slots-do-not-fit-the-measured-working-run",
        "five-slots-do-not-fit-the-measured-working-run",
        "budget-one-mib-below-the-measured-working-run",
        "server-below-the-measured-peak",
        "ceiling-too-small-for-two-working-runs",
        "no-ceiling",
    ],
)
def test_a_capacity_that_breaks_a_rule_is_refused(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _capacity(**changes)


def test_a_measurement_of_a_working_run_below_a_fresh_import_is_refused() -> None:
    """One of the two readings is wrong. A run is the import plus its work."""
    measured = {**_BASE_CAPACITY["measured"], "working_run_hwm_mib": 100.0}  # type: ignore[dict-item]

    with pytest.raises(ValidationError, match="below a fresh import"):
        _capacity(measured=measured)
