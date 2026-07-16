# Accepted Handoffs

Store one canonical JSON file per accepted producer-to-consumer handoff. Files follow
`governance/schemas/handoff-manifest.schema.json` and are immutable after acceptance;
a revocation or correction creates a new revision that points at the prior handoff.

These are records, not merge gates. To publish a handoff: write the file with the exact
evidence digests and allowed environments, reference it from the producing PR, and note
it on the issue. To consume one: pin the file path and its SHA-256 in the consuming code
or test (never `latest`), so a drifted handoff fails the consumer's own tests. Historical
records may reference pre-2026-07-17 batch IDs; new records reference issues and PRs.
