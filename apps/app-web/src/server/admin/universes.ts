/**
 * Universe provenance for the Operate world (#539 data-driven universes).
 *
 * Every governed universe head (kind 'universe-list:<etf>'), its version
 * history, and the newest constituent snapshot WITH raw lineage — ticker rows
 * join back to the exact bytes the index operator answered (`raw.fetches`
 * sha256 + object URI), so "where did this universe come from" is a click,
 * not an investigation. Read-only through app_ops_reader, like every loader
 * in this directory.
 */

import type { PoolClient } from "pg";
import { withOpsReader } from "./ops";

export interface UniverseVersionRow {
  kind: string;
  sequence: number;
  note: string;
  advanced_at: string;
  universe_id: string;
  instrument_count: number;
  mapping_sha256: string;
}

export interface UniverseMemberRow {
  ticker: string;
  company_name: string;
  cik: number | null;
  figi: string | null;
  market_cap: string | null;
  as_of: string;
  source: string;
  fetch_sha256: string;
  object_uri: string | null;
}

export interface UniverseOverview {
  heads: UniverseVersionRow[];
  history: UniverseVersionRow[];
  members: Record<string, UniverseMemberRow[]>;
}

const VERSIONS_SQL = `
  select r.kind, r.sequence, coalesce(r.note, '') as note, r.advanced_at::text as advanced_at,
         o.payload->>'universe_id' as universe_id,
         (o.payload->>'instrument_count')::int as instrument_count,
         o.payload->>'instrument_mapping_sha256' as mapping_sha256
  from staging.accepted_rulesets r
  join staging.contract_objects o on o.contract_id = r.contract_id
  where r.kind like 'universe-list:%'
  order by r.kind, r.sequence desc
`;

const MEMBERS_SQL = `
  select f.ticker, f.company_name, f.cik, f.figi, f.market_cap::text as market_cap,
         f.as_of::text as as_of, f.source, r.payload_sha256 as fetch_sha256, r.object_uri
  from staging.etf_constituent_facts f
  join raw.fetches r on r.id = f.raw_fetch_id
  where f.etf_symbol = $1
    and f.as_of = (select max(as_of) from staging.etf_constituent_facts where etf_symbol = $1)
  order by f.ticker
`;

export async function loadUniverses(): Promise<UniverseOverview> {
  return withOpsReader(async (client: Pick<PoolClient, "query">) => {
    const versions = await client.query(VERSIONS_SQL);
    const history = versions.rows as UniverseVersionRow[];
    const heads: UniverseVersionRow[] = [];
    const seen = new Set<string>();
    for (const row of history) {
      if (!seen.has(row.kind)) {
        seen.add(row.kind);
        heads.push(row);
      }
    }
    const members: Record<string, UniverseMemberRow[]> = {};
    for (const head of heads) {
      const etf = head.kind.replace("universe-list:", "");
      const result = await client.query(MEMBERS_SQL, [etf]);
      members[head.kind] = result.rows as UniverseMemberRow[];
    }
    return { heads, history, members };
  });
}
