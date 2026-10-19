/**
 * #494 P0b: consumer-side reads of `mart.entity_display_resolution`
 * (#495 surface 1, migration 0033) — issuer → ticker / display name so no
 * research first screen ever shows a raw LEI. Reads through mart_readonly
 * like every other consumer path; display_name is nullable (the KG carries
 * no names yet), so rendering falls back to ticker, and an issuer missing
 * from the view falls back to its raw id — honesty over invention.
 *
 * #953: the map is keyed by BOTH coordinates an issuer reaches a page under.
 * `mart.entity_display_resolution.issuer_id` is the entity UUID, and that is
 * what `mart.issuer_theme_purity` and the other per-issuer mart tables carry —
 * but `strategy-run-repository.ts` now translates its `issuer_id` through
 * `mart.entity_identity` so the MCP twin stops serving bare UUIDs, and a
 * decision reaches `/research/strategy` under its symbolic `issuer:lei:…` id.
 * One key would leave the other coordinate falling through `entityLabel`'s raw-id
 * fallback, which puts a raw LEI on a first screen — the exact string
 * `e2e/walk-tree.mjs` fails on and the exact reason this module exists. Both keys
 * resolve to the same display, through the same single projection; nothing here
 * parses an id.
 */

import { withMartReadonly } from "@/server/mart/db";

export interface EntityDisplay {
  ticker: string;
  displayName: string | null;
}

// #1327: this statement evaluates mart.entity_identity once, not once for each display row.
// The view calls staging.entity_survivor() for each alias row and for each identity row.
// A lateral lookup for each display row ran the view's final join again for every row.
// On production, 234 display rows took 15.8 s to 30.7 s that way, against 2.7 s for one evaluation.
// The cast entity_id::text blocks every index, so no lookup for each row can be cheap.
//
// The view's staging.kg_entities join can match one entity twice: by UUID and by legacy id.
// A plain join would then return that display row twice. DISTINCT ON keeps one row for each
// entity. The smallest legacy_id wins, and NULL sorts last.
// tests/mart-entity-display-map.test.ts seeds this case.
export const ENTITY_DISPLAY_SQL = `select r.issuer_id,
       coalesce(nullif(btrim(r.ticker), ''), nullif(btrim(ei.current_ticker), '')) as ticker,
       coalesce(nullif(btrim(r.display_name), ''), nullif(btrim(ei.name), '')) as display_name,
       ei.legacy_id
       from mart.entity_display_resolution r
       left join (
         select distinct on (e.entity_id::text)
                e.entity_id::text as issuer_id,
                e.current_ticker,
                e.name,
                e.legacy_id
         from mart.entity_identity e
         order by e.entity_id::text, e.legacy_id
       ) ei on ei.issuer_id = r.issuer_id`;

export async function loadEntityDisplayMap(): Promise<Map<string, EntityDisplay>> {
  const rows = await withMartReadonly(async (client) => {
    const result = await client.query(ENTITY_DISPLAY_SQL);
    return result.rows as {
      issuer_id: string;
      ticker: string;
      display_name: string | null;
      legacy_id: string | null;
    }[];
  });
  const map = new Map<string, EntityDisplay>();
  for (const row of rows) {
    const display: EntityDisplay = {
      ticker: String(row.ticker),
      displayName: row.display_name,
    };
    map.set(row.issuer_id, display);
    // The symbolic id the read path now returns. Never overwrite a direct
    // entry: the UUID key is the authoritative one, the alias is the alternate.
    if (row.legacy_id && !map.has(row.legacy_id)) map.set(row.legacy_id, display);
  }
  return map;
}

/** `TICKER · Name` when both known; ticker alone when the KG has no name;
 * the raw id when the issuer is not in the resolution view at all. */
export function entityLabel(issuerId: string, map: Map<string, EntityDisplay>): string {
  const entry = map.get(issuerId);
  if (!entry || !entry.ticker || entry.ticker.trim() === "") return issuerId;
  return entry.displayName ? `${entry.ticker} · ${entry.displayName}` : entry.ticker;
}
