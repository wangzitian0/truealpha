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

### 0.1 Why this exists

This isn't about building a flashy data platform — it's about engineering an already-developed investment framework into something that can be verified repeatably and kept current, without re-pulling data, rebuilding tables, and re-checking conventions by hand every time. The tech stack will keep changing (it already has, multiple times, over the course of this design process); this document won't — it records the part that doesn't move with the tech stack.

### 0.2 Core investment framework (this is what determines which factors matter — none of this is arbitrary)

- **The "large-model-driven company" three-tier framework**: whether a company needs to add headcount proportionally to handle a new category of decisions. Companies are sorted into three tiers, each mapping to a different P/S range. Traditional 3-4x, tech 8-10x, and large-model-native 20-30x are the framework's current illustrative anchors; the executable v1 bands and boundary semantics must be independently reviewed and versioned before use. The owner reviewed them on 2026-10-12 (#1235): strategy definition 1.0.0 values by P/GP = market value / gross profit, with bands equal to these P/S anchors divided by a reference gross margin. The values live in one definition file; the review record is `governance/reviews/large_model_value_v0-definition-1.0.0.md`. This is a genuine computed output (Section 7, module 7), derived from gross profit per employee — not just a mental model for reading that number
- **Gross profit per employee as the core operating metric**: top large-model-native companies can reach roughly $5M in annual gross profit per employee, versus roughly $1M for the prior generation's leaders. This metric captures whether "AI leverage" is actually happening better than revenue growth alone does, and gross profit is defined differently for financial vs. non-financial companies — these need separate handling
- **PEG needs multiple growth-rate conventions**: analyst consensus, historical CAGR, and company guidance can each tell a different story depending on context. The tool needs to support switching between them, not commit to a single convention
- **Supply chain / industry value chain perspective**: upstream-downstream relationships are themselves a source of alpha, not just a side note to financial comparisons
- **Analyst backtesting**: the goal is to verify whose historical judgment has actually been worth trusting, rather than passively accepting consensus
- **ETF-as-virtual-company**: treat a basket of holdings as if it were a single company and look at its fundamentals, to judge whether a given theme or portfolio has real substance behind the packaging
- **"Pure-blood" company screening**: given a theme, find whose revenue exposure is purest — for expressing a specific view precisely, instead of buying a basket of noise

### 0.3 Use cases

- **Personal investment research**: when analyzing a new company or theme, historical factors should be reusable and traceable, without rebuilding models by hand each time
- **Content creation**: the AIGC short-form video channel needs material in a "cold verdict" register — the factors, rankings, and reports this tool produces are one source of that raw material, and ultimately feed into Xiaohongshu card decks

### 0.4 What this project is not

- Not a high-frequency / intraday trading system — the inherent disclosure lag in these data sources (e.g., a ~60-day public lag on ETF holdings) is acceptable
- Not currently a multi-user SaaS product — this is a personal tool; the architecture leaves room to grow but doesn't add complexity for that now
- Not aiming to cover every stock. Production runs TOPT and QQQ first and the S&P 500 next (owner decisions 2026-10-20 and 2026-10-21, #1410; governed by `common/contracts/src/truealpha_contracts/data/environment_contract.v1.json`, #1416). "Depth over breadth" means that each company in that universe gets all seven modules with traceable history before any name outside it is added

### 0.5 What success looks like

The six success statements below keep their numbers. Other documents cite them by number.

1. Continuously producing results from all seven factor modules for the production universe, the S&P 500, with history that's traceable and conventions that are switchable
2. Being able to ask, in conversation, "what's this company's PEG trend" or "who's the purest play on this theme," and get an answer backed by data, traceable to a specific filing/vintage — not a number the model made up on the spot
3. Factor/ranking outputs converting directly into Xiaohongshu card material without a manual processing step in between
4. Adding a previously unseen company or theme through reviewed, versioned catalog/configuration changes, without changing factor formulas, schemas, or consumer business logic
5. Adding a source for an existing semantic type through one isolated adapter/normalizer registration, and adding a semantic record type through one typed schema/storage/projector registration, without source/type branches in the generic capture, snapshot, factor-runner, lineage, usage, or consumer path
6. *Demoted (owner decision 2026-10-20, #1410; rules 23 and 24):* querying how often declared data is captured and actually consumed, and running an immutable strategy-to-source data-quality review that exposes required-but-missing inputs and the decisions they affected. Restore this item when the core path is green in production for two nights in a row, the exit condition of `governance/freezes/core-path-2026-10-12.md`

"Continuously producing" means the versioned production universe has graduated
beyond canary/shadow operation and meets explicit per-module applicability, usable
coverage, freshness, and traceability objectives across real source refreshes. An
`unavailable`, stale, unresolved, or sentinel placeholder does not count as a
produced result for an applicable subject. A computed value with a low confidence score
counts, and its score is shown beside it (owner decision 2026-10-15, #1333). Success also requires the deployed MCP/App/
chat and report/card paths to consume the same materialized outputs; fixture-only tools,
two immediate scheduled runs, or code existence are not completion evidence.

This success state proves a reproducible research product and an honest evaluation path.
It does not by itself prove that any factor, tier, screen, or strategy produces positive
alpha; that requires a separate empirical claim and evidence.

### 0.6 Capabilities and where they live

Delivery state lives on the issues and on the dashboards (`/admin/datahub`), not in this file.

| Capability / Factor Module | Architecture Reference | Target Scope |
|---|---|---|
| **Module 1: PEG (Switchable Conventions)** | `factors.base.peg` | Historical CAGR, Consensus, Guidance |
| **Module 2: Gross Profit per Employee** | `factors.base.gross_profit_per_employee` | Operating AI leverage |
| **Module 3: Supply Chain / Value Chain** | `factors.base.supply_chain_exposure` | Upstream-downstream alpha |
| **Module 4: Analyst Backtesting** | `factors.base.analyst_track_record` | Historical analyst accuracy |
| **Module 5: ETF-as-Virtual-Company** | `factors.base.etf_virtual_company` | Holdings fundamentals aggregation |
| **Module 6: Pure-Blood Company Screening** | `factors.base.theme_purity` | Segment revenue exposure purity |
| **Module 7: Three-Tier Valuation Gap** | `factors.composite.three_tier_valuation` | P/GP tier gap against GPPE (definition 1.0.0, #1235) |
| **Xiaohongshu Card Deck Generation** | `truealpha_contracts.research_cards` | Automated material export |
| **Conversational Research Q&A** | `apps/llm-service` MCP | Natural language fact retrieval |

---

## 1. Core Design Principles (hard constraints, not to be violated)

1. **Point-in-time correctness is priority one.** Distinguish `valid_time` (the period the data describes) from `transaction_time` (when the data became knowable). A restatement always produces a new vintage — never overwrite an old record. Factor computation and backtesting always operate on "the data that was visible as of a specific point in time."
2. **Computation logic is allowed to exist in exactly one place**, implemented in `common/factors` and invoked by data-engine/Dagster. `llm-service` and the App consume materialized outputs through typed mart read contracts; they never invoke factor computation. The App layer is only allowed to do **deterministic reformatting**: sorting, filtering, pagination, unit conversion, simple within-row arithmetic that doesn't span tables (e.g., "change vs. prior period"). Any logic that jointly computes a new metric across factors or time points must go back into `common/factors` and be materialized into mart — it must not be computed on the fly in the Next.js backend. Use this concrete rule wherever the boundary feels blurry. `tools/check_app_layer_aggregation.py` enforces it in `ci-web`: `sum(`, `avg(` or `over (partition` in `apps/app-web/src/server/mart/*.ts` fails CI outside a named allow-list. **This includes screening/tagging logic** (e.g., the three-tier valuation framework, Section 7 module 7) — it reads other factors' mart outputs, but it is still a factor (a "composite factor," Section 6), not app-layer business logic.
3. **Factors never know their data's provenance.** Factors consume provenance-neutral typed semantic records: an opaque `input_id`, typed subject identity, value/unit/currency where relevant, valid period, confidence, and the snapshot cutoff. They must not receive or branch on `source`, `raw_ref`, accession, or extractor metadata. The runner uses the opaque IDs to record exact consumed-input lineage outside factor computation.
4. **LLM surfaces read only authorized typed `mart` projections.** MCP and `/chat` call bounded `ResearchQueryService` methods over a `mart_readonly` repository; they never execute arbitrary model-generated SQL or recompute factors. A trusted server adapter derives `AccessContext` from the browser session, delegated MCP OAuth credential, or service identity and authorizes the exact resource before the repository issues mart SQL or retrieves an artifact. Tool arguments never supply tenant, principal, role, entitlement, or publication authority. The database role has a `statement_timeout` (5s recommended), and repositories enforce pagination and row caps. The App implements the same semantic read contract and authorization decision directly against mart.
5. **The App reads the database directly** (querying the mart and app schemas through server-only typed repositories), not through FastAPI. FastAPI's scope is narrowed to LLM-call orchestration only. Direct database access does not bypass authorization: publication policy is checked before a mart query, and forced owner RLS is a second boundary for private app rows.
6. **Every external call — data vendors and model providers alike — goes through the global source gateway with a declared capacity** — no module decides for itself whether to call. Each source declares its capacity (rate window, daily/monthly budget, concurrency) once, per ledger seat, in the registry (`source_registrations.LEDGER_CAPACITIES`; the gateway derives its view from it), every call is recorded in `api_call_ledger`, and a call that would exceed the declared capacity is refused or queued rather than fired: capacity is part of the capture logic, not an afterthought inside one adapter. A credential that serves several environments (the Twelve Data key) declares each environment's share of its allowance, and each environment enforces its share from its own ledger, since environments never share one (§3.1). Exhaustion is a named outcome (`deferred_capacity` on the cell, `capacity refused` in the tick summary), never "the vendor had no data". This applies to SEC, Twelve Data, yfinance, OpenFIGI, N-PORT, moomoo and the LLM extraction provider alike (owner decision 2026-09-04). End-user access only reads completed materialized results and never triggers a source or model call, so user entitlements do not allocate or bypass source quota. Pipeline-wide source budgeting and scheduling remain separate from consumer authorization; any future user-funded source-demand product requires a new architecture decision. **This is a public repo: moomoo trading contexts/order calls are hard-forbidden (quote/read-only data only) and no real credential ever gets committed — see `CLAUDE.md`'s hard constraints and `.github/workflows/security-gate.yml`.**
7. **Schema changes must raise an active alert.** Core point-in-time tables use dlt's frozen/contract mode, not the default auto-evolve.
8. **Scheduling has exactly one authority: Dagster.** Local reconnaissance may use one-shot scripts, but Dagster is introduced with the first executable Gate 1 snapshot/factor slice and is the only authority for real recurring runs — see Section 8.
9. **Observability is centered on the Dagster UI** — on the condition that Dagster's own runtime metadata is persisted (see Section 6), not left on its default local storage.
10. **Supply-chain propagation is scenario analysis unless causality is independently established.** Every propagation run declares a versioned shock, direction, materiality/sensitivity rule, horizon, and minimum edge confidence. Low-confidence paths are killed, but high confidence in a disclosed relationship does not by itself prove a causal effect.
11. **No moon.** GitHub Actions with path filtering is sufficient.
12. **Multi-source truth is resolved by declared fusion rules, never by ingestion recency.** Staging keeps every source's assertion side by side; the mart winner comes from the metric registry's per-field `source_priority` (Section 6, "Source fusion"), and the ruleset version is part of mart lineage. Corollary: `transaction_time` (= knowable-at) is always written explicitly from a source property, never defaulted to the insert clock, and every staging row also carries `recorded_at` (ingestion clock, audit only) plus — for parsed facts — a `mapping_version`, so "the data changed" and "the cleaning logic changed" stay distinguishable forever.
13. **Scope is immutable and explicit.** `UniverseRef` contains the universe ID, version, and membership or resolver hash. Every capture, snapshot, factor invocation, screen, strategy, report, SLO evaluation, and audit binds that exact reference plus `as_of` and `valid_on` where applicable. Research Catalog, applicability, capture, and SLO identities remain separate immutable contracts and are bound together by the release manifest. A mutable "current universe" or an unversioned ticker list is never a valid execution scope.
14. **Applicability is frozen before execution and scope cannot shrink to obtain green status.** Required/optional/not-applicable cells are approved before a run. Removing a subject, invocation, theme, scenario, or required cell creates a new catalog/SLO version, records product-owner approval, and narrows the claim; it cannot retroactively make a failed gate pass. The supported workflow must also onboard an issuer and theme that were absent from development evidence without changing factor or consumer code.
15. **Capture completeness is row-level, not job-level.** Each capture run records every required `(scope, subject or instrument, domain, partition or vintage)` cell as a capture obligation. `staging.capture_observation_obligations` binds each obligation to the observation that satisfies it. That observation carries the raw capture, normalized record, confidence, times, mapping/policy versions, and lineage. `mart.topt_capture_status` gives the obligation, success, unchanged, unavailable, skipped and failed counts and a `complete` flag for each run. Missing required cells fail even when Dagster is green or raw payload counts look plausible.
16. **V1 total return uses unadjusted bars plus explicit corporate-action lifecycle events.** The simulator applies declaration/knowability, ex, effective, record, and pay semantics exactly once through a monotonic event clock. Adjusted-close data may be retained for reconciliation but cannot be combined with separately applied splits or dividends. A6 allows one bounded `preview` deviation (split-adjusted returns, no events) until #1121 lands.
17. **Labor efficiency is a named metric in the metric forest, and the forest keeps diversity** (owner decisions 2026-09-17, 2026-10-09 #1176, 2026-10-12 #1235; `docs/metric-forest.md`). Two metrics are live. `gppe_uniform_charge_v0` uses total assets as the capital base for every issuer. `gppe_banking_tce_v1` uses measured tangible common equity and applies to financial issuers only. Neither has a ratio test or a silent fallback. A new formula is a new node with a new key, not a branch in an old one. The operating component and the financial component of one issuer stay a target tree of the forest (#528). A negative value is a fact. The versioned definition records whether it is a valid low signal, and the output invariants assert exactly that.
18. **Evidence quality is a continuous score, and dashboards carry the verdict** (owner decision 2026-10-15, #1333; ADR A8). A holdout, a golden or an independent check raises the score of a metric. Its absence never blocks a row. Custodian labels and external evaluators are optional evidence. Changed logic gets a new version.
19. **Every source has a recorded prior and a recorded budget.** The source registry holds each source's prior reliability, declared capacity and known limits (rights, retention, SLA). The ledger records every call. An over-budget projection defers the call with a named reason (`deferred_capacity`). A deferral does not stop the source from serving rows. No source-rights dashboard flag exists: this is a single-user project (owner decision 2026-10-20, #1410).
20. **Continuous operation is read from natural refreshes on a dashboard.** Each source class declares cadence and maximum age. The served-freshness view (`mart.served_head`) shows age against that limit. Retries, reparsing unchanged bytes and replayed fixtures do not count as a refresh. A stale head is served with its age and a reason code. It is withheld at the `withhold` limit.
21. **Production consumers need TLS and authentication.** Every non-local MCP, App, `/chat`, report and artifact endpoint requires both. Database and raw-object backups exist and run on a schedule. The owner approves each production release (AGENTS.md). Dashboards show restore age and capacity. No separate drill, custodian or reviewer signature gates a release.
22. **Extension is registry-driven and deliberately static.** Content-hashed `SourceRegistry` and `SemanticTypeRegistry` snapshots are authenticated by the signed release manifest. They resolve source adapters/normalizers and typed normalized models/factor-input projectors/repositories. Adding a source for an existing semantic type changes only source-owned code, registrations, policies, and tests. Adding a record type inside an existing domain changes only its typed model, storage/migration, projector, registration, and tests. Generic capture, manifest, snapshot, Dagster composition, factor execution, lineage, usage, and review code must not branch on source or record type. A new domain or new business meaning remains an explicit contract/factor/catalog change. V1 uses checked-in registrations at process start, not dynamic plugin discovery, runtime code loading, an event bus, or arbitrary JSON facts. **Concretely for metrics: adding one is a registry edit, never a migration and never an edit to generic transport code (see `docs/architecture-decisions/A7-standard-to-wide-row-loop.md`).**
23. **Data usage is automatic, idempotent infrastructure evidence.** *Demoted (owner decision 2026-10-20, #1410): no deployed code builds the usage views. Restore this rule when the core path is green in production for two nights in a row, the exit condition of `governance/freezes/core-path-2026-10-12.md`.* Capture manifests, repositories, snapshot selection, and factor/strategy runners record append-only semantic-use identities outside factor code. Usage views distinguish planned demand, capture/normalization evidence, snapshot selection, factor consumption, and strategy consumption. Retries do not double-count one semantic use, missing telemetry is an error rather than zero use, and source attribution is recovered through lineage rather than exposed to a factor. V1 deliberately excludes page-view/query analytics so the App remains strictly `mart_readonly`.
24. **Strategy quality review starts from expected data, not successful outputs.** *Demoted with rule 23 (owner decision 2026-10-20, #1410): no deployed code builds the review. The restore trigger of rule 23 applies.* The framework compiles source-neutral `DataRequirement` records from the immutable strategy, factor, and execution/return-rule graph for every scheduled cutoff and applicable subject, then left-joins actual capture, snapshot, consumption, lineage, rights, freshness, and quality evidence. Missing data that suppresses a candidate or produces no trade remains a failed expected cell with affected decisions; undeclared consumption or broken reverse lineage also fails. Observed low usage can prioritize remediation but can never relax applicability, retention, source policy, or an SLO retroactively.
25. **Native Decimal implementations in `common/factors` are the computation. No engine is a data or semantic authority.** Each base factor computes its value in Decimal Python. That value is the source of truth for monetary results, and it is the value that reaches `mart`. Serializable TrueAlpha definitions are versioned `truealpha_contracts.ast` expressions. The Polars AST compiler is a test-time reproducibility oracle in `common/factors/tests/support/expression_oracle`. Tests compile each expression through it and check that it reproduces the Decimal value. No deployed path imports it. A5 selected VectorBT as the backtest engine, but the repository never adopted it. The backtest engine is `factors.backtest.engine` (#758). It is a Decimal simulation that reads its execution, transaction-cost, total-return and sizing rules from the strategy definition. Only code inside `common/factors` computes factors or simulates portfolios. Its inputs are provenance-neutral records projected from durable PIT snapshots and the stored price series. No engine or oracle may crawl or select vintages, resolve membership, infer confidence or lineage, combine adjusted prices with explicit corporate actions in one return, or replace Decimal monetary logic. `ExpressionEngineExecutionBinding` pins one exact factor-expression runtime — `distribution="polars"`, version, release commit, runtime artifact and lock digests — plus its adapter. `StrategyEngineBinding` carries that binding together with the strategy identity. A backtest run pins its engine by `ENGINE_ID` in its `run_id`, together with the definition hash, the input snapshot and the price snapshot. No binding pins the operator set; A5 records why the Qlib operator-registry digest was dropped rather than reproduced as a placeholder. Native implementations also stay valid for graph and other computations that do not fit a columnar matrix-expression model. `docs/architecture-decisions/A5-polars-vectorbt-engine.md` records why Qlib left the expression role.
26. **Consumer identity and research access are governed outside computation.** The additive `app` schema stores principals, memberships, append-only entitlement grants/revocations, immutable release-bound publication policy sets, owner-only private object locators, authorization decisions, and content-free audit events. Browser, App, chat, Claude Code, Codex, and service-agent access uses the same server-derived `AccessContext` and stable authorization contract. Private conversation/document content is owner-only by default; administrators may see policy-permitted materialized strategy/backtest results and tenant-filtered non-content audit metadata, not private content. Only a policy-authorized administrator may record a request for an already registered immutable replay definition; that request cannot carry code, parameters, dates, source selectors, or execution inputs, and Dagster remains the sole execution authority. `AccessContext`, tenant, principal, role, entitlement, and publication identities never enter factors, engine expressions, `BacktestDataGateway`, `DecisionSnapshot`, or `ReplayEventStream`. Consumer reads resolve through a governed, access-controlled current-pointer to an immutable exact run per ADR A1 (`docs/architecture-decisions/A1-evidence-chain-in-database.md`); exact run identities stay queryable for reproducible reads and a bare mutable `latest` is never a read path.

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
L2 Factor computation     ── common/factors: native Decimal/graph factors (a test-time Polars AST oracle cross-checks the expressible ones), wrapped as Dagster assets with code_version + data_version tracked (see Section 9 exception for LLM-assisted extraction factors)
                              - base factors consume provenance-neutral views projected from durable PIT snapshots
                              - composite factors reload other factors' mart outputs; confidence cannot exceed the minimum consumed confidence, and a versioned stricter policy is allowed
L3 Analytics modules      ── the 7 concrete tools (Section 7)
L4 Backtest/portfolio validation ── factors.backtest.engine, a Decimal simulation: PIT decision snapshots + a separate monotonic future market-event stream (unadjusted bars and explicit split events)
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
Both are demoted with rules 23 and 24.

### 2.2 Service Topology and Priority (the four services are not peers)

- **Tier 0 (needed now)**: Postgres + S3-compatible raw archive + the ingestion part of data-engine (dlt adapters)
- **Tier 1 (Gate 1 onward)**: `common/factors`, the mart schema, and Dagster asset execution
- **Tier 2 (Gate 1 Staging through Gate 4 Production)**: persistent Dagster scheduling/UI plus Dokploy environments; Production output is shadow output until #54 records Gate 4 graduation
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
| Factor expression and backtest engine | Native Decimal implementations in `common/factors` compute every factor. The Polars AST compiler (`truealpha_contracts.ast` expressions, compiled in `common/factors/tests/support/expression_oracle`) is a test-time reproducibility oracle. `factors.backtest.engine` simulates portfolios in Decimal from the strategy definition's rules. VectorBT was selected in A5 and never adopted |
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

Three deployed environments stay isolated: preview, staging and production. Each one
answers one question, with the smallest scope that can prove the answer false.

- **Preview proves that the release works.** It boots on the platform, applies its
  migrations and reaches every source.
- **Staging proves accuracy.** It runs every code path on live data for the TOPT universe
  plus the sentinel listings outside TOPT, and the dashboards and the sentinel known answers
  agree.
- **Production pursues completeness.** It runs TOPT and QQQ, then the S&P 500 when its
  unlock holds (Section 0), the strategy included. It keeps the accuracy checks of staging.
  The `sp500` universe of the environment contract (cited below) holds the unlock and the
  current state.

Local and GitHub CI prove the code on recorded inputs and call no vendor. The runtime
names six logical tiers (`local_dev`, `local_test`, `github_ci`, `preview`, `staging`,
`production`), because each one substitutes dependencies differently.

This section states the principle only. The scope, sentinel set, cadence and trigger of
each environment have one source:
`common/contracts/src/truealpha_contracts/data/environment_contract.v1.json` (with sentinel known
answers in `sentinel_known_answers.v1.json`, #1416). The delivery tiers
(what each tier must catch, and its time budget) are in
`docs/architecture-decisions/A4-the-iteration-loop-is-the-product.md`. Do not copy either
into a document.

Promotion is artifact-based: PRs prove code in CI; one signed release manifest then
runs against real sources in Staging; Production receives that exact complete manifest
only after the Staging pipeline/backtest gates and a human approval. No two deployed
environments share writable Postgres databases, S3 buckets, Dagster metadata,
API ledgers, or Vault secret paths. Strategy computation remains in
`common/factors`; environments only schedule and materialize versioned results.

The promoted unit is a versioned release manifest, not one ambiguous application
image. It binds the App, LLM service, data-engine/Dagster execution artifact,
migrations, Research Catalog and SLO versions, configuration hashes, and accepted
consumer artifacts. Manual host sweeps remain reconnaissance/bootstrap tools and can
never satisfy a scheduled-run gate. The deployed Dagster resource contract must reach
the host-only moomoo OpenD boundary without exposing OpenD publicly; #11 owns the
executable connectivity and negative-network proof.

---

## 4. Repo Structure

`README.md` ("Layout") lists the top-level directories. These rules hold for each part:

- `common/contracts` holds the cross-module DTOs and the storage, repository and backtest
  ports. It holds no computation logic.
- `common/factors` is the one implementation of the seven modules. Its `base` factors read
  provenance-neutral views of PIT snapshots. Its `composite` factors read other factors'
  mart outputs, and their confidence cannot exceed the minimum consumed confidence. Its
  `shared` package holds entity resolution and the LLM structured-extraction primitive. No
  module reimplements them.
- `common/runtime` holds the environment and dependency manifest, the Postgres/KG/S3 probes
  and the immutable raw-object adapter. Every environment gets the same `DATABASE_URL` and
  `S3_*` contract.
- `db/migrations` is the schema source of truth for `raw`, `staging`, `mart`, `dagster` and
  `app`. `db/roles.sql` holds the role permissions.

---

## 5. Data Sources

| Source | Role | Details |
|---|---|---|
| SEC EDGAR | Official structured financial data | `data.sec.gov/api/xbrl/companyfacts/CIK##########.json`; User-Agent must include an email. Financial companies have no gross-profit field, needs an industry branch. Headcount is free text, needs extraction with a fallback. |
| yfinance | Zero-quota default base for daily prices and split events | Unofficial, no SLA. Its low source prior lowers row confidence. It is a normal critical-path source: it serves every universe, and a corroborating origin raises the score. Capture and reuse key is `(listing, session_date)` (#1333, #1352). |
| Twelve Data | Corroborating source for daily prices and fundamentals | One key serves every environment. Each environment enforces its own share of the plan limit from its own ledger (`derived_environment_shares("twelvedata")` from `environment_contract.v1.json`, #1416). An environment with no share (local, CI) is refused. The credit cost of one listing is in the docstring of `apps/data-engine/src/data_engine/datahub/production_topt/twelve_data_origin.py`. A cell Twelve Data cannot serve stays served by the other origins at lower confidence. **QQQ price refresh (#1277):** `qqq_price_refresh_pipeline` stores the daily series of the QQQ listings outside the TOPT symbols. Its cadence and credit budget are in the docstring of `apps/data-engine/src/data_engine/lanes/qqq_prices.py`. |
| U.S. Treasury | Official source of the macro base data and of the risk-free rate of the core strategy (#1255) | "Daily Treasury Par Yield Curve Rates": one public CSV per calendar year, no key (`home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv`). One file carries every series. Three series are stored: `us_treasury_bill_3_month` (column `3 Mo`), `us_treasury_note_2_year` (`2 Yr`) and `us_treasury_note_10_year` (`10 Yr`), in percent in the file and as decimal fractions in the table. The column set differs between years, so the parser reads each column by its name. They are declared once, in `MACRO_REGISTRATIONS` (`source_registrations.py`), with the ledger seat `us-treasury` (100 requests per day as our backstop), the per-key cost 1 and the as-of axis `business_day`. The first run loads one file per year since 2000. The bytes land in `raw.fetches`. `mart.macro_series_daily` is append-only: `observation_date` is the valid time, `knowable_at` is the transaction time (00:00 UTC of the day after the observation, because the Treasury obtains its input quotes at about 3:30 PM New York time; for a revised value the time of the fetch that showed it), and a revised value is a new row. A file that landed on the same UTC day is not fetched again. `datahub.risk_free` takes the newest 3-month observation that was knowable at the cutoff and is at most 10 days old, and raises `MissingRiskFreeRate` (reason `missing_risk_free_rate_parameter`) otherwise. A tick without a rate keeps its capture and fails before it freezes a snapshot, so the served head stays on the last accepted run. A backtest without a rate fails and writes nothing. The lane `macro_data` fetches every day at 20:30 UTC in production and staging, records the verdict `macro_freshness` (red when the newest value of a series is older than 5 days), and can be launched by hand. The rights and budget attestation of this source is `missing`, like every source except the model provider. Under rule 19 that is a dashboard flag, not a gate. |
| LLM extraction provider | External source for prose-only fields (headcount, segment classification, disclosed relationships) | Module 6's segment-theme classifier calls it per (issuer, filing, theme) and records the served model on each `mart.issuer_theme_purity` row. `common/factors/src/factors/shared/extraction.py` owns the shared selection rules (`select_single_candidate`, `select_exhaustive_partition`) and mints every rule identity (#769, #803). The provider enters like any other source: a `SourceRegistry` entry, a rule-19 prior and budget record, a declared capacity (the `filing-extraction-model` seat in `LEDGER_CAPACITIES`), every call recorded in `api_call_ledger` with its token cost, and an append-only invocation record per Section 9. It is never called from a factor or a consumer surface. Replay reads the stored result: a second run of the same ask returns the recorded decision without a call to the provider. |
| moomoo | Licensed terminal feed for K-lines, statements, analyst ratings and valuation detail. Rate-limited (bursts/30s), not a monthly quota — see below | OpenD runs on the VPS host. `moomoo-kline:v1` and `moomoo-financials:v1` fetch on schedule. The gated lanes call `get_analyst_consensus`, `get_rating_summary` (per-analyst rating history filtered to `recommendation_date_str <= cutoff`, `success_rate`, `excess_return`), and `get_valuation_detail` (`profit_growth_rate`) through `sources/moomoo.py` (#1333, #1354). Vendor timestamps are accepted evidence. A moomoo rating event is knowable at its vendor date, and the row carries the `vendor_timestamp` reason code and a lower time-quality factor. Fixtures live in `apps/data-engine/samples/moomoo/`. ETF weights and company-level supply-chain edges are not sourced from moomoo. |
| Historical analyst ratings — fallback | Optional second route for a rating event | A second route for the same event raises the confidence of that event. Its absence does not block the moomoo row. |
| ETF holdings weight data | **Confirmed: SEC EDGAR N-PORT-P** | Monthly per-series filings, per-holding `pctVal` + CUSIP (verified on QQQ, ARKK, IVV, AGIX and MCHI). Pitfalls: the raw XML is `primary_doc.xml` (the filing's `primaryDocument` field points at the XSL-rendered HTML); multi-series trusts (e.g. ARK) must be queried by series ID via browse-edgar, not by trust CIK; foreign holdings carry CUSIP `000000000`, so `same_as` resolution needs an ISIN/name fallback. **SPY is a UIT and absent from SEC's fund-ticker mapping — proxy the S&P 500 with IVV/VOO.** The publicly available filing is the last month of each fiscal quarter (~1-3 months behind) — fine for defining a universe, not a live holdings feed. |

**moomoo quota, corrected 2026-07-10**: moomoo's own "Authorities and Quota" docs only define two quota systems — a *subscription* quota (real-time push, tiered 100/300/1000/2000 by account assets/trade volume) and a *historical-candlestick* quota (per unique stock per rolling window, not per call). Fundamental/quote endpoints (`get_financials_statements`, `get_research_rating_summary`, `get_valuation_detail`, etc. — everything the Phase -1 moomoo audit actually uses) appear in neither table; they're only rate-limited (bursts per 30s), not capped at a monthly total. No evidence shows a monthly cap on these endpoints. `moomoo_ledger.py`'s gate stays as a defensive throttle and audit trail (Section 1 rule 6: no module decides for itself whether to call).

**moomoo analyst-rating depth, confirmed 2026-07-10 (Path A)**: `get_research_rating_summary`'s per-analyst summary rows embed each analyst's own historical `rating_item_list` inline — DDOG alone has 20 analysts with 159 combined dated historical rating rows, plus moomoo-computed `success_rate`/`excess_return` per analyst. This is enough for module 4's per-analyst backtesting — see `apps/data-engine/samples/README.md` for the full findings and `apps/data-engine/samples/moomoo/` for the captured fixtures.

---

## 6. Database Schema Design

Five schemas: `raw`, `staging`, `mart`, `dagster` (Dagster's own run/event/schedule storage, explicitly configured, not left on default local storage), and `app` (consumer identity, immutable access policy/audit records, and owner-isolated private object locators; never factor computation or materialized analytics).

**Time axes (every staging table carries all three):**

- `valid_time` — the real-world period the data describes.
- `transaction_time` — when the fact became **publicly knowable** (the DTO layer calls this `knowable_at`). Always set explicitly by the writer from a source property (filing date, publish time) — **never defaulted to the insert clock**. The failure mode this kills: a new source added in year N backfills five years of history; stamped with `now()` those rows are invisible to every historical as-of query, stamped correctly they are usable while `recorded_at` still tells the truth about when we got them.
- `recorded_at` — when TrueAlpha ingested the row. Audit and reproducibility only; as-of resolution never reads it.

**Identity.** #57 distinguishes issuer, security and listing identities. Migration work
preserves legacy IDs as versioned aliases and never rewrites history. The transitional
`unified_id` is not a second valid identity model.

**Entity identity (owner decision 2026-09-17, #877).** No identifier scheme is canonical. An
issuer, instrument, listing or fund is named by an opaque UUID, derived (UUIDv5) from the alias
it was first minted for under its kind's namespace; CIK, LEI, CUSIP, ISIN, FIGI, ticker@MIC,
moomoo codes and the legacy `issuer:…`/`security:…`/`listing:…` strings are typed aliases with a
validity interval, a knowable-at time, provenance and confidence. Merges and splits add edges
and rewrite nothing, and every read resolves an alias at an explicit valid date and
knowable-at time. The store is `staging.entities` / `entity_aliases` / `entity_relations`
(migration `20260917T0412`, filled by the Dagster job `entity_identity_backfill`, never at
boot); the ontology, rules, backfill and
switch-over plan are `docs/entity-identity.md`. Until captures carry UUIDs (#877 PR-3), the
legacy strings remain the payload ids, and each one resolves through its `legacy-id` alias.

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

**Confidence and multi-source value selection (owner decisions 2026-09-17 and 2026-10-15, #1333; supersedes the source-count tiers of 2026-09-17).** Confidence is one continuous score on 0 to 100. It is derived per cell. A constant per semantic never stands in for it. The formula is `docs/confidence-calibration.md` (#207): source prior reliability, origin-group independence, agreement, freshness, semantic mapping quality, lineage completeness and component completeness.
- **Source prior.** The registry holds one prior per source. The order is SEC filings, then moomoo, then Twelve Data, then yfinance, then single-source prose extraction. A prior is data in the registry, not a branch in factor code (rule 3).
- **Value selection.** At least 3 independent origin groups within the tolerance: the served value is their median. Two groups within the tolerance: the served value is the priority source's value. One group, or no agreement: the served value is the highest-priority source's value. A disagreeing extra source is recorded and lowers agreement.
- **Tolerance.** 0.1% relative for each family unless a versioned policy sets another (market prices use `market-price-fusion:v3`, financial facts use `financial-fact-fusion:v1`).
- **Bands for display.** High is a score of 80 or more. Medium is 60 to 79. Low is below 60. SEC plus moomoo agreeing can reach High. One SEC source can reach Medium. One yfinance source is Low and is still served and counted as available.
- **No score blocks a row.** A low score is shown on the dashboard and carried to the consumer. It never turns a computable value into `unavailable`.

**Where fusion is exercised.** The rule above applies on the planes that carry data:

- **Evidence lives on the observation plane.** `staging.capture_normalized_observations`
  holds one row per `(obligation, semantic)` with its source, `parser_version`,
  `mapping_version` and confidence. Two sources asserting the same subject are two rows.
  That IS "every source's assertion side by side"; the granularity is the captured
  payload, not one row per metric.
- **Selection is exercised at snapshot freeze.** `staging.topt_core_snapshots` is the
  durable "selected fact set", and every selected observation ID plus policy version is
  persisted in its manifest before any factor runs.
- **Target selection ranks by the registry.** Every candidate is kept. The registry order
  of `source_priority` selects the value, and a second candidate raises or lowers the score
  through agreement. A second candidate never refuses the run. #1333 records the gap between
  this target and the snapshot freeze in code.
- **Prices fail over in policy order.** A cell the primary origin cannot serve is served by
  the next origin in the policy's order, declared as a failover and graded on its own
  corroboration. Registering a second source for an existing metric changes only
  source-owned code and registrations (rule 22).

Mart lineage points to the snapshot and its exact selected records. They chain through
mapping/extraction IDs to `raw_ref` and immutable bytes. Changing any selection policy
creates a new snapshot/materialization; old evidence and results remain addressable.

**Factor-input projection (`staging.strategy_backtest_inputs`).** The provenance-neutral
projection factors actually consume, and the only shape rule 3 permits them to see. Its
axes are `issuer x cutoff x metric x fiscal_period x vintage`:

- `input_key` names a metric. It MUST validate against the metric registry
  (`common/contracts/src/truealpha_contracts/metrics.py`), never against an enumerated `CHECK` — see rule 22.
- `fiscal_period` is the period a value DESCRIBES, and is NULL for point-in-time
  observations (price, share count, headcount) which describe an instant. A metric may
  appear once per period per cutoff; that axis is what lets a multi-period factor live in
  `common/factors` instead of being pre-reduced in the capture layer.
- **The period tag is a declared format, not a convention:**
  `<filing fiscal year>:<kind>:<start ISO date>:<end ISO date>`, e.g.
  `FY2025:FY:2025-01-01:2025-12-31`. The leading fiscal year is the FILING's, never the
  period's — a filing stamps its comparatives with its own tag. Consumers key on the END
  DATE. Writer and reader must share one encoder/parser; a format known only by a producer
  and a regex fails silently, as an empty series rather than an error.
- `vintage` is `recorded_at`-ordered supersession: a restatement appends and wins by
  recency; history is never overwritten.
- **History rows (#1139, #758).** `datahub/strategy_history.py` writes the rows of past
  cutoffs from stored bytes only, with zero vendor calls. Its module docstring holds the
  cutoff, point-in-time, share-basis and idempotency rules. The lane is
  `strategy_history_projection_pipeline`.
- **Live rows (#429, #1262).** The deployed tick seeds the rows of its own cutoff from the
  captured cells of its run (`datahub/strategy_bridge.py`). `last_close` is the captured close.
  The share count is put on the share basis of that close by the rule in
  `datahub/share_basis.py`, which the history rows also use. The splits come from the two
  stored daily series of the issuer's listings. Any registered price origin may supply them, yfinance included (#1333). A wrong share
  basis gives a wrong number, so this guard is an integrity check and not a confidence score.
  When those series cannot prove the splits between the count's basis date and the
  close's session, the tick writes no share row for the issuer. The strategy then excludes the
  issuer with `missing_market_value_input`, and the tick log names the reason. The series may end
  one session before the close's session. Then the close must stay strictly within a factor of 1.5
  of the last stored close (ADR A6). The filed count never stands beside a close of another share
  basis. The TOPT core engine follows the same rule (#1277). `_snapshot_member` drops the count of a
  listing whose series cannot prove the splits, or whose symbols have no stored bar. The core result
  is then unavailable, and its `reason_codes` hold the reason of `share_basis`
  (for example `no_split_adjusted_series`).
- **There is deliberately NO `source` column.** Rule 3 forbids factors seeing source,
  `raw_ref` or accession. Traceability is served by lineage recorded OUTSIDE factor
  computation (rule 3): `staging.evidence_nodes` / `evidence_edges`.

**Knowledge graph.** Entity resolution is not unique to companies — companies, ETFs, analysts, and supply-chain nodes all have the same "same real-world thing, different IDs per source" problem, so it's modeled once as a graph, implemented as plain Postgres tables (no separate graph engine — the query patterns needed here are shallow joins, not deep multi-hop traversal). The tables are `staging.kg_entities`, `staging.kg_identifiers` and `staging.kg_edges`; `db/migrations` holds their DDL. These rules hold:

- An entity has one type: issuer, security, listing, analyst, universe, theme or supply-chain node.
- A source identifier row locates a source-specific node. Resolution traverses a `same_as` edge to the unified entity at the requested transaction-time cutoff.
- An edge carries a relation type (`same_as` for the cross-source ID crosswalk, `supplies_to`, `holds` for ETF to company, `covers` for analyst to company), `valid_time`, an explicit `transaction_time`, `recorded_at`, `confidence`, `source` and `raw_ref`.
- Scenario propagation enforces a versioned minimum edge confidence. Edge confidence alone cannot prove causality (rule 10).

Mart's flattened 2D tables (e.g. a company-to-ticker lookup, or a supply-chain adjacency table for a given company) are SQL views/materializations over `kg_entities` + `kg_edges`, not separately maintained.

The entity identity store (#877, `docs/entity-identity.md`) reads the graph's rows as evidence (the N-PORT/OpenFIGI ISIN crosswalk). Its `company:*` nodes become aliases, and its writers move to the store in #877 PR-5.

The `mart_readonly` role (`db/roles.sql`) reads `mart` only and has a statement timeout. Typed mart repositories add cursor pagination and bounded row counts. MCP and chat never accept model-generated SQL.

Other tables: `api_call_ledger` (the external call ledger for every source and the model provider — rule 6; moomoo was its first tenant), `ingestion_health_log` (only business-specific metrics the Dagster UI doesn't already cover).

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
`ReplayEventStream`, or engine expression types. This contract does not activate authentication,
routes, identity-provider bindings, retention policy, replay execution, or sharing.

---

## 7. The Seven Analytics Modules

**The module number is an identity, and `common/factors`' `@factor(..., module=N)` must match this list**, checked by `common/factors/tests/test_module_identity.py` against this exact section. A **feeder** (`@factor(..., kind="feeder")`) answers none of the seven questions on its own -- it is an input a composite reduces -- and registers the module it feeds rather than a number of its own, so it needs no item here and no question column (#855 B3). `price_to_sales` is one: the market-value input `three_tier_valuation` (module 7, the composite) reduces alongside gross profit per employee, so it registers `module=7` (#770). `price_to_gross_profit` is the second: market value / gross profit, the valuation basis of strategy definition 1.0.0, so it also registers `module=7` (#1235).

Modules 1-6 are **base factors** (Section 4, `common/factors/src/factors/base`) — the runner projects provenance-neutral snapshot inputs for them. Module 7 is a **composite factor** (`common/factors/src/factors/composite`) — it reloads other modules' materialized mart outputs, and its confidence cannot exceed the minimum confidence consumed; a declared versioned policy may be stricter.

1. **PEG**: switchable growth-rate conventions. `factors.base.peg` computes the historical-CAGR convention. An issuer without a PEG carries its reason on the row (`mart.strategy_decisions.peg_reason_codes`, #837). The analyst-consensus convention reads moomoo `get_valuation_detail` `profit_growth_rate` (#1333). No plane carries company guidance yet.
2. **Gross profit per employee**: named metrics of the metric forest (rule 17): `gppe_uniform_charge_v0` for every issuer and `gppe_banking_tce_v1` for financial issuers. Headcount gaps are explicitly flagged rather than silently dropped; a negative value is a fact, not a defect — the datahub refuses only what cannot exist or cannot be computed (e.g. division by zero), and distortions are the factor layer's declared, versioned adjustments (docs/metric-forest.md §6.1)
3. **Supply-chain relationship graph + confidence-gated scenario exposure**: graph first (KG `supplies_to` edges); path propagation must declare a versioned shock/exposure scenario, direction, materiality/sensitivity, and confidence kill condition. It may be described as causal only after independent causal evidence, not merely because an edge is high-confidence. An extracted edge lands with its evidence span, a single-source prose prior and a confidence from the score (owner decision 2026-10-15, #1333). A second route, such as the filing text of the partner, raises it. The mart separates two reasons for an empty row: `no_supply_chain_extraction` (no `supplies_to` edge exists for any issuer at the cutoff) and `no_disclosed_suppliers` (edges exist for other issuers and none for this issuer).
4. **Analyst backtesting**: moomoo historical rating depth is confirmed. A rating event is knowable at its vendor date (reason code `vendor_timestamp`). It enters PIT scoring with a lower time-quality factor than an event with a second, public date. Backfilled rows are not hidden: the scoring window of an analyst states how many events carry only a vendor date.
5. **ETF virtual company**: SEC N-PORT-P is the confirmed holdings-weight source; calculations must respect report/filing lag, fund-series identity, instrument type, unresolved weight, currency, and period alignment
6. **Pure-blood company screening**: LLM-assisted semantic classification of segment revenue (`factors.base.theme_purity` -> `mart.issuer_theme_purity` -> `/research/themes`, #772), over the accepted segment partitions in `staging.issuer_segment_revenue_facts`. These rules hold:
   - The denominator of a share is **consolidated** revenue, never the sum of the classified parts. A missed segment would otherwise raise every remaining share.
   - A declined classification is unclassified revenue, not a negative. Coverage below the theme's governed floor refuses the row instead of ranking it.
   - The parts are the segment revenue that the filing tags on the business-segment axis of its inline XBRL. A filing that tags no segments is refused (#830, #835).
   - The segment count that the filer tags in its inline XBRL decides a one-segment issuer, never a sentence (#822). The description of a one-part row is the filer's segment note (`us-gaap:SegmentReportingDisclosureTextBlock`, read through its `ix:continuation` chain) plus its nature-of-business opening, and the classifier is told the issuer's ticker (#841, #849).
   - The rows are the governed run's members only, under the issuer id that the run gives them (#828). A withdrawn extractor's rows stay in the append-only plane and are absent to every reader.
7. **Three-tier valuation tagging** (composite): sorts a company into the traditional / tech / large-model-native tier (Vision, "large-model-driven company" framework) by reading module 2's gross-profit-per-employee output (and other base factors as needed), and values it against the tier's band of the definition's valuation basis (P/GP since strategy definition 1.0.0, #1235; one definition file supplies the bands to the strategy and the TOPT core engine) — this is a screening/interpretation layer expressed as a factor, not app-layer logic (Section 1, rule 2)

---

## 8. Release Gates and Verification Criteria

| Gate | Goal | Verification | Infrastructure scope |
|---|---|---|---|
| Gate 0: Semantic & Data Closure | Freeze research semantics, issuer/security/listing and currency/time/return rules, executable snapshot/extraction/invocation/lineage contracts, source/type registries, usage/reverse-review contracts, longitudinal source rights, and module coverage/freshness SLOs | Contract examples and fixture/Postgres conformance pass; additive source/type probes require no generic dispatch change; every required field has a registered source with a prior and every module has a usable-coverage denominator and a dashboard row | Local/CI only; no interface is called v1-frozen before this gate |
| Gate 1: Core Strategy MVP | Gross profit per employee, financial branch, three-tier valuation, `large_model_value_v0`, deterministic Decimal replay, mart/report, and real Staging canary | Independent core oracle passes; the pinned engine/adapter path reproduces golden decisions with required usable coverage and no look-ahead; Staging proves the same artifact operates idempotently | Dagster enters with the first executable snapshot/factor slice; persistent Staging by gate end |
| Gate 2: Seven Research Modules | Add all PEG conventions, analyst track records, ETF virtual company, supply-chain scenario exposure, and pure-blood screening through the shared PIT path | Every module runs on deployed data with a continuous confidence score and one shared seven-module replay; a computable value is never served as `unavailable` because its score is low | Dagster assets and module-specific longitudinal data planes in Local/CI/Staging |
| Gate 3: Research Consumption | Stable mart reads, usage/review reads, MCP, reports/cards, App, and deferred `/chat` | Canonical Vision questions agree across the deployed consumers (App, MCP, reports/cards) reading the same governed materialized outputs — fixture conformance suites remain CI-only evidence and cannot close a consumption issue; report/card facts come only from materialized outputs; bounded usage and reverse-review reads retain exact trace identities | App reads mart directly; MCP/chat use typed read tools; no raw/staging consumer access |
| Gate 4: Production Strategy Validation | Multi-regime data/strategy checks, reverse data-quality review, all-module Staging soak, recovery/exact-release promotion, deployed Production consumers, curated-universe expansion, and shadow graduation | Known-reference sanity, independent price/data reconciliation, usage/reverse-review reconciliation, SLO soak over natural source changes, backup/restore, real-client questions, and final Vision audit all pass | Isolated Production remains shadow while #67 freezes the candidate and #68 certifies capture; only #54 records authoritative graduation |

**Gate acceptance and issue ownership (a gate claim needs a standing check or a dashboard row):**

- **Gate 0 — #56 (#57-#61):** `UniverseRef`, capture obligations (rule 15), source/type registry snapshots, applicability-independent `SourceCapabilityCatalog`, source-neutral data requirements, requirement graph/schedule demand compilation, automatic run/module/emitter usage, reverse quality review, catalog, applicability, source-prior/budget and natural-refresh contracts are versioned and executable. #60's capability inventory cannot depend on #61 applicability; #61 projects it onto the exact denominator. The approved catalog cannot be smaller than the product-owner-approved sensor/core scope.
- **Gate 1 — #29 (#14, #21-#27, #70-#71):** #70 owns only the PIT document-to-headcount data path needed by the Core Strategy. After #24/#25 freeze candidate implementations, #71 runs the blind financial/non-financial GPPE, valuation-multiple (P/S, and P/GP from strategy definition 1.0.0, #1235), tier, and valuation-gap holdout as optional evidence (rule 18). #26 owns the version-pinned engine adapter and replay evidence; replay still uses unadjusted bars and explicit actions under the TrueAlpha clock. #27 proves that exact artifact on the immutable TOPT Core canary; #51 later owns all-module Staging soak, and a two-run canary is not continuous operation.
- **Gate 2 — #30 (#33-#40, #62-#65):** all catalog-required module invocations have longitudinal inputs and meet predeclared per-required-subject coverage. #65 holdouts are optional evidence that raises a score (rule 18). An `unavailable` placeholder cannot satisfy this gate. A low score does not block it; the dashboard shows the score.
- **Gate 3 — #31 (#41-#46, #48, #72, plus registered additions #229/#235/#236/#251):** the deployed consumers agree on typed materialized outputs and bounded usage/review reads from the same governed head; fixture conformance suites are CI-only evidence and do not close a consumption issue (AGENTS.md rule 6, audit #429). #72 separately proves configuration-only onboarding for an unseen issuer/theme and additive onboarding for one test source plus one semantic type; the latter may add isolated extension code and one probe factor, but cannot change central dispatch, generic Dagster/snapshot/lineage/usage/review code, existing factors, or consumers.
- **Gate 4 — #32 (#11, #49-#54, #66-#68):** the exact signed release manifest and full approved `UniverseRef` pass natural-refresh soak, anti-shrink SLOs, usage and reverse-quality reconciliation, mandatory Production authentication, full-scope capacity tests, scheduled database and raw-object backups with a visible restore age, real-client equivalence, and a human look at the card deck. #67 produces the full-universe Production shadow candidate; #68 independently certifies its row-complete capture; #54 then reads the dashboards for scope, backup age, consumers, capture, usage and quality and records authoritative graduation. Seeded bad evidence must make `vision-audit` fail.

**Availability and validation are separate**. Every factor output carries an
`availability_status` such as `available`, `unavailable`, `stale`, `excluded`,
`low_confidence`, or `error`, plus a `source_evidence_status` for the selected semantic
records and a separate `factor_validation_status` for the factor version's independent
golden/holdout gate. Only applicable, available, fresh outputs from an accepted factor
version count toward the module's versioned usable-coverage SLO. A low confidence score does
not remove an output from that count. Consumers display all
three dimensions rather than conflating source evidence with formula validation.

Environment and evidence scale are part of the gate contract. Local/CI exist
throughout; Dagster is introduced early in Gate 1 and is the only authority for real
scheduled runs; Production remains isolated shadow output until Gate 4 data, strategy,
consumer, recovery, soak, capture-audit, and human-graduation evidence all pass.

Release gates define claims and promotion order; implementation is conventional issue→PR
work (see `AGENTS.md`). Grow evidence incrementally — prove a slice on a tiny fixed
corpus before scaling it — and merge verified PRs independently into `main`; a merge
never promotes an environment or implies gate completion. This holds exactly the same
way once a green, quiet main HEAD is tagged automatically: `auto-release-staging.yml`
(#860, owner decision 2026-09-17) cuts a Staging release on a debounced timer so nobody
has to run the ceremony by hand, and stops there — Production only ever moves on a
deliberate `tools/cut_release.sh --prod` run, a soaking Staging tag asserts nothing about
any gate above, and the automation carries no authority to promote or close one (see
`docs/release-protocol.md`, "Staging cuts itself; production still does not"). Disjoint
capture, platform, strategy, consumption, and verification lanes work in parallel against exact
content-hashed handoffs; coordinate through issues before touching shared surfaces
(types, exports, registries, migration numbering, generated contracts, lockfiles,
authoritative architecture documents).

Dependencies state whether they block provisional implementation, candidate freeze, or
issue/gate closure. Downstream fixture/local development may begin after its required
contract-repair handoff even if rights, holdout, soak, or natural-refresh evidence remains
pending. Its
declared readiness ceiling cannot exceed its evidence: fixture tests prove contracts,
development goldens prove candidates, holdouts (when run) raise module scores, Staging canaries
prove bounded operation, and only natural-refresh plus independent Production evidence
can prove graduation. Formula/source/applicability/SLO semantics and exact candidate
hashes freeze before protected evaluation; post-reveal changes require a new version and
fresh untouched evidence. PIT rules, append-only restatements, fixed denominators,
environment-scoped rights/budgets, row-complete capture, recovery, and human approvals
remain mandatory at their applicable evidence scale.

The gate rows above are claims; they are checked, not closed in order. The gate epics
(#56, #29, #30, #31, #32) remain the registry of what each gate claims, but
delivery is not sequenced through them (`governance/README.md`): each claim is
proven or refuted by a standing check on the axis roots (#434 factor chain, #530 datahub
completeness, #284 factor flexibility, #544 backtest reproducibility, #70 extraction,
#581 production invariants, #712 release identity) under `AGENTS.md` rules 6 and 7, and
a gate epic closes by hand when every claim in its row has such a check. Evidence
(captured corpora, evaluation records) is content-hashed for replayability. Graduation additionally requires the
capture audit (#68), the final Vision audit, and recorded owner approval. Day-to-day
delivery is conventional: one issue, one pull request, tests and review before merge, as
defined in `AGENTS.md`.

### Served freshness (read time)

`mart.served_head` computes the age, the limit, the `freshness` label and the `availability`
at each read. It uses the time of the last good refresh and the data cadence. No consumer relies on a stamp written at publication.

The limits live in `mart.freshness_limit`. Git is the authority for the rows that the seed
lists. The seed in `db/migrations/20261006T1020_datahub_served_head_freshness.sql` restores
them at every boot. A row removed from the seed stays in deployed databases until a migration
removes it. Change a limit with a reviewed edit to that seed. The table cannot hold a limit
above 720 hours.

| Cadence family | Limit (hours) | Limit (days) | Data in this family |
|---|---|---|---|
| `daily` | 72 | 3 | Governed heads from the daily capture ticks; daily quality checks; market data (weekdays only) |
| `weekly` | 336 | 14 | Universe refresh, standards backfill, entity identity |
| `quarterly` | 720 | 30 | Filing-derived facts, checked monthly |
| `withhold` | 720 | 30 | Cap for every family: no value older than this is served |

These rules describe what `mart.served_head` and `mart.head_freshness` return.

- **Age** is the time since the last good refresh. An age equal to the limit reads `fresh`.
  An age above the limit reads `stale`.
- **Past its family limit**, a value is served with `freshness = 'stale'`, its `age_hours` and
  a reason code. The codes are `older_than_3d` and `older_than_14d`.
- **Past the `withhold` limit** (30 days), the value is withheld. Then `availability =
  'unavailable'`, the reason is `older_than_30d`, and `run_id` is null. No number is shown.
- **A quarterly value** reaches its limit at the `withhold` limit. It goes from `fresh`
  straight to withheld. It is never served `stale`.
- **A head with no registry row** gets the strictest limit.
- **A retired universe version** is not a head (#1320). The family of a head is its
  `universe_id` without a trailing `-YYYY-MM-DD`. Per environment, family and factor, the view
  returns the head that advanced last. Equal times go to the larger `universe_id`. A family with
  one head keeps that head, also when it is stale or withheld. `mart.current_pointer_head` keeps
  every version.
- **A missing refresh time** reads `unknown` and `unavailable`. The reason is
  `refresh_time_unknown`.

`mart.served_artifact` is the registry. Each row maps one served artifact to one cadence
family. Each scheduled lane has a row. `apps/data-engine/tests/test_freshness_registry.py` fails
when a lane or a schedule has no row.

The `wired` column is a readiness flag. It is true when an age source is wired for the artifact.
The age source of a governed head is the pointer's `advanced_at`.

There is one read point. A consumer reads `mart.served_head` and not
`mart.current_pointer_head`. A consumer computes no head age of its own.
`common/runtime/tests/test_served_head_guard.py` fails on the bypass shapes that it knows. It is a
heuristic, and a new shape can pass it. Its BASELINE lists the exceptions, and it can only
shrink. The guard works per file. It cannot prove that a run id flows from the head into a result
query. Per-reader behaviour tests are the real proof.

`/api/health` reads the head. It reports each governed pointer with `freshness`,
`limit_hours`, `staleness_reason` and `availability`. Its `status` stays `ok` for a stale head.
It reports `governed_pointers: "unknown"` when heads exist that this database does not serve.

Four limits are accepted:
- The word `unknown` has two uses. It is the `freshness` value for a missing refresh time. It is
  also the health marker for pointers that cannot be read. Both keys are frozen.
- A refresh time in the future counts as age 0.
- `/api/health` rounds `age_hours` to one decimal. An entry can show `72.0` and `stale`.
- The environment filter appears in `mart.served_head` and in `mart.served_head_environments`.
  One scenario test ties the two views to each other.

This is a different measure from three others. The capture-time windows in
`raw.capture_schedule_policies` judge one observation when it is captured. They are 5 days for
`market-price` and 730 days for `financial-fact`. The two-day maximum age in
`docs/datahub-service-demand.md` is the demand objective of one capture requirement.
`tools/nightly_verdicts.py` ages each nightly verdict at twice the cadence of its check. None of
the three changes. The nightly `report_surface_proof` fails on a scheduled head that
`mart.served_head` labels stale, so the pointer is bounded at the `daily` limit. It also fails
when the environment serves no scheduled head, because zero heads prove nothing.

---

## 9. Known Risks / Pitfalls

- SEC XBRL tags are inconsistent across industries — don't assume field names/units are uniform
- yfinance has no SLA. It is the default price base and carries a low prior; a second origin corroborates it. A price cell it cannot serve fails over to the registered origins in the fusion policy's order (Twelve Data, then moomoo K-line), served under that origin's own identity one confidence grade lower and never counted as corroborated on its own (`docs/datahub-quality-report.md`, "Primary failover")
- Arbitrary SQL touching raw/staging carries silent look-ahead risk; roles deny it and MCP/chat expose typed mart reads only
- dlt schema evolution must use frozen mode for core tables
- **Dagster's `code_version`/`data_version` assumes deterministic inputs.** LLM extraction is therefore a separate, versioned, append-only step. Its invocation binds model, instructions, schema, and decoding settings; the stored semantic result and evidence spans determine downstream `data_version`. Replay reuses that stored result and never silently calls the model again. A new extraction is a new invocation/vintage, not sampling noise hidden behind the old ID.
- **LLM self-reported confidence is not ground truth.** It may be retained as one signal with a low prior. The versioned continuous confidence policy sets its weight, and a holdout, when one exists, calibrates it. `low_confidence` is a displayed score band. It does not remove a value from usable coverage. More expensive self-consistency or review paths are introduced only through a new versioned policy backed by that evidence.
- Discriminated unions only reliably generate `oneOf` when declared at the top level with an explicit discriminator; nested cases are a known open issue — handle if/when encountered

---

## 10. Known Architectural Debt (deliberately not solved now, but written down)

- **Multi-user moomoo quota allocation**: not relevant while single-user; must be designed before going multi-user, no pre-modeling now
- **Consumer page/query analytics**: V1 measures semantic pipeline and strategy consumption only. Add read-frequency telemetry later only with a separate append-only audit writer that preserves the App's `mart_readonly` role and cannot recursively count usage queries.
- If Postgres concurrency becomes a real bottleneck, re-evaluate read/write splitting or a caching layer
