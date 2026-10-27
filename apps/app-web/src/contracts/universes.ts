/**
 * Hand-written lookups of the universe record (#1522).
 *
 * `environmentContract.generated.ts` holds the record: one entry per universe. This module adds the
 * lookups that readers need. A key, id or lane name outside the record throws. No reader keeps its own list of universes,
 * id prefixes or lane names.
 *
 * Pure module: client components may import it.
 */

import {
  UNIVERSES,
  idPrefixLike,
  universeOfId,
  universeRecord,
  type UniverseRecordContract,
} from "./environmentContract.generated";

export type UniverseRole = UniverseRecordContract["roles"][number];

/** The key of the one universe that holds `role`. Zero or several holders throw. */
export function universeWithRole(role: UniverseRole): string {
  const holders = UNIVERSES.filter((universe) => universe.roles.includes(role)).map((universe) => universe.key);
  if (holders.length !== 1) {
    throw new Error(`the role "${role}" needs exactly one universe, the record holds: ${holders.join(", ") || "none"}`);
  }
  return holders[0];
}

/** The SQL `like` pattern of every universe id of the universe that holds `role`. */
export function idPrefixLikeOfRole(role: UniverseRole): string {
  return idPrefixLike(universeWithRole(role));
}

/** The SQL `like` patterns of every universe id of each universe that holds `role`, in record order. A role that no universe holds throws. */
export function idPrefixLikesOfRole(role: UniverseRole): string[] {
  const likes = UNIVERSES.filter((universe) => universe.roles.includes(role)).map((universe) => idPrefixLike(universe.key));
  if (likes.length === 0) {
    throw new Error(`no universe of the environment contract holds the role "${role}"`);
  }
  return likes;
}

/** Whether the universe that governs `universeId` holds `role`. An id outside the record throws. */
export function universeIdHasRole(universeId: string, role: UniverseRole): boolean {
  return universeRecord(universeOfId(universeId)).roles.includes(role);
}

/** The key of the universe that the lanes call `laneName` (for example `universe-list:qqq`). A name outside the record throws. */
export function universeOfLane(laneName: string): string {
  const found = UNIVERSES.find((universe) => universe.lane_name === laneName);
  if (!found) {
    throw new Error(`"${laneName}" is the lane name of no universe of the environment contract`);
  }
  return found.key;
}

/** The lane name of every universe of the record, in record order. */
export function laneNames(): readonly string[] {
  return UNIVERSES.map((universe) => universe.lane_name);
}
