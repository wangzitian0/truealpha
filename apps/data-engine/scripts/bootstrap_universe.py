"""Build the research universe in the knowledge graph from ETF holdings.

For each ETF (default IVV/QQQ/AGIX/MCHI — IVV proxies the S&P 500 because SPY is
a UIT and absent from SEC's fund-ticker mapping): fetch the latest N-PORT-P into
raw.fetches, resolve every equity holding line through OpenFIGI (ISIN -> primary
listing -> moomoo code; US listings additionally -> SEC ticker -> CIK), and write
kg_entities + same_as/holds edges.

Line vs. company: several MCHI issuers appear as TWO lines (A-share + H-share —
Ping An, CATL, Midea, BYD, observed in the 2026-02-28 filing). Lines sharing an
identical issuer name are merged into one company entity whose id comes from the
group's alphabetically-first ISIN; every line's identifiers point at that one
entity, and each line keeps its own holds edge (discriminated by
properties.isin). Fetching stays line-keyed — company rollup is a KG concern.

Deliberate gap, logged not guessed: an HK-listed 20-F filer held via its HK line
(e.g. BABA) gets no CIK edge — its ordinary-share ISIN doesn't lead to the ADR
ticker SEC knows, and linking those is a separate resolution problem. Such
companies are covered by moomoo only until that lands.

Idempotent: unchanged assertions are skipped (see entity_resolution.add_edge);
a new N-PORT period appends new holds vintages, never overwrites (CLAUDE.md
point-in-time hard constraint).

Usage:
    uv run --package truealpha-data-engine python apps/data-engine/scripts/bootstrap_universe.py [--etfs IVV,QQQ,AGIX,MCHI]
"""

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime

from data_engine import db, raw_store, universe
from data_engine.config import settings
from data_engine.sources import nport, openfigi
from data_engine.sources.sec import TICKERS_URL, _client
from factors.shared import entity_resolution as er

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
    info: dict
    holdings: list[nport.Holding]

    @property
    def entity_id(self) -> str:
        return f"etf:series:{self.series_id}"


def fetch_sec_ticker_map(client, conn) -> dict[str, tuple[int, str]]:
    resp = client.get(TICKERS_URL)
    resp.raise_for_status()
    data = resp.json()
    raw_id = raw_store.insert_fetch(conn, source="sec", endpoint="company_tickers", entity_key="ALL", payload=data)
    print(f"SEC ticker map: {len(data)} rows -> raw.fetches:{raw_id}")
    return {row["ticker"].upper(): (int(row["cik_str"]), row["title"]) for row in data.values()}


def fetch_etf(client, conn, ticker: str) -> EtfFetch:
    cik, series_id = nport.fund_series(client, ticker)
    accession = nport.latest_nport_accession(client, series_id)
    xml = nport.fetch_nport_xml(client, cik, accession)
    info, all_holdings = nport.parse_nport(xml)
    raw_id = raw_store.insert_fetch(
        conn,
        source="sec_nport",
        endpoint="nport_p",
        entity_key=ticker,
        params={"cik": cik, "series_id": series_id, "accession": accession},
        content=xml.decode("utf-8", errors="replace"),
    )
    # Equity lines only: N-PORT also lists cash sweeps, futures, repos. Lines
    # without an assetCat are kept (the field is optional) — ISIN/listing
    # resolution downstream weeds out non-equities among those anyway.
    holdings = [h for h in all_holdings if h.asset_cat in (None, "EC") and h.pct_val is not None]
    print(
        f"{ticker}: {info['series_name']}, period {info['report_period']}, "
        f"{len(holdings)} equity lines (dropped {len(all_holdings) - len(holdings)}) -> raw.fetches:{raw_id}"
    )
    return EtfFetch(raw_id=raw_id, cik=cik, series_id=series_id, info=info, holdings=holdings)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--etfs", default=",".join(DEFAULT_ETFS), help="comma-separated fund tickers")
    args = parser.parse_args()
    etf_tickers = [t.strip().upper() for t in args.etfs.split(",") if t.strip()]

    today = datetime.now(UTC).date().isoformat()
    conn = db.connect()

    # --- fetch everything (SEC + OpenFIGI), landing verbatim payloads in raw ---
    with _client() as client:
        ticker_map = fetch_sec_ticker_map(client, conn)
        cik_titles = {cik: title for cik, title in ticker_map.values()}
        etfs = {ticker: fetch_etf(client, conn, ticker) for ticker in etf_tickers}
        conn.commit()

        isins = sorted({h.isin for f in etfs.values() for h in f.holdings if h.isin})
        print(
            f"\nOpenFIGI: mapping {len(isins)} unique ISINs "
            f"({'with' if settings.openfigi_api_key else 'no'} API key)..."
        )
        batch_ids: list[int] = []

        def persist_batch(batch, results):
            batch_ids.append(
                raw_store.insert_fetch(
                    conn,
                    source="openfigi",
                    endpoint="mapping",
                    entity_key=f"batch:{len(batch_ids)}",
                    params={"isins": batch},
                    payload=results,
                )
            )

        figi = openfigi.map_isins(client, isins, api_key=settings.openfigi_api_key, on_batch=persist_batch)
        conn.commit()
        print(
            f"OpenFIGI: {sum(1 for v in figi.values() if v)} mapped, "
            f"{sum(1 for v in figi.values() if not v)} unmapped, {len(batch_ids)} batches -> raw"
        )

    # --- group holding lines into companies ---
    # Group key: CIK when the primary listing is a US/SEC one; otherwise the
    # exact issuer name as filed (merges A+H lines); otherwise the ISIN alone.
    groups: dict[tuple, dict] = {}
    skipped_lines = 0
    for etf, f in etfs.items():
        for h in f.holdings:
            listing = universe.pick_listing(figi.get(h.isin, [])) if h.isin else None
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

    # --- write ETF entities ---
    for etf, f in etfs.items():
        er.ensure_entity(conn, f.entity_id, "etf", f.info["series_name"] or etf)
        er.add_same_as(
            conn,
            namespace="ticker",
            value=etf,
            entity_id=f.entity_id,
            confidence=1.0,
            source="sec_mf_tickers",
            valid_from=today,
        )
        er.add_same_as(
            conn,
            namespace="sec_series",
            value=f.series_id,
            entity_id=f.entity_id,
            confidence=1.0,
            source="sec_mf_tickers",
            valid_from=today,
        )

    # --- write company entities, identifier edges, holds edges ---
    stats = Counter()
    market_coverage = Counter()
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
            er.add_same_as(
                conn,
                namespace="cik",
                value=str(cik),
                entity_id=entity_id,
                confidence=0.98,
                source="openfigi+sec_tickers",
                valid_from=today,
            )

        primary_isin = group_isins[0] if group_isins else None
        seen_moomoo = set()
        for etf, h, listing in group["lines"]:
            f = etfs[etf]
            # Secondary lines of a name-merged group attach with the merge's
            # confidence, not the filing's.
            is_primary = cik is not None or h.isin == primary_isin
            line_conf = CONF_FILING if is_primary else CONF_NAME_MERGE
            line_source = "sec_nport" if is_primary else "nport_name_match"
            if h.isin:
                er.add_same_as(
                    conn,
                    namespace="isin",
                    value=h.isin,
                    entity_id=entity_id,
                    confidence=line_conf,
                    source=line_source,
                    valid_from=today,
                    raw_ref=raw_store.raw_ref(f.raw_id),
                )
            if h.cusip:
                er.add_same_as(
                    conn,
                    namespace="cusip",
                    value=h.cusip,
                    entity_id=entity_id,
                    confidence=line_conf,
                    source=line_source,
                    valid_from=today,
                    raw_ref=raw_store.raw_ref(f.raw_id),
                )
            if listing is not None:
                us_ticker = universe.sec_ticker(listing)
                if us_ticker:
                    er.add_same_as(
                        conn,
                        namespace="ticker",
                        value=us_ticker,
                        entity_id=entity_id,
                        confidence=0.98,
                        source="openfigi",
                        valid_from=today,
                    )
                mm = universe.moomoo_code(listing)
                if mm is not None:
                    code, conf = mm
                    er.add_same_as(
                        conn,
                        namespace="moomoo",
                        value=code,
                        entity_id=entity_id,
                        confidence=conf,
                        source="openfigi",
                        valid_from=today,
                    )
                    if code not in seen_moomoo:
                        seen_moomoo.add(code)
                        market_coverage[code.split(".")[0]] += 1
                else:
                    market_coverage[f"uncovered:{listing.exch_token}"] += 1
            else:
                market_coverage["unmapped"] += 1

            er.add_edge(
                conn,
                from_id=f.entity_id,
                to_id=entity_id,
                relation_type="holds",
                confidence=CONF_FILING,
                source="sec_nport",
                valid_from=f.info["report_period"] or today,
                raw_ref=raw_store.raw_ref(f.raw_id),
                properties={"pct_val": h.pct_val, "report_period": f.info["report_period"], "isin": h.isin},
            )
            stats["holds_edges"] += 1

    conn.commit()
    conn.close()

    print("\nBootstrap summary:")
    print(f"  companies: {stats['companies']}  holds edges: {stats['holds_edges']}")
    print(f"  moomoo listing-line coverage: {dict(sorted(market_coverage.items()))}")
    print(f"  companies without CIK (no SEC coverage — moomoo only): {len(no_cik)}")
    if skipped_lines:
        print(f"  skipped lines (no usable identifier): {skipped_lines}")
    print(
        "\nNext: sweep_sec_facts.py (anywhere), then probe_moomoo_nonus.py + "
        "sweep_moomoo_fundamentals.py (on the OpenD host)."
    )


if __name__ == "__main__":
    main()
