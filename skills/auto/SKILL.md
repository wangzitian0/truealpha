---
name: auto
description: Step 5 of the five-step flow. Drive a verified change to a merge on main with no owner prompts. Orchestrates review, gate, and merge. Stops at production.
---

# auto: deliver to main

Run this after `smoke` passes. Use `prr` for the review and gate mechanics.

## Definition of done

**Done means merged on main.** The owner corrected "done" claims for unmerged work many times:
"If you did not open the PR and merge it, it is not done."

- Merge only through a PR. The owner grants merge authority when every gate passes.
- Merge when ready. Then continue from the latest main. Do not pile up divergent branches.

## Loop

1. Open the PR. Quote the owner instruction in the description when the change touches rules, gates, or other protected files.
2. Run the `prr` gate. Fix each finding in the issue worktree. Do not touch unrelated files.
3. Run focused checks on every fix. Read the decision from the gate exit code.
4. A change with no side effect needs no question to the owner. Fix it and merge it.
5. Merge. Verify on `origin/main` that the commit exists.

## Stop and return to the owner

Stop only for one of these:

- **Production change** (apply, promote, runner rebuild, and the rest of the repository's production reservation). Merge is not deploy. After the merge, report the merge SHA, the staging evidence, and the production state, then ask the owner for "deploy" or "hold".
- **Self-adjudication**: the PR changes what decides whether this PR may merge. A relaxation, or a change whose direction you cannot prove, goes to the owner.
  A change that the gate proves tighter may proceed, with one exception. Until the owner signs off an unbypassable production lock (infra2 #1035),
  an edit to a workflow, to merge-gate code or data, or to code that a release workflow runs goes to the owner.
- **Irreversible action**: data deletion, or an external publication that cannot be recalled.
- **Another worker's issue lock**: an issue-prefixed worktree, an open PR, or a running process.

A large impact or an unclear preference is not a stop reason. Choose, state the basis, and keep a way back.

## Report rules

- State the root cause before you ask for a decision. The owner could not decide without it.
- Do only what was asked. "I asked you to add a skill. What else did you do?" is a real correction.
- Keep committed text free of private names, internal hosts, and local paths. It must be safe to paste into a public issue.
- Keep the description short and plain. The owner rejected text with an "AI tone".
