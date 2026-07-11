"""SEC N-PORT-P: the confirmed ETF holdings-weight source (init.md Section 5,
2026-07-07). Filed per fund series; the public copy is the last month of each
fiscal quarter, so report periods lag ~1-3 months — fine for defining a research
universe, not a live holdings feed.

Pitfalls encoded here (verified on QQQ/ARKK/IVV/AGIX/MCHI):
- the raw XML is always primary_doc.xml (the filing's primaryDocument field
  points at the XSL-rendered HTML instead);
- multi-series trusts (iShares, Vanguard, ARK) must be queried by series id via
  browse-edgar, not by trust CIK, or every series' filings interleave;
- foreign holdings carry CUSIP '000000000' and some lines have literal 'N/A'
  name/cusip — normalized to None here; ISIN is the identifier that actually
  resolves (561/563 MCHI lines have one).
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass

MF_TICKERS_URL = "https://www.sec.gov/files/company_tickers_mf.json"
# browse-edgar accepts a series ID as CIK — required for multi-series trusts.
BROWSE_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={series_id}&type=NPORT-P&count=1&output=atom"
)
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/primary_doc.xml"

NS = {"n": "http://www.sec.gov/edgar/nport"}
_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}

# SEC's own placeholders for "no such identifier", seen in real filings.
_PLACEHOLDERS = {"", "N/A", "000000000"}


@dataclass(frozen=True)
class Holding:
    name: str | None
    cusip: str | None
    isin: str | None
    pct_val: float | None  # percentage of net assets
    asset_cat: str | None  # 'EC' = equity common; cash/derivative lines differ


def fund_series(client, ticker: str) -> tuple[int, str]:
    """(trust CIK, series id) for a fund ticker via SEC's official mapping.
    Raises KeyError if absent — notably SPY (a UIT) is not in this file; use an
    equivalent management-company ETF (IVV/VOO) instead."""
    resp = client.get(MF_TICKERS_URL)
    resp.raise_for_status()
    for cik, series_id, _class_id, symbol in resp.json()["data"]:
        if symbol.upper() == ticker.upper():
            return int(cik), series_id
    raise KeyError(f"ticker not found in SEC mutual-fund/ETF mapping: {ticker}")


def latest_nport_accession(client, series_id: str) -> str:
    """Accession number (dashes stripped) of the series' most recent NPORT-P."""
    resp = client.get(BROWSE_URL.format(series_id=series_id))
    resp.raise_for_status()
    atom = ET.fromstring(resp.content)
    acc = atom.findtext(".//a:entry/a:content/a:accession-number", None, _ATOM_NS)
    if acc is None:
        raise LookupError(f"no NPORT-P filing found for series {series_id}")
    return acc.replace("-", "")


def fetch_nport_xml(client, cik: int, accession: str) -> bytes:
    resp = client.get(ARCHIVE_URL.format(cik=cik, accession=accession))
    resp.raise_for_status()
    return resp.content


def _clean(value: str | None) -> str | None:
    if value is None or value.strip() in _PLACEHOLDERS:
        return None
    return value.strip()


def parse_nport(xml_bytes: bytes) -> tuple[dict, list[Holding]]:
    """(gen_info, holdings). gen_info carries series_name and report_period
    (the 'as of' date every weight in this filing describes)."""
    root = ET.fromstring(xml_bytes)
    gen = root.find(".//n:genInfo", NS)
    info = {
        "series_name": gen.findtext("n:seriesName", None, NS) if gen is not None else None,
        "report_period": gen.findtext("n:repPdDate", None, NS) if gen is not None else None,
    }
    holdings = []
    for sec in root.findall(".//n:invstOrSec", NS):
        ids = sec.find("n:identifiers", NS)
        isin_el = ids.find("n:isin", NS) if ids is not None else None
        pct_text = sec.findtext("n:pctVal", None, NS)
        holdings.append(
            Holding(
                name=_clean(sec.findtext("n:name", None, NS)),
                cusip=_clean(sec.findtext("n:cusip", None, NS)),
                isin=_clean(isin_el.attrib.get("value")) if isin_el is not None else None,
                pct_val=float(pct_text) if pct_text is not None else None,
                asset_cat=_clean(sec.findtext("n:assetCat", None, NS)),
            )
        )
    return info, holdings
