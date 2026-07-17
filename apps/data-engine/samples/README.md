# Raw sample fixtures

Real API/filing responses, byte-for-byte as returned by the source (no local
redaction — see "A note on masked-looking fields" below), captured
**2026-07-09** from the VPS (the same host truealpha deploys to, identified as
`$VPS_HOST` in infra2 — not published here; not from a local laptop, so these
reflect the network/IP conditions the real ingestion pipeline will see).

Purpose: ground schema design and data-quality profiling for `db/migrations`
and `libs/factors` in real payloads before building the `raw.fetches` + MinIO
ingestion pipeline (Phase -1 reconnaissance; see `init.md` "Current Baseline and Next Gate"). Not refreshed by CI;
re-run the scripts in `../scripts/` manually to recapture a new snapshot.

Initial universe: DDOG, NICE, SHOP, DUOL (the reconnaissance test universe) +
QQQ and ARKK (ETF holdings-weight source check).

Issue #14 adds a bounded evidence universe rather than a broad second sweep:

- JPM — financial-company semantics and a cash-dividend replay case;
- ADM — a traditional agricultural processor;
- NVDA — filed company guidance and a 2024 10:1 split;
- META — the FB to META symbol transition; and
- PLUG — an original/amended filing pair with changed company-facts values.

Run `make sample-evidence` to resume the public SEC/Yahoo/issuer capture. The
command never overwrites differing bytes. Yahoo revised overlapping historical
values during the 2026-07-12 capture itself; both the old one-year and new
three-year files therefore remain immutable point-in-time vintages.

Run `make sample-audit` to validate this corpus against the machine-readable
strategy requirements. Sampling and the executable composite replay now satisfy
the local-backtest boundary. Five-year coverage and primary/fallback price
reconciliation remain strategy-evaluation requirements. See
`docs/strategy-data-quality.md` for the gate definitions.

The first continuous-confidence sensitivity report is documented in
`docs/confidence-calibration.md`. Its four-symbol Yahoo/Twelve Data comparison is
an aggregate empirical anchor only: Yahoo CSV bytes are retained and hash
verified, but Twelve Data raw response bytes are absent. The latter remains in
lineage but contributes zero independent support. The anchor does not shrink or
calibrate the 20-issuer TOPT denominator and does not authorize a Production
confidence threshold.

## Contents

- `sec/` — SEC EDGAR company-facts JSON (`GET /api/xbrl/companyfacts/CIK##########.json`)
- `filings/` — latest 10-K (or 20-F for NICE, a foreign private issuer) primary document, raw HTML
- `nport/` — latest NPORT-P primary_doc.xml (ETF holdings + weights)
- `prices/` — immutable 1y/3y/5y daily OHLCV captures (`data_engine.sources.yahoo`, Yahoo chart endpoint)
- `events/` — normalized issuer statements plus raw Yahoo corporate-action responses
- `golden/` — human-reviewed expected semantic records tied to raw artifact hashes
- `moomoo/` — one JSON per ticker (`data_engine/scripts/capture_fundamental_samples.py`), captured
  2026-07-10 once OpenD login was fixed (see git log). 14 endpoints per ticker: company
  profile, financials (income/balance-sheet/cash-flow/key-metrics), revenue breakdown,
  valuation trend, analyst consensus + rating summary, Morningstar report, shareholders
  overview, insider trades, dividends, short interest — plus one batched `owner_plate.json`
  covering all 4 tickers. Endpoints were chosen by reading moomoo's proto defs and SDK
  source first (57 calls total, no repeated trial-and-error against the live API — the
  relevant endpoints use burst rate limits, not a monthly quota; see `init.md` Section 5).

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

## moomoo findings (analyst-rating source reconnaissance, now resolved)

- **Path A confirmed: moomoo has real per-analyst historical ratings, not just
  current-outstanding ones.** `rating_summary`'s `analyst_rating_summary_list` embeds each
  analyst's own `rating_item_list` inline — DDOG alone has 20 analysts covering it with
  159 combined historical dated rating rows (one analyst, Koji Ikeda, has 18 by himself),
  each with date/rating/target-price/source-URL. NICE/SHOP/DUOL are similar (85–107 rows
  across 15–20 analysts). moomoo also ships pre-computed per-analyst `success_rate` and
  `excess_return` fields — the backtesting scoring module 4 wants may already be half
  done by moomoo's own vendor, not something to build from scratch.
  **This corrects an earlier same-day misread**: an ad-hoc probe script printed "2 rows"
  for this same endpoint by calling `len()` on the raw response *dict* (2 top-level keys:
  `next_key` + `analyst_rating_summary_list`) instead of the nested list — not a real
  data-scarcity finding. Lesson: always inspect the actual parsed structure, not a
  generic `len()`, before concluding an API is "thin."
- **Financials history is deep**: `get_financials_statements` returned 32–50 periods
  (quarterly + annual combined) per statement type per ticker, reaching back to
  2016–2019 depending on the ticker — comfortably enough for point-in-time backtesting,
  no pagination needed for a first pass.
- **Financial field IDs are numeric and undocumented client-side**: `structure_list`
  entries came back with populated `field_id` (e.g. 8001, 8002...) but an **empty
  `display_name`** for every field, for all 4 tickers, in `en` language mode — moomoo's
  own financials have the same "opaque tag" problem SEC XBRL does (see above), just
  with integer IDs instead of XBRL tag strings. A field_id → meaning mapping needs to be
  sourced separately before this is usable; don't assume the field names are self-evident.
- **Morningstar coverage exists for all 4 tickers** (star ratings 2–5), but
  `economic_moat_label` came back empty for all 4 despite the star rating being
  populated — coverage is per-field, not all-or-nothing even within one endpoint response.
- **`get_industrial_chain_*` is an industry taxonomy, not a company-to-company supply
  graph** (checked via the proto definitions, not called): nodes are named industry
  segments with an optional `plateId` (sector), not stock codes. It does not give
  "DDOG's suppliers are AWS/GCP/Azure" directly — the LLM-extraction path
  (`libs/factors/shared/extraction.py`) is still the way to build `supplies_to` KG edges,
  not a shortcut moomoo replaces.
- Ledger-recorded calls this capture run: 57 (the `api_call_ledger` is throttle and
  audit infrastructure, not a monthly quota).

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
