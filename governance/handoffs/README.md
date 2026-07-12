# Accepted Handoffs

Store one canonical JSON file per produced E2-or-higher handoff. Files follow
`governance/schemas/handoff-manifest.schema.json` and are immutable after acceptance;
revocation creates a new revision that points at the prior handoff.

A handoff may be consumed only when `state` is `accepted`, its reviewer differs from the
producer owner, every evidence digest matches, the consumer batch ID is explicitly listed,
and the requested environment is allowed. The consumer records both this file path and its
SHA-256 in its batch manifest. The delivery-governance check rejects unverified, altered,
revoked, or unauthorized dependencies.

Issue #57/#58 contracts predate this lifecycle. Their two explicitly marked legacy Git refs
are the only exemption; no new `legacy_accepted` dependency is permitted.
