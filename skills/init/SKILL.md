---
name: init
description: Step 1 of the five-step flow. Run before any change. Inventory existing tools, read the original design intent, recall memory, claim the issue, and create an isolated worktree.
---

# init: question the requirement, inventory, isolate

Run this before the first edit. Each rule exists because an agent skipped it and the owner had to correct it.

## 1. Inventory before you build

The owner repeatedly found new scripts that duplicated existing ones. The duplicates polluted the repository.

- List what exists: `ls tools/ libs/` and the SSOT index (`docs/ssot/MANIFEST.yaml` when present).
- Search for the capability by name and by behavior. Reuse or extend the existing tool.
- State in the first reply which existing tool you reuse. If none fits, state why.
- Ask "is this over-designed?" and "which existing logic must I remove?" before you propose a new mechanism.

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
