"""Phase -1 recon: can SEC N-PORT-P filings serve as the ETF holdings-weight source?

Confirmed (init.md Section 5) — the fetch/parse machinery now lives in
data_engine.sources.nport, shared with bootstrap_universe.py; this stays as the
quick one-ETF eyeball tool that writes the raw XML locally without needing a
database.

Usage:
    uv run --package truealpha-data-engine python apps/data-engine/scripts/probe_etf_holdings.py [TICKER]

Defaults to QQQ (Invesco QQQ Trust is a single-series trust, so the latest
NPORT-P is unambiguous). Raw XML lands in apps/data-engine/data/samples/.
"""

import sys
from pathlib import Path

from data_engine.sources import nport
from data_engine.sources.sec import client as sec_client

OUT_DIR = Path(__file__).resolve().parents[1] / "data" / "samples"


def main() -> None:
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "QQQ").upper()
    with sec_client() as client:
        cik, series_id = nport.fund_series(client, ticker)
        print(f"{ticker}: CIK {cik}, series {series_id}")
        accession, filing_date = nport.latest_nport_accession(client, series_id)
        print(f"latest NPORT-P filed {filing_date}")
        xml = nport.fetch_nport_xml(client, cik, accession)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{ticker}_NPORT_{accession}.xml"
    path.write_bytes(xml)

    info, holdings = nport.parse_nport(xml)
    weighted = sorted(
        ((h.pct_val, h.name or "?", h.cusip or "?") for h in holdings if h.pct_val is not None), reverse=True
    )
    print(f"series: {info['series_name']}, report period: {info['report_period']}")
    print(f"holdings with pctVal: {len(weighted)} (sum {sum(w[0] for w in weighted):.2f}%)")
    print("top 10 by weight:")
    for pct, name, cusip in weighted[:10]:
        print(f"  {pct:6.2f}%  {name}  (CUSIP {cusip})")
    print(f"\nRaw XML saved to {path}")


if __name__ == "__main__":
    main()
