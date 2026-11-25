/** GENERATED FILE. DO NOT EDIT. Regenerate: uv run python common/contracts/conformance/export_environment_contract.py --write */

export interface EnvironmentTickRule { readonly scheduled: boolean; readonly cron: string; }
export interface RunTimeBoundsPolicy { readonly margin: number; readonly floorMinutes: number; readonly windowDays: number; }
export interface UniverseRecordContract { readonly key: string; readonly region: string; readonly id_prefix: string; readonly lane_name: string; readonly label: string; readonly roles: readonly ("strategy" | "fund_consolidation" | "sentinel")[]; readonly scheduled_somewhere: boolean; }
export interface SentinelMemberContract { readonly ticker: string; readonly issuer_id: string; readonly listing_id: string; readonly trait: string; }
export type EvidenceKindContract = "published" | "measured" | "self_imposed" | "none_published" | "not_known";
export interface LimitEvidenceContract { readonly kind: EvidenceKindContract; readonly source: readonly string[]; readonly checked_at: string | null; readonly method: string | null; readonly reason: string | null; }
export interface VendorLimitContract { readonly id: string; readonly unit: "request" | "distinct_stock"; readonly amount: number | null; readonly window_seconds: number | null; readonly counts_per: "api_key" | "host_ip" | "account" | "not_stated"; readonly applies_to: string | null; readonly divided: boolean; readonly evidence: LimitEvidenceContract; readonly shares: Readonly<Record<string, number | null>>; }
export interface VendorAccountContract { readonly name: string; readonly holder: "api_key" | "host_ip" | "account"; readonly seats: readonly string[]; readonly outside_consumers: boolean; readonly percent: Readonly<Record<string, number>>; readonly percent_reason: string | null; readonly note: string | null; readonly limits: readonly VendorLimitContract[]; }
export interface AccountLimitShareContract { readonly limitId: string; readonly unit: "request" | "distinct_stock"; readonly windowSeconds: number | null; readonly amount: number | null; readonly share: number | null; readonly divided: boolean; readonly evidence: LimitEvidenceContract; }
export interface AccountShareContract { readonly account: string; readonly environment: string; readonly percent: number; readonly admitsACall: boolean; readonly dailyShare: number | null; readonly rateWindowSeconds: number | null; readonly rateWindowShare: number | null; readonly limits: readonly AccountLimitShareContract[]; }
export interface ModelSeatEnvironmentLimitsContract { readonly max_concurrent_calls: number; readonly calls_per_minute: number; }
export interface ModelSeatContract { readonly seat: string; readonly measured_at: string; readonly environments: Readonly<Record<string, ModelSeatEnvironmentLimitsContract>>; }
export interface NightlyVerdictsContract { readonly checks: readonly string[]; readonly per_universe: readonly string[]; }

export const ENVIRONMENT_CONTRACT_VERSION = "3.0.0" as const;
export const ENVIRONMENT_CONTRACT_SHA256 = "d7d5bce14a04dcb0526dc0e09e5c6d91e71cfa8b8d16bb67c88e76968175246b" as const;
export const UTILIZATION_CEILING = 0.7 as const;
export const DAY_SECONDS = 86400 as const;
export const SENTINEL_MEMBERS: readonly SentinelMemberContract[] = [{"ticker":"AAPL","issuer_id":"issuer:cik:0000320193","listing_id":"listing:xnas:aapl","trait":"all_three_universes_september_fy"},{"ticker":"GOOGL","issuer_id":"issuer:cik:0001652044","listing_id":"listing:xnas:googl","trait":"dual_class_share_class_a"},{"ticker":"GOOG","issuer_id":"issuer:cik:0001652044","listing_id":"listing:xnas:goog","trait":"dual_class_share_class_c"},{"ticker":"ASML","issuer_id":"issuer:cik:0000937966","listing_id":"listing:xnas:asml","trait":"foreign_20f_ifrs_filer"},{"ticker":"HBAN","issuer_id":"issuer:cik:0000049196","listing_id":"listing:xnas:hban","trait":"regional_bank_financial_branch"},{"ticker":"CINF","issuer_id":"issuer:cik:0000020286","listing_id":"listing:xnas:cinf","trait":"property_casualty_insurer_branch"},{"ticker":"JPM","issuer_id":"issuer:cik:0000019617","listing_id":"listing:xnys:jpm","trait":"nyse_bank_tangible_common_equity"},{"ticker":"NVDA","issuer_id":"issuer:cik:0001045810","listing_id":"listing:xnas:nvda","trait":"stock_split_ten_for_one"},{"ticker":"BRK.B","issuer_id":"issuer:cik:0001067983","listing_id":"listing:xnys:brk.b","trait":"class_b_punctuation_no_gross_profit"},{"ticker":"CRWD","issuer_id":"issuer:cik:0001535527","listing_id":"listing:xnas:crwd","trait":"revenue_concept_including_assessed_tax"}] as const;
export const RUN_TIME_BOUNDS: RunTimeBoundsPolicy = { margin: 2.0, floorMinutes: 10.0, windowDays: 30 } as const;
export const ENVIRONMENT_TICKS: Readonly<Record<string, Readonly<Record<string, EnvironmentTickRule>>>> = {"preview":{},"staging":{"topt":{"scheduled":true,"cron":"45 22 * * *"},"qqq":{"scheduled":true,"cron":"5 1 * * *"},"sp500":{"scheduled":true,"cron":"45 1 * * *"},"canary":{"scheduled":true,"cron":"47 23 * * *"}},"production":{"topt":{"scheduled":true,"cron":"15 22 * * *"},"qqq":{"scheduled":true,"cron":"20 23 * * *"},"sp500":{"scheduled":false,"cron":"15 1 * * *"},"canary":{"scheduled":true,"cron":"47 23 * * *"}}} as const;
export const UNIVERSES: readonly UniverseRecordContract[] = [{"key":"topt","region":"us","id_prefix":"universe:topt-us-","lane_name":"topt","label":"TOPT: the frozen top-100 corpus","roles":["strategy"],"scheduled_somewhere":true},{"key":"qqq","region":"us","id_prefix":"universe:qqq-us-","lane_name":"universe-list:qqq","label":"Nasdaq-100 (QQQ) constituents, from the index operator's API","roles":["strategy","fund_consolidation"],"scheduled_somewhere":true},{"key":"sp500","region":"us","id_prefix":"universe:sp500-us-","lane_name":"universe-list:sp500","label":"S&P 500 constituents, from the index constituent plane","roles":["strategy"],"scheduled_somewhere":true},{"key":"canary","region":"us","id_prefix":"universe:canary-us-","lane_name":"universe-list:canary","label":"Deploy-verification canary: five issuers spanning every operating branch","roles":["sentinel"],"scheduled_somewhere":true}] as const;
export const VENDOR_ACCOUNTS: readonly VendorAccountContract[] = [{"name":"twelvedata","holder":"api_key","seats":["twelvedata"],"outside_consumers":false,"percent":{"preview":0,"staging":40,"production":60},"percent_reason":"Keeps 3/min and 4/min split across staging/production; preview makes zero calls (#1520).","note":null,"limits":[{"applies_to":null,"amount":8,"window_seconds":60,"id":"per_minute","unit":"request","counts_per":"api_key","divided":true,"evidence":{"checked_at":"2026-10-27","method":null,"reason":null,"kind":"published","source":["https://twelvedata.com/pricing"]},"shares":{"preview":0,"staging":3,"production":4}},{"applies_to":null,"amount":800,"window_seconds":86400,"id":"per_day","unit":"request","counts_per":"api_key","divided":true,"evidence":{"checked_at":"2026-10-27","method":null,"reason":null,"kind":"published","source":["https://twelvedata.com/pricing"]},"shares":{"preview":0,"staging":320,"production":480}}]},{"name":"sec_edgar","holder":"host_ip","seats":["sec","nport"],"outside_consumers":false,"percent":{"preview":0,"staging":40,"production":60},"percent_reason":"Gives staging 4/s and production 6/s while preview makes zero live calls (#1664 C2).","note":null,"limits":[{"applies_to":"Shared across sec and nport seats.","amount":10,"window_seconds":1,"id":"per_second","unit":"request","counts_per":"not_stated","divided":true,"evidence":{"checked_at":"2026-10-27","method":null,"reason":null,"kind":"published","source":["https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data"]},"shares":{"preview":0,"staging":4,"production":6}}]},{"name":"openfigi","holder":"api_key","seats":["openfigi"],"outside_consumers":false,"percent":{"preview":20,"staging":20,"production":60},"percent_reason":null,"note":"API key managed in infra2.","limits":[{"applies_to":"Mapping requests with API key.","amount":250,"window_seconds":60,"id":"per_minute","unit":"request","counts_per":"api_key","divided":true,"evidence":{"checked_at":"2026-10-27","method":null,"reason":null,"kind":"published","source":["https://www.openfigi.com/api/documentation"]},"shares":{"preview":50,"staging":50,"production":150}}]},{"name":"moomoo_opend","holder":"account","seats":["moomoo"],"outside_consumers":false,"percent":{"preview":0,"staging":40,"production":60},"percent_reason":"Doubles staging burst capacity from 6 to 12 per 30s without reducing production 18 per 30s (#1664 C2); preview makes zero live calls.","note":"K-line stock quota checked by moomoo_ledger.py (#1520).","limits":[{"applies_to":"All endpoints across processes and environments.","amount":30,"window_seconds":30,"id":"burst","unit":"request","counts_per":"not_stated","divided":true,"evidence":{"checked_at":"2026-10-27","method":null,"reason":null,"kind":"published","source":["https://openapi.futunn.com/futu-api-doc/en/quote/get-research-analyst-consensus.html","https://openapi.futunn.com/futu-api-doc/en/quote/get-research-rating-summary.html","https://openapi.futunn.com/futu-api-doc/en/quote/get-valuation-detail.html","https://openapi.futunn.com/futu-api-doc/en/quote/request-history-kline.html"]},"shares":{"preview":0,"staging":12,"production":18}},{"applies_to":"Distinct stocks in history K-line requests.","amount":null,"window_seconds":604800,"id":"kline_stocks","unit":"distinct_stock","counts_per":"account","divided":false,"evidence":{"checked_at":null,"method":null,"reason":"Account-tier quota unpublished by vendor (#1520).","kind":"not_known","source":[]},"shares":{"preview":null,"staging":null,"production":null}}]},{"name":"yahoo","holder":"host_ip","seats":["yahoo"],"outside_consumers":false,"percent":{"preview":20,"staging":20,"production":60},"percent_reason":null,"note":null,"limits":[{"applies_to":null,"amount":null,"window_seconds":null,"id":"vendor_figure","unit":"request","counts_per":"not_stated","divided":false,"evidence":{"checked_at":null,"method":null,"reason":"Unofficial chart endpoint publishes no limit.","kind":"none_published","source":[]},"shares":{"preview":null,"staging":null,"production":null}},{"applies_to":null,"amount":2,"window_seconds":1,"id":"pace","unit":"request","counts_per":"not_stated","divided":false,"evidence":{"checked_at":"2026-10-27","method":null,"reason":"Self-imposed pace to avoid runaway loops (#1520).","kind":"self_imposed","source":[]},"shares":{"preview":2,"staging":2,"production":2}}]},{"name":"nasdaq_index","holder":"host_ip","seats":["nasdaq-index"],"outside_consumers":false,"percent":{"preview":20,"staging":20,"production":60},"percent_reason":null,"note":null,"limits":[{"applies_to":null,"amount":null,"window_seconds":null,"id":"vendor_figure","unit":"request","counts_per":"not_stated","divided":false,"evidence":{"checked_at":null,"method":null,"reason":"No published limit.","kind":"none_published","source":[]},"shares":{"preview":null,"staging":null,"production":null}},{"applies_to":null,"amount":1,"window_seconds":1,"id":"pace","unit":"request","counts_per":"not_stated","divided":false,"evidence":{"checked_at":"2026-10-27","method":null,"reason":"Self-imposed weekly refresh pace (#1520).","kind":"self_imposed","source":[]},"shares":{"preview":1,"staging":1,"production":1}}]},{"name":"us_treasury","holder":"host_ip","seats":["us-treasury"],"outside_consumers":false,"percent":{"preview":20,"staging":20,"production":60},"percent_reason":null,"note":null,"limits":[{"applies_to":null,"amount":null,"window_seconds":null,"id":"vendor_figure","unit":"request","counts_per":"not_stated","divided":false,"evidence":{"checked_at":null,"method":null,"reason":"Public CSV states no limit.","kind":"none_published","source":[]},"shares":{"preview":null,"staging":null,"production":null}},{"applies_to":null,"amount":1,"window_seconds":1,"id":"pace","unit":"request","counts_per":"not_stated","divided":false,"evidence":{"checked_at":"2026-10-27","method":null,"reason":"Self-imposed nightly pace (#1520).","kind":"self_imposed","source":[]},"shares":{"preview":1,"staging":1,"production":1}}]},{"name":"model_plan","holder":"account","seats":["filing-extraction-model"],"outside_consumers":true,"percent":{"preview":20,"staging":20,"production":60},"percent_reason":null,"note":"Shared coding plan; per-environment pacing sits in model_seat.environments (#1520).","limits":[{"applies_to":null,"amount":null,"window_seconds":null,"id":"rate","unit":"request","counts_per":"not_stated","divided":false,"evidence":{"checked_at":"2026-10-27","method":null,"reason":"Provider returns code 1302 on limit without published numeric quota.","kind":"none_published","source":["https://docs.z.ai/api-reference/api-code"]},"shares":{"preview":null,"staging":null,"production":null}}]}] as const;
export const MODEL_SEAT: ModelSeatContract = {"seat":"filing-extraction-model","measured_at":"2026-10-21","environments":{"production":{"max_concurrent_calls":2,"calls_per_minute":10},"staging":{"max_concurrent_calls":2,"calls_per_minute":10}}} as const;
export const NIGHTLY_VERDICTS: NightlyVerdictsContract = {"checks":["market_data_freshness","model_key_health","output_invariants","job_run_health","topt_baseline","report_surface_proof","nightly_backtest","macro_freshness"],"per_universe":["datahub_confidence_report","question_coverage","theme_purity"]} as const;

function normEnv(env: string | null | undefined, fallback = "production"): string {
  const raw = (env || fallback).trim().toLowerCase() || fallback;
  return raw === "prod" ? "production" : raw;
}

export function vendorAccountRecord(name: string): VendorAccountContract {
  const found = VENDOR_ACCOUNTS.find((a) => a.name === name);
  if (!found) throw new Error(`"${name}" is no vendor account of the environment contract`);
  return found;
}

export function accountShare(account: string, environment: string | null | undefined): AccountShareContract {
  const row = vendorAccountRecord(account);
  const env = normEnv(environment);
  const known = Object.prototype.hasOwnProperty.call(row.percent, env) ? row.percent[env] : undefined;
  const percent = known !== undefined ? known : row.holder === "host_ip" ? 100 : 0;
  const limits: AccountLimitShareContract[] = row.limits.map((l) => ({
    limitId: l.id,
    unit: l.unit,
    windowSeconds: l.window_seconds,
    amount: l.amount,
    share: l.amount !== null && l.divided ? Math.floor((l.amount * percent) / 100) : l.amount,
    divided: l.divided,
    evidence: l.evidence,
  }));
  const reqLimits = limits.filter((l) => l.unit === "request");
  const daily = reqLimits.find((l) => l.windowSeconds === DAY_SECONDS && l.share !== null);
  const rate = reqLimits.find((l) => l.windowSeconds !== null && l.windowSeconds !== DAY_SECONDS && l.share !== null);
  return {
    account: row.name,
    environment: env,
    percent,
    admitsACall: reqLimits.every((l) => l.share === null || l.share >= 1),
    dailyShare: daily ? daily.share : null,
    rateWindowSeconds: rate ? rate.windowSeconds : null,
    rateWindowShare: rate ? rate.share : null,
    limits,
  };
}

export function universeRecord(key: string): UniverseRecordContract {
  const found = UNIVERSES.find((u) => u.key === key);
  if (!found) throw new Error(`"${key}" is no universe of the environment contract`);
  return found;
}

export function findUniverseOfId(universeId: string): string | null {
  const matches = UNIVERSES.filter((u) => universeId.startsWith(u.id_prefix));
  return matches.length === 0 ? null : matches.reduce((best, u) => (u.id_prefix.length > best.id_prefix.length ? u : best)).key;
}

export function universeOfId(universeId: string): string {
  const key = findUniverseOfId(universeId);
  if (key === null) throw new Error(`"${universeId}" starts with no universe id prefix of the environment contract`);
  return key;
}

export function idPrefixLike(key: string): string { return `${universeRecord(key).id_prefix}%`; }

export function isHeadScheduledFromContract(universeId: string, environment: string | null | undefined): boolean {
  const env = normEnv(environment);
  if (env === "preview") return true;
  const envTicks = ENVIRONMENT_TICKS[env];
  if (!envTicks) return true;
  const key = findUniverseOfId(universeId);
  return key === null ? true : (envTicks[key]?.scheduled ?? false);
}

export function expectsVerdictFromContract(check: string, environment: string | null | undefined): boolean {
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
}
