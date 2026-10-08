---
name: audit
description: Step 2 of the five-step flow. Falsify a result against two independent external sources, then run the 10-Intern adversarial swarm audit. Use before you call work done or correct.
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

1. **The auditor must read code.** Use a read-only native agent, or provide complete source code and diff in the prompt for `subagent_batch` workers. Never ask an auditor to judge code that the auditor cannot see. Six of eight false findings came from agents without code access.
2. **See each new test fail first.** Run only the target test case on unfixed code and confirm RED (`pytest <file>::<test> -x`).
   Never run full suites during Gate 2. This caught a fake `assertIn(role, text)` assertion that prose satisfied.
3. **Re-review every fix independently.** The reviewer gets only the new code, not the defect story.
   For a security fix, ask for "the second chain from the same entry".
   On 2026-09-22 removing `eval` moved the value into an existing `python -c` string concatenation,
   and a regex guard missed the `git -C <dir>` prefix. One round found 3 HIGH in the Director's own fix.
4. **Screening output carries anchors.** Each fact cites `file#Lxx-Lyy` or a note id.
   The Director reads 1 to 2 anchors before a decision. Lossy small-model summaries hide facts.

## The 10-Intern Scout Matrix

The 10 parallel Interns in Round 1 are allocated to specific, non-overlapping audit dimensions across four categories:

**Category M: Contract and impact (reads docs and code).**
- M1 breaking changes (Intern 1): Renamed fields, new required parameters, breaking protobuf/schema contracts.
- M2 design promises (Intern 2): Does code do what the README and architecture specify, or stub it with `pass` and TODO?
- M3 blast radius (Intern 3): Shared state, events, or middleware that break downstream consumers or callers.
- M4 semantic drift (Intern 4): Config names, default values, environment variable names, error codes.

**Category G: General engineering (doc-blind).** Give these scouts source code and tests only. Withhold `*.md` and `docs/`. They must not guess business intent.
- G1 SRE defense (Intern 5): Leaks, missing locks, child processes not killed as a group, timeouts, shutdown signals.
- G2 hygiene (Intern 6): Swallowed errors (`except: pass`, ignored error objects), dead code, empty stubs, hidden hardcodes.
- G3 fake tests (Intern 7): GREEN-WHILE-EMPTY, `assert True`, `assert len(x) >= 0`, over-mocking, shadowed test functions.

**Category T: Goal and side effects (reads issue, PR text, and code).**
- T1 completeness (Intern 8): Did the change finish the stated goal, or only the happy path?
- T2 side effects (Intern 9): Latency, rate limits, lock contention, broken global invariants.

**Category S: Single source of truth & rules (reads SSOT, MANIFEST, and rules).**
- S1 SSOT and drift (Intern 10): Mismatches between implementation, MANIFEST.yaml keys, and rule boundaries.

The Director cross-checks scouts: a doc claim (M2) that a doc-blind scout (G1) cannot find in code is a false feature; a completeness claim (T1) against empty-run tests (G3) is false prosperity.

## Swarm execution workflow

Audit executes via the `swarm` skill state machine:

1. **Round 1 (Propose)**: The Director dispatches the 10-Intern Scout Matrix concurrently via `subagent_batch`. Each Intern outputs structured defect hypotheses with exact `file#Lxx-Lyy` anchors and counterexamples.
2. **Round 2 (Cross-Falsify)**: Interns cross-examine opposing claims. Opponents must actively seek counterexamples in code to disprove hypotheses. Hypotheses are marked strictly as `[DISPROVEN]` or `[CONFIRMED]`.
3. **Round 3 (Director Triangulation)**: The Director reviews surviving `[CONFIRMED]` claims, runs targeted physical reality checks (Touch Reality), and rejects false positives.

A round with zero HIGH and zero new MIDDLE findings converges. Stop after round 5 at most. The last round is audit-only: it edits nothing.
During review rounds, run focused tests only (`pytest <file>::<test> -x`). Never run full test suites during audit rounds.

## Scout liveness

Scouts are Interns dispatched via `subagent_batch`. The Director monitors task progression:
- Child processes have OS-level timeouts (50s default).
- The Director checks task outputs and worker logs (`~/.local/state/subagent-worker/worker.log`).
- Accept exit code zero plus a real diff or commit as completion. Prose is not evidence.
