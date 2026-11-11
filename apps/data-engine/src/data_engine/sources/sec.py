"""SEC EDGAR company-facts, submissions, and ticker crosswalk client."""

import json
import re
from pathlib import Path
from typing import Any

import httpx

from data_engine.config import settings
from data_engine.datahub.fetch_key import FetchKey
from data_engine.sources import gateway
from data_engine.sources.accept_rules import accept_rule

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
EXCHANGE_MIC_MAP: dict[str, str] = {"NYSE": "XNYS", "NASDAQ": "XNAS", "CBOE": "BATS"}
_CORP_SUFFIXES = {
    "INC",
    "CORP",
    "CORPORATION",
    "CO",
    "COMPANY",
    "HOLDINGS",
    "HOLDING",
    "LTD",
    "PLC",
    "NEW",
    "THE",
    "DE",
    "MD",
}


def normalize_company_title(title: str) -> str:
    cleaned = re.sub(r"/[A-Z]+/", " ", title.upper())
    return "".join(tok for tok in re.split(r"[^A-Z0-9]+", cleaned) if tok and tok not in _CORP_SUFFIXES)


def company_facts_key(cik: int) -> FetchKey:
    return FetchKey("sec", "companyfacts", f"issuer:cik:{cik:010d}", gateway.utc_day())


def submissions_key(cik: int, *, overflow: str | None = None) -> FetchKey:
    entity = f"issuer:cik:{cik:010d}" if overflow is None else f"issuer:cik:{cik:010d}:{overflow}"
    return FetchKey("sec", "submissions", entity, gateway.utc_day())


def company_tickers_key() -> FetchKey:
    return FetchKey("sec", "company_tickers", "sec:company-tickers", gateway.utc_day())


def company_tickers_exchange_key() -> FetchKey:
    return FetchKey("sec", "company_tickers_exchange", "sec:company-tickers-exchange", gateway.utc_day())


def client() -> httpx.Client:
    if not settings.sec_user_agent:
        raise RuntimeError("Set SEC_USER_AGENT (must include a contact email) — see .env.example")
    return httpx.Client(headers={"User-Agent": settings.sec_user_agent}, timeout=30.0, follow_redirects=True)


def _get(http: httpx.Client, endpoint: str, url: str, caller: str, identity: FetchKey) -> bytes:
    resp = gateway.http_get(http, "sec", endpoint, url, identity=identity, caller=caller)
    resp.raise_for_status()
    return bytes(resp.content)


def _fetch_sec(
    key: FetchKey,
    endpoint: str,
    url: str,
    caller: str,
    http: httpx.Client | None = None,
    *,
    via: gateway.SourceGateway | None = None,
) -> bytes:
    def fetch(identity: FetchKey) -> bytes:
        def request() -> bytes:
            if http is None:
                with client() as c:
                    return _get(c, endpoint, url, caller, identity)
            return _get(http, endpoint, url, caller, identity)

        return request() if via is None else via.call("sec", endpoint, request, identity=identity)

    return gateway.fetch_once(key, fetch)


def _fetch_sec_json(
    key: FetchKey,
    endpoint: str,
    url: str,
    caller: str,
    http: httpx.Client | None = None,
    *,
    via: gateway.SourceGateway | None = None,
) -> Any:
    return json.loads(_fetch_sec(key, endpoint, url, caller, http, via=via).decode())


def ticker_cik_index(http: httpx.Client | None = None, *, via: gateway.SourceGateway | None = None) -> dict[str, int]:
    rows = _fetch_sec_json(company_tickers_key(), "company_tickers", TICKERS_URL, "sec.ticker_cik_index", http, via=via)
    return {str(row["ticker"]).upper(): int(row["cik_str"]) for row in rows.values()}


def title_ticker_index(http: httpx.Client | None = None, *, via: gateway.SourceGateway | None = None) -> dict[str, str]:
    rows = _fetch_sec_json(
        company_tickers_key(), "company_tickers", TICKERS_URL, "sec.title_ticker_index", http, via=via
    )
    index: dict[str, str] = {}
    for row in rows.values():
        ticker = str(row["ticker"]).upper()
        norm = normalize_company_title(str(row.get("title") or ""))
        if not norm:
            continue
        existing = index.get(norm)
        if existing is None or ("-" in existing and "-" not in ticker):
            index[norm] = ticker
    return index


def ticker_mic_index(http: httpx.Client | None = None, *, via: gateway.SourceGateway | None = None) -> dict[str, str]:
    payload = _fetch_sec_json(
        company_tickers_exchange_key(),
        "company_tickers_exchange",
        TICKERS_EXCHANGE_URL,
        "sec.ticker_mic_index",
        http,
        via=via,
    )
    index: dict[str, str] = {}
    for row in payload.get("data") or []:
        if not isinstance(row, list) or len(row) < 4:
            continue
        ticker_upper = str(row[2] or "").strip().upper()
        mic = EXCHANGE_MIC_MAP.get(str(row[3] or "").strip().upper())
        if ticker_upper and mic:
            index[ticker_upper] = mic
            index[ticker_upper.replace("-", ".")] = mic
    return index


def ticker_to_cik(ticker: str) -> int:
    cik = ticker_cik_index().get(ticker.upper())
    if cik is None:
        raise KeyError(f"ticker not found in SEC mapping: {ticker}")
    return cik


@accept_rule("sec", "submissions")
def _is_json_object(_key: FetchKey, body: bytes) -> bool:
    try:
        return isinstance(json.loads(body.decode()), dict)
    except ValueError:
        return False


def fetch_company_submissions(cik: int, http: httpx.Client | None = None) -> dict[str, Any]:
    payload = _fetch_sec_json(
        submissions_key(cik), "submissions", SUBMISSIONS_URL.format(cik=cik), "sec.fetch_company_submissions", http
    )
    if not isinstance(payload, dict):
        raise ValueError(f"the submissions of CIK {cik} are not a JSON object")
    return payload


def fetch_company_facts_response(cik: int, http: httpx.Client | None = None) -> tuple[bytes, dict[str, Any]]:
    body = _fetch_sec(
        company_facts_key(cik),
        "companyfacts",
        COMPANY_FACTS_URL.format(cik=cik),
        "sec.fetch_company_facts_response",
        http,
    )
    return body, json.loads(body.decode())


def fetch_company_facts(cik: int, http: httpx.Client | None = None) -> dict[str, Any]:
    return fetch_company_facts_response(cik, http)[1]


def save_sample(ticker: str, out_dir: Path) -> Path:
    cik = ticker_to_cik(ticker)
    facts = fetch_company_facts(cik)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{ticker.upper()}_CIK{cik:010d}.json"
    path.write_text(json.dumps(facts, indent=1))
    return path
