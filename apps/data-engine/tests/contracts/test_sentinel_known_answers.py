"""Tests for sentinel known answers contract moved to data_engine.contracts."""

from __future__ import annotations

from decimal import Decimal

import pytest
from data_engine.contracts.sentinel_known_answers import load_sentinel_known_answers
from truealpha_contracts.environment_contract import load_environment_contract
from truealpha_contracts.metrics import is_registered_input_key

pytestmark = pytest.mark.contract


def test_sentinel_known_answers_cover_all_nine_sentinel_issuers_with_accessions() -> None:
    contract = load_environment_contract()
    catalog = load_sentinel_known_answers()
    sentinel_issuers = {member.issuer_id for member in contract.sentinels}
    answered_issuers = {answer.issuer_id for answer in catalog.answers}
    assert sentinel_issuers <= answered_issuers, f"missing {sentinel_issuers - answered_issuers}"
    for answer in catalog.answers:
        assert answer.accession and answer.source_citation
    neg_tickers = {entry.ticker for entry in catalog.negative_theme_classifications}
    assert {"SBUX", "PANW", "INTU"} <= neg_tickers
    for entry in catalog.negative_theme_classifications:
        assert Decimal(entry.max_theme_share) == Decimal("0.00")


def test_quantitative_sentinel_known_answers_resolve_to_registered_metrics() -> None:
    catalog = load_sentinel_known_answers()
    quantitative = [a for a in catalog.answers if a.metric != "operating_branch"]
    assert len(quantitative) > 0
    for answer in quantitative:
        assert is_registered_input_key(answer.metric)
