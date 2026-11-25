"""Tests for iXBRL dei:EntityNumberOfEmployees headcount extraction (#1851)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from data_engine.contracts.standards import STANDARDS, confidence_for
from data_engine.datahub.standards import filing_extraction
from data_engine.datahub.standards.filing_extraction import (
    IXBRL_HEADCOUNT_EXTRACTOR,
    FilingDocument,
    _ixbrl_headcount_candidate,
    extract_headcount,
)
from data_engine.sources.gateway import MemoryLedger, SourceGateway

pytestmark = pytest.mark.contract

CUTOFF = datetime(2026, 4, 1, tzinfo=UTC)
FILING_DATE = date(2026, 2, 15)


def _doc(body: str) -> FilingDocument:
    return FilingDocument(
        cik=320193,
        accession="000032019326000010",
        form="10-K",
        filing_date=FILING_DATE,
        primary_document="aapl-20251231.htm",
        url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000010/aapl-20251231.htm",
        body=body.encode("utf-8"),
    )


def test_ixbrl_headcount_resolves_without_llm_and_has_confidence_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    html_body = """
    <html><body>
      <ix:header><ix:resources>
        <xbrli:context id="c_entity">
          <xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000320193</xbrli:identifier></xbrli:entity>
          <xbrli:period><xbrli:instant>2025-12-31</xbrli:instant></xbrli:period>
        </xbrli:context>
      </ix:resources></ix:header>
      <p>People at year end: <ix:nonFraction name="dei:EntityNumberOfEmployees" contextRef="c_entity" decimals="0" scale="0">12,500</ix:nonFraction>.</p>
    </body></html>
    """
    monkeypatch.setattr(filing_extraction, "require_annual_filing", lambda *_a, **_k: _doc(html_body))

    def _forbidden_transport(*_a: object, **_k: object) -> object:
        raise AssertionError("LLM transport must not be called when iXBRL dei:EntityNumberOfEmployees resolves")

    outcome = extract_headcount(
        320193,
        connection=None,
        http=None,
        gateway=SourceGateway(MemoryLedger(), caller="test"),
        standard=STANDARDS["employees_total"],
        cutoff=CUTOFF,
        write=False,
        model_transport=_forbidden_transport,
    )
    assert outcome.status == "resolved"
    assert outcome.extractor == IXBRL_HEADCOUNT_EXTRACTOR
    assert outcome.value == 12500
    assert outcome.as_of == date(2025, 12, 31)
    assert confidence_for(STANDARDS["employees_total"].confidence_policy_id, IXBRL_HEADCOUNT_EXTRACTOR) == Decimal(
        "0.95"
    )


def test_ixbrl_headcount_ignores_dimensional_facts_and_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    html_body = """
    <html><body>
      <ix:header><ix:resources>
        <xbrli:context id="c_seg">
          <xbrli:entity>
            <xbrli:identifier scheme="http://www.sec.gov/CIK">0000320193</xbrli:identifier>
            <xbrli:segment><xbrldi:explicitMember dimension="us-gaap:StatementBusinessSegmentsAxis">us-gaap:AmericasMember</xbrldi:explicitMember></xbrli:segment>
          </xbrli:entity>
          <xbrli:period><xbrli:instant>2025-12-31</xbrli:instant></xbrli:period>
        </xbrli:context>
      </ix:resources></ix:header>
      <p>Americas staff: <ix:nonFraction name="dei:EntityNumberOfEmployees" contextRef="c_seg" decimals="0" scale="0">4,200</ix:nonFraction>.</p>
    </body></html>
    """
    doc = _doc(html_body)
    assert _ixbrl_headcount_candidate(doc) is None
    monkeypatch.setattr(filing_extraction, "require_annual_filing", lambda *_a, **_k: doc)
    outcome = extract_headcount(
        320193,
        connection=None,
        http=None,
        gateway=SourceGateway(MemoryLedger(), caller="test"),
        standard=STANDARDS["employees_total"],
        cutoff=CUTOFF,
        write=False,
        select_with_model=False,
    )
    assert outcome.status == "no_candidate"


def test_ixbrl_headcount_fails_closed_on_conflicting_same_date_values() -> None:
    html_body = """
    <html><body>
      <ix:header><ix:resources>
        <xbrli:context id="c1">
          <xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000320193</xbrli:identifier></xbrli:entity>
          <xbrli:period><xbrli:instant>2025-12-31</xbrli:instant></xbrli:period>
        </xbrli:context>
        <xbrli:context id="c2">
          <xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000320193</xbrli:identifier></xbrli:entity>
          <xbrli:period><xbrli:instant>2025-12-31</xbrli:instant></xbrli:period>
        </xbrli:context>
      </ix:resources></ix:header>
      <ix:nonFraction name="dei:EntityNumberOfEmployees" contextRef="c1" decimals="0" scale="0">10,000</ix:nonFraction>
      <ix:nonFraction name="dei:EntityNumberOfEmployees" contextRef="c2" decimals="0" scale="0">12,000</ix:nonFraction>
    </body></html>
    """
    assert _ixbrl_headcount_candidate(_doc(html_body)) is None
