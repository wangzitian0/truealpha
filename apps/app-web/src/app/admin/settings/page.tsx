import { api } from "@/client/api";
import { formatCell } from "@/client/format";
import { ReadStateNotice } from "@/components/read-state";
import { ResearchTable } from "@/components/research-table";
import { loadAuthed } from "../../_load";

export const dynamic = "force-dynamic";

function scalar(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export default async function AdminSettingsPage() {
  const { data: settings, error } = await loadAuthed("/admin/settings", (opts) => api.getAdminSettings(opts), { admin: true });
  const card = "rounded-xl border border-border bg-card p-4";
  const td = "px-4 py-2";
  const tdMono = `${td} font-mono text-xs`;

  const freshnessLimits = (settings?.freshness_limits ?? []) as Record<string, unknown>[];
  const environments = (settings?.environments ?? []) as Record<string, unknown>[];
  const universes = (settings?.universes ?? []) as Record<string, unknown>[];
  const vendorAccounts = (settings?.vendor_accounts ?? []) as Record<string, unknown>[];
  const sentinels = (settings?.sentinels ?? []) as Record<string, unknown>[];
  const exclusions = (settings?.metric_applicability ?? []) as Record<string, unknown>[];
  const thresholds = (settings?.confidence_thresholds ?? {}) as Record<string, string>;
  const codeCap = (settings?.code_server_capacity ?? {}) as Record<string, unknown>;
  const modelSeat = (settings?.model_seat ?? {}) as Record<string, unknown>;
  const modelEnvs = (modelSeat.environments ?? {}) as Record<string, Record<string, unknown>>;

  return (
    <section aria-labelledby="admin-settings-heading" className="space-y-8">
      <div>
        <h1 id="admin-settings-heading" className="text-2xl font-bold tracking-tight">Global Settings &amp; Contracts</h1>
        <p className="mt-1 text-sm text-gray-400">
          Read-only view of <code className="text-gray-300">environment_contract.v1.json</code>,{" "}
          <code className="text-gray-300">confidence_bands.v1.json</code>, and{" "}
          <code className="text-gray-300">mart.freshness_limit</code>.
        </p>
      </div>
      {error && <ReadStateNotice state={{ kind: "error", message: error }} />}
      {settings && (
        <>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4" data-testid="admin-settings-summary">
            <div className={card}>
              <div className="text-xs text-gray-400">Environment / Schema</div>
              <div className="mt-1 font-mono text-sm">
                {scalar(settings.environment)} · v{scalar(settings.schema_version)}
              </div>
              <div className="mt-1 font-mono text-xs text-gray-400">Contract {scalar(settings.contract_version)}</div>
            </div>
            <div className={card}>
              <div className="text-xs text-gray-400">Confidence bands</div>
              <div className="mt-1 font-mono text-sm">
                high ≥ {scalar(thresholds.high_from)} · med ≥ {scalar(thresholds.medium_from)}
              </div>
              <div className="mt-1 text-xs text-gray-400">Source: confidence_bands.v1.json</div>
            </div>
            <div className={card}>
              <div className="text-xs text-gray-400">Utilization ceiling</div>
              <div className="mt-1 font-mono text-sm">
                {Math.round(Number(settings.utilization_ceiling ?? 0) * 100)}% ({scalar(settings.utilization_ceiling)})
              </div>
              <div className="mt-1 text-xs text-gray-400">Compute &amp; vendor safety margin</div>
            </div>
            <div className={card}>
              <div className="text-xs text-gray-400">Code server budget</div>
              <div className="mt-1 font-mono text-sm">
                {scalar(codeCap.usable_mib)} / {scalar(codeCap.ceiling_mib)} MiB · {scalar(codeCap.max_concurrent_runs)} concurrent
              </div>
              <div className="mt-1 font-mono text-xs text-gray-400">
                server {scalar(codeCap.server_mib)} MiB · run {scalar(codeCap.run_mib)} MiB
              </div>
            </div>
          </div>

          <div className="space-y-2">
            <h2 className="text-lg font-semibold">Freshness limits (mart.freshness_limit)</h2>
            {freshnessLimits.length === 0 ? (
              <p className="text-sm text-gray-400">No freshness limits loaded.</p>
            ) : (
              <ResearchTable caption="Freshness limits" headers={["Limit key", "Max age (hours)", "Approx days"]}>
                {freshnessLimits.map((row) => {
                  const hours = Number(row.hours ?? 0);
                  return (
                    <tr key={String(row.limit_key ?? "")} className="border-t border-border font-mono text-xs">
                      <td className={td}>{scalar(row.limit_key)}</td>
                      <td className={td}>{scalar(row.hours)}h</td>
                      <td className={td}>{hours > 0 ? `${(hours / 24).toFixed(1)}d` : "—"}</td>
                    </tr>
                  );
                })}
              </ResearchTable>
            )}
          </div>

          <div className="space-y-2">
            <h2 className="text-lg font-semibold">Environments &amp; Dagster schedules</h2>
            <ResearchTable
              caption="Environment schedules"
              headers={["Environment", "Trigger", "Live vendor calls", "Scheduled universes", "Stopped", "List refresh"]}
            >
              {environments.map((env) => {
                const uList = Array.isArray(env.universes) ? (env.universes as Record<string, unknown>[]) : [];
                const active = uList.filter((u) => u.status === "scheduled").map((u) => String(u.key ?? ""));
                const stopped = uList.filter((u) => u.status !== "scheduled").map((u) => String(u.key ?? ""));
                const refresh = Array.isArray(env.list_refresh) ? (env.list_refresh as string[]) : [];
                return (
                  <tr key={String(env.environment ?? "")} className="border-t border-border font-mono text-xs">
                    <td className={td}>{scalar(env.environment)}</td>
                    <td className={td}>{scalar(env.trigger)}</td>
                    <td className={td}>{env.live_vendor_calls ? "yes" : "no"}</td>
                    <td className={td}>{active.length > 0 ? active.join(", ") : "none"}</td>
                    <td className={td}>{stopped.length > 0 ? stopped.join(", ") : "none"}</td>
                    <td className={td}>{refresh.length > 0 ? refresh.join(", ") : "none"}</td>
                  </tr>
                );
              })}
            </ResearchTable>
          </div>

          <div className="space-y-2">
            <h2 className="text-lg font-semibold">Universe registry</h2>
            <ResearchTable
              caption="Universe registry"
              headers={["Key", "Label", "ID prefix", "Lane", "Membership", "Roles", "Cron schedules"]}
            >
              {universes.map((u) => {
                const crons = (u.cron ?? {}) as Record<string, string>;
                const cronText = Object.entries(crons)
                  .map(([k, v]) => `${k}: ${v}`)
                  .join(" · ");
                return (
                  <tr key={String(u.key ?? "")} className="border-t border-border font-mono text-xs">
                    <td className={td}>{scalar(u.key)}{u.is_sentinel ? " (sentinel)" : ""}</td>
                    <td className={`${td} font-sans`}>{scalar(u.label)}</td>
                    <td className={td}>{scalar(u.id_prefix)}</td>
                    <td className={td}>{scalar(u.lane_name)}</td>
                    <td className={td}>{scalar(u.membership_kind)}</td>
                    <td className={td}>{Array.isArray(u.roles) ? u.roles.join(", ") : "—"}</td>
                    <td className={td}>{formatCell(cronText || null)}</td>
                  </tr>
                );
              })}
            </ResearchTable>
          </div>

          <div className="space-y-2">
            <h2 className="text-lg font-semibold">Vendor account quotas &amp; limits ({scalar(settings.environment)})</h2>
            <ResearchTable caption="Vendor account contracts" headers={["Account", "Holder", "Share %", "Seats", "Limits"]}>
              {vendorAccounts.map((a) => {
                const lims = Array.isArray(a.limits) ? (a.limits as Record<string, unknown>[]) : [];
                const limText = lims
                  .map((l) => `${scalar(l.limit_id)}: share=${scalar(l.share)}/${scalar(l.amount)} per ${scalar(l.window_seconds)}s (${scalar(l.unit)})`)
                  .join(" · ");
                return (
                  <tr key={String(a.account ?? "")} className="border-t border-border font-mono text-xs">
                    <td className={td}>{scalar(a.account)}</td>
                    <td className={td}>{scalar(a.holder)}</td>
                    <td className={td}>{scalar(a.percent)}%</td>
                    <td className={td}>{Array.isArray(a.seats) ? a.seats.join(", ") : "—"}</td>
                    <td className={td}>{limText || "—"}</td>
                  </tr>
                );
              })}
            </ResearchTable>
          </div>

          <div className="space-y-2">
            <h2 className="text-lg font-semibold">Model seat ({scalar(modelSeat.seat)})</h2>
            <ResearchTable caption="Model seat configuration" headers={["Environment", "Max concurrent", "Calls / min", "Probe cron", "Decoding"]}>
              {Object.entries(modelEnvs).map(([envKey, spec]) => (
                <tr key={envKey} className="border-t border-border font-mono text-xs">
                  <td className={td}>{envKey}</td>
                  <td className={td}>{scalar(spec.max_concurrent_calls)}</td>
                  <td className={td}>{scalar(spec.calls_per_minute)}</td>
                  <td className={td}>{scalar(spec.probe_cron)}</td>
                  <td className={td}>{scalar(spec.decoding_version)}</td>
                </tr>
              ))}
            </ResearchTable>
          </div>

          {(sentinels.length > 0 || exclusions.length > 0) && (
            <div className="grid gap-6 lg:grid-cols-2">
              <div className="space-y-2">
                <h2 className="text-lg font-semibold">Sentinels ({sentinels.length})</h2>
                <ResearchTable caption="Sentinels" headers={["Ticker", "Issuer", "Trait", "Defect refs"]}>
                  {sentinels.map((s) => (
                    <tr key={`${String(s.ticker)}-${String(s.issuer_id)}`} className="border-t border-border font-mono text-xs">
                      <td className={td}>{scalar(s.ticker)}</td>
                      <td className={td}>{scalar(s.issuer_id)}</td>
                      <td className={tdMono}>{scalar(s.trait)}</td>
                      <td className={td}>{Array.isArray(s.defect_refs) ? s.defect_refs.join(", ") : "—"}</td>
                    </tr>
                  ))}
                </ResearchTable>
              </div>
              <div className="space-y-2">
                <h2 className="text-lg font-semibold">Metric applicability exclusions ({exclusions.length})</h2>
                <ResearchTable caption="Metric applicability exclusions" headers={["Ticker", "Universe", "Metric", "Status", "Tracking"]}>
                  {exclusions.map((e) => (
                    <tr key={`${String(e.universe)}-${String(e.ticker)}-${String(e.metric)}`} className="border-t border-border font-mono text-xs">
                      <td className={td}>{scalar(e.ticker)}</td>
                      <td className={td}>{scalar(e.universe)}</td>
                      <td className={td}>{scalar(e.metric)}</td>
                      <td className={td}>{scalar(e.status)}</td>
                      <td className={td}>{scalar(e.tracking_issue)}</td>
                    </tr>
                  ))}
                </ResearchTable>
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
}
