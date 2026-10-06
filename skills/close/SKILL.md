---
name: close
description: Step 6 of the flow. End a session. Choose Complete or Suspend, write the handover, update the issue, ask the production question, route lessons, and clean up.
---

# close: exit gate

Run this when you stop, for any reason. Each rule came from a session that ended wrongly.

## 1. Choose the mode

```bash
BRANCH="$(git branch --show-current)"
gh pr view "$BRANCH" --json state,mergedAt
git status -sb
git log --branches --not --remotes --oneline | wc -l   # commits that exist only on this machine
```

| Condition | Mode |
|---|---|
| PR merged on main, and no service or runtime-config impact (a workflow change is an impact) | **Complete**, production disposition `none` |
| PR merged, but the repository has no production release pipeline or production already runs the merged SHA | **Complete**, production disposition `none` |
| PR merged, service impact, and the owner gave "deploy" or "hold" | **Complete** |
| PR merged, service impact, no answer yet | **Complete** after the production question; the issue stays open (section 3) |
| Anything else (unmerged, uncommitted) | **Suspend** |

Unmerged work is never Complete. Do not close the issue and do not delete the worktree in Suspend.
Unpushed commits are invisible to everyone else. One log feature lived 3 days on a never-pushed branch
while the docs described it as existing. Push the branch or write it into the handover.

## 2. Prove the running process is new

Skip this step when no process serves the change. Otherwise compare the process age with the file edit time.

```bash
python3 - "$FILE" "$PID" <<'PROBE'
import os, re, subprocess, sys, time

path, pid = sys.argv[1], sys.argv[2]
age = time.time() - os.stat(path).st_mtime
r = subprocess.run(["ps", "-o", "etime=", "-p", pid], capture_output=True, text=True)
raw = r.stdout.strip()
m = re.fullmatch(r"(?:(?:(\d+)-)?(\d+):)?(\d+):(\d+)", raw)
print(f"file changed {age:.0f}s ago; ps reports etime={raw!r}")
if r.returncode != 0 or not m:
    print("verdict: UNDETERMINED (ps gave no parseable etime: process gone, no permission, or other platform format)")
    sys.exit(2)
d, h, mi, sec = (int(x or 0) for x in m.groups())
elapsed = ((d * 24 + h) * 60 + mi) * 60 + sec
print(f"process running for {elapsed}s")
print("verdict: STALE (process is older than the edit, so it runs old code)" if elapsed > age
      else "verdict: FRESH (process started after the edit)")
PROBE
```

Use `ps -o etime=`. The keyword `etimes` exists only in procps-ng: macOS prints its keyword list to stdout and the output looks like a measurement.
Report "cannot measure" as UNDETERMINED. Never turn it into "not stale".

## 3. Production gatekeeper (service changes)

Merge, staging deploy, and production deploy are three separate stages. Report all three with evidence, then ask:

```text
[PROD GATEKEEPER]
- Stage 1 merge: release tag vX.Y.Z = commit <sha>
- Stage 2 staging: run <url>, soak <duration>, health <result>
- Stage 3 production: image <digest>, watchdog <state>
Authorize production deployment of vX.Y.Z? Reply "deploy" or "hold".
```

- Claim "deployed" only with an evidence chain: image digest, ledger entry, and a real end-to-end probe (the real browser or TUI).
  The owner asked twice "are you sure the last version is deployed?" Find the proof, then answer.
- Only a production change requires owner approval, with the owner present (owner instruction, 2026-10-06). Ask once, then finish the non-production close.
  Closing in silence is forbidden.
- Without an answer, keep the issue open. Reopen it if the merge closed it. Create the label `prod-pending` if it is missing.
  Comment `prod disposition: pending` with the release tag or commit SHA, and add the label.
- A "deploy" covers only the release it names. Immediately before dispatch, the owner must answer you live in this session; an earlier or relayed answer is not enough. Without approval, do not deploy production.
- After "deploy", you own the whole loop, including the physical check. Never ask the owner to run a command.

## 4. Suspend: handover and issue

Write the handover with four parts. Include the exact next command.

1. **Done:** commit SHAs, changed files, tests that passed.
2. **Blocked:** the cause (CI, open design question, waiting for approval).
3. **Decisions:** contracts you fixed and assumptions you overturned.
4. **Next:** the first command for the next session.

```bash
ws-bm write-note --folder memory/handover --tags handover --title "Handover: issue-<N> <slug>" --content "..."
```

Also update the issue with the same four parts. Search for an existing issue first. Create one only when none matches.
Commands for this machine are in `local.md`.

## 5. Complete: clean up

1. Kill every background task, watcher, and subagent bound to the worktree. Check with `lsof +D "$WORKTREE"`.
   Removing a tree under a running task corrupts it.
2. `git worktree remove ../<repo>_issue<N>_<slug>`.
3. Close the issue with the merge proof when the production disposition is `none`, `deployed`, or `hold`.
   With `pending`, keep it open (section 3).
4. Delete scratch files. Record each leftover TODO in the handover.

## 6. Route each lesson (distill)

Every finding from the session has exactly one destination, or an announced discard. Use a read-only agent to decide.
The agent that wrote the code does not judge its own lessons.

Ask in order. The first YES decides:

1. True only this time (one SHA, one count, one temp path)? **Discard, and say so in the report.**
2. Can an automatic check catch it? **Build the check. Do not write a rule.** A red assertion works every time. Prose works when read.
3. Would it change a future judgment? **Write it as a criterion.** Choose the tier by what it depends on:
   - holds on any machine: Root rules;
   - holds only with this environment's shared infrastructure: Workspace rules;
   - holds only for this project's goal: Repo rules.

   If you cannot name the tier, you do not yet know what it depends on. Do not write it.
4. Unfinished work, or state for someone else? **Issue or handover.**
5. A location of an external thing (dashboard, ticket, log path)? **Memory reference.**
6. True only for this checkout, or tier unclear? **Put it in the local holding area** for the read-only agent to place.

Red lines:

- **R1** No rule without the measurement that triggered it: which session, what went wrong, what it cost.
- **R2** Resident text holds criteria only. Move steps, lists, and templates to a skill and leave a pointer.
- **R3** The distiller is not the executor.
- **R4** Discard out loud.
- **R5** One invariant lives in one tier. Copies drift. Skills may restate a criterion but must point back to the owner.
  (2026-09-22: "the president writes no code" lived in two tiers, and one copy missed the stop condition added that day.)
- **R6** A pointer must resolve to the same literal string in the target. A pointer to a dropped invariant disguises the loss as convergence.

A finding that is both a criterion and unfinished work becomes two records. One record inside an issue dies when the issue closes.

## 7. Report

Report in the owner's language, in a short table. Show the mode, merge proof, verification that ran on which machine,
the oracles you did not touch, the sample size, the data age, and the discarded findings.
End with two answers: "Sufficient?" and "MECE?".
