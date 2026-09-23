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

export async function loadEntityDisplayMap(): Promise<Map<string, EntityDisplay>> {
  const rows = await withMartReadonly(async (client) => {
    const result = await client.query(
      // LATERAL ... limit 1 rather than a plain join, for the reason
      // strategy-run-repository.ts documents: mart.entity_identity's
      // staging.kg_entities join can match one entity twice, and a display
      // lookup must never multiply the rows it is decorating.
      `select r.issuer_id, r.ticker, r.display_name, ei.legacy_id
       from mart.entity_display_resolution r
       left join lateral (
         select ei.legacy_id
         from mart.entity_identity ei
         where ei.entity_id::text = r.issuer_id
         limit 1
       ) ei on true`,
    );
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
