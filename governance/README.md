# Governance Records

This directory holds records, not enforcement. Nothing here gates a merge. CI does not validate these files against issues or PRs.

## What remains

- `sources/model-provider-zhipu-glm-coding-plan.v1.json`: the admission record of the model provider (`init.md` rule 19).
- `reviews/large_model_value_v0-definition-1.0.0.md`: the owner review of strategy definition 1.0.0 (#1235), with its content hash and the production replay.
- `reviews/large_model_value_v0-definition-1.1.0.md`: the active review record for strategy definition 1.1.0.
- `freezes/core-path-2026-10-12.md`: the freeze of the non-core areas (#1237), lifted on 2026-10-20: the paths, the baseline, the exit condition, and how to measure it again.

## What was removed

A delivery-governance machine was enforced until 2026-07-17. It used batch manifests, integration leases, a Gate 0 manifest chain, and CI validators. The owner removed the machine. The records it produced stayed as frozen history.

#1061 deleted that history on 2026-10-06. It covered batches, leases, capabilities, evidence, handoffs, schemas, and the other Gate 0 files. No test, tool, or workflow read them. They remain in git history before that change.

New work needs only an issue and a PR. The content hashes that matter for point-in-time correctness are unchanged and stay mandatory. They are raw capture checksums, corpus identities, and snapshot identities. See the red lines in `AGENTS.md`.
