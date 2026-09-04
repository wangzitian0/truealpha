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

import sys

from data_engine.datahub.standards.filing_extraction import ANNUAL_FORMS, candidates, filing_plain_text
from data_engine.sources import sec

DEFAULT_TICKERS = ("AAPL ABBV AMZN AVGO BRK-B COST GOOGL JNJ JPM LLY MA META MSFT MU NFLX NVDA TSLA V WMT XOM").split()

ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"


def filing_text(cik: int, http) -> tuple[str, str, str] | None:
    """The latest annual filing's primary document as plain text, with its identity.

    The deployed loop (`data_engine.datahub.standards`) does the same through the source
    gateway and lands the bytes in object storage; this probe stays a read-only,
    ledger-free reconnaissance tool and shares the parsing code rather than a copy of it.
    """
    recent = sec.fetch_company_submissions(cik, http)["filings"]["recent"]
    index = next((i for i, form in enumerate(recent["form"]) if form in ANNUAL_FORMS), None)
    if index is None:
        return None
    accession = recent["accessionNumber"][index].replace("-", "")
    url = ARCHIVE_URL.format(cik=cik, accession=accession, document=recent["primaryDocument"][index])
    return filing_plain_text(http.get(url).content), accession, recent["filingDate"][index]


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
            for candidate in found:
                mark = "partial" if candidate.partial else "total  "
                print(
                    f"       {candidate.value:>10,d}  {mark}  as of {candidate.as_of or '-':20s}  …{candidate.sentence[-150:]}"
                )


if __name__ == "__main__":
    main()
