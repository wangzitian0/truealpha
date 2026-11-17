import {
  UNIVERSES,
  idPrefixLike,
  universeOfId,
  universeRecord,
  type UniverseRecordContract,
} from "./environmentContract.generated";

export type UniverseRole = UniverseRecordContract["roles"][number];

export function universeWithRole(role: UniverseRole): string {
  const holders = UNIVERSES.filter((universe) => universe.roles.includes(role)).map((universe) => universe.key);
  if (holders.length !== 1) {
    throw new Error(`the role "${role}" needs exactly one universe, the record holds: ${holders.join(", ") || "none"}`);
  }
  return holders[0];
}

export function idPrefixLikeOfRole(role: UniverseRole): string {
  return idPrefixLike(universeWithRole(role));
}

export function idPrefixLikesOfRole(role: UniverseRole): string[] {
  const likes = UNIVERSES.filter((universe) => universe.roles.includes(role)).map((universe) => idPrefixLike(universe.key));
  if (likes.length === 0) {
    throw new Error(`no universe of the environment contract holds the role "${role}"`);
  }
  return likes;
}

export function universeIdHasRole(universeId: string, role: UniverseRole): boolean {
  return universeRecord(universeOfId(universeId)).roles.includes(role);
}

export function universeOfLane(laneName: string): string {
  const found = UNIVERSES.find((universe) => universe.lane_name === laneName);
  if (!found) {
    throw new Error(`"${laneName}" is the lane name of no universe of the environment contract`);
  }
  return found.key;
}

export function laneNames(): readonly string[] {
  return UNIVERSES.map((universe) => universe.lane_name);
}
