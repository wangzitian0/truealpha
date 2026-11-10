# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
import json; import xml.etree.ElementTree as ET; from dataclasses import dataclass; from data_engine.datahub.fetch_key import FetchKey; from data_engine.sources import gateway; MF_TICKERS_URL = "https://www.sec.gov/files/company_tickers_mf.json"; BROWSE_URL = ( "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={series_id}&type=NPORT-P&count=1&output=atom" ); ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/primary_doc.xml"; NS = {"n": "http://www.sec.gov/edgar/nport"}; _ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}; _PLACEHOLDERS = {"", "N/A", "000000000"}
@dataclass(frozen=True)
class Holding: name: str | None; cusip: str | None; isin: str | None; lei: str | None; balance: float | None; value_usd: float | None; pct_val: float | None; asset_cat: str | None
def fund_series(client, ticker: str) -> tuple[int, str]:
    def fetch(identity: FetchKey) -> bytes: resp = gateway.http_get( client, "nport", "mf_tickers", MF_TICKERS_URL, identity=identity, caller="nport.fund_series" ); resp.raise_for_status(); return bytes(resp.content)
    body = gateway.fetch_once(FetchKey("nport", "mf_tickers", "sec:mf-tickers", gateway.utc_day()), fetch)
    for cik, series_id, _class_id, symbol in json.loads(body)["data"]:
        if symbol.upper() == ticker.upper(): return int(cik), series_id
    raise KeyError(f"ticker not found in SEC mutual-fund/ETF mapping: {ticker}")
def latest_nport_accession(client, series_id: str) -> tuple[str, str]:
    def fetch(identity: FetchKey) -> bytes: resp = gateway.http_get( client, "nport", "browse", BROWSE_URL.format(series_id=series_id), identity=identity, caller="nport.latest_nport_accession", ); resp.raise_for_status(); return bytes(resp.content)
    body = gateway.fetch_once(FetchKey("nport", "browse", f"series:{series_id}", gateway.utc_day()), fetch); atom = ET.fromstring(body); acc = atom.findtext(".//a:entry/a:content/a:accession-number", None, _ATOM_NS); filing_date = atom.findtext(".//a:entry/a:content/a:filing-date", None, _ATOM_NS)
    if acc is None or filing_date is None: raise LookupError(f"no NPORT-P filing (with a filing date) found for series {series_id}")
    return acc.replace("-", ""), filing_date
def fetch_nport_xml(client, cik: int, accession: str) -> bytes: resp = gateway.http_get( client, "nport", "archive", ARCHIVE_URL.format(cik=cik, accession=accession), identity=FetchKey("nport", "archive", f"filing:{cik}:{accession}", gateway.utc_day()), caller="nport.fetch_nport_xml", ); resp.raise_for_status(); return resp.content
def _clean(value: str | None) -> str | None:
    if value is None or value.strip() in _PLACEHOLDERS: return None
    return value.strip()
def _number(value: str | None) -> float | None:
    cleaned = _clean(value)
    if cleaned is None: return None
    try: return float(cleaned)
    except ValueError: return None
def parse_nport(xml_bytes: bytes) -> tuple[dict, list[Holding]]:
    root = ET.fromstring(xml_bytes); gen = root.find(".//n:genInfo", NS); info = { "series_name": gen.findtext("n:seriesName", None, NS) if gen is not None else None, "report_period": gen.findtext("n:repPdDate", None, NS) if gen is not None else None, }; holdings = []
    for sec in root.findall(".//n:invstOrSec", NS): ids = sec.find("n:identifiers", NS); isin_el = ids.find("n:isin", NS) if ids is not None else None; holdings.append( Holding( name=_clean(sec.findtext("n:name", None, NS)), cusip=_clean(sec.findtext("n:cusip", None, NS)), isin=_clean(isin_el.attrib.get("value")) if isin_el is not None else None, lei=_clean(sec.findtext("n:lei", None, NS)), balance=_number(sec.findtext("n:balance", None, NS)), value_usd=_number(sec.findtext("n:valUSD", None, NS)), pct_val=_number(sec.findtext("n:pctVal", None, NS)), asset_cat=_clean(sec.findtext("n:assetCat", None, NS)), ) )
    return info, holdings
