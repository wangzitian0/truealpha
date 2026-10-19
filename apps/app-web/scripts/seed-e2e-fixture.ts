#!/usr/bin/env bun
/**
 * #494 criterion 1: the fixture `e2e/walk-tree.mjs` needs to have anything to
 * assert against. TEST-ONLY — never invoked from a deployed path (AGENTS.md
 * rule 6); `ci-web.yml` is its single caller, against that workflow's own
 * throwaway Postgres service container.
 *
 * Seeds three things, all required:
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
 * 3. **One capture run with a lineage** (#1241). The parity canon holds no capture
 *    run, so no seeded decision has a core result. The trace page of every seeded
 *    decision reads "Core result: not recorded". The walk would never reach the
 *    filled "Decision inputs" table. This seed binds one capture run to the seeded
 *    strategy run. It adds one core result with its lineage for the decision that
 *    `/research` lists first. The walk opens that first trace link.
 *
 *    The rows are synthetic and live only in the throwaway CI database. The walk
 *    checks how the page renders a lineage, not whether a capture is real. So this
 *    is the one capture chain that the seed fabricates, and the limitation below
 *    still holds for assertion (a). The seed uses `session_replication_role =
 *    replica`, as `tests/trace-lineage-mart.test.ts` does. It holds the rows that
 *    `mart.topt_core_meta_info` reads and not the capture pipeline.
 *    `scripts/lineage-seed.ts` owns the rows.
 *
 * 4. **Five golden replay issuers with entity display resolution** (#1322).
 *    Seeds the 2026-03-31 cutoff of `strategy_run_preview.v1.json` (`ADM`, `DDOG`,
 *    `JPM`, `NICE`, `SHOP`, covering all four decision outcomes) together with
 *    `staging.entities`, `staging.entity_aliases` (`legacy-id` and `mic-ticker`),
 *    `staging.kg_entities`, and `staging.topt_core_snapshot_members` so that
 *    `mart.entity_display_resolution` resolves all five issuers and assertion (a)
 *    ("no visible raw entity id on a research first screen") is a real check in
 *    PR CI rather than passing vacuously.
 *
 * Rerun semantics differ by table, deliberately:
 *   - `mart.*` is ON CONFLICT DO NOTHING. Those tables carry append-only
 *     triggers, so DO UPDATE is not an option and a rerun must be a no-op.
 *   - The lineage chain of item 3 is seeded once. Its ids are random, so a rerun
 *     finds the binding of the strategy run and skips the chain.
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

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { Pool, type PoolClient } from "pg";
import { hashPassword } from "../src/server/auth/security";
import { hex, seedResult } from "./lineage-seed";

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
const PREVIEW_URL = new URL(
  "../../../libs/contracts/src/truealpha_contracts/data/strategy_run_preview.v1.json",
  import.meta.url,
);
const PREVIEW_CUTOFF = "2026-03-31T23:59:59Z";

export interface PreviewIssuerIdentity {
  entityId: string;
  legacyId: string;
  micTicker: string;
  ticker: string;
  displayName: string;
}

/**
 * #1322 Tier 1: the five golden replay issuers from `core_strategy_replay.py`
 * (`strategy_run_preview.v1.json`). Each issuer has a deterministic entity UUID,
 * an LEI-shaped `legacy-id` matching `e2e/walk-tree.mjs` (`ID_PATTERN`), and a
 * display resolution entry so assertion (a) is a real check in PR CI.
 */
export const PREVIEW_ISSUERS: Record<string, PreviewIssuerIdentity> = {
  "issuer:adm": {
    entityId: "11111111-1111-5111-8111-111111111101",
    legacyId: "issuer:lei:549300ADM00000000001",
    micTicker: "XNYS:ADM",
    ticker: "ADM",
    displayName: "Archer-Daniels-Midland",
  },
  "issuer:ddog": {
    entityId: "11111111-1111-5111-8111-111111111102",
    legacyId: "issuer:lei:549300DDOG0000000002",
    micTicker: "XNAS:DDOG",
    ticker: "DDOG",
    displayName: "Datadog",
  },
  "issuer:jpm": {
    entityId: "11111111-1111-5111-8111-111111111103",
    legacyId: "issuer:lei:549300JPM00000000003",
    micTicker: "XNYS:JPM",
    ticker: "JPM",
    displayName: "JPMorgan Chase",
  },
  "issuer:nice": {
    entityId: "11111111-1111-5111-8111-111111111104",
    legacyId: "issuer:lei:549300NICE0000000004",
    micTicker: "XNAS:NICE",
    ticker: "NICE",
    displayName: "NICE Ltd.",
  },
  "issuer:shop": {
    entityId: "11111111-1111-5111-8111-111111111105",
    legacyId: "issuer:lei:549300SHOP0000000005",
    micTicker: "XNYS:SHOP",
    ticker: "SHOP",
    displayName: "Shopify",
  },
};

function sha256Hex(input: string): string {
  return createHash("sha256").update(input).digest("hex");
}

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
  // #1235: the P/GP pair of a definition 1.0.0 decision; null on a 0.1.0 decision.
  current_price_to_gross_profit: string | null;
  target_price_to_gross_profit: string | null;
  valuation_gap: string | null;
  eligible: boolean;
  outcome: string;
  exclusion_reason: string | null;
  rank: number | null;
  target_weight: string | null;
}

/** The selected decision that `/research` lists first. The page lists selections by cutoff, then issuer id. */
function firstListedSelection(decisions: CanonDecision[]): CanonDecision {
  const selected = decisions
    .filter((decision) => decision.outcome === "selected")
    .sort(
      (a, b) =>
        Date.parse(a.cutoff_at) - Date.parse(b.cutoff_at) ||
        (a.issuer_id < b.issuer_id ? -1 : a.issuer_id > b.issuer_id ? 1 : 0),
    );
  if (selected.length === 0) throw new Error("the seeded strategy run holds no selected decision");
  return selected[0];
}

/**
 * #1241 / #1322: binds one capture run to the seeded strategy run, seeds the 5-issuer
 * entity identity + display resolution chain (`mart.entity_identity` and
 * `mart.entity_display_resolution`), and gives `decision` one core result with a
 * lineage. Returns false when the strategy run is bound already.
 * The connection must be a superuser connection, because only a superuser can set the replica role.
 */
async function seedTraceLineage(client: PoolClient, run: CanonRun, decision: CanonDecision): Promise<boolean> {
  const bound = await client.query(
    "select 1 from mart.strategy_run_capture_bindings where strategy_run_id = $1",
    [run.strategy_run_id],
  );
  if (bound.rows.length > 0) return false;
  // Transaction-local: the foreign keys and insert triggers come back at the commit.
  await client.query("set local session_replication_role = replica");
  const snapshotId = `topt-core-snapshot:${sha256Hex(`e2e-preview-snapshot:${run.strategy_run_id}`)}`;
  for (const identity of Object.values(PREVIEW_ISSUERS)) {
    const listingSymbol = identity.ticker.toLowerCase();
    const listingId = `listing:xnas:${listingSymbol}`;
    const instrumentId = `instrument:${listingSymbol}`;
    await client.query(
      `insert into staging.entities (entity_id, kind, mint_rule, minted_by, birth_scheme, birth_value)
       values ($1, 'issuer', 'uuidv5:v1', 'e2e-seed', 'lei', $2)
       on conflict (entity_id) do nothing`,
      [identity.entityId, identity.legacyId],
    );
    await client.query(
      `insert into staging.entity_aliases
         (entity_id, scheme, value, valid_from, transaction_time, source, raw_ref, method, confidence, mapping_version)
       values
         ($1, 'legacy-id', $2, '2026-01-01', '2026-01-01T00:00:00Z', 'e2e-seed', 'e2e-seed', 'asserted', 1, 'e2e-seed:v1'),
         ($1, 'mic-ticker', $3, '2026-01-01', '2026-01-01T00:00:00Z', 'e2e-seed', 'e2e-seed', 'asserted', 1, 'e2e-seed:v1')
       on conflict do nothing`,
      [identity.entityId, identity.legacyId, identity.micTicker],
    );
    await client.query(
      `insert into staging.kg_entities (id, entity_type, display_name)
       values ($1, 'company', $2)
       on conflict (id) do nothing`,
      [identity.entityId, identity.displayName],
    );
    await client.query(
      `insert into staging.topt_core_snapshot_members
         (snapshot_id, instrument_id, issuer_id, listing_id, observation_ids, member_sha256, factor_input)
       values ($1, $2, $3, $4, $5, $6, '{}'::jsonb)
       on conflict (snapshot_id, issuer_id) do nothing`,
      [
        snapshotId,
        instrumentId,
        identity.entityId,
        listingId,
        ["obs:1", "obs:2", "obs:3", "obs:4"],
        sha256Hex(`member:${identity.entityId}`),
      ],
    );
    await client.query(
      `insert into mart.issuer_analyst_ratings
         (run_id, issuer_id, cutoff, consensus_rating, analysts_count, buy_count, hold_count, sell_count,
          confidence, reason_codes, extractor, availability_status, source_evidence_status, factor_validation_status)
       values ($1, $2, $3, 4.35, 18, 12, 5, 1, 0.88, $4, 'origin:moomoo:v1', 'available', 'verified', 'accepted')
       on conflict (run_id, issuer_id) do nothing`,
      [run.strategy_run_id, identity.entityId, PREVIEW_CUTOFF, ["target_price:195.00", "consensus:buy"]],
    );
    await client.query(
      `insert into mart.issuer_supply_chain_exposure
         (run_id, issuer_id, cutoff, exposure_score, direct_partners, suppliers_count, customers_count,
          max_partner_share, confidence, reason_codes, extractor, availability_status, source_evidence_status, factor_validation_status)
       values ($1, $2, $3, 0.72, 6, 3, 3, 0.34, 0.85, $4, 'graph:kg-edges:v1', 'available', 'verified', 'accepted')
       on conflict (run_id, issuer_id) do nothing`,
      [run.strategy_run_id, identity.entityId, PREVIEW_CUTOFF, ["supplier:tsmc", "customer:msft"]],
    );
  }
  const captureRunId = `capture-run:${hex()}`;
  const matchedIssuer = Object.values(PREVIEW_ISSUERS).find((item) => item.entityId === decision.issuer_id);
  const symbol = (matchedIssuer?.ticker ?? decision.issuer_id.split(":").pop() ?? "e2e").toLowerCase();
  await seedResult(client, {
    captureRunId,
    entityId: decision.issuer_id,
    cutoff: decision.cutoff_at,
    resultHashPrefix: "",
    createdAt: run.executed_at,
    grossProfit: "210000000",
    listings: [{ listingId: `listing:xnas:${symbol}`, instrumentId: `instrument:${symbol}`, shares: "10000000", close: "40" }],
  });
  await client.query(
    "insert into mart.strategy_run_capture_bindings (capture_run_id, strategy_run_id, bound_by) values ($1, $2, 'tick')",
    [captureRunId, run.strategy_run_id],
  );
  return true;
}

async function seedRunAndDecisions(client: PoolClient, run: CanonRun, decisions: CanonDecision[]): Promise<void> {
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
          rank, target_weight, current_price_to_gross_profit, target_price_to_gross_profit)
       values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)
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
        decision.current_price_to_gross_profit,
        decision.target_price_to_gross_profit,
      ],
    );
  }
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
  const preview = JSON.parse(readFileSync(PREVIEW_URL, "utf8")) as {
    corpus_sha256: string;
    strategy_id: string;
    decisions: Array<Omit<CanonDecision, "strategy_decision_id" | "content_sha256">>;
  };
  const previewRunHash = sha256Hex(`e2e-preview-run:${preview.corpus_sha256}:${PREVIEW_CUTOFF}`);
  const previewRun: CanonRun = {
    strategy_run_id: `strategy-run:${previewRunHash}`,
    content_sha256: sha256Hex(`e2e-preview-run-content:${preview.corpus_sha256}:${PREVIEW_CUTOFF}`),
    strategy_key: preview.strategy_id,
    strategy_version: canon.report.run.strategy_version,
    definition_content_sha256: canon.report.run.definition_content_sha256,
    corpus_sha256: preview.corpus_sha256,
    claim_ceiling: "preview",
    executed_at: "2027-06-02T00:00:00Z",
  };
  const previewDecisions: CanonDecision[] = preview.decisions
    .filter((item) => item.cutoff_at === PREVIEW_CUTOFF)
    .map((item) => {
      const identity = PREVIEW_ISSUERS[item.issuer_id];
      if (!identity) {
        throw new Error(`unmapped preview issuer ${item.issuer_id} in strategy_run_preview.v1.json`);
      }
      const decisionHash = sha256Hex(JSON.stringify({ ...item, entity_id: identity.entityId }));
      return {
        ...item,
        issuer_id: identity.entityId,
        content_sha256: decisionHash,
        strategy_decision_id: `strategy-decision:${decisionHash}`,
        current_price_to_gross_profit: item.current_price_to_gross_profit ?? null,
        target_price_to_gross_profit: item.target_price_to_gross_profit ?? null,
      };
    });

  const pool = new Pool({ connectionString });
  const client = await pool.connect();
  let lineageSeeded = false;
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

    await seedRunAndDecisions(client, canon.report.run, canon.report.decisions);
    await seedRunAndDecisions(client, previewRun, previewDecisions);
    lineageSeeded = await seedTraceLineage(client, previewRun, firstListedSelection(previewDecisions));

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
      `and the 5-issuer golden preview run ${previewRun.strategy_run_id.slice(0, 24)}… with ${previewDecisions.length} decisions. ` +
      (lineageSeeded
        ? `Entity display resolution for 5 issuers and one core result with a lineage (#1241, #1322).`
        : `The lineage chain was seeded before.`),
  );
}

if (process.argv[1]?.includes("seed-e2e-fixture")) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : String(error));
    process.exit(1);
  });
}
