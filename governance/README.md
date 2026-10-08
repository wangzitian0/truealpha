# Governance Records

This directory holds records, not enforcement. Nothing here gates a merge. CI does not validate these files against issues or PRs.

## What remains

- `approvals/<env>.yaml`: the reviewed capture approval of each environment. `tools/release_identity.py` reads it at run time. infra2 injects the value as `CAPTURE_APPROVED_BY`.
- `sources/model-provider-zhipu-glm-coding-plan.v1.json`: the admission record of the model provider (`init.md` rule 19).
- `gate0/issue-60.source-readiness.candidate-v1.json`: the source readiness record. Every source except the model provider is still `missing` there (`init.md` rule 19).

The readiness record names a predecessor file with a `sha256`. That file is not in the tree. Find it in git history.

## What was removed

A delivery-governance machine was enforced until 2026-07-17. It used batch manifests, integration leases, a Gate 0 manifest chain, and CI validators. The owner removed the machine. The records it produced stayed as frozen history.

#1061 deleted that history on 2026-10-06. It covered batches, leases, capabilities, evidence, handoffs, schemas, and the other Gate 0 files. No test, tool, or workflow read them. They remain in git history before that change.

New work needs only an issue and a PR. The content hashes that matter for point-in-time correctness are unchanged and stay mandatory. They are raw capture checksums, corpus identities, and snapshot identities. See the red lines in `AGENTS.md`.
