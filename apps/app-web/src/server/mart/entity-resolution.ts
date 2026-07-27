/**
 * #494 P0b: consumer-side reads of `mart.entity_display_resolution`
 * (#495 surface 1, migration 0033) — issuer → ticker / display name so no
 * research first screen ever shows a raw LEI. Reads through mart_readonly
 * like every other consumer path; display_name is nullable (the KG carries
 * no names yet), so rendering falls back to ticker, and an issuer missing
 * from the view falls back to its raw id — honesty over invention.
 */

import { withMartReadonly } from "@/server/mart/db";

export interface EntityDisplay {
  ticker: string;
  displayName: string | null;
}

export async function loadEntityDisplayMap(): Promise<Map<string, EntityDisplay>> {
  const rows = await withMartReadonly(async (client) => {
    const result = await client.query(
      "select issuer_id, ticker, display_name from mart.entity_display_resolution",
    );
    return result.rows as { issuer_id: string; ticker: string; display_name: string | null }[];
  });
  return new Map(
    rows.map((row) => [
      row.issuer_id,
      { ticker: String(row.ticker), displayName: row.display_name },
    ]),
  );
}

/** `TICKER · Name` when both known; ticker alone when the KG has no name;
 * the raw id when the issuer is not in the resolution view at all. */
export function entityLabel(issuerId: string, map: Map<string, EntityDisplay>): string {
  const entry = map.get(issuerId);
  if (!entry) return issuerId;
  return entry.displayName ? `${entry.ticker} · ${entry.displayName}` : entry.ticker;
}
