# A8 — Strong Dashboards, Weak Gates: Observability over Deadlocking Blockers

Status: Accepted (in force; `AGENTS.md` Rule 4 and Rule 6 updated 2026-10-12 and revised 2026-10-21)
Date: 2026-10-12
Extends: `AGENTS.md` Rule 4 ("How work happens"), Rule 6 ("Closed means verified on staging with real data"), and ADR A2

## Context

Over successive iterations, defensive engineering mechanisms degenerated into brittle, multi-tiered "hard gates" (强门禁). Rather than preventing defects, these gates produced development paralysis, workarounds, and false security:

1. **External Platform Deadlocks**: `tools/check_pr_ready.py` (deleted in #1237) introduced fail-closed logic that blocked PR merges whenever an external AI reviewer (such as GitHub Copilot) suffered a network timeout, platform outage, or emitted unparseable prose. PR #1198—which implemented necessary GPPE metric naming, was fully tested, and passed all CI checks—was locked for two days due solely to Copilot emitting an "encountered an error" response. <!-- doc-drift: history -->
2. **Form over Substance Cargo Cult**: `tools/vision_close_guard.py` and its accompanying GitHub Actions workflows (`vision-close-check.yml` and `vision-close-reopen.yml`) introduced regex-based pull request blockers on GitHub closing keywords and automated bots that reopened closed issues lacking literal Markdown headings (`Deployed call site` and `Real-data output`). This incentivized boilerplate copy-pasting to satisfy the regex, while failing to detect whether the underlying service was healthy. <!-- doc-drift: history -->
3. **Metric-to-Gate Inversion and Issue Spam**: `tools/deploy_freshness.py` and `deploy-freshness.yml` inverted an informational age measurement (production deployment older than 14 days, staging older than 3 days) into an exit 1 CI blocker that automatically filed noisy tracker issues (#1202, #1203). This converted normal release soak intervals into pipeline red lights. <!-- doc-drift: history -->
4. **The False Green Paradox**: Despite dozens of mechanical gates, substantive physical defects repeatedly escaped detection:
   - A `0.08` hardcoded constant stood in for measured tangible common equity (#1108, #1176);
   - A demo close-price sorting strategy stood in for fundamental factor backtesting (#758);
   - A `not_applicable` grade masked uncomputed PEG metrics on QQQ (#749, #1115).
   Mechanical gates checked only syntax and presence of tokens; they could not check physical reality.

## Decision

We adopt the foundational principle: **"Strong Dashboards, Weak Gates" (看板优于门禁，强看板，弱门禁)**.

### 1. Three gate classes and four test classes
A gate belongs to one of three classes. Everything else is a dashboard.
- **Class 1, the merge gate:** compilation/build, typecheck, backward-compatible migrations, secret scanning, security/role boundaries, and dependency/contract/smoke checks. It also holds the review-finding budget and the independent review of gate and release code (`AGENTS.md` Rule 4).
- **Class 2, the post-deploy effect probe:** after a deploy, a read-only probe shows that the change takes effect. It also shows that `main` still deploys.
- **Class 3, staging evidence before a production deploy:** a production deploy of a release waits for the staging evidence of that same release.
- **Four-level classification rule (`dev_env#782`, MECE order):** evaluate every proposed check sequentially by first `Yes`:
  1. **Gate** (blocks PR merge): prevents production-reservation bypass, cross-domain privilege leaks, secret exposure, Moomoo trading calls, or non-idempotent migrations.
  2. **Dependency / Smoke** (CI contract + thin Staging smoke): depends on external runtime boundaries (Postgres/SQL/roles, S3, containers, OpenAPI/env manifests, Dagster, HTTP/SSR, external vendors).
  3. **Fact** (dashboard only, non-blocking): measures operational metrics or data quality (coverage, reasons, freshness age, oracle disagreements, quotas, 429/1302, cost, vulnerabilities).
  4. **None** (deleted): pure logic branches, prose regexes, AST static-police checks, count ratchets, history tombstones, or fake-DOM E2E proxies; AI review and Staging dashboards govern these.
- Third-party and AI reviewer feedback is **advisory**: infrastructure timeouts, reviewer platform errors, or unclassified review comments log warnings and never fail-close PR readiness.
- Mechanical text policing (regex matching on PR closing keywords, AST style/count ratchets, automated bot reopen scripts) is abolished.

### 2. Strong Dashboards and L0–L4 Split (Radical Observability as Primary Governance)
- Platform observability follows a strict L0–L4 boundary: CI/delivery health lives in `infra2` Watchdog and `StageRecord`, container CPU/memory/latency telemetry lives in SigNoz, and runtime/business truth lives in TrueAlpha's unified dashboards and MCP tools:
  - **Operations & Recovery (`/admin`)**: Exposes runtime dependency probes (Postgres, Graph Store, S3), `mart.served_head` freshness, `ops.nightly_verdicts`, failed Dagster run errors, manual pipeline and backtest trigger controls, trigger queues across all universes (`ops.pipeline_trigger_requests`), and append-only pointer rollback (`POST /api/v1/admin/head-rollback` -> `ops.head_rollback_requests`).
  - **DataHub Truth (`/admin/datahub`)**: Exposes question coverage matrices (`mart.question_coverage_report`, 5 reason categories, per-run deltas), full oracle agree/disagree counts and mismatch samples (`mart.oracle_outcomes`), `critical (fact)` / `fact` output invariants (`ops.output_invariant_results`), confidence reports, vendor quota capacity and 429/1302 trends, and LLM usage/model drift.
  - **Global Settings & Contracts (`/admin/settings`)**: Exposes `environment_contract.v1.json`, `confidence_bands.v1.json`, `mart.freshness_limit`, compute/model-seat budgets, sentinels, and metric-applicability exclusions.
  - **Strategy & Research Consumption (`/research/*`)**: Pure research consumption (renders backend-computed head freshness/age limits, three-state availability, historical backtest cutoffs, returns, drawdowns, and live Dagster execution verdicts without operational trigger controls).
  - **Read-only Admin MCP Tools (`admin_ops_snapshot`, `admin_datahub_facts`)**: Expose structured facts and prior-run diffs directly from the same Postgres views to AI reviewers and Staging verification.
- Single Source of Truth: Operational and data plane truth lives in Postgres `ops` and `mart` models. Migrations create each table/view (`mart.served_head`, `mart.question_coverage_report`, `ops.output_invariant_results`, `ops.nightly_verdicts`). Web surfaces and health/MCP endpoints read these models directly.

### 3. Issue Lifecycle and Verification
- An agent or engineer independently verifies the change on staging. The verifier reads the relevant dashboard or runs a read-only probe (SQL rows, HTTP responses). Then `/close` walks the acceptance rows (ADR A2) and the issue closes.
- Production state is not an issue state. The release process that infra2 owns records it (the document `ops.pipeline.md` in the docs/ssot folder of wangzitian0/infra2).
- Reopening is driven by physical regressions identified on the dashboard or by operational failure, not by arbitrary comment text parsers.

## Consequences

- **Restored Velocity**: Pull requests are no longer held hostage by third-party reviewer hiccups. PR #1198 is unblocked and merged immediately.
- **Truth Surfaced in Sunlight**: Substantive defects can no longer hide behind green syntactical checks; they are prominently visualized on operational dashboards.
- **Reduced Noise**: Automated alerts stop creating spurious issue tracker churn for known soak periods, focusing attention on genuine operational breakages.
- **Release age is a dashboard row** (#1412): `deploy-freshness.yml`, `tools/deploy_freshness.py` and `tools/datahub_freshness.py` are deleted. The daily `delivery-report.yml` shows release age, walk evidence, nightly verdicts and the deploy streak. It opens no issue. <!-- doc-drift: history -->
