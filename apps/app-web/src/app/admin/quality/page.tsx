import { api } from "@/client/api";
import { ReadStateNotice } from "@/components/read-state";
import { loadAuthed } from "../../_load";

export const dynamic = "force-dynamic";

const STYLE: Record<string, string> = {
  ok: "border-emerald-500/40 text-emerald-300",
  warn: "border-amber-500/40 text-amber-300",
  crit: "border-rose-500/40 text-rose-300",
  muted: "border-gray-700 text-gray-400",
};

export default async function AdminQualityPage() {
  const { data: quality, error } = await loadAuthed("/admin/quality", (opts) => api.getAdminQuality(opts), { admin: true });

  return (
    <section aria-labelledby="quality-heading" className="space-y-6">
      <div>
        <h1 id="quality-heading" className="text-2xl font-bold tracking-tight">L1–L5 Quality Funnel</h1>
        <p className="mt-1 text-sm text-gray-400">
          {quality?.run_id ? `Governed run ${quality.run_id}` : "Latest governed quality funnel across Capture -> Facts -> Factors -> Strategy -> Served Head."}
        </p>
      </div>
      <div className="rounded-xl border border-border bg-card p-5 space-y-4">
        <h2 className="font-semibold">L0–L5 funnel</h2>
        {error && <ReadStateNotice state={{ kind: "error", message: error }} />}
        {quality && !quality.available && <ReadStateNotice state={{ kind: "unavailable", reason: quality.reason ?? "unavailable" }} />}
        {!error && !quality && <ReadStateNotice state={{ kind: "unavailable", reason: "not_computed" }} />}
        {quality?.available && (
          <div className="grid gap-4 md:grid-cols-5">
            {(quality.layers ?? []).map((layer) => (
              <div key={layer.key} data-funnel-layer={layer.key} className={`rounded-xl border bg-card p-4 ${STYLE[layer.status] ?? STYLE.muted}`}>
                <div className="text-xs font-mono uppercase">{layer.key} · {layer.title}</div>
                <div className="mt-2 text-lg font-bold text-white">{layer.headline}</div>
                <div className="mt-1 text-xs text-gray-400">{layer.detail}</div>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
