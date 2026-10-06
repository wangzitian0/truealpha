---
name: audit
description: Step 2 of the five-step flow. Falsify a result against two independent external sources, then run a read-only adversarial review in three categories. Use before you call work done or correct.
---

# audit: try to prove it wrong

A system's own tests, CI, and issue states are claims written by the system. A test cannot catch its author's premise.

## Phase 0: Touch Reality (run before any scout)

1. **Label the delivery state.** Run `git status -s`, then `gh pr list --head <branch> --json number,state,mergedAt`.
   The owner repeatedly found "done" claims for unmerged work.

   | State | Label | Allowed words |
   |---|---|---|
   | PR merged | `(PR #N, merged)` | Final, Done |
   | PR open | `(PR #N, pending merge)` | In review |
   | Committed, no PR | `(committed, no PR)` | Local verified |
   | Uncommitted | `(uncommitted)` | Draft |

2. **Touch two independent oracles** that this repository did not write: the vendor or upstream source,
   and the real runtime (database query, byte check with sha256, scheduler failure log, staging against production).
   Operate the real surface (the real TUI or the browser) when the claim is about user behavior.
3. **Scan the three failure shapes.** Each passes lint, tests, and CI.
   - WRONG FORMULA: one formula breaks a special case (a bank with negative profit per head).
   - GREEN-WHILE-EMPTY: a filter removes all rows and the job reports success.
   - STALE-REPORTED-AS-FRESH: ten-year-old data carries `fresh`.
4. **Implausible output is evidence.** Explain it before you propose anything else.
5. **A score must come from executed checks.** The owner asked how a score could be right if the audit only read documents.
   Run the command, record the exit code, and cite it. A score without a command is not reported.
6. **Disclose.** Name the inputs you did not verify, the sample size, the data age, and the oracles you did not use.
   Ask "Sufficient?" (could an unverified input overturn the conclusion?) and "MECE?" (overlap or orphan?). A troubling answer is a finding.

## Four gates

Each gate came from a measured failure.

1. **The auditor must read code.** Use a read-only native agent. Never use tool-less `subagent_batch` for judgment.
   Six of eight false findings came from agents without code access.
2. **See each new test fail first.** Run it on the unfixed code and confirm RED.
   This caught a fake `assertIn(role, text)` assertion that prose satisfied.
3. **Re-review every fix independently.** The reviewer gets only the new code, not the defect story.
   For a security fix, ask for "the second chain from the same entry".
   On 2026-09-22 removing `eval` moved the value into an existing `python -c` string concatenation,
   and a regex guard missed the `git -C <dir>` prefix. One round found 3 HIGH in the Director's own fix.
4. **Screening output carries anchors.** Each fact cites `file#Lxx-Lyy` or a note id.
   The Director reads 1 to 2 anchors before a decision. Lossy small-model summaries hide facts.

## Scouts (owner design: three categories, 4+3+2 = 9)

Use all nine only for a large or risky change. Lean mode uses one scout per category (M2, G3, T1).

**Category M: contract and impact (reads docs and code).**
- M1 API breaking changes: renamed fields, new required fields, incompatible types.
- M2 design promises: does code do what the README and design say, or stub it with `pass` and TODO?
- M3 blast radius: shared state, events, or middleware that break downstream consumers.
- M4 semantic drift: config names, defaults, environment variables, error codes.

**Category G: general engineering (doc-blind).** Give these scouts source code and tests only. Withhold `*.md` and `docs/`. They must not guess business intent.
- G1 SRE defense: leaks, missing locks, child processes not killed as a group, timeouts, shutdown.
- G2 hygiene: swallowed errors (`except: pass`, ignored `err`), dead code, empty stubs, hidden hardcodes.
- G3 fake tests: GREEN-WHILE-EMPTY, `assert True`, `assert len(x) >= 0`, over-mocking.

**Category T: goal and side effects (reads the issue and the PR text).**
- T1 completeness: did the change finish the stated goal, or only the happy path?
- T2 side effects: latency, rate limits, lock contention, broken global invariants.

The Director cross-checks scouts. A doc claim (M2) that a doc-blind scout (G1) cannot find in code is a false feature.
A completeness claim (T1) against empty-run tests (G3) is false prosperity.

## Convergence

A round with zero HIGH and zero new middle findings converges. The last round is audit-only: it edits nothing.
Swarm mode runs 10 scouts for 3 rounds: find, refute, judge. Use it for architecture changes.

## Scout liveness

Scouts are Interns with read-only tools. The Director never waits blind: read each running scout's tool-call output
about every two minutes. No tool call for about two minutes means stalled. Prove the old process stopped before you start
a replacement. The host rules ("Observation and liveness") hold the full rule.
Accept `exit=0` plus a real diff or commit as completion. Prose is not evidence.
