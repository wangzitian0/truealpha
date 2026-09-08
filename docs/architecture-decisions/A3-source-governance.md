# A3 — Source governance: corroboration classes, admission, and expansion order

Status: Accepted. Decisions 1-2 (corroboration classes, admission checklist) are how
sources have been added since 2026-08-17 and codify #579; Decision 3's expansion order was
approved by the owner on 2026-09-08 (N-PORT holdings before analyst forecasts) and remains
gated on #530's vintage plane being deployed and proven. Header corrected 2026-09-08.
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
3. one named entry in `apps/data-engine/scripts/vendor_contract_smoke.py` per load-bearing vendor assumption;
4. a throttle/shared-budget declaration — requests per window, and the consumption window when
   the credential is shared across environments (#574's collision class);
5. an origin-registry entry for every parser vintage it introduces (#543's class).

The same rule extends to storage planes: a PR that ships a plane (table + read path) must
contain its deployed writer, or it does not merge (#527/#532/#576 measured the alternative).

## Decision 3 — expansion order is by corroboration added, not data added

Approved by the owner 2026-09-08. Gated on #530's vintage plane being deployed and proven
on a scheduled tick, then:

1. **#63 N-PORT holdings first** — independent share counts attack the worst measured lie
   class (a 2010 share count served as fresh, ranking the #1 position);
2. #62 analyst forecasts last — a new class-C-at-best plane that adds no corroboration to
   anything existing.

   Consequence recorded 2026-09-08: milestone #773's deliverable 7 (module 4, analyst track
   record) rides on #62 and therefore sorts after the #63-backed work inside that milestone.
   The ordering is the corroboration rule, not a scheduling preference: an ETF holdings plane
   gives an independent share count that falsifies the class that produced #529's 2010 count,
   while a forecast plane has nothing to check it against.

Correctness before coverage, coverage before scale; width is a reward, not a starting point.

## Consequences

- Adding a source becomes a checklist review, not a judgment call per PR.
- The quality report can state, per cell, which falsifier covered it — and D8 keeps every
  falsifier capable of firing.
- Single-source semantics stop being unfalsifiable by construction; they are class B with
  graded oracles, or they are declared inventory.
