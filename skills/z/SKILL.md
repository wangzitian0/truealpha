---
name: z
description: Closed-loop orchestrator for the five-step flow. Senses physical delivery state, advances through steps, and loops until verified complete.
---

# z: closed-loop execution engine

Execute the five-step flow as a closed state machine.
Do not stop or declare completion until the physical delivery state is `VERIFIED_COMPLETE`.

## 1. Physical state sensing

Run the physical state validator before taking any action:

```bash
ws-delivery-status --json
```

Read the `"state"` field from stdout and execute the corresponding branch:

| Physical state | Flow phase | Mandatory action | Next transition |
|---|---|---|---|
| (Not in worktree) | Step 1: `init` | Run `init` to claim issue and make an isolated worktree | Enter worktree, sense state |
| `DRAFT` | Step 2/3: `ssot` / `smoke` | Implement minimal logic, verify focused tests pass | Run `git commit`, sense state |
| `LOCAL_VERIFIED` | Step 4: `auto` | Run `auto` to create PR and initiate review gate | PR opened, sense state |
| `IN_REVIEW` | Step 4: `auto` | Monitor CI checks, resolve review feedback, trigger merge | PR merged, sense state |
| `CODE_LANDED_STAGE1` | Step 5: `close` | Execute Business Reality Probe with `--assert-complete` | Verify probe exit 0, sense state |
| `VERIFIED_COMPLETE` | Exit gate | Run `close` final session cleanup; delivery is complete | Break loop; conclude session |

## 2. State branches

### Branch A: `DRAFT` (Working tree dirty)
1. Read the task specification and relevant SSOT documents.
2. Apply changes following the `ssot` skill (minimal code, delete obsolete logic).
3. Run focused tests following the `smoke` skill (`pytest <file>::<test> -x`).
4. Commit changes with an ASD-STE100 compliant conventional commit message.
5. Immediately loop back to Section 1 to re-sense state.

### Branch B: `LOCAL_VERIFIED` (Committed, unmerged)
1. Verify HEAD commit is pushed or pushable.
2. Invoke the `auto` skill to create a GitHub PR with title and body linking the issue.
3. Immediately loop back to Section 1 to re-sense state.

### Branch C: `IN_REVIEW` (PR open)
1. Run `python -m tools.pr_merge_gate <PR_NUM> --request-review --merge` (or repo merge tool).
2. If CI checks are running, schedule a wait timer and re-evaluate on notification.
3. If review comments arrive, resolve actionable feedback and push fixes.
4. When the gate merges the PR to `main`, immediately loop back to Section 1.

### Branch D: `CODE_LANDED_STAGE1` (Merged on main, reality unverified)
Stage 1 (Code Merge) has completed. Real-world execution is NOT yet verified.
1. Formulate a read-only, non-mutating Business Reality Probe `<CMD>` verifying actual system behavior:
   - For database/data changes: query live tables or check metrics.
   - For CLI/APIs: query actual endpoint or CLI status.
   - For library/tools: run integration tests or end-to-end verification.
   - Prohibit bare `echo`/`printf` and mutating commands (`DROP`, `DELETE`, `rm -rf`).
2. Execute the verification gate:
   ```bash
   ws-delivery-status --assert-complete --reality-probe "<CMD>"
   ```
3. If the command exits with code 2, inspect the failure reason, fix defects, and re-probe.
4. If the command exits with code 0 (`VERIFIED_COMPLETE`), proceed to Branch E.

### Branch E: `VERIFIED_COMPLETE` (Physical proof achieved)
1. Both conditions are satisfied: commit is on `main` and reality probe passed.
2. Invoke the `close` skill to record evidence, update tracking issue, and clean up.
3. Report final completion with verified evidence.

## 3. Loop invariants

1. **Zero self-grading**: Never determine completion from model reasoning or prose. Only `ws-delivery-status` exit code 0 (`VERIFIED_COMPLETE`) permits session conclusion.
2. **Compaction resilience**: When resuming after context compaction, run `ws-delivery-status --json` immediately to recover the current state frame.
3. **No premature exit**: If an agent feels "done" before Branch E, this is a satisficing defect. Re-sense state and continue the loop.
