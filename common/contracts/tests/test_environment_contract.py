"""#1416: Environment contract SSOT, generated TypeScript module, and sentinel known answers."""

from __future__ import annotations

import json
import re
import runpy
from decimal import Decimal
from importlib import resources
from pathlib import Path

import pytest
from pydantic import ValidationError
from truealpha_contracts.environment_contract import (
    ENVIRONMENT_CONTRACT_RESOURCE,
    UTILIZATION_CEILING,
    CodeServerCapacity,
    EnvironmentContract,
    EnvironmentUniverse,
    JobRunTimeBasis,
    ModelSeatContract,
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
        ("universe:qqq-us-2026-09-30", "preview", True),
    ],
)
def test_contract_is_head_scheduled_matches_declared_ticks(universe_id: str, environment: str, expected: bool) -> None:
    contract = load_environment_contract()
    assert contract.is_head_scheduled(universe_id, environment) is expected


def _raw_contract() -> dict:
    return json.loads(resources.files("truealpha_contracts.data").joinpath(ENVIRONMENT_CONTRACT_RESOURCE).read_text())


def _contract_with(mutate) -> EnvironmentContract:
    """The real contract file after `mutate(raw)`, validated again."""
    raw = _raw_contract()
    mutate(raw)
    return EnvironmentContract.model_validate_json(json.dumps(raw))


def test_production_lists_topt_and_qqq_as_scheduled_and_sp500_as_stopped() -> None:
    """Owner decision of 2026-10-21: production runs TOPT, QQQ and then the S&P 500."""
    production = load_environment_contract().environments["production"]

    assert [(entry.key, entry.status) for entry in production.universes] == [
        ("topt", "scheduled"),
        ("qqq", "scheduled"),
        ("sp500", "stopped"),
    ]


def test_staging_lists_only_topt() -> None:
    """Owner decision of 2026-10-21: staging runs TOPT only. The QQQ and SP500 ticks are not declared there."""
    staging = load_environment_contract().environments["staging"]

    assert [(entry.key, entry.status) for entry in staging.universes] == [("topt", "scheduled")]
    assert set(staging.ticks) == {"topt", "canary"}


def test_the_one_universe_list_replaces_the_strategy_universe_fields_and_the_scheduled_flag() -> None:
    """#1310: "stopped" has one place. A flag on a tick would be a second place."""
    raw = _raw_contract()

    for name, spec in raw["environments"].items():
        assert "strategy_universe" not in spec, name
        assert "target_strategy_universe" not in spec, name
        for key, tick in spec["ticks"].items():
            assert "scheduled" not in tick, (name, key)


def test_the_sp500_unlock_is_qqq_end_to_end_and_three_green_nights_of_fetch_keys_once() -> None:
    """Owner decision of 2026-10-21. The verdict `fetch_keys_once` is built by another lane."""
    contract = load_environment_contract()
    unlock = contract.unlock_of("sp500", "production")

    assert unlock is not None
    assert (unlock.end_to_end.environment, unlock.end_to_end.universe) == ("production", "qqq")
    assert (unlock.verdict.name, unlock.verdict.environment) == ("fetch_keys_once", "production")
    assert unlock.verdict.consecutive_green_nights == 3
    assert contract.unlock_of("qqq", "production") is None, "a scheduled universe has no precondition"
    assert contract.unlock_of("sp500", "staging") is None, "staging does not list the SP500"


@pytest.mark.parametrize(
    ("environment", "expected"),
    [("production", ("topt", "qqq")), ("prod", ("topt", "qqq")), ("staging", ("topt",)), ("preview", ()), ("dev", ())],
)
def test_the_scheduled_universes_of_an_environment(environment: str, expected: tuple[str, ...]) -> None:
    assert load_environment_contract().scheduled_universes(environment) == expected


def test_the_stopped_universe_keys_are_the_universes_that_wait_for_an_unlock() -> None:
    assert load_environment_contract().stopped_universe_keys == frozenset({"sp500"})


def test_unlocking_a_universe_in_the_file_makes_the_contract_schedule_it() -> None:
    """The status is read, not copied: a changed file changes every answer. This is the mutation of #1310."""

    def unlock_sp500(raw: dict) -> None:
        raw["environments"]["production"]["universes"][2] = {"key": "sp500", "status": "scheduled"}

    contract = _contract_with(unlock_sp500)

    assert contract.is_universe_scheduled("sp500", "production") is True
    assert contract.is_head_scheduled("universe:sp500-us-2026-09-30", "production") is True
    assert contract.scheduled_universes("production") == ("topt", "qqq", "sp500")
    assert contract.stopped_universe_keys == frozenset()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda raw: raw["environments"]["production"]["universes"][2].pop("unlock"), "needs an unlock"),
        (
            lambda raw: raw["environments"]["production"]["universes"][0].update(
                unlock=raw["environments"]["production"]["universes"][2]["unlock"]
            ),
            "must not have an unlock",
        ),
        (
            lambda raw: raw["environments"]["production"]["universes"].append({"key": "topt", "status": "scheduled"}),
            "listed twice",
        ),
        (
            lambda raw: raw["environments"]["staging"]["universes"].append({"key": "qqq", "status": "scheduled"}),
            "differ",
        ),
        (lambda raw: raw["environments"]["staging"]["ticks"].pop("topt"), "differ"),
        (
            lambda raw: raw["environments"]["production"]["universes"][2]["unlock"]["end_to_end"].update(
                environment="staging"
            ),
            "not scheduled there",
        ),
        (
            lambda raw: raw["environments"]["production"]["universes"][2]["unlock"]["verdict"].update(
                consecutive_green_nights=0
            ),
            "greater than or equal to 1",
        ),
        (lambda raw: raw["environments"]["production"].update(strategy_universe="topt"), "strategy_universe"),
        (lambda raw: raw["environments"]["production"]["ticks"]["qqq"].update(scheduled=True), "scheduled"),
    ],
    ids=[
        "stopped-without-unlock",
        "scheduled-with-unlock",
        "universe-listed-twice",
        "universe-without-tick",
        "tick-without-universe",
        "unlock-needs-a-universe-that-staging-does-not-run",
        "unlock-needs-zero-nights",
        "old-strategy-universe-field",
        "old-scheduled-flag",
    ],
)
def test_a_universe_list_that_breaks_a_rule_is_refused(mutate, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _contract_with(mutate)


@pytest.mark.parametrize(
    ("check", "environment", "expected"),
    [
        ("theme_purity@universe-list:qqq", "production", True),
        ("theme_purity@universe-list:qqq", "prod", True),
        ("theme_purity@universe-list:qqq", "staging", False),
        ("question_coverage@universe-list:qqq", "staging", False),
        ("datahub_confidence_report@universe-list:qqq", "staging", False),
        ("datahub_confidence_report@universe-list:qqq", "production", True),
        ("theme_purity@topt", "staging", True),
        ("theme_purity@topt", "production", True),
        ("theme_purity@universe-list:sp500", "production", False),
        ("runtime_shape", "staging", True),
        ("theme_purity@universe:adhoc", "staging", True),
        ("theme_purity@universe-list:qqq", "dev", True),
        ("theme_purity@universe-list:qqq", None, True),
    ],
)
def test_an_environment_records_the_per_universe_verdicts_of_the_universes_it_schedules(
    check: str, environment: str | None, expected: bool
) -> None:
    """Staging runs TOPT only (owner decision of 2026-10-21). An unnamed environment shows every name."""
    assert load_environment_contract().expects_verdict(check, environment) is expected


def test_a_stopped_universe_needs_the_unlock_to_be_built_from_the_model() -> None:
    with pytest.raises(ValidationError, match="needs an unlock"):
        EnvironmentUniverse(key="sp500", status="stopped")


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
    release_text = (REPO_ROOT / "docs" / "release-protocol.md").read_text(encoding="utf-8")

    assert "environment_contract.v1.json" in init_text
    assert "| Local | `local_dev`, `local_test` |" not in init_text
    assert "TWELVE_DATA_ENVIRONMENT_SHARES" not in init_text

    # Section 0 states the production target and points to the contract.
    goal_section = init_text.split("## 1. Core Design Principles", 1)[0]
    assert "environment_contract.v1.json" in goal_section
    assert "S&P 500" in goal_section

    assert "waits\n20 minutes" not in release_text
    # The daily cap of automatic releases has one value, `DEFAULT_DAILY_CAP`. The docs name it and state no number.
    assert "`DEFAULT_DAILY_CAP`" in release_text
    assert re.search(r"daily cap\s*\(\s*(?:\d+|[a-z]+),", release_text) is None
    assert "owner's bound" not in release_text


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
#: The server holds 200 MiB x 1.2 = 240 MiB.
_BASE_CAPACITY: dict[str, object] = {
    "description": "test",
    "ceiling_mib": 1536,
    "ceiling_source": "test",
    "server_margin": 0.2,
    "concurrent_runs": 2,
    "server_processes": 1,
    "processes_per_run": 1,
    "measured": {
        "at": "2026-10-21",
        "environment": "staging",
        "method": "test",
        "grpc_server_hwm_mib": 200.0,
        "fresh_import_hwm_mib": 180.0,
        "working_run_hwm_mib": 380.0,
        "source": "test",
    },
}


def _capacity(**changes: object) -> CodeServerCapacity:
    """The fixed capacity with `changes` applied, validated again (#1467)."""
    return CodeServerCapacity.model_validate({**_BASE_CAPACITY, **changes})


def test_the_declared_slots_leave_each_run_a_budget_that_fits_inside_the_usable_share() -> None:
    """Whatever the contract declares: the server and all slots at the budget fit; one more MiB per slot does not."""
    capacity = load_environment_contract().code_server_capacity
    slots = capacity.concurrent_runs * capacity.processes_per_run
    held = capacity.server_processes * capacity.server_mib + slots * capacity.run_mib

    assert capacity.usable_mib == int(capacity.ceiling_mib * UTILIZATION_CEILING)
    assert held <= capacity.usable_mib < held + slots, "the budget is the largest whole MiB per slot"
    assert capacity.max_concurrent_runs == capacity.concurrent_runs
    assert capacity.max_waiting_runs == capacity.concurrent_runs - 1 >= 1


def test_the_declared_server_derives_from_its_measurement_and_margin() -> None:
    """The server is the measured VmHWM plus the margin, rounded up. A run is no smaller than a fresh import."""
    capacity = load_environment_contract().code_server_capacity
    measured = capacity.measured
    grown = Decimal(str(measured.grpc_server_hwm_mib)) * (1 + Decimal(str(capacity.server_margin)))

    assert capacity.server_mib == int(grown) + (grown % 1 > 0)
    assert capacity.server_mib > measured.grpc_server_hwm_mib
    assert capacity.run_mib >= measured.working_run_hwm_mib >= measured.fresh_import_hwm_mib


def test_the_one_seventy_percent_rule_is_a_python_constant_and_no_json_field() -> None:
    """The owner rule of #1418 lives in `UTILIZATION_CEILING`. The JSON holds no second copy."""
    raw = json.loads(resources.files("truealpha_contracts.data").joinpath(ENVIRONMENT_CONTRACT_RESOURCE).read_text())

    assert "utilization_limit" not in raw["code_server_capacity"]
    assert "70%" not in json.dumps(raw["code_server_capacity"])


def test_the_peak_of_the_container_is_no_field_because_it_counts_page_cache() -> None:
    """#1484: `container_peak_mib` was a required field that no code read. It holds 77% of the ceiling."""
    raw = json.loads(resources.files("truealpha_contracts.data").joinpath(ENVIRONMENT_CONTRACT_RESOURCE).read_text())
    measured = {**raw["code_server_capacity"]["measured"], "container_peak_mib": 1186.4}

    with pytest.raises(ValidationError, match="container_peak_mib"):
        CodeServerCapacity.model_validate({**raw["code_server_capacity"], "measured": measured})


def test_the_fixed_capacity_gives_two_slots_and_a_budget_of_417_mib() -> None:
    """1536 MiB x 0.7 = 1075 MiB. The server holds 240 MiB. The other 835 MiB go to two slots: 417 MiB each."""
    capacity = _capacity()

    assert (capacity.usable_mib, capacity.server_mib, capacity.run_mib) == (1075, 240, 417)
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
    ],
)
def test_the_run_budget_follows_each_input_of_the_derivation(changes: dict[str, object], budget: int) -> None:
    assert _capacity(**changes).run_mib == budget


@pytest.mark.parametrize(
    ("margin", "server"),
    [(0.1, 220), (0.2, 240), (0.201, 241), (0.5, 300)],
)
def test_the_server_is_its_measurement_times_one_plus_the_margin_rounded_up(margin: float, server: int) -> None:
    assert _capacity(server_margin=margin).server_mib == server


@pytest.mark.parametrize(
    "changes",
    [
        {"server_margin": 0},
        {"server_margin": -0.1},
        {"concurrent_runs": 1},
        {"concurrent_runs": 3},
        {"concurrent_runs": 5},
        {"ceiling_mib": 1426},
        {"ceiling_mib": 768},
        {"ceiling_mib": 0},
    ],
    ids=[
        "no-margin-above-the-measured-server",
        "negative-margin",
        "one-slot-cannot-wait-for-another-run",
        "three-slots-do-not-fit-the-measured-working-run",
        "five-slots-do-not-fit-the-measured-working-run",
        "budget-one-mib-below-the-measured-working-run",
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


# --- the model seat: one retry policy, per-environment pacing, probe slots (#1444) ----------


def _seat(**changes: object) -> dict[str, object]:
    """The shipped model seat as plain data, with ``changes`` laid over it."""
    return {**load_environment_contract().model_seat.model_dump(mode="json"), **changes}


def _retry(**changes: object) -> dict[str, object]:
    return {**load_environment_contract().model_seat.retry.model_dump(mode="json"), **changes}


def test_every_environment_that_calls_live_vendors_declares_model_seat_limits() -> None:
    contract = load_environment_contract()
    live = {name for name, spec in contract.environments.items() if spec.live_vendor_calls}
    assert live == {"staging", "production"}
    for name in live:
        limits = contract.model_seat.limits_for(name)
        assert limits is not None and limits.max_concurrent_calls >= 1 and limits.calls_per_minute >= 1
    assert contract.model_seat.limits_for("dev") is None


def test_the_shipped_retry_waits_at_least_ninety_seconds_with_no_random_extra() -> None:
    retry = load_environment_contract().model_seat.retry
    assert [retry.base_backoff_seconds(n) for n in range(1, retry.max_attempts)] == [5, 10, 20, 40, 40, 40]
    assert retry.base_total_wait_seconds == 155 >= retry.min_total_wait_seconds == 90
    assert retry.deadline_seconds >= retry.min_total_wait_seconds


def test_probe_slots_of_the_two_live_environments_differ_by_at_least_the_retry_deadline() -> None:
    seat = load_environment_contract().model_seat
    production, staging = seat.probe_cron("production"), seat.probe_cron("staging")
    assert production != staging
    minutes = sorted(seat.probe_offset_minutes(name) for name in ("production", "staging"))
    longest = seat.retry.deadline_seconds + seat.probe.attempt_timeout_seconds + seat.probe.launch_margin_seconds
    assert minutes[1] - minutes[0] >= longest / 60
    assert production == f"{seat.probe_offset_minutes('production')} {seat.probe.hour_utc} * * *"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"retry": _retry(max_attempts=3)}, "below min_total_wait_seconds"),
        ({"retry": _retry(max_backoff_seconds=5, max_attempts=7)}, "below min_total_wait_seconds"),
        ({"retry": _retry(deadline_seconds=60)}, "deadline_seconds 60 is below"),
        ({"retry": _retry(transient_provider_codes=["1302", "1113"])}, "transient and terminal"),
        ({"retry": _retry(jitter_percent=-1)}, "greater than or equal to 0"),
        (
            {
                "environments": {
                    "production": {"max_concurrent_calls": 2, "calls_per_minute": 10, "probe_offset_minutes": 0},
                    "staging": {"max_concurrent_calls": 1, "calls_per_minute": 3, "probe_offset_minutes": 6},
                }
            },
            "closer than slot_minutes",
        ),
        (
            {
                "environments": {
                    "production": {"max_concurrent_calls": 0, "calls_per_minute": 10, "probe_offset_minutes": 0}
                }
            },
            "greater than or equal to 1",
        ),
        (
            {"probe": {"hour_utc": 6, "slot_minutes": 6, "attempt_timeout_seconds": 60, "launch_margin_seconds": 60}},
            "below the longest probe of 7 minutes",
        ),
        (
            {"probe": {"hour_utc": 6, "slot_minutes": 7, "attempt_timeout_seconds": 120, "launch_margin_seconds": 60}},
            "below the longest probe of 8 minutes",
        ),
    ],
)
def test_a_model_seat_that_breaks_a_rule_is_refused(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ModelSeatContract.model_validate_json(json.dumps(_seat(**changes)))


def test_a_live_environment_without_model_seat_limits_is_refused() -> None:
    data = load_environment_contract().model_dump(mode="json")
    data["model_seat"]["environments"].pop("staging")
    with pytest.raises(ValidationError, match="staging calls live vendors and declares no model seat limits"):
        EnvironmentContract.model_validate_json(json.dumps(data))
    data = load_environment_contract().model_dump(mode="json")
    data["model_seat"]["environments"]["qa"] = {
        "max_concurrent_calls": 1,
        "calls_per_minute": 1,
        "probe_offset_minutes": 30,
    }
    with pytest.raises(ValidationError, match="name unknown environments"):
        EnvironmentContract.model_validate_json(json.dumps(data))
