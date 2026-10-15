---
name: prr
description: PR review mechanics. Weigh unresolved review findings by severity, verify each against source, reply before you resolve, and check the merge gate on one head SHA.
---

# prr: review threads and the merge gate

`auto` calls this skill. Use it alone when only the review loop is needed.

## Weighted score

| Severity tag | Weight |
|---|---|
| `severity: high` | 1.0 |
| `severity: middle` | 0.5 (also the weight of an untagged finding) |
| `severity: low` | 0.25 |

A total of 1.0 or more blocks the merge. Two middle findings block it. Read the literal tag. Do not infer severity from tone.

## Thread rules

1. **Read the source.** The GitHub diff can redact text: `Bearer {token}` appears as `Bearer ******`. Check the local file before you judge a report.
2. **Reply, then resolve.** The reply cites evidence: a commit SHA, a test command, or a line link. "Fixed" alone is not a reply.
3. **Resolve only what you verified** as fixed or obsolete. Never resolve an actionable, ambiguous, or unverified thread.
4. **Turn a false report into a test.** Add a falsifiable invariant test that guards the concern. Do not only dismiss it.

## Merge gate

- Checks, review, and merge refer to the same head SHA. A result from an older head does not count.
- Every check marked `blocks_merge: true` ends in success. Pending, skipped, cancelled, and unreadable all count as unmet.
- Let the gate tool compute the decision. Do not read a check table by eye.
- Commands are in `local.md`. Use the exit code only. Do not grep the output text.
