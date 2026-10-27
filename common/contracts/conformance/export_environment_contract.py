"""Generate the TypeScript twin of the environment contract SSOT (#1416 E2).

The web image builds from ``apps/app-web`` alone, so the App cannot read the packaged
``environment_contract.v1.json`` file at run time. This script writes
``apps/app-web/src/contracts/environmentContract.generated.ts`` from
``truealpha_contracts.environment_contract.load_environment_contract()``.

Run from the repository root:

    uv run python common/contracts/conformance/export_environment_contract.py --write
    uv run python common/contracts/conformance/export_environment_contract.py --check
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from truealpha_contracts.environment_contract import load_environment_contract

REPO_ROOT = Path(__file__).resolve().parents[3]
TARGET = REPO_ROOT / "apps" / "app-web" / "src" / "contracts" / "environmentContract.generated.ts"

_REGENERATE = "uv run python common/contracts/conformance/export_environment_contract.py --write"
_GUARD = "common/contracts/tests/test_environment_contract.py"


def render() -> str:
    """Render the TypeScript module for the environment contract."""
    contract = load_environment_contract()
    ticks_by_env: dict[str, dict[str, dict[str, object]]] = {}
    for env_name in ("preview", "staging", "production"):
        spec = contract.environments[env_name]
        ticks_by_env[env_name] = {
            tick_key: {
                "scheduled": contract.is_universe_scheduled(tick_key, env_name),
                "cron": contract.record(tick_key).cron[env_name],
                "estimated_listings": tick.estimated_listings,
            }
            for tick_key, tick in spec.ticks.items()
        }
    universes = [
        {
            "key": key,
            "region": universe.region,
            "id_prefix": universe.id_prefix,
            "lane_name": universe.lane_name,
            "label": universe.label,
            "roles": list(universe.roles),
            "scheduled_somewhere": any(
                contract.is_universe_scheduled(key, env_name) for env_name in ("staging", "production")
            ),
        }
        for key, universe in contract.universes.items()
    ]
    sentinels = [
        {
            "ticker": member.ticker,
            "issuer_id": member.issuer_id,
            "listing_id": member.listing_id,
            "trait": member.trait,
        }
        for member in contract.sentinels
    ]
    ticks_json = json.dumps(ticks_by_env, indent=2)
    universes_json = json.dumps(universes, indent=2)
    sentinels_json = json.dumps(sentinels, indent=2)
    return f"""\
/**
 * GENERATED FILE. DO NOT EDIT.
 *
 * Source: truealpha_contracts.environment_contract.load_environment_contract() (#1416).
 * Regenerate: {_REGENERATE}
 * Guard: {_GUARD} fails when this file differs.
 */

export interface EnvironmentTickRule {{
  readonly scheduled: boolean;
  readonly cron: string;
  readonly estimated_listings: number;
}}

export interface UniverseRecordContract {{
  readonly key: string;
  readonly region: string;
  readonly id_prefix: string;
  readonly lane_name: string;
  readonly label: string;
  readonly roles: readonly ("strategy" | "fund_consolidation" | "sentinel")[];
  /** Whether some environment schedules the universe, or runs it as a sentinel. */
  readonly scheduled_somewhere: boolean;
}}

export interface SentinelMemberContract {{
  readonly ticker: string;
  readonly issuer_id: string;
  readonly listing_id: string;
  readonly trait: string;
}}

export const ENVIRONMENT_CONTRACT_VERSION = {json.dumps(contract.contract_version)} as const;
export const ENVIRONMENT_CONTRACT_SHA256 = {json.dumps(contract.content_sha256)} as const;

export const SENTINEL_MEMBERS: readonly SentinelMemberContract[] = {sentinels_json} as const;

export const ENVIRONMENT_TICKS: Readonly<Record<string, Readonly<Record<string, EnvironmentTickRule>>>> = {ticks_json} as const;

export const UNIVERSES: readonly UniverseRecordContract[] = {universes_json} as const;

/** The record of the universe `key`. A key outside the record throws (#1522). */
export function universeRecord(key: string): UniverseRecordContract {{
  const found = UNIVERSES.find((universe) => universe.key === key);
  if (!found) {{
    throw new Error(`"${{key}}" is no universe of the environment contract`);
  }}
  return found;
}}

/** The key of the universe that governs `universeId`: the longest id prefix wins; the id is not split. Null when none matches. */
export function findUniverseOfId(universeId: string): string | null {{
  const matches = UNIVERSES.filter((universe) => universeId.startsWith(universe.id_prefix));
  if (matches.length === 0) {{
    return null;
  }}
  return matches.reduce((best, universe) => (universe.id_prefix.length > best.id_prefix.length ? universe : best)).key;
}}

/** The key of the universe that governs `universeId`. An id that starts with no id prefix throws. */
export function universeOfId(universeId: string): string {{
  const key = findUniverseOfId(universeId);
  if (key === null) {{
    throw new Error(`"${{universeId}}" starts with no universe id prefix of the environment contract`);
  }}
  return key;
}}

/** The SQL `like` pattern of every universe id of the universe `key`. */
export function idPrefixLike(key: string): string {{
  return `${{universeRecord(key).id_prefix}}%`;
}}

/**
 * Generated from environment_contract.v1.json (#1416 E1/E2, #1522).
 * Fails closed (returns true) when environment is unrecognised or universeId starts with no id prefix of the
 * records (the Python twin and the SQL function answer the same way until step 2 of #1522 retires the function).
 * A universe that another environment ticks and this one does not is not scheduled here.
 */
export function isHeadScheduledFromContract(
  universeId: string,
  environment: string | null | undefined,
): boolean {{
  const rawEnv = (environment || "production").trim().toLowerCase() || "production";
  const env = rawEnv === "prod" ? "production" : rawEnv;
  if (env === "preview") {{
    return true;
  }}
  const envTicks = ENVIRONMENT_TICKS[env];
  if (!envTicks) {{
    return true;
  }}
  const key = findUniverseOfId(universeId);
  if (key === null) {{
    return true;
  }}
  const rule = envTicks[key];
  return rule ? rule.scheduled : false;
}}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="write the generated file")
    mode.add_argument("--check", action="store_true", help="exit 1 when the checked-in file differs")
    args = parser.parse_args()
    text = render()
    if args.write:
        TARGET.write_text(text, encoding="utf-8")
        return 0
    if not TARGET.exists() or TARGET.read_text(encoding="utf-8") != text:
        print(
            f"{TARGET.relative_to(REPO_ROOT)} is out of date; run `{_REGENERATE}`",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
