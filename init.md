# Project Init Document v2 (init.md)

## 0. Goal: Data Sources → Questions → Reports

This project answers a specific set of investment-research questions — it isn't "build a general-purpose data platform":

- **Is this company actually leveraged by large models?** — using metrics like gross profit per employee, tied to the "doesn't need proportional headcount growth to handle a new category of decisions" framework, to sort companies into valuation tiers
- **Is the current valuation reasonable relative to growth?** — PEG, but the growth-rate convention needs to be switchable, since different conventions (analyst consensus / historical CAGR / company guidance) can point to different conclusions
- **What is this company exposed to, up and down its supply chain — risk or opportunity?** — relationship graph + causal reasoning
- **Is a given analyst's track record worth trusting?** — TipRanks-style historical backtesting
- **Does an ETF/portfolio "look like" a healthy company when treated as one?** — virtual consolidation
- **Who's the "purest" name under a given theme?** — revenue-share ranking

Data sources (SEC / yfinance / Twelve Data / moomoo) are the raw material; the seven factor modules (Section 7) are the processing; the final output takes three forms: report cards for personal use (in the existing dark, Xiaohongshu-style aesthetic), an App dashboard, and a conversational LLM entry point. **Every step downstream has to be traceable back to the original raw material — that's the reason the point-in-time principle exists, not a technical purity fetish.**

---

## 1. Core Design Principles (hard constraints, not to be violated)

1. **Point-in-time correctness is priority one.** Distinguish `valid_time` (the period the data describes) from `transaction_time` (when the data became knowable). A restatement always produces a new vintage — never overwrite an old record. Factor computation and backtesting always operate on "the data that was visible as of a specific point in time."
2. **Computation logic is allowed to exist in exactly one place**, implemented in `libs/factors`; both `data-engine` and `llm-service` import from there. The App layer is only allowed to do **deterministic reformatting**: sorting, filtering, pagination, unit conversion, simple within-row arithmetic that doesn't span tables (e.g., "change vs. prior period"). Any logic that jointly computes a new metric across factors or time points must go back into `libs/factors` and be materialized into mart — it must not be computed on the fly in the Next.js backend. Use this concrete rule wherever the boundary feels blurry. **This includes screening/tagging logic** (e.g., the three-tier valuation framework, Section 7 module 7) — it reads other factors' mart outputs, but it is still a factor (a "composite factor," Section 6), not app-layer business logic.
3. **Factors never know their data's provenance, only its confidence.** Every fact reaching a factor is a `(entity_id, value, confidence, as_of)` tuple — a factor must not branch on "this came from SEC vs. moomoo vs. an LLM extraction." Provenance lives in `staging`/`raw` (via `raw_ref`) for traceability, not in the factor's decision logic. See Section 6 for the `confidence` column and Section 7 for the base/composite split this enables.
4. **The LLM reads only the `mart` schema**, enforced via a dedicated database role (`mart_readonly`) with a `statement_timeout` (5s recommended) and an application-layer cap on returned rows — this solves resource contention, not access control; the two are separate problems, and this doesn't need a heavier solution like connection-pool isolation.
5. **The App reads the database directly** (querying the mart schema), not through FastAPI. FastAPI's scope is narrowed to LLM-call orchestration only.
6. **Every moomoo call must go through the global call-budget gateway** — no module decides for itself whether to call. **Currently single-user only; how this quota gets allocated across multiple users is a deliberately deferred architectural debt, not something to design now** — see Section 10. **This is a public repo: moomoo trading contexts/order calls are hard-forbidden (quote/read-only data only) and no real credential ever gets committed — see `CLAUDE.md`'s hard constraints and `.github/workflows/security-gate.yml`.**
7. **Schema changes must raise an active alert.** Core point-in-time tables use dlt's frozen/contract mode, not the default auto-evolve.
8. **Scheduling has exactly one authority: Dagster.** Phase -1/0 don't yet introduce Dagster scheduling — see Section 8.
9. **Observability is centered on the Dagster UI** — on the condition that Dagster's own runtime metadata is persisted (see Section 6), not left on its default local storage.
10. **Supply-chain causal reasoning has a kill condition, defined in terms of confidence, not a separate accuracy concept**: no causal-reasoning step until the `confidence` of materialized supply-chain edges clears a bar (the bar itself is set after Phase -1 sampling, not guessed now).
11. **No moon.** GitHub Actions with path filtering is sufficient.

---

## 2. System Architecture

### 2.1 Data Flow Layers

```
L0 Source adapter layer   ── dlt: SEC / yfinance / Twelve Data / moomoo, producing versioned raw captures
L1 Storage layer          ── S3-compatible object storage: immutable source-response bytes
                           + Postgres raw.fetches: checksum, object pointer, fetch/publication timestamps, and lineage
                           → Postgres staging (normalized, dual timestamps + mandatory confidence, see Section 6 DDL),
                              including the knowledge graph (entities + edges — this is where all entity resolution lives, see Section 6)
                              → mart (clean point-in-time result tables, flattened 2D projections of the KG where applicable)
L2 Factor computation     ── libs/factors, wrapped as Dagster assets, code_version + data_version tracked (see Section 9 exception for LLM-assisted extraction factors)
                              - base factors    consume staging (incl. KG) data directly
                              - composite factors consume other factors' mart outputs; confidence = min() of all inputs consumed
L3 Analytics modules      ── the 7 concrete tools (Section 7)
L4 Backtest/portfolio validation ── historical replay at the staging layer
L5 Consumption layer      ── App (reads mart directly) + LLM chat (mart read-only SQL / tool calls)
```

### 2.2 Service Topology and Priority (the four services are not peers)

- **Tier 0 (needed now)**: Postgres + S3-compatible raw archive + the ingestion part of data-engine (dlt adapters)
- **Tier 1 (used in Phase 1-2)**: `libs/factors` + the mart schema
- **Tier 2 (starting in Phase 3)**: Dagster scheduling/UI, Dokploy Staging deployment; Production remains Phase 6-gated
- **Tier 3 (explicitly deferred to Phase 7)**: `llm-service`'s self-built `/chat` interface, `app-web`'s chat UI. **Exception: the MCP endpoint** — it reuses the same `libs/factors` functions and needs no new UI; wiring it into Claude Desktop/claude.ai is nearly free, and can happen well before the self-built `/chat`.

The four application units exchange structured data only through Postgres. The
data-engine/runtime boundary additionally uses S3-compatible object storage as
the immutable raw archive; object storage is not an App/LLM integration path.

---

## 3. Tech Stack

| Layer | Choice |
|---|---|
| Repo structure | GitHub monorepo |
| Repo task orchestration | No moon — GitHub Actions + path filtering |
| Python package management | uv |
| Data ingestion | dlt |
| Orchestration/scheduling | Dagster (asset-based, code_version + data_version tracked; **runtime metadata explicitly pointed at Postgres's `dagster` schema, not the default local store**) |
| Storage | Postgres for raw metadata/staging/mart/KG + S3-compatible object storage for immutable raw bytes (MinIO in Local/CI) |
| TS package management/runtime | Bun |
| App frontend | Next.js + TypeScript |
| App data display | Next.js backend queries the mart schema directly |
| Styling | Tailwind CSS + shadcn/ui |
| LLM chat UI | Next.js + Vercel AI SDK, Tier 3 |
| LLM backend | FastAPI, `/chat` + MCP endpoints, MCP prioritized over `/chat` |
| Contract sync | OpenAPI + openapi-typescript, limited to `/chat` and a few other genuinely cross-language endpoints |
| Deployment | Dokploy |

### 3.1 Environment Model and Promotion Gates

The runtime keeps six logical tiers because local tests, GitHub CI, and a future
PR preview have different dependency-substitution semantics. Only four actual
environments are provisioned in the active rollout:

| Actual environment | Logical tier(s) | Purpose | Data policy |
|---|---|---|---|
| Local | `local_dev`, `local_test` | Development and fixture replay | Fixtures by default; developer-owned Compose Postgres + MinIO |
| GitHub CI | `github_ci` | Code, DDL, image, security, and runtime-contract validation | Ephemeral fixtures/mocks; no real source credentials |
| Staging | `staging` | Real pipeline, point-in-time replay, and bounded backtest validation | Real sources; isolated canary universe and credentials |
| Production | `production` | Authoritative raw/staging/mart data and versioned strategy outputs | Real production universe; isolated credentials and storage |

`preview` remains a logical tier but no preview environment is provisioned until
the Web application needs per-PR visual review.

Promotion is artifact-based: PRs prove code in CI; the same image digest then
runs against real sources in Staging; Production receives that exact digest only
after the Staging pipeline/backtest gates and a human approval. Staging and
Production never share writable Postgres databases, S3 buckets, Dagster metadata,
API ledgers, or Vault secret paths. Strategy computation remains in
`libs/factors`; environments only schedule and materialize versioned results.

---

## 4. Repo Structure

```
/apps
  /data-engine        Python: dlt pipelines + dagster assets/schedules/sensors
  /app-web             TypeScript: Next.js frontend + data-display backend + chat UI (Tier 3)
  /llm-service         Python: FastAPI, MCP endpoint (priority) + /chat SSE endpoint (Tier 3)
/libs
  /contracts           Python: sample-aware cross-module DTOs + storage/repository/backtest ports;
                        no computation logic
  /factors             Python: the single implementation of the seven modules; function signatures
                        align with the eventual Dagster asset convention starting from Phase -1
    /base              Factors that consume staging (incl. KG) data directly
    /composite         Factors that consume other factors' mart outputs (e.g., module 7's three-tier tagging);
                        confidence = min() of all consumed inputs
    /shared            Entity resolution (KG read/write) + LLM structured-extraction primitive,
                        used by both base and composite factors, not reimplemented per module
  /runtime             Python: environment/dependency manifest, Postgres/KG/S3 probes, and the
                        boto3 immutable raw-object adapter; local/CI use Compose Postgres + MinIO,
                        deployed environments receive the same DATABASE_URL/S3_* contract from infra2
/db
  /migrations          DDL for the four schemas: raw / staging / mart / dagster
  /roles.sql           mart_readonly (incl. statement_timeout config) and other role permissions
/.github
  /workflows           CI triggered with path filtering
```

---

## 5. Data Sources

| Source | Role | Details |
|---|---|---|
| SEC EDGAR | Official structured financial data | `data.sec.gov/api/xbrl/companyfacts/CIK##########.json`; User-Agent must include an email. Financial companies have no gross-profit field, needs an industry branch. Headcount is free text, needs extraction with a fallback. |
| yfinance | Fallback source for daily price data | Unofficial, no SLA, fallback only |
| Twelve Data | Official source for daily price/fundamentals | One of the primary sources for price data |
| moomoo | Rate-limited (bursts/30s), not a monthly quota — see below | Whether it has analyst ratings, ETF holding weights, or supply-chain fields — **all unverified, don't assume it does**, see Phase -1 |
| Historical analyst ratings — alternate source | **To be confirmed, currently unresolved** | If the moomoo audit confirms it has none, Phase -1 needs to scout free/low-cost rating archives; the analyst-backtest module is marked blocked until a source is confirmed, not deleted from the roadmap |
| ETF holdings weight data | **Confirmed (2026-07-07): SEC EDGAR N-PORT-P** | Monthly per-series filings, per-holding `pctVal` + CUSIP (verified on QQQ and ARKK; 2026-07-11 also on IVV, AGIX, MCHI). Pitfalls: the raw XML is `primary_doc.xml` (the filing's `primaryDocument` field points at the XSL-rendered HTML); multi-series trusts (e.g. ARK) must be queried by series ID via browse-edgar, not by trust CIK; foreign holdings carry CUSIP `000000000`, so `same_as` resolution needs an ISIN/name fallback. **SPY is a UIT and absent from SEC's fund-ticker mapping — proxy the S&P 500 with IVV/VOO.** The publicly available filing is the last month of each fiscal quarter (~1-3 months behind) — fine for defining a universe, not a live holdings feed. |

**moomoo quota, corrected 2026-07-10**: moomoo's own "Authorities and Quota" docs only define two quota systems — a *subscription* quota (real-time push, tiered 100/300/1000/2000 by account assets/trade volume) and a *historical-candlestick* quota (per unique stock per rolling window, not per call). Fundamental/quote endpoints (`get_financials_statements`, `get_research_rating_summary`, `get_valuation_detail`, etc. — everything the Phase -1 moomoo audit actually uses) appear in neither table; they're only rate-limited (bursts per 30s), not capped at a monthly total. The "2,000 calls/month" figure that was previously written here conflated the subscription-quota tier ceiling with a call budget — there is no evidence for a real monthly cap on these endpoints. `moomoo_ledger.py`'s gate is kept anyway as a defensive throttle/audit trail (Section 1 rule 6's "no module decides for itself whether to call" principle still holds), not because 2,000/month is a real moomoo-side limit.

**moomoo analyst-rating depth, confirmed 2026-07-10 (Path A)**: `get_research_rating_summary`'s per-analyst summary rows embed each analyst's own historical `rating_item_list` inline — DDOG alone has 20 analysts with 159 combined dated historical rating rows, plus moomoo-computed `success_rate`/`excess_return` per analyst. This is enough for module 4's per-analyst backtesting — see `apps/data-engine/samples/README.md` for the full findings and `apps/data-engine/samples/moomoo/` for the captured fixtures.

---

## 6. Database Schema Design

Four schemas: `raw`, `staging`, `mart`, `dagster` (Dagster's own run/event/schedule storage, explicitly configured, not left on default local storage).

The concrete point-in-time structure (staging layer):

```sql
create table staging.financial_facts (
    id                bigint generated always as identity primary key,
    unified_id        text not null,          -- KG entity id, see staging.kg_entities below
    metric            text not null,          -- 'revenue' / 'gross_profit' / ...
    fiscal_period     text not null,          -- '2025Q4'
    valid_time        daterange not null,     -- the period this data describes
    transaction_time  timestamptz not null default now(),  -- when this became knowable
    value             numeric,
    confidence        numeric not null,       -- 0-1, MANDATORY on every row, no nulls.
                                               -- Absorbs both "extraction uncertainty" (LLM-derived
                                               -- facts self-report a score) AND "source reliability"
                                               -- (e.g. yfinance's lack of an SLA is expressed here,
                                               -- not as a separate ad hoc rule). Official filed data
                                               -- (SEC) defaults to 1.0.
    source            text not null,          -- 'sec' | 'yfinance' | 'twelvedata' | 'moomoo'
    raw_ref           text,                   -- pointer back to the original record in the raw schema
    is_restatement    boolean not null default false
);

create index idx_financial_facts_asof
    on staging.financial_facts (unified_id, metric, fiscal_period, transaction_time desc);
```

The core shape of a point-in-time query:

```sql
select distinct on (unified_id, metric, fiscal_period) *
from staging.financial_facts
where transaction_time <= :as_of_timestamp
order by unified_id, metric, fiscal_period, transaction_time desc;
```

Every row in the mart layer must already have this logic resolved — it can't be left for downstream consumers to choose.

**Knowledge graph (replaces the old flat `symbol_mapping` table).** Entity resolution is not unique to companies — companies, ETFs, analysts, and supply-chain nodes all have the same "same real-world thing, different IDs per source" problem, so it's modeled once as a graph, implemented as plain Postgres tables (no separate graph engine — the query patterns needed here are shallow joins, not deep multi-hop traversal):

```sql
create table staging.kg_entities (
    id            text primary key,          -- our internal unified_id
    entity_type   text not null,             -- 'company' | 'etf' | 'analyst' | 'supply_chain_node'
    display_name  text not null
);

-- Source identifier property rows locate source-specific entity nodes. Entity
-- resolution then traverses a same_as edge to the unified entity as of the
-- requested transaction-time cutoff.
create table staging.kg_identifiers (
    id                bigint generated always as identity primary key,
    entity_id         text not null references staging.kg_entities(id),
    source            text not null,
    identifier_type   text not null,
    identifier_value  text not null,
    valid_time        daterange not null,
    transaction_time  timestamptz not null,
    confidence        numeric not null,
    raw_ref           text not null
);

create table staging.kg_edges (
    id                bigint generated always as identity primary key,
    from_id           text not null references staging.kg_entities(id),
    to_id             text not null references staging.kg_entities(id),
    relation_type     text not null,          -- 'same_as' (cross-source ID crosswalk, e.g. CIK<->ticker<->
                                               -- moomoo_code<->CUSIP/ISIN) | 'supplies_to' | 'holds' (ETF->company)
                                               -- | 'covers' (analyst->company) | ...
    valid_time        daterange not null,
    transaction_time  timestamptz not null default now(),
    confidence        numeric not null,       -- same semantics as financial_facts.confidence.
                                               -- The supply-chain causal-reasoning kill condition
                                               -- (Section 1, rule 10) reads confidence on 'supplies_to' edges.
    source            text not null,
    raw_ref           text
);

create index idx_kg_edges_asof
    on staging.kg_edges (from_id, relation_type, transaction_time desc);
```

Mart's flattened 2D tables (e.g. a company-to-ticker lookup, or a supply-chain adjacency table for a given company) are SQL views/materializations over `kg_entities` + `kg_edges`, not separately maintained.

Role permissions (`roles.sql`):

```sql
alter role mart_readonly set statement_timeout = '5s';
grant select on schema mart to mart_readonly;
-- the application layer additionally forces a LIMIT (1000 rows suggested) on LLM-generated
-- queries; don't rely solely on the database-side setting
```

Other tables: `api_call_ledger` (moomoo quota ledger), `ingestion_health_log` (only business-specific metrics the Dagster UI doesn't already cover).

---

## 7. The Seven Analytics Modules

Modules 1-6 are **base factors** (Section 4, `libs/factors/base`) — they consume staging/KG data directly. Module 7 is a **composite factor** (`libs/factors/composite`) — it consumes other modules' mart outputs, and its own confidence is the min() of everything it reads.

1. **PEG**: switchable growth-rate conventions
2. **Gross profit per employee**: financial/non-financial branch, headcount gaps explicitly flagged rather than silently dropped
3. **Supply-chain relationship graph + causal reasoning**: graph first (KG `supplies_to` edges), causal reasoning gated behind the confidence-based kill condition (Section 1, rule 10)
4. **Analyst backtesting**: **two paths** — Path A (moomoo confirmed to have historical ratings) uses it directly; Path B (it doesn't) marks this blocked, Phase -1 scouts alternate sources, module stays on the roadmap
5. **ETF virtual company**: depends on a holdings-weight data source, must be confirmed in Phase -1
6. **Pure-blood company screening**: LLM-assisted semantic classification of segment revenue
7. **Three-tier valuation tagging** (composite): sorts a company into the traditional / tech / large-model-native P/S tier (Vision, "large-model-driven company" framework) by reading module 2's gross-profit-per-employee output (and other base factors as needed) — this is a screening/interpretation layer expressed as a factor, not app-layer logic (Section 1, rule 2)

---

## 8. Implementation Phases and Verification Criteria

| Phase | Goal | Verification | Infrastructure scope |
|---|---|---|---|
| Phase -1 | Data reconnaissance + **data availability matrix** (below) | Matrix surfaces at least 3 unexpected findings; ETF-weight source and analyst-rating alternate source must reach a definitive yes/no | Local scripts, no Dagster/Dokploy |
| Phase 0 | Walking skeleton, function signatures aligned to the future Dagster asset convention | Querying the value as of some historical point matches what was actually disclosed at that time | Local Compose + GitHub CI; no deployed scheduler |
| Phase 1 | PEG + ETF virtual company (if the weight source is confirmed) | Manually check PEG for 3 stocks; reverse-engineer a real ETF | Dagster introduced (optional) |
| Phase 2 | Gross profit / headcount | Spot-check 10 companies, ≥90% accuracy | Dagster |
| Phase 2.5 | Three-tier valuation tagging (module 7, composite factor over Phase 2's output) | Cross-check tier assignment against a handful of companies with an obvious, undisputed tier (e.g. a clear large-model-native name and a clear traditional name) — the boundary cases are exactly what Phase -1/2 sample data should surface, not something to pre-guess | Dagster |
| Phase 3 | Analyst backtest + activate the real Staging pipeline | Two consecutive Dagster-scheduled canary runs prove idempotency, new vintages, lineage, and frozen-schema stability | Dagster + Dokploy Staging deployment |
| Phase 4 | Supply-chain relationship graph | Manually verify edge plausibility | |
| Phase 5 | Pure-blood company screening | Spot-check top 10 for plausibility | |
| Phase 6 | Backtest and portfolio validation + Production readiness | Direction matches a known strategy's actual performance; backup/restore and exact-image promotion gates pass | Production is initialized only after this gate |
| Phase 7 | App / MCP (priority) / `/chat` (Tier 3, last) | Walk the full pipeline end to end once | |

**Data availability matrix**: not a document to file away once written. Every factor function's return value carries a `data_availability: "verified" | "unverified"` field matching the matrix, so both the App display and LLM answers can show whether the number behind them still hasn't been verified.

Environment sequencing is therefore part of the phase contract: Local/CI are
available in Phase 0; no real scheduled Staging run may use cron or GitHub
Actions before Dagster becomes the single scheduling authority in Phase 3; and
Production remains closed until the Phase 6 data, backtest, recovery, and
promotion gates pass.

---

## 9. Known Risks / Pitfalls

- SEC XBRL tags are inconsistent across industries — don't assume field names/units are uniform
- yfinance has no SLA — can't be the sole dependency on a critical path
- LLM-generated ad hoc SQL touching raw/staging carries a semantic look-ahead-bias risk without raising an error — already blocked off via role permissions (Section 6)
- dlt schema evolution must use frozen mode for core tables
- **Dagster's code_version/data_version assumes computation is deterministic.** LLM-based extraction (e.g., headcount) is inherently non-deterministic — rerunning the same code_version can produce slightly different results, which the mechanism may misread as "data_version changed, recompute downstream" — or conversely, treat a genuine restatement signal as noise. This will surface in Phase 2 (headcount extraction) — for LLM-extraction-based factors, the data_version hash should be based on the extraction result itself, not the raw call each time; set a manual change-tolerance threshold where needed rather than relying on the default automatic hash.
- **LLM self-reported confidence is a starting point, not a ground truth.** Phase -1/2 use a plain self-reported 0-1 score in the extraction prompt (cheapest to ship). It may turn out poorly calibrated once real samples come in — the fallback is multi-sample self-consistency (same fact extracted N times, agreement rate as confidence), which costs several times the LLM calls. Don't build the self-consistency path until sample data shows the self-reported score is actually unreliable.
- Discriminated unions only reliably generate `oneOf` when declared at the top level with an explicit discriminator; nested cases are a known open issue — handle if/when encountered

---

## 10. Known Architectural Debt (deliberately not solved now, but written down)

- **Multi-user moomoo quota allocation**: not relevant while single-user; must be designed before going multi-user, no pre-modeling now
- **Enforcement of the App-side "deterministic reformatting" boundary**: Section 1 states the rule but there's no code-level check (e.g., a lint rule preventing cross-table aggregation in the Next.js backend) — could add a CI check later, not now
- If Postgres concurrency becomes a real bottleneck, re-evaluate read/write splitting or a caching layer

---

## 11. Phase -1 Starter Checklist

1. Initialize the monorepo directory structure (Section 4)
2. `db/migrations`: stub out the four schemas (raw/staging/mart/dagster) + `staging.kg_entities`/`staging.kg_edges` + `roles.sql`
3. `libs/factors`: a factor registry skeleton split into `base`/`composite`/`shared`, function signatures aligned to the future Dagster asset convention (even without wiring into Dagster yet) — every base-factor signature carries `confidence` in and out
4. Local scripts (no Dagster/Dokploy): connect to the SEC company-facts API, pull samples for test names like DDOG, NICE, SHOP, DUOL
5. **KG entity-resolution smoke test, one concrete sample per entity type** — not just companies: one ETF, one analyst, one supply-chain relationship (e.g. a real DDOG supplier), each run through the `same_as`/`supplies_to`/`covers` edge machinery end to end. This is what actually validates the KG design, not just a checklist item
6. **Data availability matrix**: confirm the ETF-holdings-weight source and moomoo's historical-rating coverage definitively, rather than continuing to assume
7. `.github/workflows`: a minimal path-filtered CI skeleton

Everything else gets resolved inside Claude Code as the actual code takes shape.
