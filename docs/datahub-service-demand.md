# Sample-driven DataHub service demand

DataHub accepts durable service requests from factors, strategies, and research
modules. A request defines the data outcome and supplies representative evidence; it
does not select a vendor or describe infrastructure.

The public contract is `truealpha_contracts.service_demand.DataHubServiceDemand`.
The checked-in TOPT round-trip example is
`libs/contracts/tests/fixtures/datahub_service_demand.v1.json`.

## Request contents

An accepted request binds all of the following into one content address:

1. Exact requester ID, version, definition hash, and immutable universe.
2. Existing source-neutral `DataRequirement` identities.
3. Required normalized fields with definitions, value kinds, unit behavior,
   valid-time behavior, and knowable-time rules.
4. At least one content-hashed sample artifact and expected case. Every required field
   must have an exact, presence, absence, absolute-tolerance, or relative-tolerance
   assertion.
5. Coverage, availability, continuous-confidence, independent-origin, lineage,
   conflict, and quality-report objectives.
6. Exact downstream materialization definitions with non-overlap and
   exact-snapshot idempotency rules.

Sample paths are safe relative POSIX references. URLs, absolute paths, parent
traversal, provider IDs, credentials, hosts, buckets, databases, and mutable `latest`
references are not contract fields. Sample delivery transport is outside the demand
identity; the artifact SHA-256 is the byte identity.

## TOPT example

The fixture asks DataHub to serve the GPPE factor's `gross_profit` requirement. It
includes one JSON sample and asserts the expected Decimal-compatible value. The field
definition states the reporting-period meaning, unit comes from the record, and
knowability follows filing publication time.

Its service objective is:

- daily refresh and a two-day freshness maximum age, exactly matching the embedded
  `DataRequirement`;
- 100% denominator coverage and at least 95% availability;
- continuous confidence of at least 70 on the 0-100 presentation scale;
- the `high` target band, which requires at least two canonical original-source
  groups; mirrors or resellers of the same origin do not increase this count;
- daily reports retaining denominator, terminal state, coverage, availability,
  freshness, confidence, source composition, conflicts, lineage, retries, and
  unavailable reasons;
- GPPE recomputation only after an accepted exact snapshot, with no overlapping run
  and an exact snapshot-plus-definition idempotency key.

`evaluate_datahub_service_demand` converts untrusted input into an accepted demand or
a content-addressed rejection report with bounded reason codes. It never echoes the
rejected payload.

## Ownership boundary

The demand contract freezes what DataHub must provide. DataHub owns acquisition,
normalization, confidence evidence, quality reports, refresh, and downstream
recomputation behavior for the accepted request.

Issue #207 owns confidence formula calibration and persistence. The demand only pins
an exact confidence policy and target; it does not implement or silently tune the
formula.

TrueAlpha does not implement infrastructure through this contract. Runtime storage,
release, environment, and scheduling capabilities are requested later through a
released `infra2-sdk` contract. infra2 owns the implementation and operation of that
platform contract. Provider execution, Dagster activation, and Production promotion
also remain outside this contract-only slice.
