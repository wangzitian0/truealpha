# Delivery Governance

`vision-issue-graph.json` is the machine-readable ownership and dependency view for every
GitHub issue labeled `scope:vision`. It separates ordered Gate fan-in from typed artifact
dependencies so a later Gate never becomes a global implementation lock.

`batches/*.json` is the canonical source for capability-batch authorization. A GitHub issue
links the path and exact SHA-256, but its mutable body is not authoritative. A queued batch
may have unresolved activation fields. An active batch must pin its `main` base SHA, corpus,
reviewer, path ownership, and exact accepted start handoffs.
`target_rung` is work in progress; only `last_accepted_rung` is evidence already earned.

`gate0/manifest-v4.json` is the aggregate Gate 0 acceptance candidate. It is separate
from capability-rung manifests: it binds #57-#61 and all external attestations into one
immutable candidate tree and is the only Gate 0 PR allowed to target `main`. The candidate
check may validate an explicitly blocked review packet; the acceptance check fails until
every artifact and attestation is accepted.

`candidate-v1` artifacts are proposal evidence and cannot become accepted through a
state-field edit. Acceptance requires separately materialized `accepted-v1` artifacts that
bind the completed evidence before external comments attest their exact artifact hashes.
Live validation proves the GitHub comment identity, wording, target hash, and configured
role separation; it cannot prove that a human actually holds legal, budget, or
organizational authority. That authority remains an explicit external boundary and must
not be inferred from repository access or a passing workflow.

Issue #81 is an activation blocker. Until its actual-diff, merge-base, corpus-byte, glob,
lease, command-evidence, and handoff checks merge, every batch in this directory remains
queued regardless of its GitHub label.

Run `make issue-graph-check` for offline validation. CI also exports live GitHub issues and
checks `scope:vision` parity, Gate/batch labels, milestone ownership, and batch manifest
hashes. A parity failure blocks Gate closure and batch activation.

E1 output is immutable `TinyEvidence`, not a stable dependency. An E2 handoff must be stored
under `governance/handoffs/` using the lifecycle defined in `docs/iterative-delivery.md`
before a named downstream consumer may pin it.

Pull-request authorization uses `--pr-base-sha` and `--pr-head-sha`. A preparation PR
freezes the corpus, reviewer, exact base, and paths before an implementation PR may move
the batch to `active` and accept one rung. Shared integration paths require a content-
addressed, unexpired `IntegrationLease`. CI executes the manifest's commands on the exact
head and retains a `RungEvidence` report; this report is evidence for the rung only and is
not an E2 handoff.
