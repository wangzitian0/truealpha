/**
 * #362: MartStrategyRunRepository against a real local Postgres (skips
 * gracefully without one). Proves the App reads mart.strategy_runs/
 * strategy_decisions through the mart_readonly role and returns the same shape
 * the Python PostgresStrategyRunRepository does (semantic parity).
 *
 * Inserts obviously-fake rows under a unique strategy_key (the repository opens
 * its own mart_readonly connection, so rows must be committed to be visible),
 * then best-effort deletes them. Run standalone: `bun run tests/mart-strategy-run-repository.test.ts`.
 */

import { randomBytes } from "node:crypto";

import { Client } from "pg";

import { DECISIONS_SQL, MartStrategyRunRepository } from "../src/server/mart/strategy-run-repository";
import type { AccessContext } from "../src/contracts/strategyRun";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const REQUIRE_DB = Boolean(process.env.DATABASE_URL || process.env.TRUEALPHA_REQUIRE_RUNTIME);
// The repository under test reads process.env.DATABASE_URL (via withMartReadonly);
// default it so a bare local run points the read and the seed at the same DB.
process.env.DATABASE_URL ??= "postgresql://postgres:postgres@localhost:5432/truealpha";
const DATABASE_URL = process.env.DATABASE_URL;

const CONTEXT: AccessContext = {
  contextId: "ctx:test",
  principalId: "principal:test",
  tenantId: "tenant:test",
  sessionId: "session:test",
  authenticationMethod: "service_identity",
  issuedAt: "2026-07-19T00:00:00Z",
  expiresAt: "2026-07-19T01:00:00Z",
};

const hex64 = () => randomBytes(32).toString("hex");

async function reachable(): Promise<Client | null> {
  const client = new Client({ connectionString: DATABASE_URL, connectionTimeoutMillis: 3000 });
  try {
    await client.connect();
    return client;
  } catch (error) {
    await client.end().catch(() => {});
    if (REQUIRE_DB) throw new Error(`configured Postgres is unreachable: ${String(error)}`);
    // Local-only escape hatch. ci-web provisions a real Postgres and sets
    // TRUEALPHA_REQUIRE_RUNTIME=1, so in CI this branch is unreachable — an
    // unreachable database fails hard above, and ci-web's grep gate rejects
    // any "— SKIP" line as a second line of defense (#468).
    console.log("mart-strategy-run-repository: no local Postgres and TRUEALPHA_REQUIRE_RUNTIME unset — SKIP");
    return null;
  }
}

const admin = await reachable();
if (admin !== null) {
  // The report DTO pins strategy_id to the Literal "large_model_value_v0" —
  // a run under any other key is schema_mismatch on BOTH twins (#469), so the
  // happy path cannot use a disposable per-test key. Isolation mirrors the
  // Python twin's convention: executed_at = now() outranks any prior
  // committed run, so this test's row is deterministically "the latest".
  const strategyKey = "large_model_value_v0";
  const runId = `strategy-run:${hex64()}`;
  const corpus = hex64();

  try {
    await admin.query(
      `insert into mart.strategy_runs
         (strategy_run_id, content_sha256, strategy_key, strategy_version,
          definition_content_sha256, corpus_sha256, claim_ceiling, executed_at)
       values ($1, $2, $3, 'v0', $4, $5, 'preview', now())`,
      [runId, hex64(), strategyKey, hex64(), corpus],
    );
    // Two decisions, deliberately out of (cutoff, issuer) order to prove the
    // repository's ordering; distinct outcomes/tiers to exercise the mapping.
    await admin.query(
      `insert into mart.strategy_decisions
         (strategy_decision_id, content_sha256, strategy_run_id, issuer_id, cutoff_at,
          capital_adjusted_labor_efficiency, tier, current_price_to_sales, target_price_to_sales,
          valuation_gap, eligible, outcome, exclusion_reason, rank, target_weight, peg)
       values
         ($1, $2, $3, 'issuer:zeta', '2026-03-31T00:00:00Z', '12.5', 'tech', '20', '18.75', '0.5', true, 'selected', null, 1, '1.0', '0.50'),
         ($4, $5, $3, 'issuer:adm',  '2026-03-31T00:00:00Z', null, null, null, null, null, false, 'excluded', 'insufficient_confidence', null, null, null)`,
      [`strategy-decision:${hex64()}`, hex64(), runId, `strategy-decision:${hex64()}`, hex64()],
    );

    const report = await new MartStrategyRunRepository().getLatest(strategyKey, CONTEXT);
    assert("decisions" in report, `expected a report, got unavailable: ${JSON.stringify(report)}`);
    assert(report.source === "mart", `expected source 'mart', got '${report.source}'`);
    assert(report.corpus_sha256 === corpus, "corpus_sha256 must round-trip from mart");
    assert(report.golden_mismatches.length === 0, "mart read carries no golden mismatches");
    assert(report.decisions.length === 2, `expected 2 decisions, got ${report.decisions.length}`);

    // Ordered by (cutoff_at, issuer_id): 'issuer:adm' sorts before 'issuer:zeta'.
    const [first, second] = report.decisions;
    assert(first.issuer_id === "issuer:adm", `expected issuer:adm first, got ${first.issuer_id}`);
    assert(first.outcome === "excluded" && first.eligible === false, "adm is an excluded, ineligible decision");
    assert(first.exclusion_reason === "insufficient_confidence", "adm carries its exclusion reason");
    assert(first.tier === null && first.valuation_gap === null, "adm's null numerics stay null");
    assert(first.confidence === null, "no mart.topt_core_results row at this cutoff; the join yields null");

    assert(second.issuer_id === "issuer:zeta", `expected issuer:zeta second, got ${second.issuer_id}`);
    assert(second.outcome === "selected" && second.tier === "tech", "zeta is a selected tech decision");
    // numeric comes back as a precision-preserving string, never a JS number.
    assert(second.valuation_gap === "0.5" && typeof second.valuation_gap === "string", "numeric stays a string");
    assert(second.target_weight === "1.0", "target_weight round-trips verbatim");
    assert(second.rank === 1, "rank round-trips as an integer");
    // Module 1 (#284): a factor that reaches mart but not the read model is invisible on
    // the surface the owner looks at, which is the gap #434 exists to close.
    assert(second.peg === "0.50", `expected peg '0.50' on the read path, got ${second.peg}`);
    assert(first.peg === null, "an excluded decision carries no PEG");

    // An unknown key reads no run — the same structured outcome the Python repo returns.
    const missing = await new MartStrategyRunRepository().getLatest(`nonexistent-${randomBytes(4).toString("hex")}`, CONTEXT);
    assert(!("decisions" in missing), "an unknown strategy key must be unavailable");
    assert(missing.reason === "no_runs_recorded", `expected no_runs_recorded, got ${missing.reason}`);

    console.log("#362 mart-strategy-run-repository parity passed");
  } finally {
    // Best-effort cleanup so a local run leaves no residue (superuser can DELETE;
    // the read path's mart_readonly role cannot).
    await admin.query("delete from mart.strategy_decisions where strategy_run_id = $1", [runId]).catch(() => {});
    await admin.query("delete from mart.strategy_runs where strategy_run_id = $1", [runId]).catch(() => {});
    await admin.end().catch(() => {});
  }
}

// ---------------------------------------------------------------------------
// #953: an entity-UUID issuer_id is translated to its symbolic identity.
//
// mart.strategy_decisions.issuer_id has held the opaque entity UUID since #928 wrote
// entity coordinates. Both twins served it verbatim, so the MCP `strategy_run` tool
// answered `02587046-dc99-5a44-b811-e2d086a58ccb` where staging v0.0.90 answered
// `issuer:lei:29DX7H14B9S6O3FD6V18`. This is the TypeScript half of the fix: the same
// lateral join to `mart.entity_identity` the Python twin carries, against the same real
// schema, so "behaviourally identical twins" is executed rather than asserted in a
// comment. Red-proven against `select d.issuer_id`: it reads back the bare UUID.
// ---------------------------------------------------------------------------
{
  const client = new Client({ connectionString: DATABASE_URL, connectionTimeoutMillis: 3000 });
  let connected = false;
  try {
    await client.connect();
    connected = true;
  } catch (error) {
    await client.end().catch(() => {});
    if (REQUIRE_DB) throw new Error(`configured Postgres is unreachable: ${String(error)}`);
    console.log("mart-strategy-run-repository #953: no local Postgres and TRUEALPHA_REQUIRE_RUNTIME unset — SKIP");
  }

  if (connected) {
    const strategyKey = "large_model_value_v0";
    const runId = `strategy-run:${hex64()}`;
    // LEI format is enforced by staging.validate_entity_alias (^[0-9A-Z]{18}[0-9]{2}$),
    // and the entity UUID must be the uuidv5 its birth alias derives — so the entity is
    // minted by staging.entity_mint rather than written by hand. A random LEI keeps
    // repeated local runs from colliding on an append-only table.
    const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
    const lei =
      Array.from({ length: 18 }, () => alphabet[Math.floor(Math.random() * alphabet.length)]).join("") +
      String(Math.floor(Math.random() * 100)).padStart(2, "0");
    const legacyId = `issuer:lei:${lei}`;

    try {
      // One transaction: staging.require_entity_birth_alias is a deferred constraint
      // trigger, so the entity and its birth alias must commit together.
      await client.query("begin");
      const minted = await client.query(
        "select staging.entity_mint('issuer', 'lei', $1, 'mart-strategy-run-repository-test') as entity_id",
        [lei],
      );
      const entityUuid: string = minted.rows[0].entity_id;
      await client.query(
        `insert into staging.entity_aliases
           (entity_id, scheme, value, valid_from, transaction_time, source, raw_ref,
            method, confidence, mapping_version)
         values ($1, 'lei', $2, date '2026-01-01', timestamptz '2026-01-01',
                 'mart-strategy-run-repository-test', 'test-ref', 'asserted', 1.0, 'v1'),
                ($1, 'legacy-id', $3, date '2026-01-01', timestamptz '2026-01-01',
                 'mart-strategy-run-repository-test', 'test-ref', 'asserted', 1.0, 'v1')`,
        [entityUuid, lei, legacyId],
      );
      await client.query("commit");

      await client.query(
        `insert into mart.strategy_runs
           (strategy_run_id, content_sha256, strategy_key, strategy_version,
            definition_content_sha256, corpus_sha256, claim_ceiling, executed_at)
         values ($1, $2, $3, 'v0', $4, $5, 'preview', now())`,
        [runId, hex64(), strategyKey, hex64(), hex64()],
      );
      await client.query(
        `insert into mart.strategy_decisions
           (strategy_decision_id, content_sha256, strategy_run_id, issuer_id, cutoff_at,
            eligible, outcome)
         values ($1, $2, $3, $4, '2026-03-31T23:59:59Z', true, 'selected')`,
        [`strategy-decision:${hex64()}`, hex64(), runId, entityUuid],
      );

      const report = await new MartStrategyRunRepository().getLatest(strategyKey, CONTEXT);
      assert("decisions" in report, `expected a report, got ${JSON.stringify(report)}`);
      // A display lookup must never multiply the rows it decorates: mart.entity_identity's
      // staging.kg_entities join can match one entity twice, which is why both twins use
      // LATERAL ... limit 1 rather than a plain join.
      assert(
        report.decisions.length === 1,
        `the identity join multiplied decisions: ${report.decisions.length}`,
      );
      const served = report.decisions[0].issuer_id;
      assert(
        !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(served),
        `issuer_id is still a bare entity UUID: ${served}`,
      );
      assert(served === legacyId, `expected the symbolic issuer identity ${legacyId}, got ${served}`);
      // The SQL, not only the value: a future edit that drops the join but happens to be
      // handed an already-symbolic id would pass the assertions above and leak again.
      assert(
        DECISIONS_SQL.includes("mart.entity_identity ei"),
        "DECISIONS_SQL must resolve the issuer through mart.entity_identity",
      );

      console.log("#953 mart-strategy-run-repository symbolic issuer_id translation passed");
    } finally {
      await client.query("delete from mart.strategy_decisions where strategy_run_id = $1", [runId]).catch(() => {});
      await client.query("delete from mart.strategy_runs where strategy_run_id = $1", [runId]).catch(() => {});
      await client.end().catch(() => {});
    }
  }
}
