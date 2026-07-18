/**
 * Server-only route loaders for the research dashboard — see #370/#371.
 *
 * Each loader takes an already-resolved `AccessContext | null` as an explicit
 * parameter — it never derives identity itself. That keeps this module
 * environment-agnostic (testable with a bare fixture context, no Next.js
 * request/cookie/DB machinery) and lets the caller be whichever identity
 * source is correct for that route: #371's `getServerPrincipal()` (real
 * session) for production route groups, or a literal test context. A `null`
 * context is `denied`; the mart adapter is not touched before authorization.
 * Reads go straight to the mart adapter, never through FastAPI.
 *
 * These return fully-resolved typed states. `loading` exists in the union for a future
 * streaming/suspense boundary; the current synchronous fixture read never returns it.
 */

import type { AccessContext } from "@/contracts/strategyRun";
import {
  FixtureMartReadAdapter,
  MartReadUnavailable,
  type ComparisonRow,
  type EntityDetail,
  type ModuleOverviewRow,
  type RankingRow,
  type TraceView,
} from "@/server/mart/research-read";
import { paginate, type PageInfo } from "@/server/mart/pagination";

export type ReadState<T> =
  | { kind: "loading" }
  | { kind: "ready"; data: T }
  | { kind: "empty" }
  | { kind: "unavailable"; reason: string }
  | { kind: "stale"; data: T; asOf: string }
  | { kind: "error"; message: string }
  | { kind: "denied" };

export interface OverviewData {
  modules: readonly ModuleOverviewRow[];
  latestCutoff: string | null;
}

export interface RankingData {
  cutoffAt: string;
  rows: readonly RankingRow[];
  page: PageInfo;
}

export interface ComparisonData {
  cutoffAt: string;
  rows: readonly ComparisonRow[];
  page: PageInfo;
}

/** Adapter injection is for tests only; production callers omit it. */
export interface MartAdapterLike {
  overview: FixtureMartReadAdapter["overview"];
  latestCutoff: FixtureMartReadAdapter["latestCutoff"];
  ranking: FixtureMartReadAdapter["ranking"];
  comparison: FixtureMartReadAdapter["comparison"];
  entityDetail: FixtureMartReadAdapter["entityDetail"];
  traceView: FixtureMartReadAdapter["traceView"];
}

function guard<T>(
  context: AccessContext | null,
  run: (adapter: MartAdapterLike, context: AccessContext) => ReadState<T>,
  adapter: MartAdapterLike,
): ReadState<T> {
  if (context === null) return { kind: "denied" };
  try {
    return run(adapter, context);
  } catch (error) {
    if (error instanceof MartReadUnavailable) return { kind: "unavailable", reason: error.reason };
    return { kind: "error", message: error instanceof Error ? error.message : String(error) };
  }
}

export function loadOverview(
  context: AccessContext | null,
  adapter: MartAdapterLike = new FixtureMartReadAdapter(),
): ReadState<OverviewData> {
  return guard<OverviewData>(context, (mart, ctx) => {
    const modules = mart.overview(ctx);
    const latestCutoff = mart.latestCutoff(ctx);
    return { kind: "ready", data: { modules, latestCutoff } };
  }, adapter);
}

export function loadRanking(
  context: AccessContext | null,
  params: { cutoffAt?: string; cursor?: string | null; limit?: number } = {},
  adapter: MartAdapterLike = new FixtureMartReadAdapter(),
): ReadState<RankingData> {
  return guard<RankingData>(context, (mart, ctx) => {
    const cutoffAt = params.cutoffAt ?? mart.latestCutoff(ctx);
    if (cutoffAt === null) return { kind: "empty" };
    const rows = mart.ranking(ctx, cutoffAt);
    if (rows.length === 0) return { kind: "empty" };
    const page = paginate(rows, params.cursor ?? null, params.limit);
    return { kind: "ready", data: { cutoffAt, rows: page.items, page: page.info } };
  }, adapter);
}

export function loadComparison(
  context: AccessContext | null,
  params: { cutoffAt?: string; cursor?: string | null; limit?: number } = {},
  adapter: MartAdapterLike = new FixtureMartReadAdapter(),
): ReadState<ComparisonData> {
  return guard<ComparisonData>(context, (mart, ctx) => {
    const cutoffAt = params.cutoffAt ?? mart.latestCutoff(ctx);
    if (cutoffAt === null) return { kind: "empty" };
    const rows = mart.comparison(ctx, cutoffAt);
    if (rows.length === 0) return { kind: "empty" };
    const page = paginate(rows, params.cursor ?? null, params.limit);
    return { kind: "ready", data: { cutoffAt, rows: page.items, page: page.info } };
  }, adapter);
}

export function loadEntityDetail(
  context: AccessContext | null,
  issuerId: string,
  adapter: MartAdapterLike = new FixtureMartReadAdapter(),
): ReadState<EntityDetail> {
  return guard<EntityDetail>(context, (mart, ctx) => {
    const detail = mart.entityDetail(ctx, issuerId);
    if (detail === null) return { kind: "empty" };
    return { kind: "ready", data: detail };
  }, adapter);
}

export function loadTrace(
  context: AccessContext | null,
  issuerId: string,
  cutoffAt: string,
  adapter: MartAdapterLike = new FixtureMartReadAdapter(),
): ReadState<TraceView> {
  return guard<TraceView>(context, (mart, ctx) => {
    const trace = mart.traceView(ctx, issuerId, cutoffAt);
    if (trace === null) return { kind: "empty" };
    return { kind: "ready", data: trace };
  }, adapter);
}
