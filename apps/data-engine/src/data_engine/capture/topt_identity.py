"""Evidence-backed TOPT baseline identity and holding capture."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from factors.shared import entity_resolution as er
from truealpha_contracts import DataDomain, DataSource

from data_engine import instruments, raw_store, universe
from data_engine.capture import source_results
from data_engine.capture.topt import (
    TOPT_BASELINE_ACCESSION,
    TOPT_BASELINE_KNOWABLE_AT,
    TOPT_BASELINE_REPORT_PERIOD,
    TOPT_FUND_ID,
    TOPT_INSTRUMENTS,
    ToptInstrument,
)
from data_engine.config import settings
from data_engine.sources import nport, openfigi
from data_engine.sources.sec import TICKERS_URL
from data_engine.sources.sec import client as sec_client

MAPPING_VERSION = "topt-identity:1"
NPORT_MAPPING_VERSION = "nport-holdings:2"
OPENFIGI_MAPPING_VERSION = "openfigi-listing:2"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"


@dataclass(frozen=True)
class ToptIdentityResult:
    observed_at: datetime
    nport_raw_ref: str
    company_map_raw_ref: str
    fund_map_raw_ref: str
    figi_raw_refs: dict[str, str]
    sec_identity_raw_refs: dict[str, tuple[str, ...]]
    holding_record_ids: tuple[str, ...]
    membership_record_ids: tuple[str, ...]
    issuer_record_ids: dict[str, tuple[str, ...]]
    instrument_record_ids: dict[str, tuple[str, ...]]
    relationship_record_ids: dict[str, tuple[str, ...]]


def validate_baseline(info: dict[str, Any], holdings: list[nport.Holding]) -> dict[str, nport.Holding]:
    if info.get("report_period") != TOPT_BASELINE_REPORT_PERIOD:
        raise ValueError(
            f"TOPT report period drifted: expected {TOPT_BASELINE_REPORT_PERIOD}, got {info.get('report_period')}"
        )
    expected_isins = {item.isin for item in TOPT_INSTRUMENTS}
    us_equities = [
        holding
        for holding in holdings
        if holding.asset_cat == "EC" and holding.isin is not None and holding.isin.startswith("US")
    ]
    observed_isins = [str(holding.isin) for holding in us_equities]
    if (
        len(us_equities) != 21
        or set(observed_isins) != expected_isins
        or len(observed_isins) != len(set(observed_isins))
    ):
        missing = sorted(expected_isins - set(observed_isins))
        extra = sorted(set(observed_isins) - expected_isins)
        raise ValueError(
            f"TOPT baseline must contain exactly 21 unique selected U.S. equity lines; "
            f"count={len(us_equities)}, missing={missing}, extra={extra}"
        )
    return {holding.isin: holding for holding in us_equities if holding.isin is not None}


def _insert_json_response(conn, *, source_record_id: str, response, fetched_at: datetime, metadata=None) -> int:
    response.raise_for_status()
    return raw_store.insert_fetch(
        conn,
        source=DataSource.SEC,
        source_record_id=source_record_id,
        body=response.content,
        content_type=response.headers.get("content-type", "application/json").split(";")[0],
        fetched_at=fetched_at,
        metadata=metadata,
    )


def _openfigi_from_raw(conn, isins: list[str]) -> tuple[dict[str, list[dict]], dict[str, int]]:
    rows = conn.execute(
        "select id, metadata from raw.fetches where source = %s order by id",
        (DataSource.OPENFIGI.value,),
    ).fetchall()
    records: dict[str, list[dict]] = {}
    raw_ids: dict[str, int] = {}
    for raw_id, metadata in rows:
        batch_isins = metadata.get("isins", [])
        payload = json.loads(raw_store.get_payload(conn, raw_id))
        for isin, result in zip(batch_isins, payload):
            records[isin] = result.get("data", [])
            raw_ids[isin] = raw_id
    return (
        {isin: records[isin] for isin in isins if isin in records},
        {isin: raw_ids[isin] for isin in isins if isin in raw_ids},
    )


def _resolve_expected_identity(
    expected: ToptInstrument,
    *,
    records: list[dict],
    issuer_name: str,
    sec_ticker_map: dict[str, tuple[int, str]],
) -> tuple[universe.Listing, int, str] | None:
    listing = universe.resolve_listing(
        records,
        isin=expected.isin,
        issuer_name=issuer_name,
        sec_ticker_map=sec_ticker_map,
    )
    if listing is None or universe.sec_ticker(listing) != expected.ticker:
        return None
    sec_row = sec_ticker_map.get(expected.ticker)
    if sec_row is None or sec_row[0] != expected.issuer_cik:
        return None
    moomoo = universe.moomoo_code(listing)
    if moomoo is None or moomoo[0] != expected.moomoo_code:
        return None
    return listing, sec_row[0], sec_row[1]


def _identifier_id(conn, entity_id: str, identifier_type: str, identifier_value: str) -> int:
    row = conn.execute(
        """
        select id from staging.kg_identifiers
        where entity_id = %s and identifier_type = %s and identifier_value = %s
        order by transaction_time desc, id desc limit 1
        """,
        (entity_id, identifier_type, identifier_value),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"missing persisted identifier {entity_id}/{identifier_type}/{identifier_value}")
    return row[0]


def _edge_id(conn, from_id: str, to_id: str, relation_type: str) -> int:
    row = conn.execute(
        """
        select id from staging.kg_edges
        where from_id = %s and to_id = %s and relation_type = %s
        order by transaction_time desc, id desc limit 1
        """,
        (from_id, to_id, relation_type),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"missing persisted edge {from_id}/{relation_type}/{to_id}")
    return row[0]


def capture(conn, *, reuse_openfigi_raw: bool = False, fetched_at: datetime | None = None) -> ToptIdentityResult:
    observed_at = fetched_at or datetime.now(UTC)
    with sec_client() as client:
        company_response = client.get(TICKERS_URL)
        company_map_raw_id = _insert_json_response(
            conn,
            source_record_id="company_tickers",
            response=company_response,
            fetched_at=observed_at,
        )
        company_payload = company_response.json()
        company_map = {
            str(row["ticker"]).upper(): (int(row["cik_str"]), str(row["title"])) for row in company_payload.values()
        }

        fund_response = client.get(nport.MF_TICKERS_URL)
        fund_map_raw_id = _insert_json_response(
            conn,
            source_record_id="fund_tickers",
            response=fund_response,
            fetched_at=observed_at,
        )
        fund_rows = fund_response.json().get("data", [])
        fund_match = next((row for row in fund_rows if str(row[3]).upper() == "TOPT"), None)
        if fund_match is None or fund_match[1] != "S000088434":
            raise ValueError("SEC fund map no longer resolves TOPT to series S000088434")
        fund_cik = int(fund_match[0])

        xml = nport.fetch_nport_xml(client, fund_cik, TOPT_BASELINE_ACCESSION)
        nport_raw_id = raw_store.insert_fetch(
            conn,
            source=DataSource.NPORT,
            source_record_id=f"TOPT:{TOPT_BASELINE_ACCESSION}",
            body=xml,
            content_type="application/xml",
            fetched_at=observed_at,
            source_published_at=TOPT_BASELINE_KNOWABLE_AT,
            metadata={"cik": fund_cik, "series_id": "S000088434", "ticker": "TOPT"},
        )
        info, all_holdings = nport.parse_nport(xml)
        holdings = validate_baseline(info, all_holdings)

        isins = sorted(holdings)
        figi_records: dict[str, list[dict]] = {}
        figi_raw_ids: dict[str, int] = {}
        if reuse_openfigi_raw:
            figi_records, figi_raw_ids = _openfigi_from_raw(conn, isins)
        missing_isins = [isin for isin in isins if isin not in figi_records]

        def persist_batch(batch, results):
            raw_id = raw_store.insert_json_fetch(
                conn,
                source=DataSource.OPENFIGI,
                source_record_id=f"mapping:{batch[0]}",
                payload=results,
                fetched_at=observed_at,
                metadata={"isins": batch},
            )
            figi_raw_ids.update({isin: raw_id for isin in batch})

        if missing_isins:
            figi_records.update(
                openfigi.map_isins(
                    client,
                    missing_isins,
                    api_key=settings.openfigi_api_key,
                    on_batch=persist_batch,
                )
            )

    if set(figi_records) != set(isins) or set(figi_raw_ids) != set(isins):
        raise ValueError("OpenFIGI capture did not produce raw-backed results for all 21 TOPT instruments")

    nport_ref = raw_store.raw_ref(nport_raw_id)
    company_map_ref = raw_store.raw_ref(company_map_raw_id)
    fund_map_ref = raw_store.raw_ref(fund_map_raw_id)
    report_period = TOPT_BASELINE_REPORT_PERIOD
    fund_name = str(info.get("series_name") or "iShares Top 20 U.S. Stocks ETF")
    er.ensure_entity(conn, TOPT_FUND_ID, "etf", fund_name)
    for identifier_type, value in (("ticker", "TOPT"), ("sec_series", "S000088434")):
        er.assert_identifier(
            conn,
            entity_id=TOPT_FUND_ID,
            source="sec",
            identifier_type=identifier_type,
            identifier_value=value,
            confidence=1.0,
            transaction_time=observed_at,
            valid_from=observed_at.date().isoformat(),
            raw_ref=fund_map_ref,
        )

    holding_ids: list[str] = []
    membership_ids: list[str] = []
    issuer_ids: dict[str, list[str]] = {}
    instrument_ids: dict[str, list[str]] = {}
    relationship_ids: dict[str, tuple[str, ...]] = {}
    figi_refs: dict[str, str] = {}
    sec_identity_refs: dict[str, tuple[str, ...]] = {}
    submissions_cache: dict[str, tuple[dict[str, tuple[int, str]], str]] = {}

    for expected in TOPT_INSTRUMENTS:
        holding = holdings[expected.isin]
        resolved = _resolve_expected_identity(
            expected,
            records=figi_records[expected.isin],
            issuer_name=holding.name or expected.issuer_name,
            sec_ticker_map=company_map,
        )
        identity_raw_refs: tuple[str, ...] = (company_map_ref,)
        if resolved is None:
            cached = submissions_cache.get(expected.issuer_id)
            if cached is None:
                with sec_client() as submissions_client:
                    submissions_response = submissions_client.get(SUBMISSIONS_URL.format(cik=expected.issuer_cik))
                submissions_raw_id = _insert_json_response(
                    conn,
                    source_record_id=f"submissions:CIK{expected.issuer_cik:010d}",
                    response=submissions_response,
                    fetched_at=observed_at,
                )
                payload = submissions_response.json()
                if int(payload.get("cik", 0)) != expected.issuer_cik:
                    raise ValueError(f"SEC submissions CIK drift for {expected.isin}")
                submissions_map = {
                    str(ticker).upper(): (expected.issuer_cik, str(payload.get("name") or ""))
                    for ticker in payload.get("tickers", [])
                }
                cached = submissions_map, raw_store.raw_ref(submissions_raw_id)
                submissions_cache[expected.issuer_id] = cached
            submissions_map, submissions_ref = cached
            resolved = _resolve_expected_identity(
                expected,
                records=figi_records[expected.isin],
                issuer_name=holding.name or expected.issuer_name,
                sec_ticker_map=submissions_map,
            )
            identity_raw_refs = (company_map_ref, submissions_ref)
        if resolved is None:
            raise ValueError(f"mandatory TOPT listing unresolved: {expected.isin}")
        listing, observed_cik, sec_name = resolved
        moomoo = universe.moomoo_code(listing)
        assert moomoo is not None

        issuer_id = expected.issuer_id
        instrument_id = expected.instrument_id
        sec_identity_refs[issuer_id] = identity_raw_refs
        figi_ref = raw_store.raw_ref(figi_raw_ids[expected.isin])
        figi_refs[instrument_id] = figi_ref
        er.ensure_entity(conn, issuer_id, "company", sec_name)
        er.assert_identifier(
            conn,
            entity_id=issuer_id,
            source="sec",
            identifier_type="cik",
            identifier_value=str(observed_cik),
            confidence=listing.confidence,
            transaction_time=observed_at,
            valid_from=observed_at.date().isoformat(),
            raw_ref=identity_raw_refs[-1],
        )
        issuer_ids.setdefault(issuer_id, [f"staging.kg_entities:{issuer_id}"])
        cik_id = f"staging.kg_identifiers:{_identifier_id(conn, issuer_id, 'cik', str(observed_cik))}"
        if cik_id not in issuer_ids[issuer_id]:
            issuer_ids[issuer_id].append(cik_id)

        instruments.ensure_instrument(conn, instrument_id, "equity_common", holding.name or expected.ticker)
        record_ids = [f"staging.instruments:{instrument_id}"]
        link_id = instruments.assert_issuer_link(
            conn,
            instrument_id=instrument_id,
            issuer_id=issuer_id,
            valid_from=report_period,
            transaction_time=observed_at,
            confidence=Decimal(str(listing.confidence)),
            source=listing.resolution_method,
            raw_ref=figi_ref,
            mapping_version=OPENFIGI_MAPPING_VERSION,
        )
        if link_id is None:
            link_id = conn.execute(
                """
                select id from staging.instrument_issuer_links where instrument_id = %s
                order by transaction_time desc, id desc limit 1
                """,
                (instrument_id,),
            ).fetchone()[0]
        record_ids.append(f"staging.instrument_issuer_links:{link_id}")

        for identifier_type, value, source, transaction_time, raw_ref, confidence, mapping in (
            ("isin", expected.isin, "nport", TOPT_BASELINE_KNOWABLE_AT, nport_ref, Decimal("1"), NPORT_MAPPING_VERSION),
            (
                "cusip",
                expected.cusip,
                "nport",
                TOPT_BASELINE_KNOWABLE_AT,
                nport_ref,
                Decimal("1"),
                NPORT_MAPPING_VERSION,
            ),
            (
                "ticker",
                expected.ticker,
                listing.resolution_method,
                observed_at,
                figi_ref,
                Decimal(str(listing.confidence)),
                OPENFIGI_MAPPING_VERSION,
            ),
            (
                "moomoo",
                expected.moomoo_code,
                listing.resolution_method,
                observed_at,
                figi_ref,
                Decimal(str(moomoo[1])),
                OPENFIGI_MAPPING_VERSION,
            ),
        ):
            identifier_id = instruments.assert_identifier(
                conn,
                instrument_id=instrument_id,
                identifier_type=identifier_type,
                identifier_value=value,
                valid_from=report_period,
                transaction_time=transaction_time,
                confidence=confidence,
                source=source,
                raw_ref=raw_ref,
                mapping_version=mapping,
            )
            if identifier_id is None:
                identifier_id = conn.execute(
                    """
                    select id from staging.instrument_identifiers
                    where identifier_type = %s and identifier_value = %s
                    order by transaction_time desc, id desc limit 1
                    """,
                    (identifier_type, value),
                ).fetchone()[0]
            record_ids.append(f"staging.instrument_identifiers:{identifier_id}")

        listing_id = f"listing:vendor:{expected.moomoo_code}"
        currency, timezone_name, calendar = universe.market_metadata(listing.exch_token)
        listing_row_id = instruments.assert_listing(
            conn,
            listing_id=listing_id,
            instrument_id=instrument_id,
            venue_code=listing.exch_token,
            ticker=listing.ticker,
            currency=currency,
            trading_timezone=timezone_name,
            trading_calendar=calendar,
            price_policy="raw_plus_actions",
            is_primary=True,
            valid_from=observed_at.date().isoformat(),
            transaction_time=observed_at,
            confidence=Decimal(str(listing.confidence)),
            source=listing.resolution_method,
            raw_ref=figi_ref,
            mapping_version=OPENFIGI_MAPPING_VERSION,
        )
        if listing_row_id is None:
            listing_row_id = conn.execute(
                "select id from staging.listings where listing_id = %s order by transaction_time desc, id desc limit 1",
                (listing_id,),
            ).fetchone()[0]
        record_ids.append(f"staging.listings:{listing_row_id}")
        instrument_ids[instrument_id] = record_ids

        membership_id = instruments.assert_membership(
            conn,
            universe_id=TOPT_FUND_ID,
            universe_version=report_period,
            fund_id=TOPT_FUND_ID,
            issuer_id=issuer_id,
            instrument_id=instrument_id,
            listing_id=listing_id,
            valid_from=report_period,
            transaction_time=TOPT_BASELINE_KNOWABLE_AT,
            confidence=Decimal("1"),
            source="nport",
            raw_ref=nport_ref,
            mapping_version=NPORT_MAPPING_VERSION,
        )
        if membership_id is None:
            membership_id = conn.execute(
                """
                select id from staging.universe_memberships
                where universe_id = %s and universe_version = %s and instrument_id = %s
                order by transaction_time desc, id desc limit 1
                """,
                (TOPT_FUND_ID, report_period, instrument_id),
            ).fetchone()[0]
        membership_ids.append(f"staging.universe_memberships:{membership_id}")

        er.add_edge(
            conn,
            from_id=TOPT_FUND_ID,
            to_id=issuer_id,
            relation_type="holds",
            confidence=1.0,
            source="nport",
            transaction_time=TOPT_BASELINE_KNOWABLE_AT,
            valid_from=report_period,
            raw_ref=nport_ref,
        )
        relationship_ids[issuer_id] = (f"staging.kg_edges:{_edge_id(conn, TOPT_FUND_ID, issuer_id, 'holds')}",)

        if holding.value_usd is None or holding.pct_val is None:
            raise ValueError(f"TOPT holding lost value/weight: {expected.isin}")
        holding_row = conn.execute(
            """
            insert into staging.fund_holding_facts
                (fund_id, holding_id, holding_name, report_period, transaction_time,
                 cusip, isin, lei, balance, value_usd, percent_of_net_assets,
                 confidence, raw_ref, instrument_id, listing_id, asset_type,
                 currency, mapping_version)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s,
                    %s, %s, 'equity_common', 'USD', %s)
            on conflict do nothing returning id
            """,
            (
                TOPT_FUND_ID,
                issuer_id,
                holding.name or expected.ticker,
                report_period,
                TOPT_BASELINE_KNOWABLE_AT,
                holding.cusip,
                holding.isin,
                holding.lei,
                holding.balance,
                holding.value_usd,
                holding.pct_val,
                nport_ref,
                instrument_id,
                listing_id,
                NPORT_MAPPING_VERSION,
            ),
        ).fetchone()
        if holding_row is None:
            holding_row = conn.execute(
                """
                select id from staging.fund_holding_facts
                where fund_id = %s and instrument_id = %s and report_period = %s
                  and transaction_time = %s and raw_ref = %s and mapping_version = %s
                """,
                (
                    TOPT_FUND_ID,
                    instrument_id,
                    report_period,
                    TOPT_BASELINE_KNOWABLE_AT,
                    nport_ref,
                    NPORT_MAPPING_VERSION,
                ),
            ).fetchone()
        if holding_row is None:
            raise RuntimeError(f"could not persist TOPT holding {instrument_id}")
        holding_ids.append(f"staging.fund_holding_facts:{holding_row[0]}")

    if len(holding_ids) != 21 or len(membership_ids) != 21 or len(issuer_ids) != 20 or len(instrument_ids) != 21:
        raise RuntimeError("TOPT identity persistence counts do not match the frozen scope")
    return ToptIdentityResult(
        observed_at=observed_at,
        nport_raw_ref=nport_ref,
        company_map_raw_ref=company_map_ref,
        fund_map_raw_ref=fund_map_ref,
        figi_raw_refs=figi_refs,
        sec_identity_raw_refs=sec_identity_refs,
        holding_record_ids=tuple(sorted(holding_ids)),
        membership_record_ids=tuple(sorted(membership_ids)),
        issuer_record_ids={key: tuple(sorted(value)) for key, value in issuer_ids.items()},
        instrument_record_ids={key: tuple(sorted(value)) for key, value in instrument_ids.items()},
        relationship_record_ids={key: tuple(sorted(value)) for key, value in relationship_ids.items()},
    )


def emit_source_results(conn, *, run_id: str, scope, result: ToptIdentityResult, attempt: int = 0) -> tuple[int, ...]:
    requirements = scope.requirement_map()
    ids: list[int] = []

    def emit(subject_id, domain, source, raw_refs, record_ids, fields, confidence, mapping_version):
        requirement = next(
            requirement for key, requirement in requirements.items() if key[0] == subject_id and key[1] is domain
        )
        ids.append(
            source_results.put(
                conn,
                source_results.CaptureSourceResult(
                    run_id=run_id,
                    subject_id=subject_id,
                    domain=domain,
                    partition_key=requirement.partition_key,
                    source=source,
                    outcome=source_results.SourceResultOutcome.SUCCESS,
                    raw_refs=tuple(raw_refs),
                    domain_record_ids=tuple(record_ids),
                    observed_fields=tuple(fields),
                    min_knowable_at=TOPT_BASELINE_KNOWABLE_AT,
                    max_knowable_at=TOPT_BASELINE_KNOWABLE_AT,
                    observed_at=result.observed_at,
                    confidence=Decimal(str(confidence)),
                    mapping_version=mapping_version,
                    attempt=attempt,
                ),
            )
        )

    emit(
        TOPT_FUND_ID,
        DataDomain.FUND_HOLDINGS,
        DataSource.NPORT,
        (result.nport_raw_ref,),
        result.holding_record_ids,
        ("instrument_id", "report_period", "knowable_at", "value", "weight", "currency"),
        1,
        NPORT_MAPPING_VERSION,
    )
    emit(
        TOPT_FUND_ID,
        DataDomain.UNIVERSE,
        DataSource.NPORT,
        (result.nport_raw_ref,),
        result.membership_record_ids,
        ("issuer_id", "instrument_id", "valid_from", "knowable_at"),
        1,
        NPORT_MAPPING_VERSION,
    )
    for issuer_id, record_ids in result.issuer_record_ids.items():
        sec_raw_refs = result.sec_identity_raw_refs[issuer_id]
        emit(
            issuer_id,
            DataDomain.ENTITY_IDENTITY,
            DataSource.SEC,
            (*sec_raw_refs, result.nport_raw_ref),
            record_ids,
            ("cik", "issuer_name"),
            Decimal("0.9"),
            MAPPING_VERSION,
        )
        emit(
            issuer_id,
            DataDomain.KNOWLEDGE_GRAPH,
            DataSource.NPORT,
            (result.nport_raw_ref,),
            result.relationship_record_ids[issuer_id],
            ("counterparty_id", "relation_type", "valid_from", "evidence_span"),
            1,
            NPORT_MAPPING_VERSION,
        )
    issuer_by_instrument = {item.instrument_id: item.issuer_id for item in TOPT_INSTRUMENTS}
    for instrument_id, record_ids in result.instrument_record_ids.items():
        sec_raw_refs = result.sec_identity_raw_refs[issuer_by_instrument[instrument_id]]
        emit(
            instrument_id,
            DataDomain.INSTRUMENTS,
            DataSource.OPENFIGI,
            (
                result.nport_raw_ref,
                result.figi_raw_refs[instrument_id],
                *sec_raw_refs,
            ),
            record_ids,
            ("issuer_id", "isin", "cusip", "ticker", "listing", "currency"),
            Decimal("0.9"),
            OPENFIGI_MAPPING_VERSION,
        )
    return tuple(ids)
