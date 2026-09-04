# Project Init Document v2 (init.md)

## 0. Goal: Data Sources → Questions → Reports

This project answers a specific set of investment-research questions — it isn't "build a general-purpose data platform":

- **Is this company actually leveraged by large models?** — using metrics like gross profit per employee, tied to the "doesn't need proportional headcount growth to handle a new category of decisions" framework, to sort companies into valuation tiers
- **Is the current valuation reasonable relative to growth?** — PEG, but the growth-rate convention needs to be switchable, since different conventions (analyst consensus / historical CAGR / company guidance) can point to different conclusions
- **What is this company exposed to, up and down its supply chain — risk or opportunity?** — relationship graph + versioned scenario exposure; causal language requires separate causal evidence
- **Is a given analyst's track record worth trusting?** — TipRanks-style historical backtesting
- **Does an ETF/portfolio "look like" a healthy company when treated as one?** — virtual consolidation
- **Who's the "purest" name under a given theme?** — revenue-share ranking

Data sources (SEC / yfinance / Twelve Data / moomoo) are the raw material; the seven factor modules (Section 7) are the processing; the final output takes three forms: report cards for personal use (in the existing dark, Xiaohongshu-style aesthetic), an App dashboard, and a conversational LLM entry point. **Every step downstream has to be traceable back to the original raw material — that's the reason the point-in-time principle exists, not a technical purity fetish.**

---

## 1. Core Design Principles (hard constraints, not to be violated)

1. **Point-in-time correctness is priority one.** Distinguish `valid_time` (the period the data describes) from `transaction_time` (when the data became knowable). A restatement always produces a new vintage — never overwrite an old record. Factor computation and backtesting always operate on "the data that was visible as of a specific point in time."
2. **Computation logic is allowed to exist in exactly one place**, implemented in `libs/factors` and invoked by data-engine/Dagster. `llm-service` and the App consume materialized outputs through typed mart read contracts; they never invoke factor computation. The App layer is only allowed to do **deterministic reformatting**: sorting, filtering, pagination, unit conversion, simple within-row arithmetic that doesn't span tables (e.g., "change vs. prior period"). Any logic that jointly computes a new metric across factors or time points must go back into `libs/factors` and be materialized into mart — it must not be computed on the fly in the Next.js backend. Use this concrete rule wherever the boundary feels blurry. **This includes screening/tagging logic** (e.g., the three-tier valuation framework, Section 7 module 7) — it reads other factors' mart outputs, but it is still a factor (a "composite factor," Section 6), not app-layer business logic.
3. **Factors never know their data's provenance.** Factors consume provenance-neutral typed semantic records: an opaque `input_id`, typed subject identity, value/unit/currency where relevant, valid period, confidence, and the snapshot cutoff. They must not receive or branch on `source`, `raw_ref`, accession, or extractor metadata. The runner uses the opaque IDs to record exact consumed-input lineage outside factor computation.
4. **LLM surfaces read only authorized typed `mart` projections.** MCP and `/chat` call bounded `ResearchQueryService` methods over a `mart_readonly` repository; they never execute arbitrary model-generated SQL or recompute factors. A trusted server adapter derives `AccessContext` from the browser session, delegated MCP OAuth credential, or service identity and authorizes the exact resource before the repository issues mart SQL or retrieves an artifact. Tool arguments never supply tenant, principal, role, entitlement, or publication authority. The database role has a `statement_timeout` (5s recommended), and repositories enforce pagination and row caps. The App implements the same semantic read contract and authorization decision directly against mart.
5. **The App reads the database directly** (querying the mart and app schemas through server-only typed repositories), not through FastAPI. FastAPI's scope is narrowed to LLM-call orchestration only. Direct database access does not bypass authorization: publication policy is checked before a mart query, and forced owner RLS is a second boundary for private app rows.
6. **Every external call — data vendors and model providers alike — goes through the global source gateway with a declared capacity** — no module decides for itself whether to call. Each source declares its capacity (rate window, daily/monthly budget, concurrency) in the registry, every call is recorded in `api_call_ledger`, and a call that would exceed the declared capacity is refused or queued rather than fired: capacity is part of the capture logic, not an afterthought inside one adapter. This applies to SEC, Twelve Data, yfinance, OpenFIGI, N-PORT, moomoo and the LLM extraction provider alike (owner decision 2026-09-04; until then only moomoo was gated). End-user access only reads completed materialized results and never triggers a source or model call, so user entitlements do not allocate or bypass source quota. Pipeline-wide source budgeting and scheduling remain separate from consumer authorization; any future user-funded source-demand product requires a new architecture decision. **This is a public repo: moomoo trading contexts/order calls are hard-forbidden (quote/read-only data only) and no real credential ever gets committed — see `CLAUDE.md`'s hard constraints and `.github/workflows/security-gate.yml`.**
7. **Schema changes must raise an active alert.** Core point-in-time tables use dlt's frozen/contract mode, not the default auto-evolve.
8. **Scheduling has exactly one authority: Dagster.** Local reconnaissance may use one-shot scripts, but Dagster is introduced with the first executable Gate 1 snapshot/factor slice and is the only authority for real recurring runs — see Section 8.
9. **Observability is centered on the Dagster UI** — on the condition that Dagster's own runtime metadata is persisted (see Section 6), not left on its default local storage.
10. **Supply-chain propagation is scenario analysis unless causality is independently established.** Every propagation run declares a versioned shock, direction, materiality/sensitivity rule, horizon, and minimum edge confidence. Low-confidence paths are killed, but high confidence in a disclosed relationship does not by itself prove a causal effect.
11. **No moon.** GitHub Actions with path filtering is sufficient.
12. **Multi-source truth is resolved by declared fusion rules, never by ingestion recency.** Staging keeps every source's assertion side by side; the mart winner comes from the metric registry's per-field `source_priority` (Section 6, "Source fusion"), and the ruleset version is part of mart lineage. Corollary: `transaction_time` (= knowable-at) is always written explicitly from a source property, never defaulted to the insert clock, and every staging row also carries `recorded_at` (ingestion clock, audit only) plus — for parsed facts — a `mapping_version`, so "the data changed" and "the cleaning logic changed" stay distinguishable forever.
13. **Scope is immutable and explicit.** `UniverseRef` contains the universe ID, version, and membership or resolver hash. Every capture, snapshot, factor invocation, screen, strategy, report, SLO evaluation, and audit binds that exact reference plus `as_of` and `valid_on` where applicable. Research Catalog, applicability, capture, and SLO identities remain separate immutable contracts and are bound together by the release manifest. A mutable "current universe" or an unversioned ticker list is never a valid execution scope.
14. **Applicability is frozen before execution and scope cannot shrink to obtain green status.** Required/optional/not-applicable cells are approved before a run. Removing a subject, invocation, theme, scenario, or required cell creates a new catalog/SLO version, records product-owner approval, and narrows the claim; it cannot retroactively make a failed gate pass. The supported workflow must also onboard an issuer and theme that were absent from development evidence without changing factor or consumer code.
15. **Capture completeness is row-level, not job-level.** A versioned `CaptureScope` enumerates every required `(scope, subject or instrument, domain, partition or vintage)` cell. Its `CaptureManifest` records raw capture, normalized record, confidence, times, mapping/policy versions, quality result, and lineage for every cell. Missing required cells fail even when Dagster is green or raw payload counts look plausible. #58/#61 own the contracts, #27/#51/#67 emit the evidence at their bounded scopes, and #68 independently audits the complete Production shadow candidate before #54 may record authoritative graduation.
16. **V1 total return uses unadjusted bars plus explicit corporate-action lifecycle events.** The simulator applies declaration/knowability, ex, effective, record, and pay semantics exactly once through a monotonic event clock. Adjusted-close data may be retained for reconciliation but cannot be combined with separately applied splits or dividends.
17. **Every issuer decomposes into an operating component and a financial component; both are computed, and both land in one wide mart row** (owner decision 2026-09-04, restating #59 round 2). Every company's net assets earn a return, so the split is not a sector branch: operating real profit = operating gross profit − risk-free return on the capital that earns it; financial real profit = investment/financial returns − risk-free return on the assets that earn them; each divided by headcount where the module calls for labor efficiency. The research specification defines and independently reviews the per-class proxies (a bank's pre-provision profit stands in for operating gross profit where cost of revenue is not reported) and the comparison rule that reads the two components side by side. Blanket exclusion of financial issuers may be a strategy eligibility choice, but it cannot satisfy the gross-profit-per-employee module or the Vision's financial/non-financial handling requirement — and a uniform formula that makes an entire issuer class structurally negative does not satisfy it either (v0.2.0's uniform charge, #528). Whether a negative component is a valid low signal is recorded per component with the versioned definition, and the output invariants assert exactly what the definition says.
18. **Independent holdouts must be executable and blind.** A named custodian controls labels; implementation code, parameters, catalog, and thresholds are content-hashed before authors see labels or results. In a solo workflow, independence requires an external evaluator or a newly sampled post-freeze corpus. Failed results or changed logic require a new version and a fresh untouched holdout; self-reviewed public fixtures are development goldens only.
19. **Every source has an expiring rights and budget go/no-go.** Before Production use, a named authorized owner records raw-retention, caching, derived-metric, report/card/publication, quotation, and attribution rights; quota/SLA behavior; approved vendor, API, storage, extraction, and human-review budgets; and a review expiry. Unknown rights, expired approval, or an over-budget full-catalog projection fails the gate. The only alternatives are to approve/fund a valid source or explicitly narrow `vision.md` and the Research Catalog.
20. **Continuous operation requires natural source refreshes.** Before a soak begins, each required source class declares its cadence, maximum age, required naturally changed partitions/publication transitions, observation window, owner, and alert budget. Immediate retries, reparsing unchanged bytes, synthetic mutations, and replaying old fixtures do not count. Slow quarterly or annual sources keep graduation blocked unless their pre-approved natural-refresh requirement is observed.
21. **Production graduation is a user-facing and independently reviewed event.** Every non-local MCP, App, `/chat`, report, and artifact endpoint requires TLS and authentication. Full-catalog load must pass approved capacity limits without starving Staging, backup, monitoring, or consumers. A destructive clean restore must meet declared database and raw-object RPO/RTO. A human approves a Production card deck against the content/visual/rights rubric, and an independent reviewer signs the final audit; automated green checks alone cannot graduate shadow output.
22. **Extension is registry-driven and deliberately static.** Content-hashed `SourceRegistry` and `SemanticTypeRegistry` snapshots are authenticated by the signed release manifest. They resolve source adapters/normalizers and typed normalized models/factor-input projectors/repositories. Adding a source for an existing semantic type changes only source-owned code, registrations, policies, and tests. Adding a record type inside an existing domain changes only its typed model, storage/migration, projector, registration, and tests. Generic capture, manifest, snapshot, Dagster composition, factor execution, lineage, usage, and review code must not branch on source or record type. A new domain or new business meaning remains an explicit contract/factor/catalog change. V1 uses checked-in registrations at process start, not dynamic plugin discovery, runtime code loading, an event bus, or arbitrary JSON facts. **Concretely for metrics: adding one is a registry edit, never a migration and never an edit to generic transport code.** Two current violations are named so they are fixed rather than copied: `staging.strategy_backtest_inputs.input_key` carries an enumerated `CHECK` instead of validating against the metric registry, and `strategy_bridge._STRATEGY_PERIODIC_KEYS` hard-codes which metrics are period-shaped inside generic transport, which is the branch-on-record-type this rule forbids.
23. **Data usage is automatic, idempotent infrastructure evidence.** Capture manifests, repositories, snapshot selection, and factor/strategy runners record append-only semantic-use identities outside factor code. Usage views distinguish planned demand, capture/normalization evidence, snapshot selection, factor consumption, and strategy consumption. Retries do not double-count one semantic use, missing telemetry is an error rather than zero use, and source attribution is recovered through lineage rather than exposed to a factor. V1 deliberately excludes page-view/query analytics so the App remains strictly `mart_readonly`.
24. **Strategy quality review starts from expected data, not successful outputs.** The framework compiles source-neutral `DataRequirement` records from the immutable strategy, factor, and execution/return-rule graph for every scheduled cutoff and applicable subject, then left-joins actual capture, snapshot, consumption, lineage, rights, freshness, and quality evidence. Missing data that suppresses a candidate or produces no trade remains a failed expected cell with affected decisions; undeclared consumption or broken reverse lineage also fails. Observed low usage can prioritize remediation but can never relax applicability, retention, source policy, or an SLO retroactively.
25. **Qlib is the selected factor-expression and backtest engine, not a data or semantic authority.** Serializable TrueAlpha definitions wrap versioned Qlib expressions and operators, and only adapters inside `libs/factors` may invoke Qlib. Qlib receives provenance-neutral inputs projected from durable PIT snapshots and the explicit post-decision market-event stream; it may not crawl or select vintages, resolve membership, infer confidence or lineage, combine adjusted prices with explicit actions, or replace Decimal monetary logic. Every run pins the Qlib build, adapter, operator registry, strategy, and input snapshot identities. Native `libs/factors` implementations remain valid for Decimal, graph, and other computations that do not fit Qlib's matrix-expression model.
26. **Consumer identity and research access are governed outside computation.** The additive `app` schema stores principals, memberships, append-only entitlement grants/revocations, immutable release-bound publication policy sets, owner-only private object locators, authorization decisions, and content-free audit events. Browser, App, chat, Claude Code, Codex, and service-agent access uses the same server-derived `AccessContext` and stable authorization contract. Private conversation/document content is owner-only by default; administrators may see policy-permitted materialized strategy/backtest results and tenant-filtered non-content audit metadata, not private content. Only a policy-authorized administrator may record a request for an already registered immutable replay definition; that request cannot carry code, parameters, dates, source selectors, or execution inputs, and Dagster remains the sole execution authority. `AccessContext`, tenant, principal, role, entitlement, and publication identities never enter factors, Qlib, `BacktestDataGateway`, `DecisionSnapshot`, or `ReplayEventStream`. Consumer reads resolve through a governed, access-controlled current-pointer to an immutable exact run per ADR A1 (`docs/architecture-decisions/A1-evidence-chain-in-database.md`); exact run identities stay queryable for reproducible reads and a bare mutable `latest` is never a read path.

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
L2 Factor computation     ── libs/factors: Qlib-backed expressions plus native Decimal/graph factors, wrapped as Dagster assets with code_version + data_version tracked (see Section 9 exception for LLM-assisted extraction factors)
                              - base factors consume provenance-neutral views projected from durable PIT snapshots
                              - composite factors reload other factors' mart outputs; confidence cannot exceed the minimum consumed confidence, and a versioned stricter policy is allowed
L3 Analytics modules      ── the 7 concrete tools (Section 7)
L4 Backtest/portfolio validation ── Qlib behind the TrueAlpha adapter: PIT decision snapshots + a separate monotonic future market-event stream
L5 Consumption layer      ── server-derived AccessContext + policy-before-read authorization
                           + App direct mart adapter + typed MCP/ResearchQueryService tools used by `/chat`
                           + owner-only private state and content-free audit metadata in app
```

The extensible core stays intentionally small:

```text
checked-in SourceRegistry + SemanticTypeRegistry
  -> Capture -> Normalize -> Snapshot -> Compute -> Materialize
                                |                    |
                                +-> automatic usage +-> bounded usage views
ResearchCatalog + RequirementGraph + Schedule + Applicability
  -> compile_expected_demand -> run/module/emitter usage requirements
StrategyDefinition -> DataRequirement graph -> StrategyDataQualityReview
```

Dagster compiles and schedules this one write spine. The two registries select
implementations; the demand compiler derives the exact denominator for one explicitly
finite invocation batch and never accepts caller-supplied planned cells. Each requirement
binds the partition-resolver output and lookback window; recurring horizon completeness
remains Dagster's responsibility. Usage and strategy-quality projections are
read/audit slices over the same immutable manifests and lineage, not alternative pipelines.

### 2.2 Service Topology and Priority (the four services are not peers)

- **Tier 0 (needed now)**: Postgres + S3-compatible raw archive + the ingestion part of data-engine (dlt adapters)
- **Tier 1 (Gate 1 onward)**: `libs/factors`, the mart schema, and Dagster asset execution; the first real recurring run already uses Dagster
- **Tier 2 (Gate 1 Staging through Gate 4 Production)**: persistent Dagster scheduling/UI plus Dokploy environments; Production serves the App and MCP today without a recorded graduation (#54); until #54 records it, its outputs are ungraduated shadow output that happens to be reachable, and every Gate 4 requirement still stands
- **Tier 3 (Gate 3 consumption, deployed proof in Gate 4)**: MCP first, then reports/cards and the App; `llm-service`'s self-built `/chat` and `app-web` chat UI remain the last consumption surface

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
| Factor expression and backtest engine | Qlib (`pyqlib`), invoked only through versioned `libs/factors` adapters over TrueAlpha PIT snapshots and event streams |
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
PR preview have different dependency-substitution semantics. The table below is the
target active rollout, not a claim that every environment has passed its gate:

| Actual environment | Logical tier(s) | Purpose | Data policy |
|---|---|---|---|
| Local | `local_dev`, `local_test` | Development and fixture replay | Fixtures by default; developer-owned Compose Postgres + MinIO |
| GitHub CI | `github_ci` | Code, DDL, image, security, and runtime-contract validation | Ephemeral fixtures/mocks; no real source credentials |
| Staging | `staging` | Real pipeline, point-in-time replay, and bounded backtest validation | Real sources; isolated canary universe and credentials |
| Production | `production` | Serving since 2026-07 (App behind login, MCP, daily TOPT/QQQ/canary ticks) but **ungraduated**: authoritative research only after #54 records Gate 4 graduation | Exact approved Research Catalog/universe; isolated credentials and storage; rule-19 source attestations still missing (#60) |

`preview` remains a logical tier but no preview environment is provisioned until
the Web application needs per-PR visual review.

Promotion is artifact-based: PRs prove code in CI; one signed release manifest then
runs against real sources in Staging; Production receives that exact complete manifest
only after the Staging pipeline/backtest gates and a human approval. Staging and
Production never share writable Postgres databases, S3 buckets, Dagster metadata,
API ledgers, or Vault secret paths. Strategy computation remains in
`libs/factors`; environments only schedule and materialize versioned results.

The promoted unit is a versioned release manifest, not one ambiguous application
image. It binds the App, LLM service, data-engine/Dagster execution artifact,
migrations, Research Catalog and SLO versions, configuration hashes, and accepted
consumer artifacts. Manual host sweeps remain reconnaissance/bootstrap tools and can
never satisfy a scheduled-run gate. The deployed Dagster resource contract must reach
the host-only moomoo OpenD boundary without exposing OpenD publicly; #11 owns the
executable connectivity and negative-network proof.

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
                        align with the executable Dagster asset convention frozen by Gates 0-1
    /base              Factors that consume provenance-neutral views projected from PIT snapshots
    /composite         Factors that consume other factors' mart outputs (e.g., module 7's three-tier tagging);
                        confidence cannot exceed the minimum consumed confidence;
                        a versioned stricter policy is allowed
    /shared            Entity resolution (KG read/write) + LLM structured-extraction primitive,
                        used by both base and composite factors, not reimplemented per module
  /runtime             Python: environment/dependency manifest, Postgres/KG/S3 probes, and the
                        boto3 immutable raw-object adapter; local/CI use Compose Postgres + MinIO,
                        deployed environments receive the same DATABASE_URL/S3_* contract from infra2
/db
  /migrations          DDL for the five schemas: raw / staging / mart / dagster / app
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
| LLM extraction provider | External source for prose-only fields (headcount, segment classification, disclosed relationships) | Not provisioned as of 2026-09-04 (`libs/factors/shared/extraction.py` is a stub; #70). Enters like any other source: a `SourceRegistry` entry, a rule-19 rights/budget record, a declared capacity (requests per window, tokens per day) enforced through the rule-6 gateway and `api_call_ledger`, and an append-only invocation record per Section 9. Never called from a factor or a consumer surface; replay reads the stored result. |
| moomoo | Rate-limited (bursts/30s), not a monthly quota — see below | Historical per-analyst rating events are confirmed. Historical public availability for backtest eligibility still requires independent evidence; vendor backfill/update time is not a substitute. ETF weights and company-level supply-chain edges are not sourced from moomoo. |
| Historical analyst ratings — fallback | Required only if moomoo history or usage rights fail the production source gate | Any fallback must preserve analyst identity, recommendation time, independently defensible public availability, vendor revision time, target/rating semantics, and usage/retention rights. |
| ETF holdings weight data | **Confirmed (2026-07-07): SEC EDGAR N-PORT-P** | Monthly per-series filings, per-holding `pctVal` + CUSIP (verified on QQQ and ARKK; 2026-07-11 also on IVV, AGIX, MCHI). Pitfalls: the raw XML is `primary_doc.xml` (the filing's `primaryDocument` field points at the XSL-rendered HTML); multi-series trusts (e.g. ARK) must be queried by series ID via browse-edgar, not by trust CIK; foreign holdings carry CUSIP `000000000`, so `same_as` resolution needs an ISIN/name fallback. **SPY is a UIT and absent from SEC's fund-ticker mapping — proxy the S&P 500 with IVV/VOO.** The publicly available filing is the last month of each fiscal quarter (~1-3 months behind) — fine for defining a universe, not a live holdings feed. |

**moomoo quota, corrected 2026-07-10**: moomoo's own "Authorities and Quota" docs only define two quota systems — a *subscription* quota (real-time push, tiered 100/300/1000/2000 by account assets/trade volume) and a *historical-candlestick* quota (per unique stock per rolling window, not per call). Fundamental/quote endpoints (`get_financials_statements`, `get_research_rating_summary`, `get_valuation_detail`, etc. — everything the Phase -1 moomoo audit actually uses) appear in neither table; they're only rate-limited (bursts per 30s), not capped at a monthly total. The "2,000 calls/month" figure that was previously written here conflated the subscription-quota tier ceiling with a call budget — there is no evidence for a real monthly cap on these endpoints. `moomoo_ledger.py`'s gate is kept anyway as a defensive throttle/audit trail (Section 1 rule 6's "no module decides for itself whether to call" principle still holds), not because 2,000/month is a real moomoo-side limit.

**moomoo analyst-rating depth, confirmed 2026-07-10 (Path A)**: `get_research_rating_summary`'s per-analyst summary rows embed each analyst's own historical `rating_item_list` inline — DDOG alone has 20 analysts with 159 combined dated historical rating rows, plus moomoo-computed `success_rate`/`excess_return` per analyst. This is enough for module 4's per-analyst backtesting — see `apps/data-engine/samples/README.md` for the full findings and `apps/data-engine/samples/moomoo/` for the captured fixtures.

---

## 6. Database Schema Design

Five schemas: `raw`, `staging`, `mart`, `dagster` (Dagster's own run/event/schedule storage, explicitly configured, not left on default local storage), and `app` (consumer identity, immutable access policy/audit records, and owner-isolated private object locators; never factor computation or materialized analytics).

**Time axes (every staging table carries all three):**

- `valid_time` — the real-world period the data describes.
- `transaction_time` — when the fact became **publicly knowable** (the DTO layer calls this `knowable_at`). Always set explicitly by the writer from a source property (filing date, publish time) — **never defaulted to the insert clock**. The failure mode this kills: a new source added in year N backfills five years of history; stamped with `now()` those rows are invisible to every historical as-of query, stamped correctly they are usable while `recorded_at` still tells the truth about when we got them.
- `recorded_at` — when TrueAlpha ingested the row. Audit and reproducibility only; as-of resolution never reads it.

The checked-in migrations still use a transitional `unified_id`. Gate 0 does not freeze
that representation: #57 must distinguish issuer, security, and listing identities and
#58 must bind exact selected rows and every domain policy into a durable snapshot. The
target financial-fact shape below shows that semantic boundary; migration work must
preserve legacy IDs as versioned aliases rather than rewriting history.

```sql
-- RETIRED 2026-08-19 (see the note below). Kept as the record of what was intended,
-- not as a target. `staging.financial_facts` holds 0 rows in Production and its four
-- writers are statically unreachable.
create table staging.financial_facts (
    id                bigint generated always as identity primary key,
    subject_kind      text not null,          -- 'issuer' or 'security', registry-constrained
    subject_id        text not null,
    metric            text not null,          -- canonical field name; registry: libs/contracts metrics.py
    fiscal_period     text not null,          -- '2025Q4'
    valid_time        daterange not null,     -- the period this data describes
    transaction_time  timestamptz not null,   -- knowable-at; NO default (see "Time axes")
    recorded_at       timestamptz not null default now(),  -- ingestion clock, audit only
    value             numeric,
    unit              text not null,
    currency          char(3),
    confidence        numeric not null check (confidence between 0 and 1),
    source            text not null,
    source_metric     text not null,
    raw_ref           text not null,
    mapping_version   text not null,
    accession         text,
    form              text,
    is_restatement    boolean not null default false
);

create index idx_financial_facts_asof
    on staging.financial_facts
       (subject_kind, subject_id, metric, fiscal_period, transaction_time desc);
```

**`staging.financial_facts` is retired, and the reason is a layering error rather than a
delivery failure.** It holds 0 rows in Production, and all four of its writers
(`financial_facts_assets`, `sec_financial_facts`, `financial_facts_pipeline`,
`mvp_medium_repository`) are statically unreachable from the composition root. It never
acquired a writer because neither layer needed it where it sits:

- **The datahub layer optimises for completeness and confidence.** Completeness is
  row-level at OBLIGATION granularity (rule 15): a required `(scope, subject, domain,
  partition)` cell either captured or did not. `raw.capture_*` plus
  `staging.capture_normalized_observations` answer that for every cell, with per-cell
  confidence, `parser_version` and `mapping_version`. Exploding one payload into one row
  per metric adds nothing to "was the required cell captured".
- **What this table uniquely offered — any metric, across periods, addressable by name —
  is a FACTOR-layer need** (flexibility and traceability). But a factor may not read it:
  rule 3 forbids factors from seeing `source`, `raw_ref`, or accession, and half of these
  columns are exactly that. So the table sat in the datahub's schema solving the factor
  layer's problem, in a shape the factor layer is not allowed to consume.

The factor-input projection that does the job is `staging.strategy_backtest_inputs`
(issuer, cutoff, `input_key`, `fiscal_period`, value, confidence, `knowable_at`). Its lack
of a `source` column is REQUIRED by rule 3, not a gap; traceability is served by lineage
recorded outside factor computation (rule 23) in `staging.evidence_nodes` /
`evidence_edges`. Its remaining limit is that `input_key` is a `CHECK`-enumerated list
rather than validated against the metric registry, which is what makes adding a metric a
migration; closing that is what this table was reached for and is the actual work.

Identity still matters and is unchanged: #57 must distinguish issuer, security and listing
identities, migration work preserves legacy IDs as versioned aliases rather than rewriting
history, and the transitional `unified_id` is not a second valid identity model. Note the
open case: `mart.entity_display_resolution` currently resolves both
`issuer:cik:0000320193` and `issuer:lei:HWUPKR0MPOU8FGXBT394` to AAPL, because the QQQ and
TOPT universes mint issuer identities from different sources. Harmless while they run as
separate pipelines; a prerequisite before they are one universe.

**Source fusion (staging → snapshot → mart).** Staging is evidence, the durable
snapshot is the selected fact set, and mart is its materialized projection. Multiple
sources may assert the same `(subject_kind, subject_id, metric, fiscal_period)` and
coexist as separate rows. The winner is chosen by **declared rules, never by ingestion
recency**. The metric registry declares `source_priority`; the snapshot additionally
binds fusion, metric, identity, membership, extraction, price/FX, action, and every other
requested domain-selection policy version. The financial selection shape is:

1. Rows with `transaction_time <= :as_of` and a source registered for the metric compete; unregistered sources stay in staging as evidence and never reach mart.
2. The highest-priority source present wins.
3. Within the winning source, the latest `transaction_time` (restatement) wins; `id` breaks same-instant ties.
4. `confidence` rides along as data for the factor — it never arbitrates between sources (static per-source confidence must not silently decide truth).

**Where fusion is exercised (revised 2026-08-20).** The rule above outlived the table it
was written against, so it is restated on the planes that carry data. Fusion is not
homeless — it is UNEXERCISED, and the difference matters:

- **Evidence lives on the observation plane.** `staging.capture_normalized_observations`
  holds one row per `(obligation, semantic)` with its source, `parser_version`,
  `mapping_version` and confidence. Two sources asserting the same subject are two rows.
  That IS "every source's assertion side by side"; the granularity is the captured
  payload, not one row per metric.
- **Selection is exercised at snapshot freeze.** `staging.topt_core_snapshots` is the
  durable "selected fact set", and every selected observation ID plus policy version is
  persisted in its manifest before any factor runs.
- **What is missing is a second source, not a plane.** Every financial metric resolves
  from SEC alone today; Twelve Data is a second ORIGIN used for price reconciliation (a
  disagreement measure), not a fusion candidate. So `source_priority` has never had to
  choose. Registering a second source for an existing metric is what activates it, and by
  rule 22 that must change only source-owned code and registrations.

```sql
-- Selection at snapshot freeze: highest-priority source first, then restatement recency.
select distinct on (o.subject_id, o.semantic_type)  o.*
from staging.capture_normalized_observations o
where o.knowable_at <= :as_of_timestamp
  and array_position(:source_priority, o.source) is not null  -- registry order for the metric
order by o.subject_id, o.semantic_type,
         array_position(:source_priority, o.source),          -- fusion rank first
         o.knowable_at desc, o.observation_id desc;
```

This SQL is a financial-domain illustration, not the public snapshot API or a generic
"latest row" rule. Every selected staging ID and policy version is persisted in the
snapshot manifest before factor execution. Mart lineage points to that snapshot and its
exact selected records, which in turn chain through mapping/extraction IDs to `raw_ref`
and immutable bytes. Changing any selection policy creates a new snapshot/materialization;
old evidence and results remain addressable.

**Factor-input projection (`staging.strategy_backtest_inputs`).** The provenance-neutral
projection factors actually consume, and the only shape rule 3 permits them to see. Its
axes are `issuer x cutoff x metric x fiscal_period x vintage`:

- `input_key` names a metric. It MUST validate against the metric registry
  (`libs/contracts` `metrics.py`), never against an enumerated `CHECK` — see rule 22.
- `fiscal_period` is the period a value DESCRIBES, and is NULL for point-in-time
  observations (price, share count, headcount) which describe an instant. A metric may
  appear once per period per cutoff; that axis is what lets a multi-period factor live in
  `libs/factors` instead of being pre-reduced in the capture layer.
- **The period tag is a declared format, not a convention:**
  `<filing fiscal year>:<kind>:<start ISO date>:<end ISO date>`, e.g.
  `FY2025:FY:2025-01-01:2025-12-31`. The leading fiscal year is the FILING's, never the
  period's — a filing stamps its comparatives with its own tag. Consumers key on the END
  DATE. Writer and reader must share one encoder/parser; a format known only by a producer
  and a regex fails silently, as an empty series rather than an error.
- `vintage` is `recorded_at`-ordered supersession: a restatement appends and wins by
  recency; history is never overwritten.
- **There is deliberately NO `source` column.** Rule 3 forbids factors seeing source,
  `raw_ref` or accession. Traceability is served by lineage recorded OUTSIDE factor
  computation (rule 23): `staging.evidence_nodes` / `evidence_edges`.

**Knowledge graph (replaces the old flat `symbol_mapping` table).** Entity resolution is not unique to companies — companies, ETFs, analysts, and supply-chain nodes all have the same "same real-world thing, different IDs per source" problem, so it's modeled once as a graph, implemented as plain Postgres tables (no separate graph engine — the query patterns needed here are shallow joins, not deep multi-hop traversal):

```sql
create table staging.kg_entities (
    id            text primary key,
    entity_type   text not null,             -- issuer | security | listing | analyst |
                                               -- universe | theme | supply_chain_node
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
    recorded_at       timestamptz not null default now(),
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
    transaction_time  timestamptz not null,   -- knowable-at; NO default (see "Time axes")
    recorded_at       timestamptz not null default now(),
    confidence        numeric not null,       -- edge evidence confidence; scenario propagation
                                               -- enforces a versioned minimum but cannot infer causality
    source            text not null,
    raw_ref           text not null
);

create index idx_kg_edges_asof
    on staging.kg_edges (from_id, relation_type, transaction_time desc);
```

Mart's flattened 2D tables (e.g. a company-to-ticker lookup, or a supply-chain adjacency table for a given company) are SQL views/materializations over `kg_entities` + `kg_edges`, not separately maintained.

Role permissions (`roles.sql`):

```sql
alter role mart_readonly set statement_timeout = '5s';
grant select on schema mart to mart_readonly;
-- typed mart repositories additionally enforce cursor pagination and bounded row counts;
-- MCP/chat never accept arbitrary model-generated SQL
```

Other tables: `api_call_ledger` (the external call ledger for every source and the model provider — rule 6; moomoo was its first tenant), `ingestion_health_log` (only business-specific metrics the Dagster UI doesn't already cover).

**Data-accountability projections.** Generic runner and strategy materialization write
idempotent input-to-output and output-to-decision lineage. `mart.data_usage_frequency`
joins those edges with capture/snapshot/consumption records and the declared requirement
graph; `mart.strategy_data_quality_review` starts from every expected requirement cell
and left-joins the actual evidence. Neither projection may infer non-applicability from
zero use, and neither becomes a second computation path for factors.

**Governed research access.** Trusted browser, delegated-MCP OAuth, administrator,
and service middleware construct a server-derived `AccessContext`; clients never
supply tenant, principal, role, entitlement, or publication-policy authority. Every
private-content or materialized-result request is authorized before mart SQL, private
row lookup, or artifact retrieval. Private conversations and documents are tenant- and
owner-bound, administrators receive non-content audit metadata by default, and
materialized strategy/backtest results require an immutable release-bound publication
policy set and a matching active entitlement grant unless an explicit administrator
rule applies. Missing, invalid, not-yet-valid, expired, revoked, forged, wrong-policy,
or cross-tenant authority fails closed and emits a paired append-only decision/audit
record. Identity and
access metadata never enters factor inputs, `BacktestDataGateway`, `DecisionSnapshot`,
`ReplayEventStream`, or Qlib types. This contract does not activate authentication,
routes, identity-provider bindings, retention policy, replay execution, or sharing.

---

## 7. The Seven Analytics Modules

**The module number is an identity, and `libs/factors`' `@factor(..., module=N)` must match this list.** Two registrations do not, and are recorded here so the correction is deliberate rather than silent: `price_to_sales` registers `module=6` (module 6 is pure-blood screening; P/S is not one of the seven -- it is an input to module 7), and `registered_semantic_probe` registers `kind="base", module=7` (module 7 is the composite). Correcting either changes a published identity, so each is its own versioned change.

Modules 1-6 are **base factors** (Section 4, `libs/factors/base`) — the runner projects provenance-neutral snapshot inputs for them. Module 7 is a **composite factor** (`libs/factors/composite`) — it reloads other modules' materialized mart outputs, and its confidence cannot exceed the minimum confidence consumed; a declared versioned policy may be stricter.

1. **PEG**: switchable growth-rate conventions
2. **Gross profit per employee**: operating and financial components computed for every issuer and merged into one wide row (rule 17, #59 round 2; v0.2.0 still publishes one uniform capital-adjusted number, #528), headcount gaps explicitly flagged rather than silently dropped
3. **Supply-chain relationship graph + confidence-gated scenario exposure**: graph first (KG `supplies_to` edges); path propagation must declare a versioned shock/exposure scenario, direction, materiality/sensitivity, and confidence kill condition. It may be described as causal only after independent causal evidence, not merely because an edge is high-confidence.
4. **Analyst backtesting**: moomoo historical rating depth is confirmed, but only events with independently defensible public availability may enter PIT scoring; backfilled rows remain unavailable before that time
5. **ETF virtual company**: SEC N-PORT-P is the confirmed holdings-weight source; calculations must respect report/filing lag, fund-series identity, instrument type, unresolved weight, currency, and period alignment
6. **Pure-blood company screening**: LLM-assisted semantic classification of segment revenue
7. **Three-tier valuation tagging** (composite): sorts a company into the traditional / tech / large-model-native P/S tier (Vision, "large-model-driven company" framework) by reading module 2's gross-profit-per-employee output (and other base factors as needed) — this is a screening/interpretation layer expressed as a factor, not app-layer logic (Section 1, rule 2)

---

## 8. Release Gates and Verification Criteria

| Gate | Goal | Verification | Infrastructure scope |
|---|---|---|---|
| Gate 0: Semantic & Data Closure | Freeze research semantics, issuer/security/listing and currency/time/return rules, executable snapshot/extraction/invocation/lineage contracts, source/type registries, usage/reverse-review contracts, longitudinal source rights, and module coverage/freshness SLOs | Contract examples and fixture/Postgres conformance pass; additive source/type probes require no generic dispatch change; every required field has a viable longitudinal source and every module has an independent oracle and usable-coverage denominator | Local/CI only; no interface is called v1-frozen before this gate |
| Gate 1: Core Strategy MVP | Gross profit per employee, financial branch, three-tier valuation, `large_model_value_v0`, deterministic Qlib replay, mart/report, and real Staging canary | Independent core oracle passes; the pinned Qlib/adapter path reproduces golden decisions with required usable coverage and no look-ahead; Staging proves the same artifact operates idempotently | Dagster enters with the first executable snapshot/factor slice; persistent Staging by gate end |
| Gate 2: Seven Research Modules | Add all PEG conventions, analyst track records, ETF virtual company, supply-chain scenario exposure, and pure-blood screening through the shared PIT path | Every module passes a sealed independent holdout and one shared seven-module replay; required subjects cannot pass as `unavailable` | Dagster assets and module-specific longitudinal data planes in Local/CI/Staging |
| Gate 3: Research Consumption | Stable mart reads, usage/review reads, MCP, reports/cards, App, and deferred `/chat` | Canonical Vision questions agree across the deployed consumers (App, MCP, reports/cards) reading the same governed materialized outputs — fixture conformance suites remain CI-only evidence and cannot close a consumption issue; report/card facts come only from materialized outputs; bounded usage and reverse-review reads retain exact trace identities | App reads mart directly; MCP/chat use typed read tools; no raw/staging consumer access |
| Gate 4: Production Strategy Validation | Multi-regime data/strategy checks, reverse data-quality review, all-module Staging soak, recovery/exact-release promotion, deployed Production consumers, curated-universe expansion, and shadow graduation | Known-reference sanity, independent price/data reconciliation, usage/reverse-review reconciliation, SLO soak over natural source changes, backup/restore, real-client questions, and final Vision audit all pass | Isolated Production remains shadow while #67 freezes the candidate and #68 certifies capture; only #54 records authoritative graduation |

**Gate acceptance and issue ownership (fail closed):**

- **Gate 0 — #56 (#57-#61):** `UniverseRef`, `CaptureScope`, `CaptureManifest`, source/type registry snapshots, applicability-independent `SourceCapabilityCatalog`, source-neutral data requirements, requirement graph/schedule demand compilation, automatic run/module/emitter usage, reverse quality review, catalog, applicability, source-rights/budget, holdout-custody, and natural-refresh contracts are versioned and executable. #60's capability inventory cannot depend on #61 applicability; #61 projects it onto the exact denominator. The approved catalog cannot be smaller than the product-owner-approved sensor/core scope, and an expired or unresolved source decision keeps Gate 0 open.
- **Gate 1 — #29 (#14, #21-#27, #70-#71):** #70 owns only the PIT document-to-headcount data path needed by the Core Strategy. After #24/#25 freeze candidate implementations, #71 runs the blind financial/non-financial GPPE, P/S, tier, and valuation-gap holdout before #26/#27 can close. #26 owns the version-pinned Qlib adapter and replay evidence; replay still uses unadjusted bars and explicit actions under the TrueAlpha clock. #27 proves that exact artifact on the immutable TOPT Core canary; #51 later owns all-module Staging soak, and a two-run canary is not continuous operation.
- **Gate 2 — #30 (#33-#40, #62-#65):** all catalog-required module invocations have longitudinal inputs, meet predeclared per-required-subject coverage, and pass #65 under the custody rule above. Unavailable or low-confidence placeholders and a seen/public holdout cannot satisfy this gate.
- **Gate 3 — #31 (#41-#46, #48, #72, plus registered additions #229/#235/#236/#251):** the deployed consumers agree on typed materialized outputs and bounded usage/review reads from the same governed head; fixture conformance suites are CI-only evidence and do not close a consumption issue (AGENTS.md rule 6, audit #429). #72 separately proves configuration-only onboarding for an unseen issuer/theme and additive onboarding for one test source plus one semantic type; the latter may add isolated extension code and one probe factor, but cannot change central dispatch, generic Dagster/snapshot/lineage/usage/review code, existing factors, or consumers.
- **Gate 4 — #32 (#11, #49-#54, #66-#68):** the exact signed release manifest and full approved `UniverseRef` pass natural-refresh soak, anti-shrink SLOs, usage and reverse-quality reconciliation, mandatory Production authentication, full-scope capacity tests, database and raw-object recovery within RPO/RTO, real-client equivalence, and human card review. #67 produces the full-universe Production shadow candidate; #68 independently certifies its row-complete capture; #54 then verifies the complete holdout, scope, rights, recovery, consumer, capture, usage, and quality bundle and records authoritative graduation. Seeded bad evidence must make `vision-audit` fail.

**Availability and validation are separate**. Every factor output carries an
`availability_status` such as `available`, `unavailable`, `stale`, `excluded`,
`low_confidence`, or `error`, plus a `source_evidence_status` for the selected semantic
records and a separate `factor_validation_status` for the factor version's independent
golden/holdout gate. Only applicable, available, fresh outputs from an accepted factor
version count toward the module's versioned usable-coverage SLO. Consumers display all
three dimensions rather than conflating source evidence with formula validation.

Environment and evidence scale are part of the gate contract. Local/CI exist
throughout; Dagster is introduced early in Gate 1 and is the only authority for real
scheduled runs; Production remains isolated shadow output until Gate 4 data, strategy,
consumer, recovery, soak, capture-audit, and human-graduation evidence all pass.

Release gates define claims and promotion order; implementation is conventional issue→PR
work (see `AGENTS.md`). Grow evidence incrementally — prove a slice on a tiny fixed
corpus before scaling it — and merge verified PRs independently into `main`; a merge
never promotes an environment or implies gate completion. Disjoint capture, platform,
strategy, consumption, and verification lanes work in parallel against exact
content-hashed handoffs; coordinate through issues before touching shared surfaces
(types, exports, registries, migration numbering, generated contracts, lockfiles,
authoritative architecture documents).

Dependencies state whether they block provisional implementation, candidate freeze, or
issue/gate closure. Downstream fixture/local development may begin after its required
contract-repair handoff even if rights, holdout, soak, or natural-refresh evidence remains
pending. Its
declared readiness ceiling cannot exceed its evidence: fixture tests prove contracts,
development goldens prove candidates, sealed holdouts prove modules, Staging canaries
prove bounded operation, and only natural-refresh plus independent Production evidence
can prove graduation. Formula/source/applicability/SLO semantics and exact candidate
hashes freeze before protected evaluation; post-reveal changes require a new version and
fresh untouched evidence. PIT rules, append-only restatements, fixed denominators,
environment-scoped rights/budgets, row-complete capture, recovery, and human approvals
remain mandatory at their applicable evidence scale.

The gate rows above are claims; they are checked, not closed in order. The gate epics
(#56, #29, #30, #31, #32) remain the registry of what each gate claims, but since
2026-07-17 (`governance/README.md`) delivery is not sequenced through them: each claim is
proven or refuted by a standing check on the axis roots (#434 factor chain, #530 datahub
completeness, #284 factor flexibility, #544 backtest reproducibility, #70 extraction,
#581 production invariants, #712 release identity) under `AGENTS.md` rules 6 and 7, and
a gate epic closes by hand when every claim in its row has such a check. Evidence
(captured corpora, evaluation records, handoff documents) is content-hashed under
`governance/` for replayability. Graduation additionally requires the independent capture audit, the final
Vision audit, and recorded human approval. Day-to-day delivery is conventional: one issue,
one pull request, tests and review before merge, as defined in `AGENTS.md`. The
capability dependency graph under `governance/capabilities/` is planning information, not
merge enforcement.

---

## 9. Known Risks / Pitfalls

- SEC XBRL tags are inconsistent across industries — don't assume field names/units are uniform
- yfinance has no SLA — can't be the sole dependency on a critical path
- Arbitrary SQL touching raw/staging carries silent look-ahead risk; roles deny it and MCP/chat expose typed mart reads only
- dlt schema evolution must use frozen mode for core tables
- **Dagster's `code_version`/`data_version` assumes deterministic inputs.** LLM extraction is therefore a separate, versioned, append-only step. Its invocation binds model, instructions, schema, and decoding settings; the stored semantic result and evidence spans determine downstream `data_version`. Replay reuses that stored result and never silently calls the model again. A new extraction is a new invocation/vintage, not sampling noise hidden behind the old ID.
- **LLM self-reported confidence is not ground truth.** It may be retained as one signal, but Gate 0 freezes the confidence policy and Gate 2's sealed holdout measures calibration. `low_confidence` remains distinct from evidence verification and cannot count toward usable coverage. More expensive self-consistency or review paths are introduced only through a new versioned policy backed by that evidence.
- Discriminated unions only reliably generate `oneOf` when declared at the top level with an explicit discriminator; nested cases are a known open issue — handle if/when encountered

---

## 10. Known Architectural Debt (deliberately not solved now, but written down)

- **Multi-user moomoo quota allocation**: not relevant while single-user; must be designed before going multi-user, no pre-modeling now
- **Enforcement of the App-side "deterministic reformatting" boundary**: Section 1 states the rule but there's no code-level check (e.g., a lint rule preventing cross-table aggregation in the Next.js backend) — could add a CI check later, not now
- **Consumer page/query analytics**: V1 measures semantic pipeline and strategy consumption only. Add read-frequency telemetry later only with a separate append-only audit writer that preserves the App's `mart_readonly` role and cannot recursively count usage queries.
- If Postgres concurrency becomes a real bottleneck, re-evaluate read/write splitting or a caching layer

---

## 11. Current Baseline and Next Gate

**Baseline as of 2026-09-04, measured on the deployed environments rather than on tracker
state.** The reconnaissance baseline this section used to describe (monorepo, four schemas,
registry skeleton, samples, first corpus audit) is history; what exists now is a running
system with named gaps.

What runs daily on the Dagster schedule in both Staging and Production:

- real-source capture for the TOPT 20, the QQQ 101 and a 5-issuer canary universe (SEC
  company-facts, Twelve Data, yfinance corroboration, N-PORT holdings), immutable raw bytes in
  object storage with `raw.fetches` pointers, row-level capture control and a quality report
  per run;
- module 2 (GPPE, v0.2.0 uniform), module 7 (three-tier valuation), P/S, module 1 (PEG,
  historical convention only) and the `large_model_value_v0` strategy, materialized into
  `mart.topt_core_results` and `mart.strategy_*` behind a governed `current_pointer` that
  advances with each accepted tick;
- the App (`/research/*`, `/admin/*`, behind login), the MCP endpoint, and a `/chat` v0 that
  answers one question class deterministically — all reading `mart` only.

What is verified and what is not, with the owning issue:

| claim | state on 2026-09-04 | owner |
|---|---|---|
| the chain is non-empty and scheduled | TOPT 17/20 scored daily; PEG 14/20; pointer advances each tick | #434 (root) |
| the published numbers are possible | JPM GPPE is negative under the uniform charge; no invariant has run against Production | #528, #581, #544 |
| the denominator is real | every headcount is a manual seed; the extraction primitive is a stub; QQQ is 12/101 available | #70 (root) |
| the vintage is a measurement | `valid_from` / `freshness_state` are near-constant | #530 |
| every source is admitted (rule 19) | all attestations `missing` | #60 |
| one release promotes all three images (§3.1) | data-engine is promoted from `main` by a separate manual dispatch | #712, infra2#622 |
| every external call is gated (rule 6) | only moomoo goes through `api_call_ledger`; the model provider has no seat | #729 |
| non-local endpoints are authenticated (rule 21) | MCP answers without a credential | #728 |
| Production is graduated | no: #54 has not run; restore drill (#650) and sealed holdouts (#65) outstanding | #54 |

**Next gate.** Not "Gate 0": the semantic and contract closure it named is in force in code
(registries, PIT contracts, capture control, the frozen factor contract), and the
batch/lease machine that would have closed it formally was retired on 2026-07-17. The next
gate is the one whose claims are false in the table: make the numbers right and provably
so (#434's standing criteria), fill the prose-only inputs through the gated extraction
source (#70), admit the sources (#60). Gate epics #56 and #29 close by hand when their rows
have standing checks; #54 remains the only path to calling Production output authoritative.

Provisional lower-gate work is ordinary issue→PR work; it remains excluded from the
accepted `ReleaseManifest` registry/configuration bindings and cannot close a higher gate.
Sealed holdouts under #71's protocol (#65), additive registry/catalog proof (#72), and the
independent Production capture audit (#68) keep their meaning: they are what separates
"serving" from "graduated".
