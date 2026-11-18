import type { components } from "./openapi.generated";

export type Schemas = components["schemas"];
export type SessionResponse = Schemas["SessionResponse"];
export type LoginRequest = Schemas["LoginRequest"];
export type LoginResponse = Schemas["LoginResponse"];
export type LogoutResponse = Schemas["LogoutResponse"];
export type UniverseOverviewResponse = Schemas["UniverseOverviewResponse"];
export type PanelCoverageResponse = Schemas["PanelCoverageResponse"];
export type CoverageDecisionDto = Schemas["CoverageDecisionDto"];
export type MacroPanelResponse = Schemas["MacroPanelResponse"];
export type StrategyDecisionItem = Schemas["StrategyDecisionItemDto"];
export type WidePanelCell = Schemas["WidePanelCellDto"];
export type EntityDisplayItem = Schemas["EntityDisplayItem"];
export type RankingsResponse = Schemas["RankingsResponse"];
export type TraceResponse = Schemas["TraceResponse"];
export type EntityDetailResponse = Schemas["EntityDetailResponse"];
export type SupplyChainExposure = Schemas["SupplyChainExposure"];
export type AnalystRatings = Schemas["AnalystRatings"];
export type IssuerModuleUnavailable = Schemas["IssuerModuleUnavailable"];
export type ThemesResponse = Schemas["ThemesResponse"];
export type ThemePurityGroup = Schemas["ThemePurityGroupDto"];
export type ThemePurityRow = Schemas["ThemePurityRowDto"];
export type FundValuationsResponse = Schemas["FundValuationsResponse"];
export type FundValuation = Schemas["FundValuationDto"];
export type BacktestRunsResponse = Schemas["BacktestRunsResponse"];
export type BacktestRun = Schemas["BacktestRunDto"];
export type BacktestDetailResponse = Schemas["BacktestDetailResponse"];
export type BacktestValuation = Schemas["BacktestValuationDto"];
export type BacktestTrade = Schemas["BacktestTradeDto"];
export type LibraryReportResponse = Schemas["LibraryReportResponse"];
export type DocumentListResponse = Schemas["DocumentListResponse"];
export type DocumentDetailResponse = Schemas["DocumentDetailResponse"];
export type DocumentItem = Schemas["DocumentDto"];
export type DocumentRevision = Schemas["DocumentRevisionDto"];
export type CreateDocumentRequest = Schemas["CreateDocumentRequest"];
export type CreateDocumentResponse = Schemas["CreateDocumentResponse"];
export type DeleteDocumentResponse = Schemas["DeleteDocumentResponse"];
export type CardExportRequest = Schemas["CardExportRequest"];
export type CardExportResponse = Schemas["CardExportResponse"];
export type OpsOverviewResponse = Schemas["OpsOverviewResponse"];
export type DatahubStatsResponse = Schemas["DatahubStatsResponse"];
export type TriggerPipelineRequest = Schemas["TriggerPipelineRequest"];
export type TriggerPipelineResponse = Schemas["TriggerPipelineResponse"];

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  constructor(status: number, detail: string) {
    super(`API ${status}: ${detail}`);
    this.status = status;
    this.detail = detail;
  }
}

export function getBackendBaseUrl(): string {
  if (typeof window !== "undefined") return "";
  const configured = process.env.LLM_SERVICE_URL?.trim();
  if (configured) return configured.replace(/\/+$/, "");
  const suffix = process.env.ENV_SUFFIX;
  if (suffix !== undefined) return `http://truealpha-llm${suffix}:8000`;
  const env = (process.env.ENVIRONMENT || "").trim().toLowerCase();
  if (env === "staging") return "http://truealpha-llm-staging:8000";
  if (env === "prod" || env === "production") return "http://truealpha-llm:8000";
  return "http://127.0.0.1:8000";
}

export interface RequestOpts {
  cookieHeader?: string;
  authorization?: string;
}

type FetchFn = typeof fetch;
let fetchOverride: FetchFn | null = null;
export function __setApiFetch(fn: FetchFn | null): void {
  fetchOverride = fn;
}

export async function apiRequest<T>(path: string, init: RequestInit & RequestOpts = {}): Promise<T> {
  const { cookieHeader, authorization, ...rest } = init;
  const headers = new Headers(rest.headers);
  if (!headers.has("content-type") && rest.body !== undefined) headers.set("content-type", "application/json");
  if (cookieHeader) headers.set("cookie", cookieHeader);
  if (authorization) headers.set("authorization", authorization);
  const res = await (fetchOverride ?? fetch)(`${getBackendBaseUrl()}${path}`, { ...rest, headers, credentials: "include", cache: "no-store" });
  if (!res.ok) {
    let detail = res.statusText;
    try { const p = (await res.json()) as { detail?: string; error?: string }; detail = p.detail || p.error || detail; } catch {}
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

function qs(params: Record<string, string | number | undefined | null>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && String(v) !== "") sp.set(k, String(v));
  const encoded = sp.toString();
  return encoded ? `?${encoded}` : "";
}

const json = <T>(path: string, body: unknown, opts?: RequestOpts) =>
  apiRequest<T>(path, { method: "POST", body: JSON.stringify(body), ...opts });

export const api = {
  getSession: (opts?: RequestOpts) => apiRequest<SessionResponse>("/api/v1/auth/session", opts),
  login: (body: LoginRequest) => json<LoginResponse>("/api/v1/auth/login", body),
  logout: () => apiRequest<LogoutResponse>("/api/v1/auth/logout", { method: "POST" }),
  getPanelUniverses: (opts?: RequestOpts) => apiRequest<UniverseOverviewResponse>("/api/v1/panel/universes", opts),
  getPanelCoverage: (params?: { strategy_id?: string; universe?: string; cutoff?: string; node_key?: string; issuer_id?: string; limit?: number }, opts?: RequestOpts) =>
    apiRequest<PanelCoverageResponse>(`/api/v1/panel/coverage${qs(params ?? {})}`, opts),
  getPanelMacro: (opts?: RequestOpts) => apiRequest<MacroPanelResponse>("/api/v1/panel/macro", opts),
  getRankings: (params?: { strategy_id?: string; universe?: string; cutoff?: string; limit?: number }, opts?: RequestOpts) =>
    apiRequest<RankingsResponse>(`/api/v1/strategy/rankings${qs(params ?? {})}`, opts),
  listBacktests: (limit = 20, opts?: RequestOpts) =>
    apiRequest<BacktestRunsResponse>(`/api/v1/strategy/backtests${qs({ limit })}`, opts),
  getBacktest: (runId: string, opts?: RequestOpts) =>
    apiRequest<BacktestDetailResponse>(`/api/v1/strategy/backtests/${encodeURIComponent(runId)}`, opts),
  getFunds: (opts?: RequestOpts) => apiRequest<FundValuationsResponse>("/api/v1/entities/holdings", opts),
  getThemes: (opts?: RequestOpts) => apiRequest<ThemesResponse>("/api/v1/entities/themes", opts),
  getTrace: (params: { issuer_id: string; cutoff?: string }, opts?: RequestOpts) =>
    apiRequest<TraceResponse>(`/api/v1/entities/trace${qs(params)}`, opts),
  getSupplyChain: (issuerId: string, opts?: RequestOpts) =>
    apiRequest<SupplyChainExposure | IssuerModuleUnavailable>(`/api/v1/entities/${encodeURIComponent(issuerId)}/supply-chain`, opts),
  getAnalystRatings: (issuerId: string, opts?: RequestOpts) =>
    apiRequest<AnalystRatings | IssuerModuleUnavailable>(`/api/v1/entities/${encodeURIComponent(issuerId)}/analyst-ratings`, opts),
  getEntity: (issuerId: string, opts?: RequestOpts) =>
    apiRequest<EntityDetailResponse>(`/api/v1/entities/${encodeURIComponent(issuerId)}`, opts),
  getLibraryReport: (params?: { report_kind?: string; target_entity_ids?: string; cutoff_at?: string; format?: "json" | "markdown" }, opts?: RequestOpts) =>
    apiRequest<LibraryReportResponse>(`/api/v1/library/reports${qs(params ?? {})}`, opts),
  exportCard: (body: CardExportRequest, opts?: RequestOpts) => json<CardExportResponse>("/api/v1/library/cards/export", body, opts),
  listDocuments: (params?: { limit?: number; before_created_at?: string; before_document_id?: string }, opts?: RequestOpts) =>
    apiRequest<DocumentListResponse>(`/api/v1/library/documents${qs(params ?? {})}`, opts),
  createDocument: (body: CreateDocumentRequest, opts?: RequestOpts) => json<CreateDocumentResponse>("/api/v1/library/documents", body, opts),
  getDocument: (documentId: string, opts?: RequestOpts) =>
    apiRequest<DocumentDetailResponse>(`/api/v1/library/documents/${encodeURIComponent(documentId)}`, opts),
  deleteDocument: (documentId: string, opts?: RequestOpts) =>
    apiRequest<DeleteDocumentResponse>(`/api/v1/library/documents/${encodeURIComponent(documentId)}`, { method: "DELETE", ...opts }),
  getAdminOps: (opts?: RequestOpts) => apiRequest<OpsOverviewResponse>("/api/v1/admin/ops", opts),
  triggerPipeline: (body: TriggerPipelineRequest, opts?: RequestOpts) => json<TriggerPipelineResponse>("/api/v1/admin/triggers", body, opts),
  getAdminDatahub: (opts?: RequestOpts) => apiRequest<DatahubStatsResponse>("/api/v1/admin/datahub", opts),
};
