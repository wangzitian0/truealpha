# Contract Closure Architecture
Status: design draft for the v1 interface freeze; `init.md` remains authoritative.
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
```python
class FactorDefinition(Protocol):
    factor_id: str
    factor_version: str
    kind: Literal["base", "composite"]
    required_domains: frozenset[str]
    required_factor_ids: frozenset[str]
    def compute(
        self,
        inputs: FactorInputBundle,
        *,
        entity_ids: Sequence[str],
        parameters: Mapping[str, JsonValue],
        upstream: Mapping[str, FactorOutputBatch],
    ) -> FactorOutputBatch: ...
class ScreenDefinition(Protocol):
    screen_id: str
    screen_version: str
    required_factor_ids: frozenset[str]
    def evaluate(
        self,
        batches: Mapping[str, FactorOutputBatch],
        *,
        universe: Sequence[str],
        parameters: Mapping[str, JsonValue],
    ) -> ScreenResult: ...
```
Base factors reject undeclared upstream input. Composites require every declared batch
to share `snapshot_id` and `as_of`. Screens return ranked and explicitly rejected entities.
```python
class StrategyDefinition(BaseModel):
    strategy_id: str
    strategy_version: str
    universe_id: str
    screen_id: str
    rebalance_rule_id: str
    sizing_rule_id: str
    holding_rule_id: str
    return_convention: Literal["price", "total_return"]
    parameters: dict[str, JsonValue]
class BacktestDefinition(BaseModel):
    strategy: StrategyDefinition
    start: date
    end: date
    initial_cash: Decimal
    currency: str
    execution_lag: timedelta
    transaction_cost_rule_id: str
    as_of_schedule_id: str
class BacktestRunResult(BaseModel):
    run_id: str
    definition_sha256: str
    contract_version: str
    snapshot_ids: tuple[str, ...]
    decisions: tuple[PortfolioDecision, ...]
    trades: tuple[SimulatedTrade, ...]
    valuations: tuple[PortfolioValuation, ...]
    metrics: tuple[MetricObservation, ...]
    flags: tuple[str, ...]
```
Persisted definitions contain registry IDs, never callables. Immutable implementation
versions make historical runs reconstructible.
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
class FactorOutputRepository(Protocol):
    def put(self, batch: FactorOutputBatch) -> None: ...
    def get(
        self, *, factor_id: str, factor_version: str, snapshot_id: str
    ) -> FactorOutputBatch | None: ...
class BacktestRunRepository(Protocol):
    def put(self, result: BacktestRunResult) -> None: ...
    def get(self, run_id: str) -> BacktestRunResult | None: ...
```
Fine-grained domain methods may remain private helpers. Atomic `load_snapshot` is the
public boundary, preventing domains from using subtly different cutoffs.
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
## 14. Implementation Phases
1. **Contract closure:** add DTOs, union, snapshot, manifest, and ports; add price
   confidence; keep adapters for current results; test generated discriminators.
2. **Evidence-aware fixture repository:** build canonical snapshots, typed evidence and
   assertion runners; fix per-symbol history; run the shared contract kit.
3. **PEG vertical slice:** implement historical-CAGR PEG through snapshot, typed batch,
   mart, and golden replay. Add guidance only after #14 supplies semantic evidence.
4. **ETF and composite slice:** implement ETF multi-metric batches, identifier fallback,
   and three-tier classification with snapshot/confidence checks.
5. **Postgres and backtest:** run the same conformance kit; add action and membership
   replay before return results; require #14 local-backtest gates.
6. **Remaining modules:** add modules behind the protocol, relationship outputs for
   supply chain, and screens for rankings. Keep causal reasoning confidence-gated.
## 15. Interface Freeze and Versioning
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
