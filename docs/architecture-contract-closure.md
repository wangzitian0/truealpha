# TrueAlpha Vision Delivery Architecture
Status: proposed v1 delivery design; `init.md` remains authoritative. This document is
not frozen until the semantic closure gate in Section 18 passes.
## 1. Decision
Converge on this semantic pipeline before implementing more factor logic:
```text
RawCapture -> normalized PIT records -> ResearchSnapshot + DataSnapshotManifest
  -> run_factor(FactorDefinition.compute) -> FactorOutputBatch[Metric | Classification | Relationship]
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
    share_class: str | None
    underlying_security_id: SecurityId | None
    underlying_shares_per_security_unit: Decimal = Field(gt=0)
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
    listing_role: Literal["primary", "secondary"]
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

class PriceAdjustmentMode(StrEnum):
    RAW = "raw"
    ADJUSTED = "adjusted"

class ReturnPolicy(BaseModel):
    policy_id: str
    policy_version: str
    price_mode: PriceAdjustmentMode
    corporate_action_mode: Literal["explicit", "suppressed"]

    @model_validator(mode="after")
    def require_raw_explicit_v1(self) -> Self: ...
```

V1 either converts monetary inputs through explicit PIT FX observations or rejects a
cross-currency calculation. It never assumes that two bare monetary values share a
currency.
`PriceAdjustmentMode.ADJUSTED` exists only to classify reconciliation evidence in
normalized storage. `ReturnPolicy.require_raw_explicit_v1` rejects every V1 combination
except `price_mode=raw` plus `corporate_action_mode=explicit`.
V1 execution and total return permit only raw listing bars plus explicit corporate-action
events. Adjusted series may be stored and reconciled as evidence, but cannot enter V1
execution, valuation, or returns.

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
    adjustment_mode: Literal[PriceAdjustmentMode.RAW] = PriceAdjustmentMode.RAW
    confidence: Decimal; as_of: datetime
class FactorActionInput(BaseModel):
    input_id: InputId; action_id: str; security_id: SecurityId
    action_type: CorporateActionType; announced_at: datetime | None
    ex_date: date; record_date: date | None; effective_date: date; pay_date: date | None
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
    record_date: date | None
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
class UniverseRef(BaseModel):
    universe_id: UniverseId
    universe_version: str
    content_sha256: str

class UniverseManifest(BaseModel):
    ref: UniverseRef
    definition_kind: Literal["fixed_cohort", "pit_membership"]
    membership_ids: tuple[str, ...]
    resolver_version: str | None
    effective_at: datetime
    owner: str

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
    universe: UniverseRef | None = None

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
When scope uses a universe, its resolved membership and resolver version must match the
requested `UniverseRef`; repositories never substitute a mutable latest universe.
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
    invocation_template_id: str
    execution_id: str
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
    invocation_template_id: str
    execution_id: str
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
identities: factor code can use semantic values but cannot inspect source or raw lineage.
It never constructs final observations or a `ConsumedInputLineage`. `run_factor` creates
one tracked scope per `output_key`, records every selector and upstream access, stamps the
resulting IDs on drafts, and rejects access outside a scope or emission under another key.
Module conformance tests prove that no semantic record or upstream observation can be
read through the public view without the runner recording its identity.
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
- every invocation template has a caller-chosen stable alias and a deterministic template
  ID over factor version, parameters, and dependency templates;
- every execution has a separate deterministic ID over the template, snapshot,
  dependency batch IDs, and ordered subject scope;
- persisted definitions contain versioned registry invocations and immutable parameters,
  never callables;
- factor, screen, strategy, and rule versions make historical runs reconstructible.

## 8. Repository Ports
```python
class NormalizedRecordRepository(Protocol):
    def append(self, batch: NormalizedBatch) -> MaterializationResult: ...
    def candidates(self, request: SnapshotRequest) -> tuple[NormalizedRecord, ...]: ...

class UniverseRepository(Protocol):
    def get(self, ref: UniverseRef) -> UniverseManifest | None: ...
    def resolve(
        self, ref: UniverseRef, *, valid_on: date, as_of: datetime
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
  -> registry.resolve(template.factor_id, template.factor_version)
  -> snapshot_repository.build_snapshot(request)
     -> resolve PIT membership, issuer/security/listing links, and eligible vintages
     -> reconcile sources, assign confidence, build canonical manifest
  -> snapshot_store.put(snapshot)
  -> run_factor(template, snapshot, subjects)  # projects provenance-free tracked inputs
  -> output_repository.put(batch)
  -> materialize deterministic mart projection
  -> for a composite: reload declared upstream batches from the materialized mart boundary
     -> run_factor(template, snapshot, subjects, materialized_upstream)
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
- A price convention cannot expose a future action; V1 total return uses raw listing bars
  and explicit action phases exactly once under a versioned `ReturnPolicy`.
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
- Fundamentals attach to an issuer, outstanding share units and corporate actions attach
  to a security, and prices attach to a listing. Valuation resolves all links at the same
  cutoff; share classes or ADRs require an explicit security-unit conversion ratio.
- Monetary arithmetic requires matching currencies or a consumed PIT FX input.
- Execution uses an exchange calendar/timezone and unadjusted listing bars; adjusted
  history cannot be combined with explicit corporate-action events.
- Applicable financial issuers use the frozen operating-efficiency/comparison branch;
  blanket exclusion cannot satisfy module coverage. The selected level, elasticity, or
  combined leverage rule is an immutable factor parameter, never an implicit heuristic.

Scope, applicability, and readiness:
- Capture, snapshot, factor execution, screen, strategy, report, SLO, and audit resolve the
  exact `UniverseRef` ID/version/hash; no repository may substitute a mutable latest value.
- Applicability is joined from the approved catalog before execution. Producers cannot
  self-report applicability, and a narrower scope requires a new signed catalog and claim.
- A capture manifest has exactly one cell for every required scope/subject/domain/partition
  key. Missing raw or normalized evidence, confidence, eligible times, mapping/policy
  versions, passing quality, or lineage makes a required cell fail.
- Readiness is the signed deterministic conjunction of evaluator checks. A caller cannot
  set `ready`, relabel a required cell, or make a failed run green by removing it.
- Unknown/restricted/expired use rights, an expired approval, or projected source cost over
  an approved budget blocks source execution and release. Immediate retries, unchanged-byte
  reparsing, fixture replay, and synthetic mutations never count as natural refreshes.
- Public aliases resolve only inside the exact release-bound research catalog. There is no
  global `current` catalog or universe, and every response returns the resolved catalog
  identity. Canonical questions execute only through their frozen typed query contract.

Provenance and determinism:
- Normalized records retain `raw_ref`; manifests commit to the lineage set.
- Only `FactorInputBundle`, never `ResearchSnapshot`, crosses into factor code.
- Bundle records contain no source, raw reference, accession, or repository metadata.
- Factors cannot access or branch on provenance; projection tests enforce this boundary.
- Factors emit drafts only. `run_factor` alone creates final observations and automatically
  derives input/upstream consumption lineage, confidence ceilings, status, and IDs from
  accesses made inside each output scope.
- Template ID, execution ID, and runner-generated per-output consumption references
  recover output lineage; factor version alone is insufficient when parameters differ.
- Canonical ordering and Decimal serialization define content hashes.
- Identical definition, template, snapshot, ordered subjects, and upstream batches produce
  the same execution and output IDs.
- LLM extraction binds an immutable provider revision, decoding settings, prompt, schema,
  source-document hash, and attempt. Each new attempt is append-only; replay reads the
  stored result and never invokes a mutable model alias implicitly.
- A release, asset catalog, runtime artifacts, extraction templates/model revisions,
  universe, capture/applicability/source/SLO catalogs, and accepted evidence must agree by
  ID and content hash before publication.

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
- Reject a missing `UniverseRef`, content-hash mismatch, mutable-latest substitution, or
  cross-universe reuse in a snapshot, factor, strategy, report, SLO, or audit.
- Keep source and raw references out of factor-ready facts.
- Reject composite batches with mismatched snapshots or future timestamps.
- Prove selector/upstream access automatically creates per-output lineage and enforces the
  confidence ceiling; factor code has no API for constructing final lineage or status.
- Keep two parameterizations and subject scopes of one factor/version collision-free.
- Reject issuer/security/listing coercion, cross-currency arithmetic without PIT FX,
  missing share-class/ADR conversion, calendar-day execution, and adjusted-close plus
  explicit-action returns.
- Keep future execution bars out of decision snapshots and apply each market event once.
- Prove idempotent writes by semantic ID and append-only vintages.
- Reject a capture manifest with a missing/duplicate required cell or incomplete evidence,
  and reject a post-run applicability relabel that tries to shrink the denominator.
- Reject unknown or expired rights, an over-budget full-catalog projection, and a soak that
  counts retries or unchanged bytes as natural updates.
- Reject a mutable model alias, unapproved extraction template/revision, or replay that
  attempts a model call; preserve each old extraction result and evidence span.
- Reject catalog/release hash mismatch and prove an unversioned alias resolves only inside
  the service's bound catalog across Python and TypeScript adapters.
- Exercise both the non-financial and financial comparison branches and every selected
  leverage-rule boundary against frozen independent oracles.
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
    invocation_template_id: str
    execution_id: str
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

class ObservationDraftBase(BaseModel):
    output_key: str
    output_type: str
    subject: SubjectRef
    valid_from: date | None
    valid_to: date | None
    fiscal_period: str | None
    availability_status: AvailabilityStatus
    confidence_cap: Decimal | None = None
    status_reasons: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()

class MetricObservationDraft(ObservationDraftBase):
    output_type: Literal["metric"] = "metric"
    metric: str
    value: Decimal | None
    unit: str

class ClassificationObservationDraft(ObservationDraftBase):
    output_type: Literal["classification"] = "classification"
    taxonomy: str
    label: str
    score: Decimal | None = None

class RelationshipObservationDraft(ObservationDraftBase):
    output_type: Literal["relationship"] = "relationship"
    to_subject: SubjectRef
    relation_type: str
    strength: Decimal | None = None

class ScenarioExposureObservationDraft(ObservationDraftBase):
    output_type: Literal["scenario_exposure"] = "scenario_exposure"
    scenario_id: str
    scenario_version: str
    direction: Literal["positive", "negative", "mixed", "unknown"]
    exposure_value: Decimal | None
    unit: str
    path_count: int

FactorObservationDraft = Annotated[
    MetricObservationDraft | ClassificationObservationDraft
    | RelationshipObservationDraft | ScenarioExposureObservationDraft,
    Field(discriminator="output_type"),
]

class OutputComputationScope(Protocol):
    output_key: str
    snapshot_id: str
    as_of: datetime
    def select(
        self,
        record_type: type[I],
        *,
        where: Mapping[str, JsonScalar],
    ) -> tuple[I, ...]: ...
    def upstream(self, slot: str) -> tuple[FactorObservation, ...]: ...
    def emit(self, draft: FactorObservationDraft) -> None: ...

class TrackedFactorInputView(Protocol):
    snapshot_id: str
    as_of: datetime
    def output_scope(
        self, output_key: str
    ) -> ContextManager[OutputComputationScope]: ...

class FactorInvocationTemplate(BaseModel):
    invocation_alias: str  # stable name within a strategy/report definition
    invocation_template_id: str
    factor_id: str
    factor_version: str
    parameters: dict[str, JsonValue]
    dependencies: dict[str, str] = Field(default_factory=dict)  # slot -> invocation_alias

class CatalogAliasRef(BaseModel):
    alias: str
    entry_version: str | None = None

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

class SubjectSourceCoverage(BaseModel):
    subject: SubjectRef
    earliest_knowable_at: datetime
    latest_knowable_at: datetime
    observed_partitions: int = Field(ge=0)
    natural_update_ids: tuple[str, ...]
    gap_ranges: tuple[DateRange, ...]

class SourceUsePermission(StrEnum):
    PERMITTED = "permitted"
    RESTRICTED = "restricted"
    UNVERIFIED = "unverified"
    EXPIRED = "expired"

class ApprovedSourceBudget(BaseModel):
    vendor: MoneyValue | None
    api: MoneyValue | None
    storage: MoneyValue | None
    extraction: MoneyValue | None
    human_review: MoneyValue | None

class RightsApproval(BaseModel):
    rights_approval_id: str
    source: DataSource
    environment: Literal["local", "github_ci", "staging", "production"]
    raw_retention: SourceUsePermission
    caching: SourceUsePermission
    derived_metrics: SourceUsePermission
    report_publication: SourceUsePermission
    card_publication: SourceUsePermission
    quotation: SourceUsePermission
    attribution_requirement: str | None
    approved_budget: ApprovedSourceBudget
    valid_from: datetime
    expires_at: datetime
    reviewer: str
    evidence_ids: tuple[str, ...]
    signature_ref: str

class SourceCoverageEntry(BaseModel):
    requirement_id: str
    module_id: str
    domain: DataDomain
    field_semantics: str
    environment: Literal["local", "github_ci", "staging", "production"]
    subject_scope: tuple[SubjectRef, ...]
    identifier_level: Literal["issuer", "security", "listing", "analyst", "fund"]
    primary_source: DataSource
    fallback_sources: tuple[DataSource, ...]
    knowability_basis: Literal["source_publication", "independently_corroborated"]
    history_start: date
    history_end: date | None
    expected_cadence: timedelta
    rights_approval_ids: dict[DataSource, str]
    retention_policy_ref: str
    quota_policy_ref: str
    sla_policy_ref: str
    projected_budget: ApprovedSourceBudget
    capture_adapter_id: str
    revision_policy_ref: str
    fallback_policy_ref: str
    natural_refresh_observation_window: timedelta
    minimum_naturally_changed_partitions: int = Field(ge=1)
    refresh_alert_after: timedelta
    owner: str
    review_expires_at: datetime
    evidence_ids: tuple[str, ...]
    observed_coverage: tuple[SubjectSourceCoverage, ...]

class SourceCoverageCatalog(BaseModel):
    source_coverage_catalog_id: str
    entries: tuple[SourceCoverageEntry, ...]
    content_sha256: str

class ApplicabilityCell(BaseModel):
    module_id: str
    catalog_entry: CatalogAliasRef
    subject: SubjectRef
    requirement: Literal["required", "optional", "not_applicable"]
    reason_code: str
    effective_from: datetime

class ApplicabilityCatalog(BaseModel):
    applicability_catalog_id: str
    research_catalog_id: str
    research_catalog_sha256: str
    universe: UniverseRef
    cells: tuple[ApplicabilityCell, ...]
    approved_at: datetime
    approved_by: str
    approval_signature_ref: str
    supersedes_catalog_id: str | None
    change_reason: str
    content_sha256: str

class ModuleSlo(BaseModel):
    module_id: str
    universe: UniverseRef
    applicability_catalog_id: str
    applicability_catalog_sha256: str
    required_catalog_entries: tuple[CatalogAliasRef, ...]
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
    evaluator_id: str
    evaluator_version: str
    evaluator_artifact_digest: str
    evaluated_at: datetime
    input_content_sha256: tuple[str, ...]
    checks: tuple[ReadinessCheck, ...]
    content_sha256: str
    signature_ref: str

    @computed_field
    @property
    def ready(self) -> bool: ...

class SourceCoverageEvaluator(Protocol):
    def evaluate(
        self,
        catalog: SourceCoverageCatalog,
        *,
        as_of: datetime,
        evidence: Mapping[str, EvidenceCase],
        rights: Sequence[RightsApproval],
    ) -> ReadinessReport: ...

class SloEvaluator(Protocol):
    def evaluate(
        self,
        catalog: SloCatalog,
        applicability: ApplicabilityCatalog,
        *,
        window: DateRange,
        observations: Sequence[SloObservation],
        consumer_observations: Sequence[ConsumerSloObservation],
    ) -> ReadinessReport: ...

def evaluate_source_readiness(
    catalog: SourceCoverageCatalog,
    *,
    rights: Sequence[RightsApproval],
    evidence: Mapping[str, EvidenceCase],
    as_of: datetime,
    evaluator: SourceCoverageEvaluator,
) -> ReadinessReport: ...

class CaptureRequirement(BaseModel):
    requirement_id: str
    domain: DataDomain
    required_fields: tuple[str, ...]
    subject_kinds: frozenset[SubjectKind]
    cadence: timedelta
    partition_rule_id: str
    freshness_policy_id: str
    quality_policy_ids: tuple[str, ...]
    source_coverage_entry_ids: tuple[str, ...]

class CaptureScope(BaseModel):
    capture_scope_id: str
    environment: Literal["local", "github_ci", "staging", "production"]
    research_catalog_id: str
    research_catalog_sha256: str
    universe: UniverseRef
    applicability_catalog_id: str
    applicability_catalog_sha256: str
    source_coverage_catalog_id: str
    source_coverage_catalog_sha256: str
    slo_catalog_id: str
    slo_catalog_sha256: str
    requirements: tuple[CaptureRequirement, ...]
    effective_at: datetime
    owner: str
    content_sha256: str

class CaptureRecordEvidence(BaseModel):
    raw_id: str
    raw_sha256: str
    normalized_id: str
    knowable_at: datetime
    recorded_at: datetime
    confidence: Decimal = Field(ge=0, le=1)
    mapping_version: str
    policy_versions: dict[str, str]
    quality_check_ids: tuple[str, ...]
    lineage_sha256: str

class CaptureCell(BaseModel):
    subject: SubjectRef
    domain: DataDomain
    partition_key: str
    requirement_id: str
    applicability: Literal["required", "optional", "not_applicable"]
    status: Literal[
        "complete", "optional", "not_applicable", "missing", "stale", "unresolved", "error"
    ]
    evidence: tuple[CaptureRecordEvidence, ...]
    reason_codes: tuple[str, ...]

class CaptureManifest(BaseModel):
    capture_manifest_id: str
    capture_scope_id: str
    capture_scope_sha256: str
    environment: Literal["local", "github_ci", "staging", "production"]
    partition_key: str
    as_of: datetime
    cells: tuple[CaptureCell, ...]
    created_at: datetime
    content_sha256: str

class CaptureEvaluator(Protocol):
    def evaluate(
        self,
        scope: CaptureScope,
        manifest: CaptureManifest,
        *,
        applicability: ApplicabilityCatalog,
        as_of: datetime,
    ) -> ReadinessReport: ...

class ModelRevisionRef(BaseModel):
    provider: str
    model_id: str
    immutable_revision: str
    endpoint_or_artifact_sha256: str
    decoding_parameters_sha256: str

class ExtractionTemplate(BaseModel):
    extraction_template_id: str
    extractor_id: str
    extractor_version: str
    model_revision: ModelRevisionRef
    parameters_sha256: str
    prompt_sha256: str
    schema_sha256: str
    content_sha256: str

class ExtractionInvocation(BaseModel):
    extraction_invocation_id: str
    template: ExtractionTemplate
    source_document_semantic_sha256: str
    attempt_id: str
    content_sha256: str

class ReleaseManifest(BaseModel):
    release_manifest_id: str
    contract_version: str
    mart_schema_version: str
    research_catalog_id: str
    research_catalog_sha256: str
    universe: UniverseRef
    capture_scope_id: str
    capture_scope_sha256: str
    applicability_catalog_id: str
    applicability_catalog_sha256: str
    source_coverage_catalog_id: str
    source_coverage_catalog_sha256: str
    source_readiness_report_sha256: str
    slo_catalog_id: str
    slo_catalog_sha256: str
    configuration_sha256: dict[str, str]
    migration_ids: tuple[str, ...]
    migration_set_sha256: str
    artifacts: tuple[ReleaseArtifact, ...]
    accepted_evidence_ids: tuple[str, ...]
    accepted_capture_manifest_sha256: tuple[str, ...]
    accepted_readiness_report_sha256: tuple[str, ...]
    approved_model_revisions: tuple[ModelRevisionRef, ...]
    approved_extraction_template_ids: tuple[str, ...]
    created_at: datetime
    manifest_sha256: str
    manifest_signature_ref: str

class ReleaseManifestRepository(Protocol):
    def put(self, manifest: ReleaseManifest) -> PutResult: ...
    def get(self, release_manifest_id: str) -> ReleaseManifest | None: ...

class ResolvedFactorInvocation(BaseModel):
    template: FactorInvocationTemplate
    definition_sha256: str
    parameters_sha256: str
    execution_id: str
    snapshot_id: str
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
        inputs: TrackedFactorInputView,
        parameters: P,
    ) -> None: ...

def project_factor_inputs(
    snapshot: ResearchSnapshot,
    *,
    domains: frozenset[DataDomain],
    subjects: Sequence[SubjectRef],
) -> FactorInputBundle: ...

def run_factor(
    definition: FactorDefinition[P],
    *,
    template: FactorInvocationTemplate,
    snapshot: ResearchSnapshot,
    subjects: Sequence[SubjectRef],
    upstream: Mapping[str, FactorOutputBatch] | None = None,
) -> FactorOutputBatch: ...

class FactorRegistry(Protocol):
    def resolve(self, factor_id: str, factor_version: str) -> FactorDefinition[Any]: ...
```

`ReadinessReport.ready` is the deterministic conjunction of its checks; evaluators expose
no override/flag setter. A required capture cell is `complete` only when it has at least
one valid raw-to-normalized evidence row with mandatory confidence, eligible times,
mapping/policy versions, passing quality checks, and a resolvable lineage hash. Optional
or not-applicable status comes only from the pre-approved `ApplicabilityCatalog`; a run
cannot relabel or omit a required cell. A release binds exact source-coverage, SLO,
research catalog, universe, capture scope/manifests, applicability, configuration,
migration, artifact, evidence, model revision, extraction template, and accepted
readiness hashes.

`run_factor` owns generic validation: definition/parameter compatibility, declared
domains, entity scope, one snapshot/cutoff, upstream dependency completeness, output
identity, canonical ordering, deterministic template/execution/output/batch IDs,
consumption references,
and confidence ceilings. It is the only computation-layer function that sees the raw
bundle and materialized upstream batches. Module code receives `TrackedFactorInputView`;
every selector and upstream access is recorded inside an output scope, and only
`scope.emit` may create an output draft. Projection derives each opaque `InputId`
deterministically from the snapshot
domain and selected normalized record ID; it never copies source or raw references.
After computation, `run_factor` derives `source_evidence_status` from the consumed
manifest records and stamps `factor_validation_status` from the immutable registry entry;
module logic cannot branch on either status.
The resolved definition hash binds `validation_record_id` and `oracle_version`, so a later
holdout graduation creates a new append-only registry/materialization identity rather than
mutating old outputs or changing a batch under the same key.
`invocation_template_id` is the hash of factor/version, canonical parameters, and
dependency templates. `execution_id` additionally binds snapshot ID, ordered subject
scope, and dependency batch IDs. Alias equality never implies template or execution
equality. Repository keys use `execution_id`, never
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
    adjustment_mode: PriceAdjustmentMode

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
    record_date: date | None
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
    invocation: ExtractionInvocation
    source_document_record_id: str
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
    revision: ModelRevisionRef
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
    invocation: ExtractionInvocation
    source_document_record_id: str
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
row with `extraction_id`. The template binds the immutable provider revision, decoding
settings, extractor, parameters, prompt, and schema; the invocation additionally binds the source
document semantic hash and a unique attempt. A rerun or changed provider deployment is a
new append-only invocation, even when the public model alias is unchanged. Only templates
and model revisions approved by the exact release may execute. The record preserves that
invocation, the source-document record/version, exact evidence spans, and draft-to-record
links. Replay consumes the produced rows and never calls a model implicitly; trace reads follow
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
    inputs: TrackedFactorInputView,
    parameters: PegParameters,
) -> None: ...
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

class FinancialOperatingEfficiencyPolicy(BaseModel):
    policy_id: str
    policy_version: str
    issuer_classification_policy_id: str
    numerator_metric: str
    denominator_metric: str
    output_metric: str
    comparison_universe: UniverseRef

class GrossProfitPerEmployeeParameters(FactorParameters):
    non_financial_gross_profit_metric: str
    headcount_metric: str
    financial_policy: FinancialOperatingEfficiencyPolicy
    max_period_gap: timedelta

def extract_headcount(
    document: FactorDocumentInput,
    *,
    invocation: ExtractionInvocation,
    parameters: HeadcountExtractionParameters,
    model: StructuredExtractionModel,
) -> ExtractionResult: ...

def compute_gross_profit_per_employee(
    context: FactorExecutionContext,
    inputs: TrackedFactorInputView,
    parameters: GrossProfitPerEmployeeParameters,
) -> None: ...
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
    *,
    invocation: ExtractionInvocation,
    parameters: SupplyChainExtractionParameters,
    model: StructuredExtractionModel,
) -> ExtractionResult: ...

def compute_supply_chain_exposure(
    context: FactorExecutionContext,
    inputs: TrackedFactorInputView,
    parameters: SupplyChainReasoningParameters,
) -> None: ...
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
    inputs: TrackedFactorInputView,
    parameters: AnalystBacktestParameters,
) -> None: ...
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
    inputs: TrackedFactorInputView,
    parameters: EtfVirtualCompanyParameters,
) -> None: ...
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
    inputs: TrackedFactorInputView,
    parameters: PureBloodParameters,
) -> None: ...

def extract_theme_segments(
    document: FactorDocumentInput,
    *,
    invocation: ExtractionInvocation,
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

class FinancialComparisonPolicy(BaseModel):
    policy_id: str
    policy_version: str
    operating_efficiency_policy_id: str
    operating_efficiency_policy_version: str
    comparison_universe: UniverseRef
    bands: tuple[ValuationBand, ...]

class LevelLeverageRule(BaseModel):
    rule_type: Literal["level"] = "level"
    thresholds: tuple[Decimal, Decimal]

class ElasticityLeverageRule(BaseModel):
    rule_type: Literal["elasticity"] = "elasticity"
    window_periods: int = Field(ge=2)
    minimum_periods: int = Field(ge=2)
    estimator_id: str
    thresholds: tuple[Decimal, Decimal]

class CombinedLeverageRule(BaseModel):
    rule_type: Literal["combined"] = "combined"
    level: LevelLeverageRule
    elasticity: ElasticityLeverageRule
    combination_rule_id: str

LeverageRule = Annotated[
    LevelLeverageRule | ElasticityLeverageRule | CombinedLeverageRule,
    Field(discriminator="rule_type"),
]

class ThreeTierValuationParameters(FactorParameters):
    leverage_rule: LeverageRule
    non_financial_bands: tuple[ValuationBand, ValuationBand, ValuationBand]
    financial_comparison: FinancialComparisonPolicy
    minimum_confidence: Decimal = Field(ge=0, le=1)

class PriceToSalesParameters(FactorParameters):
    revenue_metric: str
    shares_metric: str
    revenue_basis: Literal["ttm", "latest_fiscal_year"]
    shares_basis: Literal["period_end_diluted", "latest_basic"]
    security_policy: Literal["primary_common_equity"] = "primary_common_equity"
    price_field: Literal["close"] = "close"
    listing_policy: Literal["primary_listing"] = "primary_listing"
    security_unit_conversion_policy_id: str
    valuation_currency: CurrencyCode
    maximum_price_age: timedelta
    maximum_fx_age: timedelta
    maximum_period_gap: timedelta

def compute_price_to_sales(
    context: FactorExecutionContext,
    inputs: TrackedFactorInputView,
    parameters: PriceToSalesParameters,
) -> None: ...

def compute_three_tier_valuation(
    context: FactorExecutionContext,
    inputs: TrackedFactorInputView,
    parameters: ThreeTierValuationParameters,
) -> None: ...
```

Price-to-sales resolves issuer -> security -> primary listing at the cutoff, validates
that outstanding shares belong to the selected security, and applies the frozen
share-class/ADR unit ratio before multiplying by the listing price. It validates units
and converts revenue/market capitalization only through explicit `FactorFxInput` records;
missing links, conversion ratios, or stale FX reject the value. The composite consumes
gross-profit-per-employee and price-to-sales batches from the
same snapshot/cutoff. It emits tier, band, valuation gap, eligibility, and flags. Bands
and thresholds are research parameters, not performance-validated constants. The
3-4x/8-10x/20-30x ranges in `vision.md` are illustrative research anchors, not executable
defaults; #59 must freeze explicit v1 values and independent boundary oracles before a
three-tier invocation can graduate beyond `UNVALIDATED`. It must also freeze exactly one
level, elasticity, or combined leverage rule. Applicable financial issuers use the
versioned comparison branch, which must reference the exact operating-efficiency policy
used by module 2; blanket exclusion is not a valid policy.

### 14.10 Screens, Strategy, and Replay

```python
class ScreenInvocation(BaseModel):
    screen_id: str
    screen_version: str
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    factor_inputs: dict[str, str]  # screen slot -> factor template alias

class StrategyDefinition(BaseModel):
    strategy_id: str
    strategy_version: str
    universe: UniverseRef
    factors: tuple[FactorInvocationTemplate, ...]
    screen: ScreenInvocation
    rebalance_rule: RuleInvocation
    sizing_rule: RuleInvocation
    holding_rule: RuleInvocation
    return_rule: RuleInvocation
    return_policy: ReturnPolicy

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
        templates: Sequence[FactorInvocationTemplate],
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
        execution_id: str,
    ) -> FactorOutputBatch | None: ...

class MaterializedFactorOutputRepository(Protocol):
    def get_batch(
        self, *, execution_id: str
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

class FactorInvocationTemplateSelector(BaseModel):
    invocation_alias: str
    invocation_template_id: str
    factor_id: str
    factor_version: str
    parameters_sha256: str

class FactorCatalogTarget(BaseModel):
    target_type: Literal["factor"] = "factor"
    selector: FactorInvocationTemplateSelector

class RankingCatalogTarget(BaseModel):
    target_type: Literal["ranking"] = "ranking"
    screen_id: str
    screen_version: str
    parameters_sha256: str

class ThemeCatalogTarget(BaseModel):
    target_type: Literal["theme"] = "theme"
    theme_id: str
    theme_version: str
    factor_selector: FactorInvocationTemplateSelector
    ranking_alias: str

class ScenarioCatalogTarget(BaseModel):
    target_type: Literal["scenario"] = "scenario"
    scenario_id: str
    scenario_version: str
    factor_selector: FactorInvocationTemplateSelector

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
    universe: UniverseRef
    published_at: datetime

class CanonicalQuestion(BaseModel):
    question_id: str
    question_version: str
    prompt_examples: tuple[str, ...]
    query_kind: Literal["history", "comparison", "ranking", "strategy", "trace"]
    catalog_aliases: tuple[CatalogAliasRef, ...]
    required_output_types: frozenset[str]
    required_subject_kinds: frozenset[SubjectKind]
    required_subjects: tuple[SubjectRef, ...] = ()
    required_universe: UniverseRef | None = None
    argument_contract_sha256: str
    expected_status_policy_ref: str

class ResearchScopeFloor(BaseModel):
    minimum_issuers: int = Field(ge=1)
    minimum_funds: int = Field(ge=1)
    minimum_themes: int = Field(ge=1)
    minimum_analysts: int = Field(ge=1)
    minimum_scenarios: int = Field(ge=1)
    minimum_screens: int = Field(ge=1)
    minimum_strategies: int = Field(ge=1)
    required_entry_aliases: tuple[str, ...]
    required_question_ids: tuple[str, ...]
    approved_by: str
    approval_signature_ref: str

class ResearchCatalogManifest(BaseModel):
    catalog_id: str
    entries: tuple[ResearchCatalogEntry, ...]
    questions: tuple[CanonicalQuestion, ...]
    scope_floor: ResearchScopeFloor
    content_sha256: str
    published_at: datetime

class ResearchCatalog(Protocol):
    def get(self, catalog_id: str) -> ResearchCatalogManifest | None: ...
    def resolve(
        self, catalog_id: str, ref: CatalogAliasRef
    ) -> ResearchCatalogEntry: ...

class CatalogQuery(BaseModel):
    target_type: Literal["factor", "ranking", "theme", "scenario", "strategy"] | None = None
    page: PageRequest = Field(default_factory=PageRequest)

class CatalogResult(BaseModel):
    catalog_id: str
    content_sha256: str
    entries: tuple[ResearchCatalogEntry, ...]
    next_cursor: str | None

class HistoryQuery(BaseModel):
    query_kind: Literal["history"] = "history"
    item: CatalogAliasRef  # factor, theme, or scenario target
    subjects: tuple[SubjectRef, ...]
    observed_range: DateRange
    as_of: datetime
    page: PageRequest = Field(default_factory=PageRequest)

class HistoryResult(BaseModel):
    result_kind: Literal["history"] = "history"
    catalog_entry: ResearchCatalogEntry
    observations: tuple[FactorObservation, ...]
    next_cursor: str | None

class EntityComparisonQuery(BaseModel):
    query_kind: Literal["comparison"] = "comparison"
    factors: tuple[CatalogAliasRef, ...]
    subjects: tuple[SubjectRef, ...]
    observed_on: date
    as_of: datetime

class EntityComparison(BaseModel):
    result_kind: Literal["comparison"] = "comparison"
    catalog_entries: tuple[ResearchCatalogEntry, ...]
    observations: tuple[FactorObservation, ...]

class RankingQuery(BaseModel):
    query_kind: Literal["ranking"] = "ranking"
    ranking: CatalogAliasRef
    as_of: datetime
    page: PageRequest = Field(default_factory=PageRequest)

class RankingResult(BaseModel):
    result_kind: Literal["ranking"] = "ranking"
    catalog_entry: ResearchCatalogEntry
    screen_result_id: str
    candidates: tuple[ScreenCandidate, ...]
    next_cursor: str | None

class StrategyRunQuery(BaseModel):
    query_kind: Literal["strategy"] = "strategy"
    strategy: CatalogAliasRef
    run_id: str

class TraceOutputQuery(BaseModel):
    query_kind: Literal["trace"] = "trace"
    output_id: OutputId

CanonicalQuestionQuery = Annotated[
    HistoryQuery | EntityComparisonQuery | RankingQuery | StrategyRunQuery | TraceOutputQuery,
    Field(discriminator="query_kind"),
]

class CanonicalQuestionRequest(BaseModel):
    question_id: str
    question_version: str | None = None
    query: CanonicalQuestionQuery

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
    invocation: ExtractionInvocation
    source_document_record_id: str
    evidence_spans: tuple[EvidenceSpan, ...]
    produced_record_ids: tuple[str, ...]

class TraceabilityView(BaseModel):
    output_id: OutputId
    template: FactorInvocationTemplate
    execution_id: str
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

class StrategyRunQueryResult(BaseModel):
    result_kind: Literal["strategy"] = "strategy"
    catalog_entry: ResearchCatalogEntry
    run: StrategyRunView

class TraceOutputQueryResult(BaseModel):
    result_kind: Literal["trace"] = "trace"
    trace: TraceabilityView

CanonicalQuestionPayload = Annotated[
    HistoryResult | EntityComparison | RankingResult
    | StrategyRunQueryResult | TraceOutputQueryResult,
    Field(discriminator="result_kind"),
]

class CanonicalQuestionResult(BaseModel):
    question: CanonicalQuestion
    catalog_id: str
    catalog_sha256: str
    release_manifest_id: str
    resolved_entries: tuple[ResearchCatalogEntry, ...]
    payload: CanonicalQuestionPayload

class ResearchReadRepository(Protocol):
    def catalog(self, query: CatalogQuery) -> CatalogResult: ...
    def history(self, query: HistoryQuery) -> HistoryResult: ...
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
    capture_scope_id: str
    capture_scope_sha256: str
    requirement_ids: tuple[str, ...]
    subjects: tuple[SubjectRef, ...]
    request_parameters: dict[str, JsonValue]

class NormalizationAssetSpec(BaseModel):
    asset_key: str
    capture_asset_key: str
    normalizer_id: str

class ExtractionAssetSpec(BaseModel):
    asset_key: str
    document_asset_key: str
    template: ExtractionTemplate
    model_resource_key: str

class CaptureManifestAssetSpec(BaseModel):
    asset_key: str
    capture_scope_id: str
    capture_scope_sha256: str
    capture_asset_keys: tuple[str, ...]
    normalized_asset_keys: tuple[str, ...]
    extraction_asset_keys: tuple[str, ...] = ()

class SnapshotAssetSpec(BaseModel):
    asset_key: str
    capture_manifest_asset_key: str
    normalized_asset_keys: tuple[str, ...]
    universe: UniverseRef
    domains: frozenset[DataDomain]

class FactorAssetSpec(BaseModel):
    asset_key: str
    snapshot_asset_key: str
    template: FactorInvocationTemplate
    materialized_upstream_asset_keys: dict[str, str] = Field(default_factory=dict)

class StrategyAssetSpec(BaseModel):
    asset_key: str
    definition: StrategyDefinition
    factor_asset_keys: tuple[str, ...]

@dataclass(frozen=True)
class DagsterAssetCatalog:
    release_manifest_id: str
    execution_artifact_digest: str
    capture_scope: CaptureScope
    applicability: ApplicabilityCatalog
    source_coverage: SourceCoverageCatalog
    slo: SloCatalog
    capture: tuple[CaptureAssetSpec, ...]
    normalization: tuple[NormalizationAssetSpec, ...]
    extraction: tuple[ExtractionAssetSpec, ...]
    capture_manifest: CaptureManifestAssetSpec
    snapshots: tuple[SnapshotAssetSpec, ...]
    factors: tuple[FactorAssetSpec, ...]
    strategies: tuple[StrategyAssetSpec, ...]
    partitions_def: PartitionsDefinition

def build_capture_asset(spec: CaptureAssetSpec) -> AssetsDefinition: ...
def build_normalization_asset(spec: NormalizationAssetSpec) -> AssetsDefinition: ...
def build_extraction_asset(spec: ExtractionAssetSpec) -> AssetsDefinition: ...
def build_capture_manifest_asset(
    spec: CaptureManifestAssetSpec,
) -> AssetsDefinition: ...
def build_capture_readiness_check(
    spec: CaptureManifestAssetSpec,
) -> AssetChecksDefinition: ...
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
    universe: UniverseRef
    release_manifest_id: str
    execution_artifact_digest: str

def build_strategy_schedule(spec: StrategyScheduleSpec) -> ScheduleDefinition: ...
def build_release_preflight_sensor(
    *, catalog: DagsterAssetCatalog, release: ReleaseManifest
) -> SensorDefinition: ...
```

The bounded TOPT implementation establishes three reusable runtime rules. First,
each run persists its scope, release-manifest ID, image digest, and configuration
hash before the first source call and rejects a later mismatch. Second,
source/subject/domain/partition/attempt results are append-only; retry exhaustion
persists confidence-zero failure evidence for the manifest instead of erasing the
failed attempt. Third, semantic evidence versions exclude run ID, retry number,
and ingestion-clock identity, so an identical retry has the same data version.

The manifest is built only from persisted source results and raw checksums. A
successful empty result is valid only for explicitly allowed event/assertion
domains, retains the raw query observation, and creates no fabricated domain
record. The current blocking Dagster asset check fails whenever the frozen cell
set and persisted manifest do not pass.

The concrete TOPT adapter currently predates the complete public models above; it
must be migrated to the exact `UniverseRef`, Research Catalog, applicability,
source-readiness, policy-version, quality-check, and lineage-hash bindings rather
than redefining those contracts.

Dagster is introduced with the first executable snapshot/factor slice. Local and CI use
in-process jobs and fixture resources; Staging and Production add schedules and persistent
metadata. `adapter_id` and `normalizer_id` resolve from Dagster resources at execution;
service instances are never captured inside asset definitions. The shared partition is an
aware `as_of` cutoff; capture scope, universe, and invocation are explicit asset-spec
dimensions. Capture and normalization assets may persist failure evidence, but no snapshot,
factor, or strategy partition may materialize until the row-complete capture-manifest
check passes for that exact scope and partition. A
composite `FactorAssetSpec` must depend on materialized upstream asset keys and reload
those batches from mart. Factor `data_version` is the hash of snapshot ID, `execution_id`,
and upstream batch IDs. The release preflight sensor re-evaluates rights expiry, projected
budget, source coverage, catalog hashes, artifact digests, and every extraction template's
immutable model revision; failure blocks source execution and downstream publication.
No alternative scheduler may launch real source runs.
The data-engine/Dagster code location is an immutable digest in the multi-artifact
`ReleaseManifest`; definitions and schedules reject a digest mismatch or floating tag.
Promotion moves the complete signed manifest, not an assumed single image.

### 14.13 Reports, MCP, App, and Chat

```python
class ReportItemRequest(BaseModel):
    item: CatalogAliasRef  # factor, ranking, theme, scenario, or strategy alias
    subjects: tuple[SubjectRef, ...] = ()

class ResearchReportRequest(BaseModel):
    subjects: tuple[SubjectRef, ...]
    universe: UniverseRef
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
    release_manifest_id: str
    catalog_id: str
    catalog_sha256: str
    universe: UniverseRef
    as_of: datetime
    sections: tuple[ReportSection, ...]
    traceability: tuple[TraceabilityView, ...]

def build_research_report(
    request: ResearchReportRequest,
    *,
    query_service: "ResearchQueryService",
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
    def __init__(
        self,
        repository: ResearchReadRepository,
        *,
        catalog: ResearchCatalogManifest,
        release_manifest_id: str,
    ) -> None: ...
    def catalog(self, request: CatalogQuery) -> CatalogResult: ...
    def history(self, request: HistoryQuery) -> HistoryResult: ...
    def compare_entities(self, request: EntityComparisonQuery) -> EntityComparison: ...
    def rank_entities(self, request: RankingQuery) -> RankingResult: ...
    def explain_output(self, request: TraceOutputQuery) -> TraceOutputQueryResult: ...
    def strategy_run(self, request: StrategyRunQuery) -> StrategyRunQueryResult: ...
    def canonical_question(
        self, request: CanonicalQuestionRequest
    ) -> CanonicalQuestionResult: ...

async def mcp_catalog(
    request: CatalogQuery, *, service: ResearchQueryService
) -> CatalogResult: ...
async def mcp_history(
    request: HistoryQuery, *, service: ResearchQueryService
) -> HistoryResult: ...
async def mcp_compare_entities(
    request: EntityComparisonQuery, *, service: ResearchQueryService
) -> EntityComparison: ...
async def mcp_rank_entities(
    request: RankingQuery, *, service: ResearchQueryService
) -> RankingResult: ...
async def mcp_explain_output(
    request: TraceOutputQuery, *, service: ResearchQueryService
) -> TraceOutputQueryResult: ...
async def mcp_strategy_run(
    request: StrategyRunQuery, *, service: ResearchQueryService
) -> StrategyRunQueryResult: ...
async def mcp_canonical_question(
    request: CanonicalQuestionRequest, *, service: ResearchQueryService
) -> CanonicalQuestionResult: ...

class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None

class ChatRequest(BaseModel):
    conversation_id: str
    messages: tuple[ChatMessage, ...]
    universe: UniverseRef
    as_of: datetime

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
  history(query: HistoryQuery): Promise<HistoryResult>;
  entityComparison(query: EntityComparisonQuery): Promise<EntityComparison>;
  ranking(query: RankingQuery): Promise<RankingResult>;
  strategyRun(query: StrategyRunQuery): Promise<StrategyRunQueryResult>;
  traceOutput(query: TraceOutputQuery): Promise<TraceOutputQueryResult>;
  canonicalQuestion(query: CanonicalQuestionRequest): Promise<CanonicalQuestionResult>;
}

export function createMartResearchRepository(
  sql: SqlExecutor,
  options: {
    maxRows: number;
    statementTimeoutMs: number;
    releaseManifestId: string;
    catalogId: string;
    catalogSha256: string;
  },
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
Public consumers use versioned `CatalogAliasRef` aliases such as a named PEG convention,
theme ranking, or supply scenario; they never construct parameter hashes or internal
materialization keys. Every repository and service instance is bound at startup to the
exact catalog ID/hash in its signed release manifest; an omitted `entry_version` resolves
only inside that bound catalog, never through a global current pointer. A catalog or
release mismatch fails startup. Catalog publication is append-only and fails when its
entries/questions do not satisfy the product-owner-approved `ResearchScopeFloor`.
Each canonical question binds concrete subjects or an immutable universe whenever
subject-kind-only dispatch could permit a smaller scope. Each response returns the
resolved entry and immutable catalog ID/hash so a conversational answer
remains reconstructible after an alias advances. Each target binds its exact `UniverseRef`
while the enclosing release binds the downstream applicability and SLO catalogs, avoiding
a circular catalog hash; strategy aliases are catalog targets rather than magic run names.
`CanonicalQuestionRequest` is executable only when its query discriminator,
aliases, subject kinds, and output schema satisfy the frozen question contract.

## 15. Complete Vision Call Graph

```text
Dagster schedule / local in-process job
  -> resolve signed ReleaseManifest and immutable artifact digests
  -> verify exact Research Catalog -> Applicability -> SLO dependency chain
  -> verify UniverseRef, CaptureScope, SourceCoverageCatalog, extraction templates,
     immutable model revisions, and runtime artifact digest against the release
  -> re-evaluate environment rights, projected budget, coverage, and expiry as of run time
  -> enumerate every required CaptureScope cell for the partition
  -> SourceCallGateway.execute(SourceAdapter.capture) -> immutable object + raw.fetches
  -> Normalizer.normalize -> append-only staging records
  -> optional versioned extraction invocation -> semantic drafts
     -> data-engine atomically attaches evidence lineage -> append-only staging records
  -> materialize CaptureManifest for every required/optional/not-applicable cell
  -> CaptureEvaluator.evaluate -> signed readiness; fail before snapshot on any required gap
  -> MetricRegistry + select_canonical_facts(source priority, mapping/fusion version)
  -> ResearchSnapshotRepository.build_snapshot(one as_of, exact PIT UniverseRef and links)
  -> SnapshotStore.put
  -> project_factor_inputs(strips provenance)
  -> run_factor(base templates 1-6 and supporting metrics) -> execution IDs + automatic lineage
  -> FactorOutputRepository -> MartMaterializer
  -> reload materialized upstream -> run_factor(composite template 7)
  -> FactorOutputRepository -> MartMaterializer
  -> ScreenDefinition.evaluate
  -> evaluate_strategy_at
  -> simulation clock consumes newly available market events -> run_backtest
  -> StrategyRunRepository -> MartMaterializer
  -> SloEvaluator.evaluate(pre-approved applicability denominator, natural refreshes)
  -> publish only when signed readiness and release hashes agree
  -> release-bound ResearchQueryService / direct-mart repository
     -> ResearchReport -> report-card / Xiaohongshu renderers
     -> canonical typed MCP questions -> Claude/other MCP clients
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
semantics; freeze the Research Catalog, `CaptureScope`, row-complete manifest,
applicability denominator, independent research oracles, immutable extraction revisions,
longitudinal source coverage, expiring use rights/budgets, natural-refresh rules, and
graduation SLOs. Tracked by
[epic #56](https://github.com/wangzitian0/truealpha/issues/56); its issue tree must own
each of those artifacts rather than leave them implicit in a later implementation issue.
Section 18 is the executable interface portion of this gate.

### 16.1 Gate 1: Core Strategy MVP

Deliver PIT snapshots, early Dagster composition, gross profit per employee, three-tier
valuation, `large_model_value_v0`, deterministic local replay, mart/report projection,
and a real scheduled Staging canary. Completion proves the bounded core slice can execute
idempotently under Dagster and produce a row-complete manifest for its frozen TOPT scope;
it does not establish continuous all-module coverage,
Production readiness, or complete Vision delivery.
Tracked by [epic #29](https://github.com/wangzitian0/truealpha/issues/29), with
#14, #21-#27, and #70-#71 as sub-issues. #70 owns the narrow document-to-headcount
data plane; #71 owns the independent Core holdout before local replay.

### 16.2 Gate 2: Seven Research Modules

Implement PEG's three conventions, analyst track records, ETF virtual-company metrics,
supply-chain extraction and versioned scenario exposure with the confidence kill
condition, and pure-blood theme ranking. Build the forecast/analyst and PIT
ETF/instrument data planes in #62-#63, the generic filing/extraction-result substrate in
#64, and the domain candidates in #37/#39. Run one shared
seven-module replay, materialize every output, and pass the independent sealed holdout in
#65. Every LLM-assisted path uses stored release-approved extraction results, and
high-confidence edges alone never justify a causal claim.
Tracked by [epic #30](https://github.com/wangzitian0/truealpha/issues/30), with
#33-#40 and #62-#65 as delivery and graduation issues.

### 16.3 Gate 3: Research Consumption

Freeze mart read models, expose typed MCP tools, generate traceable personal report
cards and Xiaohongshu card artifacts, add the App dashboard, and finally add `/chat`
as a tool-orchestration surface. Completion proves every Vision question can be answered
through a typed canonical question bound to the release catalog and exact universe, from
mart with a filing/vintage trace. A new issuer and theme must be onboardable through new
catalog/scope versions without factor or consumer code changes.
Tracked by [epic #31](https://github.com/wangzitian0/truealpha/issues/31), with
#41-#46, #48, and #72 as sub-issues. #72 is the configuration-only unseen
issuer/theme onboarding proof.

### 16.4 Gate 4: Production Validation and Graduation

Extend the evaluation corpus to five years/multiple regimes, reconcile critical prices
against an independent source, validate strategy direction against a known reference,
schedule all seven modules in Staging, prove backup/restore and alerting, then promote
the exact signed multi-artifact release manifest to isolated Production shadow operation
with explicit approval.
Validate the deployed Production MCP, App, chat, report, and card paths against the same
mart outputs in #66. Expand to the owned curated universe and graduate shadow outputs only
after the natural-refresh soak, per-module SLOs, traceability, recovery, and recorded human
approval pass in #67. The Production capture audit in #68 must then prove every required
cell in that graduated scope has complete raw-to-normalized lineage; a green Dagster run
or raw-only payload count is insufficient.
Tracked by [epic #32](https://github.com/wangzitian0/truealpha/issues/32), with
#11, #49-#54, #66-#68 as the environment, evaluation, consumer, graduation, and final
capture-certification tree.

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
   only when independent causal evidence exists. Financial and non-financial operating
   efficiency branches and the selected leverage rule have separate boundary evidence.
2. The owned curated Production universe has graduated from shadow operation. Dagster is
   its only scheduler, and every applicable module meets its versioned usable-coverage,
   freshness, and traceability SLO across natural source refreshes; unavailable, stale,
   unresolved, excluded, low-confidence, and error outputs do not count as produced.
   The exact #68 Production capture audit proves every required scope cell has raw and
   normalized evidence, eligible times, confidence, mapping/policy versions, quality, and
   lineage under unexpired rights and approved budgets.
3. A user can ask every frozen canonical Vision question through the deployed Production
   MCP, App, and `/chat` paths and receive equivalent typed results from the exact
   release-bound catalog and universe, traceable to template/execution parameters,
   snapshot policy, filing/vintage or extraction evidence, and raw checksum.
4. The same mart outputs produce personal report cards and Xiaohongshu card artifacts
   without manual metric recomputation, and a previously unseen issuer/theme can traverse
   capture through all consumer surfaces by publishing new scope/catalog versions only.
5. Strategy evaluation uses at least five years, independent price reconciliation,
   survivorship-safe membership, corporate actions, immutable definitions, and a known
   strategy sanity result. Returns use raw bars plus explicit lifecycle events without
   double counting; no positive-alpha claim is required unless separately tested.
6. Production uses the exact Staging-tested signed release manifest, including the
   immutable data-engine/Dagster artifact, catalog, universe, capture/applicability/source/
   SLO hashes, model revisions, and extraction templates, with isolated credentials/
   storage, demonstrated backup/restore, append-only data, deployed-consumer evidence, a
   natural source-refresh soak, human card approval, and independent final sign-off.

No milestone may claim the full Vision based on fixture readiness, code existence,
manual flag changes, immediate repeated canary runs, or one successful happy-path run.

## 18. Semantic Closure Gate and Versioning

V1 is **proposed**, not frozen. The semantic closure gate passes only when all of these
are executable and reviewed:

1. Every public model in Section 14 builds JSON Schema with no unresolved, duplicate, or
   ambiguous type; generated Python/TypeScript discriminated unions agree.
2. Issuer/security/listing, share-class/ADR conversion, currency/time, and `ReturnPolicy`
   validators have positive and negative fixtures at historical cutoffs.
3. Fixture and Postgres repositories produce the same durable snapshot ID, exact selected
   record set, `UniverseRef`, identity links, policy versions, and lineage for one request;
   a wrong hash or mutable-latest substitution fails.
4. A competing-source/restatement test proves source-priority selection, changes the
   fusion ruleset, and still retrieves the exact original snapshot by ID.
5. A synthetic `CaptureScope` probe enumerates the full applicability cross-product and
   fails on a missing/duplicate cell, absent evidence component, stale row, post-run scope
   shrink, or manually asserted readiness despite a green upstream asset.
6. Source-readiness probes reject unverified/restricted/expired rights, approval expiry,
   over-budget projection, and false natural refreshes made from retries, unchanged bytes,
   fixtures, or synthetic mutations.
7. A synthetic extraction probe binds an approved immutable model revision and template,
   runs stored document -> semantic draft -> atomic extraction/row persistence -> snapshot,
   then replays without a model call and traces exact spans. A new attempt or model
   deployment produces a new immutable invocation and preserves the old result.
8. Two templates of one probe factor/version with different parameters and two executions
   with different snapshot/subject scopes coexist without repository, mart, Dagster asset,
   or query-key collisions.
9. A dummy base batch is persisted/materialized and a dummy composite reloads it from mart.
   Selector/upstream access automatically creates per-output lineage and confidence caps;
   factor code cannot construct final status or consumed IDs.
10. A synthetic replay probe excludes future bars from its decision snapshot, advances to
    the next eligible raw listing bar, converts an ADR/share class explicitly, applies
    split/dividend lifecycle events once, handles FX, and rejects adjusted/action mixing.
11. A dummy output projected into an ephemeral mart is traceable by a mart-only role through
    template/execution, snapshot policy, staging IDs, extraction evidence, and raw checksum.
12. Release-bound Python and TypeScript adapters agree on every canonical question's
    catalog resolution, concrete subject/universe scope, history/comparison/ranking/
    strategy/trace DTO, pagination, status, and lineage. Catalog publication fails below
    `ResearchScopeFloor`, and consumer startup fails for a catalog/release hash mismatch.
13. A signed multi-artifact release probe rejects a floating or mismatched runtime digest,
    universe, capture/applicability/source/SLO hash, accepted manifest/readiness report,
    extraction template, or model revision.
14. The frozen financial operating-efficiency branch and selected level/elasticity/combined
    leverage rule have independently reviewed semantics and boundary oracles; implementing
    the real GPPE/valuation candidates remains Gate 1 work.
15. A design review finds no remaining conflict with authoritative `init.md`, and every
    implementation issue names the closure probe and later gate evidence that prove its
    downstream boundary without depending on its own downstream verifier.

After this gate, the v1 freeze covers field semantics, discriminators, identity and time
meaning, confidence/lineage rules, port behavior, and registry identity. It does not
freeze storage schemas or private internals.

- Contracts use major/minor semantic `contract_version` values.
- Factor, screen, strategy, and rule versions are independent and immutable.
- Research Catalog -> Applicability -> SLO catalogs form a one-way immutable dependency
  chain; a signed release binds their exact content hashes with `UniverseRef` and
  `CaptureScope`.
- Extraction templates and immutable provider revisions are independently versioned;
  changing either always creates a new invocation and staging vintage.
- Removed/renamed fields, changed time meaning, confidence rules, or discriminators
  require a new major version.
- Formula, taxonomy, default threshold, or rule behavior changes require a new
  computation version even if contract shape is unchanged.
- Readers use explicit old-version adapters; writers emit only the current version.
- Persisted runs and consumer artifacts retain release/catalog/universe identities,
  definitions, parameters, execution IDs, and snapshot IDs permanently.
Until every gate passes, issue acceptance may validate an incremental slice but must not
describe these contracts as frozen or claim complete Vision closure.
These probes close semantics only. Real GPPE/strategy evidence belongs to Gate 1, real
seven-module and holdout evidence to Gate 2, deployed consumers to Gates 3/4, and natural
refresh/Production graduation to Gate 4.
