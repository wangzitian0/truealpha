/**
 * #1466: the /admin/pipeline view renders every state with words, and e2e/walk-tree.mjs asserts
 * its markers and fallback sentences.
 */

import { readFileSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import type { PipelineOverviewResponse } from "../src/client/api";
import { RUNS_UNAVAILABLE_MESSAGE } from "../src/client/read-state";
import { PipelineHealth } from "../src/app/admin/pipeline/pipeline-health";
import type { ReadState } from "../src/components/read-state";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const render = (outcome: ReadState<PipelineOverviewResponse>): string =>
  renderToStaticMarkup(createElement(PipelineHealth, { outcome }));
const text = (html: string): string => html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ");

const ready: ReadState<PipelineOverviewResponse> = {
  kind: "ready",
  data: {
    available: true,
    window_days: 7,
    app_git_sha: "fedcba9876543210fedcba9876543210fedcba98",
    job_run_health: { ok: false, ran_at: "2026-10-21T06:20:00.000Z", summary: "head_reports_pipeline (3 of 13 runs failed)" },
    release: { git_sha: "0123456789abcdef0123456789abcdef01234567", since: "2026-10-19T08:00:00.000Z", age_hours: 50 },
    jobs: [
      {
        job: "head_reports_pipeline",
        runs: 13,
        successes: 10,
        failures: 3,
        success_rate: 10 / 13,
        last_success_at: "2026-10-21T07:00:00.000Z",
        last_failure: {
          run_id: "run-1",
          at: "2026-10-21T08:04:18.000Z",
          first_error: "psycopg.errors.RaiseException: entity 7989d366 was minted without its birth alias",
        },
        longest_success_seconds: 1350,
        derived_bound_seconds: 2700,
        runs_ended_by_bound: 1,
      },
      {
        job: "market_data_refresh_pipeline",
        runs: 5,
        successes: 5,
        failures: 0,
        success_rate: 1,
        last_success_at: "2026-10-20T21:15:00.000Z",
        last_failure: null,
        longest_success_seconds: 900,
        derived_bound_seconds: 1800,
        runs_ended_by_bound: 0,
      },
      {
        job: "standard_history_backfill_pipeline",
        runs: 0,
        successes: 0,
        failures: 0,
        success_rate: null,
        last_success_at: null,
        last_failure: null,
        longest_success_seconds: null,
        derived_bound_seconds: null,
        runs_ended_by_bound: 0,
      },
    ],
  },
};

{
  const html = render(ready);
  const words = text(html);
  assert((html.match(/<tr[^>]*data-job="/g) ?? []).length === 3, "one row per job");
  assert(words.includes("77%") && words.includes("10 ok · 3 failed"), "success rate and counts printed");
  assert(words.includes("psycopg.errors.RaiseException: entity 7989d366 was minted without its birth alias"), "first error printed");
  assert(words.includes("no run"), "job with no run reads no run");
  assert(words.includes("never"), "job without failure reads never");
  assert(/data-verdict="job_run_health">red</.test(html), "red verdict reads red");
  assert(html.includes('data-release-age="true"') && words.includes("2.1d"), "release age printed");
  assert(!/undefined|NaN|\[object Object\]/.test(words), "view leaks no undefined/NaN/[object Object]");
}

const unavailable = text(render({ kind: "unavailable", reason: RUNS_UNAVAILABLE_MESSAGE }));
const empty = text(render({ kind: "empty" }));
const failed = text(render({ kind: "error", message: "permission denied for table runs" }));
const denied = text(render({ kind: "denied" }));
assert(unavailable.includes("Run history unavailable"), "unavailable says so");
assert(empty.includes("No finished run"), "empty history says so");
assert(failed.includes("Error loading data: permission denied for table runs"), "error names cause");
assert(denied.includes("Access denied"), "denied read says so");

const walk = readFileSync(new URL("../e2e/walk-tree.mjs", import.meta.url), "utf8");
const sentences = walk.match(/!\/(Run history unavailable[^/]*)\/\.test\(mainText\)/);
assert(sentences !== null, "walk-tree.mjs asserts /admin/pipeline unavailable/empty sentences");
const sentencePattern = new RegExp(sentences[1]);
for (const [state, words] of [["unavailable", unavailable], ["empty", empty]] as const) {
  assert(sentencePattern.test(words), `walk rejects ${state}: "${words}"`);
}
assert(!sentencePattern.test(failed), "walk rejects error state");
const markers = walk.match(/for \(const marker of \[(.*)\]\) \{/);
assert(markers !== null, "walk-tree.mjs lists /admin/pipeline markers");
const readyHtml = render(ready);
for (const marker of Array.from(markers[1].matchAll(/'([^']+)'|"([^"]+)"/g), (m) => m[1] ?? m[2])) {
  const attribute = marker.match(/^\[(data-[a-z-]+)(?:="([^"]*)")?\]$/);
  assert(attribute !== null, `unexpected walk marker ${marker}`);
  const wanted = attribute[2] === undefined ? attribute[1] : `${attribute[1]}="${attribute[2]}"`;
  assert(readyHtml.includes(wanted), `view missing walk marker ${wanted}`);
}
assert(walk.includes("tr[data-job]") && readyHtml.includes("data-job="), "walk and view align on data-job");
console.log("#1466 pipeline view verified");
