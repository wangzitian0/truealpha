# Governance Records

This directory holds **records, not enforcement**. Nothing here gates a merge; CI does
not validate these files against issues, PRs, or each other. They exist so that captured
data, evaluation results, and accepted interfaces stay verifiable and replayable.

- `capabilities/issue-<number>.v1.json` — the dependency graph between capability issues
  (which capability feeds which, at which gate). Planning information for humans and
  agents; update it when the plan changes, ignore it when it is stale.
- `evidence/` — accepted evaluation evidence. Each record content-hashes the exact
  inputs, commands, and outputs of an accepted run so it can be replayed byte-for-byte.
  Records are append-only: a new evaluation adds a new versioned file.
- `handoffs/` — accepted interface handoffs between producers and consumers (for example
  DataHub → strategy). Consumers pin the exact handoff ID and hash they build against,
  never `latest`.
- `schemas/` — JSON Schemas for the record types above.
- `batches/`, `leases/`, `gate0/`, `vision-issue-graph.json` — **frozen history**. Until
  2026-07-17 these drove an enforced delivery-governance machine (batch manifests,
  integration leases, a Gate 0 candidate manifest chain, CI validators, and issue-body
  mirrors). The machine was removed by owner decision because its coordination cost
  exceeded its value; the files stay because accepted tests pin corpus and handoff hashes
  recorded in them. Do not extend them: new work needs only an issue and a PR.

The content hashes that matter for point-in-time correctness — raw capture checksums,
corpus and snapshot identities, evidence and handoff records — are unchanged and remain
mandatory (see the red lines in `AGENTS.md`).
