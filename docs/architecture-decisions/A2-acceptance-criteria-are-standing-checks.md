# A2 — Acceptance criteria are standing checks, and they cover the whole scope

Status: Draft (needs owner sign-off — adds `AGENTS.md` rule 7)
Date: 2026-07-30
Extends: `AGENTS.md` rule 6 ("Closed means deployed, real, and evidenced")

## Context

A 2026-07-30 end-to-end review of `apps/app-web` found nine gaps in a UI system whose four
delivery issues (#493, #494, #495, #496) were all closed with detailed evidence. None of
the gaps is a coding mistake. Every one of them entered through the *closing* of an issue,
by one of two mechanisms:

**Scope wider than acceptance.** #494's body scoped `role switch in the top bar` alongside
the OPERATE chrome, and an `/admin/quality` `L2 tile shows "metric pending #496"`
placeholder. Its three acceptance criteria asserted neither. The closing comment reads
"Everything this issue scoped is deployed and proven" while enumerating only the asserted
items. Today there is no link to `/admin` from anywhere in the application and no placeholder
on the quality page: an administrator must type the URL, and the funnel block disappears
without comment when it cannot be computed. #495 is the same shape one layer down — its
acceptance checked `200 + typed JSON` at the API boundary, so the *rendered* states of the
same data were never anyone's deliverable, and `/admin`'s run table still renders a bare
header with no message when the list is empty.

**Acceptance satisfied by an execution rather than a check.** #494's first acceptance
criterion reads "Playwright suite checked into `apps/app-web`, **run in ci-web** against a
seeded local stack". The suite was written and it passes; the closing evidence is a manual
run against production. `e2e/walk-tree.mjs` is referenced by no workflow and no Makefile
target. The regression guard the criterion existed to install does not exist, and rule 6 —
"deployed + evidence posted" — was fully satisfied by the manual run.

A third pattern compounds both: **`Closes #N` auto-closure**. #368 and #371 each closed at
the exact second their PR merged, with no comment walking their criteria. Those two issues
carried the only user-journey criteria in the batch — #371's "logged-in member sees member
nav; administrator sees admin nav; anonymous is denied", #368's "Verified by driving the
flow, not just typecheck". A merge proves compilation and tests; it cannot prove a
role-dependent view. #371's criterion is still false today, and `research-nav.tsx` carries
a comment asserting the "separate navs" that were never built — the same
comment-instead-of-check root cause the 2026-07-22 seam audit identified, recurring in a
new place.

That audit produced a seven-rule acceptance covenant. It was never written into `AGENTS.md`,
so it applied only to the twelve issues manually upgraded at the time; #493–#496 were
authored five days later without it.

## Decision

`AGENTS.md` rule 7 states two rules, both checkable while reading an issue:

1. **Every scope item has a matching acceptance criterion.** A deliverable named in the body
   must be asserted by exactly one criterion. A deliverable with no criterion is deleted from
   the scope or given a check. Closure is judged scope item by scope item.
2. **A criterion is a check that runs again, not a run that happened.** Acceptance is a named
   CI step, test, or gate that turns red on the next regression. A one-time manual walk, a
   hand-run script, or a measurement taken on a single production run is *evidence* under
   rule 6 and is *not* acceptance under rule 7.

Auto-closing with `Closes #N` asserts both rules were evaluated. An issue carrying a
criterion a merge cannot prove references its issue with a plain `#N` and is closed by hand,
with a comment walking the criteria in order.

## Consequences

- Issues get longer at authoring time. That is the cost: the failure mode being priced out
  is a scope line that no one ever has to answer for.
- Some criteria become impossible to write cheaply, which is information. "Administrator sees
  an admin nav" is hard to assert without a two-principal browser walk — and the absence of
  that walk is exactly why the gap survived nine days across two issues.
- Rule 6 is unchanged and still governs *closing*: deployed, real, evidenced. Rule 7 governs
  what the acceptance list is allowed to contain. An issue can satisfy 6 and fail 7 — #494
  did.
- Not retroactive by itself. #371, #494 and #495 are reopened with criteria rewritten to this
  form; earlier closed issues stay closed unless a concrete gap surfaces.

## Alternatives considered

- **A reviewer checklist instead of a repository rule.** Rejected: the 2026-07-22 covenant
  was exactly that, lived outside the repository, and stopped being applied within five days.
- **Forbidding `Closes #N` entirely.** Rejected as too blunt — most issues are genuinely
  provable by a merge, and losing auto-closure on those costs bookkeeping for no safety.
