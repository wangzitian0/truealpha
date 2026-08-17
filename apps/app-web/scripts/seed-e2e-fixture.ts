#!/usr/bin/env bun
/**
 * #494 criterion 1: the fixture `e2e/walk-tree.mjs` needs to have anything to
 * assert against. TEST-ONLY — never invoked from a deployed path (AGENTS.md
 * rule 6); `ci-web.yml` is its single caller, against that workflow's own
 * throwaway Postgres service container.
 *
 * Seeds two things, both required:
 *
 * 1. **Two principals with credentials.** `scripts/seed-principal-credential.ts`
 *    only ever adds a credential to a principal that already exists, and
 *    nothing in the repository creates a tenant or principal — so this does,
 *    for `member` and `administrator`, which is what the route-group gates
 *    (#371/#493) discriminate on.
 *
 * 2. **The frozen twin-parity canon** (`libs/contracts/conformance/
 *    strategy_run_parity.json`, #482) into `mart.strategy_runs` /
 *    `mart.strategy_decisions`, verbatim. Deliberately NOT invented rows: that
 *    file is already the contract both language consumers assert against, so
 *    the browser suite and the parity test see the same bytes. Its two
 *    decisions (one `selected`, one `excluded`) take every research surface to
 *    `ready`.
 *
 *    This part is about COVERAGE, not about passing — measured, not assumed:
 *    against a migrated database carrying principals but no mart rows the suite
 *    is green 11/11, because each surface renders an `unavailable` notice and
 *    the claim ceiling is unconditional on both strategy-output pages. Green
 *    over four words is not a walk. Without this seed, assertion (a) has no
 *    rendered entity to scan and every assertion the stacked issues add walks
 *    empty pages.
 *
 * KNOWN LIMITATION, recorded rather than papered over: this does not make
 * assertion (a) ("no visible raw entity id on a research first screen") a real
 * check. That needs an issuer whose id is LEI-shaped AND a row in
 * `mart.entity_display_resolution`, which is a view over
 * `staging.topt_core_snapshot_members` — whose insert trigger requires durable
 * normalized-payload lineage for every observation id. Fabricating that chain
 * would be fabricating capture evidence, so this does not. Assertion (a)
 * passes vacuously here (the canon's ids are `issuer:zeta`-shaped, which the
 * suite's pattern does not match) and stays a real check only where real
 * snapshots exist. See #494.
 *
 * Rerun semantics differ by table, deliberately:
 *   - `mart.*` is ON CONFLICT DO NOTHING. Those tables carry append-only
 *     triggers, so DO UPDATE is not an option and a rerun must be a no-op.
 *   - `app.principal_credentials` UPSERTS, matching
 *     `seed-principal-credential.ts`. E2E_PASSWORD is generated per CI run, so
 *     DO NOTHING would leave a stale hash on any database that already has the
 *     row and every login in the suite would then fail — a fresh CI service
 *     container hides that, a local rerun does not.
 *
 * `--principals-only` seeds the login identities and stops. #580: the browser
 * suite ran against one data state, and every UI defect of the 2026-07 review
 * was in the other one — the claim ceiling dropped when no run existed, the
 * run table drew a bare header when the list was empty, the funnel vanished
 * when it could not be computed. A suite seeded with good data sees none of
 * them, because the branch it walks is the branch whoever wrote the page was
 * looking at.
 *
 * Usage (see ci-web.yml):
 *   DATABASE_URL=postgresql://… E2E_PASSWORD=… bun run scripts/seed-e2e-fixture.ts
 *   DATABASE_URL=…  … bun run scripts/seed-e2e-fixture.ts --principals-only
 */

import { readFileSync } from "node:fs";
import { Pool } from "pg";
import { hashPassword } from "../src/server/auth/security";

export const E2E_TENANT_ID = "tenant:e2e";
export const E2E_PRINCIPALS = [
  { principalId: "principal:e2e:member", email: "member@e2e.invalid", kind: "member" },
  { principalId: "principal:e2e:admin", email: "admin@e2e.invalid", kind: "administrator" },
] as const;

/** Resolved from this file, not from the process cwd, so the seeder works
 * whether it is invoked from `apps/app-web` or the repository root. */
const CANON_URL = new URL(
  "../../../libs/contracts/conformance/strategy_run_parity.json",
  import.meta.url,
);

interface CanonRun {
  strategy_run_id: string;
  content_sha256: string;
  strategy_key: string;
  strategy_version: string;
  definition_content_sha256: string;
  corpus_sha256: string;
  claim_ceiling: string;
  executed_at: string;
}

interface CanonDecision {
  strategy_decision_id: string;
  content_sha256: string;
  issuer_id: string;
  cutoff_at: string;
  capital_adjusted_labor_efficiency: string | null;
  tier: string | null;
  current_price_to_sales: string | null;
  target_price_to_sales: string | null;
  valuation_gap: string | null;
  eligible: boolean;
  outcome: string;
  exclusion_reason: string | null;
  rank: number | null;
  target_weight: string | null;
}

async function main() {
  const principalsOnly = process.argv.includes("--principals-only");
  const connectionString = process.env.DATABASE_URL;
  if (!connectionString) {
    console.error("DATABASE_URL is not set.");
    process.exit(1);
  }
  const password = process.env.E2E_PASSWORD;
  if (!password || password.length < 12) {
    console.error("E2E_PASSWORD is not set, or is shorter than 12 characters.");
    process.exit(1);
  }

  const canon = JSON.parse(readFileSync(CANON_URL, "utf8")) as {
    report: { run: CanonRun; decisions: CanonDecision[] };
  };
  const { run, decisions } = canon.report;

  const pool = new Pool({ connectionString });
  const client = await pool.connect();
  try {
    await client.query("begin");

    await client.query("insert into app.tenants (tenant_id) values ($1) on conflict do nothing", [
      E2E_TENANT_ID,
    ]);
    for (const principal of E2E_PRINCIPALS) {
      await client.query(
        "insert into app.principals (principal_id, tenant_id, principal_kind) values ($1, $2, $3) " +
          "on conflict (principal_id) do nothing",
        [principal.principalId, E2E_TENANT_ID, principal.kind],
      );
      await client.query(
        "insert into app.principal_credentials (principal_id, email, hashed_password) values ($1, $2, $3) " +
          "on conflict (principal_id) do update set email = excluded.email, hashed_password = excluded.hashed_password",
        [principal.principalId, principal.email, await hashPassword(password)],
      );
    }

    if (principalsOnly) {
      await client.query("commit");
      console.log(
        `Seeded ${E2E_PRINCIPALS.length} principals only — mart left empty, so the walk sees ` +
          `every surface's absent state (#580).`,
      );
      return;
    }

    await client.query(
      `insert into mart.strategy_runs
         (strategy_run_id, content_sha256, strategy_key, strategy_version,
          definition_content_sha256, corpus_sha256, claim_ceiling, executed_at)
       values ($1, $2, $3, $4, $5, $6, $7, $8)
       on conflict (strategy_run_id) do nothing`,
      [
        run.strategy_run_id,
        run.content_sha256,
        run.strategy_key,
        run.strategy_version,
        run.definition_content_sha256,
        run.corpus_sha256,
        run.claim_ceiling,
        run.executed_at,
      ],
    );

    for (const decision of decisions) {
      await client.query(
        `insert into mart.strategy_decisions
           (strategy_decision_id, content_sha256, strategy_run_id, issuer_id, cutoff_at,
            capital_adjusted_labor_efficiency, tier, current_price_to_sales,
            target_price_to_sales, valuation_gap, eligible, outcome, exclusion_reason,
            rank, target_weight)
         values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
         on conflict (strategy_decision_id) do nothing`,
        [
          decision.strategy_decision_id,
          decision.content_sha256,
          run.strategy_run_id,
          decision.issuer_id,
          decision.cutoff_at,
          decision.capital_adjusted_labor_efficiency,
          decision.tier,
          decision.current_price_to_sales,
          decision.target_price_to_sales,
          decision.valuation_gap,
          decision.eligible,
          decision.outcome,
          decision.exclusion_reason,
          decision.rank,
          decision.target_weight,
        ],
      );
    }

    await client.query("commit");
  } catch (error) {
    await client.query("rollback").catch(() => {});
    throw error;
  } finally {
    client.release();
    await pool.end();
  }

  console.log(
    `Seeded ${E2E_PRINCIPALS.length} principals (${E2E_PRINCIPALS.map((p) => p.kind).join(", ")}) ` +
      `and the frozen parity canon: run ${run.strategy_run_id.slice(0, 24)}… with ${decisions.length} decisions.`,
  );
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
});
