---
name: init
description: Step 1 of the five-step flow. Run before any change. Inventory existing tools, read the original design intent, recall memory, claim the issue, and create an isolated worktree.
---

# init: question the requirement, inventory, isolate

Run this before the first edit. Each rule exists because an agent skipped it and the owner had to correct it.

## 1. Inventory before you build

The owner repeatedly found new scripts that duplicated existing ones. Follow the **Key List Protocol**:

1. **Check the Key List**: Read the repository's SSOT index (`common/meta/data/MANIFEST.yaml`, `docs/ssot/MANIFEST.yaml`, or the App's contracts index).
   - Scan the capability keys (e.g., `deploy_v2`, `infra2-sdk`, `vault`, `signoz`, `pr_merge_gate`).
2. **Reuse if present**: If an existing component covers the need, reuse it directly. Do not build private alternatives.
3. **Append when new**: If no entry fits and a new capability is necessary:
   - Build the minimal implementation.
   - **Register the new key and summary into `MANIFEST.yaml` in the same PR.**
4. **First-turn contract**: State in your opening response:
   - `"Reusing existing component: <key>"` OR `"New capability: will register <key> in MANIFEST.yaml"`.

## 2. Read the original intent before you change a mechanism

Agents reversed or "fixed" a design without knowing why it existed. Example: skill content is one source, the mapping to hosts is another source.

- Find the issue, PR, or SSOT page that created the mechanism. Read it first.
- Write one sentence: "This exists because ...". If you cannot, you are not ready to change it.
- A fix that "does not take effect" needs the deep cause. Check that the host really loads the file. Do not retry the same edit.

## 3. Recall

Use the `recall` skill for the search. Read hits as claims, not facts: check the current code.

## 4. Scan handover and claim the issue

- Read the latest handover for the issue (`handover issue-<N>`; fall back to `gh issue view <N> --comments`).
- Search for an existing issue before you create one. Update it when it exists.
- Another worktree or running process for the same issue is a lock. Do not take over. Match the full token:

```bash
git worktree list | grep -F "_issue<N>_"
```

A substring match is wrong: `issue12` would match `issue123`.

Report the open production decisions once: issues labelled `prod-pending` in every repository of the owner. The command is in `local.md`.

## 5. Create the worktree

- Name: `<repo>_issue<N>_<slug>`. Branch: `feat/issue<N>-<slug>`. Base: the latest `origin/main`.
- Never edit in a shared checkout. One `git add` in a shared index can commit another worker's files.
- The worktree location decides which rules reach it. See `local.md`.
- The worktree must work alone. Tools and tests must not use `../..` to find files outside it.

## 6. Report the baseline

Reply in the owner's language with four lines: the goal, the tools you reuse, the facts you recalled, the worktree path.
