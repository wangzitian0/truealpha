import { formatIssuerLabel } from "@/client/format";
import { queryMartRows } from "@/server/mart/db";

export interface EntityDisplay {
  ticker: string;
  displayName: string | null;
}

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
  const rows = await queryMartRows<{
    issuer_id: string;
    ticker: string;
    display_name: string | null;
    legacy_id: string | null;
  }>(ENTITY_DISPLAY_SQL);
  const map = new Map<string, EntityDisplay>();
  for (const row of rows) {
    const display: EntityDisplay = {
      ticker: String(row.ticker),
      displayName: row.display_name,
    };
    map.set(row.issuer_id, display);
    if (row.legacy_id && !map.has(row.legacy_id)) map.set(row.legacy_id, display);
  }
  return map;
}

export function entityLabel(issuerId: string, map: Map<string, EntityDisplay>): string {
  const entry = map.get(issuerId);
  const ticker = entry?.ticker?.trim() ?? "";
  const displayName = entry?.displayName?.trim() ?? "";
  if (ticker && displayName) return `${ticker} · ${displayName}`;
  if (ticker) return ticker;
  if (displayName) return displayName;
  return formatIssuerLabel(issuerId) ?? issuerId;
}
