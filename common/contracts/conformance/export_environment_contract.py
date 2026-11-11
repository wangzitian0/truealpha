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
    contract = load_environment_contract()
    ticks_by_env = {
        env: {
            k: {"scheduled": contract.is_universe_scheduled(k, env), "cron": contract.record(k).cron[env]}
            for k in contract.environments[env].ticks
        }
        for env in ("preview", "staging", "production")
    }
    universes = [
        {
            "key": k,
            "region": u.region,
            "id_prefix": u.id_prefix,
            "lane_name": u.lane_name,
            "label": u.label,
            "roles": list(u.roles),
            "scheduled_somewhere": any(contract.is_universe_scheduled(k, e) for e in ("staging", "production")),
        }
        for k, u in contract.universes.items()
    ]
    sentinels = [
        {"ticker": m.ticker, "issuer_id": m.issuer_id, "listing_id": m.listing_id, "trait": m.trait}
        for m in contract.sentinels
    ]
    env_names = tuple(contract.vendor_accounts.default_percent.keys())
    vendor_accounts = []
    for name, acc in contract.vendor_accounts.accounts.items():
        shares = {e: contract.account_share(name, e) for e in env_names}
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
                "shares": {e: shares[e].limits[i].share for e in env_names},
            }
            for i, lim in enumerate(acc.limits)
        ]
        vendor_accounts.append(
            {
                "name": name,
                "holder": acc.holder,
                "seats": list(acc.seats),
                "outside_consumers": acc.outside_consumers,
                "percent": {e: shares[e].percent for e in env_names},
                "percent_reason": acc.percent_reason,
                "note": acc.note,
                "limits": limits,
            }
        )
    model_seat = {
        "seat": contract.model_seat.seat,
        "measured_at": contract.model_seat.measured_at,
        "environments": {
            e: {"max_concurrent_calls": lim.max_concurrent_calls, "calls_per_minute": lim.calls_per_minute}
            for e, lim in contract.model_seat.environments.items()
        },
    }
    nightly_verdicts = {
        "checks": list(contract.nightly_verdicts.checks.keys()),
        "per_universe": list(contract.nightly_verdicts.per_universe.keys()),
    }
    b = contract.run_time_bounds
    return f"""\
/** GENERATED FILE. DO NOT EDIT. Regenerate: {_REGENERATE} */

export interface EnvironmentTickRule {{ readonly scheduled: boolean; readonly cron: string; }}
export interface RunTimeBoundsPolicy {{ readonly margin: number; readonly floorMinutes: number; readonly windowDays: number; }}
export interface UniverseRecordContract {{ readonly key: string; readonly region: string; readonly id_prefix: string; readonly lane_name: string; readonly label: string; readonly roles: readonly ("strategy" | "fund_consolidation" | "sentinel")[]; readonly scheduled_somewhere: boolean; }}
export interface SentinelMemberContract {{ readonly ticker: string; readonly issuer_id: string; readonly listing_id: string; readonly trait: string; }}
export type EvidenceKindContract = "published" | "measured" | "self_imposed" | "none_published" | "not_known";
export interface LimitEvidenceContract {{ readonly kind: EvidenceKindContract; readonly source: readonly string[]; readonly checked_at: string | null; readonly method: string | null; readonly reason: string | null; }}
export interface VendorLimitContract {{ readonly id: string; readonly unit: "request" | "distinct_stock"; readonly amount: number | null; readonly window_seconds: number | null; readonly counts_per: "api_key" | "host_ip" | "account" | "not_stated"; readonly applies_to: string | null; readonly divided: boolean; readonly evidence: LimitEvidenceContract; readonly shares: Readonly<Record<string, number | null>>; }}
export interface VendorAccountContract {{ readonly name: string; readonly holder: "api_key" | "host_ip" | "account"; readonly seats: readonly string[]; readonly outside_consumers: boolean; readonly percent: Readonly<Record<string, number>>; readonly percent_reason: string | null; readonly note: string | null; readonly limits: readonly VendorLimitContract[]; }}
export interface AccountLimitShareContract {{ readonly limitId: string; readonly unit: "request" | "distinct_stock"; readonly windowSeconds: number | null; readonly amount: number | null; readonly share: number | null; readonly divided: boolean; readonly evidence: LimitEvidenceContract; }}
export interface AccountShareContract {{ readonly account: string; readonly environment: string; readonly percent: number; readonly admitsACall: boolean; readonly dailyShare: number | null; readonly rateWindowSeconds: number | null; readonly rateWindowShare: number | null; readonly limits: readonly AccountLimitShareContract[]; }}
export interface ModelSeatEnvironmentLimitsContract {{ readonly max_concurrent_calls: number; readonly calls_per_minute: number; }}
export interface ModelSeatContract {{ readonly seat: string; readonly measured_at: string; readonly environments: Readonly<Record<string, ModelSeatEnvironmentLimitsContract>>; }}
export interface NightlyVerdictsContract {{ readonly checks: readonly string[]; readonly per_universe: readonly string[]; }}

export const ENVIRONMENT_CONTRACT_VERSION = {_j(contract.contract_version)} as const;
export const ENVIRONMENT_CONTRACT_SHA256 = {_j(contract.content_sha256)} as const;
export const UTILIZATION_CEILING = {UTILIZATION_CEILING} as const;
export const DAY_SECONDS = {DAY_SECONDS} as const;
export const SENTINEL_MEMBERS: readonly SentinelMemberContract[] = {_j(sentinels)} as const;
export const RUN_TIME_BOUNDS: RunTimeBoundsPolicy = {{ margin: {b.margin}, floorMinutes: {b.floor_minutes}, windowDays: {b.window_days} }} as const;
export const ENVIRONMENT_TICKS: Readonly<Record<string, Readonly<Record<string, EnvironmentTickRule>>>> = {_j(ticks_by_env)} as const;
export const UNIVERSES: readonly UniverseRecordContract[] = {_j(universes)} as const;
export const VENDOR_ACCOUNTS: readonly VendorAccountContract[] = {_j(vendor_accounts)} as const;
export const MODEL_SEAT: ModelSeatContract = {_j(model_seat)} as const;
export const NIGHTLY_VERDICTS: NightlyVerdictsContract = {_j(nightly_verdicts)} as const;

function normEnv(env: string | null | undefined, fallback = "production"): string {{
  const raw = (env || fallback).trim().toLowerCase() || fallback;
  return raw === "prod" ? "production" : raw;
}}

export function vendorAccountRecord(name: string): VendorAccountContract {{
  const found = VENDOR_ACCOUNTS.find((a) => a.name === name);
  if (!found) throw new Error(`"${{name}}" is no vendor account of the environment contract`);
  return found;
}}

export function accountShare(account: string, environment: string | null | undefined): AccountShareContract {{
  const row = vendorAccountRecord(account);
  const env = normEnv(environment);
  const known = Object.prototype.hasOwnProperty.call(row.percent, env) ? row.percent[env] : undefined;
  const percent = known !== undefined ? known : row.holder === "host_ip" ? 100 : 0;
  const limits: AccountLimitShareContract[] = row.limits.map((l) => ({{
    limitId: l.id,
    unit: l.unit,
    windowSeconds: l.window_seconds,
    amount: l.amount,
    share: l.amount !== null && l.divided ? Math.floor((l.amount * percent) / 100) : l.amount,
    divided: l.divided,
    evidence: l.evidence,
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
  return matches.length === 0 ? null : matches.reduce((best, u) => (u.id_prefix.length > best.id_prefix.length ? u : best)).key;
}}

export function universeOfId(universeId: string): string {{
  const key = findUniverseOfId(universeId);
  if (key === null) throw new Error(`"${{universeId}}" starts with no universe id prefix of the environment contract`);
  return key;
}}

export function idPrefixLike(key: string): string {{ return `${{universeRecord(key).id_prefix}}%`; }}

export function isHeadScheduledFromContract(universeId: string, environment: string | null | undefined): boolean {{
  const env = normEnv(environment);
  if (env === "preview") return true;
  const envTicks = ENVIRONMENT_TICKS[env];
  if (!envTicks) return true;
  const key = findUniverseOfId(universeId);
  return key === null ? true : (envTicks[key]?.scheduled ?? false);
}}

export function expectsVerdictFromContract(check: string, environment: string | null | undefined): boolean {{
  const atIdx = check.indexOf("@");
  if (atIdx === -1) return NIGHTLY_VERDICTS.checks.includes(check);
  const prefix = check.slice(0, atIdx);
  const laneName = check.slice(atIdx + 1);
  if (!NIGHTLY_VERDICTS.per_universe.includes(prefix)) return false;
  const u = UNIVERSES.find((x) => x.lane_name === laneName);
  if (!u) return false;
  const envTicks = ENVIRONMENT_TICKS[normEnv(environment, "")];
  if (!envTicks) return true;
  const rule = envTicks[u.key];
  return rule ? rule.scheduled && !u.roles.includes("sentinel") : false;
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
