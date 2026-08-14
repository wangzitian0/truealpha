"""Enumerate the headcount candidate spans in each issuer's latest annual filing (#70).

Headcount is the one GPPE input SEC company-facts cannot supply: across the TOPT 20 there
are 33 concepts whose name contains "Employee" and every one of them is share-based
compensation or an employee-related liability. The figure exists only as prose in Item 1,
so it needs extraction — which is why `staging.issuer_headcount_facts` is fed by a
reviewed seed today and why that seed is stale (measured 2026-08-14: 7 of 14 checkable
issuers off by more than 10%, AVGO by 39% and NVDA by 30%, all understating and therefore
inflating the factor).

This script is the RECALL half of that extraction, and deliberately only that half. It
finds every sentence in the filing that states a headcount and prints all of them with the
surrounding context, the "as of" date, and the accession. It does not choose between them,
because choosing is a semantic judgement a pattern cannot make:

    LLY   12,000  "employed approximately 12,000 people in pharmaceutical research and development"
          50,000  "we employed approximately 50,000 people, including approximately 27,000 ..."
    BRK.B 387,800 "Berkshire and its operating subsidiaries employed approximately 387,800"
          + seven per-segment counts (insurance 42,600, BNSF 35,000, manufacturing 175,600, ...)

In every multi-candidate case measured so far the correct answer was among the candidates,
so recall is the tractable half and precision is what the `libs/factors/shared` extraction
primitive is for (init.md Section 9: append-only, binds model/instructions/schema, replay
reuses the stored result). That primitive currently raises NotImplementedError and the
project has no model credential provisioned, so this output is its input, not its
replacement.

Nothing here writes to the database. A number printed by this script is not a fact until it
lands through the extraction plane with its accession and evidence span, and hand-copying
one into the seed would only produce a fresher uncited row.

Usage:
    uv run --package truealpha-data-engine python apps/data-engine/scripts/probe_headcount_candidates.py [TICKER ...]

Defaults to the TOPT 20. Requires SEC_USER_AGENT.
"""

from __future__ import annotations

import html
import re
import sys

from data_engine.sources import sec

DEFAULT_TICKERS = ("AAPL ABBV AMZN AVGO BRK-B COST GOOGL JNJ JPM LLY MA META MSFT MU NFLX NVDA TSLA V WMT XOM").split()

ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
ANNUAL_FORMS = ("10-K", "20-F")

# "we had approximately 33,000 employees", "employed 341,000 employees worldwide",
# "approximately 164,000 full-time employees". Deliberately loose: a missed candidate is
# unrecoverable downstream, whereas a spurious one is discarded by the selection step.
_HEADCOUNT = re.compile(
    r"(?:had|employed|have|of)\s+"
    r"(?:approximately\s+|about\s+|over\s+|more than\s+|nearly\s+)?"
    r"([\d][\d,]{2,12})\s+"
    r"(?:full[- ]time\s+|part[- ]time\s+|regular\s+|global\s+|salaried\s+)?"
    r"(?:employees|persons|people|associates|team members|colleagues)",
    re.I,
)
_AS_OF = re.compile(r"[Aa]s of ([A-Z][a-z]+ \d{1,2},? \d{4})")
# Inline XBRL leaks concept names into the text ("us-gaap:EmployeeSeveranceMember"); those
# are tag soup, not prose, and matching them is how a naive pass finds "employees" in a
# filing that never states a headcount.
_TAG_SOUP = re.compile(r"us-gaap:|Member\b|xbrli:")


def filing_text(cik: int, http) -> tuple[str, str, str] | None:
    """The latest annual filing's primary document as plain text, with its identity."""
    recent = sec.fetch_company_submissions(cik, http)["filings"]["recent"]
    index = next((i for i, form in enumerate(recent["form"]) if form in ANNUAL_FORMS), None)
    if index is None:
        return None
    accession = recent["accessionNumber"][index].replace("-", "")
    url = ARCHIVE_URL.format(cik=cik, accession=accession, document=recent["primaryDocument"][index])
    body = http.get(url).content.decode("utf-8", "ignore")
    # The ix:header block is a machine-readable duplicate of the whole filing; leaving it in
    # both doubles the text and pollutes it with concept names.
    body = re.sub(r"(?is)<ix:header.*?</ix:header>", " ", body)
    body = re.sub(r"(?is)<(script|style).*?</\1>", " ", body)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", body))
    return re.sub(r"\s+", " ", text), accession, recent["filingDate"][index]


def candidates(text: str) -> list[tuple[str, str, str]]:
    """Every stated headcount: (value, as-of date or '-', the sentence it came from)."""
    found: list[tuple[str, str, str]] = []
    for match in _HEADCOUNT.finditer(text):
        context = text[max(0, match.start() - 220) : match.end() + 80]
        if _TAG_SOUP.search(context):
            continue
        as_of = _AS_OF.search(context)
        found.append((match.group(1), as_of.group(1) if as_of else "-", context.strip()))
    return list(dict.fromkeys(found))


def main() -> None:
    tickers = sys.argv[1:] or DEFAULT_TICKERS
    with sec.client() as http:
        for ticker in tickers:
            try:
                cik = sec.ticker_to_cik(ticker)
                resolved = filing_text(cik, http)
            except Exception as error:  # noqa: BLE001 - a probe reports failures, never hides them
                print(f"{ticker:6s} ERROR {error}")
                continue
            if resolved is None:
                print(f"{ticker:6s} no {'/'.join(ANNUAL_FORMS)} on file")
                continue
            text, accession, filed = resolved
            found = candidates(text)
            print(f"\n{ticker:6s} accession={accession} filed={filed} candidates={len(found)}")
            if not found:
                print("       (none — this issuer phrases it differently and needs the extraction path)")
            for value, as_of, sentence in found:
                print(f"       {value:>10s}  as of {as_of:20s}  …{sentence[-150:]}")


if __name__ == "__main__":
    main()
