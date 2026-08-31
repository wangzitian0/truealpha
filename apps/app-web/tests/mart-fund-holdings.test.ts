/**
 * loadFundHoldings against a real local Postgres (skips gracefully without
 * one; ci-web provisions one and sets TRUEALPHA_REQUIRE_RUNTIME=1). Proves the
 * reader sees mart.fund_holdings through mart_readonly and answers only the
 * NEWEST vintage per fund, weights ordered as filed.
 *
 * Seeds are idempotent inserts under fixed test ids: staging.fund_holding_facts
 * is append-only (reject_mutation), so cleanup-by-DELETE is impossible by
 * design — re-runs land on the unique vintage key instead.
 */

import { Client } from "pg";

import { loadFundHoldings } from "../src/server/mart/fund-holdings";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const REQUIRE_DB = Boolean(process.env.DATABASE_URL || process.env.TRUEALPHA_REQUIRE_RUNTIME);
process.env.DATABASE_URL ??= "postgresql://postgres:postgres@localhost:5432/truealpha";
const DATABASE_URL = process.env.DATABASE_URL;

const FUND = "etf:series:TEST-HOLDINGS-READER";
const HOLDING_A = "company:isin:TEST00000001";
const HOLDING_B = "company:isin:TEST00000002";
const ENRICHED_ISSUER = "issuer:cik:0009990001";

async function reachable(): Promise<Client | null> {
  const client = new Client({ connectionString: DATABASE_URL, connectionTimeoutMillis: 3000 });
  try {
    await client.connect();
    return client;
  } catch (error) {
    await client.end().catch(() => {});
    if (REQUIRE_DB) throw new Error(`configured Postgres is unreachable: ${String(error)}`);
    console.log("mart-fund-holdings: no local Postgres and TRUEALPHA_REQUIRE_RUNTIME unset — SKIP");
    return null;
  }
}

async function seed(client: Client): Promise<void> {
  for (const [id, kind, name] of [
    [FUND, "etf", "Test Reader Fund"],
    [HOLDING_A, "company", "Alpha Holding"],
    [HOLDING_B, "company", "Beta Holding"],
    [ENRICHED_ISSUER, "company", "Beta Holding Inc"],
  ]) {
    await client.query(
      "insert into staging.kg_entities (id, entity_type, display_name) values ($1, $2, $3) on conflict (id) do nothing",
      [id, kind, name],
    );
  }
  const line = `
    insert into staging.fund_holding_facts
      (fund_id, holding_id, holding_name, report_period, transaction_time,
       cusip, isin, lei, balance, value_usd, percent_of_net_assets, confidence, raw_ref)
    values ($1, $2, $3, $4, $5, null, $6, null, 10, $7, $8, 1.0, 'raw.fetches:0')
    on conflict do nothing`;
  // older vintage: one line the reader must NOT return
  await client.query(line, [FUND, HOLDING_A, "Alpha Holding", "2026-03-31", "2026-05-01T00:00:00Z", "TEST00000001", 100, 60.0]);
  // newest vintage: two lines, weights deliberately out of name order
  await client.query(line, [FUND, HOLDING_B, "Beta Holding", "2026-06-30", "2026-08-01T00:00:00Z", "TEST00000002", 300, 55.5]);
  await client.query(line, [FUND, HOLDING_A, "Alpha Holding", "2026-06-30", "2026-08-01T00:00:00Z", "TEST00000001", 200, 44.25]);
  // Beta's ISIN is enriched (isin + ticker vintages on the cik-keyed issuer);
  // Alpha stays unresolved — the valuation view must answer both honestly.
  const identifier = `
    insert into staging.kg_identifiers
      (entity_id, source, identifier_type, identifier_value, valid_time, transaction_time, confidence, raw_ref)
    values ($1, 'test', $2, $3, daterange('2026-08-01', null, '[)'), '2026-08-01T00:00:00Z', 0.98, 'raw.fetches:0')
    on conflict (source, identifier_type, identifier_value, transaction_time) do nothing`;
  await client.query(identifier, [ENRICHED_ISSUER, "isin", "TEST00000002"]);
  await client.query(identifier, [ENRICHED_ISSUER, "ticker", "TSTB"]);
}

const client = await reachable();
if (client) {
  try {
    await seed(client);

    const funds = await loadFundHoldings();
    const fund = funds.find((candidate) => candidate.fundId === FUND);
    assert(fund, "seeded fund is readable through the mart view");
    assert(fund.fundName === "Test Reader Fund", "fund display name joins from the KG registry");
    assert(fund.reportPeriod === "2026-06-30", "only the newest vintage answers");
    assert(fund.filedOn === "2026-08-01", "filed date is the vintage's transaction time");
    assert(fund.lines.length === 2, `newest vintage has exactly its own lines, got ${fund.lines.length}`);
    assert(fund.lines[0]?.holdingName === "Beta Holding", "lines order by filed weight, descending");
    assert(fund.weightSumPct === "99.75", `weights sum as filed, got ${fund.weightSumPct}`);
    assert(
      fund.lines.every((row) => row.holdingName !== "Alpha Holding" || row.weightPct !== "60"),
      "the older vintage's weight never leaks into the newest answer",
    );

    const valuation = await client.query(
      "select holding_name, ticker, listing_id from mart.fund_holdings_valuation where fund_id = $1 order by holding_name",
      [FUND],
    );
    assert(valuation.rows.length === 2, "valuation view answers the newest vintage only");
    const [alpha, beta] = valuation.rows;
    assert(alpha.ticker === null && alpha.listing_id === null, "unresolved ISIN carries no guessed listing");
    assert(beta.ticker === "TSTB" && beta.listing_id === "listing:xnas:tstb", "enriched ISIN resolves to its listing");

    console.log("mart fund-holdings reader passed");
  } finally {
    await client.end().catch(() => {});
  }
}
