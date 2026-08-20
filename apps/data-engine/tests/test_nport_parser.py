"""Module 5's source parser, pinned to the captured sample filings (#641 audit:
the samples sat unused for six weeks while the parser did not exist)."""

from decimal import Decimal
from pathlib import Path

import pytest
from data_engine.sources.nport import parse_nport_holdings

SAMPLES = Path(__file__).resolve().parents[1] / "samples" / "nport"


def test_the_captured_filing_parses_completely() -> None:
    body = (SAMPLES / "QQQ_NPORT_000106783926000016.xml").read_bytes()
    holdings = parse_nport_holdings(body)
    assert len(holdings) == 101
    first = holdings[0]
    assert first.name == "AstraZeneca PLC"
    assert first.isin == "US0463531089"
    assert first.cusip == "046353108"
    assert first.weight_pct == Decimal("0.295326990687")
    assert first.value_usd == Decimal("1204029457.06000000")
    # Weights are percentages of net assets; a full portfolio sums near 100.
    total = sum(h.weight_pct for h in holdings)
    assert Decimal("50") < total <= Decimal("110")


def test_every_sample_filing_parses() -> None:
    for path in sorted(SAMPLES.glob("*.xml")):
        holdings = parse_nport_holdings(path.read_bytes())
        assert holdings, path.name


def test_an_empty_document_fails_loud() -> None:
    with pytest.raises(ValueError, match="no holdings"):
        parse_nport_holdings(b"<edgarSubmission></edgarSubmission>")
