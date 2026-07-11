"""Build the research universe in the knowledge graph from ETF holdings.

For each ETF (default IVV/QQQ/AGIX/MCHI — IVV proxies the S&P 500 because SPY is
a UIT and absent from SEC's fund-ticker mapping): land the latest N-PORT-P in
raw (S3 bytes + raw.fetches pointer), resolve every equity holding line through
OpenFIGI (ISIN -> primary listing -> moomoo code; US listings additionally ->
SEC ticker -> CIK), then write:

- kg_entities (companies, ETFs) + kg_identifiers (typed crosswalk rows)
- one 'holds' edge per (ETF, company) in kg_edges
- one staging.fund_holding_facts row per holding LINE (weights/value; A-share and
  H-share lines of the same issuer stay separate rows, discriminated by ISIN)

Line vs. company: several MCHI issuers appear as TWO lines (A-share + H-share —
Ping An, CATL, Midea, BYD, observed in the 2026-02-28 filing). Lines sharing an
identical issuer name are merged into one company entity whose id comes from the
group's alphabetically-first ISIN. Fetching stays line-keyed (one moomoo code
per line) — company rollup is a KG concern.

Point-in-time: everything derived from a filing carries the FILING DATE as its
transaction_time (when it became publicly knowable); mapping assertions derived
from live lookups (SEC ticker file, OpenFIGI) carry the fetch time. Re-running
against unchanged sources skips everything (idempotent); a new N-PORT period
appends new vintages, never overwrites.

Deliberate gap, logged not guessed: an HK-listed 20-F filer held via its HK line
(e.g. BABA) gets no CIK identifier — its ordinary-share ISIN doesn't lead to the
ADR ticker SEC knows, and linking those is a separate resolution problem. Such
companies are covered by moomoo only until that lands.

Usage:
    uv run --package truealpha-data-engine python apps/data-engine/scripts/bootstrap_universe.py \
        [--etfs IVV,QQQ,AGIX,MCHI] [--figi-from-raw]
"""

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime

from data_engine import db, raw_store, universe
from data_engine.config import settings
from data_engine.sources import nport, openfigi
from data_engine.sources.sec import TICKERS_URL
from data_engine.sources.sec import client as sec_client
from factors.shared import entity_resolution as er
from truealpha_contracts import DataSource

DEFAULT_ETFS = ["IVV", "QQQ", "AGIX", "MCHI"]

# Confidence per assertion origin: filing-stated facts 1.0; OpenFIGI-derived
# mappings 0.98 (board-inferred moomoo codes drop to 0.9 inside universe.py);
# identifiers attached to a name-merged secondary line 0.9 — that merge rests
# on an exact name match within official filings, not a stated identifier.
CONF_FILING = 1.0
CONF_NAME_MERGE = 0.9


@dataclass
class EtfFetch:
    raw_id: int
    cik: int
    series_id: str
    filing_date: str  # transaction time of everything this filing asserts
    info: dict
    holdings: list[nport.Holding]

    @property
    def entity_id(self) -> str:
        return f"etf:series:{self.series_id}"

    @property
    def filing_dt(self) -> datetime:
        return datetime.fromisoformat(self.filing_date).replace(tzinfo=UTC)


def fetch_sec_ticker_map(client, conn, fetched_at: datetime) -> tuple[int, dict[str, tuple[int, str]]]:
    resp = client.get(TICKERS_URL)
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
    # Equity lines only: N-PORT also lists cash sweeps, futures, repos. Lines
    # without an assetCat are kept (the field is optional) — ISIN/listing
    # resolution downstream weeds out non-equities among those anyway.
    holdings = [h for h in all_holdings if h.asset_cat in (None, "EC") and h.pct_val is not None]
    print(
        f"{ticker}: {info['series_name']}, period {info['report_period']}, filed {filing_date}, "
        f"{len(holdings)} equity lines (dropped {len(all_holdings) - len(holdings)}) -> raw.fetches:{raw_id}"
    )
    return EtfFetch(raw_id=raw_id, cik=cik, series_id=series_id, filing_date=filing_date, info=info, holdings=holdings)


def figi_from_raw(conn, isins: list[str]) -> tuple[dict[str, list[dict]], list[str]]:
    """Rebuild the ISIN->records mapping from OpenFIGI batches already landed in
    raw — newest batch wins per ISIN. Returns (mapping, still-missing ISINs).
    This is what makes a KG rebuild cheap and offline: the keyless mapping run
    takes minutes of rate-limit waiting that a re-run shouldn't re-spend when
    the responses are already in the landing zone."""
    rows = conn.execute(
        "select id, metadata from raw.fetches where source = %s order by id",
        (DataSource.OPENFIGI.value,),
    ).fetchall()
    out: dict[str, list[dict]] = {}
    for fetch_id, metadata in rows:
        batch_isins = metadata.get("isins", [])
        results = json.loads(raw_store.get_payload(conn, fetch_id))
        for isin, job in zip(batch_isins, results):
            out[isin] = job.get("data", [])
    return {i: out[i] for i in isins if i in out}, [i for i in isins if i not in out]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--etfs", default=",".join(DEFAULT_ETFS), help="comma-separated fund tickers")
    parser.add_argument(
        "--figi-from-raw",
        action="store_true",
        help="reuse OpenFIGI mapping batches already in raw; only still-unseen ISINs hit the API",
    )
    args = parser.parse_args()
    etf_tickers = [t.strip().upper() for t in args.etfs.split(",") if t.strip()]

    run_at = datetime.now(UTC)
    today = run_at.date().isoformat()
    conn = db.connect()

    # --- fetch everything (SEC + OpenFIGI), landing verbatim payloads in raw ---
    with sec_client() as client:
        map_raw_id, ticker_map = fetch_sec_ticker_map(client, conn, run_at)
        cik_titles = {cik: title for cik, title in ticker_map.values()}
        etfs = {ticker: fetch_etf(client, conn, ticker, run_at) for ticker in etf_tickers}
        conn.commit()

        isins = sorted({h.isin for f in etfs.values() for h in f.holdings if h.isin})
        figi: dict[str, list[dict]] = {}
        if args.figi_from_raw:
            figi, isins_to_fetch = figi_from_raw(conn, isins)
            print(f"\nOpenFIGI: {len(figi)} ISINs reused from raw, {len(isins_to_fetch)} still to fetch")
        else:
            isins_to_fetch = isins
            print(
                f"\nOpenFIGI: mapping {len(isins_to_fetch)} unique ISINs "
                f"({'with' if settings.openfigi_api_key else 'no'} API key)..."
            )
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
            # Commit per batch: the keyless mapping run takes minutes, and a
            # crash near the end shouldn't discard every batch already fetched.
            conn.commit()

        if isins_to_fetch:
            figi.update(
                openfigi.map_isins(client, isins_to_fetch, api_key=settings.openfigi_api_key, on_batch=persist_batch)
            )
        print(
            f"OpenFIGI: {sum(1 for v in figi.values() if v)} mapped, "
            f"{sum(1 for v in figi.values() if not v)} unmapped, {len(figi_raw_ids)} new batches -> raw"
        )

    # --- group holding lines into companies ---
    # Group key: CIK when the primary listing is a US/SEC one; otherwise the
    # exact issuer name as filed (merges A+H lines); otherwise the ISIN alone.
    groups: dict[tuple, dict] = {}
    skipped_lines = 0
    for etf, f in etfs.items():
        for h in f.holdings:
            listing = universe.pick_listing(figi.get(h.isin, []), h.isin) if h.isin else None
            if h.isin is None and h.name is None:
                skipped_lines += 1
                continue
            cik = None
            if listing is not None:
                us_ticker = universe.sec_ticker(listing)
                if us_ticker and us_ticker in ticker_map:
                    cik = ticker_map[us_ticker][0]
            if cik is not None:
                key = ("cik", cik)
            elif h.name is not None:
                key = ("name", h.name)
            else:
                key = ("isin", h.isin)
            group = groups.setdefault(key, {"cik": None, "names": [], "lines": []})
            group["cik"] = group["cik"] or cik
            if h.name and h.name not in group["names"]:
                group["names"].append(h.name)
            group["lines"].append((etf, h, listing))

    # --- write ETF entities + their identifiers ---
    map_ref = raw_store.raw_ref(map_raw_id)
    for etf, f in etfs.items():
        er.ensure_entity(conn, f.entity_id, "etf", f.info["series_name"] or etf)
        for id_type, id_value in (("ticker", etf), ("sec_series", f.series_id)):
            er.assert_identifier(
                conn,
                entity_id=f.entity_id,
                source="sec",
                identifier_type=id_type,
                identifier_value=id_value,
                confidence=1.0,
                transaction_time=run_at,
                valid_from=today,
                raw_ref=map_ref,
            )

    # --- write company entities, identifiers, holds edges, holding facts ---
    stats = Counter()
    market_coverage = Counter()
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
            # name-only group (no ISIN on any line): nothing durable to key or
            # fetch by — log, don't guess.
            skipped_lines += len(group["lines"])
            continue

        er.ensure_entity(conn, entity_id, "company", display)
        stats["companies"] += 1
        if cik is None:
            no_cik.append(display)
        else:
            er.assert_identifier(
                conn,
                entity_id=entity_id,
                source="sec",
                identifier_type="cik",
                identifier_value=str(cik),
                confidence=0.98,
                transaction_time=run_at,
                valid_from=today,
                raw_ref=map_ref,
            )

        primary_isin = group_isins[0] if group_isins else None
        held_by: set[str] = set()
        for etf, h, listing in group["lines"]:
            f = etfs[etf]
            filing_ref = raw_store.raw_ref(f.raw_id)
            # Secondary lines of a name-merged group attach with the merge's
            # confidence, not the filing's.
            is_primary = cik is not None or h.isin == primary_isin
            line_conf = CONF_FILING if is_primary else CONF_NAME_MERGE
            for id_type, id_value in (("isin", h.isin), ("cusip", h.cusip), ("lei", h.lei)):
                if id_value:
                    er.assert_identifier(
                        conn,
                        entity_id=entity_id,
                        source="nport",
                        identifier_type=id_type,
                        identifier_value=id_value,
                        confidence=line_conf,
                        transaction_time=f.filing_dt,
                        valid_from=f.filing_date,
                        raw_ref=filing_ref,
                    )
            if listing is not None:
                us_ticker = universe.sec_ticker(listing)
                if us_ticker:
                    er.assert_identifier(
                        conn,
                        entity_id=entity_id,
                        source="openfigi",
                        identifier_type="ticker",
                        identifier_value=us_ticker,
                        confidence=0.98,
                        transaction_time=run_at,
                        valid_from=today,
                        raw_ref=map_ref,
                    )
                mm = universe.moomoo_code(listing)
                if mm is not None:
                    code, conf = mm
                    er.assert_identifier(
                        conn,
                        entity_id=entity_id,
                        source="openfigi",
                        identifier_type="moomoo",
                        identifier_value=code,
                        confidence=conf,
                        transaction_time=run_at,
                        valid_from=today,
                        raw_ref=map_ref,
                    )
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

    print("\nBootstrap summary:")
    print(
        f"  companies: {stats['companies']}  holds edges: {stats['holds_edges']}  "
        f"holding-fact lines: {stats['holding_fact_lines']}"
    )
    print(f"  moomoo listing-line coverage: {dict(sorted(market_coverage.items()))}")
    print(f"  companies without CIK (no SEC coverage — moomoo only): {len(no_cik)}")
    if stats["lines_without_value"]:
        print(f"  lines without valUSD (no holding fact written): {stats['lines_without_value']}")
    if skipped_lines:
        print(f"  skipped lines (no usable identifier): {skipped_lines}")
    print(
        "\nNext: sweep_sec_facts.py (anywhere), then probe_moomoo_nonus.py + "
        "sweep_moomoo_fundamentals.py (on the OpenD host)."
    )


if __name__ == "__main__":
    main()
