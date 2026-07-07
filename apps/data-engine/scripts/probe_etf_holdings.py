"""Phase -1 recon: can SEC N-PORT-P filings serve as the ETF holdings-weight source?

This probes one open item of the data availability matrix (init.md Section 5):
N-PORT-P is filed monthly per fund series and carries per-holding `pctVal`
(percentage of net assets) — exactly what the ETF-virtual-company module needs.

Usage:
    uv run --package truealpha-data-engine python apps/data-engine/scripts/probe_etf_holdings.py [TICKER]

Defaults to QQQ (Invesco QQQ Trust is a single-series trust, so the latest
NPORT-P is unambiguous). Raw XML lands in apps/data-engine/data/samples/.
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from data_engine.sources.sec import _client

MF_TICKERS_URL = "https://www.sec.gov/files/company_tickers_mf.json"
# browse-edgar accepts a series ID as CIK — required for multi-series trusts (e.g. ARK),
# where the trust-level submissions feed interleaves every series' filings.
BROWSE_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={series_id}&type=NPORT-P&count=1&output=atom"
)
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc}"

OUT_DIR = Path(__file__).resolve().parents[1] / "data" / "samples"
NS = {"n": "http://www.sec.gov/edgar/nport"}


def fund_cik(client, ticker: str) -> tuple[int, str]:
    resp = client.get(MF_TICKERS_URL)
    resp.raise_for_status()
    data = resp.json()["data"]  # rows of [cik, seriesId, classId, symbol]
    for cik, series_id, _class_id, symbol in data:
        if symbol.upper() == ticker.upper():
            return int(cik), series_id
    raise KeyError(f"ticker not found in SEC mutual-fund/ETF mapping: {ticker}")


def latest_nport(client, series_id: str) -> tuple[str, str]:
    resp = client.get(BROWSE_URL.format(series_id=series_id))
    resp.raise_for_status()
    atom = ET.fromstring(resp.content)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    acc = atom.findtext(".//a:entry/a:content/a:accession-number", None, ns)
    if acc is None:
        raise LookupError(f"no NPORT-P filing found for series {series_id}")
    # The raw XML is always primary_doc.xml in the accession directory
    # (the filing's primaryDocument field points at the XSL-rendered HTML instead).
    return acc.replace("-", ""), "primary_doc.xml"


def main() -> None:
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "QQQ").upper()
    with _client() as client:
        cik, series_id = fund_cik(client, ticker)
        print(f"{ticker}: CIK {cik}, series {series_id}")
        accession, doc = latest_nport(client, series_id)
        resp = client.get(ARCHIVE_URL.format(cik=cik, accession=accession, doc=doc))
        resp.raise_for_status()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{ticker}_NPORT_{accession}.xml"
    path.write_bytes(resp.content)

    root = ET.fromstring(resp.content)
    gen = root.find(".//n:genInfo", NS)
    series = gen.findtext("n:seriesName", "?", NS) if gen is not None else "?"
    period = gen.findtext("n:repPdDate", "?", NS) if gen is not None else "?"
    holdings = []
    for sec in root.findall(".//n:invstOrSec", NS):
        name = sec.findtext("n:name", "?", NS)
        cusip = sec.findtext("n:cusip", "?", NS)
        pct = sec.findtext("n:pctVal", None, NS)
        if pct is not None:
            holdings.append((float(pct), name, cusip))
    holdings.sort(reverse=True)

    print(f"series: {series}, report period: {period}")
    print(f"holdings with pctVal: {len(holdings)} (sum {sum(h[0] for h in holdings):.2f}%)")
    print("top 10 by weight:")
    for pct, name, cusip in holdings[:10]:
        print(f"  {pct:6.2f}%  {name}  (CUSIP {cusip})")
    print(f"\nRaw XML saved to {path}")


if __name__ == "__main__":
    main()
