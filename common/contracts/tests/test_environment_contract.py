"""#1416: Environment contract SSOT, generated TypeScript module, and sentinel known answers."""

from __future__ import annotations

import runpy
from decimal import Decimal
from pathlib import Path

import pytest
from truealpha_contracts.environment_contract import (
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
    release_doc = f"{'release'}-protocol.md"
    release_text = (REPO_ROOT / "docs" / release_doc).read_text(encoding="utf-8")

    assert "environment_contract.v1.json" in init_text
    assert "| Local | `local_dev`, `local_test` |" not in init_text
    assert "TWELVE_DATA_ENVIRONMENT_SHARES" not in init_text

    assert "environment_contract.v1.json" in vision_text
    assert "S&P 500" in vision_text

    assert "waits\n20 minutes" not in release_text
    assert "daily cap (4," not in release_text
