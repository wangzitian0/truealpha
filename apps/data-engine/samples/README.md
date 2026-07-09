# Raw sample fixtures

Real API/filing responses, byte-for-byte as returned by the source (no local
redaction — see "A note on masked-looking fields" below), captured
**2026-07-09** from the VPS (the same host truealpha deploys to, identified as
`$VPS_HOST` in infra2 — not published here; not from a local laptop, so these
reflect the network/IP conditions the real ingestion pipeline will see).

Purpose: ground schema design and data-quality profiling for `db/migrations`
and `libs/factors` in real payloads before building the `raw.fetches` + MinIO
ingestion pipeline (Phase -1 — see `init.md` Section 11). Not refreshed by CI;
re-run the scripts in `../scripts/` manually to recapture a new snapshot.

Universe: DDOG, NICE, SHOP, DUOL (init.md Section 11's test names) + QQQ, ARKK
(ETF holdings-weight source check).

## Contents

- `sec/` — SEC EDGAR company-facts JSON (`GET /api/xbrl/companyfacts/CIK##########.json`)
- `filings/` — latest 10-K (or 20-F for NICE, a foreign private issuer) primary document, raw HTML
- `nport/` — latest NPORT-P primary_doc.xml (ETF holdings + weights)
- `prices/` — 1y daily OHLCV bars (`data_engine.sources.yahoo`, Yahoo Finance chart endpoint)

**The `yfinance` PyPI package gets HTTP 429 from this VPS's IP on every
endpoint** (a real Phase -1 finding — Yahoo appears to fingerprint at the
session/TLS level, not just check the User-Agent string). Worked around by
dropping the package and hitting Yahoo's chart endpoint directly with a plain
`httpx` client and a non-default `User-Agent`
(`data_engine/sources/yahoo.py`) — the same approach finance_report runs in
production from this same VPS
(`apps/backend/src/pricing/extension/market_data/_providers.py` in that repo).
Verified working end-to-end for all 4 tickers.

## Profiling findings from this snapshot (see `git log` for the exact pull)

- **Revenue tag inconsistency confirmed across all 4 issuers** — no single
  us-gaap tag reliably identifies "revenue": DDOG/DUOL lack `Revenues` (use
  `RevenueFromContractWithCustomerExcludingAssessedTax` instead), NICE/SHOP
  lack `RevenueFromContractWithCustomerExcludingAssessedTax` (NICE has plain
  `Revenues`, SHOP has both). Confirms init.md Section 9's tag-inconsistency
  warning — factor code must branch/fallback across candidate tags, not
  assume one tag name per metric.
- **`EntityNumberOfEmployees` XBRL tag is absent for all 4 issuers** — headcount
  is not machine-readable via XBRL for this universe; module 2's
  gross-profit-per-employee factor must go through the LLM-extraction path
  (`libs/factors/shared/extraction.py`) reading `filings/*.html`, not staging
  data pulled straight from company-facts.
- **Headcount is genuinely ambiguous even in prose**: DDOG's 10-K states 3,600
  (sales & marketing), 3,900 (R&D), and 8,100 (total) employees in three
  separate sentences — an extraction prompt naively grepping "employees" needs
  to disambiguate "total" from segment breakdowns, not just take the first
  match. NICE states a single unambiguous "9,626 employees worldwide" figure —
  extraction confidence should reasonably differ between these two cases.
- **Supply-chain keyword density varies a lot by issuer** — DDOG's 10-K
  explicitly names AWS/GCP/Azure (1/1/2 mentions); SHOP and DUOL each name only
  1-2 of the three; NICE names only AWS once. A `supplies_to` KG edge
  extraction pass will have very different confidence/evidence strength per
  company from this alone — expect coverage gaps, not a uniform hit rate.
- **N-PORT foreign holdings carry CUSIP `000000000`** (ARKK: CRISPR
  Therapeutics, Wix.com, several Israeli names) — `same_as` entity resolution
  for ETF holdings needs an ISIN/name fallback, CUSIP alone is insufficient
  (already noted in `init.md` Section 5, reconfirmed here).
- **NICE is a 20-F filer, not 10-K** (foreign private issuer) — any filing-type
  assumption in the ingestion pipeline must handle both forms, not hardcode `10-K`.

## A note on masked-looking fields

`nport/*.xml` contains `<ccc>XXXXXXXX</ccc>` (identical literal placeholder in
both files) and `nport/ARKK_*.xml` contains `<documents>XXXX</documents>`
(absent entirely from the QQQ file). Neither is something this repo redacted —
`probe_etf_holdings.py` writes `response.content` straight to disk with no
string substitution. Both are SEC EDGAR's own public-facing masking: `ccc`
(CIK Confirmation Code) is a private EDGAR filing-agent credential the SEC
never exposes even in public downloads, and `documents` is a placeholder for
attachments served separately from the filing index, not inline in
`primary_doc.xml`. The differing presence of `<documents>` between the two
otherwise-identically-fetched files is itself evidence this is SEC's serving
behavior, not a local transformation.
