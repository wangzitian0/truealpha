"""#63 tranche 2: minted company:isin identities re-point onto issuer:cik
entities from landed OpenFIGI batches — offline, idempotent, forward-resolving.

The fixture seeds what tranche 1 leaves behind (facts + minted entities) plus a
landed ISIN-keyed OpenFIGI batch, then runs the sweep with NO http client: the
whole resolution must come from the landing zone and the injected SEC index."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from data_engine import raw_store
from data_engine.config import settings
from data_engine.datahub.capture_core.holdings_enrichment import (
    enrich_holding_identities,
    figi_from_raw,
)
from factors.shared import entity_resolution as er
from truealpha_contracts.models import DataSource
from truealpha_runtime.testing import InMemoryRawObjectStore as _InMemoryObjectStore

_AT = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
_NVDA_ISIN = "US67066G1040"
_FOREIGN_ISIN = "XS0000000001"  # maps to no US listing — must stay minted
# Alphabet's two classes: same issuer CIK, DIFFERENT tickers per ISIN (#706).
_GOOGL_ISIN = "US02079K3059"
_GOOG_ISIN = "US02079K1079"
_NVDA_RECORDS = [
    {
        "figi": "BBG000BLARC1",
        "ticker": "NVDA",
        "exchCode": "US",
        "name": "NVIDIA CORP",
        "securityType": "Common Stock",
        "marketSector": "Equity",
    }
]


def _class_records(ticker: str) -> list[dict]:
    return [
        {
            "figi": f"BBG00{ticker}",
            "ticker": ticker,
            "exchCode": "US",
            "name": "ALPHABET INC",
            "securityType": "Common Stock",
            "marketSector": "Equity",
        }
    ]


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        pytest.skip(f"no local database: {error}")
    try:
        yield active
    finally:
        active.rollback()
        active.close()


def _seed_tranche_one(connection) -> None:
    fund_uuid = er.ensure_entity(connection, "etf:series:S-TEST-ENRICH", "etf", "Enrichment Test Fund")
    for isin in (_NVDA_ISIN, _FOREIGN_ISIN):
        inst_uuid = er.ensure_entity(connection, f"company:isin:{isin}", "instrument", isin, transaction_time=_AT)
        er.assert_identifier(
            connection,
            entity_id=inst_uuid,
            source="nport",
            identifier_type="isin",
            identifier_value=isin,
            confidence=1.0,
            transaction_time=_AT,
            valid_from="2026-06-30",
            raw_ref="raw.fetches:0",
        )
        connection.execute(
            """
            insert into staging.fund_holding_facts
                (fund_id, holding_id, holding_name, report_period, transaction_time,
                 cusip, isin, lei, balance, value_usd, percent_of_net_assets, confidence, raw_ref)
            values (%s, %s, %s, %s, %s, null, %s, null, 1, 100, 5.0, 1.0, 'raw.fetches:0')
            on conflict do nothing
            """,
            (fund_uuid, inst_uuid, isin, "2026-06-30", _AT, isin),
        )


def _land_openfigi_batch(connection, store) -> None:
    raw_store.insert_json_fetch(
        connection,
        source=DataSource.OPENFIGI,
        source_record_id=f"mapping:{_NVDA_ISIN}",
        payload=[{"data": _NVDA_RECORDS}, {"data": []}],
        fetched_at=_AT,
        metadata={"isins": [_NVDA_ISIN, _FOREIGN_ISIN]},
        store=store,
    )


def test_sweep_repoints_from_landed_batches_without_a_vendor(connection) -> None:
    store = _InMemoryObjectStore()
    _seed_tranche_one(connection)
    _land_openfigi_batch(connection, store)

    outcome = enrich_holding_identities(
        connection, ticker_cik={"NVDA": 1045810}, now=_AT, store=store, isins=[_NVDA_ISIN, _FOREIGN_ISIN]
    )

    assert outcome.unresolved == 2
    assert outcome.reused_from_raw == 2, "both ISINs answer from the landing zone"
    assert outcome.fetched == 0, "no vendor call when raw already has the batch"
    assert outcome.repointed == 1
    assert outcome.unmapped == 1, "the foreign ISIN stays minted, retried next sweep"

    inst_id = er.resolve(connection, "isin", _NVDA_ISIN, as_of=_AT + timedelta(days=1))
    issuer_uuid = er.coerce_entity_uuid(connection, "issuer:cik:0001045810")
    assert inst_id is not None and issuer_uuid is not None
    assert er.resolve(connection, "ticker", "NVDA", as_of=_AT + timedelta(days=1)) == str(issuer_uuid)
    foreign_id = er.resolve(connection, "isin", _FOREIGN_ISIN, as_of=_AT + timedelta(days=1))
    assert foreign_id is not None

    issues = connection.execute(
        "select from_entity_id from staging.entity_relations where to_entity_id = %s::uuid and relation_type = 'issues'",
        (inst_id,),
    ).fetchall()
    assert issues == [(issuer_uuid,)], "immutable facts resolve forward through the issues relation"
    foreign_issues = connection.execute(
        "select from_entity_id from staging.entity_relations where to_entity_id = %s::uuid and relation_type = 'issues'",
        (foreign_id,),
    ).fetchall()
    assert foreign_issues == []
    vintage_time = connection.execute(
        "select transaction_time from staging.entity_aliases where scheme = 'isin' and value = %s and source = 'openfigi'",
        (_NVDA_ISIN,),
    ).fetchone()[0]
    assert vintage_time == _AT, "the vintage carries the supplying batch's own clock (review on #695)"


def test_second_sweep_is_a_no_op(connection) -> None:
    store = _InMemoryObjectStore()
    _seed_tranche_one(connection)
    _land_openfigi_batch(connection, store)
    enrich_holding_identities(
        connection, ticker_cik={"NVDA": 1045810}, now=_AT, store=store, isins=[_NVDA_ISIN, _FOREIGN_ISIN]
    )

    again = enrich_holding_identities(
        connection, ticker_cik={"NVDA": 1045810}, now=_AT, store=store, isins=[_NVDA_ISIN, _FOREIGN_ISIN]
    )

    assert again.repointed == 0, "a re-keyed identity never re-enters the sweep"
    assert again.unresolved == 1 and again.unmapped == 1, "only the still-minted foreign ISIN is considered"
    vintages = connection.execute(
        "select count(*) from staging.entity_aliases where scheme = 'isin' and value = %s and source = 'openfigi'",
        (_NVDA_ISIN,),
    ).fetchone()[0]
    assert vintages == 1, "no duplicate identifier vintages sprayed by re-runs"


def test_share_classes_keep_their_own_tickers(connection) -> None:
    """#706: two ISINs of ONE issuer re-point to the same issuer entity, while
    each minted per-ISIN entity carries ITS OWN ticker — the association the
    valuation view reads so the GOOG line can never render as GOOGL."""
    store = _InMemoryObjectStore()
    fund_uuid = er.ensure_entity(connection, "etf:series:S-TEST-DUAL", "etf", "Dual Class Test Fund")
    for isin in (_GOOGL_ISIN, _GOOG_ISIN):
        inst_uuid = er.ensure_entity(connection, f"company:isin:{isin}", "instrument", isin, transaction_time=_AT)
        er.assert_identifier(
            connection,
            entity_id=inst_uuid,
            source="nport",
            identifier_type="isin",
            identifier_value=isin,
            confidence=1.0,
            transaction_time=_AT,
            valid_from="2026-06-30",
            raw_ref="raw.fetches:0",
        )
        connection.execute(
            """
            insert into staging.fund_holding_facts
                (fund_id, holding_id, holding_name, report_period, transaction_time,
                 cusip, isin, lei, balance, value_usd, percent_of_net_assets, confidence, raw_ref)
            values (%s, %s, 'Alphabet Inc.', '2026-06-30', %s, null, %s, null, 1, 100, 3.0, 1.0, 'raw.fetches:0')
            on conflict do nothing
            """,
            (fund_uuid, inst_uuid, _AT, isin),
        )
    raw_store.insert_json_fetch(
        connection,
        source=DataSource.OPENFIGI,
        source_record_id=f"mapping:{_GOOGL_ISIN}",
        payload=[{"data": _class_records("GOOGL")}, {"data": _class_records("GOOG")}],
        fetched_at=_AT,
        metadata={"isins": [_GOOGL_ISIN, _GOOG_ISIN]},
        store=store,
    )

    outcome = enrich_holding_identities(
        connection,
        ticker_cik={"GOOGL": 1652044, "GOOG": 1652044},
        now=_AT,
        store=store,
        isins=[_GOOGL_ISIN, _GOOG_ISIN],
    )

    assert outcome.repointed == 2
    goog_issuer = er.coerce_entity_uuid(connection, "issuer:cik:0001652044")
    assert goog_issuer is not None
    for isin, expected in ((_GOOGL_ISIN, "GOOGL"), (_GOOG_ISIN, "GOOG")):
        inst_id = er.resolve(connection, "isin", isin, as_of=_AT + timedelta(days=1))
        assert inst_id is not None
        linked = connection.execute(
            "select from_entity_id from staging.entity_relations where to_entity_id = %s::uuid and relation_type = 'issues'",
            (inst_id,),
        ).fetchone()
        assert linked == (goog_issuer,)
        own = connection.execute(
            "select value from staging.entity_aliases"
            " where entity_id = %s::uuid and scheme = 'ticker'"
            " order by transaction_time desc, confidence desc, alias_id desc limit 1",
            (inst_id,),
        ).fetchone()
        assert own == (expected,), f"{isin} must keep its own ticker, got {own}"
        issuer_row = connection.execute(
            "select 1 from staging.entity_aliases where entity_id = %s and scheme = 'ticker' and value = %s",
            (goog_issuer, expected),
        ).fetchone()
        assert issuer_row is not None, (
            f"the issuer-level {expected} vintage must land TOO — the unique key"
            " (source, type, value, time) swallowed whichever claim asserted second"
        )

    again = enrich_holding_identities(
        connection,
        ticker_cik={"GOOGL": 1652044, "GOOG": 1652044},
        now=_AT,
        store=store,
        isins=[_GOOGL_ISIN, _GOOG_ISIN],
    )
    assert again.repointed == 0 and again.unresolved <= 1, "the dual-class pair never re-enters the sweep"


def test_figi_from_raw_skips_ticker_keyed_batches(connection) -> None:
    store = _InMemoryObjectStore()
    raw_store.insert_json_fetch(
        connection,
        source=DataSource.OPENFIGI,
        source_record_id="mapping:ticker-keyed",
        payload=[{"data": _NVDA_RECORDS}],
        fetched_at=_AT,
        metadata={"request_jobs": [{"idType": "TICKER", "idValue": "NVDA"}]},
        store=store,
    )
    mapping, missing = figi_from_raw(connection, [_NVDA_ISIN], store=store)
    assert mapping == {} and missing == [_NVDA_ISIN], "ticker-keyed batches carry no isins metadata"


def test_per_isin_ticker_lands_even_when_sec_crosswalk_omits_ticker(connection) -> None:
    """An OpenFIGI-resolved US equity omitted from SEC company_tickers.json still records its
    per-ISIN openfigi-listing ticker on company:isin:<isin>, so mart.fund_holdings_resolved
    resolves listing_id."""
    store = _InMemoryObjectStore()
    ea_isin = "US2855121099"
    fund_uuid = er.ensure_entity(connection, "etf:series:S-TEST-OMITTED", "etf", "Omitted Crosswalk Test Fund")
    inst_uuid = er.ensure_entity(connection, f"company:isin:{ea_isin}", "instrument", ea_isin)
    er.assert_identifier(
        connection,
        entity_id=inst_uuid,
        source="nport",
        identifier_type="isin",
        identifier_value=ea_isin,
        confidence=1.0,
        transaction_time=_AT,
        valid_from="2026-06-30",
        raw_ref="raw.fetches:0",
    )
    connection.execute(
        """
        insert into staging.fund_holding_facts
            (fund_id, holding_id, holding_name, report_period, transaction_time,
             cusip, isin, lei, balance, value_usd, percent_of_net_assets, confidence, raw_ref)
        values (%s, %s, 'Electronic Arts Inc.', '2026-06-30', %s, null, %s, null, 1, 100, 1.5, 1.0, 'raw.fetches:0')
        on conflict do nothing
        """,
        (fund_uuid, inst_uuid, _AT, ea_isin),
    )
    raw_store.insert_json_fetch(
        connection,
        source=DataSource.OPENFIGI,
        source_record_id=f"mapping:{ea_isin}",
        payload=[{"data": _class_records("EA")}],
        fetched_at=_AT,
        metadata={"isins": [ea_isin]},
        store=store,
    )

    outcome = enrich_holding_identities(
        connection,
        ticker_cik={},
        now=_AT,
        store=store,
        isins=[ea_isin],
    )
    assert outcome.repointed == 0 and outcome.unmapped == 1
    from truealpha_runtime.mart_refresh import MART_FUND_HOLDINGS_MV_ORDER, refresh_mart_materialized_views

    refresh_mart_materialized_views(connection, MART_FUND_HOLDINGS_MV_ORDER)
    row = connection.execute(
        "select ticker, listing_id from mart.fund_holdings_resolved where fund_id = %s and isin = %s",
        (str(fund_uuid), ea_isin),
    ).fetchone()
    assert row == ("EA", "listing:xnas:ea")


def test_company_cik_issuer_entity_resolves_ticker_in_fund_holdings_resolved(connection) -> None:
    """Holdings whose ISIN maps to bootstrap_universe's company:cik:<cik> entity resolve
    ticker and listing_id through the issuer_ticker lateral join in mart.fund_holdings_resolved."""
    ea_isin = "US2855121099"
    fund_uuid = er.ensure_entity(connection, "etf:series:S-TEST-COMPANY-CIK", "etf", "Company CIK Test Fund")
    company_uuid = er.ensure_entity(connection, "company:cik:712515", "issuer", "Electronic Arts Inc.")
    er.assert_identifier(
        connection,
        entity_id=company_uuid,
        source="openfigi",
        identifier_type="isin",
        identifier_value=ea_isin,
        confidence=0.95,
        transaction_time=_AT,
        valid_from="2026-06-30",
        raw_ref="raw.fetches:0",
    )
    er.assert_identifier(
        connection,
        entity_id=company_uuid,
        source="sec_edgar",
        identifier_type="ticker",
        identifier_value="EA",
        confidence=1.0,
        transaction_time=_AT,
        valid_from="2026-06-30",
        raw_ref="raw.fetches:0",
    )
    connection.execute(
        """
        insert into staging.fund_holding_facts
            (fund_id, holding_id, holding_name, report_period, transaction_time,
             cusip, isin, lei, balance, value_usd, percent_of_net_assets, confidence, raw_ref)
        values (%s, %s, 'Electronic Arts Inc.', '2026-06-30', %s, null, %s, null, 1, 100, 1.5, 1.0, 'raw.fetches:0')
        on conflict do nothing
        """,
        (fund_uuid, company_uuid, _AT, ea_isin),
    )
    from truealpha_runtime.mart_refresh import MART_FUND_HOLDINGS_MV_ORDER, refresh_mart_materialized_views

    refresh_mart_materialized_views(connection, MART_FUND_HOLDINGS_MV_ORDER)

    row = connection.execute(
        "select issuer_entity, ticker, listing_id from mart.fund_holdings_resolved where fund_id = %s and isin = %s",
        (str(fund_uuid), ea_isin),
    ).fetchone()
    assert row == (str(company_uuid), "EA", "listing:xnas:ea")
