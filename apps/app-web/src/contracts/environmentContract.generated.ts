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
}

export interface RunTimeBoundsPolicy {
  readonly margin: number;
  readonly floorMinutes: number;
  readonly windowDays: number;
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

export type EvidenceKindContract =
  | "published"
  | "measured"
  | "self_imposed"
  | "none_published"
  | "not_known";

export interface LimitEvidenceContract {
  readonly kind: EvidenceKindContract;
  readonly source: readonly string[];
  readonly checked_at: string | null;
  readonly method: string | null;
  readonly reason: string | null;
}

export interface VendorLimitContract {
  readonly id: string;
  readonly unit: "request" | "distinct_stock";
  readonly amount: number | null;
  readonly window_seconds: number | null;
  readonly counts_per: "api_key" | "host_ip" | "account" | "not_stated";
  readonly applies_to: string | null;
  readonly divided: boolean;
  readonly evidence: LimitEvidenceContract;
  readonly shares: Readonly<Record<string, number | null>>;
}

export interface VendorAccountContract {
  readonly name: string;
  readonly holder: "api_key" | "host_ip" | "account";
  readonly seats: readonly string[];
  readonly outside_consumers: boolean;
  readonly percent: Readonly<Record<string, number>>;
  readonly percent_reason: string | null;
  readonly note: string | null;
  readonly limits: readonly VendorLimitContract[];
}

export interface AccountLimitShareContract {
  readonly limitId: string;
  readonly unit: "request" | "distinct_stock";
  readonly windowSeconds: number | null;
  readonly amount: number | null;
  readonly share: number | null;
  readonly divided: boolean;
  readonly evidence: LimitEvidenceContract;
}

export interface AccountShareContract {
  readonly account: string;
  readonly environment: string;
  readonly percent: number;
  readonly admitsACall: boolean;
  readonly dailyShare: number | null;
  readonly rateWindowSeconds: number | null;
  readonly rateWindowShare: number | null;
  readonly limits: readonly AccountLimitShareContract[];
}

export interface ModelSeatEnvironmentLimitsContract {
  readonly max_concurrent_calls: number;
  readonly calls_per_minute: number;
}

export interface ModelSeatContract {
  readonly seat: string;
  readonly measured_at: string;
  readonly environments: Readonly<Record<string, ModelSeatEnvironmentLimitsContract>>;
}

export const ENVIRONMENT_CONTRACT_VERSION = "3.0.0" as const;
export const ENVIRONMENT_CONTRACT_SHA256 = "8013ef48bb8888f1ed1419ee375762883682c707d2c9ed78e5118cb92336edc2" as const;
export const UTILIZATION_CEILING = 0.7 as const;
export const DAY_SECONDS = 86400 as const;

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

export const RUN_TIME_BOUNDS: RunTimeBoundsPolicy = {
  margin: 2.0,
  floorMinutes: 10.0,
  windowDays: 30,
} as const;

export const ENVIRONMENT_TICKS: Readonly<Record<string, Readonly<Record<string, EnvironmentTickRule>>>> = {
  "preview": {},
  "staging": {
    "topt": {
      "scheduled": true,
      "cron": "45 22 * * *"
    },
    "canary": {
      "scheduled": true,
      "cron": "47 23 * * *"
    }
  },
  "production": {
    "topt": {
      "scheduled": true,
      "cron": "15 22 * * *"
    },
    "qqq": {
      "scheduled": true,
      "cron": "20 23 * * *"
    },
    "sp500": {
      "scheduled": false,
      "cron": "15 1 * * *"
    },
    "canary": {
      "scheduled": true,
      "cron": "47 23 * * *"
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

export const VENDOR_ACCOUNTS: readonly VendorAccountContract[] = [
  {
    "name": "twelvedata",
    "holder": "api_key",
    "seats": [
      "twelvedata"
    ],
    "outside_consumers": false,
    "percent": {
      "preview": 0,
      "staging": 40,
      "production": 60
    },
    "percent_reason": "Working choice OD3 A of #1520, recorded 2026-10-27 and not yet an owner approval. It keeps the split that staging and production ran before this row: 3 and 4 calls a minute, 320 and 480 a day. Preview calls no Twelve Data endpoint.",
    "note": null,
    "limits": [
      {
        "id": "per_minute",
        "unit": "request",
        "amount": 8,
        "window_seconds": 60,
        "counts_per": "api_key",
        "applies_to": null,
        "divided": true,
        "evidence": {
          "kind": "published",
          "source": [
            "https://twelvedata.com/pricing"
          ],
          "checked_at": "2026-10-27",
          "method": null,
          "reason": null
        },
        "shares": {
          "preview": 0,
          "staging": 3,
          "production": 4
        }
      },
      {
        "id": "per_day",
        "unit": "request",
        "amount": 800,
        "window_seconds": 86400,
        "counts_per": "api_key",
        "applies_to": null,
        "divided": true,
        "evidence": {
          "kind": "published",
          "source": [
            "https://twelvedata.com/pricing"
          ],
          "checked_at": "2026-10-27",
          "method": null,
          "reason": null
        },
        "shares": {
          "preview": 0,
          "staging": 320,
          "production": 480
        }
      }
    ]
  },
  {
    "name": "sec_edgar",
    "holder": "host_ip",
    "seats": [
      "sec",
      "nport"
    ],
    "outside_consumers": false,
    "percent": {
      "preview": 20,
      "staging": 20,
      "production": 60
    },
    "percent_reason": null,
    "note": null,
    "limits": [
      {
        "id": "per_second",
        "unit": "request",
        "amount": 10,
        "window_seconds": 1,
        "counts_per": "not_stated",
        "applies_to": "All SEC hosts that the sec and nport seats call share one counter. The vendor publishes no daily limit.",
        "divided": true,
        "evidence": {
          "kind": "published",
          "source": [
            "https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data"
          ],
          "checked_at": "2026-10-27",
          "method": null,
          "reason": null
        },
        "shares": {
          "preview": 2,
          "staging": 2,
          "production": 6
        }
      }
    ]
  },
  {
    "name": "openfigi",
    "holder": "api_key",
    "seats": [
      "openfigi"
    ],
    "outside_consumers": false,
    "percent": {
      "preview": 20,
      "staging": 20,
      "production": 60
    },
    "percent_reason": null,
    "note": "Whether staging and production use one key is not verified here. The key is held in infra2.",
    "limits": [
      {
        "id": "per_minute",
        "unit": "request",
        "amount": 250,
        "window_seconds": 60,
        "counts_per": "api_key",
        "applies_to": "Mapping requests with an API key. Each window starts with the first request.",
        "divided": true,
        "evidence": {
          "kind": "published",
          "source": [
            "https://www.openfigi.com/api/documentation"
          ],
          "checked_at": "2026-10-27",
          "method": null,
          "reason": null
        },
        "shares": {
          "preview": 50,
          "staging": 50,
          "production": 150
        }
      }
    ]
  },
  {
    "name": "moomoo_opend",
    "holder": "account",
    "seats": [
      "moomoo"
    ],
    "outside_consumers": false,
    "percent": {
      "preview": 20,
      "staging": 20,
      "production": 60
    },
    "percent_reason": null,
    "note": "The shares 6/6/18 come from the default percent and are a working choice of 2026-10-27 (OD5 A of #1520), not owner approval. CapacityGate admits every moomoo call and moomoo_ledger.py checks the K-line stock quota from this row.",
    "limits": [
      {
        "id": "burst",
        "unit": "request",
        "amount": 30,
        "window_seconds": 30,
        "counts_per": "not_stated",
        "applies_to": "All endpoints, all processes and all environments, at the smallest published figure.",
        "divided": true,
        "evidence": {
          "kind": "published",
          "source": [
            "https://openapi.futunn.com/futu-api-doc/en/quote/get-research-analyst-consensus.html",
            "https://openapi.futunn.com/futu-api-doc/en/quote/get-research-rating-summary.html",
            "https://openapi.futunn.com/futu-api-doc/en/quote/get-valuation-detail.html",
            "https://openapi.futunn.com/futu-api-doc/en/quote/request-history-kline.html"
          ],
          "checked_at": "2026-10-27",
          "method": null,
          "reason": null
        },
        "shares": {
          "preview": 6,
          "staging": 6,
          "production": 18
        }
      },
      {
        "id": "kline_stocks",
        "unit": "distinct_stock",
        "amount": null,
        "window_seconds": 604800,
        "counts_per": "account",
        "applies_to": "Distinct stocks of history K-line requests.",
        "divided": false,
        "evidence": {
          "kind": "not_known",
          "source": [],
          "checked_at": null,
          "method": null,
          "reason": "The vendor sets this quota by the tier of the account and states no figure for ours. No stored reading exists yet. Step 2 of #1520 stores get_history_kl_quota readings."
        },
        "shares": {
          "preview": null,
          "staging": null,
          "production": null
        }
      }
    ]
  },
  {
    "name": "yahoo",
    "holder": "host_ip",
    "seats": [
      "yahoo"
    ],
    "outside_consumers": false,
    "percent": {
      "preview": 20,
      "staging": 20,
      "production": 60
    },
    "percent_reason": null,
    "note": null,
    "limits": [
      {
        "id": "vendor_figure",
        "unit": "request",
        "amount": null,
        "window_seconds": null,
        "counts_per": "not_stated",
        "applies_to": null,
        "divided": false,
        "evidence": {
          "kind": "none_published",
          "source": [],
          "checked_at": null,
          "method": null,
          "reason": "The chart endpoint is unofficial. It has no published limit and no SLA (init.md section 9)."
        },
        "shares": {
          "preview": null,
          "staging": null,
          "production": null
        }
      },
      {
        "id": "pace",
        "unit": "request",
        "amount": 2,
        "window_seconds": 1,
        "counts_per": "not_stated",
        "applies_to": null,
        "divided": false,
        "evidence": {
          "kind": "self_imposed",
          "source": [],
          "checked_at": "2026-10-27",
          "method": null,
          "reason": "Our own pace, carried over from source_registrations.py. It stops a runaway loop. OD2 A of #1520 deletes the daily number."
        },
        "shares": {
          "preview": 2,
          "staging": 2,
          "production": 2
        }
      }
    ]
  },
  {
    "name": "nasdaq_index",
    "holder": "host_ip",
    "seats": [
      "nasdaq-index"
    ],
    "outside_consumers": false,
    "percent": {
      "preview": 20,
      "staging": 20,
      "production": 60
    },
    "percent_reason": null,
    "note": null,
    "limits": [
      {
        "id": "vendor_figure",
        "unit": "request",
        "amount": null,
        "window_seconds": null,
        "counts_per": "not_stated",
        "applies_to": null,
        "divided": false,
        "evidence": {
          "kind": "none_published",
          "source": [],
          "checked_at": null,
          "method": null,
          "reason": "The index-constituent endpoint publishes no limit."
        },
        "shares": {
          "preview": null,
          "staging": null,
          "production": null
        }
      },
      {
        "id": "pace",
        "unit": "request",
        "amount": 1,
        "window_seconds": 1,
        "counts_per": "not_stated",
        "applies_to": null,
        "divided": false,
        "evidence": {
          "kind": "self_imposed",
          "source": [],
          "checked_at": "2026-10-27",
          "method": null,
          "reason": "Our own pace, carried over from source_registrations.py. The weekly refresh makes one call. OD2 A of #1520 deletes the daily number."
        },
        "shares": {
          "preview": 1,
          "staging": 1,
          "production": 1
        }
      }
    ]
  },
  {
    "name": "us_treasury",
    "holder": "host_ip",
    "seats": [
      "us-treasury"
    ],
    "outside_consumers": false,
    "percent": {
      "preview": 20,
      "staging": 20,
      "production": 60
    },
    "percent_reason": null,
    "note": null,
    "limits": [
      {
        "id": "vendor_figure",
        "unit": "request",
        "amount": null,
        "window_seconds": null,
        "counts_per": "not_stated",
        "applies_to": null,
        "divided": false,
        "evidence": {
          "kind": "none_published",
          "source": [],
          "checked_at": null,
          "method": null,
          "reason": "The public CSV on home.treasury.gov needs no key and states no limit."
        },
        "shares": {
          "preview": null,
          "staging": null,
          "production": null
        }
      },
      {
        "id": "pace",
        "unit": "request",
        "amount": 1,
        "window_seconds": 1,
        "counts_per": "not_stated",
        "applies_to": null,
        "divided": false,
        "evidence": {
          "kind": "self_imposed",
          "source": [],
          "checked_at": "2026-10-27",
          "method": null,
          "reason": "Our own pace, carried over from source_registrations.py. The nightly run makes one request. OD2 A of #1520 deletes the daily number."
        },
        "shares": {
          "preview": 1,
          "staging": 1,
          "production": 1
        }
      }
    ]
  },
  {
    "name": "model_plan",
    "holder": "account",
    "seats": [
      "filing-extraction-model"
    ],
    "outside_consumers": true,
    "percent": {
      "preview": 20,
      "staging": 20,
      "production": 60
    },
    "percent_reason": null,
    "note": "Other users of the coding plan spend the same rate limit. The provider states no whole-account figure; the per-environment pace stays in model_seat.environments (#1520).",
    "limits": [
      {
        "id": "rate",
        "unit": "request",
        "amount": null,
        "window_seconds": null,
        "counts_per": "not_stated",
        "applies_to": null,
        "divided": false,
        "evidence": {
          "kind": "none_published",
          "source": [
            "https://docs.z.ai/api-reference/api-code"
          ],
          "checked_at": "2026-10-27",
          "method": null,
          "reason": "Code 1302 means that the rate limit is reached. The page states no figure and no scope."
        },
        "shares": {
          "preview": null,
          "staging": null,
          "production": null
        }
      }
    ]
  }
] as const;

export const MODEL_SEAT: ModelSeatContract = {
  "seat": "filing-extraction-model",
  "measured_at": "2026-10-21",
  "environments": {
    "production": {
      "max_concurrent_calls": 2,
      "calls_per_minute": 10
    },
    "staging": {
      "max_concurrent_calls": 1,
      "calls_per_minute": 3
    }
  }
} as const;

/** The vendor account row `name` of the environment contract (#1520). */
export function vendorAccountRecord(name: string): VendorAccountContract {
  const found = VENDOR_ACCOUNTS.find((account) => account.name === name);
  if (!found) {
    throw new Error(`"${name}" is no vendor account of the environment contract`);
  }
  return found;
}

/** What `environment` may spend of each limit of `account` (#1520). */
export function accountShare(
  account: string,
  environment: string | null | undefined,
): AccountShareContract {
  const row = vendorAccountRecord(account);
  const rawEnv = (environment || "production").trim().toLowerCase() || "production";
  const env = rawEnv === "prod" ? "production" : rawEnv;
  const knownPercent = Object.prototype.hasOwnProperty.call(row.percent, env) ? row.percent[env] : undefined;
  const percent = knownPercent !== undefined ? knownPercent : row.holder === "host_ip" ? 100 : 0;
  const limits: AccountLimitShareContract[] = row.limits.map((limit) => ({
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
  }));
  const requestLimits = limits.filter((limit) => limit.unit === "request");
  const admitsACall = requestLimits.every((limit) => limit.share === null || limit.share >= 1);
  const daily = requestLimits.find((limit) => limit.windowSeconds === DAY_SECONDS && limit.share !== null);
  const rate = requestLimits.find(
    (limit) => limit.windowSeconds !== null && limit.windowSeconds !== DAY_SECONDS && limit.share !== null,
  );
  return {
    account: row.name,
    environment: env,
    percent,
    admitsACall,
    dailyShare: daily ? daily.share : null,
    rateWindowSeconds: rate ? rate.windowSeconds : null,
    rateWindowShare: rate ? rate.share : null,
    limits,
  };
}

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
