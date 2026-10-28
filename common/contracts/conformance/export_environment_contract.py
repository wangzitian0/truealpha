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

from truealpha_contracts.environment_contract import (
    DAY_SECONDS,
    UTILIZATION_CEILING,
    load_environment_contract,
)

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
            }
            for tick_key in spec.ticks
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
    env_names = tuple(contract.vendor_accounts.default_percent.keys())
    vendor_accounts = []
    for account_name, account in contract.vendor_accounts.accounts.items():
        shares_by_env = {env_name: contract.account_share(account_name, env_name) for env_name in env_names}
        limits = []
        for idx, limit in enumerate(account.limits):
            limits.append(
                {
                    "id": limit.id,
                    "unit": limit.unit,
                    "amount": limit.amount,
                    "window_seconds": limit.window_seconds,
                    "counts_per": limit.counts_per,
                    "applies_to": limit.applies_to,
                    "divided": limit.divided,
                    "evidence": {
                        "kind": limit.evidence.kind,
                        "source": list(limit.evidence.source),
                        "checked_at": limit.evidence.checked_at,
                        "method": limit.evidence.method,
                        "reason": limit.evidence.reason,
                    },
                    "shares": {env_name: shares_by_env[env_name].limits[idx].share for env_name in env_names},
                }
            )
        vendor_accounts.append(
            {
                "name": account_name,
                "holder": account.holder,
                "seats": list(account.seats),
                "outside_consumers": account.outside_consumers,
                "percent": {env_name: shares_by_env[env_name].percent for env_name in env_names},
                "percent_reason": account.percent_reason,
                "note": account.note,
                "limits": limits,
            }
        )
    model_seat = {
        "seat": contract.model_seat.seat,
        "measured_at": contract.model_seat.measured_at,
        "environments": {
            env_name: {
                "max_concurrent_calls": limits.max_concurrent_calls,
                "calls_per_minute": limits.calls_per_minute,
            }
            for env_name, limits in contract.model_seat.environments.items()
        },
    }
    ticks_json = json.dumps(ticks_by_env, indent=2)
    universes_json = json.dumps(universes, indent=2)
    sentinels_json = json.dumps(sentinels, indent=2)
    vendor_accounts_json = json.dumps(vendor_accounts, indent=2)
    model_seat_json = json.dumps(model_seat, indent=2)
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
}}

export interface RunTimeBoundsPolicy {{
  readonly margin: number;
  readonly floorMinutes: number;
  readonly windowDays: number;
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

export type EvidenceKindContract =
  | "published"
  | "measured"
  | "self_imposed"
  | "none_published"
  | "not_known";

export interface LimitEvidenceContract {{
  readonly kind: EvidenceKindContract;
  readonly source: readonly string[];
  readonly checked_at: string | null;
  readonly method: string | null;
  readonly reason: string | null;
}}

export interface VendorLimitContract {{
  readonly id: string;
  readonly unit: "request" | "distinct_stock";
  readonly amount: number | null;
  readonly window_seconds: number | null;
  readonly counts_per: "api_key" | "host_ip" | "account" | "not_stated";
  readonly applies_to: string | null;
  readonly divided: boolean;
  readonly evidence: LimitEvidenceContract;
  readonly shares: Readonly<Record<string, number | null>>;
}}

export interface VendorAccountContract {{
  readonly name: string;
  readonly holder: "api_key" | "host_ip" | "account";
  readonly seats: readonly string[];
  readonly outside_consumers: boolean;
  readonly percent: Readonly<Record<string, number>>;
  readonly percent_reason: string | null;
  readonly note: string | null;
  readonly limits: readonly VendorLimitContract[];
}}

export interface AccountLimitShareContract {{
  readonly limitId: string;
  readonly unit: "request" | "distinct_stock";
  readonly windowSeconds: number | null;
  readonly amount: number | null;
  readonly share: number | null;
  readonly divided: boolean;
  readonly evidence: LimitEvidenceContract;
}}

export interface AccountShareContract {{
  readonly account: string;
  readonly environment: string;
  readonly percent: number;
  readonly admitsACall: boolean;
  readonly dailyShare: number | null;
  readonly rateWindowSeconds: number | null;
  readonly rateWindowShare: number | null;
  readonly limits: readonly AccountLimitShareContract[];
}}

export interface ModelSeatEnvironmentLimitsContract {{
  readonly max_concurrent_calls: number;
  readonly calls_per_minute: number;
}}

export interface ModelSeatContract {{
  readonly seat: string;
  readonly measured_at: string;
  readonly environments: Readonly<Record<string, ModelSeatEnvironmentLimitsContract>>;
}}

export const ENVIRONMENT_CONTRACT_VERSION = {json.dumps(contract.contract_version)} as const;
export const ENVIRONMENT_CONTRACT_SHA256 = {json.dumps(contract.content_sha256)} as const;
export const UTILIZATION_CEILING = {UTILIZATION_CEILING} as const;
export const DAY_SECONDS = {DAY_SECONDS} as const;

export const SENTINEL_MEMBERS: readonly SentinelMemberContract[] = {sentinels_json} as const;

export const RUN_TIME_BOUNDS: RunTimeBoundsPolicy = {{
  margin: {contract.run_time_bounds.margin},
  floorMinutes: {contract.run_time_bounds.floor_minutes},
  windowDays: {contract.run_time_bounds.window_days},
}} as const;

export const ENVIRONMENT_TICKS: Readonly<Record<string, Readonly<Record<string, EnvironmentTickRule>>>> = {ticks_json} as const;

export const UNIVERSES: readonly UniverseRecordContract[] = {universes_json} as const;

export const VENDOR_ACCOUNTS: readonly VendorAccountContract[] = {vendor_accounts_json} as const;

export const MODEL_SEAT: ModelSeatContract = {model_seat_json} as const;

/** The vendor account row `name` of the environment contract (#1520). */
export function vendorAccountRecord(name: string): VendorAccountContract {{
  const found = VENDOR_ACCOUNTS.find((account) => account.name === name);
  if (!found) {{
    throw new Error(`"${{name}}" is no vendor account of the environment contract`);
  }}
  return found;
}}

/** What `environment` may spend of each limit of `account` (#1520). */
export function accountShare(
  account: string,
  environment: string | null | undefined,
): AccountShareContract {{
  const row = vendorAccountRecord(account);
  const rawEnv = (environment || "production").trim().toLowerCase() || "production";
  const env = rawEnv === "prod" ? "production" : rawEnv;
  const knownPercent = Object.prototype.hasOwnProperty.call(row.percent, env) ? row.percent[env] : undefined;
  const percent = knownPercent !== undefined ? knownPercent : row.holder === "host_ip" ? 100 : 0;
  const limits: AccountLimitShareContract[] = row.limits.map((limit) => ({{
    limitId: limit.id,
    unit: limit.unit,
    windowSeconds: limit.window_seconds,
    amount: limit.amount,
    share:
      limit.amount !== null && limit.divided
        ? Math.floor((limit.amount * percent) / 100)
        : limit.amount,
    divided: limit.divided,
    evidence: limit.evidence,
  }}));
  const requestLimits = limits.filter((limit) => limit.unit === "request");
  const admitsACall = requestLimits.every((limit) => limit.share === null || limit.share >= 1);
  const daily = requestLimits.find((limit) => limit.windowSeconds === DAY_SECONDS && limit.share !== null);
  const rate = requestLimits.find(
    (limit) => limit.windowSeconds !== null && limit.windowSeconds !== DAY_SECONDS && limit.share !== null,
  );
  return {{
    account: row.name,
    environment: env,
    percent,
    admitsACall,
    dailyShare: daily ? daily.share : null,
    rateWindowSeconds: rate ? rate.windowSeconds : null,
    rateWindowShare: rate ? rate.share : null,
    limits,
  }};
}}

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
