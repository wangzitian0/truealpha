---
name: deploy-evidence
description: TrueAlpha Deploy Evidence & Release Proof Guard. Verifies Staging and Production deployments on real captured data after release promotion.
---

# TrueAlpha Deploy Evidence & Release Guard

> **Core Axiom (`AGENTS.md` Rule 6 & `close` skill)**:
> *"A sub-issue closes after Stage 1 merge and a passing `smoke` reality probe (`ws-delivery-status --assert-complete --reality-probe`, with real captured data whenever testing Staging/Production); `infra2` `release-train` handles Staging verification on real captured data and reopens on regression."*

## 1. When to Activate This Skill
- When auditing Stage 2 Staging verification or Stage 3 Production promotion evidence for a `vX.Y.Z` release tag.
- When confirming deployed execution paths and real captured Postgres `mart` rows.
- Never use this skill to block Stage 1 sub-issue closure that already passed its `smoke` reality probe; it gates deployment and Staging/Production claims.

## 2. The Verification Protocol (Step-by-Step)

### Step 1: Release Workflow, Health & Image Digest Check
Verify that the target release tag (`vX.Y.Z`) and commit SHA are promoted and served on the target environment:
```bash
# 1. Verify the infra2 staging reconcile and deploy workflows succeeded for vX.Y.Z
gh run list -R wangzitian0/infra2 --workflow reconcile-iac-inputs.yml --branch vX.Y.Z --limit 5
gh run list -R wangzitian0/infra2 --workflow deploy.yml --limit 5

# 2. If running directly on the VPS host via SSH, verify container image digests:
docker inspect -f '{{.Config.Image}}' truealpha-dagster-code-server-staging
docker inspect -f '{{.Config.Image}}' truealpha-dagster-daemon-staging
docker inspect -f '{{.Config.Image}}' truealpha-web-staging
docker inspect -f '{{.Config.Image}}' truealpha-llm-staging
```

### Step 2: Dagster & Runtime Liveness Proof
Confirm live service health (`git_sha` and `governed_pointers` in `/api/health`) and verify daemon container status/PostgreSQL heartbeat (do NOT run `dagster-daemon liveness-check`, which spawns a heavy cold CLI start).

### Step 3: Deployed Execution Proof (Reachable Path)
Verify that the capability was invoked via a deployed path. A deployed path is reachable from the Dagster composition root (`dagster_defs.py`) or a deployed service/App entrypoint:
- **For DataHub/Ingestion**: The Dagster job ticked under daemon or sensor, writing to `mart.served_head`.
- **For Web/API**: An HTTP 200 response with typed data from the live service domain (`truealpha.club` or staging subdomain).
- **For Factors**: Factor outputs materialized into `mart` tables and readable via `mart_readonly`.

### Step 4: Real-Data Row Assertion
Never use `*_fixture` outputs as completion evidence!
Query the actual database:
```sql
-- Assert real data rows exist in the target mart table
SELECT run_id, count(*), min(valid_time), max(valid_time)
FROM mart.capture_status
WHERE complete = true
GROUP BY run_id
ORDER BY max(valid_time) DESC
LIMIT 5;
```

## 3. Red Lines (Instant Rejection)
- ❌ Claiming Staging or Production deployment from a `main` merge without matching release proof: **REJECT DEPLOYMENT EVIDENCE**.
- ❌ Green tests using fixture data or mocked responses cited as Staging/Production proof: **REJECT DEPLOYMENT EVIDENCE**.
- ❌ Output says `unavailable`, `unsourced`, or returns a sentinel placeholder / hardcoded confidence stamp (`0.85`, `0.92`, `1.00`) without an explicit scope demotion note (a real computed value with a `low_confidence` continuous score is valid and counts as produced): **REJECT DEPLOYMENT EVIDENCE**.
- ❌ Standing checks supply parameters that deployed callers omit (#284): **REJECT EVIDENCE**.
