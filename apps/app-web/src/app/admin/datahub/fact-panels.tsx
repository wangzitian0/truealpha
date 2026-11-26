import { formatCell, formatRatio, formatShortSha } from "@/client/format";
import { HeadRollbackControl } from "@/components/head-rollback-control";
import { ResearchTable } from "@/components/research-table";

export function safeCell(value: unknown): string {
  if (value === null || value === undefined || value === "") return formatCell(null);
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return formatCell(value);
  return formatCell(JSON.stringify(value));
}

export function DataSection({ title, headers, rows }: { title: string; headers: string[]; rows: (string | number)[][] }) {
  return (
    <div className="space-y-2">
      <h2 className="text-lg font-semibold">{title}</h2>
      <ResearchTable caption={title} headers={headers}>
        {rows.map((cells, i) => (
          <tr key={`${i}-${cells[0] ?? ""}`} className="border-t border-border font-mono text-xs">
            {cells.map((v, j) => (
              <td key={j} className="px-3 py-2">
                {String(v)}
              </td>
            ))}
          </tr>
        ))}
      </ResearchTable>
    </div>
  );
}

const QUESTION_CLASSES = ["world_absence", "not_applicable", "pipeline_gap", "transient", "disputed"] as const;
const QUESTION_VIOLATIONS = ["no_row", "no_reason", "unregistered", "status_class_conflict"] as const;

function countMapSummary(obj: unknown, keys: readonly string[]): string {
  if (!obj || typeof obj !== "object") return formatCell(null);
  const rec = obj as Record<string, unknown>;
  const parts: string[] = [];
  for (const k of keys) {
    const val = Number(rec[k] ?? 0);
    if (Number.isFinite(val) && val > 0) parts.push(`${k}=${val}`);
  }
  for (const [k, raw] of Object.entries(rec)) {
    if (!keys.includes(k)) {
      const val = Number(raw ?? 0);
      if (Number.isFinite(val) && val > 0) parts.push(`${k}=${val}`);
    }
  }
  return parts.length > 0 ? parts.join(", ") : "0";
}

function deltaSummary(qObj: Record<string, unknown>): string {
  const change = qObj.change as Record<string, unknown> | undefined;
  if (!change || typeof change !== "object") return formatCell(null);
  const answeredDelta = Number(change.answered ?? 0);
  const sign = answeredDelta > 0 ? `+${answeredDelta}` : String(answeredDelta);
  if (Array.isArray(change.subjects_changed)) {
    const subjects = change.subjects_changed as unknown[];
    const sample = subjects.slice(0, 3).map((s) => String(s)).join(", ");
    return subjects.length > 0 ? `${sign} (${subjects.length}: ${sample})` : sign;
  }
  const changedCount = Number(change.subjects_changed ?? 0);
  const examples = Array.isArray(change.examples) ? (change.examples as unknown[]) : [];
  const sample = examples
    .slice(0, 3)
    .map((ex) => {
      if (typeof ex === "string") return ex;
      if (ex && typeof ex === "object") {
        const rec = ex as Record<string, unknown>;
        return String(rec.subject ?? rec.subject_id ?? rec.ticker ?? rec.issuer_id ?? JSON.stringify(rec));
      }
      return String(ex);
    })
    .join(", ");
  if (changedCount > 0) {
    return sample ? `${sign} (${changedCount}: ${sample})` : `${sign} (${changedCount})`;
  }
  return sign;
}

export function QuestionCoverageDetails({ questionCoverage }: { questionCoverage: Record<string, unknown>[] }) {
  const matrixRows: (string | number)[][] = [];
  for (const row of questionCoverage) {
    const universe = safeCell(row.universe_id);
    const cutoff = safeCell(row.cutoff);
    const payload = row.payload as Record<string, unknown> | undefined;
    const prevRun = payload?.previous_run_id ? formatShortSha(String(payload.previous_run_id)) : formatCell(null);
    const questions = (payload?.questions ?? {}) as Record<string, Record<string, unknown>>;
    const qKeys = Object.keys(questions).sort();
    if (qKeys.length === 0) {
      matrixRows.push([universe, cutoff, prevRun, formatCell(null), formatCell(null), formatCell(null), formatCell(null), formatCell(null)]);
      continue;
    }
    for (const qk of qKeys) {
      const qVal = questions[qk] ?? {};
      const answered = Number(qVal.answered ?? 0);
      const denom = Number(qVal.denominator ?? 0);
      const na = Number(qVal.not_applicable ?? 0);
      const pct = denom > 0 ? formatRatio((answered * 100) / denom) : formatCell(null);
      matrixRows.push([
        universe,
        qk,
        `${answered}/${denom} (${pct}% · na=${na})`,
        countMapSummary(qVal.by_class, QUESTION_CLASSES),
        countMapSummary(qVal.violations, QUESTION_VIOLATIONS),
        `${prevRun} · ${deltaSummary(qVal)}`,
        cutoff,
        safeCell(row.created_at),
      ]);
    }
  }
  return (
    <DataSection
      title="Question coverage detail (layers & deltas)"
      headers={["Universe", "Question", "Answered / Denom", "By class", "Violations", "Delta vs prior", "Cutoff", "Created"]}
      rows={matrixRows}
    />
  );
}

export function DatahubFactPanels({ stats }: { stats: Record<string, unknown> }) {
  const oracleDisagreements = (stats.oracle_disagreements ?? []) as Record<string, unknown>[];
  const servedHeads = (stats.served_heads ?? []) as Record<string, unknown>[];
  const servedArtifacts = (stats.served_artifacts ?? []) as Record<string, unknown>[];
  const captureRuns = (stats.capture_runs ?? []) as Record<string, unknown>[];
  const outputInvariants = (stats.output_invariants ?? []) as Record<string, unknown>[];
  const validation = (stats.validation ?? []) as Record<string, unknown>[];
  const nightlyVerdicts = (stats.nightly_verdicts ?? []) as Record<string, unknown>[];
  const confidence = (stats.confidence ?? []) as Record<string, unknown>[];
  const capacity = (stats.capacity ?? []) as Record<string, unknown>[];
  const model1302 = (stats.model_1302_by_day ?? []) as Record<string, unknown>[];
  const llmUsage = (stats.llm_usage ?? []) as Record<string, unknown>[];
  const recentCalls = (stats.recent_calls ?? []) as Record<string, unknown>[];
  const backfillHealth = (stats.backfill_health ?? []) as Record<string, unknown>[];
  const triggerQueue = (stats.trigger_queue ?? []) as Record<string, unknown>[];
  const rollbackRequests = (stats.rollback_requests ?? []) as Record<string, unknown>[];
  const dbHygiene = (stats.db_hygiene ?? {}) as Record<string, unknown>;
  const zeroScanIndexes = (dbHygiene.zero_scan_indexes ?? []) as Record<string, unknown>[];
  const idleTables = (dbHygiene.idle_tables ?? []) as Record<string, unknown>[];
  const statsReset = dbHygiene.stats_reset as string | null | undefined;

  return (
    <div className="space-y-8">
      <DataSection
        title="Oracle disagreements (sample)"
        headers={["Universe", "Subject", "Field", "Period", "Kind", "Source", "Served", "Oracle", "Ratio / Tol", "Reason", "Compared at"]}
        rows={oracleDisagreements.map((d) => [
          safeCell(d.universe_id),
          safeCell(d.subject_id),
          safeCell(d.field),
          safeCell(d.period),
          safeCell(d.oracle_kind),
          safeCell(d.source),
          safeCell(d.served_value),
          safeCell(d.oracle_value),
          `${safeCell(d.ratio)} / ${safeCell(d.tolerance)}`,
          safeCell(d.reason),
          safeCell(d.compared_at),
        ])}
      />

      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Served heads & pipeline artifacts</h2>
        <ResearchTable
          caption="Served heads"
          headers={[
            "Universe",
            "Version",
            "Factor",
            "Seq",
            "Freshness",
            "Availability",
            "Age / Limit (h)",
            "Served run",
            "Head run",
            "Served to",
            "Strategy run",
            "Staleness reason",
          ]}
        >
          {servedHeads.map((h, i) => (
            <tr key={`${String(h.universe_id ?? "")}-${i}`} className="border-t border-border font-mono text-xs">
              <td className="px-3 py-2">{safeCell(h.universe_id)}</td>
              <td className="px-3 py-2">{safeCell(h.universe_version)}</td>
              <td className="px-3 py-2">{safeCell(h.factor_id)}</td>
              <td className="px-3 py-2">{safeCell(h.sequence)}</td>
              <td className="px-3 py-2">{safeCell(h.freshness)}</td>
              <td className="px-3 py-2">{safeCell(h.availability)}</td>
              <td className="px-3 py-2">
                {formatCell(formatRatio(h.age_hours as number | string | null | undefined))} /{" "}
                {formatCell(formatRatio(h.limit_hours as number | string | null | undefined))}
              </td>
              <td className="px-3 py-2">{h.run_id ? formatShortSha(String(h.run_id)) : "withheld"}</td>
              <td className="px-3 py-2">{formatShortSha(h.head_run_id as string | null | undefined)}</td>
              <td className="px-3 py-2">{safeCell(h.served_to)}</td>
              <td className="px-3 py-2">{formatShortSha(h.bound_strategy_run_id as string | null | undefined)}</td>
              <td className="px-3 py-2">{safeCell(h.staleness_reason)}</td>
            </tr>
          ))}
        </ResearchTable>
        <HeadRollbackControl heads={servedHeads} captureRuns={captureRuns} />
        <ResearchTable
          caption="Served artifacts"
          headers={["Artifact key", "Lane", "Schedule", "Universe filter", "Family", "Served to", "Wired"]}
        >
          {servedArtifacts.map((a) => (
            <tr key={String(a.artifact_key ?? "")} className="border-t border-border font-mono text-xs">
              <td className="px-3 py-2">{safeCell(a.artifact_key)}</td>
              <td className="px-3 py-2">{safeCell(a.lane)}</td>
              <td className="px-3 py-2">{safeCell(a.schedule_name)}</td>
              <td className="px-3 py-2">{safeCell(a.universe_like)}</td>
              <td className="px-3 py-2">{safeCell(a.family)}</td>
              <td className="px-3 py-2">{safeCell(a.served_to)}</td>
              <td className="px-3 py-2">{a.wired ? "yes" : "no"}</td>
            </tr>
          ))}
        </ResearchTable>
      </div>

      <DataSection
        title="Output invariants & validation"
        headers={["Invariant / Check", "Kind", "Population", "Violations / Verdict", "Deferred", "Sample / Detail", "Checked at"]}
        rows={[
          ...outputInvariants.map((inv) => [
            safeCell(inv.invariant_id),
            safeCell(inv.kind),
            safeCell(inv.population),
            safeCell(inv.violation_count),
            inv.deferred ? "yes" : "no",
            Array.isArray(inv.sample_rows) && inv.sample_rows.length > 0 ? safeCell(inv.sample_rows.slice(0, 2)) : "0 samples",
            safeCell(inv.checked_at),
          ]),
          ...validation.map((v) => [
            safeCell(v.check),
            "validation",
            formatCell(null),
            safeCell(v.verdict),
            "no",
            safeCell(v.detail),
            formatCell(null),
          ]),
        ]}
      />

      <DataSection
        title="Nightly verdicts"
        headers={["Check name", "Verdict", "Age / Max", "Ran at", "Summary", "Dagster run ID"]}
        rows={nightlyVerdicts.map((v) => [
          safeCell(v.check_name),
          v.freshness_state === "stale"
            ? "STALE"
            : v.freshness_state === "missing"
              ? "MISSING"
              : v.ok === true
                ? "pass"
                : v.ok === false
                  ? "fail"
                  : "pending",
          typeof v.age_hours === "number" && typeof v.max_age_hours === "number"
            ? `${v.age_hours.toFixed(1)} / ${v.max_age_hours.toFixed(0)}h`
            : typeof v.max_age_hours === "number"
              ? `— / ${v.max_age_hours.toFixed(0)}h`
              : formatCell(null),
          safeCell(v.ran_at),
          safeCell(v.summary),
          safeCell(v.dagster_run_id),
        ])}
      />

      <DataSection
        title="Confidence reports (mart.datahub_confidence_report)"
        headers={[
          "Universe",
          "Family",
          "Cells",
          "Mean confidence",
          "High / Med / Low",
          "1-origin / 2+-origin",
          "Agreement rate",
          "Cutoff",
        ]}
        rows={confidence.flatMap((c) => {
          const payload = (c.payload ?? {}) as Record<string, unknown>;
          const families = (payload.families ?? {}) as Record<string, Record<string, unknown>>;
          const cellsByFamily = (payload.cells ?? {}) as Record<string, Record<string, Record<string, unknown>>>;
          const accuracy = (payload.accuracy ?? {}) as Record<string, Record<string, unknown>>;
          const familyNames = Object.keys(families).sort();
          if (familyNames.length === 0) {
            return [[safeCell(c.universe_id), "—", "0", formatCell(null), formatCell(null), formatCell(null), formatCell(null), safeCell(c.cutoff)]];
          }
          return familyNames.map((famName) => {
            const fam = families[famName] ?? {};
            const cellEntries = Object.values(cellsByFamily[famName] ?? {});
            let high = 0;
            let med = 0;
            let low = 0;
            for (const cell of cellEntries) {
              const tier = typeof cell.tier === "string" ? cell.tier.toLowerCase() : "";
              const confNum = Number(cell.confidence);
              if (tier === "high" || (!tier && Number.isFinite(confNum) && confNum >= 0.85)) high += 1;
              else if (tier === "medium" || (!tier && Number.isFinite(confNum) && confNum >= 0.5)) med += 1;
              else low += 1;
            }
            const tierSummary = cellEntries.length > 0 ? `${high} / ${med} / ${low}` : "—";
            const originsMap = (fam.by_independent_origins ?? {}) as Record<string, unknown>;
            let oneOrigin = 0;
            let multiOrigin = 0;
            for (const [k, raw] of Object.entries(originsMap)) {
              const count = Number(raw ?? 0);
              if (Number.isFinite(count)) {
                if (Number(k) >= 2) multiOrigin += count;
                else oneOrigin += count;
              }
            }
            const agreement = fam.agreement_rate ?? accuracy[famName]?.agreement_rate;
            return [
              safeCell(c.universe_id),
              famName,
              Number(fam.cells ?? 0),
              safeCell(fam.mean_confidence),
              tierSummary,
              `${oneOrigin} / ${multiOrigin}`,
              agreement === null || agreement === undefined ? formatCell(null) : safeCell(agreement),
              safeCell(c.cutoff),
            ];
          });
        })}
      />

      <DataSection
        title="Capacity, Vendor Quotas & 429/1302"
        headers={["Account", "Env", "Limit ID", "Window (s)", "Share", "Observed", "Remaining", "Utilization (%)", "Rate-limited (24h)"]}
        rows={capacity.map((cap) => [
          safeCell(cap.account),
          safeCell(cap.environment),
          safeCell(cap.limit_id),
          safeCell(cap.window_seconds),
          safeCell(cap.share),
          safeCell(cap.observed_calls),
          safeCell(cap.remaining_estimate),
          cap.utilization_pct !== null && cap.utilization_pct !== undefined ? `${formatCell(formatRatio(cap.utilization_pct as number))}%` : formatCell(null),
          safeCell(cap.rate_limited_24h),
        ])}
      />

      {model1302.length > 0 && (
        <DataSection
          title="Rate limits & 429 / 1302 by day"
          headers={["Day", "Source", "429 count", "1302 / rate-limit count", "Total calls", "Failed calls"]}
          rows={model1302.map((m) => [
            safeCell(m.day),
            safeCell(m.source),
            safeCell(m.status_429),
            safeCell(m.code_1302),
            safeCell(m.total_calls),
            safeCell(m.failed_calls),
          ])}
        />
      )}

      <DataSection
        title="LLM usage & model drift"
        headers={["Day", "Provider", "Served model", "Standard", "Calls", "Prompt tokens", "Completion tokens", "Cost", "Errors", "Refusals"]}
        rows={llmUsage.map((u) => [
          safeCell(u.day),
          safeCell(u.provider),
          safeCell(u.served_model),
          safeCell(u.standard),
          safeCell(u.calls),
          safeCell(u.prompt_tokens),
          safeCell(u.completion_tokens),
          safeCell(u.cost),
          safeCell(u.error_calls),
          safeCell(u.refusal_calls),
        ])}
      />

      <DataSection
        title="Recent API calls (staging.api_call_ledger)"
        headers={["ID", "Called at", "Source", "Endpoint", "Caller", "OK", "Status", "ms", "Run key", "Error"]}
        rows={recentCalls.map((c) => [
          safeCell(c.id),
          safeCell(c.called_at),
          safeCell(c.source),
          safeCell(c.endpoint),
          safeCell(c.caller),
          c.ok ? "yes" : "no",
          safeCell(c.status_code),
          safeCell(c.duration_ms),
          safeCell(c.run_key),
          safeCell(c.error),
        ])}
      />

      <DataSection
        title="Backfill health & queues"
        headers={["Type", "Key / Source", "Detail / Metric", "Value / Status", "Timestamp", "Note / Reason"]}
        rows={[
          ...backfillHealth.map((b) => [
            "health",
            safeCell(b.source),
            safeCell(b.metric),
            safeCell(b.value),
            safeCell(b.logged_at),
            safeCell(b.note),
          ]),
          ...triggerQueue.map((t) => [
            "trigger",
            safeCell(t.job_name),
            `#${safeCell(t.request_id)} force=${safeCell(t.force_fetch)}`,
            t.consumed_at ? `consumed (${safeCell(t.launched_run_key)})` : "pending",
            safeCell(t.requested_at),
            safeCell(t.requested_by),
          ]),
          ...rollbackRequests.map((rb) => [
            "rollback",
            safeCell(rb.universe_id),
            `#${safeCell(rb.request_id)} → ${formatShortSha(rb.target_run_id as string | null | undefined)}`,
            rb.consumed_at ? `consumed (${formatShortSha(rb.result_pointer_id as string | null | undefined)})` : "pending",
            safeCell(rb.requested_at),
            safeCell(rb.reason),
          ]),
        ]}
      />

      <DataSection
        title={`DB hygiene facts${statsReset ? ` (stats reset: ${statsReset})` : ""}`}
        headers={["Kind", "Schema", "Table", "Index", "Live tuples / Scans", "Size (bytes)"]}
        rows={[
          ...zeroScanIndexes.map((idx) => [
            "zero-scan-index",
            safeCell(idx.schemaname),
            safeCell(idx.table_name),
            safeCell(idx.index_name),
            "0",
            safeCell(idx.size_bytes),
          ]),
          ...idleTables.map((tbl) => [
            "idle-table",
            safeCell(tbl.schemaname),
            safeCell(tbl.table_name),
            formatCell(null),
            `${safeCell(tbl.n_live_tup)} live / ${safeCell(tbl.total_scans)} scans`,
            safeCell(tbl.size_bytes),
          ]),
        ]}
      />
    </div>
  );
}
