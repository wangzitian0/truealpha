<!-- WS_STATIC_START adapter=rules-v2 inputs=d31e58d39b4f7cd00b920161828f00006291acdff948428cc51bd9ea1d447daa -->
<!-- Generated file: do not edit by hand. These rules are maintained in the owner's rule source and re-rendered here. -->

## Engineering discipline

- **Measure the physical system first.** Inspect the system with read-only probes (`time cmd`, process chains, file locks) before proposing architecture. Never combine validation and mutation in one command. Measure, read the result, then decide.
- **Green does not prove truth.** The tested system writes its own unit tests, CI, and issue states. Cross-check critical conclusions against two external sources not written by this repository.
- **Classify checks (`Gate -> Dependency -> Fact -> None`).** Keep falsifiable tests for security or release gates; report drift and metrics on the Fact Board. Never hide assertions inside `if` branches, match prose with `indexOf`, or shadow test names.
- **Falsifiable documentation and assertions:** State test requirements, error messages, and specifications in concrete ASD-STE100 terms without vague qualifiers.
- **One source of truth:** Keep one authoritative definition per core fact. Repeated hardcoding and scattered configuration invite drift.
- **Clean up during migration:** After the new mechanism is live and equivalence is proved, remove its predecessor, obsolete files, and dead code in the same change. Define contracts first and derive CI from them.
- **Deletion can leave guards green and empty:** A guard for an old structure can stop checking anything after deletion. Check each guard and remove it or redirect it to the new structure; green tests alone do not prove safe deletion.
- **Fix an owner hold by deletion:** Delete the rule or gate branch that holds non-production work. Do not add logic that reads a command to permit it. Such logic lets unsafe commands through.
- **Define guard scope from what it must govern, not from today's passing tree.** Let the guard fail on existing violations, then repair them. A guard never seen failing is not yet evidence of protection.
- **Worktree self-sufficiency:** Every worktree must resolve its dependencies, toolchain, skills, and configuration internally. Tools and tests must not navigate upward with `../..` to locate files in parent checkouts.
- **Local and remote test segregation:** Before a PR, run only the focused tests of the change in one process (`pytest <file>::<test> -x`). The full suite, parallel runs and coverage run on remote CI only.
- **Bounded file reads:** Pass `StartLine` and `EndLine` (`<= 800` lines per call) when reading existing files with `read_file` or `view_file`.

## Delivery and merge

- **ASD-STE100 specification for all artifacts:** Write committed artifacts, rules, code, comments, commits, and docs in ASD-STE100 English. Keep instructions `<= 20` words and descriptions `<= 25` words in active voice without vague qualifiers. Operational skills (`skills/**/SKILL.md`) follow the target repository's language policy.
- **Deliverable format:** Deliver a mergeable PR or a traceable issue, rather than a process-only report. Use issues and PRs as the collaboration bus. Search for an existing similar issue before creating one; update it if it exists. Write a new issue to the issue form of its type in `.github/ISSUE_TEMPLATE/` of the repository, else of its owner's `.github` repository.
- **Fail fast left to right:** Put the cheapest and likeliest failure checks first.
- **Review standing authorization:** Resolve a review thread after verifying in source code (not redacted diffs) that it is fixed or obsolete. Never resolve actionable or unverified feedback. Turn false reports into falsifiable invariant tests.
- **Weighted review gates:** Each repository defines its own severity weights and blocking thresholds. Read literal `severity: <level>` tags; do not infer severity from prose.
- **Merge when ready:** Once all merge conditions pass, merge and continue from the latest main rather than piling up divergent branches.
- **Conflict-free merge window:** Merge conflict-free PRs (`mergeable=MERGEABLE`) within 24 hours of `main` without rebasing; post-merge CI validates `main`. Rebase on conflicts or after 24 hours.
- **Delivery state invariant:** Never declare complete, done, or finished before merge to main and a verified Business Reality Probe (`ws-delivery-status`). State labels are `'Draft'`, `'Local verified'`, `'In review'`, `'Stage 1 Landed'`, and `'VERIFIED_COMPLETE'`.
- **Anti-exclusion and reality invariant:** Exclusion is not resolution. Never exclude failing entities or add exemptions to manufacture green checks. A task is complete only when a physical reality probe confirms non-empty execution.
- **Three-stage deployment prerequisite:** Stage 1 (merge to main) is a non-bypassable physical prerequisite for Stage 2 (staging) and Stage 3 (production). If HEAD is not on main, fail closed and refuse any deployment request.
- **Deploy provenance invariant:** Before dispatching any deploy workflow, physically verify that the target ref or tag contains current branch commits (`git merge-base --is-ancestor HEAD <target>`). Never deploy an older tag as completion evidence for current work.

## Production reservation

The single definition of the production reservation. Other tiers, skills and gates point here.

- **Only a production change returns to the owner.** A production change alters what production runs or stores (deploy, promote or apply to production; production secret write; manual production data write or deletion; production DNS, route or host change; unrecallable release; or scheduled unapproved production code). Each Repo tier names its production paths. A path can be a merge or a release-checkout pull.
- **Approval:** the owner approves the exact commit SHA or release tag of each production change, and is present when it runs. A "deploy" reply covers only the release that it names. Immediately before a production dispatch, get a live reply from the owner.
- **Self-guard:** an edit that removes, narrows or redefines the production reservation needs the owner's approval of its head SHA. This includes an edit that makes fewer production runs wait for the owner (deploy-path approval check, production job classification, or production environment settings). It also covers removing or weakening a pin of this section, its pin test, or the required check that runs the test.
- **Everything else is the agent's:** in any repository the owner owns, every non-production change (including `dev_env` outside the self-guard), staging and canary deploy, release tag, rule text, skill, gate/release code, and required-check edit. Before merging gate or release code, an independent read-only reviewer inspects the head SHA and states whether the gate gets stricter, equal or looser. A looser edit in the self-guard follows the self-guard.
- **Every other hold is a defect:** a rule, skill, label or gate holding non-production work for the owner is a defect. Do the work, then fix the hold. `ws_owner_scope` fails on it.

## Runtime safety

- **Reason from the worst case.** For environment changes, wrappers, redirection, or interception rules, check for no-TTY deadlock in CI or child processes, concurrent shared-file truncation/races, and network or cold-start failure cascades. Reject a proposal whose lack of backlash cannot be established.
- **Treat three hidden green failures as defects:** WRONG FORMULA (an incorrect formula passes assertions), GREEN-WHILE-EMPTY (filtering removes all output but reports success), and STALE-REPORTED-AS-FRESH (old data is labeled fresh). Implausible output is evidence of a defect.
- **Protect ambient services.** Default unit tests and subagent tasks must not destructively act on host ports, shared background processes, or development databases (`DROP`, `TRUNCATE`, `--clear`, forced restart). Resets require an isolated sandbox/worktree with a dedicated random port, or an explicit `CI=true` or `ALLOW_CLEAR_TEST=1` guard; otherwise skip safely with a warning.
- **Two triggers:** Every background job or batch process needs both scheduled execution and manual replay.
- **Do not steal CD locks:** A trigger is instant but publication is delayed. Do not interrupt a running deployment; the next run must coalesce commits accumulated while it was busy.

# TrueAlpha Agent Contract

> **Rule source**: This file is generated. Change its dev_env source and pass the rule-text checklist; do not hand-edit it.
> **Repository language**: Code, commits, branches, pull requests, and issues are written
> in English. User-facing conversation may follow the user's language.
> **Architecture authority**: `init.md` is the sole authority for architecture, public
> contracts, and product scope.

## How work happens

1. **One issue, one PR.** Every PR binds exactly one issue. The issue can be an umbrella issue.
   Use `Closes #N` when the merge completes the issue. Use `Refs #N` when staging evidence or
   other PRs remain. An automatic rule-render PR (`ws-render-pr`) binds to the dev_env commit
   that its body names. Keep PRs small and reviewable.
2. **Issues are the shared coordination surface.** Record verified findings, decisions, and
   next actions on the issue as you learn them.
3. **Parallel agents** work on disjoint files. Prefix issue and PR titles with your workspace
   name (for example `[truealpha-factors]`). If two lanes need the same file, coordinate through
   the issue instead of racing. Shared surfaces (migrations, registries, public exports,
   lockfiles) deserve a heads-up comment first. File a finding outside your lane with its evidence
   and hand it to the owning lane: recording it is in scope, adopting it is not.
4. **Before starting**: `git status --short --branch`, sync `main`, check open issues and
   PRs for the same work. **Strong dashboards, weak gates**: three classes of
   gate exist. Class 1, the merge gate, covers compilation, type check, backward-compatible
   migration, secrets scan, focused tests, and the review-finding budget below. Class 2, the
   post-deploy effect probe, shows that a deployed change takes effect and that `main` still
   deploys. Class 3 requires the staging evidence of a release before its production deploy.
   Everything else is a dashboard.
   Third-party and AI reviewer feedback is optional and non-blocking: reviewer platform
   errors, timeouts, and unparseable reviews warn and never block. Unresolved review threads
   use the unified weighted `severity:` score (`>= 1.0` blocks; see `prr` skill). Live dashboards
   (`/admin/datahub`, `/research/backtest`, Postgres `mart` health tables) govern
   observability, not regexes on PR descriptions or issue comments. Once a PR is merge-ready,
   the agent that owns it merges it. That covers gate code, workflows and release code once
   the review that the Root section `Production reservation` requires is on the PR. That review
   is part of class 1. Production here (`docs/release-protocol.md`): the infra2 workflow
   `reconcile-iac-inputs.yml` with `promote_prod=true` and the infra2 workflow `deploy.yml`
   with `type=prod`. The owner-approval checks of those infra2 paths are part of the
   reservation itself.
5. **Data and evidence stay verifiable.** Captured corpora, snapshots, handoff records,
   and evaluation evidence carry content hashes so a replay provably uses the same bytes.
   Records live under `governance/` (see its README); they document what happened and are
   never a merge gate.
6. **Sub-issue closure and Staging verification.** A sub-issue closes after Stage 1 merge
   and a passing `smoke` reality probe (`ws-delivery-status --assert-complete --reality-probe`,
   with real captured data whenever testing Staging/Production); `infra2` `release-train`
   handles Staging verification on real captured data and reopens on regression.
   Fixture data lives in tests only (`*_fixture`, never scheduled or reachable from a deployed
   route). An acceptance criterion is a check that runs again. The skills `smoke` and `close` run acceptance; issue and PR text do not. Production state is not an issue state. (`init.md` section 0; #429, #434.)
7. **Classify checks before adding them (ordered MECE rule; `dev_env#782`).** Evaluate every check in this exact order:
   1) **Gate** (blocks PR): stops production-reservation bypass, privilege leaks, secrets, Moomoo trading calls, or non-idempotent migrations.
   2) **Dependency / Smoke** (CI contract or Staging smoke): validates external boundaries (Postgres, S3, containers, manifests, Dagster, HTTP/SSR, vendors).
   3) **Fact** (dashboard only): records metrics or data quality on `/admin`, `/admin/datahub`, and admin MCP tools (`admin_ops_snapshot`, `admin_datahub_facts`). Never blocks merges or releases.
   4) **None** (delete): pure logic branches, prose regexes, AST static-police checks, count ratchets, or fake-DOM E2E proxies.
   Never add source-text regex, line-count, or AST static-police tests (`ruff` and contract tests cover static checks).

Before editing or after compaction, re-read the prompt, run rule 4 checks, and note the issue,
branch, and target files. On handoff, post done, verified, next, and failed items on the issue.

## Architecture red lines (read `init.md` for full details)

- **Secrets:** Never commit `.env`, `*.pem`, tokens, credentials, account IDs, or private hosts; redact live outputs.
- **PIT & Restatements:** Distinguish `valid_time` from `transaction_time` (from source property, never insertion clock; `recorded_at` is ingestion-audit only). Never derive universe sizes, denominators, or freshness as constants (#539). Restatements insert rows with `is_restatement` + `mapping_version`; never update in place. Select `mart` assertions by `source_priority`, never latest row.
- **Factors & Storage:** All computation lives in `common/factors` over provenance-neutral inputs (no vendor branching; composite confidence `<= min` consumed; continuous `0-100` LLM score; `numeric` for money). Consumers read `mart.served_head` (never mutable `latest`). LLM/Web surfaces use read-only `mart` queries (`mart_readonly`), never raw/staging or S3 paths; replay never calls models.
- **Moomoo & Gateways:** All vendor/LLM calls go through the source gateway + `api_call_ledger`. Moomoo is Quote API read-only (all order/trading/unlock operations forbidden; rejected in CI).

## Environments and load on demand

Target topology: Local (`make runtime-up`), GitHub CI, Staging (`infra2` release train), Production (explicit graduation via `infra2`). Staging/Production database and S3 connection details plus bootstrap scripts live in [`init.md`](init.md) and [`docs/release-protocol.md`](docs/release-protocol.md).

- Checks: `make check FILES=<test paths>` runs the static checks on the changed files and the named tests.
- Vendor specifics: `yfinance` is zero-quota base (lower confidence `Low` when single-source; key `(listing, session_date)`); N-PORT positions use CUSIP/ISIN resolved via OpenFIGI into the entity store (`staging.entities`, `staging.entity_aliases`, `staging.entity_relations`).
- Run the narrowest relevant tests first (`make test-python FILES=<test paths>`). CI runs the full suite. Report what was not run and why.
<!-- WS_STATIC_END -->
