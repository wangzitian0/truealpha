# TrueAlpha Vision Delivery Architecture
Status: v1 delivery design; `init.md` remains authoritative.
## 1. Decision
Freeze this semantic pipeline before implementing more factor logic:
```text
RawCapture -> normalized PIT records -> ResearchSnapshot + DataSnapshotManifest
  -> FactorDefinition.compute -> FactorOutputBatch[Metric | Classification | Relationship]
  -> ScreenDefinition.evaluate -> StrategyDefinition -> BacktestRunResult
```
The freeze covers interface meaning, not every adapter; implementations may land incrementally.
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
### 4.1 Time Vocabulary
- `valid_from` / `valid_to`: when a statement is true in the real world.
- `knowable_at`: earliest corroborated time TrueAlpha permits it in replay.
- `recorded_at`: ingestion time; never a substitute for knowability.
- `as_of`: inclusive transaction-time cutoff supplied by the caller.
- `valid_on`: optional real-world date for interval membership.
- `observed_at`: date or period to which an output applies.
Datetimes are aware; repositories require `knowable_at <= as_of`. Restatements append.
### 4.2 Factor-Ready Facts
```python
class Fact(BaseModel):
    entity_id: str
    metric: str
    value: Decimal | None
    unit: str | None
    confidence: Decimal  # 0..1
    as_of: datetime
    observed_at: date | None = None
    fiscal_period: str | None = None
```
`Fact` has no provenance. Reconciliation occurs first; the manifest preserves lineage.
```python
class FactorRelationshipInput(BaseModel): from_entity_id: str; to_entity_id: str; relation_type: str; valid_from: date; valid_to: date | None; confidence: Decimal; as_of: datetime
class FactorRatingInput(BaseModel): analyst_id: str; entity_id: str; recommendation_at: datetime; rating: int; target_price: Decimal | None; currency: str | None; confidence: Decimal; as_of: datetime
class FactorHoldingInput(BaseModel): fund_id: str; entity_id: str; report_period: date; weight: Decimal; value: Decimal | None; confidence: Decimal; as_of: datetime
class FactorPriceInput(BaseModel): entity_id: str; trading_date: date; open: Decimal; high: Decimal; low: Decimal; close: Decimal; adjusted_close: Decimal; volume: int; confidence: Decimal; as_of: datetime
class FactorActionInput(BaseModel): entity_id: str; action_type: str; effective_date: date; ratio: Decimal | None; cash_amount: Decimal | None; currency: str | None; confidence: Decimal; as_of: datetime
class FactorMembershipInput(BaseModel): universe_id: str; entity_id: str; valid_from: date; valid_to: date | None; confidence: Decimal; as_of: datetime
class FactorDocumentInput(BaseModel): entity_id: str; document_type: str; text: str; published_at: datetime; confidence: Decimal; as_of: datetime
class FactorInputBundle(BaseModel):
    snapshot_id: str
    as_of: datetime
    facts: tuple[Fact, ...] = ()
    relationships: tuple[FactorRelationshipInput, ...] = ()
    ratings: tuple[FactorRatingInput, ...] = ()
    holdings: tuple[FactorHoldingInput, ...] = ()
    prices: tuple[FactorPriceInput, ...] = ()
    actions: tuple[FactorActionInput, ...] = ()
    memberships: tuple[FactorMembershipInput, ...] = ()
    documents: tuple[FactorDocumentInput, ...] = ()
```
The domain inputs expose only semantic values, valid period, confidence, and `as_of`.
They never contain `source`, `raw_ref`, accession, or repository metadata. The runner
projects this bundle from an auditable snapshot for the factor's declared domains.
### 4.3 Corporate Actions
```python
class CorporateActionType(StrEnum):
    SPLIT = "split"
    CASH_DIVIDEND = "cash_dividend"
    STOCK_DIVIDEND = "stock_dividend"
    SPINOFF = "spinoff"
    DELISTING = "delisting"
class CorporateAction(BaseModel):
    action_id: str
    entity_id: str
    action_type: CorporateActionType
    ex_date: date
    effective_date: date
    pay_date: date | None
    ratio: Decimal | None
    cash_amount: Decimal | None
    currency: str | None
    knowable_at: datetime
    recorded_at: datetime
    confidence: Decimal
    raw_ref: str
```
Validation requires a ratio for ratio actions and amount/currency for cash dividends.
Corrections append vintages.
### 4.4 Membership and Identifier History
```python
class UniverseMembership(BaseModel):
    universe_id: str
    entity_id: str
    valid_from: date
    valid_to: date | None
    knowable_at: datetime
    recorded_at: datetime
    confidence: Decimal
    raw_ref: str
class EntityIdentifier(BaseModel):
    entity_id: str
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
### 4.5 Snapshot and Manifest
```python
class ManifestEntry(BaseModel):
    domain: str
    record_count: int
    content_sha256: str
    min_knowable_at: datetime | None
    max_knowable_at: datetime | None
    raw_refs_sha256: str
class DataSnapshotManifest(BaseModel):
    snapshot_id: str
    contract_version: str
    query: AsOfQuery
    created_at: datetime
    repository_kind: str
    entries: tuple[ManifestEntry, ...]
    content_sha256: str
class ResearchSnapshot(BaseModel):
    manifest: DataSnapshotManifest
    facts: tuple[Fact, ...] = ()
    graph_edges: tuple[GraphEdge, ...] = ()
    analyst_ratings: tuple[AnalystRatingEvent, ...] = ()
    fund_holdings: tuple[FundHolding, ...] = ()
    price_bars: tuple[PriceBar, ...] = ()
    corporate_actions: tuple[CorporateAction, ...] = ()
    universe_memberships: tuple[UniverseMembership, ...] = ()
    identifiers: tuple[EntityIdentifier, ...] = ()
```
`snapshot_id` derives from canonical query and content hashes, not creation time.
`repository_kind` is diagnostic and cannot affect factors. `raw_refs_sha256` commits to
lineage without exposing it to factor branches. A snapshot is immutable, canonically
sorted, consistent with its manifest, and replaces the narrower `BacktestDataset` role.
Loaders may select domains, but the manifest lists exactly what was included.
## 5. Typed Factor Outputs
```python
class OutputBase(BaseModel):
    output_id: str
    output_type: str
    factor_id: str
    factor_version: str
    entity_id: str
    as_of: datetime
    observed_at: date | None
    confidence: Decimal
    data_availability: Literal["verified", "unverified"]
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
    to_entity_id: str
    relation_type: str
    valid_from: date | None
    valid_to: date | None
    strength: Decimal | None = None
FactorObservation = Annotated[
    MetricObservation | ClassificationObservation | RelationshipObservation,
    Field(discriminator="output_type"),
]
class FactorOutputBatch(BaseModel):
    run_id: str
    factor_id: str
    factor_version: str
    snapshot_id: str
    as_of: datetime
    outputs: tuple[FactorObservation, ...]
    batch_confidence: Decimal
    flags: tuple[str, ...] = ()
```
The union is top-level so schemas reliably emit `oneOf`. `batch_confidence` is the
minimum confidence of consumed inputs and outputs unless a stricter declared policy
applies. An empty batch needs an explanatory flag. PEG emits metrics, three-tier emits
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
Section 14 is the sole authoritative signature set. The invocation rules are:

- base factors reject undeclared upstream batches;
- composites require every declared batch to share `snapshot_id` and `as_of`;
- screens return ranked and explicitly rejected entities;
- persisted definitions contain registry IDs and immutable parameters, never callables;
- factor, screen, strategy, and rule versions make historical runs reconstructible.

## 8. Repository Ports
```python
class ResearchSnapshotRepository(Protocol):
    def load_snapshot(
        self,
        query: AsOfQuery,
        *,
        domains: frozenset[str],
        price_window: DateRange | None = None,
        universe_id: str | None = None,
    ) -> ResearchSnapshot: ...
```
Fine-grained domain methods may remain private helpers. Atomic `load_snapshot` is the
public read boundary, preventing domains from using subtly different cutoffs. Section
14.11 defines the authoritative output, strategy-run, mart, and consumer read ports.
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
  -> registry.resolve(factor_id, factor_version)
  -> snapshot_repository.load_snapshot(query, required_domains)
     -> resolve identifiers and latest eligible vintages
     -> reconcile sources, assign confidence, build canonical manifest
  -> project_factor_inputs(snapshot, declared_domains)  # strips all provenance
  -> factor.compute(inputs, entity_ids, parameters, upstream)
  -> output_repository.put(batch)
  -> materialize deterministic mart projection
Backtest runner
  -> resolve definition registry IDs
  -> for each scheduled cutoff
     -> resolve PIT universe and load one snapshot
     -> compute/load factor batches -> evaluate screen -> portfolio decision
     -> apply lag, prices, costs, and corporate actions
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
Confidence:
- Every staging row, including `PriceBar`, and every factor input/output has `[0, 1]`.
- Composite confidence cannot exceed the minimum consumed confidence.
- Missing input creates an error or explicit flag, never an invented zero.
- Thresholds are versioned parameters, not hidden constants.
Provenance and determinism:
- Normalized records retain `raw_ref`; manifests commit to the lineage set.
- Only `FactorInputBundle`, never `ResearchSnapshot`, crosses into factor code.
- Bundle records contain no source, raw reference, accession, or repository metadata.
- Factors cannot access or branch on provenance; projection tests enforce this boundary.
- Factor version plus snapshot ID recovers output lineage.
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
- Reject manifests whose counts or hashes disagree with snapshots.
- Keep source and raw references out of factor-ready facts.
- Reject composite batches with mismatched snapshots or future timestamps.
- Enforce minimum-confidence propagation.
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
    subject_entity_ids: tuple[str, ...]
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
## 14. V1 Public API Freeze

The signatures below are the implementation boundary for the complete Vision. Modules
may add private helpers, but adapters, assets, factors, consumers, and tests meet at
these APIs. Parameter models are immutable and serialized with every output.

### 14.1 Shared Execution Types

```python
JsonScalar = None | bool | int | str | Decimal
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

class FactorExecutionContext(BaseModel):
    contract_version: str
    factor_id: str
    factor_version: str
    snapshot_id: str
    as_of: datetime
    entity_ids: tuple[str, ...]

class FactorParameters(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

P = TypeVar("P", bound=FactorParameters)

class FactorDefinition(Protocol, Generic[P]):
    factor_id: str
    factor_version: str
    kind: Literal["base", "composite"]
    parameters_model: type[P]
    required_domains: frozenset[DataDomain]
    required_factor_ids: frozenset[str]

    def compute(
        self,
        context: FactorExecutionContext,
        inputs: FactorInputBundle,
        parameters: P,
        upstream: Mapping[str, FactorOutputBatch],
    ) -> FactorOutputBatch: ...

def project_factor_inputs(
    snapshot: ResearchSnapshot,
    *,
    domains: frozenset[DataDomain],
) -> FactorInputBundle: ...

def run_factor(
    definition: FactorDefinition[P],
    *,
    snapshot: ResearchSnapshot,
    entity_ids: Sequence[str],
    parameters: P,
    upstream: Mapping[str, FactorOutputBatch] | None = None,
) -> FactorOutputBatch: ...
```

`run_factor` owns generic validation: definition/parameter compatibility, declared
domains, entity scope, one snapshot/cutoff, upstream dependency completeness, output
identity, canonical ordering, deterministic run ID, and confidence ceilings. Module
functions own only domain computation.

### 14.2 Capture, Normalization, and Extraction

```python
class SourceRequest(BaseModel):
    source: DataSource
    subject_ids: tuple[str, ...]
    as_of: datetime
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

class MetricRegistry(Protocol):
    fusion_ruleset_version: int
    def get(self, metric: str) -> MetricSpec: ...

def select_canonical_fact(
    records: Sequence[FinancialFact],
    *,
    metric: MetricSpec,
    as_of: datetime,
) -> FinancialFact | None: ...

class Normalizer(Protocol):
    source: DataSource
    def normalize(
        self,
        envelope: RawIngestionEnvelope,
        payload: bytes,
    ) -> NormalizedBatch: ...

class StructuredExtractor(Protocol):
    extractor_id: str
    extractor_version: str
    def extract(
        self,
        document: FactorDocumentInput,
        schema: type[BaseModel],
        *,
        instructions: str,
    ) -> BaseModel: ...

def materialize_normalized_batch(
    batch: NormalizedBatch,
    *,
    repository: NormalizedRecordRepository,
) -> MaterializationResult: ...
```

The call gateway enforces source budgets and immutable raw capture. Normalizers may use
source metadata; `FactorDocumentInput` does not expose source or raw lineage. Extraction
implementations return versioned semantic facts; replay consumes stored extraction
results and never calls a model implicitly. Every normalized row carries both
`knowable_at`/`transaction_time` and `recorded_at`, plus a parser `mapping_version`.
Canonical fact selection uses the registered `source_priority` first and the latest
eligible vintage within that source second. Confidence rides with the selected fact but
never arbitrates between sources. Unregistered source/metric pairs remain staging
evidence and cannot silently reach factors or mart. Canonical lineage records both the
selected staging row and fusion-ruleset version.

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
    inputs: FactorInputBundle,
    parameters: PegParameters,
) -> FactorOutputBatch: ...
```

The batch emits price/earnings, selected growth rate, PEG, convention, and explicit
unavailability flags. Each convention has a separate golden oracle; no fallback silently
changes the requested convention.

### 14.4 Module 2: Gross Profit per Employee

```python
class HeadcountExtractionParameters(FactorParameters):
    taxonomy_version: str
    allowed_document_types: frozenset[str]
    max_document_age: timedelta

class GrossProfitPerEmployeeParameters(FactorParameters):
    gross_profit_metric: str
    headcount_metric: str
    financial_policy: Literal["exclude", "explicit_proxy"]
    financial_proxy_metric: str | None = None
    max_period_gap: timedelta

def extract_headcount(
    context: FactorExecutionContext,
    inputs: FactorInputBundle,
    parameters: HeadcountExtractionParameters,
    extractor: StructuredExtractor,
) -> FactorOutputBatch: ...

def compute_gross_profit_per_employee(
    context: FactorExecutionContext,
    inputs: FactorInputBundle,
    parameters: GrossProfitPerEmployeeParameters,
) -> FactorOutputBatch: ...
```

Extraction selects total company headcount, not the first employee-like number. The
financial policy is mandatory and versioned. Missing, ambiguous, or incomparable facts
are flagged; they never become zero.

### 14.5 Module 3: Supply Chain

```python
class SupplyChainExtractionParameters(FactorParameters):
    taxonomy_version: str
    allowed_relation_types: frozenset[str]
    minimum_evidence_mentions: int = Field(ge=1)

class SupplyChainReasoningParameters(FactorParameters):
    minimum_edge_confidence: Decimal = Field(ge=0, le=1)
    maximum_hops: int = Field(ge=1, le=3)
    decay_per_hop: Decimal = Field(gt=0, le=1)
    allowed_relation_types: frozenset[str]

def extract_supply_chain_relationships(
    context: FactorExecutionContext,
    inputs: FactorInputBundle,
    parameters: SupplyChainExtractionParameters,
    extractor: StructuredExtractor,
) -> FactorOutputBatch: ...

def compute_supply_chain_exposure(
    context: FactorExecutionContext,
    inputs: FactorInputBundle,
    parameters: SupplyChainReasoningParameters,
) -> FactorOutputBatch: ...
```

The extraction batch emits relationship observations. Causal/exposure reasoning remains
disabled until evidence calibrates `minimum_edge_confidence`; the runtime rejects a
reasoning run below that declared kill condition.

### 14.6 Module 4: Analyst Backtesting

```python
class AnalystBacktestParameters(FactorParameters):
    forecast_horizon: timedelta
    execution_lag: timedelta
    benchmark_entity_id: str
    return_convention: Literal["price", "total_return"]
    minimum_observations: int = Field(ge=1)
    score_weights: dict[str, Decimal]

def compute_analyst_track_record(
    context: FactorExecutionContext,
    inputs: FactorInputBundle,
    parameters: AnalystBacktestParameters,
) -> FactorOutputBatch: ...
```

Recommendation time, corroborated knowability, vendor update time, execution time, and
evaluation horizon remain distinct. Outputs include observation count, hit rate, excess
return, target-price error, composite score, and insufficient-history flags.

### 14.7 Module 5: ETF Virtual Company

```python
class EtfVirtualCompanyParameters(FactorParameters):
    metrics: tuple[str, ...]
    minimum_resolved_weight: Decimal = Field(ge=0, le=1)
    missing_constituent_policy: Literal["renormalize", "reject"]
    maximum_holding_age: timedelta

def compute_etf_virtual_company(
    context: FactorExecutionContext,
    inputs: FactorInputBundle,
    parameters: EtfVirtualCompanyParameters,
) -> FactorOutputBatch: ...
```

Holdings are resolved before the factor boundary. The output records resolved/unresolved
weight, the chosen missing-data policy, weighted metrics, and minimum consumed
confidence. Current holdings are never applied to older report periods.

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
    inputs: FactorInputBundle,
    parameters: PureBloodParameters,
) -> FactorOutputBatch: ...

def extract_theme_segments(
    context: FactorExecutionContext,
    inputs: FactorInputBundle,
    parameters: PureBloodParameters,
    extractor: StructuredExtractor,
) -> FactorOutputBatch: ...
```

Structured segment revenue wins when available; semantic extraction is a declared
fallback that materializes a versioned result. Outputs expose classified, excluded, and
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
    price_metric: str
    maximum_period_gap: timedelta

def compute_price_to_sales(
    context: FactorExecutionContext,
    inputs: FactorInputBundle,
    parameters: PriceToSalesParameters,
) -> FactorOutputBatch: ...

def compute_three_tier_valuation(
    context: FactorExecutionContext,
    inputs: FactorInputBundle,
    parameters: ThreeTierValuationParameters,
    upstream: Mapping[str, FactorOutputBatch],
) -> FactorOutputBatch: ...
```

The composite consumes gross-profit-per-employee and price-to-sales batches from the
same snapshot/cutoff. It emits tier, band, valuation gap, eligibility, and flags. Bands
and thresholds are research parameters, not performance-validated constants.

### 14.10 Screens, Strategy, and Replay

```python
class FactorInvocation(BaseModel):
    factor_id: str
    factor_version: str
    parameters: dict[str, JsonValue]

class StrategyDefinition(BaseModel):
    strategy_id: str
    strategy_version: str
    universe_id: str
    factors: tuple[FactorInvocation, ...]
    screen_id: str
    screen_version: str
    screen_parameters: dict[str, JsonValue]
    rebalance_rule_id: str
    sizing_rule_id: str
    holding_rule_id: str
    return_convention: Literal["price", "total_return"]
    parameters: dict[str, JsonValue]

class BacktestDefinition(BaseModel):
    strategy: StrategyDefinition
    start: date
    end: date
    initial_cash: Decimal = Field(gt=0)
    currency: str
    execution_lag: timedelta
    transaction_cost_rule_id: str
    as_of_schedule_id: str

class ScreenExecutionContext(BaseModel):
    screen_id: str
    screen_version: str
    snapshot_id: str
    as_of: datetime

class ScreenDefinition(Protocol):
    screen_id: str
    screen_version: str
    required_factor_ids: frozenset[str]
    def evaluate(
        self,
        context: ScreenExecutionContext,
        batches: Mapping[str, FactorOutputBatch],
        *,
        universe: Sequence[str],
        parameters: Mapping[str, JsonValue],
    ) -> ScreenResult: ...

class TargetPosition(BaseModel):
    entity_id: str
    target_weight: Decimal = Field(ge=0, le=1)
    rank: int = Field(ge=1)
    reason_codes: tuple[str, ...] = ()

class PortfolioDecision(BaseModel):
    decision_id: str
    strategy_id: str
    strategy_version: str
    snapshot_id: str
    as_of: datetime
    targets: tuple[TargetPosition, ...]
    rejected_entity_ids: tuple[str, ...] = ()

class Position(BaseModel):
    entity_id: str
    quantity: Decimal
    cost_basis: Decimal

class PortfolioState(BaseModel):
    as_of: datetime
    cash: Decimal
    positions: tuple[Position, ...]

class SimulatedTrade(BaseModel):
    trade_id: str
    entity_id: str
    execution_at: datetime
    quantity: Decimal
    price: Decimal
    cost: Decimal

class PortfolioValuation(BaseModel):
    on: date
    cash: Decimal
    positions_value: Decimal
    total_value: Decimal

class BacktestRunResult(BaseModel):
    run_id: str
    definition_sha256: str
    contract_version: str
    snapshot_ids: tuple[str, ...]
    decisions: tuple[PortfolioDecision, ...]
    trades: tuple[SimulatedTrade, ...]
    valuations: tuple[PortfolioValuation, ...]
    metrics: tuple[MetricObservation, ...]
    flags: tuple[str, ...] = ()

class TransactionCostRule(Protocol):
    rule_id: str
    rule_version: str
    def estimate(
        self,
        *,
        entity_id: str,
        quantity: Decimal,
        price: Decimal,
        executed_at: datetime,
    ) -> Decimal: ...

def build_rebalance_cutoffs(
    definition: BacktestDefinition,
) -> tuple[datetime, ...]: ...

def evaluate_strategy_at(
    definition: StrategyDefinition,
    *,
    as_of: datetime,
    universe: Sequence[str],
    factor_batches: Mapping[str, FactorOutputBatch],
) -> PortfolioDecision: ...

def simulate_execution(
    decision: PortfolioDecision,
    *,
    prices: Sequence[FactorPriceInput],
    current: PortfolioState,
    execution_lag: timedelta,
    cost_rule: TransactionCostRule,
) -> tuple[PortfolioState, tuple[SimulatedTrade, ...]]: ...

def apply_corporate_actions(
    state: PortfolioState,
    *,
    actions: Sequence[FactorActionInput],
    through: date,
) -> PortfolioState: ...

def value_portfolio(
    state: PortfolioState,
    *,
    prices: Sequence[FactorPriceInput],
    on: date,
) -> PortfolioValuation: ...

def run_backtest(
    definition: BacktestDefinition,
    *,
    snapshots: ResearchSnapshotRepository,
    factors: FactorRegistry,
    screens: ScreenRegistry,
) -> BacktestRunResult: ...
```

All ranking, selection, sizing, costs, actions, returns, and metrics live in
`libs/factors`. Dagster invokes these functions; it does not reimplement them.

### 14.11 Persistence and Mart Projection

```python
class FactorOutputRepository(Protocol):
    def put(self, batch: FactorOutputBatch) -> PutResult: ...
    def get(
        self,
        *,
        factor_id: str,
        factor_version: str,
        snapshot_id: str,
    ) -> FactorOutputBatch | None: ...

class StrategyRunRepository(Protocol):
    def put(self, result: BacktestRunResult) -> PutResult: ...
    def get(self, run_id: str) -> BacktestRunResult | None: ...

class MartMaterializer(Protocol):
    def project_factor_batch(self, batch: FactorOutputBatch) -> MaterializationResult: ...
    def project_strategy_run(self, result: BacktestRunResult) -> MaterializationResult: ...

class ResearchReadRepository(Protocol):
    def factor_history(self, query: FactorHistoryQuery) -> FactorHistory: ...
    def entity_comparison(self, query: EntityComparisonQuery) -> EntityComparison: ...
    def ranking(self, query: RankingQuery) -> RankingResult: ...
    def strategy_run(self, run_id: str) -> StrategyRunView | None: ...
    def trace_output(self, output_id: str) -> TraceabilityView: ...
```

Writes are idempotent by semantic ID and append-only by version. Read methods expose
only mart projections and immutable trace links; they perform no new factor computation.

### 14.12 Dagster Composition

```python
def build_capture_asset(adapter: SourceAdapter) -> AssetsDefinition: ...
def build_normalization_asset(normalizer: Normalizer) -> AssetsDefinition: ...
def build_factor_asset(definition: FactorDefinition[Any]) -> AssetsDefinition: ...
def build_strategy_assets(definition: StrategyDefinition) -> Sequence[AssetsDefinition]: ...

def build_definitions(
    *,
    resources: Mapping[str, ResourceDefinition],
    factor_registry: FactorRegistry,
    strategy_registry: StrategyRegistry,
) -> Definitions: ...

def build_strategy_schedule(
    *,
    strategy_id: str,
    strategy_version: str,
    cron_schedule: str,
    environment: Literal["staging", "production"],
) -> ScheduleDefinition: ...
```

Dagster is introduced with the first executable snapshot/factor slice. Local and CI use
in-process jobs and fixture resources; Staging and Production add schedules and persistent
metadata. No alternative scheduler may launch real source runs.

### 14.13 Reports, MCP, App, and Chat

```python
class ResearchReportRequest(BaseModel):
    entity_ids: tuple[str, ...]
    as_of: datetime
    factor_ids: tuple[str, ...]
    strategy_run_id: str | None = None

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
    def render(self, report: ResearchReport) -> bytes: ...

class ResearchQueryService:
    def __init__(self, repository: ResearchReadRepository) -> None: ...
    def factor_history(self, request: FactorHistoryQuery) -> FactorHistory: ...
    def compare_entities(self, request: EntityComparisonQuery) -> EntityComparison: ...
    def rank_entities(self, request: RankingQuery) -> RankingResult: ...
    def explain_output(self, output_id: str) -> TraceabilityView: ...
    def strategy_run(self, run_id: str) -> StrategyRunView: ...

async def mcp_factor_history(request: FactorHistoryQuery) -> FactorHistory: ...
async def mcp_compare_entities(request: EntityComparisonQuery) -> EntityComparison: ...
async def mcp_rank_entities(request: RankingQuery) -> RankingResult: ...
async def mcp_explain_output(output_id: str) -> TraceabilityView: ...
async def mcp_strategy_run(run_id: str) -> StrategyRunView: ...

class ChatRequest(BaseModel):
    conversation_id: str
    messages: tuple[ChatMessage, ...]
    as_of: datetime | None = None

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

The MCP endpoint and `/chat` tool layer reuse `ResearchQueryService`. The App backend
implements the same read contract directly against mart, with no FastAPI hop. The App
may sort, filter, paginate, convert units, and render. Report-card and Xiaohongshu
renderers consume `ResearchReport`; they cannot join mart rows into new metrics.
`/chat` generates prose by calling the same typed tools, never by querying raw/staging
or inventing factor values.

## 15. Complete Vision Call Graph

```text
Dagster schedule / local in-process job
  -> SourceCallGateway.execute(SourceAdapter.capture)
  -> immutable object + raw.fetches
  -> Normalizer.normalize -> append-only staging records
  -> MetricRegistry + select_canonical_fact(source priority, mapping/fusion version)
  -> ResearchSnapshotRepository.load_snapshot(one as_of)
  -> project_factor_inputs(strips provenance)
  -> run_factor(base modules 1-6 and supporting metrics)
  -> run_factor(composite module 7)
  -> ScreenDefinition.evaluate
  -> evaluate_strategy_at / run_backtest
  -> FactorOutputRepository + StrategyRunRepository
  -> MartMaterializer
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

### 16.1 Core Strategy MVP

Deliver PIT snapshots, early Dagster composition, gross profit per employee, three-tier
valuation, `large_model_value_v0`, deterministic local replay, mart/report projection,
and two real scheduled Staging runs. Completion means the core strategy runs
continuously; it does not mean all seven modules or Production are complete.
Tracked by [epic #29](https://github.com/wangzitian0/truealpha/issues/29), with
#14 and #21-#27 as sub-issues.

### 16.2 Seven Research Modules

Implement PEG's three conventions, analyst track records, ETF virtual-company metrics,
supply-chain extraction/reasoning with the confidence kill condition, and pure-blood
theme ranking. Run one shared seven-module golden replay and materialize every output.
Tracked by [epic #30](https://github.com/wangzitian0/truealpha/issues/30), with
#33-#40 as sub-issues.

### 16.3 Research Consumption

Freeze mart read models, expose typed MCP tools, generate traceable personal report
cards and Xiaohongshu card artifacts, add the App dashboard, and finally add `/chat`
as a tool-orchestration surface. Completion proves every Vision question can be answered
from mart with a filing/vintage trace.
Tracked by [epic #31](https://github.com/wangzitian0/truealpha/issues/31), with
#41-#46 and #48 as sub-issues.

### 16.4 Production Strategy Validation

Extend the evaluation corpus to five years/multiple regimes, reconcile critical prices
against an independent source, validate strategy direction against a known reference,
schedule all seven modules in Staging, prove backup/restore and alerting, then promote
the exact tested image to isolated Production with explicit approval.
Tracked by [epic #32](https://github.com/wangzitian0/truealpha/issues/32), with
#11 and #49-#54 as the environment, evaluation, and closure work tree.

Milestones are sequential release gates, not strict implementation serialization. Work
inside a milestone may run in parallel when GitHub dependencies permit it.

## 17. Complete Vision Acceptance

The root `vision.md` success state is reached only when all of these are true:

1. Every one of the seven modules has a versioned implementation, real-sample golden
   evidence, PIT replay, mart projection, confidence, and traceability.
2. The curated Production universe is refreshed only by Dagster and continuously emits
   factor/ranking outputs; failures and stale data alert an owner.
3. A user can ask the named research questions through MCP or `/chat` and receive typed
   results traceable to factor version, snapshot, filing/vintage, and raw checksum.
4. The same mart outputs produce personal report cards and Xiaohongshu card artifacts
   without manual metric recomputation.
5. Strategy evaluation uses at least five years, independent price reconciliation,
   survivorship-safe membership, corporate actions, immutable definitions, and a known
   strategy sanity result; no positive-alpha claim is required unless separately tested.
6. Production uses the exact Staging-tested image, isolated credentials/storage,
   demonstrated backup/restore, append-only data, and recorded human promotion approval.

No milestone may claim the full Vision based on fixture readiness, code existence,
manual flag changes, or one successful happy-path run.

## 18. Interface Freeze and Versioning
The v1 freeze covers field semantics, discriminators, time and confidence rules, port
behavior, and registry identity. It does not freeze storage schemas or internals.
- Contracts use major/minor semantic `contract_version` values.
- Factor, screen, strategy, and rule versions are independent and immutable.
- Removed/renamed fields, changed time meaning, confidence rules, or discriminators
  require a new major version.
- Formula, taxonomy, default threshold, or rule behavior changes require a new
  computation version even if contract shape is unchanged.
- Readers use explicit old-version adapters; writers emit only the current version.
- Persisted runs retain definitions, parameters, versions, and snapshot IDs permanently.
Before v1 freezes, require design approval, passing model tests, one fixture repository
conformance run, generated-schema inspection, and one end-to-end PEG golden replay.
Afterward, implementations can land in phases without reopening top-level architecture.
