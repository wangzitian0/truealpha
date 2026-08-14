/**
 * #495: every read state has words, and absence is distinguishable from
 * breakage.
 *
 * The research surfaces had one union and one renderer since #370; the
 * administrator surfaces grew their own and rendered it with hand-written
 * branches, so `runs === []` drew a table header with nothing under it and the
 * `/admin/quality` funnel block vanished when it could not be computed. Both
 * were states nobody had written a branch for. This asserts the property
 * directly instead: for EVERY notice kind in the shared union, and for both of
 * the run table's absent states, there is a non-empty sentence — so a kind
 * added without words fails here rather than rendering an empty region.
 *
 * Run standalone: `bun run tests/read-state.test.ts`.
 */

import { NOTICE_KINDS, readStateMessage, type ReadState } from "../src/server/read-state";
import { RUNS_EMPTY_MESSAGE, RUNS_UNAVAILABLE_MESSAGE } from "../src/server/admin/ops";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

/** One representative value per notice kind, so the table below is exhaustive
 * by construction: adding a kind to `NOTICE_KINDS` without adding it here
 * fails the completeness check immediately after. */
const SAMPLES: Record<string, ReadState<unknown>> = {
  loading: { kind: "loading" },
  empty: { kind: "empty" },
  unavailable: { kind: "unavailable", reason: "no_runs_recorded" },
  error: { kind: "error", message: "connection refused" },
  denied: { kind: "denied" },
};

// --- every notice kind is sampled, and every sample says something ---
for (const kind of NOTICE_KINDS) {
  const sample = SAMPLES[kind];
  assert(sample !== undefined, `no sample for notice kind "${kind}" — add one to SAMPLES`);
  const message = readStateMessage(sample);
  assert(
    typeof message === "string" && message.trim().length > 0,
    `read state "${kind}" renders no words; every absent state must say what is absent`,
  );
}

// --- ready and stale render the caller's own content, not a notice ---
{
  assert(readStateMessage({ kind: "ready", data: 1 }) === null, "ready must not render a notice");
  assert(
    readStateMessage({ kind: "stale", data: 1, asOf: "2026-01-01" }) === null,
    "stale renders data plus its own badge, not a notice",
  );
}

// --- absence is distinguishable from breakage, in words ---
{
  const empty = readStateMessage({ kind: "empty" });
  const unavailable = readStateMessage({ kind: "unavailable", reason: "no_runs_recorded" });
  const error = readStateMessage({ kind: "error", message: "connection refused" });
  assert(empty !== unavailable, '"empty" and "unavailable" must not read identically');
  assert(empty !== error, '"empty" and "error" must not read identically');
}

// --- a surface may re-word one state without forking the vocabulary ---
{
  const overridden = readStateMessage(
    { kind: "denied" },
    { denied: "Access denied. This area requires a verified administrator identity." },
  );
  assert(
    overridden === "Access denied. This area requires a verified administrator identity.",
    "overrides must replace the default sentence for that kind",
  );
  assert(
    readStateMessage({ kind: "empty" }, { denied: "x" }) === readStateMessage({ kind: "empty" }),
    "an override for one kind must not affect another",
  );
}

// --- the run table's two absent states are different operational facts ---
{
  assert(RUNS_UNAVAILABLE_MESSAGE.trim().length > 0, "the unavailable run table must say so");
  assert(RUNS_EMPTY_MESSAGE.trim().length > 0, "an empty run table must say so");
  assert(
    RUNS_UNAVAILABLE_MESSAGE !== RUNS_EMPTY_MESSAGE,
    '"Dagster has no tables here" and "Dagster has tables but no runs" must not read identically — ' +
      "that ambiguity is the defect this issue reopened for",
  );
}

console.log(`read state: ${NOTICE_KINDS.length} notice kinds have words; empty, unavailable and error all differ`);
