/**
 * #1466: the /admin/pipeline view renders every state with words, and e2e/walk-tree.mjs asserts what the
 * view prints. The walk cannot import app code (it drives a browser at import time), so it keeps its own
 * copy of the markers and sentences. This test renders the view and fails when a copy differs.
 *
 * A copy that drifted would let the walk pass on a page that lists no job and says nothing.
 *
 * Run standalone: `bun run tests/admin-pipeline-page.test.ts`.
 */

import { readFileSync } from "node:fs";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { PipelineHealth } from "../src/app/admin/pipeline/pipeline-health";
import { RUNS_UNAVAILABLE_MESSAGE } from "../src/server/admin/ops";
import type { PipelineOutcome } from "../src/server/admin/pipeline";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const NOW = Date.parse("2026-10-21T10:00:00Z");
const render = (outcome: PipelineOutcome): string =>
  renderToStaticMarkup(createElement(PipelineHealth, { outcome, now: NOW }));
const text = (html: string): string => html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ");

const ready: PipelineOutcome = {
  kind: "ready",
  data: {
    windowDays: 7,
    eventLogUnreadable: null,
    appGitSha: "fedcba9876543210fedcba9876543210fedcba98",
    jobRunHealth: { ok: false, ranAt: "2026-10-21T06:45:00.000Z", summary: "head_reports_pipeline (3 of 13 runs failed)" },
    runtimeShape: { ok: null, ranAt: "2026-10-21T06:30:00.000Z", summary: "pending" },
    release: { gitSha: "0123456789abcdef0123456789abcdef01234567", since: "2026-10-19T08:00:00.000Z", ageHours: 50 },
    jobs: [
      {
        job: "head_reports_pipeline",
        runs: 13,
        successes: 10,
        failures: 3,
        successRate: 10 / 13,
        lastSuccessAt: "2026-10-21T07:00:00.000Z",
        lastFailure: {
          runId: "run-1",
          at: "2026-10-21T08:04:18.000Z",
          firstError: "psycopg.errors.RaiseException: entity 7989d366 was minted without its birth alias",
        },
      },
      {
        job: "market_data_refresh_pipeline",
        runs: 5,
        successes: 5,
        failures: 0,
        successRate: 1,
        lastSuccessAt: "2026-10-20T21:15:00.000Z",
        lastFailure: null,
      },
      {
        job: "standard_history_backfill_pipeline",
        runs: 0,
        successes: 0,
        failures: 0,
        successRate: null,
        lastSuccessAt: null,
        lastFailure: null,
      },
    ],
  },
};

// --- ready: one row per job, the counts, the error line, both verdicts and the release age ---
{
  const html = render(ready);
  const words = text(html);
  assert((html.match(/<tr[^>]*data-job="/g) ?? []).length === 3, "one row per job");
  assert(words.includes("77%") && words.includes("10 ok · 3 failed"), "the success rate and the counts are printed");
  assert(
    words.includes("psycopg.errors.RaiseException: entity 7989d366 was minted without its birth alias"),
    "the first error line of the last failure is printed",
  );
  assert(words.includes("no run"), "a job with no run in the window reads no run, never 100%");
  assert(words.includes("never"), "a job without a failure reads never");
  assert(html.includes('data-verdict="job_run_health"') && html.includes('data-verdict="runtime_shape"'), "both verdict cards");
  assert(/data-verdict="job_run_health">red</.test(html), "a red verdict reads red in its card");
  assert(/data-verdict="runtime_shape">pending</.test(html), "a pending verdict reads pending in its card");
  assert(html.includes('data-release-age="true"') && words.includes("2.1d"), "the release age is printed");
  assert(!/undefined|NaN|\[object Object\]/.test(words), "the view leaks no undefined, NaN or [object Object]");
}

// --- the event log is unreadable: the counts stand and the page says why the lines are missing ---
{
  const unreadable: PipelineOutcome = { kind: "ready", data: { ...ready.data, eventLogUnreadable: "permission denied for table event_logs" } };
  const html = render(unreadable);
  assert(html.includes('data-event-log-unreadable="true"'), "the notice is marked");
  assert(text(html).includes("The Dagster event log could not be read") && text(html).includes("permission denied"), "the notice names the cause");
  assert(!text(html).includes("no error text in the event log"), "a missing line is not blamed on the event log");
  assert((html.match(/<tr[^>]*data-job="/g) ?? []).length === 3, "the rows stay");
}

// --- the other states say something ---
const unavailable = text(render({ kind: "unavailable", reason: RUNS_UNAVAILABLE_MESSAGE }));
const empty = text(render({ kind: "empty" }));
const failed = text(render({ kind: "error", message: "permission denied for table runs" }));
const denied = text(render({ kind: "denied" }));
assert(unavailable.includes("Run history unavailable"), "an unbootstrapped Dagster says so");
assert(empty.includes("No finished run"), "an empty history says so");
assert(failed.includes("Error loading the pipeline health: permission denied for table runs"), "an error names its cause");
assert(denied.includes("Access denied"), "a denied read says so");

// --- the walk asserts what the view prints ---
const walk = readFileSync(new URL("../e2e/walk-tree.mjs", import.meta.url), "utf8");
const sentences = walk.match(/!\/(Run history unavailable[^/]*)\/\.test\(mainText\)/);
assert(sentences !== null, "e2e/walk-tree.mjs no longer asserts the sentences of /admin/pipeline as a regular expression");
const sentencePattern = new RegExp(sentences[1]);
for (const [state, words] of [["unavailable", unavailable], ["empty", empty]] as const) {
  assert(sentencePattern.test(words), `the walk would reject the ${state} state of /admin/pipeline: "${words}"`);
}
assert(!sentencePattern.test(failed), "the walk must reject the error state of /admin/pipeline");
const markers = walk.match(/for \(const marker of \[(.*)\]\) \{/);
assert(markers !== null, "e2e/walk-tree.mjs no longer lists the markers of /admin/pipeline as an array literal");
const html = render(ready);
for (const marker of Array.from(markers[1].matchAll(/'([^']+)'|"([^"]+)"/g), (match) => match[1] ?? match[2])) {
  const attribute = marker.match(/^\[(data-[a-z-]+)(?:="([^"]*)")?\]$/);
  assert(attribute !== null, `the walk marker ${marker} is not an attribute selector`);
  const wanted = attribute[2] === undefined ? attribute[1] : `${attribute[1]}="${attribute[2]}"`;
  assert(html.includes(wanted), `the walk expects ${marker}, but the view does not print ${wanted}`);
}
assert(walk.includes('tr[data-job]'), "the walk counts the job rows by tr[data-job]");
assert(html.includes("data-job="), "the view marks each job row with data-job");

console.log("#1466 pipeline view verified");
