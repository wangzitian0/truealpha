import { UNIVERSES, type UniverseRecordContract } from "./environmentContract.generated";

export type UniverseRole = UniverseRecordContract["roles"][number];

export function universesWithRole(role: UniverseRole): string[] {
  return UNIVERSES.filter((u) => u.roles.includes(role)).map((u) => u.key);
}
