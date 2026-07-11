# TrueAlpha Vision Delivery Architecture
Status: proposed v1 delivery design; `init.md` remains authoritative. This document is
not frozen until the semantic closure gate in Section 18 passes.
## 1. Decision
Converge on this semantic pipeline before implementing more factor logic:
```text
RawCapture -> normalized PIT records -> ResearchSnapshot + DataSnapshotManifest
  -> FactorDefinition.compute -> FactorOutputBatch[Metric | Classification | Relationship]
  -> ScreenDefinition.evaluate -> StrategyDefinition -> BacktestRunResult
```
The proposal covers interface meaning, not every adapter; implementations may land incrementally
only behind the closure tests in Section 18. No issue may claim an interface freeze merely because
the signatures are written down.
`libs/contracts` owns data-only semantic types and ports. `libs/factors` owns every calculation,
classification, screen, ranking, portfolio rule, and return calculation.
`apps/data-engine` owns adapters, normalization, repositories, orchestration, and materialization.
Consumers read materialized outputs and only reformat them.
## 2. Goals and Non-Goals
Goals:
- Reproduce every result at an explicit transaction-time cutoff.
- Support metrics, categories, and graph relationships without reducing them to `Decimal`.
- Separate pipeline stages while keeping all business computation in `libs/factors`.
- Keep confidence mandatory and provenance-blind at the factor boundary.
- Include actions, membership, and identifier history in replay contracts.
- Turn issue #14 evidence into executable golden cases rather than coverage claims.
Non-goals:
- Exposing vendor schemas to factors or selecting formula thresholds here.
- Treating fixtures as evidence of performance or closing issue #14 prematurely.
## 3. Pre-Implementation Gaps
1. `PriceBar` lacks `confidence`; add it before a price repository is conformant.
2. The sample audit uses one global price span, not history per symbol. Gate on the
   minimum per-symbol span, including missing symbols.
3. `strategy_coverage.json` uses boolean-only evidence. `true` can assert a restatement,
   split, delisting, or replay case without naming artifacts or assertions. Readiness
   above toolchain level must use typed evidence records and executable tests.
The same closure must address `BacktestDataset` omitting actions, membership,
identifier history, and a manifest; `FactorResult` being unable to express categories
or relationships; and repository ports lacking one atomic snapshot operation.
## 4. Stable Semantic Model
### 4.1 Identity, Currency, and Market Vocabulary

An issuer is the reporting business, a security is a legal instrument, and a listing is
one venue/currency in which a security trades. These IDs are never interchangeable.
Fundamentals attach to issuers, holdings and corporate actions attach to securities, and
price bars attach to listings. The KG resolves the PIT links between them before factor
execution.

```python
IssuerId = NewType("IssuerId", str)
SecurityId = NewType("SecurityId", str)
ListingId = NewType("ListingId", str)
AnalystId = NewType("AnalystId", str)
UniverseId = NewType("UniverseId", str)
InputId = NewType("InputId", str)       # opaque semantic record identity, not provenance
OutputId = NewType("OutputId", str)
CurrencyCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]

class DataDomain(StrEnum):
    FINANCIAL_FACTS = "financial_facts"
    RELATIONSHIPS = "relationships"
    ANALYST_RATINGS = "analyst_ratings"
    FORECASTS = "forecasts"
    GUIDANCE = "guidance"
    FUND_HOLDINGS = "fund_holdings"
    SEGMENT_REVENUE = "segment_revenue"
    PRICE_BARS = "price_bars"
    FX_RATES = "fx_rates"
    CORPORATE_ACTIONS = "corporate_actions"
    UNIVERSE_MEMBERSHIPS = "universe_memberships"
    IDENTIFIERS = "identifiers"
    DOCUMENTS = "documents"
    EXTRACTIONS = "extractions"

class SubjectKind(StrEnum):
    ISSUER = "issuer"
    SECURITY = "security"
    LISTING = "listing"
    ANALYST = "analyst"
    UNIVERSE = "universe"
    THEME = "theme"

class SubjectRef(BaseModel):
    kind: SubjectKind
    id: str

class SecurityKind(StrEnum):
    COMMON_STOCK = "common_stock"
    ADR = "adr"
    ETF = "etf"
    FUND = "fund"
    DEBT = "debt"
    OPTION = "option"
    FUTURE = "future"
    SWAP = "swap"
    DERIVATIVE_OTHER = "derivative_other"
    CASH = "cash"
    CASH_EQUIVALENT = "cash_equivalent"
    OTHER = "other"

class HoldingSide(StrEnum):
    LONG = "long"
    SHORT = "short"

class IssuerSecurityLink(BaseModel):
    input_id: InputId
    issuer_id: IssuerId
    security_id: SecurityId
    security_kind: SecurityKind
    valid_from: date
    valid_to: date | None
    confidence: Decimal
    as_of: datetime

class SecurityListingLink(BaseModel):
    input_id: InputId
    security_id: SecurityId
    listing_id: ListingId
    exchange_mic: str
    ticker: str
    currency: CurrencyCode
    timezone: str
    valid_from: date
    valid_to: date | None
    confidence: Decimal
    as_of: datetime

class MoneyValue(BaseModel):
    amount: Decimal
    currency: CurrencyCode

class DateRange(BaseModel):
    start: date
    end: date
```

V1 either converts monetary inputs through explicit PIT FX observations or rejects a
cross-currency calculation. It never assumes that two bare monetary values share a
currency.

### 4.2 Time Vocabulary
- `valid_from` / `valid_to`: when a statement is true in the real world.
- `knowable_at`: earliest corroborated time TrueAlpha permits it in replay.
- `recorded_at`: ingestion time; never a substitute for knowability.
- `as_of`: inclusive transaction-time cutoff supplied by the caller.
- `valid_on`: optional real-world date for interval membership.
- `observed_at`: date or period to which an output applies.
Datetimes are aware; repositories require `knowable_at <= as_of`. Restatements append.
### 4.3 Factor-Ready Inputs
```python
class Fact(BaseModel):
    input_id: InputId
    subject: SubjectRef  # metric registry constrains issuer vs security
    metric: str
    value: Decimal | None
    unit: str | None
    currency: CurrencyCode | None
    confidence: Decimal  # 0..1
    as_of: datetime
    valid_from: date | None = None
    valid_to: date | None = None
    fiscal_period: str | None = None
```
`Fact` has no provenance. Reconciliation occurs first; the manifest preserves lineage.
```python
class FactorRelationshipInput(BaseModel):
    input_id: InputId; from_subject: SubjectRef; to_subject: SubjectRef; relation_type: str
    valid_from: date; valid_to: date | None; confidence: Decimal; as_of: datetime
class RatingCategory(StrEnum):
    STRONG_SELL = "strong_sell"; SELL = "sell"; HOLD = "hold"
    BUY = "buy"; STRONG_BUY = "strong_buy"
class RatingAction(StrEnum):
    INITIATE = "initiate"; REITERATE = "reiterate"
    UPGRADE = "upgrade"; DOWNGRADE = "downgrade"; RESUME = "resume"
class FactorRatingInput(BaseModel):
    input_id: InputId; analyst_id: AnalystId; covered_subject: SubjectRef
    recommendation_at: datetime; normalized_score: Decimal = Field(ge=-1, le=1)
    category: RatingCategory; rating_action: RatingAction; rating_scale_version: str
    target_price: MoneyValue | None
    vendor_updated_at: datetime | None; confidence: Decimal; as_of: datetime
class FactorForecastInput(BaseModel):
    input_id: InputId; issuer_id: IssuerId; target_metric: str
    target_period: str | None; horizon_start: date | None; horizon_end: date | None
    point_value: Decimal | None; lower_bound: Decimal | None; upper_bound: Decimal | None
    unit: str; currency: CurrencyCode | None; statistic: Literal["mean", "median"]
    constituent_count: int | None; confidence: Decimal; as_of: datetime
class FactorGuidanceInput(BaseModel):
    input_id: InputId; issuer_id: IssuerId; target_metric: str
    target_period: str | None; horizon_start: date | None; horizon_end: date | None
    point_value: Decimal | None; lower_bound: Decimal | None; upper_bound: Decimal | None
    unit: str; currency: CurrencyCode | None
    guidance_status: Literal["issued", "updated", "withdrawn"]
    confidence: Decimal; as_of: datetime
class FactorHoldingInput(BaseModel):
    input_id: InputId; fund_security_id: SecurityId
    holding_security_id: SecurityId | None; holding_issuer_id: IssuerId | None
    unresolved_holding_key: str | None; instrument_kind: SecurityKind
    report_period: date; side: HoldingSide; weight: Decimal = Field(ge=0)
    market_value: MoneyValue | None
    notional_value: MoneyValue | None; delta: Decimal | None
    confidence: Decimal; as_of: datetime

    @model_validator(mode="after")
    def require_resolved_subject_or_unresolved_key(self) -> Self: ...
class FactorSegmentRevenueInput(BaseModel):
    input_id: InputId; issuer_id: IssuerId; segment_name: str
    revenue: MoneyValue; fiscal_period: str; confidence: Decimal; as_of: datetime
class FactorPriceInput(BaseModel):
    input_id: InputId; listing_id: ListingId; security_id: SecurityId
    trading_date: date; exchange_mic: str; timezone: str; currency: CurrencyCode
    open: Decimal; high: Decimal; low: Decimal; close: Decimal; volume: int
    confidence: Decimal; as_of: datetime
class FactorActionInput(BaseModel):
    input_id: InputId; action_id: str; security_id: SecurityId
    action_type: CorporateActionType; announced_at: datetime | None
    ex_date: date; effective_date: date; pay_date: date | None
    ratio: Decimal | None; cash_amount: MoneyValue | None
    resulting_security_id: SecurityId | None; confidence: Decimal; as_of: datetime
class FactorMembershipInput(BaseModel):
    input_id: InputId; universe_id: UniverseId; subject: SubjectRef
    valid_from: date; valid_to: date | None; confidence: Decimal; as_of: datetime
class FactorFxInput(BaseModel):
    input_id: InputId; base_currency: CurrencyCode; quote_currency: CurrencyCode
    observed_at: datetime; rate: Decimal; confidence: Decimal; as_of: datetime
class FactorDocumentInput(BaseModel):
    input_id: InputId; issuer_id: IssuerId | None; security_id: SecurityId | None
    document_type: str; text: str; published_at: datetime
    confidence: Decimal; as_of: datetime
class FactorInputBundle(BaseModel):
    snapshot_id: str
    as_of: datetime
    facts: tuple[Fact, ...] = ()
    relationships: tuple[FactorRelationshipInput, ...] = ()
    ratings: tuple[FactorRatingInput, ...] = ()
    forecasts: tuple[FactorForecastInput, ...] = ()
    guidance: tuple[FactorGuidanceInput, ...] = ()
    holdings: tuple[FactorHoldingInput, ...] = ()
    segment_revenue: tuple[FactorSegmentRevenueInput, ...] = ()
    prices: tuple[FactorPriceInput, ...] = ()
    actions: tuple[FactorActionInput, ...] = ()
    memberships: tuple[FactorMembershipInput, ...] = ()
    fx_rates: tuple[FactorFxInput, ...] = ()
    issuer_security_links: tuple[IssuerSecurityLink, ...] = ()
    security_listing_links: tuple[SecurityListingLink, ...] = ()
```
The factor-computation inputs expose only semantic values, valid period, confidence, and `as_of`.
They never contain `source`, `raw_ref`, accession, or repository metadata. The runner
projects this bundle from an auditable snapshot for the factor's declared domains.
Documents take a separate extraction path defined in Section 14.2 and never enter a
factor-computation bundle.
### 4.4 Corporate Actions
```python
class CorporateActionType(StrEnum):
    SPLIT = "split"
    CASH_DIVIDEND = "cash_dividend"
    STOCK_DIVIDEND = "stock_dividend"
    SPINOFF = "spinoff"
    DELISTING = "delisting"
class CorporateAction(BaseModel):
    action_id: str
    security_id: SecurityId
    action_type: CorporateActionType
    announced_at: datetime | None
    ex_date: date
    effective_date: date
    pay_date: date | None
    ratio: Decimal | None
    cash_amount: MoneyValue | None
    resulting_security_id: SecurityId | None
    knowable_at: datetime
    recorded_at: datetime
    confidence: Decimal
    raw_ref: str
```
Validation requires a ratio for ratio actions and amount/currency for cash dividends.
Corrections append vintages.
### 4.5 Membership and Identifier History
```python
class UniverseMembership(BaseModel):
    membership_id: str
    universe_id: UniverseId
    subject: SubjectRef
    valid_from: date
    valid_to: date | None
    knowable_at: datetime
    recorded_at: datetime
    confidence: Decimal
    raw_ref: str
class EntityIdentifier(BaseModel):
    identifier_id: str
    subject: SubjectRef
    source: DataSource
    identifier_type: str
    value: str
    valid_from: date
    valid_to: date | None
    knowable_at: datetime
    recorded_at: datetime
    confidence: Decimal
    raw_ref: str
```
Open-ended `valid_to` means active until superseded. Symbol changes and delistings are
history, not company mutations. Replay selects membership as knowable at each rebalance,
not today's constituents.
### 4.6 Snapshot, Selection Manifest, and Durable Store
```python
class SnapshotScope(BaseModel):
    issuer_ids: tuple[IssuerId, ...] = ()
    security_ids: tuple[SecurityId, ...] = ()
    listing_ids: tuple[ListingId, ...] = ()
    universe_id: UniverseId | None = None

    @model_validator(mode="after")
    def require_one_scope_mode(self) -> Self: ...

class SnapshotRequest(BaseModel):
    as_of: datetime
    valid_on: date
    scope: SnapshotScope
    domains: frozenset[DataDomain]
    price_window: DateRange | None = None

class SelectionPolicyVersions(BaseModel):
    contract_version: str
    fusion_ruleset_version: int
    identifier_resolution_version: str
    membership_resolution_version: str
    instrument_resolution_version: str
    metric_registry_version: str
    domain_selection_versions: dict[DataDomain, str]

    def validate_covers(self, domains: frozenset[DataDomain]) -> None: ...

class SnapshotRecordRef(BaseModel):
    domain: DataDomain
    record_id: str
    source: DataSource
    semantic_sha256: str
    raw_ref: str
    raw_sha256: str
    mapping_version: str | None
    accession: str | None
    knowable_at: datetime
    extraction_id: str | None
    source_evidence_status: SourceEvidenceStatus

class ManifestEntry(BaseModel):
    domain: str
    record_count: int
    content_sha256: str
    min_knowable_at: datetime | None
    max_knowable_at: datetime | None
    raw_refs_sha256: str
class DataSnapshotManifest(BaseModel):
    snapshot_id: str
    request: SnapshotRequest
    policy_versions: SelectionPolicyVersions
    created_at: datetime
    repository_kind: str
    entries: tuple[ManifestEntry, ...]
    selected_records: tuple[SnapshotRecordRef, ...]
    selected_membership_ids: tuple[str, ...]
    content_sha256: str
class ResearchSnapshot(BaseModel):
    manifest: DataSnapshotManifest
    financial_facts: tuple[CanonicalFinancialFact, ...] = ()
    graph_edges: tuple[GraphEdgeRecord, ...] = ()
    analyst_ratings: tuple[AnalystRatingRecord, ...] = ()
    forecasts: tuple[ForecastRecord, ...] = ()
    guidance: tuple[GuidanceRecord, ...] = ()
    fund_holdings: tuple[FundHoldingRecord, ...] = ()
    segment_revenue: tuple[SegmentRevenueRecord, ...] = ()
    price_bars: tuple[PriceBarRecord, ...] = ()
    fx_rates: tuple[FxRateRecord, ...] = ()
    corporate_actions: tuple[CorporateActionRecord, ...] = ()
    universe_memberships: tuple[UniverseMembershipRecord, ...] = ()
    identifiers: tuple[EntityIdentifierRecord, ...] = ()
    source_documents: tuple[SourceDocumentRecord, ...] = ()
    extraction_records: tuple[ExtractionRecord, ...] = ()
    issuer_security_links: tuple[IssuerSecurityLink, ...] = ()
    security_listing_links: tuple[SecurityListingLink, ...] = ()

class ResearchSnapshotRepository(Protocol):
    def build_snapshot(self, request: SnapshotRequest) -> ResearchSnapshot: ...

class SnapshotStore(Protocol):
    def put(self, snapshot: ResearchSnapshot) -> PutResult: ...
    def get(self, snapshot_id: str) -> ResearchSnapshot | None: ...
```
`build_snapshot` resolves PIT universe membership first, then issuer/security/listing links
and every requested domain in one transaction. Callers do not need today's entity list in
order to request a historical universe. `snapshot_id` derives from canonical request,
policy versions, selected record identities, and content hashes, not creation time.
`repository_kind` is diagnostic and cannot affect factors. `raw_refs_sha256` commits to
lineage without exposing it to factor branches. `selected_records` makes that commitment
recoverable rather than merely aggregate: an old run still resolves the exact staging row,
mapping, raw object, and fusion policy after rules change. For every selected extracted
semantic row, snapshot construction also includes its immutable `ExtractionRecord` and
source-document record as transitive lineage; those records never enter
`FactorInputBundle`. A snapshot is immutable,
persisted before factor execution, canonically sorted, consistent with its manifest, and
replaces the narrower `BacktestDataset` role. Loaders may select domains, but the manifest
lists exactly what was included.
`domain_selection_versions` must cover every requested domain plus transitive identity,
document, and extraction lineage. This includes analyst public-availability rules, fund
holding selection, segment/relationship validity, price/FX bar policy, corporate actions,
and documents; no domain may silently inherit a generic latest-row rule.
## 5. Typed Factor Outputs
```python
class ConsumedInputLineage(BaseModel):
    input_ids: tuple[InputId, ...] = ()
    upstream_output_ids: tuple[OutputId, ...] = ()

class AvailabilityStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    EXCLUDED = "excluded"
    LOW_CONFIDENCE = "low_confidence"
    ERROR = "error"

class SourceEvidenceStatus(StrEnum):
    CORROBORATED = "corroborated"
    UNCORROBORATED = "uncorroborated"
    SYNTHETIC = "synthetic"

class FactorValidationStatus(StrEnum):
    UNVALIDATED = "unvalidated"
    GOLDEN_PASSED = "golden_passed"
    HOLDOUT_PASSED = "holdout_passed"

class OutputBase(BaseModel):
    output_id: OutputId
    output_type: str
    invocation_alias: str
    invocation_id: str  # deterministic SHA-256 identity of the resolved execution
    factor_id: str
    factor_version: str
    subject: SubjectRef
    as_of: datetime
    valid_from: date | None
    valid_to: date | None
    fiscal_period: str | None
    confidence: Decimal
    availability_status: AvailabilityStatus
    source_evidence_status: SourceEvidenceStatus
    factor_validation_status: FactorValidationStatus
    factor_validation_record_id: str
    status_reasons: tuple[str, ...] = ()
    consumed: ConsumedInputLineage
    flags: tuple[str, ...] = ()
class MetricObservation(OutputBase):
    output_type: Literal["metric"] = "metric"
    metric: str
    value: Decimal | None
    unit: str
class ClassificationObservation(OutputBase):
    output_type: Literal["classification"] = "classification"
    taxonomy: str
    label: str
    score: Decimal | None = None
class RelationshipObservation(OutputBase):
    output_type: Literal["relationship"] = "relationship"
    to_subject: SubjectRef
    relation_type: str
    valid_from: date | None
    valid_to: date | None
    strength: Decimal | None = None
class ScenarioExposureObservation(OutputBase):
    output_type: Literal["scenario_exposure"] = "scenario_exposure"
    scenario_id: str
    scenario_version: str
    direction: Literal["positive", "negative", "mixed", "unknown"]
    exposure_value: Decimal | None
    unit: str
    path_count: int
FactorObservation = Annotated[
    MetricObservation | ClassificationObservation | RelationshipObservation
    | ScenarioExposureObservation,
    Field(discriminator="output_type"),
]
class FactorOutputBatch(BaseModel):
    batch_id: str
    invocation_alias: str
    invocation_id: str
    parameters_sha256: str
    parameters: dict[str, JsonValue]
    factor_id: str
    factor_version: str
    snapshot_id: str
    as_of: datetime
    subjects: tuple[SubjectRef, ...]
    outputs: tuple[FactorObservation, ...]
    batch_confidence: Decimal
    flags: tuple[str, ...] = ()
```
Availability describes whether a result is usable at this cutoff. Source evidence says
whether the consumed data is independently corroborated or synthetic. Factor validation
says whether this exact factor/version passed its golden and sealed-holdout oracle. All
three are orthogonal: an output can be available on corroborated data while its formula is
still unvalidated, or holdout-validated but stale at the requested cutoff.
The union is top-level so schemas reliably emit `oneOf`. Input IDs are opaque semantic
identities: factors may copy them into `consumed`, but cannot inspect source or raw
lineage. `run_factor` rejects unknown references and verifies each output confidence
against exactly the inputs and upstream outputs it declares; module conformance tests
prove that domain input selectors cannot return values without also returning their IDs.
`batch_confidence` is the minimum confidence of consumed inputs and outputs unless a
stricter declared policy applies. An empty batch needs an explanatory flag. PEG emits metrics, three-tier emits
a classification, supply chain emits relationships, and ETF virtual company emits a
multi-metric batch.
## 6. Factor, Screen, Strategy, Backtest
- A **factor** transforms a snapshot or prior batches into versioned observations.
  Base factors read a sanitized input bundle; composites read same-cutoff outputs.
- A **screen** filters, ranks, or tags entities from factor outputs. It is business
  computation and remains in `libs/factors`, never the App.
- A **strategy** declaratively connects a universe, schedule, screen, sizing rule,
  holding rule, and return convention. Serializable definitions live in contracts;
  rule implementations live in `libs/factors`.
- A **backtest** replays a strategy over historical cutoffs. Orchestration requests
  snapshots and records results, while all calculations delegate to `libs/factors`.
Module 7 remains a composite factor because its label is reusable. Pure-blood may expose
a theme-share metric plus a rank/filter screen. Analyst scoring is a factor; analyst
selection is a screen.
## 7. Concrete Invocation Contracts
Section 14 is the proposed signature set. It becomes authoritative only after Section 18's
semantic closure gate passes. The invocation rules are:

- base factors reject undeclared upstream batches;
- composites require every declared batch to share `snapshot_id` and `as_of`;
- screens return ranked and explicitly rejected entities;
- every invocation has a caller-chosen stable alias and a deterministic resolved ID over
  factor version, parameters, snapshot, dependency batch IDs, and subject scope;
- persisted definitions contain versioned registry invocations and immutable parameters,
  never callables;
- factor, screen, strategy, and rule versions make historical runs reconstructible.

## 8. Repository Ports
```python
class NormalizedRecordRepository(Protocol):
    def append(self, batch: NormalizedBatch) -> MaterializationResult: ...
    def candidates(self, request: SnapshotRequest) -> tuple[NormalizedRecord, ...]: ...

class MembershipRepository(Protocol):
    def resolve(
        self,
        universe_id: UniverseId,
        *,
        valid_on: date,
        as_of: datetime,
        policy_version: str,
    ) -> tuple[UniverseMembership, ...]: ...
```
Fine-grained domain methods may remain private helpers. Atomic `build_snapshot` plus
durable `SnapshotStore.put` is the public factor/backtest read boundary, preventing domains
from using subtly different cutoffs and preserving exact historical selection. Section
14.11 defines the proposed output, strategy-run, mart, and consumer read ports.
## 9. Module Boundaries
```text
libs/contracts/
  semantic.py     PIT DTOs and time vocabulary
  snapshots.py    query, manifest, snapshot, canonical hashing
  outputs.py      discriminated output union and batches
  strategy.py     screen/strategy/backtest data definitions and results
  ports.py        storage protocols only
libs/factors/
  base/           modules 1-6 snapshot-to-output calculations
  composite/      module 7 and output-to-output calculations
  screens/        filtering and ranking business rules
  strategy/       sizing, rebalance, holding, return, and cost calculations
  backtest/       deterministic replay using injected snapshots
  shared/         confidence, extraction, entity-resolution helpers
  registry.py     versioned factor, screen, and rule registrations
apps/data-engine/
  sources/        external calls and immutable captures
  normalization/  vendor schema to normalized PIT records
  repositories/   fixture and Postgres implementations
  assets/         load, invoke, persist; no duplicated computation
  quality/        evidence manifests and readiness audits
```
Folders are ownership guidance and appear only when needed; do not create empty
scaffolding.
## 10. Call Graph
```text
Dagster asset / local runner
  -> registry.resolve(invocation.factor_id, invocation.factor_version)
  -> snapshot_repository.build_snapshot(request)
     -> resolve PIT membership, issuer/security/listing links, and eligible vintages
     -> reconcile sources, assign confidence, build canonical manifest
  -> snapshot_store.put(snapshot)
  -> project_factor_inputs(snapshot, declared_domains)  # strips all provenance
  -> factor.compute(inputs, subjects, parameters, upstream)
  -> output_repository.put(batch)
  -> materialize deterministic mart projection
  -> for a composite: reload declared upstream batches from the materialized mart boundary
     -> factor.compute(inputs, subjects, parameters, materialized_upstream)
     -> output_repository.put(batch) -> materialize composite projection
Backtest runner
  -> resolve definition registry IDs
  -> for each scheduled cutoff
     -> build and persist one decision snapshot
     -> compute/load factor batches -> evaluate screen -> portfolio decision
     -> advance simulation clock; consume execution bars, FX, and actions only as available
     -> apply lag, prices, costs, and corporate actions without exposing outcomes to factors
  -> calculate metrics in libs/factors -> persist BacktestRunResult
```
Consumers never invoke sources, choose vintages, join factors into new metrics, or
reproduce strategy rules.
## 11. Invariants
Point-in-time:
- One snapshot has one inclusive `as_of`; no item has `knowable_at > as_of`.
- Apply transaction-time eligibility before `valid_on` interval filtering.
- Older cutoffs reproduce pre-restatement views.
- Composite inputs share snapshot ID and cutoff with their output.
- Membership, identifiers, ratings, holdings, edges, actions, and prices obey PIT rules.
- A price convention cannot expose a future action; total return declares action timing.
- Forward execution and outcome events are never members of the decision snapshot. The
  simulator observes them only after its clock reaches each event's availability time.
Confidence:
- Every staging row, including `PriceBar`, and every factor input/output has `[0, 1]`.
- Composite confidence cannot exceed the minimum consumed confidence.
- Missing input creates an error or explicit flag, never an invented zero.
- Thresholds are versioned parameters, not hidden constants.
- `availability_status`, `source_evidence_status`, and `factor_validation_status` report
  current usability, input corroboration, and formula validation respectively; none may be
  inferred from another or collapsed into one boolean.
Identity, units, and markets:
- Issuer, security, and listing IDs are distinct; no implicit ticker-to-company coercion.
- Monetary arithmetic requires matching currencies or a consumed PIT FX input.
- Execution uses an exchange calendar/timezone and unadjusted listing bars; adjusted
  history cannot be combined with explicit corporate-action events.
Provenance and determinism:
- Normalized records retain `raw_ref`; manifests commit to the lineage set.
- Only `FactorInputBundle`, never `ResearchSnapshot`, crosses into factor code.
- Bundle records contain no source, raw reference, accession, or repository metadata.
- Factors cannot access or branch on provenance; projection tests enforce this boundary.
- Invocation hash plus snapshot ID and per-output consumption references recover output
  lineage; factor version alone is insufficient when parameters differ.
- Canonical ordering and Decimal serialization define content hashes.
- Identical definition, version, parameters, and snapshot produce identical output.
- LLM extraction becomes versioned normalized input; replay never invokes it implicitly.
## 12. Repository Contract Test Kit
`libs/contracts` should expose a pytest suite factory accepting a repository constructor.
Run the same cases against fixture and ephemeral Postgres implementations:
- Reject naive datetimes and records newer than `as_of`.
- Return original and restated vintages at their respective cutoffs.
- Resolve PIT membership, symbol changes, and delisted historical entities.
- Return split/dividend actions only at permitted cutoffs.
- Require price confidence, exact Decimals, and complete per-symbol windows.
- Produce identical canonical snapshot hashes across implementations.
- Change hashes when value, confidence, or lineage changes.
- Change hashes when fusion, identifier, membership, or action-policy versions change,
  while retaining exact older snapshots through `SnapshotStore.get`.
- Reject manifests whose counts or hashes disagree with snapshots.
- Keep source and raw references out of factor-ready facts.
- Reject composite batches with mismatched snapshots or future timestamps.
- Enforce minimum-confidence propagation.
- Keep two parameterizations and subject scopes of one factor/version collision-free.
- Reject issuer/security/listing coercion, cross-currency arithmetic without PIT FX,
  calendar-day execution, and adjusted-close plus explicit-action returns.
- Keep future execution bars out of decision snapshots and apply each market event once.
- Prove idempotent writes by semantic ID and append-only vintages.
The kit tests behavior, not SQL. Postgres query-plan tests stay in data-engine.
## 13. Issue #14 Fixture and Evidence Integration
Issue #14 should produce immutable artifacts and an evidence manifest, not broad sampling:
```python
class EvidenceCase(BaseModel):
    evidence_id: str
    requirement_id: str
    artifact_paths: tuple[str, ...]
    artifact_sha256: tuple[str, ...]
    subjects: tuple[SubjectRef, ...]
    observed_valid_range: DateRange | None
    corroborated_knowable_at: datetime | None
    assertion_ids: tuple[str, ...]
    notes: str
```
Evolve coverage booleans into evidence references, for example
`"restatement_golden_pair": ["evidence.sec.restatement.001"]`. The audit verifies
references, artifact hashes, and executable assertions.
Targeted evidence:
- Original/restated filing pair with before/after snapshot assertions.
- Split and cash dividend with price, position, and total-return assertions.
- Historical membership plus one symbol change or delisting.
- Analyst event with externally corroborated public availability.
- Supply-chain edges with validity, knowability, confidence, and raw excerpts.
- Composite replay with same-cutoff and minimum-confidence assertions.
- Three years per local-backtest symbol and five per evaluation symbol.
- Primary/fallback price overlap with documented tolerances.
- Cross-industry companies, including financial and traditional issuers.
- Company-guidance growth and financial-industry gross-profit semantic cases.
Synthetic fixtures may unblock interface tests only when labeled `synthetic`; they do
not satisfy real-data readiness. Toolchain and strategy evidence remain distinct.
## 14. Proposed V1 Public API

The signatures below are the candidate implementation boundary for the complete Vision.
Modules may add private helpers, but adapters, assets, factors, consumers, and tests must
converge on these APIs before the Section 18 gate can freeze them. Parameter models are
immutable and serialized with every output.

### 14.1 Shared Execution Types

```python
JsonScalar = None | bool | int | str | Decimal | date | datetime
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

class PutResult(BaseModel):
    semantic_id: str
    inserted: bool

class MaterializationResult(BaseModel):
    materialization_id: str
    inserted_rows: int
    existing_rows: int
    content_sha256: str

class FactorExecutionContext(BaseModel):
    contract_version: str
    invocation_alias: str
    invocation_id: str
    parameters_sha256: str
    factor_id: str
    factor_version: str
    snapshot_id: str
    as_of: datetime
    subjects: tuple[SubjectRef, ...]

class FactorParameters(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

P = TypeVar("P", bound=FactorParameters)
I = TypeVar("I", bound=BaseModel)

class SelectedInputs(BaseModel, Generic[I]):
    records: tuple[I, ...]
    input_ids: tuple[InputId, ...]

class FactorInputView(Protocol):
    snapshot_id: str
    as_of: datetime
    def select(
        self,
        record_type: type[I],
        *,
        where: Mapping[str, JsonScalar],
    ) -> SelectedInputs[I]: ...

class FactorInvocation(BaseModel):
    invocation_alias: str  # stable name within a strategy/report definition
    factor_id: str
    factor_version: str
    parameters: dict[str, JsonValue]
    dependencies: dict[str, str] = Field(default_factory=dict)  # slot -> invocation_alias

class RuleInvocation(BaseModel):
    rule_id: str
    rule_version: str
    parameters: dict[str, JsonValue] = Field(default_factory=dict)

class ArtifactRole(StrEnum):
    DATA_ENGINE_DAGSTER = "data_engine_dagster"
    LLM_SERVICE = "llm_service"
    APP_WEB = "app_web"
    DB_MIGRATIONS = "db_migrations"

class ReleaseArtifact(BaseModel):
    role: ArtifactRole
    image_or_bundle: str
    digest: str
    git_sha: str
    sbom_sha256: str
    signature_ref: str

class SourceCoverageEntry(BaseModel):
    requirement_id: str
    module_id: str
    domain: DataDomain
    field_semantics: str
    primary_source: DataSource
    fallback_sources: tuple[DataSource, ...]
    knowability_basis: Literal["source_publication", "independently_corroborated"]
    history_start: date
    history_end: date | None
    expected_cadence: timedelta
    raw_storage_rights: Literal["permitted", "restricted", "unverified", "expired"]
    derived_output_rights: Literal["permitted", "restricted", "unverified", "expired"]
    rendered_excerpt_rights: Literal["permitted", "restricted", "unverified", "expired"]
    retention_policy_ref: str
    quota_policy_ref: str
    expected_cost: MoneyValue | None
    capture_adapter_id: str
    revision_policy_ref: str
    fallback_policy_ref: str
    owner: str
    review_expires_at: datetime
    evidence_ids: tuple[str, ...]

class SourceCoverageCatalog(BaseModel):
    source_coverage_catalog_id: str
    entries: tuple[SourceCoverageEntry, ...]
    content_sha256: str

class ModuleSlo(BaseModel):
    module_id: str
    universe_id: UniverseId
    universe_version: str
    applicability_policy_ref: str
    required_catalog_aliases: tuple[str, ...]
    minimum_usable_coverage: Decimal = Field(ge=0, le=1)
    maximum_freshness_age: timedelta
    maximum_unavailable_rate: Decimal = Field(ge=0, le=1)
    maximum_stale_rate: Decimal = Field(ge=0, le=1)
    maximum_unresolved_rate: Decimal = Field(ge=0, le=1)
    maximum_unclassified_rate: Decimal = Field(ge=0, le=1)
    maximum_low_confidence_rate: Decimal = Field(ge=0, le=1)
    maximum_error_rate: Decimal = Field(ge=0, le=1)
    minimum_trace_complete_rate: Decimal = Field(ge=0, le=1)
    minimum_soak_duration: timedelta
    minimum_natural_source_updates: int = Field(ge=1)
    alert_after: timedelta
    owner: str
    runbook_ref: str

class ConsumerSlo(BaseModel):
    surface: Literal["mcp", "app", "chat", "report", "card"]
    maximum_latency: timedelta
    maximum_error_rate: Decimal = Field(ge=0, le=1)
    minimum_trace_complete_rate: Decimal = Field(ge=0, le=1)
    maximum_rows: int = Field(ge=1)
    owner: str
    runbook_ref: str

class SloCatalog(BaseModel):
    slo_catalog_id: str
    modules: tuple[ModuleSlo, ...]
    consumers: tuple[ConsumerSlo, ...]
    content_sha256: str

class SloObservation(BaseModel):
    module_id: str
    subject: SubjectRef
    applicable: bool
    availability_status: AvailabilityStatus
    observed_at: datetime | None
    evaluated_at: datetime
    unresolved: bool
    unclassified: bool
    trace_complete: bool
    natural_source_update_id: str | None

class ConsumerSloObservation(BaseModel):
    surface: Literal["mcp", "app", "chat", "report", "card"]
    request_id: str
    latency: timedelta
    errored: bool
    row_count: int
    trace_complete: bool
    evaluated_at: datetime

class ReadinessCheck(BaseModel):
    check_id: str
    passed: bool
    observed: JsonValue
    required: JsonValue
    evidence_ids: tuple[str, ...]

class ReadinessReport(BaseModel):
    report_id: str
    evaluated_at: datetime
    input_content_sha256: tuple[str, ...]
    checks: tuple[ReadinessCheck, ...]
    ready: bool
    content_sha256: str

class SourceCoverageEvaluator(Protocol):
    def evaluate(
        self,
        catalog: SourceCoverageCatalog,
        *,
        as_of: datetime,
        evidence: Mapping[str, EvidenceCase],
    ) -> ReadinessReport: ...

class SloEvaluator(Protocol):
    def evaluate(
        self,
        catalog: SloCatalog,
        *,
        window: DateRange,
        observations: Sequence[SloObservation],
        consumer_observations: Sequence[ConsumerSloObservation],
    ) -> ReadinessReport: ...

class ReleaseManifest(BaseModel):
    release_manifest_id: str
    contract_version: str
    mart_schema_version: str
    research_catalog_id: str
    research_catalog_sha256: str
    universe_version: str
    source_coverage_catalog_id: str
    source_coverage_catalog_sha256: str
    slo_catalog_id: str
    slo_catalog_sha256: str
    configuration_sha256: dict[str, str]
    migration_ids: tuple[str, ...]
    migration_set_sha256: str
    artifacts: tuple[ReleaseArtifact, ...]
    accepted_evidence_ids: tuple[str, ...]
    accepted_readiness_report_sha256: tuple[str, ...]
    created_at: datetime
    manifest_sha256: str
    manifest_signature_ref: str

class ReleaseManifestRepository(Protocol):
    def put(self, manifest: ReleaseManifest) -> PutResult: ...
    def get(self, release_manifest_id: str) -> ReleaseManifest | None: ...

class ResolvedFactorInvocation(BaseModel):
    invocation: FactorInvocation
    definition_sha256: str
    parameters_sha256: str
    invocation_id: str  # SHA-256 over definition, params, snapshot, subjects, dependencies
    subjects: tuple[SubjectRef, ...]

class FactorDefinition(Protocol, Generic[P]):
    factor_id: str
    factor_version: str
    kind: Literal["base", "composite"]
    parameters_model: type[P]
    required_domains: frozenset[DataDomain]
    required_dependencies: Mapping[str, str]  # dependency slot -> required factor_id
    validation_status: FactorValidationStatus
    validation_record_id: str
    oracle_version: str

    def compute(
        self,
        context: FactorExecutionContext,
        inputs: FactorInputView,
        parameters: P,
        upstream: Mapping[str, FactorOutputBatch],  # dependency slot -> batch
    ) -> FactorOutputBatch: ...

def project_factor_inputs(
    snapshot: ResearchSnapshot,
    *,
    domains: frozenset[DataDomain],
    subjects: Sequence[SubjectRef],
) -> FactorInputBundle: ...

def run_factor(
    definition: FactorDefinition[P],
    *,
    invocation: FactorInvocation,
    snapshot: ResearchSnapshot,
    subjects: Sequence[SubjectRef],
    upstream: Mapping[str, FactorOutputBatch] | None = None,
) -> FactorOutputBatch: ...

class FactorRegistry(Protocol):
    def resolve(self, factor_id: str, factor_version: str) -> FactorDefinition[Any]: ...
```

`ReadinessReport.ready` is the deterministic conjunction of its checks; evaluators expose
no override/flag setter. A release binds exact source-coverage, SLO, research catalog,
universe, configuration, migration, artifact, evidence, and accepted readiness hashes.

`run_factor` owns generic validation: definition/parameter compatibility, declared
domains, entity scope, one snapshot/cutoff, upstream dependency completeness, output
identity, canonical ordering, deterministic invocation/batch IDs, consumption references,
and confidence ceilings. It is the only computation-layer function that sees the raw
bundle; module code receives `FactorInputView`, whose selectors return record IDs with
values. Projection derives each opaque `InputId` deterministically from the snapshot
domain and selected normalized record ID; it never copies source or raw references.
After computation, `run_factor` derives `source_evidence_status` from the consumed
manifest records and stamps `factor_validation_status` from the immutable registry entry;
module logic cannot branch on either status.
The resolved definition hash binds `validation_record_id` and `oracle_version`, so a later
holdout graduation creates a new append-only registry/materialization identity rather than
mutating old outputs or changing a batch under the same key.
`invocation_id` is the lowercase SHA-256 hex digest of canonical definition/version,
parameters, snapshot ID, ordered subject scope, and dependency batch IDs. Alias equality
never implies execution equality. Repository keys use `invocation_id`, never
only `(factor_id, factor_version, snapshot_id)`. Module functions own only domain
computation.

### 14.2 Capture, Normalization, and Extraction

```python
class SourceRequest(BaseModel):
    source: DataSource
    subjects: tuple[SubjectRef, ...]
    requested_at: datetime
    source_cutoff: datetime | None = None
    parameters: dict[str, JsonValue] = Field(default_factory=dict)

class SourceAdapter(Protocol):
    source: DataSource
    async def capture(self, request: SourceRequest) -> RawCapture: ...

class SourceCallGateway(Protocol):
    async def execute(
        self,
        adapter: SourceAdapter,
        request: SourceRequest,
    ) -> RawIngestionEnvelope: ...

class NormalizedBatch(BaseModel):
    batch_id: str
    source: DataSource
    raw_ref: str
    records: tuple[NormalizedRecord, ...]

class NormalizedRecordBase(BaseModel):
    record_id: str
    record_type: str
    source: DataSource
    raw_ref: str
    mapping_version: str
    knowable_at: datetime
    recorded_at: datetime
    confidence: Decimal = Field(ge=0, le=1)
    source_evidence_status: SourceEvidenceStatus
    extraction_id: str | None = None  # set only for LLM-derived semantic rows

class FinancialFact(NormalizedRecordBase):
    record_type: Literal["financial_fact"] = "financial_fact"
    subject: SubjectRef
    metric: str
    fiscal_period: str
    valid_from: date
    valid_to: date
    value: Decimal | None
    unit: str
    currency: CurrencyCode | None
    source_metric: str
    accession: str | None = None
    form: str | None = None
    is_restatement: bool = False

class GraphEdgeRecord(NormalizedRecordBase):
    record_type: Literal["graph_edge"] = "graph_edge"
    from_subject: SubjectRef
    to_subject: SubjectRef
    relation_type: str
    valid_from: date
    valid_to: date | None

class AnalystRatingRecord(NormalizedRecordBase):
    record_type: Literal["analyst_rating"] = "analyst_rating"
    analyst_id: AnalystId
    covered_subject: SubjectRef
    recommendation_at: datetime
    vendor_updated_at: datetime | None
    normalized_score: Decimal = Field(ge=-1, le=1)
    category: RatingCategory
    rating_action: RatingAction
    rating_scale_version: str
    vendor_rating_label: str
    target_price: MoneyValue | None

class ForecastRecord(NormalizedRecordBase):
    record_type: Literal["forecast"] = "forecast"
    forecast_revision_id: str
    issuer_id: IssuerId
    target_metric: str
    target_period: str | None
    horizon_start: date | None
    horizon_end: date | None
    point_value: Decimal | None
    lower_bound: Decimal | None
    upper_bound: Decimal | None
    unit: str
    currency: CurrencyCode | None
    statistic: Literal["mean", "median"]
    provider_id: str
    constituent_count: int | None
    published_at: datetime

class GuidanceRecord(NormalizedRecordBase):
    record_type: Literal["guidance"] = "guidance"
    guidance_revision_id: str
    issuer_id: IssuerId
    target_metric: str
    target_period: str | None
    horizon_start: date | None
    horizon_end: date | None
    point_value: Decimal | None
    lower_bound: Decimal | None
    upper_bound: Decimal | None
    unit: str
    currency: CurrencyCode | None
    guidance_status: Literal["issued", "updated", "withdrawn"]
    published_at: datetime

class FundHoldingRecord(NormalizedRecordBase):
    record_type: Literal["fund_holding"] = "fund_holding"
    fund_security_id: SecurityId
    holding_security_id: SecurityId | None
    holding_issuer_id: IssuerId | None
    unresolved_holding_key: str | None
    instrument_kind: SecurityKind
    report_period: date
    side: HoldingSide
    weight: Decimal = Field(ge=0)
    market_value: MoneyValue | None
    notional_value: MoneyValue | None
    delta: Decimal | None

class SegmentRevenueRecord(NormalizedRecordBase):
    record_type: Literal["segment_revenue"] = "segment_revenue"
    issuer_id: IssuerId
    segment_name: str
    revenue: MoneyValue
    fiscal_period: str

class PriceBarRecord(NormalizedRecordBase):
    record_type: Literal["price_bar"] = "price_bar"
    listing_id: ListingId
    security_id: SecurityId
    trading_date: date
    exchange_mic: str
    timezone: str
    currency: CurrencyCode
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

class FxRateRecord(NormalizedRecordBase):
    record_type: Literal["fx_rate"] = "fx_rate"
    base_currency: CurrencyCode
    quote_currency: CurrencyCode
    observed_at: datetime
    rate: Decimal

class CorporateActionRecord(NormalizedRecordBase):
    record_type: Literal["corporate_action"] = "corporate_action"
    action_id: str
    security_id: SecurityId
    action_type: CorporateActionType
    announced_at: datetime | None
    ex_date: date
    effective_date: date
    pay_date: date | None
    ratio: Decimal | None
    cash_amount: MoneyValue | None
    resulting_security_id: SecurityId | None

class UniverseMembershipRecord(NormalizedRecordBase):
    record_type: Literal["universe_membership"] = "universe_membership"
    membership_id: str
    universe_id: UniverseId
    subject: SubjectRef
    valid_from: date
    valid_to: date | None

class EntityIdentifierRecord(NormalizedRecordBase):
    record_type: Literal["entity_identifier"] = "entity_identifier"
    identifier_id: str
    subject: SubjectRef
    identifier_type: str
    identifier_value: str
    valid_from: date
    valid_to: date | None

class SourceDocumentRecord(NormalizedRecordBase):
    record_type: Literal["source_document"] = "source_document"
    issuer_id: IssuerId | None
    security_id: SecurityId | None
    document_type: str
    published_at: datetime
    text: str

class ExtractionRecord(NormalizedRecordBase):
    record_type: Literal["extraction_record"] = "extraction_record"
    extraction_id: str
    extraction_invocation_sha256: str
    extractor_id: str
    extractor_version: str
    model_id: str
    model_version: str
    prompt_sha256: str
    schema_sha256: str
    source_document_record_id: str
    source_document_semantic_sha256: str
    draft_links: tuple[ExtractionDraftLink, ...]
    evidence_spans: tuple[EvidenceSpan, ...]
    result_sha256: str

NormalizedRecord = Annotated[
    FinancialFact | GraphEdgeRecord | AnalystRatingRecord | ForecastRecord | GuidanceRecord
    | FundHoldingRecord
    | SegmentRevenueRecord
    | PriceBarRecord | FxRateRecord | CorporateActionRecord
    | UniverseMembershipRecord | EntityIdentifierRecord | SourceDocumentRecord
    | ExtractionRecord,
    Field(discriminator="record_type"),
]

Forecast and guidance validators require exactly one target-period or explicit horizon,
and exactly one point value or complete lower/upper range. Every correction uses a new
revision ID and row; `published_at` is source publication while `knowable_at` is the
independently permitted PIT time. Provider identity and raw label stay outside factor
inputs; constituent count and normalized statistic remain semantic inputs.

class MetricSpec(BaseModel):
    name: str
    subject_kind: Literal[SubjectKind.ISSUER, SubjectKind.SECURITY]
    unit_family: Literal["currency", "count", "ratio", "per_share"]
    currency_required: bool
    source_priority: tuple[DataSource, ...]
    financial_issuer_split: bool = False

class MetricRegistry(Protocol):
    fusion_ruleset_version: int
    def get(self, metric: str) -> MetricSpec: ...

class CanonicalFinancialFact(BaseModel):
    selected_record_id: str
    fusion_ruleset_version: int
    fact: FinancialFact

def select_canonical_facts(
    records: Sequence[FinancialFact],
    *,
    request: SnapshotRequest,
    registry: MetricRegistry,
) -> tuple[CanonicalFinancialFact, ...]: ...

class Normalizer(Protocol):
    source: DataSource
    def normalize(
        self,
        envelope: RawIngestionEnvelope,
        payload: bytes,
    ) -> NormalizedBatch: ...

def project_extraction_document(
    record: SourceDocumentRecord,
    *,
    as_of: datetime,
) -> FactorDocumentInput: ...

class StructuredExtractionModel(Protocol):
    model_id: str
    model_version: str
    def complete(
        self, document: FactorDocumentInput, schema: type[BaseModel], *, instructions: str
    ) -> BaseModel: ...

class EvidenceSpan(BaseModel):
    span_id: str
    start: int
    end: int
    text_sha256: str

class ExtractionDraftLink(BaseModel):
    draft_index: int
    produced_record_id: str
    evidence_span_ids: tuple[str, ...]

class FinancialFactDraft(BaseModel):
    draft_type: Literal["financial_fact"] = "financial_fact"
    subject: SubjectRef
    metric: str
    value: Decimal | None
    unit: str
    currency: CurrencyCode | None
    fiscal_period: str | None
    valid_from: date | None
    valid_to: date | None
    confidence: Decimal

class RelationshipDraft(BaseModel):
    draft_type: Literal["relationship"] = "relationship"
    from_subject: SubjectRef
    to_subject: SubjectRef
    relation_type: str
    valid_from: date
    valid_to: date | None
    confidence: Decimal

class SegmentRevenueDraft(BaseModel):
    draft_type: Literal["segment_revenue"] = "segment_revenue"
    issuer_id: IssuerId
    segment_name: str
    revenue: MoneyValue
    fiscal_period: str
    confidence: Decimal

SemanticRecordDraft = Annotated[
    FinancialFactDraft | RelationshipDraft | SegmentRevenueDraft,
    Field(discriminator="draft_type"),
]

class ExtractedDraft(BaseModel):
    draft: SemanticRecordDraft
    evidence_span_ids: tuple[str, ...]

class ExtractionResult(BaseModel):
    extraction_id: str
    extraction_invocation_sha256: str
    extractor_id: str
    extractor_version: str
    model_id: str
    model_version: str
    prompt_sha256: str
    schema_sha256: str
    source_document_record_id: str
    source_document_semantic_sha256: str
    drafts: tuple[ExtractedDraft, ...]
    evidence_spans: tuple[EvidenceSpan, ...]
    result_sha256: str

class ExtractionMaterializationContext(BaseModel):
    source: DataSource
    raw_ref: str
    raw_sha256: str
    mapping_version: str
    knowable_at: datetime
    recorded_at: datetime

class ExtractionMaterializationResult(BaseModel):
    extraction_record_id: str
    produced_record_ids: tuple[str, ...]
    inserted: bool

class ExtractionRepository(Protocol):
    def materialize(
        self,
        result: ExtractionResult,
        *,
        context: ExtractionMaterializationContext,
    ) -> ExtractionMaterializationResult: ...
    def get(self, extraction_id: str) -> ExtractionRecord | None: ...

def materialize_normalized_batch(
    batch: NormalizedBatch,
    *,
    repository: NormalizedRecordRepository,
) -> MaterializationResult: ...

def materialize_extraction_result(
    result: ExtractionResult,
    *,
    context: ExtractionMaterializationContext,
    repository: ExtractionRepository,
) -> ExtractionMaterializationResult: ...
```

The call gateway enforces source budgets and immutable raw capture. Normalizers may use
source metadata. Data-engine selects a stored `SourceDocumentRecord` and
`project_extraction_document` strips its provenance before calling extraction logic;
documents never enter `FactorInputBundle`. Extraction logic lives in
`libs/factors/shared`, returns semantic drafts, and is invoked by data-engine. Data-engine
alone attaches the materialization context. `ExtractionRepository.materialize` atomically
stores one immutable `ExtractionRecord` and every produced normalized row, stamping each
row with `extraction_id`. The record preserves invocation/model/prompt/schema versions,
the source-document record/version, exact evidence spans, and draft-to-record links.
Replay consumes the produced rows and never calls a model implicitly; trace reads follow
semantic record -> extraction ID -> stored evidence span -> source document/raw object.
Every normalized row carries both
`knowable_at`/`transaction_time` and `recorded_at`, plus a parser `mapping_version`.
`FinancialFact.source` is mandatory because source-priority selection is otherwise
impossible. Canonical fact selection groups by subject, metric, and fiscal period, then
uses the registered `source_priority` first and the latest
eligible vintage within that source second. Confidence rides with the selected fact but
never arbitrates between sources. Unregistered source/metric pairs remain staging
evidence and cannot silently reach factors or mart. Canonical lineage records both the
selected staging row and fusion-ruleset version in the durable snapshot manifest.

### 14.3 Module 1: PEG

```python
class GrowthConvention(StrEnum):
    ANALYST_CONSENSUS = "analyst_consensus"
    HISTORICAL_CAGR = "historical_cagr"
    COMPANY_GUIDANCE = "company_guidance"

class PegParameters(FactorParameters):
    growth_convention: GrowthConvention
    earnings_metric: str
    price_metric: str
    growth_horizon_years: int = Field(ge=1, le=10)
    max_input_age: timedelta
    annualize_growth: bool = True

def compute_peg(
    context: FactorExecutionContext,
    inputs: FactorInputView,
    parameters: PegParameters,
) -> FactorOutputBatch: ...
```

The batch emits price/earnings, selected growth rate, PEG, convention, and explicit
unavailability flags. Each convention has a separate golden oracle; no fallback silently
changes the requested convention. Analyst consensus consumes `FactorForecastInput` and
company guidance consumes `FactorGuidanceInput`; neither convention encodes horizon,
range, revision, or constituent semantics inside a generic metric name.

### 14.4 Module 2: Gross Profit per Employee

```python
class HeadcountExtractionParameters(FactorParameters):
    taxonomy_version: str
    allowed_document_types: frozenset[str]
    max_document_age: timedelta

class GrossProfitPerEmployeeParameters(FactorParameters):
    gross_profit_metric: str
    headcount_metric: str
    financial_policy: Literal["explicit_proxy"] = "explicit_proxy"
    financial_proxy_metric: str
    financial_semantics_version: str
    max_period_gap: timedelta

def extract_headcount(
    document: FactorDocumentInput,
    parameters: HeadcountExtractionParameters,
    model: StructuredExtractionModel,
) -> ExtractionResult: ...

def compute_gross_profit_per_employee(
    context: FactorExecutionContext,
    inputs: FactorInputView,
    parameters: GrossProfitPerEmployeeParameters,
) -> FactorOutputBatch: ...
```

Extraction selects total company headcount, not the first employee-like number, and emits
an `employees_total` `FinancialFactDraft` that is materialized to staging before factor
execution. The
financial policy is mandatory and versioned. Missing, ambiguous, or incomparable facts
are flagged; they never become zero. A financial issuer cannot satisfy module coverage by
being excluded: its separately defined proxy must compute, or the output is explicitly
`unavailable` and counts against the applicable usable-coverage SLO.

### 14.5 Module 3: Supply Chain

```python
class SupplyChainExtractionParameters(FactorParameters):
    taxonomy_version: str
    allowed_relation_types: frozenset[str]
    minimum_evidence_mentions: int = Field(ge=1)

class ExposureScenario(BaseModel):
    scenario_id: str
    scenario_version: str
    shocked_subject: SubjectRef
    shock_metric: str
    shock_direction: Literal["increase", "decrease"]
    shock_magnitude: Decimal
    shock_unit: str
    horizon: timedelta
    interpretation: Literal["scenario_not_causal", "causal_validated"]
    causal_evidence_ids: tuple[str, ...] = ()
    assumptions: tuple[str, ...]

    @model_validator(mode="after")
    def require_evidence_for_causal_label(self) -> Self: ...

class SupplyChainReasoningParameters(FactorParameters):
    scenario: ExposureScenario
    minimum_edge_confidence: Decimal = Field(ge=0, le=1)
    maximum_hops: int = Field(ge=1, le=3)
    decay_per_hop: Decimal = Field(gt=0, le=1)
    allowed_relation_types: frozenset[str]
    materiality_floor: Decimal = Field(ge=0)
    propagation_policy_version: str

def extract_supply_chain_relationships(
    document: FactorDocumentInput,
    parameters: SupplyChainExtractionParameters,
    model: StructuredExtractionModel,
) -> ExtractionResult: ...

def compute_supply_chain_exposure(
    context: FactorExecutionContext,
    inputs: FactorInputView,
    parameters: SupplyChainReasoningParameters,
) -> FactorOutputBatch: ...
```

The extraction result emits `RelationshipDraft` records that are resolved and materialized
to staging before the base factor runs. Causal/exposure reasoning remains
disabled until evidence calibrates `minimum_edge_confidence`; the runtime rejects a
reasoning run below that declared kill condition. The result is a versioned
`ScenarioExposureObservation`, not a causal conclusion, unless the scenario carries
independent causal evidence and uses the explicit `causal_validated` interpretation.

### 14.6 Module 4: Analyst Backtesting

```python
class AnalystBacktestParameters(FactorParameters):
    forecast_horizon: timedelta
    benchmark_security_id: SecurityId
    execution_rule: RuleInvocation
    trading_calendar_rule: RuleInvocation
    return_rule: RuleInvocation
    minimum_observations: int = Field(ge=1)
    score_weights: dict[str, Decimal]

def compute_analyst_track_record(
    context: FactorExecutionContext,
    inputs: FactorInputView,
    parameters: AnalystBacktestParameters,
) -> FactorOutputBatch: ...
```

Recommendation time, corroborated knowability, vendor update time, execution time, and
evaluation horizon remain distinct. Cross-firm labels are mapped through immutable
`rating_scale_version` into category/score/action before the factor boundary; the vendor's
raw label remains normalized provenance and cannot drive factor branches. Outputs include observation count, hit rate, excess
return, target-price error, composite score, and insufficient-history flags.

### 14.7 Module 5: ETF Virtual Company

```python
class EtfMetricAggregationSpec(BaseModel):
    output_metric: str
    input_metrics: tuple[str, ...]
    method: Literal["weighted_mean", "sum", "ratio_of_sums", "weighted_harmonic_mean"]
    eligible_instrument_kinds: frozenset[SecurityKind]
    minimum_metric_weight: Decimal = Field(ge=0, le=1)

class EtfVirtualCompanyParameters(FactorParameters):
    aggregations: tuple[EtfMetricAggregationSpec, ...]
    base_currency: CurrencyCode
    minimum_resolved_weight: Decimal = Field(ge=0, le=1)
    missing_constituent_policy: Literal["renormalize", "reject"]
    cash_policy: Literal["include_as_cash", "exclude_and_report", "reject"]
    derivative_policy: Literal["delta_adjusted_notional", "exclude_and_report", "reject"]
    short_policy: Literal["net_exposure", "gross_exposure", "reject"]
    fund_of_funds_policy: Literal["look_through_one_level", "treat_as_security", "reject"]
    period_alignment: Literal["latest_known_at_holding_report", "common_fiscal_period"]
    maximum_holding_age: timedelta
    maximum_fundamental_age: timedelta
    maximum_fx_age: timedelta

def compute_etf_virtual_company(
    context: FactorExecutionContext,
    inputs: FactorInputView,
    parameters: EtfVirtualCompanyParameters,
) -> FactorOutputBatch: ...
```

Each holding's resolution outcome is fixed before the factor boundary; unresolved rows
retain only an opaque semantic key so their weight is not lost. The output records
resolved/unresolved weight, the chosen missing-data policy, weighted metrics, and minimum consumed
confidence. Aggregation is declared per metric: additive, ratio, and multiple-like metrics
cannot share one weighted-average rule. Cash, derivatives, nested funds, unresolved
instruments, base-currency FX, and constituent-period alignment are explicit policies and
coverage outputs. Current holdings are never applied to older report periods.

### 14.8 Module 6: Pure-Blood Screening

```python
class ThemeDefinition(BaseModel):
    theme_id: str
    theme_version: str
    positive_concepts: tuple[str, ...]
    excluded_concepts: tuple[str, ...] = ()

class PureBloodParameters(FactorParameters):
    theme: ThemeDefinition
    taxonomy_version: str
    minimum_classified_share: Decimal = Field(ge=0, le=1)
    unclassified_policy: Literal["retain", "reject"]

def compute_theme_revenue_share(
    context: FactorExecutionContext,
    inputs: FactorInputView,
    parameters: PureBloodParameters,
) -> FactorOutputBatch: ...

def extract_theme_segments(
    document: FactorDocumentInput,
    parameters: PureBloodParameters,
    model: StructuredExtractionModel,
) -> ExtractionResult: ...
```

Structured segment revenue wins when available; semantic extraction is a declared
fallback that materializes versioned `SegmentRevenueDraft` records to staging before
`compute_theme_revenue_share` runs. Outputs expose classified, excluded, and
unclassified revenue shares so ranking cannot hide incomplete coverage.

### 14.9 Module 7: Three-Tier Valuation

```python
class ValuationBand(BaseModel):
    label: Literal["traditional", "tech", "large_model_native"]
    ps_floor: Decimal = Field(gt=0)
    ps_ceiling: Decimal = Field(gt=0)

class ThreeTierValuationParameters(FactorParameters):
    gp_per_employee_thresholds: tuple[Decimal, Decimal]
    bands: tuple[ValuationBand, ValuationBand, ValuationBand]
    financial_policy: Literal["exclude", "separate_band"]
    minimum_confidence: Decimal = Field(ge=0, le=1)

class PriceToSalesParameters(FactorParameters):
    revenue_metric: str
    shares_metric: str
    revenue_basis: Literal["ttm", "latest_fiscal_year"]
    shares_basis: Literal["period_end_diluted", "latest_basic"]
    price_field: Literal["close"] = "close"
    listing_policy: Literal["primary_listing"] = "primary_listing"
    valuation_currency: CurrencyCode
    maximum_price_age: timedelta
    maximum_fx_age: timedelta
    maximum_period_gap: timedelta

def compute_price_to_sales(
    context: FactorExecutionContext,
    inputs: FactorInputView,
    parameters: PriceToSalesParameters,
) -> FactorOutputBatch: ...

def compute_three_tier_valuation(
    context: FactorExecutionContext,
    inputs: FactorInputView,
    parameters: ThreeTierValuationParameters,
    upstream: Mapping[str, FactorOutputBatch],
) -> FactorOutputBatch: ...
```

Price-to-sales resolves issuer -> security -> primary listing at the cutoff, validates
units, and converts revenue/market capitalization only through explicit `FactorFxInput`
records; missing or stale FX rejects the value. The composite consumes
gross-profit-per-employee and price-to-sales batches from the
same snapshot/cutoff. It emits tier, band, valuation gap, eligibility, and flags. Bands
and thresholds are research parameters, not performance-validated constants. The
3-4x/8-10x/20-30x ranges in `vision.md` are illustrative research anchors, not executable
defaults; #59 must freeze explicit v1 values and independent boundary oracles before a
three-tier invocation can graduate beyond `UNVALIDATED`.

### 14.10 Screens, Strategy, and Replay

```python
class ScreenInvocation(BaseModel):
    screen_id: str
    screen_version: str
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    factor_inputs: dict[str, str]  # screen slot -> factor invocation_alias

class StrategyDefinition(BaseModel):
    strategy_id: str
    strategy_version: str
    universe_id: UniverseId
    factors: tuple[FactorInvocation, ...]
    screen: ScreenInvocation
    rebalance_rule: RuleInvocation
    sizing_rule: RuleInvocation
    holding_rule: RuleInvocation
    return_rule: RuleInvocation

class StrategyRegistry(Protocol):
    def resolve(self, strategy_id: str, strategy_version: str) -> StrategyDefinition: ...

class BacktestDefinition(BaseModel):
    strategy: StrategyDefinition
    start: date
    end: date
    initial_cash: MoneyValue
    execution_rule: RuleInvocation
    transaction_cost_rule: RuleInvocation
    trading_calendar_rule: RuleInvocation
    fx_rule: RuleInvocation
    as_of_schedule_rule: RuleInvocation

class ScreenExecutionContext(BaseModel):
    screen_id: str
    screen_version: str
    snapshot_id: str
    as_of: datetime
    invocation_sha256: str

class ScreenCandidate(BaseModel):
    subject: SubjectRef
    accepted: bool
    rank: int | None
    score: Decimal | None
    confidence: Decimal
    consumed_output_ids: tuple[OutputId, ...]
    reason_codes: tuple[str, ...]

class ScreenResult(BaseModel):
    screen_result_id: str
    invocation: ScreenInvocation
    invocation_sha256: str
    parameters_sha256: str
    snapshot_id: str
    as_of: datetime
    candidates: tuple[ScreenCandidate, ...]

class ScreenDefinition(Protocol):
    screen_id: str
    screen_version: str
    required_factor_slots: Mapping[str, str]
    def evaluate(
        self,
        context: ScreenExecutionContext,
        batches: Mapping[str, FactorOutputBatch],
        *,
        universe: Sequence[SubjectRef],
        parameters: Mapping[str, JsonValue],
    ) -> ScreenResult: ...

class ScreenRegistry(Protocol):
    def resolve(self, screen_id: str, screen_version: str) -> ScreenDefinition: ...

R = TypeVar("R")
class StrategyRuleRegistry(Protocol):
    def resolve(self, invocation: RuleInvocation, expected: type[R]) -> R: ...

class TargetPosition(BaseModel):
    security_id: SecurityId
    listing_id: ListingId
    target_weight: Decimal = Field(ge=0, le=1)
    rank: int = Field(ge=1)
    confidence: Decimal
    consumed_output_ids: tuple[OutputId, ...]
    reason_codes: tuple[str, ...] = ()

class PortfolioDecision(BaseModel):
    decision_id: str
    strategy_id: str
    strategy_version: str
    snapshot_id: str
    as_of: datetime
    targets: tuple[TargetPosition, ...]
    target_cash_weight: Decimal = Field(ge=0, le=1)
    rejected_subjects: tuple[SubjectRef, ...] = ()

    @model_validator(mode="after")
    def require_weights_sum_to_one(self) -> Self: ...

class Position(BaseModel):
    security_id: SecurityId
    listing_id: ListingId
    quantity: Decimal
    cost_basis: MoneyValue

class PendingCashEntitlement(BaseModel):
    action_id: str
    security_id: SecurityId
    pay_date: date
    amount: MoneyValue

class PortfolioState(BaseModel):
    as_of: datetime
    base_currency: CurrencyCode
    cash: Decimal
    positions: tuple[Position, ...]
    pending_cash_entitlements: tuple[PendingCashEntitlement, ...] = ()
    processed_market_event_ids: tuple[str, ...] = ()

class SimulatedTrade(BaseModel):
    trade_id: str
    decision_id: str
    security_id: SecurityId
    listing_id: ListingId
    execution_at: datetime
    quantity: Decimal
    price: MoneyValue
    cost: MoneyValue
    price_record_id: str

class PortfolioValuation(BaseModel):
    at: datetime
    value: MoneyValue
    confidence: Decimal
    consumed_market_event_ids: tuple[str, ...]

class BacktestMetric(BaseModel):
    metric: str
    value: Decimal | None
    unit: str
    confidence: Decimal

class AppliedMarketEvent(BaseModel):
    event_id: str
    applied_at: datetime
    resulting_state_sha256: str

class BacktestRunResult(BaseModel):
    run_id: str
    definition_sha256: str
    definition: BacktestDefinition
    contract_version: str
    snapshot_ids: tuple[str, ...]
    decisions: tuple[PortfolioDecision, ...]
    trades: tuple[SimulatedTrade, ...]
    valuations: tuple[PortfolioValuation, ...]
    applied_market_events: tuple[AppliedMarketEvent, ...]
    metrics: tuple[BacktestMetric, ...]
    release_manifest_id: str
    execution_artifact_digest: str
    flags: tuple[str, ...] = ()

class VersionedRule(Protocol):
    rule_id: str
    rule_version: str
    parameters_model: type[BaseModel]

@runtime_checkable
class RebalanceScheduleRule(VersionedRule, Protocol):
    def cutoffs(self, *, start: date, end: date, timezone: str) -> tuple[datetime, ...]: ...

@runtime_checkable
class SizingRule(VersionedRule, Protocol):
    def size(
        self,
        *,
        screen: ScreenResult,
        current: PortfolioState,
        as_of: datetime,
    ) -> tuple[TargetPosition, ...]: ...

@runtime_checkable
class HoldingRule(VersionedRule, Protocol):
    def apply(
        self,
        *,
        proposed: Sequence[TargetPosition],
        current: PortfolioState,
        history: Sequence[PortfolioDecision],
        as_of: datetime,
    ) -> tuple[TargetPosition, ...]: ...

@runtime_checkable
class TradingCalendarRule(VersionedRule, Protocol):
    def next_eligible_time(
        self, *, exchange_mic: str, requested_at: datetime
    ) -> datetime: ...

class ExecutionFill(BaseModel):
    security_id: SecurityId
    listing_id: ListingId
    quantity: Decimal
    executed_at: datetime
    price: MoneyValue
    price_record_id: str

@runtime_checkable
class ExecutionRule(VersionedRule, Protocol):
    def fill(
        self,
        *,
        decision: PortfolioDecision,
        current: PortfolioState,
        available_bars: Sequence[PriceBarEvent],
        calendar: TradingCalendarRule,
    ) -> tuple[ExecutionFill, ...]: ...

@runtime_checkable
class FxRule(VersionedRule, Protocol):
    def convert(
        self,
        value: MoneyValue,
        *,
        to_currency: CurrencyCode,
        at: datetime,
        rates: Sequence[FxRateEvent],
    ) -> MoneyValue: ...

@runtime_checkable
class ReturnRule(VersionedRule, Protocol):
    def value(
        self,
        *,
        state: PortfolioState,
        bars: Sequence[PriceBarEvent],
        fx: FxRule,
        fx_rates: Sequence[FxRateEvent],
        at: datetime,
    ) -> PortfolioValuation: ...

@runtime_checkable
class TransactionCostRule(VersionedRule, Protocol):
    def estimate(
        self,
        *,
        listing_id: ListingId,
        quantity: Decimal,
        price: MoneyValue,
        executed_at: datetime,
    ) -> MoneyValue: ...

class MarketEventBase(BaseModel):
    event_id: str
    event_type: str
    available_at: datetime
    effective_at: datetime
    record_id: str
    confidence: Decimal

class PriceBarEvent(MarketEventBase):
    event_type: Literal["price_bar"] = "price_bar"
    bar: FactorPriceInput

class CorporateActionEvent(MarketEventBase):
    event_type: Literal["corporate_action"] = "corporate_action"
    phase: Literal["ex", "effective", "pay"]
    action: FactorActionInput

class FxRateEvent(MarketEventBase):
    event_type: Literal["fx_rate"] = "fx_rate"
    rate: FactorFxInput

MarketEvent = Annotated[
    PriceBarEvent | CorporateActionEvent | FxRateEvent,
    Field(discriminator="event_type"),
]

class MarketEventRepository(Protocol):
    def available_events(
        self,
        *,
        after: datetime,
        through: datetime,
        listing_ids: frozenset[ListingId],
        security_ids: frozenset[SecurityId],
        currencies: frozenset[CurrencyCode],
    ) -> tuple[MarketEvent, ...]: ...

class FactorBatchProvider(Protocol):
    def for_decision(
        self,
        *,
        snapshot: ResearchSnapshot,
        invocations: Sequence[FactorInvocation],
        subjects: Sequence[SubjectRef],
    ) -> Mapping[str, FactorOutputBatch]: ...

def build_rebalance_cutoffs(
    definition: BacktestDefinition,
    *,
    rules: StrategyRuleRegistry,
) -> tuple[datetime, ...]: ...

def evaluate_strategy_at(
    definition: StrategyDefinition,
    *,
    as_of: datetime,
    universe: Sequence[SubjectRef],
    factor_batches: Mapping[str, FactorOutputBatch],
    current: PortfolioState,
    decision_history: Sequence[PortfolioDecision],
    screens: ScreenRegistry,
    rules: StrategyRuleRegistry,
) -> PortfolioDecision: ...

def simulate_execution(
    decision: PortfolioDecision,
    *,
    events: Sequence[MarketEvent],
    current: PortfolioState,
    rules: StrategyRuleRegistry,
    definition: BacktestDefinition,
) -> tuple[PortfolioState, tuple[SimulatedTrade, ...]]: ...

def apply_corporate_actions(
    state: PortfolioState,
    *,
    events: Sequence[CorporateActionEvent],
    after: datetime,
    through: datetime,
) -> PortfolioState: ...

def value_portfolio(
    state: PortfolioState,
    *,
    events: Sequence[MarketEvent],
    at: datetime,
    rules: StrategyRuleRegistry,
) -> PortfolioValuation: ...

def run_backtest(
    definition: BacktestDefinition,
    *,
    snapshots: ResearchSnapshotRepository,
    snapshot_store: SnapshotStore,
    factor_batches: FactorBatchProvider,
    screens: ScreenRegistry,
    rules: StrategyRuleRegistry,
    market_events: MarketEventRepository,
    release: ReleaseManifest,
) -> BacktestRunResult: ...
```

All ranking, selection, sizing, costs, actions, returns, and metrics live in
`libs/factors`. Dagster invokes these functions; it does not reimplement them. V1's only
permitted total-return implementation uses unadjusted bars plus explicit corporate-action
events. Adjusted-close series may be retained as staging evidence but cannot be combined
with explicit dividends or splits. The simulator advances monotonically, requests only
events with `available_at <= clock`, records every applied lifecycle event ID, and
therefore cannot apply an action phase twice. The event repository emits stable
`action_id:phase` identities: ex-date determines entitlement, effective-date changes the
security/quantity, pay-date determines cash receipt, and spinoffs require
`resulting_security_id`. Calendar-day lag arithmetic is forbidden;
the versioned execution and trading-calendar rules select the next eligible bar.

### 14.11 Persistence and Mart Projection

```python
class FactorOutputRepository(Protocol):
    def put(self, batch: FactorOutputBatch) -> PutResult: ...
    def get(
        self,
        *,
        invocation_id: str,
    ) -> FactorOutputBatch | None: ...

class MaterializedFactorOutputRepository(Protocol):
    def get_batch(
        self, *, invocation_id: str
    ) -> FactorOutputBatch | None: ...

class StrategyRunRepository(Protocol):
    def put(self, result: BacktestRunResult) -> PutResult: ...
    def get(self, run_id: str) -> BacktestRunResult | None: ...

class MartMaterializer(Protocol):
    def project_factor_batch(
        self, batch: FactorOutputBatch, *, snapshot: ResearchSnapshot
    ) -> MaterializationResult: ...
    def project_strategy_run(
        self,
        result: BacktestRunResult,
        *,
        snapshots: Sequence[ResearchSnapshot],
    ) -> MaterializationResult: ...

class PageRequest(BaseModel):
    limit: int = Field(default=100, ge=1, le=1000)
    cursor: str | None = None

class CatalogRef(BaseModel):
    alias: str
    catalog_id: str | None = None  # omitted means the current published catalog
    entry_version: str | None = None

class FactorInvocationSelector(BaseModel):
    invocation_alias: str
    factor_id: str
    factor_version: str
    parameters_sha256: str

class FactorCatalogTarget(BaseModel):
    target_type: Literal["factor"] = "factor"
    selector: FactorInvocationSelector

class RankingCatalogTarget(BaseModel):
    target_type: Literal["ranking"] = "ranking"
    screen_id: str
    screen_version: str
    parameters_sha256: str

class ThemeCatalogTarget(BaseModel):
    target_type: Literal["theme"] = "theme"
    theme_id: str
    theme_version: str
    factor_selector: FactorInvocationSelector
    ranking_alias: str

class ScenarioCatalogTarget(BaseModel):
    target_type: Literal["scenario"] = "scenario"
    scenario_id: str
    scenario_version: str
    factor_selector: FactorInvocationSelector

class StrategyCatalogTarget(BaseModel):
    target_type: Literal["strategy"] = "strategy"
    strategy_id: str
    strategy_version: str

CatalogTarget = Annotated[
    FactorCatalogTarget | RankingCatalogTarget | ThemeCatalogTarget | ScenarioCatalogTarget
    | StrategyCatalogTarget,
    Field(discriminator="target_type"),
]

class ResearchCatalogEntry(BaseModel):
    catalog_id: str
    alias: str
    entry_version: str
    label: str
    target: CatalogTarget
    universe_id: UniverseId
    universe_version: str
    applicability_policy_ref: str
    slo_policy_ref: str
    published_at: datetime

class ResearchCatalogManifest(BaseModel):
    catalog_id: str
    entries: tuple[ResearchCatalogEntry, ...]
    content_sha256: str
    published_at: datetime

class ResearchCatalog(Protocol):
    def get(self, catalog_id: str) -> ResearchCatalogManifest | None: ...
    def current(self) -> ResearchCatalogManifest: ...
    def resolve(self, ref: CatalogRef) -> ResearchCatalogEntry: ...

class CatalogQuery(BaseModel):
    target_type: Literal["factor", "ranking", "theme", "scenario", "strategy"] | None = None
    page: PageRequest = Field(default_factory=PageRequest)

class CatalogResult(BaseModel):
    catalog_id: str
    content_sha256: str
    entries: tuple[ResearchCatalogEntry, ...]
    next_cursor: str | None

class FactorHistoryQuery(BaseModel):
    factor: CatalogRef
    subjects: tuple[SubjectRef, ...]
    observed_range: DateRange
    as_of: datetime
    page: PageRequest = Field(default_factory=PageRequest)

class FactorHistory(BaseModel):
    catalog_entry: ResearchCatalogEntry
    observations: tuple[FactorObservation, ...]
    next_cursor: str | None

class EntityComparisonQuery(BaseModel):
    factors: tuple[CatalogRef, ...]
    subjects: tuple[SubjectRef, ...]
    observed_on: date
    as_of: datetime

class EntityComparison(BaseModel):
    catalog_entries: tuple[ResearchCatalogEntry, ...]
    observations: tuple[FactorObservation, ...]

class RankingQuery(BaseModel):
    ranking: CatalogRef
    universe_id: UniverseId
    as_of: datetime
    page: PageRequest = Field(default_factory=PageRequest)

class RankingResult(BaseModel):
    catalog_entry: ResearchCatalogEntry
    screen_result_id: str
    candidates: tuple[ScreenCandidate, ...]
    next_cursor: str | None

class RawTraceRef(BaseModel):
    record_id: str
    source: DataSource
    raw_ref: str
    raw_sha256: str
    mapping_version: str | None
    accession: str | None
    knowable_at: datetime
    extraction_id: str | None

class ExtractionTraceRef(BaseModel):
    extraction_id: str
    extraction_invocation_sha256: str
    extractor_id: str
    extractor_version: str
    model_id: str
    model_version: str
    prompt_sha256: str
    schema_sha256: str
    source_document_record_id: str
    source_document_semantic_sha256: str
    evidence_spans: tuple[EvidenceSpan, ...]
    produced_record_ids: tuple[str, ...]

class TraceabilityView(BaseModel):
    output_id: OutputId
    invocation: FactorInvocation
    invocation_id: str
    snapshot_id: str
    policy_versions: SelectionPolicyVersions
    consumed_input_ids: tuple[InputId, ...]
    consumed_upstream_output_ids: tuple[OutputId, ...]
    raw_records: tuple[RawTraceRef, ...]
    extractions: tuple[ExtractionTraceRef, ...]

class StrategyRunView(BaseModel):
    run_id: str
    definition: BacktestDefinition
    decisions: tuple[PortfolioDecision, ...]
    trades: tuple[SimulatedTrade, ...]
    valuations: tuple[PortfolioValuation, ...]
    metrics: tuple[BacktestMetric, ...]
    trace_output_ids: tuple[OutputId, ...]

class ResearchReadRepository(Protocol):
    def catalog(self, query: CatalogQuery) -> CatalogResult: ...
    def factor_history(self, query: FactorHistoryQuery) -> FactorHistory: ...
    def entity_comparison(self, query: EntityComparisonQuery) -> EntityComparison: ...
    def ranking(self, query: RankingQuery) -> RankingResult: ...
    def strategy_run(self, run_id: str) -> StrategyRunView | None: ...
    def trace_output(self, output_id: OutputId) -> TraceabilityView: ...
```

Writes are idempotent by semantic ID and append-only by version. A `FactorBatchProvider`
must execute a base invocation as `put -> project_factor_batch`, and a composite invocation
must reload every dependency through `MaterializedFactorOutputRepository`; in-memory,
unpublished base batches are not valid composite inputs. Read methods expose only mart
projections and immutable trace links; they perform no new factor computation. Mart trace
tables receive exact selected-record, extraction, and consumption lineage from the
snapshot and batch,
so the mart-only roles can expose raw checksums without permission on raw or staging.

### 14.12 Dagster Composition

```python
class CaptureAssetSpec(BaseModel):
    asset_key: str
    adapter_id: str
    source: DataSource
    subjects: tuple[SubjectRef, ...]
    request_parameters: dict[str, JsonValue]

class NormalizationAssetSpec(BaseModel):
    asset_key: str
    capture_asset_key: str
    normalizer_id: str

class SnapshotAssetSpec(BaseModel):
    asset_key: str
    normalized_asset_keys: tuple[str, ...]
    universe_id: UniverseId
    domains: frozenset[DataDomain]

class FactorAssetSpec(BaseModel):
    asset_key: str
    snapshot_asset_key: str
    invocation: FactorInvocation
    materialized_upstream_asset_keys: dict[str, str] = Field(default_factory=dict)

class StrategyAssetSpec(BaseModel):
    asset_key: str
    definition: StrategyDefinition
    factor_asset_keys: tuple[str, ...]

@dataclass(frozen=True)
class DagsterAssetCatalog:
    release_manifest_id: str
    execution_artifact_digest: str
    capture: tuple[CaptureAssetSpec, ...]
    normalization: tuple[NormalizationAssetSpec, ...]
    snapshots: tuple[SnapshotAssetSpec, ...]
    factors: tuple[FactorAssetSpec, ...]
    strategies: tuple[StrategyAssetSpec, ...]
    partitions_def: PartitionsDefinition

def build_capture_asset(spec: CaptureAssetSpec) -> AssetsDefinition: ...
def build_normalization_asset(spec: NormalizationAssetSpec) -> AssetsDefinition: ...
def build_snapshot_asset(spec: SnapshotAssetSpec) -> AssetsDefinition: ...
def build_factor_asset(
    spec: FactorAssetSpec, definition: FactorDefinition[Any]
) -> AssetsDefinition: ...
def build_strategy_assets(spec: StrategyAssetSpec) -> Sequence[AssetsDefinition]: ...

def build_definitions(
    *,
    catalog: DagsterAssetCatalog,
    release: ReleaseManifest,
    resources: Mapping[str, ResourceDefinition],
    factor_registry: FactorRegistry,
    screen_registry: ScreenRegistry,
    rule_registry: StrategyRuleRegistry,
    strategy_registry: StrategyRegistry,
) -> Definitions: ...

class StrategyScheduleSpec(BaseModel):
    schedule_id: str
    strategy_id: str
    strategy_version: str
    cron_schedule: str
    environment: Literal["staging", "production"]
    job_name: str
    partition_timezone: str
    universe_id: UniverseId
    release_manifest_id: str
    execution_artifact_digest: str

def build_strategy_schedule(spec: StrategyScheduleSpec) -> ScheduleDefinition: ...
```

Dagster is introduced with the first executable snapshot/factor slice. Local and CI use
in-process jobs and fixture resources; Staging and Production add schedules and persistent
metadata. `adapter_id` and `normalizer_id` resolve from Dagster resources at execution;
service instances are never captured inside asset definitions. The shared partition is an
aware `as_of` cutoff; universe and invocation are explicit asset-spec dimensions. A
composite `FactorAssetSpec` must depend on materialized upstream asset keys and reload
those batches from mart. Factor `data_version` is the hash of snapshot ID, invocation ID,
and upstream batch IDs. No alternative scheduler may launch real source runs.
The data-engine/Dagster code location is an immutable digest in the multi-artifact
`ReleaseManifest`; definitions and schedules reject a digest mismatch or floating tag.
Promotion moves the complete signed manifest, not an assumed single image.

### 14.13 Reports, MCP, App, and Chat

```python
class ReportItemRequest(BaseModel):
    item: CatalogRef  # factor, ranking, theme, scenario, or strategy alias
    subjects: tuple[SubjectRef, ...] = ()
    universe_id: UniverseId | None = None

class ResearchReportRequest(BaseModel):
    subjects: tuple[SubjectRef, ...]
    as_of: datetime
    items: tuple[ReportItemRequest, ...]
    observed_range: DateRange
    strategy_run_id: str | None = None

class NarrativeBlock(BaseModel):
    block_type: Literal["narrative"] = "narrative"
    text: str
    source_output_ids: tuple[OutputId, ...]

class ObservationBlock(BaseModel):
    block_type: Literal["observations"] = "observations"
    observations: tuple[FactorObservation, ...]

class RankingBlock(BaseModel):
    block_type: Literal["ranking"] = "ranking"
    ranking: RankingResult

class StrategyBlock(BaseModel):
    block_type: Literal["strategy"] = "strategy"
    strategy: StrategyRunView

ReportBlock = Annotated[
    NarrativeBlock | ObservationBlock | RankingBlock | StrategyBlock,
    Field(discriminator="block_type"),
]

class ReportSection(BaseModel):
    section_id: str
    title: str
    blocks: tuple[ReportBlock, ...]
    source_output_ids: tuple[OutputId, ...]

class ResearchReport(BaseModel):
    report_id: str
    as_of: datetime
    sections: tuple[ReportSection, ...]
    traceability: tuple[TraceabilityView, ...]

def build_research_report(
    request: ResearchReportRequest,
    *,
    read_repository: ResearchReadRepository,
) -> ResearchReport: ...

class ReportRenderer(Protocol):
    media_type: str
    def render(self, report: ResearchReport) -> RenderedArtifact: ...

class RenderedArtifact(BaseModel):
    artifact_id: str
    file_name: str
    media_type: str
    sha256: str
    content: bytes

class CardSpec(BaseModel):
    card_id: str
    title: str
    blocks: tuple[ReportBlock, ...]
    source_output_ids: tuple[OutputId, ...]

class XiaohongshuDeck(BaseModel):
    deck_id: str
    cards: tuple[CardSpec, ...]
    traceability: tuple[TraceabilityView, ...]

def build_xiaohongshu_deck(report: ResearchReport) -> XiaohongshuDeck: ...

class CardRenderer(Protocol):
    def render(self, deck: XiaohongshuDeck) -> tuple[RenderedArtifact, ...]: ...

class ResearchQueryService:
    def __init__(self, repository: ResearchReadRepository) -> None: ...
    def catalog(self, request: CatalogQuery) -> CatalogResult: ...
    def factor_history(self, request: FactorHistoryQuery) -> FactorHistory: ...
    def compare_entities(self, request: EntityComparisonQuery) -> EntityComparison: ...
    def rank_entities(self, request: RankingQuery) -> RankingResult: ...
    def explain_output(self, output_id: OutputId) -> TraceabilityView: ...
    def strategy_run(self, run_id: str) -> StrategyRunView: ...

async def mcp_catalog(request: CatalogQuery) -> CatalogResult: ...
async def mcp_factor_history(request: FactorHistoryQuery) -> FactorHistory: ...
async def mcp_compare_entities(request: EntityComparisonQuery) -> EntityComparison: ...
async def mcp_rank_entities(request: RankingQuery) -> RankingResult: ...
async def mcp_explain_output(output_id: OutputId) -> TraceabilityView: ...
async def mcp_strategy_run(run_id: str) -> StrategyRunView: ...

class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None

class ChatRequest(BaseModel):
    conversation_id: str
    messages: tuple[ChatMessage, ...]
    as_of: datetime | None = None

class ChatEvent(BaseModel):
    event_type: Literal["token", "tool_call", "tool_result", "error", "done"]
    payload: dict[str, JsonValue]

class ToolCallingModel(Protocol):
    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: ResearchQueryService,
    ) -> AsyncIterator[ChatEvent]: ...

async def stream_chat(
    request: ChatRequest,
    *,
    query_service: ResearchQueryService,
    model: ToolCallingModel,
) -> AsyncIterator[ChatEvent]: ...
```

The direct-mart App adapter implements the same semantic methods without calling
FastAPI:

```typescript
export interface MartResearchRepository {
  catalog(query: CatalogQuery): Promise<CatalogResult>;
  factorHistory(query: FactorHistoryQuery): Promise<FactorHistory>;
  entityComparison(query: EntityComparisonQuery): Promise<EntityComparison>;
  ranking(query: RankingQuery): Promise<RankingResult>;
  strategyRun(runId: string): Promise<StrategyRunView | null>;
  traceOutput(outputId: string): Promise<TraceabilityView>;
}

export function createMartResearchRepository(
  sql: SqlExecutor,
  options: { maxRows: number; statementTimeoutMs: number },
): MartResearchRepository;
```

The MCP endpoint and `/chat` tool layer reuse `ResearchQueryService`. Python JSON Schemas
for every read DTO are checked in and generate the TypeScript DTOs; CI runs identical
golden queries against the Python and TypeScript mart adapters. The App backend
implements the same read contract directly against mart, with no FastAPI hop. The App
may sort, filter, paginate, convert units, and render. Report-card and Xiaohongshu
renderers consume `ResearchReport`; deck construction only selects/reorders report blocks
and cannot join mart rows into new metrics.
`/chat` generates prose by calling the same typed tools, never by querying raw/staging
or inventing factor values.
Public consumers use versioned `CatalogRef` aliases such as a named PEG convention,
theme ranking, or supply scenario; they never construct parameter hashes or internal
materialization keys. Catalog publication is append-only, and each response returns the
resolved entry and immutable catalog ID/hash so a conversational answer remains
reconstructible after an alias advances. Each target binds its universe version and
applicability/SLO policy; strategy aliases are catalog targets rather than magic run names.

## 15. Complete Vision Call Graph

```text
Dagster schedule / local in-process job
  -> resolve signed ReleaseManifest and immutable data-engine/Dagster artifact digest
  -> SourceCallGateway.execute(SourceAdapter.capture)
  -> immutable object + raw.fetches
  -> Normalizer.normalize -> append-only staging records
  -> optional libs/factors/shared extraction -> semantic drafts
     -> data-engine attaches lineage -> append-only staging records
  -> MetricRegistry + select_canonical_facts(source priority, mapping/fusion version)
  -> ResearchSnapshotRepository.build_snapshot(one as_of, PIT universe and identity links)
  -> SnapshotStore.put
  -> project_factor_inputs(strips provenance)
  -> run_factor(base modules 1-6 and supporting metrics)
  -> FactorOutputRepository -> MartMaterializer
  -> reload materialized upstream -> run_factor(composite module 7)
  -> FactorOutputRepository -> MartMaterializer
  -> ScreenDefinition.evaluate
  -> evaluate_strategy_at
  -> simulation clock consumes newly available market events -> run_backtest
  -> StrategyRunRepository -> MartMaterializer
  -> ResearchReadRepository
     -> ResearchReport -> report-card / Xiaohongshu renderers
     -> MCP tools -> Claude/other MCP clients
     -> Next.js dashboard
     -> /chat tool orchestration
```

The graph has one computation path. Scheduled, backtest, MCP, App, report, and chat
results cannot disagree because all consume the same versioned factor outputs.

## 16. Vision Delivery Milestones

The GitHub source of truth is the [complete Vision epic](https://github.com/wangzitian0/truealpha/issues/28).

### 16.0 Gate 0: Semantic and Data Closure

Before implementation interfaces are called frozen, close issuer/security/listing,
currency/time/return, universe, snapshot, extraction, invocation, replay, and lineage
semantics; freeze independent research oracles; prove longitudinal source coverage and
usage rights; and define module applicability, usable-coverage, freshness, and graduation
SLOs. Tracked by [epic #56](https://github.com/wangzitian0/truealpha/issues/56), with
#57-#61 as its closure issues. Section 18 is the executable interface portion of this gate.

### 16.1 Gate 1: Core Strategy MVP

Deliver PIT snapshots, early Dagster composition, gross profit per employee, three-tier
valuation, `large_model_value_v0`, deterministic local replay, mart/report projection,
and a real scheduled Staging canary. Completion proves the bounded core slice can execute
idempotently under Dagster; it does not establish continuous all-module coverage,
Production readiness, or complete Vision delivery.
Tracked by [epic #29](https://github.com/wangzitian0/truealpha/issues/29), with
#14 and #21-#27 as sub-issues.

### 16.2 Gate 2: Seven Research Modules

Implement PEG's three conventions, analyst track records, ETF virtual-company metrics,
supply-chain extraction and versioned scenario exposure with the confidence kill
condition, and pure-blood theme ranking. Build the forecast/analyst, PIT ETF/instrument,
and document/extraction/segment/relationship data planes in #62-#64. Run one shared
seven-module replay, materialize every output, and pass the independent sealed holdout in
#65; high-confidence edges alone never justify a causal claim.
Tracked by [epic #30](https://github.com/wangzitian0/truealpha/issues/30), with
#33-#40 and #62-#65 as delivery and graduation issues.

### 16.3 Gate 3: Research Consumption

Freeze mart read models, expose typed MCP tools, generate traceable personal report
cards and Xiaohongshu card artifacts, add the App dashboard, and finally add `/chat`
as a tool-orchestration surface. Completion proves every Vision question can be answered
from mart with a filing/vintage trace.
Tracked by [epic #31](https://github.com/wangzitian0/truealpha/issues/31), with
#41-#46 and #48 as sub-issues.

### 16.4 Gate 4: Production Validation and Graduation

Extend the evaluation corpus to five years/multiple regimes, reconcile critical prices
against an independent source, validate strategy direction against a known reference,
schedule all seven modules in Staging, prove backup/restore and alerting, then promote
the exact signed multi-artifact release manifest to isolated Production shadow operation
with explicit approval.
Validate the deployed Production MCP, App, chat, report, and card paths against the same
mart outputs in #66. Expand to the owned curated universe and graduate shadow outputs only
after the natural-refresh soak, per-module SLOs, traceability, recovery, and recorded human
approval pass in #67.
Tracked by [epic #32](https://github.com/wangzitian0/truealpha/issues/32), with
#11, #49-#54, #66, and #67 as the environment, evaluation, consumer, and graduation tree.

Milestones are sequential release gates, not strict implementation serialization. Work
inside a milestone may run in parallel when GitHub dependencies permit it.
Usable coverage counts only applicable outputs whose `availability_status` is available
and fresh enough for the module SLO. `source_evidence_status` separately reports consumed
data corroboration, while `factor_validation_status` records golden/holdout graduation;
no one status can make another pass.

## 17. Complete Vision Acceptance

The root `vision.md` success state is reached only when all of these are true:

1. Every one of the seven modules has frozen semantics, a versioned implementation,
   independent golden and sealed-holdout evidence, PIT replay, mart projection,
   confidence, and output-to-evidence traceability; supply-chain output is called causal
   only when independent causal evidence exists.
2. The owned curated Production universe has graduated from shadow operation. Dagster is
   its only scheduler, and every applicable module meets its versioned usable-coverage,
   freshness, and traceability SLO across natural source refreshes; unavailable, stale,
   unresolved, excluded, low-confidence, and error outputs do not count as produced.
3. A user can ask the named research questions through the deployed Production MCP, App,
   and `/chat` paths and receive equivalent typed results traceable to invocation
   parameters, snapshot policy, filing/vintage or extraction evidence, and raw checksum.
4. The same mart outputs produce personal report cards and Xiaohongshu card artifacts
   without manual metric recomputation.
5. Strategy evaluation uses at least five years, independent price reconciliation,
   survivorship-safe membership, corporate actions, immutable definitions, and a known
   strategy sanity result; no positive-alpha claim is required unless separately tested.
6. Production uses the exact Staging-tested signed release manifest, including the
   immutable data-engine/Dagster artifact, with isolated credentials/storage,
   demonstrated backup/restore, append-only data, deployed-consumer evidence, a natural
   source-refresh soak, and recorded human graduation approval.

No milestone may claim the full Vision based on fixture readiness, code existence,
manual flag changes, immediate repeated canary runs, or one successful happy-path run.

## 18. Semantic Closure Gate and Versioning

V1 is **proposed**, not frozen. The semantic closure gate passes only when all of these
are executable and reviewed:

1. Every public model in Section 14 builds JSON Schema with no unresolved or ambiguous
   type, and issuer/security/listing plus currency/time validators have negative tests.
2. Fixture and Postgres repositories produce the same durable snapshot ID, exact selected
   record set, membership, policy versions, and lineage for the same request.
3. A competing-source/restatement test proves source-priority selection, then changes the
   fusion ruleset and still retrieves the original snapshot by ID.
4. A synthetic extraction contract probe runs stored document -> semantic draft -> atomic
   extraction/row persistence -> snapshot, then replays without a model call and traces
   exact evidence spans. This does not require the production headcount extractor or GPPE.
5. Two dummy invocations of one probe factor/version with different parameters and subject
   scopes coexist without repository, mart, Dagster asset, or query-key collisions.
6. A dummy base batch is persisted/materialized and a dummy composite reloads it from mart;
   exact consumed-input lineage enforces its confidence ceiling without requiring a real
   research module.
7. A synthetic replay contract probe excludes future bars from its decision snapshot,
   advances the clock to the next eligible unadjusted bar, applies split/dividend lifecycle
   events once, handles FX explicitly, and rejects adjusted-close/action double counting.
8. A dummy output projected into an ephemeral mart is traceable by a mart-only role through
   invocation, snapshot policy, staging IDs, extraction evidence, and raw checksum.
9. Generated Python/TypeScript schema-conformance fixtures and minimal in-memory adapters
   agree on catalog, history, comparison, ranking, pagination, and trace DTOs; deployed
   consumer behavior remains a Gate 3/4 obligation.
10. A signed multi-artifact release-manifest probe rejects a floating or mismatched
    data-engine/Dagster artifact digest.
11. A design review finds no remaining conflict with authoritative `init.md`, and every
    implementation issue names which closure test proves its downstream boundary.

After this gate, the v1 freeze covers field semantics, discriminators, identity and time
meaning, confidence/lineage rules, port behavior, and registry identity. It does not
freeze storage schemas or private internals.

- Contracts use major/minor semantic `contract_version` values.
- Factor, screen, strategy, and rule versions are independent and immutable.
- Removed/renamed fields, changed time meaning, confidence rules, or discriminators
  require a new major version.
- Formula, taxonomy, default threshold, or rule behavior changes require a new
  computation version even if contract shape is unchanged.
- Readers use explicit old-version adapters; writers emit only the current version.
- Persisted runs retain definitions, parameters, versions, and snapshot IDs permanently.
Until every gate passes, issue acceptance may validate an incremental slice but must not
describe these contracts as frozen or claim complete Vision closure.
These probes close semantics only. Real GPPE/strategy evidence belongs to Gate 1, real
seven-module and holdout evidence to Gate 2, deployed consumers to Gates 3/4, and natural
refresh/Production graduation to Gate 4.
