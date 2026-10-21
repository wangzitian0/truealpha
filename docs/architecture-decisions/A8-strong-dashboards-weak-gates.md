# A8 — Strong Dashboards, Weak Gates: Observability over Deadlocking Blockers

Status: Accepted (in force; `AGENTS.md` Rule 4 and Rule 6 updated 2026-10-12 and revised 2026-10-21)
Date: 2026-10-12
Extends: `AGENTS.md` Rule 4 ("How work happens"), Rule 6 ("Closed means verified on staging with real data"), and ADR A2

## Context

Over successive iterations, defensive engineering mechanisms degenerated into brittle, multi-tiered "hard gates" (强门禁). Rather than preventing defects, these gates produced development paralysis, workarounds, and false security:

1. **External Platform Deadlocks**: `tools/check_pr_ready.py` (deleted in #1237) introduced fail-closed logic that blocked PR merges whenever an external AI reviewer (such as GitHub Copilot) suffered a network timeout, platform outage, or emitted unparseable prose. PR #1198—which implemented necessary GPPE metric naming, was fully tested, and passed all CI checks—was locked for two days due solely to Copilot emitting an "encountered an error" response.
2. **Form over Substance Cargo Cult**: `tools/vision_close_guard.py` and its accompanying GitHub Actions workflows (`vision-close-check.yml` and `vision-close-reopen.yml`) introduced regex-based pull request blockers on GitHub closing keywords and automated bots that reopened closed issues lacking literal Markdown headings (`Deployed call site` and `Real-data output`). This incentivized boilerplate copy-pasting to satisfy the regex, while failing to detect whether the underlying service was healthy.
3. **Metric-to-Gate Inversion and Issue Spam**: `tools/deploy_freshness.py` and `deploy-freshness.yml` inverted an informational age measurement (production deployment older than 14 days, staging older than 3 days) into an exit 1 CI blocker that automatically filed noisy tracker issues (#1202, #1203). This converted normal release soak intervals into pipeline red lights.
4. **The False Green Paradox**: Despite dozens of mechanical gates, substantive physical defects repeatedly escaped detection:
   - A `0.08` hardcoded constant stood in for measured tangible common equity (#1108, #1176);
   - A demo close-price sorting strategy stood in for fundamental factor backtesting (#758);
   - A `not_applicable` grade masked uncomputed PEG metrics on QQQ (#749, #1115).
   Mechanical gates checked only syntax and presence of tokens; they could not check physical reality.

## Decision

We adopt the foundational principle: **"Strong Dashboards, Weak Gates" (看板优于门禁，强看板，弱门禁)**.

### 1. Three gate classes (fail-safe technical baselines only)
A gate belongs to one of three classes. Everything else is a dashboard.
- **Class 1, the merge gate:** compilation/build, typecheck, backward-compatible migrations, secret scanning, and focused unit test execution.
- **Class 2, the post-deploy effect probe:** after a deploy, a read-only probe shows that the change takes effect (SQL rows or an HTTP response).
- **Class 3, staging evidence before a production deploy:** a production deploy of a release waits for the staging evidence of that same release.
- Third-party and AI reviewer feedback is **advisory**: infrastructure timeouts, reviewer platform errors, or unclassified review comments log warnings and never fail-close PR readiness.
- The one stated exception to "everything else is a dashboard" is the review-finding budget of `AGENTS.md` Rule 4 (High = 0, Medium <= 2, Low <= 4).
- Mechanical text policing (regex matching on PR closing keywords, automated bot reopen scripts) is abolished.

### 2. Strong Dashboards (Radical Observability as Primary Governance)
- Reality, quality, and progress are verified through **live, transparent Observability Dashboards**:
  - **DataHub Freshness (`/admin/datahub`)**: Directly exposes pointer timestamps, universe freshness, partition arrival times, and frozen head alerts.
  - **Question Coverage (`/admin/datahub`)**: Renders row-by-row reality across the core questions (verified vs. degraded vs. unavailable with reason), leaving zero room for fake constants or hidden missing cells. It reads `mart.question_coverage_report`.
  - **Strategy & Backtest Observability (`/research/backtest`)**: Exposes historical backtest cutoffs, returns, drawdowns, and live Dagster execution verdicts directly to researchers.
- Single Source of Truth: Operational and data plane truth lives in Postgres `mart` models. A migration creates each one: `mart.served_head` (pointer freshness), `mart.question_coverage_report` (question coverage), and `mart.nightly_verdicts` (nightly check verdicts). The web surfaces above and the health endpoints read these models.

### 3. Issue Lifecycle and Verification
- Issues are closed when staging verifies the change: an agent or engineer reads the relevant dashboard or runs a physical read-only probe (SQL rows / HTTP responses), and `/close` walks the acceptance rows (ADR A2).
- Production state is not an issue state. The release process that infra2 owns records it (`docs/ssot/ops.pipeline.md` in wangzitian0/infra2).
- Reopening is driven by physical regressions identified on the dashboard or by operational failure, not by arbitrary comment text parsers.

## Consequences

- **Restored Velocity**: Pull requests are no longer held hostage by third-party reviewer hiccups. PR #1198 is unblocked and merged immediately.
- **Truth Surfaced in Sunlight**: Substantive defects can no longer hide behind green syntactical checks; they are prominently visualized on operational dashboards.
- **Reduced Noise**: Automated alerts stop creating spurious issue tracker churn for known soak periods, focusing attention on genuine operational breakages.
- **Release age is a dashboard row** (#1412): `deploy-freshness.yml`, `tools/deploy_freshness.py` and `tools/datahub_freshness.py` are deleted. The daily `delivery-report.yml` shows release age, walk evidence, nightly verdicts and the deploy streak. It opens no issue.
