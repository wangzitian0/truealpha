# A3 — Source governance: corroboration classes, admission, and expansion order

Status: Draft (needs owner sign-off — codifies #579; extends AGENTS.md rule 7's spirit to
data planes)
Date: 2026-08-17

## Context

Every vendor-boundary incident of 2026-08 was a premise failure at an unguarded seam: an HTTP
error status a fake never sent (#557), an operating branch a fixture never emitted (#553), a
per-minute ceiling two same-instant consumers never declared (#574), a parser vintage the
origin registry never retained (#543). Meanwhile the only quality metric that ever told the
truth was the one with an external referent — price reconciliation against a second vendor —
and the failures that reached the served page (#529's 2010 share count, #533's
revenue-as-gross-profit) all lived in semantics with no referent at all.

Source expansion multiplies whichever property exists. This ADR makes falsifiability the
property that expansion multiplies.

## Decision 1 — every semantic declares its corroboration class

| class | falsifier | examples |
|---|---|---|
| **A** | a second independent origin, value-reconciled under a declared tolerance policy | market-price (yahoo + twelvedata) |
| **B** | one authoritative origin + internal cross-checks and domain bounds, graded into the quality report | SEC financial facts (#578's plausibility oracle) |
| **C** | judgment/extraction carrying accession, evidence span, and confidence | headcount (#70/#564) |

A source without a declared class and a working falsifier is inventory, not data. Class B's
oracle rules each ship with a fixture that fires them (D8: a rule that cannot fire measures
nothing).

## Decision 2 — the admission checklist (PR-blocking, five artifacts)

A PR that adds or materially changes a source must contain:

1. an adapter behind the existing `SourceFetchPort` — the executor never learns vendors;
2. a **cassette from the first real capture**, sha-anchored to `raw.fetches` (#569's pattern:
   the fixture IS reality, provably);
3. one named entry in `scripts/vendor_contract_smoke.py` per load-bearing vendor assumption;
4. a throttle/shared-budget declaration — requests per window, and the consumption window when
   the credential is shared across environments (#574's collision class);
5. an origin-registry entry for every parser vintage it introduces (#543's class).

The same rule extends to storage planes: a PR that ships a plane (table + read path) must
contain its deployed writer, or it does not merge (#527/#532/#576 measured the alternative).

## Decision 3 — expansion order is by corroboration added, not data added

Frozen until #530's vintage plane is deployed and proven on a scheduled tick. Then:

1. **#63 N-PORT holdings first** — independent share counts attack the worst measured lie
   class (a 2010 share count served as fresh, ranking the #1 position);
2. #62 analyst forecasts last — a new class-C-at-best plane that adds no corroboration to
   anything existing.

Correctness > coverage > scale (the /goal ranking); width is a reward, not a starting point.

## Consequences

- Adding a source becomes a checklist review, not a judgment call per PR.
- The quality report can state, per cell, which falsifier covered it — and D8 keeps every
  falsifier capable of firing.
- Single-source semantics stop being unfalsifiable by construction; they are class B with
  graded oracles, or they are declared inventory.
