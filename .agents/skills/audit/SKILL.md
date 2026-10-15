---
name: audit
description: Step 2 of the five-step flow. Executes a deterministic 3-Round Swarm pipeline (each round 10 Interns) to falsify claims and eradicate defects.
---

# audit: try to prove it wrong

A system's own tests, CI, and issue states are claims written by the system. A test cannot catch its author's premise.
Audit executes a **deterministic 3-Round Swarm pipeline** via `subagent_batch`. Each round dispatches 1 Swarm of 10 Interns (`M=10`).
The Director MUST NOT stop or return a final report before completing all 3 Swarms.

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

## The 10-Intern Scout Matrix (used in Round 1 Discovery Swarm)

The 10 parallel Interns in Round 1 are allocated across non-overlapping audit dimensions:
- M1 breaking changes (Intern 1): Renamed fields, new required parameters, breaking protobuf/schema contracts.
- M2 design promises (Intern 2): Does code do what the README and architecture specify, or stub it with `pass` and TODO?
- M3 blast radius (Intern 3): Shared state, events, or middleware that break downstream consumers or callers.
- M4 semantic drift (Intern 4): Config names, default values, environment variable names, error codes.
- G1 SRE defense (Intern 5, doc-blind): Leaks, missing locks, child processes not killed as a group, timeouts, shutdown signals.
- G2 hygiene (Intern 6, doc-blind): Swallowed errors (`except: pass`, ignored error objects), dead code, empty stubs, hidden hardcodes.
- G3 fake tests (Intern 7, doc-blind): GREEN-WHILE-EMPTY, `assert True`, `assert len(x) >= 0`, over-mocking, shadowed test functions.
- T1 completeness (Intern 8): Did the change finish the stated goal, or only the happy path?
- T2 side effects (Intern 9): Latency, rate limits, lock contention, broken global invariants.
- S1 SSOT and drift (Intern 10): Mismatches between implementation, MANIFEST.yaml keys, and rule boundaries.

## Deterministic 3-Round Swarm Pipeline

Audit executes strictly across 3 sequential Swarms (each round M=10 Interns via `subagent_batch`). Premature exit is forbidden:

1. **Round 1 (Discovery Swarm: M=10)**: Dispatch 10 Interns across the Scout Matrix (M1-M4, G1-G3, T1-T2, S1). Output dense defect hypotheses with exact `file#Lxx-Lyy` anchors.
2. **Round 2 (Adversarial Confrontation Swarm: M=10)**: Pair Interns as opponents (Red Team vs Blue Team) to disprove Round 1 hypotheses with code counterexamples. Classify each finding strictly as `[DISPROVEN]` or `[CONFIRMED]`.
3. **Round 3 (Independent Verdict Swarm: M=10 + Director Touch Reality)**: 10 fresh Interns blind-verify surviving `[CONFIRMED]` findings. The Director executes read-only Touch Reality probes to reject false positives.

## Loop Driver and Exit Guard (MANDATORY)

- **Mechanical Loop**: `WHILE round_counter < 3: dispatch_swarm(round=round_counter, M=10); record_checkpoint()`.
- **Exit Guard**: `IF round_counter < 3: ABORT (premature stop forbidden; dispatch next round immediately)`.
- **Zero Satisficing**: Never stop after Round 1 or Round 2. Rich findings are unverified hypotheses.
- **Convergence Rule**: A pipeline with zero HIGH and zero new MIDDLE findings converges at Round 3. If HIGH findings remain contested, extend up to Round 5 at most.
- **Terminal Round**: The final round is audit-only. It edits no code.
