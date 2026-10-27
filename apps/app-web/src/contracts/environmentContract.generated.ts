/**
 * GENERATED FILE. DO NOT EDIT.
 *
 * Source: truealpha_contracts.environment_contract.load_environment_contract() (#1416).
 * Regenerate: uv run python common/contracts/conformance/export_environment_contract.py --write
 * Guard: common/contracts/tests/test_environment_contract.py fails when this file differs.
 */

export interface EnvironmentTickRule {
  readonly scheduled: boolean;
  readonly cron: string;
  readonly estimated_listings: number;
}

export interface UniverseRecordContract {
  readonly key: string;
  readonly region: string;
  readonly id_prefix: string;
  readonly lane_name: string;
  readonly label: string;
  readonly roles: readonly ("strategy" | "fund_consolidation" | "sentinel")[];
  /** Whether some environment schedules the universe, or runs it as a sentinel. */
  readonly scheduled_somewhere: boolean;
}

export interface SentinelMemberContract {
  readonly ticker: string;
  readonly issuer_id: string;
  readonly listing_id: string;
  readonly trait: string;
}

export const ENVIRONMENT_CONTRACT_VERSION = "3.0.0" as const;
export const ENVIRONMENT_CONTRACT_SHA256 = "6d7ff994eb01c77e625b81ac08774267c9fda0e9d4a0cb667de55262f4d85e03" as const;

export const SENTINEL_MEMBERS: readonly SentinelMemberContract[] = [
  {
    "ticker": "AAPL",
    "issuer_id": "issuer:cik:0000320193",
    "listing_id": "listing:xnas:aapl",
    "trait": "all_three_universes_september_fy"
  },
  {
    "ticker": "GOOGL",
    "issuer_id": "issuer:cik:0001652044",
    "listing_id": "listing:xnas:googl",
    "trait": "dual_class_share_class_a"
  },
  {
    "ticker": "GOOG",
    "issuer_id": "issuer:cik:0001652044",
    "listing_id": "listing:xnas:goog",
    "trait": "dual_class_share_class_c"
  },
  {
    "ticker": "ASML",
    "issuer_id": "issuer:cik:0000937966",
    "listing_id": "listing:xnas:asml",
    "trait": "foreign_20f_ifrs_filer"
  },
  {
    "ticker": "HBAN",
    "issuer_id": "issuer:cik:0000049196",
    "listing_id": "listing:xnas:hban",
    "trait": "regional_bank_financial_branch"
  },
  {
    "ticker": "CINF",
    "issuer_id": "issuer:cik:0000020286",
    "listing_id": "listing:xnas:cinf",
    "trait": "property_casualty_insurer_branch"
  },
  {
    "ticker": "JPM",
    "issuer_id": "issuer:cik:0000019617",
    "listing_id": "listing:xnys:jpm",
    "trait": "nyse_bank_tangible_common_equity"
  },
  {
    "ticker": "NVDA",
    "issuer_id": "issuer:cik:0001045810",
    "listing_id": "listing:xnas:nvda",
    "trait": "stock_split_ten_for_one"
  },
  {
    "ticker": "BRK.B",
    "issuer_id": "issuer:cik:0001067983",
    "listing_id": "listing:xnys:brk.b",
    "trait": "class_b_punctuation_no_gross_profit"
  },
  {
    "ticker": "CRWD",
    "issuer_id": "issuer:cik:0001535527",
    "listing_id": "listing:xnas:crwd",
    "trait": "revenue_concept_including_assessed_tax"
  }
] as const;

export const ENVIRONMENT_TICKS: Readonly<Record<string, Readonly<Record<string, EnvironmentTickRule>>>> = {
  "preview": {},
  "staging": {
    "topt": {
      "scheduled": true,
      "cron": "45 22 * * *",
      "estimated_listings": 21
    },
    "canary": {
      "scheduled": true,
      "cron": "47 23 * * *",
      "estimated_listings": 6
    }
  },
  "production": {
    "topt": {
      "scheduled": true,
      "cron": "15 22 * * *",
      "estimated_listings": 21
    },
    "qqq": {
      "scheduled": true,
      "cron": "20 23 * * *",
      "estimated_listings": 102
    },
    "sp500": {
      "scheduled": false,
      "cron": "15 1 * * *",
      "estimated_listings": 503
    },
    "canary": {
      "scheduled": true,
      "cron": "47 23 * * *",
      "estimated_listings": 6
    }
  }
} as const;

export const UNIVERSES: readonly UniverseRecordContract[] = [
  {
    "key": "topt",
    "region": "us",
    "id_prefix": "universe:topt-us-",
    "lane_name": "topt",
    "label": "TOPT: the frozen top-100 corpus",
    "roles": [
      "strategy"
    ],
    "scheduled_somewhere": true
  },
  {
    "key": "qqq",
    "region": "us",
    "id_prefix": "universe:qqq-us-",
    "lane_name": "universe-list:qqq",
    "label": "Nasdaq-100 (QQQ) constituents, from the index operator's API",
    "roles": [
      "fund_consolidation"
    ],
    "scheduled_somewhere": true
  },
  {
    "key": "sp500",
    "region": "us",
    "id_prefix": "universe:sp500-us-",
    "lane_name": "universe-list:sp500",
    "label": "S&P 500 constituents, from the index constituent plane",
    "roles": [],
    "scheduled_somewhere": false
  },
  {
    "key": "canary",
    "region": "us",
    "id_prefix": "universe:canary-us-",
    "lane_name": "universe-list:canary",
    "label": "Deploy-verification canary: five issuers spanning every operating branch",
    "roles": [
      "sentinel"
    ],
    "scheduled_somewhere": true
  }
] as const;

/** The record of the universe `key`. A key outside the record throws (#1522). */
export function universeRecord(key: string): UniverseRecordContract {
  const found = UNIVERSES.find((universe) => universe.key === key);
  if (!found) {
    throw new Error(`"${key}" is no universe of the environment contract`);
  }
  return found;
}

/** The key of the universe that governs `universeId`: the longest id prefix wins; the id is not split. Null when none matches. */
export function findUniverseOfId(universeId: string): string | null {
  const matches = UNIVERSES.filter((universe) => universeId.startsWith(universe.id_prefix));
  if (matches.length === 0) {
    return null;
  }
  return matches.reduce((best, universe) => (universe.id_prefix.length > best.id_prefix.length ? universe : best)).key;
}

/** The key of the universe that governs `universeId`. An id that starts with no id prefix throws. */
export function universeOfId(universeId: string): string {
  const key = findUniverseOfId(universeId);
  if (key === null) {
    throw new Error(`"${universeId}" starts with no universe id prefix of the environment contract`);
  }
  return key;
}

/** The SQL `like` pattern of every universe id of the universe `key`. */
export function idPrefixLike(key: string): string {
  return `${universeRecord(key).id_prefix}%`;
}

/**
 * Generated from environment_contract.v1.json (#1416 E1/E2, #1522).
 * Fails closed (returns true) when environment is unrecognised or universeId starts with no id prefix of the
 * records (the Python twin and the SQL function answer the same way until step 2 of #1522 retires the function).
 * A universe that another environment ticks and this one does not is not scheduled here.
 */
export function isHeadScheduledFromContract(
  universeId: string,
  environment: string | null | undefined,
): boolean {
  const rawEnv = (environment || "production").trim().toLowerCase() || "production";
  const env = rawEnv === "prod" ? "production" : rawEnv;
  if (env === "preview") {
    return true;
  }
  const envTicks = ENVIRONMENT_TICKS[env];
  if (!envTicks) {
    return true;
  }
  const key = findUniverseOfId(universeId);
  if (key === null) {
    return true;
  }
  const rule = envTicks[key];
  return rule ? rule.scheduled : false;
}
