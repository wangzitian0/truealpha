# Release protocol

Written after a release that took a day and four failed dispatches, and after a
version number was claimed twice by two agents working in parallel.

## Claim the version first, push the tag immediately

Decide the version, create the annotated tag, and **push it before doing
anything else**. Not after CI, not after staging, not after the deploy — the
push is the first outward action of a release.

The tag push is the lock. `git push origin vX.Y.Z` fails when the ref already
exists, so two agents cannot claim the same version: the loser sees

```
! [rejected]        v0.0.21 -> v0.0.21 (already exists)
```

and takes the next number. That is exactly what happened on 2026-08-17 — a
concurrent agent tagged v0.0.21 while this lane was preparing the same release,
and the collision was visible immediately instead of producing two different
artifacts under one name.

**Never move a pushed tag.** `deploy-release.yml` calls its target an
"Immutable release promotion target", and infra2 diffs production cumulatively
from the marker it names. A tag that is deployed and later moved makes every
subsequent promotion diff from a baseline that no longer exists. When a tag
turns out to be short of what you wanted — v0.0.21 stopped seven commits before
main — cut the next number, say so in its message, and leave the old one alone.

## An abandoned tag costs nothing

v0.0.21 was tagged and never deployed. That is untidy and it is not a leak: a
tag that no environment serves holds no resource, blocks no lane, and the
freshness guard judges what production *serves*, never what tags exist.

## Parallel releases do not deadlock

`deploy-release.yml` sets

```yaml
concurrency:
  group: truealpha-release-${{ inputs.deploy_type }}-${{ inputs.version_ref }}
  cancel-in-progress: false
```

The version is part of the key, so two different releases never queue behind
each other. Two dispatches of *the same* type and version do queue, and they
wait rather than cancel — which is what you want when the second one is a
retry of a deploy whose outcome is unknown.

There is no lock held across the rest of the flow. If a release is abandoned
after the tag is pushed — CI red, an evidence gate refuses, the agent stops —
nothing is left held. The next release picks the next number and proceeds.

## What actually costs time

Measured on the v0.0.22 and v0.0.23 releases, not estimated:

| Step | Time | Compressible? |
|---|---|---|
| `ci-required` on the tag | ~5–6 min | Duplicates the run that was already green on main for the same SHA. The biggest remaining win, and a change to the evidence contract — not taken here. |
| Dispatch to infra2 | ~350 s | This is infra2 performing the deploy. Not ours to compress. |
| Surface walk | ~116 s | ~21 s of it was re-downloading Chromium every run; now cached. The rest is the walk itself, 12 routes across two identities. |
| Everything else | 2–4 s each | Nothing to take. |

Failed dispatches dominated the real cost: four consecutive attempts for
v0.0.23 collided with another project's deploys, ~20 minutes lost and the lane
shut, because the receiver run was correlated by a window in time rather than
by the request id that was already in the run title. Fixed in infra2-sdk.
