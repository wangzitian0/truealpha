"""Build the research universe in the knowledge graph from ETF N-PORT holdings."""

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime

from data_engine import db, raw_store, universe
from data_engine.config import settings
from data_engine.datahub.production_topt.holdings_enrichment import figi_from_raw
from data_engine.sources import gateway, nport, openfigi
from data_engine.sources.sec import TICKERS_URL, company_tickers_key
from data_engine.sources.sec import client as sec_client
from factors.shared import entity_resolution as er
from truealpha_contracts.models import DataSource

DEFAULT_ETFS = ["IVV", "QQQ", "AGIX", "MCHI"]
CONF_FILING = 1.0
CONF_NAME_MERGE = 0.9


@dataclass
class EtfFetch:
    raw_id: int
    cik: int
    series_id: str
    filing_date: str
    info: dict
    holdings: list[nport.Holding]

    @property
    def entity_id(self) -> str:
        return f"etf:series:{self.series_id}"

    @property
    def filing_dt(self) -> datetime:
        return datetime.fromisoformat(self.filing_date).replace(tzinfo=UTC)


def fetch_sec_ticker_map(client, conn, fetched_at: datetime) -> tuple[int, dict[str, tuple[int, str]]]:
    resp = gateway.http_get(
        client,
        "sec",
        "company_tickers",
        TICKERS_URL,
        identity=company_tickers_key(),
        caller="bootstrap_universe.fetch_sec_ticker_map",
    )
    resp.raise_for_status()
    data = resp.json()
    raw_id = raw_store.insert_fetch(
        conn,
        source=DataSource.SEC,
        source_record_id="company_tickers",
        body=resp.content,
        content_type="application/json",
        fetched_at=fetched_at,
    )
    print(f"SEC ticker map: {len(data)} rows -> raw.fetches:{raw_id}")
    return raw_id, {row["ticker"].upper(): (int(row["cik_str"]), row["title"]) for row in data.values()}


def fetch_etf(client, conn, ticker: str, fetched_at: datetime) -> EtfFetch:
    cik, series_id = nport.fund_series(client, ticker)
    accession, filing_date = nport.latest_nport_accession(client, series_id)
    xml = nport.fetch_nport_xml(client, cik, accession)
    info, all_holdings = nport.parse_nport(xml)
    raw_id = raw_store.insert_fetch(
        conn,
        source=DataSource.NPORT,
        source_record_id=f"{ticker}:{accession}",
        body=xml,
        content_type="application/xml",
        fetched_at=fetched_at,
        source_published_at=datetime.fromisoformat(filing_date).replace(tzinfo=UTC),
        metadata={"cik": cik, "series_id": series_id, "ticker": ticker},
    )
    holdings = [h for h in all_holdings if h.asset_cat in (None, "EC") and h.pct_val is not None]
    print(
        f"{ticker}: {info['series_name']}, period {info['report_period']}, filed {filing_date}, "
        f"{len(holdings)} equity lines (dropped {len(all_holdings) - len(holdings)}) -> raw.fetches:{raw_id}"
    )
    return EtfFetch(raw_id=raw_id, cik=cik, series_id=series_id, filing_date=filing_date, info=info, holdings=holdings)


def _assert_id(
    conn,
    entity_id: str,
    source: str,
    id_type: str,
    id_val: str,
    conf: float,
    tx_time: datetime,
    valid_from: str,
    raw_ref: str,
) -> None:
    er.assert_identifier(
        conn,
        entity_id=entity_id,
        source=source,
        identifier_type=id_type,
        identifier_value=id_val,
        confidence=conf,
        transaction_time=tx_time,
        valid_from=valid_from,
        raw_ref=raw_ref,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--etfs", default=",".join(DEFAULT_ETFS))
    parser.add_argument("--figi-from-raw", action="store_true")
    args = parser.parse_args()
    etf_tickers = [t.strip().upper() for t in args.etfs.split(",") if t.strip()]

    run_at = datetime.now(UTC)
    today = run_at.date().isoformat()
    conn = db.connect()

    with sec_client() as client:
        map_raw_id, ticker_map = fetch_sec_ticker_map(client, conn, run_at)
        cik_titles = {cik: title for cik, title in ticker_map.values()}
        etfs = {ticker: fetch_etf(client, conn, ticker, run_at) for ticker in etf_tickers}
        conn.commit()

        isins = sorted({h.isin for f in etfs.values() for h in f.holdings if h.isin})
        figi, isins_to_fetch = figi_from_raw(conn, isins) if args.figi_from_raw else ({}, isins)
        figi_raw_ids: list[int] = []

        def persist_batch(batch, results):
            figi_raw_ids.append(
                raw_store.insert_json_fetch(
                    conn,
                    source=DataSource.OPENFIGI,
                    source_record_id=f"mapping:{batch[0]}",
                    payload=results,
                    fetched_at=run_at,
                    metadata={"isins": batch},
                )
            )
            conn.commit()

        if isins_to_fetch:
            figi.update(
                openfigi.map_isins(client, isins_to_fetch, api_key=settings.openfigi_api_key, on_batch=persist_batch)
            )

    groups: dict[tuple, dict] = {}
    skipped_lines = 0
    for etf, f in etfs.items():
        for h in f.holdings:
            listing = universe.pick_listing(figi.get(h.isin, []), h.isin) if h.isin else None
            if h.isin is None and h.name is None:
                skipped_lines += 1
                continue
            us_ticker = universe.sec_ticker(listing) if listing is not None else None
            cik = ticker_map[us_ticker][0] if us_ticker and us_ticker in ticker_map else None
            key = ("cik", cik) if cik is not None else (("name", h.name) if h.name is not None else ("isin", h.isin))
            group = groups.setdefault(key, {"cik": None, "names": [], "lines": []})
            group["cik"] = group["cik"] or cik
            if h.name and h.name not in group["names"]:
                group["names"].append(h.name)
            group["lines"].append((etf, h, listing))

    map_ref = raw_store.raw_ref(map_raw_id)
    for etf, f in etfs.items():
        er.ensure_entity(conn, f.entity_id, "etf", f.info["series_name"] or etf)
        for id_type, id_val in (("ticker", etf), ("sec_series", f.series_id)):
            _assert_id(conn, f.entity_id, "sec", id_type, id_val, 1.0, run_at, today, map_ref)

    stats: Counter = Counter()
    market_coverage: Counter = Counter()
    seen_codes: set[str] = set()
    no_cik = []

    for _key, group in sorted(groups.items(), key=lambda kv: str(kv[0])):
        cik = group["cik"]
        group_isins = sorted({h.isin for _, h, _ in group["lines"] if h.isin})
        if cik is not None:
            entity_id = f"company:cik:{cik}"
            display = cik_titles.get(cik) or (group["names"][0] if group["names"] else f"CIK {cik}")
        elif group_isins:
            entity_id = f"company:isin:{group_isins[0]}"
            display = group["names"][0] if group["names"] else group_isins[0]
        else:
            skipped_lines += len(group["lines"])
            continue

        er.ensure_entity(conn, entity_id, "company", display)
        stats["companies"] += 1
        if cik is None:
            no_cik.append(display)
        else:
            _assert_id(conn, entity_id, "sec", "cik", str(cik), 0.98, run_at, today, map_ref)

        primary_isin = group_isins[0] if group_isins else None
        held_by: set[str] = set()
        for etf, h, listing in group["lines"]:
            f = etfs[etf]
            filing_ref = raw_store.raw_ref(f.raw_id)
            line_conf = CONF_FILING if (cik is not None or h.isin == primary_isin) else CONF_NAME_MERGE
            for id_type, id_val in (("isin", h.isin), ("cusip", h.cusip), ("lei", h.lei)):
                if id_val:
                    _assert_id(
                        conn, entity_id, "nport", id_type, id_val, line_conf, f.filing_dt, f.filing_date, filing_ref
                    )
            if listing is not None:
                us_ticker = universe.sec_ticker(listing)
                if us_ticker:
                    _assert_id(conn, entity_id, "openfigi", "ticker", us_ticker, 0.98, run_at, today, map_ref)
                mm_pair = universe.moomoo_code(listing)
                if mm_pair is not None:
                    code, conf = mm_pair
                    _assert_id(conn, entity_id, "openfigi", "moomoo", code, conf, run_at, today, map_ref)
                    if code not in seen_codes:
                        seen_codes.add(code)
                        market_coverage[code.split(".")[0]] += 1
                else:
                    market_coverage[f"uncovered:{listing.exch_token}"] += 1
            else:
                market_coverage["unmapped"] += 1

            if f.entity_id not in held_by:
                held_by.add(f.entity_id)
                er.add_edge(
                    conn,
                    from_id=f.entity_id,
                    to_id=entity_id,
                    relation_type="holds",
                    confidence=CONF_FILING,
                    source="nport",
                    transaction_time=f.filing_dt,
                    valid_from=f.info["report_period"] or f.filing_date,
                    raw_ref=filing_ref,
                )
                stats["holds_edges"] += 1

            if h.value_usd is None:
                stats["lines_without_value"] += 1
                continue
            conn.execute(
                """
                insert into staging.fund_holding_facts
                    (fund_id, holding_id, holding_name, report_period, transaction_time,
                     cusip, isin, lei, balance, value_usd, percent_of_net_assets, confidence, raw_ref)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict do nothing
                """,
                (
                    f.entity_id,
                    entity_id,
                    h.name or h.isin or "UNKNOWN",
                    f.info["report_period"] or f.filing_date,
                    f.filing_dt,
                    h.cusip,
                    h.isin,
                    h.lei,
                    h.balance,
                    h.value_usd,
                    h.pct_val,
                    line_conf,
                    filing_ref,
                ),
            )
            stats["holding_fact_lines"] += 1

    conn.commit()
    conn.close()
    print(
        f"companies={stats['companies']} holds_edges={stats['holds_edges']} holding_facts={stats['holding_fact_lines']}"
    )


if __name__ == "__main__":
    with gateway.script_run("bootstrap_universe"):
        main()
