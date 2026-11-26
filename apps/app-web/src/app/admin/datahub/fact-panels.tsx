import Link from "next/link";
import { formatCell, formatRatio, formatShortSha } from "@/client/format";
import { HeadRollbackControl } from "@/components/head-rollback-control";
import { ResearchTable } from "@/components/research-table";

export function safeCell(value: unknown): string {
  if (value === null || value === undefined || value === "") return formatCell(null);
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return formatCell(value);
  return formatCell(JSON.stringify(value));
}

export function badgeTone(tone: "emerald" | "red" | "amber" | "neutral"): string {
  if (tone === "emerald") return "inline-block rounded-full border border-emerald-500/40 bg-emerald-500/10 px-2 py-0.5 text-emerald-300";
  if (tone === "red") return "inline-block rounded-full border border-red-500/40 bg-red-500/10 px-2 py-0.5 text-red-300";
  if (tone === "amber") return "inline-block rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-amber-300";
  return "inline-block rounded-full border border-border bg-card px-2 py-0.5 text-gray-300";
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

function deltaSummary(qObj: Record<string, unknown>): { text: string; delta: number } {
  const change = qObj.change as Record<string, unknown> | undefined;
  if (!change || typeof change !== "object") return { text: formatCell(null), delta: 0 };
  const answeredDelta = Number(change.answered ?? 0);
  const sign = answeredDelta > 0 ? `+${answeredDelta}` : String(answeredDelta);
  if (Array.isArray(change.subjects_changed)) {
    const subjects = change.subjects_changed as unknown[];
    const sample = subjects.slice(0, 3).map((s) => String(s)).join(", ");
    return { text: subjects.length > 0 ? `${sign} (${subjects.length}: ${sample})` : sign, delta: answeredDelta };
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
    return { text: sample ? `${sign} (${changedCount}: ${sample})` : `${sign} (${changedCount})`, delta: answeredDelta };
  }
  return { text: sign, delta: answeredDelta };
}

export function QuestionCoverageDetails({ questionCoverage }: { questionCoverage: Record<string, unknown>[] }) {
  const matrixRows: { cells: (string | number)[]; delta: number; hasGap: boolean }[] = [];
  for (const row of questionCoverage) {
    const universe = safeCell(row.universe_id);
    const cutoff = safeCell(row.cutoff);
    const payload = row.payload as Record<string, unknown> | undefined;
    const prevRun = payload?.previous_run_id ? formatShortSha(String(payload.previous_run_id)) : formatCell(null);
    const questions = (payload?.questions ?? {}) as Record<string, Record<string, unknown>>;
    const qKeys = Object.keys(questions).sort();
    if (qKeys.length === 0) {
      matrixRows.push({
        cells: [universe, cutoff, prevRun, formatCell(null), formatCell(null), formatCell(null), formatCell(null), formatCell(null)],
        delta: 0,
        hasGap: false,
      });
      continue;
    }
    for (const qk of qKeys) {
      const qVal = questions[qk] ?? {};
      const answered = Number(qVal.answered ?? 0);
      const denom = Number(qVal.denominator ?? 0);
      const na = Number(qVal.not_applicable ?? 0);
      const pct = denom > 0 ? formatRatio((answered * 100) / denom) : formatCell(null);
      const d = deltaSummary(qVal);
      const gapCount = Number((qVal.by_class as Record<string, unknown> | undefined)?.pipeline_gap ?? 0);
      matrixRows.push({
        cells: [
          universe,
          qk,
          `${answered}/${denom} (${pct}% · na=${na})`,
          countMapSummary(qVal.by_class, QUESTION_CLASSES),
          countMapSummary(qVal.violations, QUESTION_VIOLATIONS),
          `${prevRun} · ${d.text}`,
          cutoff,
          safeCell(row.created_at),
        ],
        delta: d.delta,
        hasGap: gapCount > 0,
      });
    }
  }
  return (
    <div className="space-y-2">
      <h2 className="text-lg font-semibold">Question coverage detail (layers &amp; deltas)</h2>
      <ResearchTable
        caption="Question coverage detail (layers & deltas)"
        headers={["Universe", "Question", "Answered / Denom", "By class", "Violations", "Delta vs prior", "Cutoff", "Created"]}
      >
        {matrixRows.map((row, i) => (
          <tr key={`${i}-${row.cells[0]}-${row.cells[1]}`} className="border-t border-border font-mono text-xs">
            <td className="px-3 py-2">{String(row.cells[0])}</td>
            <td className="px-3 py-2">{String(row.cells[1])}</td>
            <td className="px-3 py-2">{String(row.cells[2])}</td>
            <td className="px-3 py-2">
              <span className={row.hasGap ? "text-amber-300" : "text-gray-300"}>{String(row.cells[3])}</span>
            </td>
            <td className="px-3 py-2">{String(row.cells[4])}</td>
            <td className="px-3 py-2">
              <span className={badgeTone(row.delta > 0 ? "emerald" : row.delta < 0 ? "red" : "neutral")}>{String(row.cells[5])}</span>
            </td>
            <td className="px-3 py-2">{String(row.cells[6])}</td>
            <td className="px-3 py-2">{String(row.cells[7])}</td>
          </tr>
        ))}
      </ResearchTable>
    </div>
  );
}

function resolveTierSummary(
  famName: string,
  tierCountsByFamily: Record<string, Record<string, unknown>>,
  cellsByFamily: Record<string, Record<string, Record<string, unknown>>>,
): string {
  const precomputed = tierCountsByFamily[famName];
  if (precomputed && typeof precomputed === "object") {
    const high = Number(precomputed.high ?? 0);
    const med = Number(precomputed.medium ?? 0);
    const low = Number(precomputed.low ?? 0);
    if (high + med + low > 0) return `${high} / ${med} / ${low}`;
  }
  const cellEntries = Object.values(cellsByFamily[famName] ?? {});
  if (cellEntries.length === 0) return "—";
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
  return `${high} / ${med} / ${low}`;
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

  const hasHeadIssue = servedHeads.some((h) => h.freshness !== "fresh" || h.availability !== "available" || !h.run_id);
  const hasBadVerdict = nightlyVerdicts.some((v) => v.ok === false || v.freshness_state === "stale" || v.freshness_state === "missing");
  const hasPendingQueue = triggerQueue.some((t) => !t.consumed_at) || rollbackRequests.some((rb) => !rb.consumed_at);

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

      <div className="space-y-2">
        <h2 className="text-lg font-semibold">Output invariants &amp; validation</h2>
        <ResearchTable
          caption="Output invariants & validation"
          headers={["Invariant / Check", "Kind", "Population", "Violations / Verdict", "Sample / Detail", "Checked at"]}
        >
          {outputInvariants.map((inv) => {
            const vCount = Number(inv.violation_count ?? 0);
            return (
              <tr key={`inv-${String(inv.invariant_id ?? "")}`} className="border-t border-border font-mono text-xs">
                <td className="px-3 py-2">{safeCell(inv.invariant_id)}</td>
                <td className="px-3 py-2"><span className={badgeTone(inv.kind === "hard" ? "amber" : "neutral")}>{safeCell(inv.kind)}</span></td>
                <td className="px-3 py-2">{safeCell(inv.population)}</td>
                <td className="px-3 py-2"><span className={badgeTone(vCount > 0 ? "red" : "emerald")}>{safeCell(inv.violation_count)}</span></td>
                <td className="px-3 py-2">
                  {Array.isArray(inv.sample_rows) && inv.sample_rows.length > 0 ? safeCell(inv.sample_rows.slice(0, 2)) : "0 samples"}
                </td>
                <td className="px-3 py-2">{safeCell(inv.checked_at)}</td>
              </tr>
            );
          })}
          {validation.map((v, i) => (
            <tr key={`val-${i}-${String(v.check ?? "")}`} className="border-t border-border font-mono text-xs">
              <td className="px-3 py-2">{safeCell(v.check)}</td>
              <td className="px-3 py-2"><span className={badgeTone("neutral")}>validation</span></td>
              <td className="px-3 py-2">{formatCell(null)}</td>
              <td className="px-3 py-2"><span className={badgeTone(v.verdict === "violations" ? "red" : "emerald")}>{safeCell(v.verdict)}</span></td>
              <td className="px-3 py-2">{safeCell(v.detail)}</td>
              <td className="px-3 py-2">{formatCell(null)}</td>
            </tr>
          ))}
        </ResearchTable>
      </div>

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
          const tierCountsByFamily = (payload.tier_counts ?? {}) as Record<string, Record<string, unknown>>;
          const cellsByFamily = (payload.cells ?? {}) as Record<string, Record<string, Record<string, unknown>>>;
          const accuracy = (payload.accuracy ?? {}) as Record<string, Record<string, unknown>>;
          const familyNames = Object.keys(families).sort();
          if (familyNames.length === 0) {
            return [[safeCell(c.universe_id), "—", "0", formatCell(null), formatCell(null), formatCell(null), formatCell(null), safeCell(c.cutoff)]];
          }
          return familyNames.map((famName) => {
            const fam = families[famName] ?? {};
            const tierSummary = resolveTierSummary(famName, tierCountsByFamily, cellsByFamily);
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

      <div className="space-y-2">
        <h2 className="text-lg font-semibold">Capacity, Vendor Quotas &amp; 429/1302</h2>
        <ResearchTable
          caption="Capacity, Vendor Quotas & 429/1302"
          headers={["Account", "Env", "Limit ID", "Window (s)", "Share", "Observed", "Remaining", "Utilization (%)", "Rate-limited (24h)"]}
        >
          {capacity.map((cap, i) => {
            const pct = Number(cap.utilization_pct);
            const hasPct = cap.utilization_pct !== null && cap.utilization_pct !== undefined && Number.isFinite(pct);
            const tone = !hasPct ? "neutral" : pct >= 100 ? "red" : pct >= 70 ? "amber" : "emerald";
            return (
              <tr key={`${String(cap.account ?? "")}-${String(cap.limit_id ?? "")}-${i}`} className="border-t border-border font-mono text-xs">
                <td className="px-3 py-2">{safeCell(cap.account)}</td>
                <td className="px-3 py-2">{safeCell(cap.environment)}</td>
                <td className="px-3 py-2">{safeCell(cap.limit_id)}</td>
                <td className="px-3 py-2">{safeCell(cap.window_seconds)}</td>
                <td className="px-3 py-2">{safeCell(cap.share)}</td>
                <td className="px-3 py-2">{safeCell(cap.observed_calls)}</td>
                <td className="px-3 py-2">{safeCell(cap.remaining_estimate)}</td>
                <td className="px-3 py-2">
                  {hasPct ? <span className={badgeTone(tone)}>{`${formatCell(formatRatio(pct))}%`}</span> : formatCell(null)}
                </td>
                <td className="px-3 py-2">{safeCell(cap.rate_limited_24h)}</td>
              </tr>
            );
          })}
        </ResearchTable>
      </div>

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

      <details open={hasHeadIssue} className="rounded-xl border border-border bg-card/40 p-4">
        <summary className="cursor-pointer select-none text-sm font-semibold">
          Served heads &amp; pipeline artifacts (heads: {servedHeads.length}, artifacts: {servedArtifacts.length}
          {hasHeadIssue ? " · attention needed" : ""})
        </summary>
        <div className="mt-4 space-y-3">
          <h2 className="text-lg font-semibold">Served heads &amp; pipeline artifacts</h2>
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
      </details>

      <details open={hasBadVerdict} className="rounded-xl border border-border bg-card/40 p-4">
        <summary className="cursor-pointer select-none text-sm font-semibold">
          Nightly verdicts ({nightlyVerdicts.length} checks{hasBadVerdict ? " · has red/stale" : " · all green"}) ·{" "}
          <Link href="/admin" className="text-accent hover:underline">
            View on Operations →
          </Link>
        </summary>
        <div className="mt-4">
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
        </div>
      </details>

      <details className="rounded-xl border border-border bg-card/40 p-4">
        <summary className="cursor-pointer select-none text-sm font-semibold">
          Recent API calls (staging.api_call_ledger: {recentCalls.length} rows)
        </summary>
        <div className="mt-4">
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
        </div>
      </details>

      <details open={hasPendingQueue} className="rounded-xl border border-border bg-card/40 p-4">
        <summary className="cursor-pointer select-none text-sm font-semibold">
          Backfill health &amp; queues (health: {backfillHealth.length}, triggers: {triggerQueue.length}, rollbacks: {rollbackRequests.length})
        </summary>
        <div className="mt-4">
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
        </div>
      </details>

      <details className="rounded-xl border border-border bg-card/40 p-4">
        <summary className="cursor-pointer select-none text-sm font-semibold">
          DB hygiene facts (zero-scan indexes: {zeroScanIndexes.length}, idle tables: {idleTables.length})
        </summary>
        <div className="mt-4">
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
      </details>
    </div>
  );
}
