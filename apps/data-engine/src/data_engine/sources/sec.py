# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
import json; import re; from pathlib import Path; from typing import Any; import httpx; from data_engine.config import settings; from data_engine.datahub.fetch_key import FetchKey; from data_engine.sources import gateway; from data_engine.sources.accept_rules import accept_rule; TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"; TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"; COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"; SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"; EXCHANGE_MIC_MAP: dict[str, str] = {"NYSE": "XNYS", "NASDAQ": "XNAS", "CBOE": "BATS"}; _CORP_SUFFIXES = { "INC", "CORP", "CORPORATION", "CO", "COMPANY", "HOLDINGS", "HOLDING", "LTD", "PLC", "NEW", "THE", "DE", "MD", }
def normalize_company_title(title: str) -> str: cleaned = re.sub(r"/[A-Z]+/", " ", title.upper()); tokens = [tok for tok in re.split(r"[^A-Z0-9]+", cleaned) if tok and tok not in _CORP_SUFFIXES]; return "".join(tokens)
def company_facts_key(cik: int) -> FetchKey: return FetchKey("sec", "companyfacts", f"issuer:cik:{cik:010d}", gateway.utc_day())
def submissions_key(cik: int, *, overflow: str | None = None) -> FetchKey: entity = f"issuer:cik:{cik:010d}" if overflow is None else f"issuer:cik:{cik:010d}:{overflow}"; return FetchKey("sec", "submissions", entity, gateway.utc_day())
def company_tickers_key() -> FetchKey: return FetchKey("sec", "company_tickers", "sec:company-tickers", gateway.utc_day())
def company_tickers_exchange_key() -> FetchKey: return FetchKey("sec", "company_tickers_exchange", "sec:company-tickers-exchange", gateway.utc_day())
def client() -> httpx.Client:
    if not settings.sec_user_agent: raise RuntimeError("Set SEC_USER_AGENT (must include a contact email) — see .env.example")
    return httpx.Client(headers={"User-Agent": settings.sec_user_agent}, timeout=30.0, follow_redirects=True)
def ticker_cik_index(http: httpx.Client | None = None, *, via: gateway.SourceGateway | None = None) -> dict[str, int]:
    def fetch(identity: FetchKey) -> bytes:
        def request() -> bytes:
            if http is None:
                with client() as c: return _get(c, "company_tickers", TICKERS_URL, "sec.ticker_cik_index", identity)
            return _get(http, "company_tickers", TICKERS_URL, "sec.ticker_cik_index", identity)
        return request() if via is None else via.call("sec", "company_tickers", request, identity=identity)
    rows = json.loads(gateway.fetch_once(company_tickers_key(), fetch).decode()); return {str(row["ticker"]).upper(): int(row["cik_str"]) for row in rows.values()}
def title_ticker_index(http: httpx.Client | None = None, *, via: gateway.SourceGateway | None = None) -> dict[str, str]:
    def fetch(identity: FetchKey) -> bytes:
        def request() -> bytes:
            if http is None:
                with client() as c: return _get(c, "company_tickers", TICKERS_URL, "sec.title_ticker_index", identity)
            return _get(http, "company_tickers", TICKERS_URL, "sec.title_ticker_index", identity)
        return request() if via is None else via.call("sec", "company_tickers", request, identity=identity)
    rows = json.loads(gateway.fetch_once(company_tickers_key(), fetch).decode()); index: dict[str, str] = {}
    for row in rows.values():
        ticker = str(row["ticker"]).upper(); norm = normalize_company_title(str(row.get("title") or ""))
        if not norm: continue
        existing = index.get(norm)
        if existing is None or ("-" in existing and "-" not in ticker): index[norm] = ticker
    return index
def ticker_mic_index(http: httpx.Client | None = None, *, via: gateway.SourceGateway | None = None) -> dict[str, str]:
    def fetch(identity: FetchKey) -> bytes:
        def request() -> bytes:
            if http is None:
                with client() as c: return _get(c, "company_tickers_exchange", TICKERS_EXCHANGE_URL, "sec.ticker_mic_index", identity)
            return _get(http, "company_tickers_exchange", TICKERS_EXCHANGE_URL, "sec.ticker_mic_index", identity)
        return request() if via is None else via.call("sec", "company_tickers_exchange", request, identity=identity)
    payload = json.loads(gateway.fetch_once(company_tickers_exchange_key(), fetch).decode()); index: dict[str, str] = {}
    for row in payload.get("data") or []:
        if not isinstance(row, list) or len(row) < 4: continue
        ticker_upper = str(row[2] or "").strip().upper(); mic = EXCHANGE_MIC_MAP.get(str(row[3] or "").strip().upper())
        if not ticker_upper or not mic: continue
        index[ticker_upper] = mic; index[ticker_upper.replace("-", ".")] = mic
    return index
def _get(http: httpx.Client, endpoint: str, url: str, caller: str, identity: FetchKey) -> bytes: resp = gateway.http_get(http, "sec", endpoint, url, identity=identity, caller=caller); resp.raise_for_status(); return bytes(resp.content)
def ticker_to_cik(ticker: str) -> int:
    cik = ticker_cik_index().get(ticker.upper())
    if cik is None: raise KeyError(f"ticker not found in SEC mapping: {ticker}")
    return cik
@accept_rule("sec", "submissions")
def _is_json_object(_key: FetchKey, body: bytes) -> bool:
    try: return isinstance(json.loads(body.decode()), dict)
    except ValueError: return False
def fetch_company_submissions(cik: int, http: httpx.Client | None = None) -> dict[str, Any]:
    def fetch(identity: FetchKey) -> bytes:
        url = SUBMISSIONS_URL.format(cik=cik)
        if http is None:
            with client() as c: return _get(c, "submissions", url, "sec.fetch_company_submissions", identity)
        return _get(http, "submissions", url, "sec.fetch_company_submissions", identity)
    body = gateway.fetch_once(submissions_key(cik), fetch); payload = json.loads(body.decode())
    if not isinstance(payload, dict): raise ValueError(f"the submissions of CIK {cik} are not a JSON object")
    return payload
def fetch_company_facts_response(cik: int, http: httpx.Client | None = None) -> tuple[bytes, dict[str, Any]]:
    def fetch(identity: FetchKey) -> bytes:
        url = COMPANY_FACTS_URL.format(cik=cik)
        if http is None:
            with client() as c: return _get(c, "companyfacts", url, "sec.fetch_company_facts_response", identity)
        return _get(http, "companyfacts", url, "sec.fetch_company_facts_response", identity)
    body = gateway.fetch_once(company_facts_key(cik), fetch); return body, json.loads(body.decode())
def fetch_company_facts(cik: int, http: httpx.Client | None = None) -> dict[str, Any]: return fetch_company_facts_response(cik, http)[1]
def save_sample(ticker: str, out_dir: Path) -> Path: cik = ticker_to_cik(ticker); facts = fetch_company_facts(cik); out_dir.mkdir(parents=True, exist_ok=True); path = out_dir / f"{ticker.upper()}_CIK{cik:010d}.json"; path.write_text(json.dumps(facts, indent=1)); return path
