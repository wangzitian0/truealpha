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
  readonly tick_type: "strategy" | "fund" | "index" | "sentinel";
  readonly estimated_listings: number;
}

export interface SentinelMemberContract {
  readonly ticker: string;
  readonly issuer_id: string;
  readonly listing_id: string;
  readonly trait: string;
}

export const ENVIRONMENT_CONTRACT_VERSION = "1.9.0" as const;
export const ENVIRONMENT_CONTRACT_SHA256 = "bf558bb071c9f5abc8e010b850350d3c4b4f795951924790b9cf1d2d4eea847d" as const;

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
      "tick_type": "strategy",
      "estimated_listings": 21
    },
    "canary": {
      "scheduled": true,
      "cron": "47 23 * * *",
      "tick_type": "sentinel",
      "estimated_listings": 6
    }
  },
  "production": {
    "topt": {
      "scheduled": true,
      "cron": "15 22 * * *",
      "tick_type": "strategy",
      "estimated_listings": 21
    },
    "qqq": {
      "scheduled": true,
      "cron": "20 23 * * *",
      "tick_type": "fund",
      "estimated_listings": 102
    },
    "sp500": {
      "scheduled": false,
      "cron": "15 1 * * *",
      "tick_type": "index",
      "estimated_listings": 503
    },
    "canary": {
      "scheduled": true,
      "cron": "47 23 * * *",
      "tick_type": "sentinel",
      "estimated_listings": 6
    }
  }
} as const;

export const KNOWN_TICK_KEYS: readonly string[] = ["canary", "qqq", "sp500", "topt"] as const;

/**
 * Generated from environment_contract.v1.json (#1416 E1/E2).
 * Fails closed (returns true) when environment is unrecognised or universeId maps to no tick key of the contract.
 * A tick key that another environment declares and this one does not is not scheduled here.
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
  const stripped = universeId.startsWith("universe:") ? universeId.slice("universe:".length) : universeId;
  const key = stripped.split("-", 1)[0] || "";
  const rule = envTicks[key];
  if (!rule) {
    return !KNOWN_TICK_KEYS.includes(key);
  }
  return rule.scheduled;
}
