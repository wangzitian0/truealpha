"""SEC EDGAR company-facts client (Phase -1 sampling; becomes a dlt source in Phase 0).

Known pitfalls (init.md Section 9): XBRL tags are inconsistent across industries —
never assume field names/units are uniform. That inconsistency is exactly what the
Phase -1 samples are meant to surface.
"""

import json
from pathlib import Path
from typing import Any

import httpx

from data_engine.config import settings
from data_engine.datahub.fetch_key import FetchKey
from data_engine.sources import gateway
from data_engine.sources.accept_rules import accept_rule

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"


def company_facts_key(cik: int) -> FetchKey:
    """The one key of a company-facts document (#1411 K5). The adapter and the vendor oracle share it."""
    return FetchKey("sec", "companyfacts", f"issuer:cik:{cik:010d}", gateway.utc_day())


def submissions_key(cik: int, *, overflow: str | None = None) -> FetchKey:
    """The one key of an issuer's submissions index, or of one overflow file of it.

    The filing readers and `fetch_company_submissions` share it."""
    entity = f"issuer:cik:{cik:010d}" if overflow is None else f"issuer:cik:{cik:010d}:{overflow}"
    return FetchKey("sec", "submissions", entity, gateway.utc_day())


def company_tickers_key() -> FetchKey:
    """The one key of SEC's ticker-to-CIK crosswalk file."""
    return FetchKey("sec", "company_tickers", "sec:company-tickers", gateway.utc_day())


def client() -> httpx.Client:
    """The shared SEC-facing HTTP client (correct User-Agent per SEC fair-use
    policy) — also reused for other keyless public APIs (OpenFIGI). Public API:
    nport/bootstrap/sweep modules all construct their client here."""
    if not settings.sec_user_agent:
        raise RuntimeError("Set SEC_USER_AGENT (must include a contact email) — see .env.example")
    return httpx.Client(headers={"User-Agent": settings.sec_user_agent}, timeout=30.0, follow_redirects=True)


def ticker_cik_index(http: httpx.Client | None = None, *, via: gateway.SourceGateway | None = None) -> dict[str, int]:
    """The whole ticker -> CIK crosswalk from SEC's official mapping file, once.

    Resolving a universe one ticker at a time re-downloads this file per lookup; a
    capture run reads it once and indexes.
    """

    def fetch(identity: FetchKey) -> bytes:
        def request() -> bytes:
            if http is None:
                with client() as c:
                    return _get(c, "company_tickers", TICKERS_URL, "sec.ticker_cik_index", identity)
            return _get(http, "company_tickers", TICKERS_URL, "sec.ticker_cik_index", identity)

        # A standards-lane gateway admits and records the request itself. It runs only on a miss.
        return request() if via is None else via.call("sec", "company_tickers", request, identity=identity)

    rows = json.loads(gateway.fetch_once(company_tickers_key(), fetch).decode())
    return {str(row["ticker"]).upper(): int(row["cik_str"]) for row in rows.values()}


def _get(http: httpx.Client, endpoint: str, url: str, caller: str, identity: FetchKey) -> bytes:
    """One SEC request through the gateway and ledger; the verbatim body of a successful answer."""
    resp = gateway.http_get(http, "sec", endpoint, url, identity=identity, caller=caller)
    resp.raise_for_status()
    return bytes(resp.content)


def ticker_to_cik(ticker: str) -> int:
    """Resolve ticker -> CIK via SEC's official mapping file.

    NOTE: this per-source lookup is temporary. From Phase 0 on, the crosswalk lives
    in the KG as same_as edges (factors.shared.entity_resolution).
    """
    cik = ticker_cik_index().get(ticker.upper())
    if cik is None:
        raise KeyError(f"ticker not found in SEC mapping: {ticker}")
    return cik


@accept_rule("sec", "submissions")
def _is_json_object(_key: FetchKey, body: bytes) -> bool:
    """Whether ``body`` is a JSON object. Text that is not JSON is not one.

    A 200 answer that is not a JSON object is not the submissions of the issuer (#1323). The rule is the one
    of every reader of that key, the filing readers included (#1502)."""
    try:
        return isinstance(json.loads(body.decode()), dict)
    except ValueError:
        return False


def fetch_company_submissions(cik: int, http: httpx.Client | None = None) -> dict[str, Any]:
    """The issuer's EDGAR submissions metadata (name, SIC classification, filing index).

    This is issuer *registry* metadata, not reported financial data: it is what says
    an issuer is a depository institution rather than a manufacturer.
    """

    def fetch(identity: FetchKey) -> bytes:
        url = SUBMISSIONS_URL.format(cik=cik)
        if http is None:
            with client() as c:
                return _get(c, "submissions", url, "sec.fetch_company_submissions", identity)
        return _get(http, "submissions", url, "sec.fetch_company_submissions", identity)

    # The registered rule of the key keeps an answer that is no JSON object out of the index, so the retry of
    # the registry and the next caller ask SEC again (#1323).
    body = gateway.fetch_once(submissions_key(cik), fetch)
    payload = json.loads(body.decode())
    if not isinstance(payload, dict):
        raise ValueError(f"the submissions of CIK {cik} are not a JSON object")
    return payload


def fetch_company_facts_response(cik: int, http: httpx.Client | None = None) -> tuple[bytes, dict[str, Any]]:
    """The verbatim response body and the facts parsed from it.

    Callers that land evidence need the bytes SEC actually sent. Re-serializing the
    parsed dict is not the same thing — key order and separators differ, so its digest
    describes our rendering rather than the vendor's document, and a reparse under a
    corrected mapping would be replaying our own output.
    """

    def fetch(identity: FetchKey) -> bytes:
        url = COMPANY_FACTS_URL.format(cik=cik)
        if http is None:
            with client() as c:
                return _get(c, "companyfacts", url, "sec.fetch_company_facts_response", identity)
        return _get(http, "companyfacts", url, "sec.fetch_company_facts_response", identity)

    body = gateway.fetch_once(company_facts_key(cik), fetch)
    return body, json.loads(body.decode())


def fetch_company_facts(cik: int, http: httpx.Client | None = None) -> dict[str, Any]:
    """Parsed facts only, for callers with nothing to land.

    Pass a client to reuse one connection across a sweep; without one, a throwaway
    client is opened per call (fine for single fetches).
    """
    return fetch_company_facts_response(cik, http)[1]


def save_sample(ticker: str, out_dir: Path) -> Path:
    """Pull company facts for one ticker and save the raw JSON verbatim."""
    cik = ticker_to_cik(ticker)
    facts = fetch_company_facts(cik)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{ticker.upper()}_CIK{cik:010d}.json"
    path.write_text(json.dumps(facts, indent=1))
    return path
