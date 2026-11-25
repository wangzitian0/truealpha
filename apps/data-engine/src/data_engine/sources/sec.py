"""SEC EDGAR company-facts, submissions, and ticker crosswalk client."""

import json
import re
import time
from collections.abc import Callable
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


def client(user_agent: str | None = None, *, timeout: float = 30.0) -> httpx.Client:
    ua = settings.sec_user_agent if user_agent is None else user_agent
    if not ua:
        raise RuntimeError("Set SEC_USER_AGENT (must include a contact email) — see .env.example")
    return httpx.Client(headers={"User-Agent": ua}, timeout=timeout, follow_redirects=True)


def _get(
    http: httpx.Client,
    endpoint: str,
    url: str,
    caller: str,
    identity: FetchKey,
    *,
    max_attempts: int = 1,
    retry_statuses: tuple[int, ...] = (429, 502, 503, 504),
    backoff_base: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    resp = gateway.http_get(
        http,
        "sec",
        endpoint,
        url,
        identity=identity,
        caller=caller,
        max_attempts=max_attempts,
        retry_statuses=retry_statuses,
        backoff_base=backoff_base,
        sleep=sleep,
    )
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
    max_attempts: int = 1,
    retry_statuses: tuple[int, ...] = (429, 502, 503, 504),
    backoff_base: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    def fetch(identity: FetchKey) -> bytes:
        def request() -> bytes:
            if http is None:
                with client() as c:
                    return _get(
                        c,
                        endpoint,
                        url,
                        caller,
                        identity,
                        max_attempts=max_attempts,
                        retry_statuses=retry_statuses,
                        backoff_base=backoff_base,
                        sleep=sleep,
                    )
            return _get(
                http,
                endpoint,
                url,
                caller,
                identity,
                max_attempts=max_attempts,
                retry_statuses=retry_statuses,
                backoff_base=backoff_base,
                sleep=sleep,
            )

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
    max_attempts: int = 1,
    retry_statuses: tuple[int, ...] = (429, 502, 503, 504),
    backoff_base: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    return json.loads(
        _fetch_sec(
            key,
            endpoint,
            url,
            caller,
            http,
            via=via,
            max_attempts=max_attempts,
            retry_statuses=retry_statuses,
            backoff_base=backoff_base,
            sleep=sleep,
        ).decode()
    )


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


@accept_rule("sec", "submissions")
def _is_json_object(_key: FetchKey, body: bytes) -> bool:
    try:
        return isinstance(json.loads(body.decode()), dict)
    except ValueError:
        return False


def fetch_company_submissions(
    cik: int,
    http: httpx.Client | None = None,
    *,
    max_attempts: int = 1,
    retry_statuses: tuple[int, ...] = (429, 502, 503, 504),
    backoff_base: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    payload = _fetch_sec_json(
        submissions_key(cik),
        "submissions",
        SUBMISSIONS_URL.format(cik=cik),
        "sec.fetch_company_submissions",
        http,
        max_attempts=max_attempts,
        retry_statuses=retry_statuses,
        backoff_base=backoff_base,
        sleep=sleep,
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
