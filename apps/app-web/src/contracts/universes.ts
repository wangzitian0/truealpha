import { UNIVERSES, type UniverseRecordContract } from "./environmentContract.generated";

export type UniverseRole = UniverseRecordContract["roles"][number];

export function universesWithRole(role: UniverseRole): string[] {
  return UNIVERSES.filter((u) => u.roles.includes(role)).map((u) => u.key);
}

export function universeWithRole(role: UniverseRole): string {
  const holders = universesWithRole(role);
  if (holders.length !== 1) throw new Error(`role "${role}" needs 1 universe, got ${holders.join(",") || "none"}`);
  return holders[0];
}
