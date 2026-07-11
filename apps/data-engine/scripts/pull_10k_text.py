"""Phase -1 sampling: latest 10-K (or 20-F) primary document as raw text.

This is the source material for two LLM-extraction paths:
- headcount (company-facts has no EntityNumberOfEmployees for any test name)
- supply-chain edges (supplier/customer mentions, e.g. DDOG's cloud providers)

The script saves the filing and prints the sentences around "employees" and a few
known supplier keywords, to confirm the raw material actually contains what the
extraction factors will need.

Usage:
    uv run --package truealpha-data-engine python apps/data-engine/scripts/pull_10k_text.py [TICKER ...]
"""

import re
import sys
from pathlib import Path

from data_engine.sources.sec import client as sec_client
from data_engine.sources.sec import ticker_to_cik

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc}"

OUT_DIR = Path(__file__).resolve().parents[1] / "data" / "samples"
ANNUAL_FORMS = {"10-K", "20-F"}  # NICE files 20-F (foreign private issuer)
SUPPLIER_KEYWORDS = ["Amazon Web Services", "Google Cloud", "Microsoft Azure", "third-party"]


def latest_annual(client, cik: int) -> tuple[str, str, str]:
    resp = client.get(SUBMISSIONS_URL.format(cik=cik))
    resp.raise_for_status()
    recent = resp.json()["filings"]["recent"]
    for form, accession, doc in zip(recent["form"], recent["accessionNumber"], recent["primaryDocument"]):
        if form in ANNUAL_FORMS:
            return form, accession.replace("-", ""), doc
    raise LookupError(f"no annual report found for CIK {cik}")


def html_to_text(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#8217;", "'")
    return re.sub(r"\s+", " ", text)


def show_matches(text: str, pattern: str, n: int = 2, width: int = 160) -> None:
    for i, m in enumerate(re.finditer(pattern, text, re.IGNORECASE)):
        if i >= n:
            break
        start = max(0, m.start() - width // 2)
        print(f"    …{text[start : start + width].strip()}…")


def main() -> None:
    tickers = sys.argv[1:] or ["DDOG"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for ticker in tickers:
        with sec_client() as client:
            cik = ticker_to_cik(ticker)
            form, accession, doc = latest_annual(client, cik)
            resp = client.get(ARCHIVE_URL.format(cik=cik, accession=accession, doc=doc))
            resp.raise_for_status()
        path = OUT_DIR / f"{ticker.upper()}_{form.replace('-', '')}_{accession}.html"
        path.write_bytes(resp.content)
        text = html_to_text(resp.text)
        print(f"\n{ticker}: {form} ({len(resp.content) / 1e6:.1f} MB html), saved {path.name}")
        print("  'employees' mentions:")
        show_matches(text, r"[\d,]+\s+(?:full-time\s+)?employees|employees[^.]{0,80}")
        for kw in SUPPLIER_KEYWORDS:
            count = len(re.findall(re.escape(kw), text, re.IGNORECASE))
            print(f"  supplier keyword '{kw}': {count} mentions")


if __name__ == "__main__":
    main()
