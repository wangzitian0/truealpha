---
name: prr
description: PR review mechanics. Weigh unresolved review findings by severity, verify each against source, reply before you resolve, and check the merge gate on one head SHA.
---

# prr: review threads and the merge gate

`auto` calls this skill. Use it alone when only the review loop is needed.

## Blocking threshold (conjunctive budget)

| Severity tag | Allowed unresolved count | Notes |
|---|---|---|
| `severity: high` | `0` | Any unresolved high finding blocks the merge |
| `severity: medium` | `<= 2` | Unlabelled findings and `severity: middle` count as `medium` |
| `severity: low` | `<= 4` | Minor/nit findings |

A PR is review-ready only when all three conditions hold simultaneously: `High == 0` AND `Medium <= 2` AND `Low <= 4`. Read the literal `severity:` tag. Do not infer severity from tone. Reviewer platform errors, timeouts, or unparseable reviews warn and do not block.

## Thread rules

1. **Read the source.** The GitHub diff can redact text: `Bearer {token}` appears as `Bearer ******`. Check the local file before you judge a report.
2. **Reply, then resolve.** The reply cites evidence: a commit SHA, a test command, or a line link. "Fixed" alone is not a reply.
3. **Resolve only what you verified** as fixed or obsolete. Never resolve an actionable, ambiguous, or unverified thread.
4. **Turn a false report into a test.** Add a falsifiable invariant test that guards the concern. Do not only dismiss it.

## Merge gate

- Checks, review, and merge refer to the same head SHA. A result from an older head does not count.
- Every check marked `blocks_merge: true` ends in success. Pending, skipped, cancelled, and unreadable all count as unmet.
- Let the gate tool compute the decision. Do not read a check table by eye.
- Run `ws-pr-gate` (see `AGENTS.md`). Use the exit code only. Do not grep the output text.
