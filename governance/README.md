# Delivery Governance

`vision-issue-graph.json` is the machine-readable ownership and dependency view for every
GitHub issue labeled `scope:vision`. It separates ordered Gate fan-in from typed artifact
dependencies so a later Gate never becomes a global implementation lock.

`batches/*.json` is the canonical source for capability-batch authorization. A GitHub issue
links the path and exact SHA-256, but its mutable body is not authoritative. A queued batch
may have unresolved activation fields. An active batch must pin its `main` base SHA, corpus,
reviewer, path ownership, and exact accepted start handoffs.
`target_rung` is work in progress; only `last_accepted_rung` is evidence already earned.

Issue #81 is an activation blocker. Until its actual-diff, merge-base, corpus-byte, glob,
lease, command-evidence, and handoff checks merge, every batch in this directory remains
queued regardless of its GitHub label.

Run `make issue-graph-check` for offline validation. CI also exports live GitHub issues and
checks `scope:vision` parity, Gate/batch labels, milestone ownership, and batch manifest
hashes. A parity failure blocks Gate closure and batch activation.

E1 output is immutable `TinyEvidence`, not a stable dependency. An E2 handoff must be stored
under `governance/handoffs/` using the lifecycle defined in `docs/iterative-delivery.md`
before a named downstream consumer may pin it.
