/**
 * Server-only route loaders for the research dashboard — see #370.
 *
 * Each loader re-derives the single-owner `AccessContext` from the Local/CI stand-in
 * (`getLocalAdminAccessContext`, driven by `TRUEALPHA_LOCAL_ADMIN_PRINCIPAL_ID`) on every
 * call — never from a cookie, header, query string, or client input (vision.md: personal
 * tool, single owner; no login yet). A null context is `denied`; the mart adapter is not
 * touched before authorization. Reads go straight to the mart adapter, never through
 * FastAPI.
 *
 * These return fully-resolved typed states. `loading` exists in the union for a future
 * streaming/suspense boundary; the current synchronous fixture read never returns it.
 */

import type { AccessContext } from "@/contracts/strategyRun";
import { getLocalAdminAccessContext } from "@/server/auth-context";
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
  run: (adapter: MartAdapterLike, context: AccessContext) => ReadState<T>,
  adapter: MartAdapterLike,
): ReadState<T> {
  const context = getLocalAdminAccessContext();
  if (context === null) return { kind: "denied" };
  try {
    return run(adapter, context);
  } catch (error) {
    if (error instanceof MartReadUnavailable) return { kind: "unavailable", reason: error.reason };
    return { kind: "error", message: error instanceof Error ? error.message : String(error) };
  }
}

export function loadOverview(adapter: MartAdapterLike = new FixtureMartReadAdapter()): ReadState<OverviewData> {
  return guard<OverviewData>((mart, context) => {
    const modules = mart.overview(context);
    const latestCutoff = mart.latestCutoff(context);
    return { kind: "ready", data: { modules, latestCutoff } };
  }, adapter);
}

export function loadRanking(
  params: { cutoffAt?: string; cursor?: string | null; limit?: number } = {},
  adapter: MartAdapterLike = new FixtureMartReadAdapter(),
): ReadState<RankingData> {
  return guard<RankingData>((mart, context) => {
    const cutoffAt = params.cutoffAt ?? mart.latestCutoff(context);
    if (cutoffAt === null) return { kind: "empty" };
    const rows = mart.ranking(context, cutoffAt);
    if (rows.length === 0) return { kind: "empty" };
    const page = paginate(rows, params.cursor ?? null, params.limit);
    return { kind: "ready", data: { cutoffAt, rows: page.items, page: page.info } };
  }, adapter);
}

export function loadComparison(
  params: { cutoffAt?: string; cursor?: string | null; limit?: number } = {},
  adapter: MartAdapterLike = new FixtureMartReadAdapter(),
): ReadState<ComparisonData> {
  return guard<ComparisonData>((mart, context) => {
    const cutoffAt = params.cutoffAt ?? mart.latestCutoff(context);
    if (cutoffAt === null) return { kind: "empty" };
    const rows = mart.comparison(context, cutoffAt);
    if (rows.length === 0) return { kind: "empty" };
    const page = paginate(rows, params.cursor ?? null, params.limit);
    return { kind: "ready", data: { cutoffAt, rows: page.items, page: page.info } };
  }, adapter);
}

export function loadEntityDetail(
  issuerId: string,
  adapter: MartAdapterLike = new FixtureMartReadAdapter(),
): ReadState<EntityDetail> {
  return guard<EntityDetail>((mart, context) => {
    const detail = mart.entityDetail(context, issuerId);
    if (detail === null) return { kind: "empty" };
    return { kind: "ready", data: detail };
  }, adapter);
}

export function loadTrace(
  issuerId: string,
  cutoffAt: string,
  adapter: MartAdapterLike = new FixtureMartReadAdapter(),
): ReadState<TraceView> {
  return guard<TraceView>((mart, context) => {
    const trace = mart.traceView(context, issuerId, cutoffAt);
    if (trace === null) return { kind: "empty" };
    return { kind: "ready", data: trace };
  }, adapter);
}
