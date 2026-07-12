import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from data_engine.capture.topt import TOPT_BASELINE_REPORT_PERIOD, TOPT_INSTRUMENTS, build_topt_scope
from data_engine.capture.topt_identity import (
    ToptIdentityResult,
    _resolve_expected_identity,
    emit_source_results,
    validate_baseline,
)
from data_engine.config import settings
from data_engine.sources.nport import Holding
from truealpha_runtime.testing import skip_or_fail

psycopg = pytest.importorskip("psycopg")


@pytest.fixture
def conn():
    try:
        connection = psycopg.connect(settings.database_url, connect_timeout=3)
    except psycopg.OperationalError:
        skip_or_fail("no reachable Postgres (make runtime-up && make db-migrate)")
    yield connection
    connection.rollback()
    connection.close()


def _holding(isin: str, cusip: str, name: str) -> Holding:
    return Holding(
        name=name,
        cusip=cusip,
        isin=isin,
        lei=None,
        balance=Decimal("1"),
        value_usd=Decimal("100"),
        pct_val=Decimal("4.5"),
        asset_cat="EC",
    )


def test_topt_baseline_validator_requires_exact_twenty_one_us_equity_lines():
    holdings = [_holding(item.isin, item.cusip, item.issuer_name) for item in TOPT_INSTRUMENTS]
    result = validate_baseline({"report_period": TOPT_BASELINE_REPORT_PERIOD}, holdings)
    assert len(result) == 21
    assert set(result) == {item.isin for item in TOPT_INSTRUMENTS}


def test_topt_baseline_validator_fails_on_missing_or_duplicate_share_class():
    holdings = [_holding(item.isin, item.cusip, item.issuer_name) for item in TOPT_INSTRUMENTS]
    with pytest.raises(ValueError, match="exactly 21"):
        validate_baseline({"report_period": TOPT_BASELINE_REPORT_PERIOD}, holdings[:-1])
    with pytest.raises(ValueError, match="exactly 21"):
        validate_baseline({"report_period": TOPT_BASELINE_REPORT_PERIOD}, [*holdings[:-1], holdings[0]])


def test_topt_baseline_validator_fails_on_report_period_drift():
    holdings = [_holding(item.isin, item.cusip, item.issuer_name) for item in TOPT_INSTRUMENTS]
    with pytest.raises(ValueError, match="report period drifted"):
        validate_baseline({"report_period": "2026-06-30"}, holdings)


def test_xom_identity_falls_back_to_issuer_submissions_when_current_ticker_map_drifted():
    xom = next(item for item in TOPT_INSTRUMENTS if item.ticker == "XOM")
    records = [
        {
            "exchCode": "PE",
            "ticker": "XOM",
            "marketSector": "Equity",
            "securityType": "Common Stock",
            "name": "EXXON MOBIL CORP",
        }
    ]
    assert (
        _resolve_expected_identity(
            xom,
            records=records,
            issuer_name="Exxon Mobil Corp.",
            sec_ticker_map={"XOM": (2115436, "ExxonMobil Holdings Corp")},
        )
        is None
    )
    resolved = _resolve_expected_identity(
        xom,
        records=records,
        issuer_name="Exxon Mobil Corp.",
        sec_ticker_map={"XOM": (34088, "EXXON MOBIL CORP")},
    )
    assert resolved is not None
    assert resolved[0].resolution_method == "openfigi_sec_name_fallback"
    assert resolved[1:] == (34088, "EXXON MOBIL CORP")


def test_identity_result_emits_every_frozen_identity_holding_and_graph_cell(conn):
    nonce = uuid.uuid4().hex
    issuer_ids = {item.issuer_id for item in TOPT_INSTRUMENTS}
    result = ToptIdentityResult(
        observed_at=datetime.now(UTC),
        nport_raw_ref="raw.fetches:1",
        company_map_raw_ref="raw.fetches:2",
        fund_map_raw_ref="raw.fetches:3",
        figi_raw_refs={item.instrument_id: "raw.fetches:4" for item in TOPT_INSTRUMENTS},
        sec_identity_raw_refs={issuer_id: ("raw.fetches:2",) for issuer_id in issuer_ids},
        holding_record_ids=tuple(f"staging.fund_holding_facts:{index}" for index in range(1, 22)),
        membership_record_ids=tuple(f"staging.universe_memberships:{index}" for index in range(1, 22)),
        issuer_record_ids={issuer_id: (f"staging.kg_entities:{issuer_id}",) for issuer_id in issuer_ids},
        instrument_record_ids={
            item.instrument_id: (f"staging.instruments:{item.instrument_id}",) for item in TOPT_INSTRUMENTS
        },
        relationship_record_ids={
            issuer_id: (f"staging.kg_edges:{index}",) for index, issuer_id in enumerate(issuer_ids)
        },
    )
    record_ids = emit_source_results(conn, run_id=f"run:{nonce}", scope=build_topt_scope(), result=result)
    assert len(record_ids) == 63  # 2 fund + 20 identity + 20 relationship + 21 instrument cells
    counts = dict(
        conn.execute(
            """
            select domain, count(*) from staging.capture_source_results
            where run_id = %s group by domain
            """,
            (f"run:{nonce}",),
        ).fetchall()
    )
    assert counts == {
        "entity_identity": 20,
        "fund_holdings": 1,
        "instruments": 21,
        "knowledge_graph": 20,
        "universe": 1,
    }
