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

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"


def _client() -> httpx.Client:
    if not settings.sec_user_agent:
        raise RuntimeError("Set SEC_USER_AGENT (must include a contact email) — see .env.example")
    return httpx.Client(headers={"User-Agent": settings.sec_user_agent}, timeout=30.0, follow_redirects=True)


def ticker_to_cik(ticker: str) -> int:
    """Resolve ticker -> CIK via SEC's official mapping file.

    NOTE: this per-source lookup is temporary. From Phase 0 on, the crosswalk lives
    in the KG as same_as edges (factors.shared.entity_resolution).
    """
    with _client() as client:
        resp = client.get(TICKERS_URL)
        resp.raise_for_status()
        for row in resp.json().values():
            if row["ticker"].upper() == ticker.upper():
                return int(row["cik_str"])
    raise KeyError(f"ticker not found in SEC mapping: {ticker}")


def fetch_company_facts(cik: int, client: httpx.Client | None = None) -> dict[str, Any]:
    """Pass a client to reuse one connection across a sweep; without one, a
    throwaway client is opened per call (fine for single fetches)."""
    if client is not None:
        resp = client.get(COMPANY_FACTS_URL.format(cik=cik))
        resp.raise_for_status()
        return resp.json()
    with _client() as client:
        return fetch_company_facts(cik, client)


def save_sample(ticker: str, out_dir: Path) -> Path:
    """Pull company facts for one ticker and save the raw JSON verbatim."""
    cik = ticker_to_cik(ticker)
    facts = fetch_company_facts(cik)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{ticker.upper()}_CIK{cik:010d}.json"
    path.write_text(json.dumps(facts, indent=1))
    return path
