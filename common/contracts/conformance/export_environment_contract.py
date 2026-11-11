"""Generate the TypeScript twin of the environment contract SSOT (#1416 E2)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from truealpha_contracts.environment_contract import DAY_SECONDS, UTILIZATION_CEILING, load_environment_contract

REPO_ROOT = Path(__file__).resolve().parents[3]
TARGET = REPO_ROOT / "apps" / "app-web" / "src" / "contracts" / "environmentContract.generated.ts"
_REGENERATE = "uv run python common/contracts/conformance/export_environment_contract.py --write"
_GUARD = "common/contracts/tests/test_environment_contract.py"


def _j(value: object) -> str:
    return json.dumps(value, separators=(",", ":"))


def render() -> str:
    """Render the TypeScript module for the environment contract."""
    contract = load_environment_contract()
    ticks_by_env = {
        env_name: {
            tick_key: {
                "scheduled": contract.is_universe_scheduled(tick_key, env_name),
                "cron": contract.record(tick_key).cron[env_name],
            }
            for tick_key in contract.environments[env_name].ticks
        }
        for env_name in ("preview", "staging", "production")
    }
    universes = [
        {
            "key": key,
            "region": u.region,
            "id_prefix": u.id_prefix,
            "lane_name": u.lane_name,
            "label": u.label,
            "roles": list(u.roles),
            "scheduled_somewhere": any(contract.is_universe_scheduled(key, env) for env in ("staging", "production")),
        }
        for key, u in contract.universes.items()
    ]
    sentinels = [
        {"ticker": m.ticker, "issuer_id": m.issuer_id, "listing_id": m.listing_id, "trait": m.trait}
        for m in contract.sentinels
    ]
    env_names = tuple(contract.vendor_accounts.default_percent.keys())
    vendor_accounts = []
    for account_name, account in contract.vendor_accounts.accounts.items():
        shares_by_env = {env_name: contract.account_share(account_name, env_name) for env_name in env_names}
        limits = [
            {
                "id": lim.id,
                "unit": lim.unit,
                "amount": lim.amount,
                "window_seconds": lim.window_seconds,
                "counts_per": lim.counts_per,
                "applies_to": lim.applies_to,
                "divided": lim.divided,
                "evidence": {
                    "kind": lim.evidence.kind,
                    "source": list(lim.evidence.source),
                    "checked_at": lim.evidence.checked_at,
                    "method": lim.evidence.method,
                    "reason": lim.evidence.reason,
                },
                "shares": {env_name: shares_by_env[env_name].limits[idx].share for env_name in env_names},
            }
            for idx, lim in enumerate(account.limits)
        ]
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
            env_name: {"max_concurrent_calls": lim.max_concurrent_calls, "calls_per_minute": lim.calls_per_minute}
            for env_name, lim in contract.model_seat.environments.items()
        },
    }
    nightly_verdicts = {
        "checks": list(contract.nightly_verdicts.checks.keys()),
        "per_universe": list(contract.nightly_verdicts.per_universe.keys()),
    }
    return f"""\
/** GENERATED FILE. DO NOT EDIT. Regenerate: {_REGENERATE} */

export interface EnvironmentTickRule {{ readonly scheduled: boolean; readonly cron: string; }}
export interface RunTimeBoundsPolicy {{ readonly margin: number; readonly floorMinutes: number; readonly windowDays: number; }}
export interface UniverseRecordContract {{
  readonly key: string;
  readonly region: string;
  readonly id_prefix: string;
  readonly lane_name: string;
  readonly label: string;
  readonly roles: readonly ("strategy" | "fund_consolidation" | "sentinel")[];
  readonly scheduled_somewhere: boolean;
}}
export interface SentinelMemberContract {{
  readonly ticker: string;
  readonly issuer_id: string;
  readonly listing_id: string;
  readonly trait: string;
}}
export type EvidenceKindContract = "published" | "measured" | "self_imposed" | "none_published" | "not_known";
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
export interface NightlyVerdictsContract {{
  readonly checks: readonly string[];
  readonly per_universe: readonly string[];
}}

export const ENVIRONMENT_CONTRACT_VERSION = {_j(contract.contract_version)} as const;
export const ENVIRONMENT_CONTRACT_SHA256 = {_j(contract.content_sha256)} as const;
export const UTILIZATION_CEILING = {UTILIZATION_CEILING} as const;
export const DAY_SECONDS = {DAY_SECONDS} as const;
export const SENTINEL_MEMBERS: readonly SentinelMemberContract[] = {_j(sentinels)} as const;
export const RUN_TIME_BOUNDS: RunTimeBoundsPolicy = {{
  margin: {contract.run_time_bounds.margin},
  floorMinutes: {contract.run_time_bounds.floor_minutes},
  windowDays: {contract.run_time_bounds.window_days},
}} as const;
export const ENVIRONMENT_TICKS: Readonly<Record<string, Readonly<Record<string, EnvironmentTickRule>>>> = {_j(ticks_by_env)} as const;
export const UNIVERSES: readonly UniverseRecordContract[] = {_j(universes)} as const;
export const VENDOR_ACCOUNTS: readonly VendorAccountContract[] = {_j(vendor_accounts)} as const;
export const MODEL_SEAT: ModelSeatContract = {_j(model_seat)} as const;
export const NIGHTLY_VERDICTS: NightlyVerdictsContract = {_j(nightly_verdicts)} as const;

function normEnv(environment: string | null | undefined, fallback = "production"): string {{
  const raw = (environment || fallback).trim().toLowerCase() || fallback;
  return raw === "prod" ? "production" : raw;
}}

export function vendorAccountRecord(name: string): VendorAccountContract {{
  const found = VENDOR_ACCOUNTS.find((account) => account.name === name);
  if (!found) throw new Error(`"${{name}}" is no vendor account of the environment contract`);
  return found;
}}

export function accountShare(account: string, environment: string | null | undefined): AccountShareContract {{
  const row = vendorAccountRecord(account);
  const env = normEnv(environment);
  const known = Object.prototype.hasOwnProperty.call(row.percent, env) ? row.percent[env] : undefined;
  const percent = known !== undefined ? known : row.holder === "host_ip" ? 100 : 0;
  const limits: AccountLimitShareContract[] = row.limits.map((limit) => ({{
    limitId: limit.id,
    unit: limit.unit,
    windowSeconds: limit.window_seconds,
    amount: limit.amount,
    share: limit.amount !== null && limit.divided ? Math.floor((limit.amount * percent) / 100) : limit.amount,
    divided: limit.divided,
    evidence: limit.evidence,
  }}));
  const reqLimits = limits.filter((l) => l.unit === "request");
  const daily = reqLimits.find((l) => l.windowSeconds === DAY_SECONDS && l.share !== null);
  const rate = reqLimits.find((l) => l.windowSeconds !== null && l.windowSeconds !== DAY_SECONDS && l.share !== null);
  return {{
    account: row.name,
    environment: env,
    percent,
    admitsACall: reqLimits.every((l) => l.share === null || l.share >= 1),
    dailyShare: daily ? daily.share : null,
    rateWindowSeconds: rate ? rate.windowSeconds : null,
    rateWindowShare: rate ? rate.share : null,
    limits,
  }};
}}

export function universeRecord(key: string): UniverseRecordContract {{
  const found = UNIVERSES.find((u) => u.key === key);
  if (!found) throw new Error(`"${{key}}" is no universe of the environment contract`);
  return found;
}}

export function findUniverseOfId(universeId: string): string | null {{
  const matches = UNIVERSES.filter((u) => universeId.startsWith(u.id_prefix));
  if (matches.length === 0) return null;
  return matches.reduce((best, u) => (u.id_prefix.length > best.id_prefix.length ? u : best)).key;
}}

export function universeOfId(universeId: string): string {{
  const key = findUniverseOfId(universeId);
  if (key === null) throw new Error(`"${{universeId}}" starts with no universe id prefix of the environment contract`);
  return key;
}}

export function idPrefixLike(key: string): string {{
  return `${{universeRecord(key).id_prefix}}%`;
}}

export function isHeadScheduledFromContract(universeId: string, environment: string | null | undefined): boolean {{
  const env = normEnv(environment);
  if (env === "preview") return true;
  const envTicks = ENVIRONMENT_TICKS[env];
  if (!envTicks) return true;
  const key = findUniverseOfId(universeId);
  if (key === null) return true;
  return envTicks[key]?.scheduled ?? false;
}}

export function expectsVerdictFromContract(check: string, environment: string | null | undefined): boolean {{
  const atIndex = check.indexOf("@");
  if (atIndex === -1) return NIGHTLY_VERDICTS.checks.includes(check);
  const prefix = check.slice(0, atIndex);
  const laneName = check.slice(atIndex + 1);
  if (!NIGHTLY_VERDICTS.per_universe.includes(prefix)) return false;
  const universe = UNIVERSES.find((u) => u.lane_name === laneName);
  if (!universe) return false;
  const envTicks = ENVIRONMENT_TICKS[normEnv(environment, "")];
  if (!envTicks) return true;
  const rule = envTicks[universe.key];
  return rule ? rule.scheduled && !universe.roles.includes("sentinel") : false;
}}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()
    text = render()
    if args.write:
        TARGET.write_text(text, encoding="utf-8")
        return 0
    if not TARGET.exists() or TARGET.read_text(encoding="utf-8") != text:
        print(f"{TARGET.relative_to(REPO_ROOT)} is out of date; run `{_REGENERATE}` — {_GUARD}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
