---
name: auto
description: Step 5 of the five-step flow. Drive a verified change to a merge on main with no owner prompts. Orchestrates review, gate, and merge. Stops at production.
---

# auto: deliver to main

Run this after `smoke` passes. Use `prr` for the review and gate mechanics.

## Definition of Stage 1 (Code Landed)

**Merge to main is Stage 1 (Code Landed), not task completion.** The owner corrected "done" claims for unmerged work many times:
"If you did not open the PR and merge it, it is not done."
Landing on main is a physical prerequisite for Stage 2 (Staging) and Stage 3 (Prod).
Task completion requires both code landing and physical reality verification.

- Merge only through a PR. The owner grants merge authority when every gate passes.
- Merge when ready. Then continue from the latest main. Do not pile up divergent branches.

## Loop

1. Open the PR. Quote the owner instruction in the description when the change touches rules, gates, or other protected files.
   Under the quote, list each agent decision that the quote does not cover. A quote above agent decisions reads as owner approval of all of them.
   A change to rule text also carries the section `Owner touchpoints` (dev_env Repo tier, rule-text checklist item 8).
2. Run the `prr` gate. Fix each finding in the issue worktree. Do not touch unrelated files.
3. Run focused checks on every fix. Read the decision from the gate exit code:
   - Exit 0: Merge directly with `--merge`.
   - Exit 1 (Wait): Poll with exponential backoff (`30s * 1.5^n`, capped at 120s). Apply a client-side deadline of 45 minutes (maximum 30 iterations). If checks remain pending after 45 minutes, transition to `Suspend`, record the CI snapshot in the issue, and report `In review`. Do not spin indefinitely.
   - Exit 2 (Needs owner): Stop immediately. Do not spin. Label `needs-owner` and record the holding reason in the issue.
   - Exit 3 (Action required): Resolve findings or fix conflicts, push, and re-run. If unresolved after two rounds, escalate to Senior or Suspend.
   - Exit 4 (Could not evaluate): Pull main or retry after rate limits settle.
4. A change with no side effect needs no question to the owner. Fix it and merge it.
5. Merge. Verify with `ws-delivery-status` that the commit is merged on `origin/main` (exit code 0, `CODE_LANDED_STAGE1`).
   Transition to Reality Probe or deployment verification before declaring task complete.
6. Execute the post-delivery retrospective checks before closing the delivery.

## Post-delivery retrospective (Retrospective Swarm: M=3)

Do not stop immediately after `git merge`. Dispatch 1 Retrospective Swarm (M=3 Interns via `subagent_batch`) before concluding the delivery:

1. **Intern 1: Recurring mistake and harness constraint check**:
   - Check if any review comment, gate rejection, or CI retry occurred during this delivery.
   - If an error occurred, identify the mechanical guard (pre-commit check, linter, or local gate assertion) that catches it locally in under one second.
   - Do not resolve a problem only through repeated human or agent rework. Convert the lesson into a physical check or register an issue to reinforce harness constraints.

2. **Intern 2: Harness loading and context footprint audit**:
   - Inspect the size of always-loaded rules (`AGENTS.md`) and projected files.
   - Verify that always-loaded rules contain only decision invariants (boundaries, prohibitions, thresholds).
   - Verify that prompt context contains no duplicate clauses between host rules and repository projections.
   - Move operational instructions, historical incident narratives, and procedures to on-demand skills or SSOT documents.

3. **Intern 3: Delivery pipeline telemetry and acceleration**:
   - Record the runtime of each delivery phase: local tests, gate evaluation, and remote CI jobs.
   - Flag any remote CI job that exceeds 60 seconds (such as un-cached Docker builds or slow test shards).
   - Identify whether the slow job can be short-circuited by file path filters or accelerated with dependency caching.

## Stop and return to the owner

Stop only for one of these:

- **Production change** (apply, promote, runner rebuild, and the rest of the repository's production reservation). Merge is not deploy. After the merge, report the merge SHA, the staging evidence, and the production state, then ask the owner for "deploy" or "hold".
- **Self-adjudication**: the PR changes what decides whether this PR may merge.
  A relaxation, or a change whose direction you cannot prove, goes to the owner.
  Only the three carve-outs below change this.
  A release workflow deploys, promotes or applies to a production environment.
  The release code is the code that a release workflow runs.
  The owner also approves each edit to merge-gate code or data, even when the gate proves it tighter.
  Only a proven tightening of the infra2 gate inventory, with a verified run as defined below, changes this.
  A workflow that defines a required check is part of the gate, so this rule covers its edits too.
  In a repository with a release workflow, the owner also approves workflow edits and edits to the release code.
  A verified run is a run of this repository's merge gate that gives exit 0 and prints `production lock verified`.
  With a verified run, the agent merges an edit to the release code.
  With a verified run, the agent merges a workflow edit, except in a workflow that defines a required check.
  With a verified run, the agent merges a proven tightening of the infra2 gate inventory.
  In a repository whose gate does not print `production lock verified`, these edits stay with the owner.
  A Repo tier may record an owner decision that replaces this rule for that repository.
- **Irreversible action**: data deletion, or an external publication that cannot be recalled.
- **Another worker's issue lock**: an issue-prefixed worktree, an open PR, or a running process.

A large impact or an unclear preference is not a stop reason. Choose, state the basis, and keep a way back.

When a gate returns the owner for a non-production PR, add the label `needs-owner`. Remove it when the PR merges or closes.
A non-production PR that waits for the owner more than 12 hours shows a defect in the rule that holds it.
Find the missing physical fact. Do the agent work that supplies it. Ask the owner only for what needs the owner's hands.
Never list a merge or a head approval of non-production work as an owner action.
The exception is an edit that the production reservation keeps with the owner permanently.
Only three such holds exist: the reservation's self-guard, infra2's owner-held set (rule 6) and dev_env's `ci.yml` hold.
Such an edit waits for the owner by design, so its wait is not a defect.

## Report rules

- State the root cause before you ask for a decision. The owner could not decide without it.
- Do only what was asked. "I asked you to add a skill. What else did you do?" is a real correction.
- Keep committed text free of private names, internal hosts, and local paths. It must be safe to paste into a public issue.
- Keep the description short and plain. The owner rejected text with an "AI tone".
