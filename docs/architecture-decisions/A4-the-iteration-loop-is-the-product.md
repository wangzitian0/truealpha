# A4 — The iteration loop is the product

Status: proposed 2026-08-28. Every number below is measured from the
2026-08-17..20 sprint (70 merged PRs, 11 release tags, 23 deploy-release runs),
not estimated.

## The finding that forces this document

Of the 23 deploy-release runs in that sprint, **9 failed — and all nine were
defects of the pipeline itself, zero were application defects**:

    5  receiver-run correlation ambiguity   (watermark instead of request_id; infra2-sdk#16)
    2  health gate died at import           (workflow never installed the workspace; #616)
    2  other pipeline states                (walk credential 401; migration replay abort)

In the same sprint, **one HTTP redirect took four production releases** to fix
(v0.0.26, 28, 30, 32) because every one of its defect classes was observable
only after a deploy:

    v0.0.26  flags added to the Dockerfile CMD    infra2's compose overrides the CMD — never ran
    v0.0.28  FastAPI root_path                    Location fixed; routing broken; endpoint 404
    v0.0.30  path-only Location, GET handler      TLS and prefix right; slashless POST 405
    v0.0.32  all methods                          four properties hold at once

Both migration crash-loop incidents (#615's `contract_kind`, then
`pipeline_trigger_requests.job_name` three weeks later) have the same shape:
correct in sequence, fatal on the replay every container boot performs — and CI
replayed against an **empty** database, so the class was invisible until a
staging or production boot.

The pattern: **the expensive defects lived in the gap between what CI validates
and what the runtime executes.** CI checked a CMD the runtime overrides, a
database with no rows, an app with no proxy in front, and a read model no page
rendered. Each such defect's iteration loop was not "run the tests" but "cut a
release, deploy, probe" — 30–60 minutes when it worked, days when it didn't.

## Where one iteration's time went (measured)

| stage | wall time | notes |
|---|---|---|
| PR ci-required | ~6 min | pole: `python / check` 259 s (ruff+mypy+all pytest serial); liveness 159 s; web 156 s |
| Copilot review round | +1 push, +~6 min | nearly every PR had ≥1 actionable finding |
| tag ci-required | ~6 min | **identical SHA already green on main** — pure duplication, ×11 tags ≈ 66 min that sprint |
| staging deploy | ~7 min | infra2 dispatch ~350 s (the real deploy) + walk ~116 s |
| prod deploy | ~7 min | same again, gated on staging evidence |

A clean single-PR change: **~35–45 min merge→prod.** With one review round:
**~1 h.** In a class CI cannot see: **days.**

Agent-loop waste, same sprint, honestly counted: ~6 scripted edits that silently
matched nothing after a formatter pass (each discovered 10–40 min later), 2
red-case sessions run on a branch that lacked the source change under test, and
2 tags cut before the PR they described had actually merged.

## Decision: three latency budgets, each tier catches its own class

| tier | budget | must catch |
|---|---|---|
| local inner loop | ≤ 2 min | units + **runtime-shape**: proxy topology, effective entrypoint, migration replay over seeded rows, page-renders-field |
| PR CI | ≤ 3 min wall | everything local catches, plus cross-package and the browser walk |
| merge → prod | ≤ 20 min | only environment-exclusive truths: real data, infra2 dispatch, deployed walk |
| standing probes | ≤ 24 h | drift between repo assumptions and runtime truth |

A defect class sitting in a slower tier than its budget row is a bug in the
pipeline, tracked like any other.

## Changes, ordered by leverage

**D1 — Runtime-parity harness.** One compose file in this repo that runs the
app containers the way infra2 actually runs them: the same entrypoint contract
(imported from infra2 as a versioned file, not re-guessed), a minimal Traefik in
front that strips `/api` and terminates TLS, and Postgres migrated by the real
chain then seeded with production-shaped rows. `make parity`, budget ≤ 90 s.
This single harness would have caught, locally, every one of the four redirect
releases and both migration crash-loops — the two most expensive defect families
of the sprint. Cross-repo ask: infra2 publishes its entrypoint/compose contract
for these services as an artifact this repo can pin and diff.

**D2 — Thin tag verification.** The tag run re-verifies a SHA main already
proved. Replace with: assert an identical-SHA green main run exists, publish
images, done. Saves ~6 min and one serialization stage per release. This changes
what `source_run_id` means in the deploy evidence, which infra2's verifier also
reads — contract change, needs explicit sign-off on both sides.

**D3 — Split the CI pole.** `python / check` runs lint, types, and every
package's tests serially (259 s). Split into a fast lint+types job (~40 s) and
per-package test jobs in parallel (pole becomes data-engine ~150 s). PR wall
drops from ~6 to ~3 min. Pure mechanics, no contract touched.

**D4 — Promote detection.** mutation-reproof runs weekly; it costs ~1–2 min, so
run it path-filtered on PRs that touch a guard or a guarded file — detection
latency for a dead guard drops from 7 days to minutes. Output invariants run
against production daily; also run them against each environment **in the deploy
run itself, right after the walk** (+~10 s) so a data-shape regression surfaces
at deploy time, not next morning.

**D5 — Script the release.** `tools/cut_release.sh`: assert the named PRs are
MERGED and their review threads resolved, assert main is green at HEAD, tag,
push, dispatch staging, wait, probe, dispatch prod. This mechanizes exactly the
two premature-tag mistakes made by hand that sprint, and turns ~15 min of
hand-stepping into one command. Default cadence becomes batched — a tag per
day or on demand, not a tag per PR; 11 tags in 4 days each paid the full
pipeline. Break-glass stays: an incident label releases a single PR immediately.

**D6 — Agent-loop mechanics** (no approval needed, self-imposed): every scripted
edit is followed by a non-empty-diff assertion; a red case first asserts the
symbol under test exists on the current branch; `tools/runtime_truth.py` prints
each deployed container's effective entrypoint/command/image so "what actually
runs" is one command instead of SSH archaeology. All three failure modes cost
real time that sprint.

## What this deliberately does not touch

- The ~350 s infra2 dispatch is the deploy itself, not overhead.
- The two-environment promotion and the evidence chain (AGENTS.md rule 6, A1)
  stay exactly as they are; D2 changes where evidence is *produced*, never what
  is evidenced.

## Sequencing

1. D3, D4, D5, D6 — no contract changes; immediate.
2. D1 — harness in-repo behind `make parity`; the infra2 entrypoint-contract
   artifact is the one external dependency.
3. D2 — last; contract change, both-sides sign-off.
