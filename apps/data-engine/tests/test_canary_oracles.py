"""The canary's oracles about the deployed IMAGE itself.

The infra2-sdk pin the canary asserts is read from the installed `truealpha-runtime`
package metadata, the locked wheel URL. A hand-kept constant once read 1.2.0 while the lock
pinned 1.3.2 and called every correctly built image a failure; there is no copy left to drift.
"""

from __future__ import annotations

import importlib.metadata
import os
import re
import tomllib
from pathlib import Path

import psycopg
import pytest
from data_engine import release_identity
from data_engine.config import settings
from data_engine.datahub import canary_oracles
from data_engine.datahub.canary_oracles import failures_for_run, image_content_failures, pinned_infra2_sdk

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_WHEEL_PIN = re.compile(r"/releases/download/v(?P<version>\d+\.\d+\.\d+)/infra2_sdk-(?P=version)-py3-none-any\.whl$")


def _runtime_pyproject_pin() -> str:
    """The pin as the workspace source declares it (common/runtime/pyproject.toml)."""
    runtime = tomllib.loads((REPOSITORY_ROOT / "common/runtime/pyproject.toml").read_text(encoding="utf-8"))
    pins = [item for item in runtime["project"]["dependencies"] if item.startswith("infra2-sdk")]
    assert len(pins) == 1, pins
    match = _WHEEL_PIN.search(pins[0])
    assert match, f"the infra2-sdk pin is not a release wheel URL: {pins[0]}"
    return match.group("version")


def test_the_oracle_reads_the_pin_from_the_installed_package_metadata() -> None:
    assert pinned_infra2_sdk() == _runtime_pyproject_pin()


def test_the_installed_sdk_is_the_pinned_one() -> None:
    """What the oracle checks inside the image, checked here against the workspace."""
    assert importlib.metadata.version("infra2-sdk") == pinned_infra2_sdk()


def test_a_loaded_sdk_other_than_the_pin_is_a_finding(monkeypatch) -> None:
    real = importlib.metadata.version
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.0.1" if name == "infra2-sdk" else real(name))
    assert f"infra2-sdk 0.0.1 loaded, truealpha-runtime pins {pinned_infra2_sdk()}" in image_content_failures()


def test_a_pin_that_is_not_a_release_wheel_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(
        importlib.metadata, "requires", lambda name: ["infra2-sdk[otel] @ https://example.invalid/sdk.whl"]
    )
    with pytest.raises(ValueError, match="without a release wheel"):
        canary_oracles.pinned_infra2_sdk()


# --- what the deployed image contains (#784) --------------------------------------------
#
# Only a process inside the container can see this. A GitHub runner inspects the repository,
# a health probe inspects an answer; neither can tell whether the image shipped the
# environment contract infra2's entrypoint validates against — and until #784 it did not,
# with every gate green, because the validation simply never ran.


def test_the_image_content_oracles_hold_for_this_working_tree() -> None:
    assert image_content_failures() == []


def test_an_image_without_its_environment_manifest_is_a_red_canary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(release_identity, "ENV_MANIFEST_PATH", tmp_path / "required-env.generated.json")
    failures = image_content_failures()
    assert any("does not ship" in line and "boot-validate" in line for line in failures), failures
    # ... and it cannot say what release it is either, which is the same missing file.
    assert any("cannot measure its release identity" in line for line in failures), failures


def test_an_image_without_its_migration_set_is_a_red_canary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(release_identity, "MIGRATIONS_PATH", tmp_path / "migrations")
    assert any("cannot measure its release identity" in line for line in image_content_failures())


def test_the_run_verdict_carries_the_image_verdict(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Through `failures_for_run`, which is what the deploy lane calls: a wrong image is
    named even when the run it was asked about does not exist, because the image is the
    cause and "no capture status" is only the symptom."""
    try:
        connection = psycopg.connect(settings.database_url, connect_timeout=3)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    monkeypatch.setattr(release_identity, "ENV_MANIFEST_PATH", tmp_path / "required-env.generated.json")
    try:
        failures = failures_for_run(connection, "capture-run:no-such-run")
    finally:
        connection.close()
    assert any("does not ship" in line for line in failures), failures
    assert any("no capture status" in line for line in failures), failures


# --- #1416 E3/E4: derived vendor shares and sentinel known-answer oracles ----------------


def test_sentinel_known_answers_pass_on_grounded_values_and_fail_when_mutated_by_10x() -> None:
    """#1416 E4: mutation test — changing one stored value by 10x fails the oracle,
    and an answer for a period older than the latest filing is treated as stale, not failed."""
    from decimal import Decimal

    from truealpha_contracts.environment_contract import load_sentinel_known_answers

    catalog = load_sentinel_known_answers()
    baseline: dict[str, dict[str, object]] = {
        "issuer:cik:0000320193": {
            "operating_branch": "non_financial",
            "gppe": Decimal("1070000"),
            "headcount": Decimal("166000"),
        },
        "issuer:cik:0001652044": {
            "operating_branch": "non_financial",
            "headcount": Decimal("190820"),
        },
        "issuer:cik:0000049196": {"operating_branch": "financial"},
        "issuer:cik:0000020286": {"operating_branch": "insurance"},
        "issuer:cik:0000019617": {
            "operating_branch": "financial",
            "revenue": Decimal("182447000000"),
        },
        "issuer:cik:0001045810": {"headcount": Decimal("42000")},
        "issuer:cik:0001067983": {"headcount": Decimal("385500")},
        "issuer:cik:0001535527": {"revenue": Decimal("4812005000")},
        "issuer:cik:0000937966": {"operating_branch": "non_financial"},
    }
    assert canary_oracles.evaluate_sentinel_known_answers(baseline, catalog=catalog, require_all_issuers=True) == []

    # Mutate CRWD revenue by 10x -> oracle fails and cites accession 0001535527-26-000010
    mutated_crwd = {**baseline, "issuer:cik:0001535527": {"revenue": Decimal("48120050000")}}
    crwd_failures = canary_oracles.evaluate_sentinel_known_answers(mutated_crwd, catalog=catalog)
    assert len(crwd_failures) == 1
    assert "crwd-revenue-fy2026" in crwd_failures[0]
    assert "0001535527-26-000010" in crwd_failures[0]

    # Mutate AAPL GPPE by 10x -> oracle fails and cites accession 0000320193-25-000079
    mutated_aapl = {
        **baseline,
        "issuer:cik:0000320193": {
            **baseline["issuer:cik:0000320193"],
            "gppe": Decimal("10700000"),
        },
    }
    aapl_failures = canary_oracles.evaluate_sentinel_known_answers(mutated_aapl, catalog=catalog)
    assert len(aapl_failures) == 1
    assert "aapl-gppe-fy2025" in aapl_failures[0]

    # Newer filing period than the known answer is flagged stale (not failed)
    newer_filing = {
        **baseline,
        "issuer:cik:0001535527": {
            "revenue": Decimal("9999999999"),
            "revenue_period_end": "2027-01-31",
        },
    }
    assert canary_oracles.evaluate_sentinel_known_answers(newer_filing, catalog=catalog) == []

    # Negative theme classifications: Starbucks with 0% semiconductor passes; >0% fails
    assert (
        canary_oracles.evaluate_negative_theme_classifications(
            [{"issuer_id": "issuer:cik:0000829224", "theme_key": "semiconductors", "theme_share": "0.00"}],
            catalog=catalog,
        )
        == []
    )
    neg_failures = canary_oracles.evaluate_negative_theme_classifications(
        [{"issuer_id": "issuer:cik:0000829224", "theme_key": "semiconductors", "theme_share": "0.25"}],
        catalog=catalog,
    )
    assert len(neg_failures) == 1
    assert "sbux-zero-semiconductor" in neg_failures[0]


def test_derived_expected_canary_snapshot_counts() -> None:
    """Canary snapshot counts are derived from static universe members and contract sentinels (#1523 S2c)."""
    assert canary_oracles.expected_canary_snapshot() == (5, 6, 24)
