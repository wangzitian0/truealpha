# Entity identity: opaque UUIDs, typed aliases

Status: owner decision 2026-09-17 (#877). PR-2 of #877 adds the store
(`db/migrations/20260917T0412_datahub_entity_identity_store.sql`) and fills it
(`20260917T0430_datahub_entity_backfill.sql`). Nothing reads it yet.

This document replaces decision D1 of the
[#877 design note](https://github.com/wangzitian0/truealpha/issues/877#issuecomment-5695658561),
which proposed CIK and FIGI as the canonical ids.

## The decision

The owner's words (translated): *"These ids are of different categories and schemas, from an
ontology point of view. Use UUIDs to identify entities, together with aliases. Our store should
be flexible; later we will feed a graph database and an embedding store."*

So:

- **An entity's id is an opaque UUID.** Issuers, instruments, listings and funds get one now.
  Universes, people, segments and products get one when something writes them. The UUID is
  derived, not random (§2), but no code may derive meaning from it: an id is always looked up,
  never parsed.
- **Every identifier we hold is a typed alias of an entity.** That includes CIK, LEI, CUSIP,
  ISIN, FIGI, ticker@MIC, moomoo codes and our own pre-#877 strings (`issuer:lei:…`,
  `security:figi:…`, `listing:xnas:aapl`). An alias has a scheme, a value, a validity
  interval, a knowable-at time, a source, a pointer to its evidence and a confidence. No
  scheme is canonical.
- **Nothing is rewritten.** A wrong alias is ended by a retraction row. A merge adds
  `same_as` and `superseded_by` edges. A split adds `split_into` edges. The resolver follows
  the edges.

## 1. Ontology

### Kinds

`staging.entity_kinds` is the type hierarchy. Abstract kinds are never minted.

```
entity (abstract)
├── agent (abstract)
│   ├── organization (abstract)
│   │   └── issuer ─── fund          a fund issues its own shares and holds instruments
│   └── person                       analysts, executives (no writer yet)
├── instrument                       share-class level: what CUSIP, ISIN and share-class FIGI name
├── listing                          one instrument's line on one venue: MIC + symbol
├── universe                         a governed membership list (no writer yet)
├── segment                          a reportable segment of an issuer (no writer yet)
└── product                          a product or service line (no writer yet)
```

A kind is data. Adding one is an insert in a migration, not a code change.

### Relations

`staging.entity_relation_types` declares each edge type with a domain and a range. The insert
guard checks both against the hierarchy, so `issues` accepts a `fund` as its source because a
fund is an issuer.

| relation | from → to | meaning |
|---|---|---|
| `issues` | issuer → instrument | the issuer of an instrument; one issuer per instrument at a time |
| `listed_as` | instrument → listing | the instrument trades as this listing; one instrument per listing at a time |
| `holds` | fund → instrument | a holding; the weights stay in `staging.fund_holding_facts` |
| `member_of` | entity → universe | membership over a validity interval (the "universe member" is this edge, and the edge has its own UUID) |
| `subsidiary_of` | organization → organization | corporate control |
| `successor_of` | organization → organization | a reorganized successor (the #496 ExxonMobil holdco) |
| `segment_of` | segment → issuer | a reportable segment |
| `produces` | organization → product | an offered product |
| `supplies_to` | organization → organization | a disclosed supplier relationship |
| `covers` | person → issuer | analyst coverage |
| `same_as` | entity → entity (same kind) | evidence that two entities are one thing |
| `superseded_by` | entity → entity (same kind) | a merge; the resolver follows it |
| `split_into` | entity → entity (same kind) | part of the source continues as the target from `valid_from` |

Only `issues`, `listed_as` and the three identity edges have a writer after PR-2. The others
are registered so that their shape is fixed before anything writes them.

## 2. Minting: UUIDv5 from the birth alias

Ids in this repository are derived, never random. A run id comes from its cutoff and
universe, and an observation id comes from its bytes and vintage. Entity ids follow the same
rule.

**The rule.**

```
root              = uuid5(NAMESPACE_URL, "https://github.com/wangzitian0/truealpha/blob/main/docs/entity-identity.md")
kind namespace    = uuid5(root, "kind:<kind>")                 stored in entity_kinds.uuid_namespace
entity id         = uuid5(kind namespace, "<scheme>:<value>")  the birth alias, canonical value
relation id       = uuid5(uuid5(root, "relation:<type>"),
                          "<from>|<to>|<valid_from>|<transaction_time UTC>|<source>|<method>")
```

- `mint_rule` is `uuidv5:v1`.
- `staging.entity_mint(kind, scheme, value, minted_by)` is the only way an entity is created.
  An insert trigger refuses any entity id that is not the one its birth alias derives, and any
  relation id that is not the one its claim derives.
- The SQL is `staging.entity_uuid_v5`, built on pgcrypto's SHA-1. It returns exactly what
  Python's `uuid.uuid5` returns, and a test checks every minted id against Python.
- **Choosing the birth alias.** The backfill takes the component's own claims, never the clock
  or the load order. It picks the pipeline's legacy id that became knowable first (by the
  evidence's `transaction_time`), then the smallest value. For AAPL today that is
  `legacy-id:issuer:cik:0000320193`, because both universes' ids carry the same knowable-at
  and `cik` sorts before `lei`.
- **Collisions.** If an entity already holds the derived id, its birth alias has been
  retracted and the value now names something else. The name then gets a `#2`, `#3`, …
  suffix (`birth_generation`). The generation sequence depends only on the store's history.

**What derivation guarantees.**

- **Same evidence, same ids, in any database.** A fresh database, a replay, staging and
  production all mint the same UUID for the same evidence. Exports from the two environments
  join on the id.
- **Stores that learn a link late still agree.** Suppose a store minted the LEI and the CIK
  separately and learned the link later. The merge keeps as survivor the entity born from the
  component's birth alias, so every alias resolves to the UUID that a store which knew the
  link from the start minted. On staging data, a store that learned the N-PORT crosswalk one
  run later resolved all 360 legacy ids to the same UUIDs as a store loaded at once.

**What stays opaque.**

- The UUID is a name, not a key to recompute. Consumers resolve an alias with
  `entity_resolve`; they never rebuild an id from a CIK.
- A birth alias later found wrong is retracted, and the entity keeps its id. Corrections are
  new aliases and `same_as` or `superseded_by` edges, never a new id for the same entity.
- **The limit.** Evidence can arrive in a different order than its knowable-at times, for
  example an older filing captured late. A store that grows run by run can then hold an
  entity born from a different alias than a fresh store would pick. When both entities exist,
  the merge rule makes every alias resolve alike. When only the growing store's entity exists,
  the two stores keep different ids for that company, and a later `same_as` edge records the
  correspondence. Both stores still resolve every alias; only the UUID differs.

**Why not UUIDv7.** A random, time-ordered id would mint different UUIDs for one company in
staging, production and every replay. Staging and production exports could not be joined,
and the backfill could not be reproduced. The only thing v7 buys is index locality, which the
store's size does not need.

## 3. The alias store

### Scheme registry

`staging.entity_alias_schemes` records one row per scheme:

- `applies_to_kind`: which kind the scheme identifies.
- `is_unique`: whether at most one entity may hold a value at any valid instant.
- `value_reusable`: whether the authority can later give the value to another entity.
- `value_pattern`: the canonical form every stored value must match.
- `authority`: who issues the values.

| scheme | identifies | unique | reusable | canonical form |
|---|---|---|---|---|
| `legacy-id` | entity | yes | no | verbatim, case-sensitive |
| `lei` | organization | yes | no | 20 chars, upper case |
| `cik` | organization | yes | no | 10 digits, zero-padded |
| `cusip` | instrument | yes | yes | 9 chars, upper case |
| `isin` | instrument | yes | yes | 12 chars, upper case (US/CA embed the CUSIP) |
| `figi` | instrument | yes | no | 12 chars, upper case (share-class FIGI) |
| `mic-ticker` | listing | yes | yes | `XNAS:AAPL` |
| `sec-series` | fund | yes | no | `S000000000` |
| `moomoo-code` | listing | yes | yes | `US.AAPL` |
| `name` | entity | **no** | yes | trimmed text; labels only, never resolves |

`staging.entity_alias_normalize(scheme, value)` produces the canonical form. The resolver
applies it to its input, so `cik 320193` and `xnas:aapl` both resolve.

### Alias rows

`staging.entity_aliases` has one row per claim:

- **The claim:** `entity_id`, `scheme`, `value`.
- **Real-world validity:** `valid_from`, `valid_to`, as a half-open interval. `'-infinity'`
  means "for as long as the value exists".
- **Knowable-at:** `transaction_time`, taken from the evidence and never defaulted.
- **Audit:** `recorded_at`.
- **Provenance:** `source`, `raw_ref` and `evidence` (jsonb).
- **How the claim was made** (`method`):
  - `asserted`: a source named the entity with this value.
  - `parsed`: the value is spelled out inside another alias of the same entity.
  - `crosswalk`: stored evidence joins two schemes.
- **Also:** `confidence` and `mapping_version`.

For a scheme whose values are never reused, the backfill widens `valid_from` to `-infinity`.
For a reusable scheme, `valid_from` is the earliest valid date in the evidence.

An alias maps to exactly one entity, and an entity can have any number of aliases.

### Uniqueness, point in time

For a unique scheme, the insert guard (`staging.validate_entity_alias`) refuses a value when a
different entity already holds it over an overlapping validity.

The guard compares the two claims as they were known at the later of their two
`transaction_time`s. Retractions only ever shorten validity. So if the claims do not overlap
at that moment, they never overlap later, and before that moment at most one of them was
visible.

Two cases are allowed:

- **The same entity** holds the value twice. This is extra evidence, for example a parsed CIK
  and a crosswalk CIK.
- **Two entities that have already been merged** hold it. They compare equal through their
  survivor.

The resolver raises when two entities match. The guard prevents that, and raising is louder
than guessing.

### Ending, merging and splitting

- **Retraction.** A row in `staging.entity_retractions` (`alias_id` or `relation_id`,
  `valid_to`, `reason`, `transaction_time`) ends a claim.
  - A `valid_to` at or before the claim's `valid_from` withdraws it completely.
  - A replay "as known at" a time before the retraction still sees the claim.
  - This is how a reassigned ticker is handed over: retract the old claim at the handover
    date, then assert the new one from that date.
- **Merge.** When evidence shows that two entities are one thing, every entity except the
  survivor (defined below) gets a `same_as` edge and a `superseded_by` edge to the survivor.
  - Neither entity's aliases change.
  - `entity_survivor(entity, known_at)` follows `superseded_by` edges as known at that time.
    Before the merge is known, the two ids still resolve to two entities.
  - The guard refuses a second outgoing `superseded_by` edge and any edge that would close a
    cycle.
  - An unmerge is a retraction of the `superseded_by` edge, plus retractions of any alias
    that was attached to the survivor because of the merge.
- **Split.** From the split date:
  - the old entity's affected aliases are retracted with `valid_to` set to the split date;
  - the new entities get aliases valid from that date;
  - `split_into` edges record the lineage.

  History before the date keeps resolving to the old entity.
- **Every table is append-only.** A `reject_mutation` trigger refuses UPDATE and DELETE on all
  seven tables, including the registries. An entity must have its birth alias by commit, so no
  entity exists that nothing names (a deferred constraint trigger enforces this).
- **Merge survivor.** The survivor is the entity born from the component's birth alias.
  Otherwise it is the entity whose birth alias became knowable first. It is never chosen by
  mint time.

## 4. Resolver API

**SQL** (in the migration):

- `staging.entity_resolve(scheme, value, valid_at date, known_at timestamptz) → uuid`
  - Returns the entity that the alias named on `valid_at`, as known at `known_at`, after
    following merges.
  - Returns null when nothing matches.
  - Both times are required. A default of "now" would let a replay see the future.
- `staging.entity_survivor(entity_id, known_at) → uuid`: where an entity's identity continues.
- `staging.entity_alias_resolution`: every alias with its effective end and current survivor.
  Use it for set-based joins.
- `staging.entity_graph_nodes` and `staging.entity_graph_edges`: the export views (§8).

**Python** (PR-3, in `data_engine.datahub`):

- `resolve_coordinates(connection, listings, *, as_of)` returns `{listing: (issuer_uuid,
  instrument_uuid, listing_uuid, ticker)}` for any universe.
  - It resolves through the `legacy-id` or external aliases that the governed list carries.
  - On a miss it mints through `staging.entity_mint`, using the same birth rule as the
    backfill, and records the alias it resolved by.
- `alias_of(connection, entity_id, scheme, *, valid_at, known_at)` answers "which CIK does this
  issuer have" for the SEC adapter and the other consumers in §6.

## 5. How observations and payloads reference entities

**The normalized payload carries only the UUIDs.** From PR-3 on, `issuer_id`, `instrument_id`
and `listing_id` hold the entity UUIDs in canonical text form.

**The alias a capture used is lineage, and stays out of the payload.** An example is the CIK
sent to SEC or the ticker sent to Twelve Data. PR-3 records it in an append-only side table,
`staging.capture_entity_refs(observation_id, role, entity_id, scheme, value, known_at)`.
Keeping it out of the payload matters for two reasons:

- **The payload hash is the observation's identity.** TOPT (which knows AAPL by its LEI) and
  QQQ (which knows it by its CIK) must produce the same hash for the same vendor bar. If the
  alias were in the payload, the id-only duplicate groups #877 set out to stop would come
  back.
- **The side table still answers "which identifier did we send?"**, which makes the vendor
  call reproducible.

**Payloads written before PR-3 keep their legacy strings.** Every one of them is a
`legacy-id` alias, so `entity_resolve('legacy-id', payload->>'issuer_id', …)` maps old rows to
UUIDs without touching them.

## 6. Reuse, reports and consumers

- **Reuse (#635).** `_satisfy_from_recent_observations` compares payload UUIDs with plan
  UUIDs.
  - It does not resolve legacy payloads for reuse. A reused legacy observation would put
    legacy ids into a UUID snapshot, so the first tick after PR-3 fetches its cells once.
  - After that tick, QQQ and canary reuse TOPT's captures for shared listings.
  - PR-1's partition condition (H1) still applies.
- **Snapshots, core results, strategy inputs and decisions** carry UUIDs from the first tick
  after PR-3.
  - The run-scoped readers from PR-1 need no change.
  - A reader that compares a head from before the switch with one after it goes through
    `legacy-id` resolution.
- **Consumers that parse id prefixes today** switch to alias lookups in PR-4:
  - `sec_financial_adapter` (CIK, predecessor lookup);
  - `standards/planner`;
  - `confidence_report._CIK_ID`;
  - `theme_purity`;
  - `canary_oracles`, which has literal `issuer:cik:` ids;
  - `holdings_enrichment`, which also writes aliases directly from PR-5 on.
- **Mart.**
  - A new `mart.entity_identity` (`entity_id`, `kind`, current ticker, name, CIK, LEI)
    replaces the ticker parsing in `mart.entity_display_resolution`.
  - It also replaces the `'listing:xnas:' || ticker` construction in
    `mart.fund_holdings_valuation`.
  - `mart_readonly` reads it through view-owner permissions, so consumers never touch
    staging.
- **The e2e raw-id guard** (`walk-tree.mjs`) adds a UUID pattern. A UUID on a page is a raw
  id leak just like `issuer:cik:`.
- **Reports** keyed by listing or by run (quality, confidence, question coverage) compare
  across the switch unchanged.

## 7. The backfill

The backfill has its own migration, `20260917T0430_datahub_entity_backfill.sql`, separate
from the store it fills.

- `staging.entity_backfill_plan` is the whole plan as **one SELECT over tables that existed
  before either migration**, so it can run read-only against an environment before the
  deploy. A test pins that property.
- `staging.entity_backfill()` applies the plan, and the migration calls it on every boot.
  Until captures write UUIDs, each new legacy id gets an entity at the next boot.

**What counts as a use of an id:**

- every `(issuer, instrument, listing)` trio in a stored normalized observation payload;
- every trio in a published universe head (`universe-list:*` contract objects, which are the
  plane's governed corpus).

Snapshot members are frozen from those observations and add nothing. The packaged TOPT
corpus reaches the database only through its observations, and all 21 of its trios are
observed in both environments.

**What is attached:**

- each legacy id as a `legacy-id` alias;
- the typed id it spells out (`lei`, `cik`, `cusip`, `figi`, `mic-ticker`) as a `parsed`
  alias;
- CIK and ISIN from the crosswalk below;
- `issues` and `listed_as` edges from the trios.

**The cross-scheme proof, for issuers.**

- An N-PORT holding line carries the LEI and an ISIN.
- The knowledge graph resolves that ISIN to a `issuer:cik:` or `company:cik:` entity. It
  takes the newest identifier vintage and follows one `same_as` hop, exactly as
  `entity_resolution.resolve` does.
- Every line for the LEI must agree on one CIK, and no other LEI may prove the same CIK.

**The cross-scheme proof, for instruments.**

- A CUSIP-keyed trio and a FIGI-keyed trio name the same listing.
- Their issuers are the same proven component.
- The N-PORT line with that CUSIP resolves to the same CIK.
- There is one FIGI per CUSIP and one CUSIP per FIGI.

The ISIN from that line becomes an instrument alias. For US and CA ISINs, it must embed the
CUSIP.

**When no proof exists,** a shared listing still reveals that two instruments are one. The
backfill then holds back both `listed_as` edges (`conflict:listing-claimed-by-several-instruments`)
instead of giving the listing to either instrument. When the proof arrives later, the next run
merges the two instruments and writes the edge.

**What stops a component.** A component the registry refuses, or whose aliases already name
another kind, is rolled back on its own and reported in the returned summary. The boot never
crash-loops.

### Dry run, 2026-09-17

The plan SELECT was run read-only (`default_transaction_read_only=on`) on both environments.
On local copies of the relevant tables, the same SELECT and the full apply were run twice.
Two separate fresh copies of the production tables produced identical entity, relation and
alias sets.

| | production | staging |
|---|---|---|
| issuer / instrument / listing entities to mint | 111 / 112 / 112 | 111 / 112 / 112 |
| legacy ids (issuer / instrument / listing) | 123 / 125 / 112 | 123 / 125 / 112 |
| TOPT issuers linked to a plane CIK | **12 of 20** | **12 of 20** |
| TOPT instruments linked to a plane FIGI | **13 of 21** | **13 of 21** |
| TOPT issuers with a proven CIK but no plane counterpart | 0 | 7 |
| TOPT issuers with no proof | 8 | 1 (XOM) |
| aliases: legacy-id / cik / lei / cusip / figi / isin / mic-ticker | 360 / 115 / 20 / 34 / 104 / 13 / 112 | 360 / 122 / 20 / 34 / 104 / 20 / 112 |
| `issues` / `listed_as` edges | 112 / 112 | 112 / 112 |
| claims held back | 0 | 0 |
| plan SELECT time | 0.6–0.8 s | 2.2–3.0 s |
| second apply writes | nothing | nothing |

**Linked in both environments:** AAPL, AMZN, AVGO, COST, GOOG and GOOGL (one issuer, two
instruments), META, MSFT, MU, NFLX, NVDA, TSLA, WMT.

**Not linked in production:** ABBV, BRK.B, JNJ, JPM, LLY, MA, V and XOM.

- None of them is in QQQ, so no plane trio shares their listing.
- Production's N-PORT lane captures only QQQ, so no stored line proves their CIK.
- They still get entities with their LEI and CUSIP aliases.

Staging holds an S&P 500 fund's N-PORT (IVV, series S000004310), which proves CIKs for seven
of them: ABBV 1551152, BRK.B 1067983, JNJ 200406, JPM 19617, LLY 59478, MA 1141391 and
V 1403161. Six of those match the CIKs that theme purity's capture resolved independently
(#828).

XOM is unproven in both environments. Its ISIN resolves only to a minted `company:isin:`
node.

**Merges.** On a staging copy loaded without N-PORT data first, the backfill minted the LEI
and CIK entities separately and held back 26 `listed_as` claims. After the N-PORT and graph
rows were loaded, the next run added 25 `same_as` and `superseded_by` pairs and the 13 missing
edges, and changed no alias row.

## 8. Exports: graph database and embedding store

**Graph database.**

- Nodes come from `staging.entity_graph_nodes`:
  - `entity_id` is the node key;
  - labels come from the kind path (`issuer`, `organization`, `agent`, `entity`);
  - `aliases` holds the current alias list;
  - `survivor_entity_id` points to the survivor.
- Edges come from `staging.entity_graph_edges`:
  - `relation_id` is the edge key;
  - each edge carries its type, `valid_from`, `valid_to`, `transaction_time`, `confidence`,
    `source` and `attributes`;
  - identity edges are flagged.
- A loader upserts by UUID (`MERGE (n {id: $entity_id})`) and never deletes: a retracted
  edge leaves the view, and the loader closes it with `valid_to`.
- Point-in-time graph queries filter on `transaction_time` and validity, the same way the SQL
  resolver does.

**Embedding store.**

- The key is the survivor's entity UUID.
- A document is rendered from the current aliases (names, ticker@MIC, CIK, LEI), the kind and
  the first-hop relations. Filing chunks are keyed `(entity_uuid, chunk_id)`.
- Metadata carries `kind`, the alias-snapshot hash and the `known_at` of the render, so a
  backtest can filter out embeddings rendered after its cutoff.
- On a merge, the loser's vectors are re-keyed to the survivor. The loser's key stays as a
  redirect (`superseded_by`), so an old reference still resolves.
- Retrieval results are UUIDs. The mart identity view turns them into display text.

**Flexibility.** A new scheme, kind or relation type is a registry row. Relations carry
`attributes` jsonb for edge properties that are not identity, such as a supplier share or a
holding type.

## 9. Earlier decisions that still apply

- **D2: TOPT's eight unlinked listings.** Their entities exist. What they lack is a proven
  CIK, and a FIGI if a plane universe ever lists them.
  - (a) **Recommended now:** capture an S&P 500 fund's N-PORT (IVV) in production through the
    existing N-PORT lane and holdings enrichment. The backfill's existing proof then covers
    seven of the eight, as staging already shows, with no new resolution rule.
  - (b) The earlier option still works for FIGI: give each `UniverseSource` member its own
    MIC, normalize class tickers (BRK.B), and run an ids-only refresh through the SEC and
    OpenFIGI gateway. That evidence is ticker-based, so it needs its own proof rule: the
    shared listing plus the vendor's answer.
  - (c) Hand-pinning stays rejected.
- **D3: TOPT continuity** (unchanged, (a)). `UniverseRef`, `list_version_id` and the pointer
  lineage stay. The corpus file stays the filing record, and its LEI and CUSIP are aliases.
  The `e240…` pin and the #59 candidate stay valid.
- **D4: ExxonMobil, now an ontology question.** The TOPT LEI names the pre-reorganization
  parent (predecessor CIK 34088). Since the 2026 holdco reorganization, the listed shares
  belong to the holdco (index CIK 2115436).
  - The ontology answer is two issuer entities and a `successor_of` edge, with the owner-signed
    #496 row as its evidence. The XOM listing's `issues` and `listed_as` edges then carry the
    reorganization date.
  - Until the owner decides, `predecessor_ciks` resolves its `issuer:lei:J3WH…` key through
    the `legacy-id` alias (the earlier D4a), and the row is untouched.
  - An automatic N-PORT proof for XOM would link the LEI to whichever CIK OpenFIGI and SEC
    return today, so the backfill must not link XOM without that decision. It is unlinked
    today in both environments.

**Hazards from the earlier design:**

| hazard | status |
|---|---|
| H1 | fixed by #903 |
| H3 | fixed by #903 |
| H5 | fixed by #903; PR-4 adds the UUID pattern |
| H2 (XOM predecessor) | open, handled in PR-4 by D4 |
| H4 (strategy inputs read by cutoff) | open, handled in PR-4 |
| H6 (standards ticker map) | open, handled in PR-4 |

## 10. Plan

1. **PR-2 (this change).** The store, the registries, the resolver, the export views, the
   backfill run on every boot, and tests. No reader or capture changes.
2. **PR-3: captures write UUIDs.**
   - `resolve_coordinates` replaces the corpus trio in `plan_and_persist` for every universe.
     It mints on a miss and fails on an ambiguous alias.
   - The payload carries UUIDs, and `capture_entity_refs` records the alias each fetch used.
   - The SEC adapter and the standards planner read the CIK alias. The `issuer:lei:` branches
     become dead code and are removed.
   - Reuse compares UUIDs. Tests that pin payload hashes derive them instead of using
     literals.
   - Before it ships, D2(a) runs in production so that no TOPT issuer the SEC adapter needs
     lacks a CIK alias.
   - Tests go through `plan_and_persist`:
     - AAPL gets identical UUIDs from the TOPT and QQQ plans;
     - GOOG and GOOGL stay two instruments under one issuer;
     - a TOPT tick followed by a QQQ tick creates no new id-only duplicate group.
3. **PR-4: consumers read UUIDs.**
   - Add `mart.entity_identity`, and move `entity_display_resolution` and
     `fund_holdings_valuation` onto it.
   - Switch the readers in §6 to alias lookups: `confidence_report`, `theme_purity`,
     `canary_oracles`, and the predecessor lookup (D4).
   - Scope the strategy-input reader by run (H4) and the standards ticker map by head (H6).
   - Add the walk-tree UUID pattern.
   - Add a `mart` bridge from legacy ids to UUIDs for head-to-head comparisons across the
     switch.
4. **PR-5: the rest of the graph.**
   - `holdings_enrichment` and `nport_holdings` write aliases and `holds` edges for funds
     (`sec-series`).
   - Universes get entities and `member_of` edges.
   - The knowledge graph's `company:*` and `analyst:*` nodes become aliases, and
     `staging.kg_*` is frozen as evidence.
   - Add the graph and embedding export jobs.

## 11. Standing checks

| check | where it lives |
|---|---|
| All four issuer forms of AAPL (legacy LEI id, legacy CIK id, `lei`, `cik`) resolve to one UUID; all five instrument forms resolve to one UUID; GOOG and GOOGL are two instruments under one issuer; unlinked TOPT ids report why | `apps/data-engine/tests/test_entity_identity_store.py` |
| A second backfill writes nothing | same file |
| A later proof merges by adding edges, and every existing alias row is unchanged; the survivor is the id a store that knew the link from the start mints; "as known before", the ids still resolve apart | same file |
| A reassigned symbol resolves by valid date and by what was known; overlapping claimants are refused | same file |
| Every store table refuses UPDATE and DELETE; schemes, kinds and relation domains are enforced; an entity without a birth alias cannot commit | same file |
| The plan SELECT references no object either migration creates, so it can be dry-run before deploy | same file |
| The same evidence in two fresh databases mints identical entities and relations; every id equals Python's `uuid5` of its birth alias; a database that learned the crosswalk one run later resolves every legacy id to the same UUIDs | same file (builds and drops its own databases) |
| Entity and relation ids that are not derived from their claim are refused | same file |
| The migration re-applies over its own rows (the backfill runs on each pass) | `ci-db.yml` (three passes) |
