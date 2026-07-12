from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from truealpha_contracts.catalog import (
    CanonicalQuestion,
    CatalogRequirementLevel,
    CatalogTargetKind,
    ExpectedOutputStatus,
    FactorCatalogTarget,
    InvocationTemplateSelector,
    ProductOwnerApproval,
    ResearchCatalogEntry,
    ResearchCatalogManifest,
    ResearchScopeFloor,
    ResearchScopeMinimums,
    StrategyCatalogTarget,
)
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.execution import DependencyTemplate, FactorInvocationTemplate, FactorKind
from truealpha_contracts.research import (
    AnalystAction,
    AnalystBacktestPolicy,
    AnalystConsensusGrowthConvention,
    ArtifactVisibility,
    BenchmarkReturnPolicy,
    BrokenEngineNegativeControl,
    CandidateArtifactRef,
    CandidateArtifactRole,
    CandidateFreeze,
    CanonicalRating,
    CashTreatment,
    CensoringPolicy,
    CompanyGuidanceGrowthConvention,
    ConsensusStatistic,
    CurrencyConversionPolicy,
    DerivativeTreatment,
    DevelopmentGoldenCase,
    DevelopmentGoldenSet,
    ElasticityEstimator,
    EtfAggregationMethod,
    EtfCurrencyPolicy,
    EtfPeriodAlignment,
    EtfVirtualCompanyPolicy,
    EvaluationAttempt,
    EvaluationMetric,
    EvaluationProtocol,
    FinancialComparisonPolicy,
    FinancialEfficiencyProxy,
    GateOutcome,
    GppeLeverageChoice,
    GppeResearchPolicy,
    GraphDirection,
    GrowthUnit,
    GuidanceRangePoint,
    HeadcountAlignment,
    HistoricalCagrGrowthConvention,
    HoldoutStratum,
    ImmutableArtifactRef,
    IndependentApproval,
    InstrumentAggregationLevel,
    KnownReferenceControl,
    LargeModelValueV0Policy,
    MetricDirection,
    MetricObservation,
    MissingSegmentTreatment,
    MultipleComparisonMethod,
    NegativeGrowthBehavior,
    NumericBand,
    OracleCustody,
    OracleProgram,
    OverlapPolicy,
    PeBasis,
    PegPeriodAlignment,
    PegResearchPolicy,
    ProtectedLabelArtifactRef,
    RatingNormalizationEntry,
    ResearchSemanticsManifest,
    ResearchTarget,
    ScenarioOutputLabel,
    SealedHoldout,
    SemanticCatalogBinding,
    ShortTreatment,
    StratumSampleCount,
    SupplyChainOutputClaim,
    SupplyChainResearchPolicy,
    TargetEvaluationPlan,
    TargetPsSelection,
    ThemeDenominator,
    ThemePurityPolicy,
    TieBehavior,
    TierBand,
    TierValuationPolicy,
    UncertaintyMethod,
    UnclassifiedRevenueTreatment,
    UnresolvedWeightTreatment,
    ValuationTier,
    ZeroGrowthBehavior,
    authorize_evaluation,
)
from truealpha_contracts.universe import SubjectKind, SubjectRef, UniverseRef

NOW = datetime(2026, 7, 1, tzinfo=UTC)
AUTHOR = "author:factor-engine"
EVALUATOR = "reviewer:holdout-evaluator"
CUSTODIAN = "custodian:sealed-labels"


def _digest(seed: int) -> str:
    return f"{seed:064x}"


def _artifact(
    seed: int,
    visibility: ArtifactVisibility = ArtifactVisibility.INTERNAL,
    *,
    content_sha256: str | None = None,
) -> ImmutableArtifactRef:
    digest = content_sha256 or _digest(seed)
    return ImmutableArtifactRef(
        artifact_id=f"artifact:{digest}",
        content_sha256=digest,
        immutable_locator=f"urn:truealpha:sha256:{digest}",
        visibility=visibility,
    )


def _product_approval(seed: int, at: datetime = NOW) -> ProductOwnerApproval:
    digest = _digest(seed)
    return ProductOwnerApproval(
        approved_by="owner:research-product",
        approval_record_id=f"approval-record:{digest}",
        approval_record_sha256=digest,
        approved_at=at,
    )


def _independent_approval(
    seed: int,
    reviewer: str = "reviewer:independent-research",
    at: datetime = NOW,
) -> IndependentApproval:
    digest = _digest(seed)
    return IndependentApproval(
        reviewer_id=reviewer,
        reviewer_organization="organization:external-review",
        approval_record_id=f"approval-record:{digest}",
        approval_record_sha256=digest,
        approved_at=at,
    )


def _universe() -> UniverseRef:
    return UniverseRef(
        universe_id="universe:topt-core",
        universe_version="2026.07.01",
        content_sha256=_digest(10),
    )


def _subject() -> SubjectRef:
    return SubjectRef(kind=SubjectKind.ISSUER, id="issuer:ddog")


def _catalog(universe: UniverseRef) -> ResearchCatalogManifest:
    subject = _subject()
    subjects = (subject, SubjectRef(kind=SubjectKind.ISSUER, id="issuer:msft"))
    questions: list[CanonicalQuestion] = []
    entries: list[ResearchCatalogEntry] = []
    for index, target in enumerate(ResearchTarget, start=1):
        alias = target.value.replace("_", "-")
        kind = CatalogTargetKind.STRATEGY if target is ResearchTarget.LARGE_MODEL_VALUE_V0 else CatalogTargetKind.FACTOR
        target_coordinate = "large_model_value_v0" if kind is CatalogTargetKind.STRATEGY else f"research.{target.value}"
        question = CanonicalQuestion(
            question_key=f"question.{target.value}",
            tool_kind=kind,
            catalog_aliases=(alias,),
            subject_scope=subjects,
            requirement_level=CatalogRequirementLevel.REQUIRED,
            expected_output_type_ids=(f"output.{target.value}.v1",),
            expected_statuses=(ExpectedOutputStatus.AVAILABLE,),
            prompt_examples=(f"Evaluate {target.value} for the approved issuer.",),
            approved_at=NOW,
        )
        implementation_digest = _digest(100 + index)
        dependencies = (
            (
                DependencyTemplate(
                    alias="tier",
                    template_id="factor-template:" + _digest(999),
                ),
            )
            if kind is CatalogTargetKind.STRATEGY
            else ()
        )
        template = FactorInvocationTemplate(
            factor_id=target_coordinate,
            factor_version="1.0.0",
            factor_implementation_sha256=implementation_digest,
            factor_kind=FactorKind.STRATEGY if dependencies else FactorKind.BASE,
            parameter_model_key="research:FrozenParameters",
            parameter_schema_sha256=_digest(300 + index),
            canonical_parameters_sha256=canonical_sha256({}),
            data_requirement_ids=("data-requirement:" + _digest(400 + index),),
            dependencies=dependencies,
        )
        selector = InvocationTemplateSelector(
            target_kind=kind,
            factor_template=template,
            parameters=(),
            frozen_at=NOW,
        )
        catalog_target = (
            StrategyCatalogTarget(
                strategy_id=target_coordinate,
                strategy_version="1.0.0",
                definition_sha256=implementation_digest,
            )
            if kind is CatalogTargetKind.STRATEGY
            else FactorCatalogTarget(
                factor_id=target_coordinate,
                factor_version="1.0.0",
                definition_sha256=implementation_digest,
            )
        )
        entry = ResearchCatalogEntry(
            catalog_alias=alias,
            requirement_level=CatalogRequirementLevel.REQUIRED,
            target=catalog_target,
            universe=universe,
            subject_scope=subjects,
            invocation_template=selector,
            applicability_policy_id="applicability-policy:" + _digest(500),
            applicability_policy_sha256=_digest(500),
            slo_policy_id="slo-policy:" + _digest(501),
            slo_policy_sha256=_digest(501),
            canonical_question_ids=(question.canonical_question_id,),
            expected_output_type_ids=(f"output.{target.value}.v1",),
            approved_at=NOW,
        )
        questions.append(question)
        entries.append(entry)

    floor = ResearchScopeFloor(
        universe=universe,
        minimums=ResearchScopeMinimums(
            issuers=1,
            funds=1,
            themes=1,
            analysts=1,
            scenarios=1,
            screens=1,
            rankings=1,
            strategies=1,
            canonical_questions=8,
        ),
        required_entry_ids=tuple(entry.catalog_entry_id for entry in entries),
        required_question_ids=tuple(question.canonical_question_id for question in questions),
        approval=_product_approval(20),
    )
    return ResearchCatalogManifest(
        catalog_version="1.0.0",
        vision_sha256=_digest(21),
        scope_floor=floor,
        entries=tuple(entries),
        canonical_questions=tuple(questions),
        catalog_approval=_product_approval(22),
        created_at=NOW + timedelta(minutes=1),
        effective_at=NOW + timedelta(minutes=2),
    )


def _complete_bands(prefix: str) -> tuple[NumericBand, ...]:
    return (
        NumericBand(
            band_key=f"{prefix}.low",
            lower_bound=None,
            upper_bound=Decimal("1000000"),
            lower_inclusive=True,
            upper_inclusive=False,
        ),
        NumericBand(
            band_key=f"{prefix}.high",
            lower_bound=Decimal("1000000"),
            upper_bound=None,
            lower_inclusive=True,
            upper_inclusive=False,
        ),
    )


def _policies(universe: UniverseRef):
    financial = FinancialComparisonPolicy(
        proxy=FinancialEfficiencyProxy.PRE_PROVISION_PROFIT_PER_EMPLOYEE,
        numerator_semantic_type_id="semantic-type:" + _digest(30),
        comparison_universe=universe,
        comparison_bands=_complete_bands("financial"),
        independent_approval=_independent_approval(31, reviewer="reviewer:financial-proxy"),
    )
    gppe = GppeResearchPolicy(
        leverage_choice=GppeLeverageChoice.COMBINED_DISCRIMINATED,
        level_formula="annual_gross_profit/aligned_employee_count",
        output_unit="reporting_currency_per_employee",
        headcount_alignment=HeadcountAlignment.PERIOD_AVERAGE,
        level_window_periods=1,
        elasticity_formula="delta_ln_gross_profit/delta_ln_employee_count",
        elasticity_estimator=ElasticityEstimator.THEIL_SEN,
        elasticity_window_periods=5,
        decision_bands=_complete_bands("leverage"),
        reviewed_examples=_artifact(32, ArtifactVisibility.PUBLIC),
        financial_policy=financial,
    )
    peg = PegResearchPolicy(
        pe_basis=PeBasis.FORWARD_DILUTED,
        price_semantic_type_id="semantic-type:" + _digest(33),
        eps_semantic_type_id="semantic-type:" + _digest(34),
        pe_horizon_months=12,
        growth_unit=GrowthUnit.PERCENTAGE_POINTS,
        formula="pe/growth_percentage_points",
        period_alignment=PegPeriodAlignment.NEXT_TWELVE_MONTHS,
        negative_growth_behavior=NegativeGrowthBehavior.UNAVAILABLE,
        zero_growth_behavior=ZeroGrowthBehavior.UNAVAILABLE,
        conventions=(
            AnalystConsensusGrowthConvention(
                growth_metric_semantic_type_id="semantic-type:" + _digest(35),
                statistic=ConsensusStatistic.MEDIAN,
                horizon_months=12,
            ),
            HistoricalCagrGrowthConvention(
                growth_metric_semantic_type_id="semantic-type:" + _digest(36),
                lookback_years=3,
                formula="(ending_value/starting_value)^(1/years)-1",
            ),
            CompanyGuidanceGrowthConvention(
                growth_metric_semantic_type_id="semantic-type:" + _digest(37),
                range_point=GuidanceRangePoint.MIDPOINT,
                horizon_months=12,
            ),
        ),
    )
    normalization = tuple(
        RatingNormalizationEntry(source_label=rating.value, canonical_rating=rating, ordinal_score=score)
        for rating, score in zip(CanonicalRating, (-2, -1, 0, 1, 2), strict=True)
    )
    analyst = AnalystBacktestPolicy(
        rating_normalization=normalization,
        included_actions=(
            AnalystAction.INITIATE,
            AnalystAction.UPGRADE,
            AnalystAction.DOWNGRADE,
            AnalystAction.REITERATE,
            AnalystAction.MAINTAIN,
            AnalystAction.RESUME,
        ),
        excluded_actions=(AnalystAction.SUSPEND, AnalystAction.DISCONTINUE),
        horizon_days=365,
        overlap_policy=OverlapPolicy.LATEST_ACTIVE_SIGNAL,
        censoring_policy=CensoringPolicy.REQUIRE_COMPLETE_HORIZON,
        benchmark=SubjectRef(kind=SubjectKind.SECURITY, id="security:ivv"),
        benchmark_return_policy=BenchmarkReturnPolicy.SUBJECT_CURRENCY_THEN_EXCESS,
        currency_conversion_policy=CurrencyConversionPolicy.SPOT_AT_EACH_RETURN_ENDPOINT,
        minimum_history_events=10,
        minimum_history_days=730,
        uncertainty_method=UncertaintyMethod.BLOCK_BOOTSTRAP,
        confidence_level=Decimal("0.95"),
        tie_tolerance=Decimal("0.01"),
        tie_behavior=TieBehavior.REPORT_TIE,
        multiple_comparison_method=MultipleComparisonMethod.BENJAMINI_HOCHBERG,
        multiple_comparison_alpha=Decimal("0.05"),
    )
    etf = EtfVirtualCompanyPolicy(
        aggregation_method=EtfAggregationMethod.RATIO_OF_SUMS,
        ratio_numerator_semantic_type_id="semantic-type:" + _digest(38),
        ratio_denominator_semantic_type_id="semantic-type:" + _digest(39),
        cash_treatment=CashTreatment.EXCLUDE_AND_REPORT_WEIGHT,
        derivative_treatment=DerivativeTreatment.DELTA_ADJUSTED_LOOK_THROUGH,
        short_treatment=ShortTreatment.GROSS_SEPARATE,
        unresolved_weight_treatment=UnresolvedWeightTreatment.FAIL_ABOVE_LIMIT_THEN_RENORMALIZE,
        unresolved_weight_limit=Decimal("0.05"),
        currency_policy=EtfCurrencyPolicy.CONVERT_TO_FUND_BASE_AT_REPORT_DATE,
        fx_semantic_type_id="semantic-type:" + _digest(40),
        instrument_aggregation_level=InstrumentAggregationLevel.ISSUER_AFTER_SECURITY_RESOLUTION,
        period_alignment=EtfPeriodAlignment.LATEST_KNOWABLE_NOT_AFTER_HOLDINGS_REPORT,
    )
    theme = ThemePurityPolicy(
        ontology_id="theme-ontology:" + _digest(41),
        ontology_sha256=_digest(41),
        ontology_version="1.0.0",
        ontology_owner_id="owner:theme-research",
        denominator=ThemeDenominator.TOTAL_REVENUE,
        unclassified_revenue_treatment=UnclassifiedRevenueTreatment.INCLUDE_AS_UNCLASSIFIED,
        missing_segment_treatment=MissingSegmentTreatment.PARTIAL_WITH_COVERAGE,
        minimum_classified_share=Decimal("0.8"),
    )
    supply = SupplyChainResearchPolicy(
        disclosure_coverage_denominator="eligible_disclosure_opportunities",
        graph_edge_semantic="disclosed_or_independently_evidenced_relationship",
        graph_minimum_confidence=Decimal("0.7"),
        scenario_definition_id="scenario-definition:" + _digest(42),
        scenario_definition_sha256=_digest(42),
        direction=GraphDirection.BOTH,
        shock_unit="percentage-point",
        materiality_threshold=Decimal("0.05"),
        sensitivity_rule_id="sensitivity-rule:" + _digest(43),
        sensitivity_rule_sha256=_digest(43),
        horizon_days=365,
        confidence_kill_threshold=Decimal("0.8"),
        causal_evidence_schema_id="causal-schema:" + _digest(44),
        causal_evidence_schema_sha256=_digest(44),
    )
    tier = TierValuationPolicy(
        gppe_policy_id=gppe.gppe_policy_id,
        gppe_policy_sha256=gppe.content_sha256,
        bands=(
            TierBand(
                tier=ValuationTier.TRADITIONAL,
                gppe_lower_bound=None,
                gppe_upper_bound=Decimal("1000000"),
                ps_lower_bound=Decimal("3"),
                ps_upper_bound=Decimal("4"),
            ),
            TierBand(
                tier=ValuationTier.TECH,
                gppe_lower_bound=Decimal("1000000"),
                gppe_upper_bound=Decimal("3000000"),
                ps_lower_bound=Decimal("8"),
                ps_upper_bound=Decimal("10"),
            ),
            TierBand(
                tier=ValuationTier.LARGE_MODEL_NATIVE,
                gppe_lower_bound=Decimal("3000000"),
                gppe_upper_bound=None,
                ps_lower_bound=Decimal("20"),
                ps_upper_bound=Decimal("30"),
            ),
        ),
    )
    strategy = LargeModelValueV0Policy(
        strategy_version="1.0.0",
        gppe_policy_id=gppe.gppe_policy_id,
        gppe_policy_sha256=gppe.content_sha256,
        tier_policy_id=tier.tier_policy_id,
        tier_policy_sha256=tier.content_sha256,
        target_ps_selection=TargetPsSelection.BAND_MIDPOINT,
        selection_count=10,
        rebalance_frequency_days=90,
    )
    return gppe, peg, analyst, etf, theme, supply, tier, strategy


def _semantics() -> ResearchSemanticsManifest:
    universe = _universe()
    catalog = _catalog(universe)
    gppe, peg, analyst, etf, theme, supply, tier, strategy = _policies(universe)
    policy_ids = {
        ResearchTarget.GPPE: gppe.gppe_policy_id,
        ResearchTarget.PEG: peg.peg_policy_id,
        ResearchTarget.ANALYST_BACKTEST: analyst.analyst_policy_id,
        ResearchTarget.ETF_VIRTUAL_COMPANY: etf.etf_policy_id,
        ResearchTarget.THEME_PURITY: theme.theme_policy_id,
        ResearchTarget.SUPPLY_CHAIN: supply.supply_chain_policy_id,
        ResearchTarget.THREE_TIER_VALUATION: tier.tier_policy_id,
        ResearchTarget.LARGE_MODEL_VALUE_V0: strategy.strategy_policy_id,
    }
    entries_by_alias = {entry.catalog_alias: entry for entry in catalog.entries}
    bindings = tuple(
        SemanticCatalogBinding(
            target=target,
            semantic_policy_id=policy_ids[target],
            catalog_entry_id=entries_by_alias[target.value.replace("_", "-")].catalog_entry_id,
            catalog_alias=target.value.replace("_", "-"),
        )
        for target in ResearchTarget
    )
    return ResearchSemanticsManifest(
        semantics_version="1.0.0",
        catalog=catalog,
        universe=universe,
        gppe=gppe,
        peg=peg,
        analyst=analyst,
        etf=etf,
        theme=theme,
        supply_chain=supply,
        tier=tier,
        large_model_value_v0=strategy,
        catalog_bindings=bindings,
        semantic_author_ids=(AUTHOR,),
        product_owner_approval=_product_approval(45),
        independent_approval=_independent_approval(46),
        frozen_at=NOW + timedelta(minutes=3),
    )


def _protocol(semantics: ResearchSemanticsManifest) -> EvaluationProtocol:
    entry_by_id = {entry.catalog_entry_id: entry for entry in semantics.catalog.entries}
    plans = []
    for index, binding in enumerate(semantics.catalog_bindings, start=1):
        entry = entry_by_id[binding.catalog_entry_id]
        plans.append(
            TargetEvaluationPlan(
                target=binding.target,
                canonical_question_ids=entry.canonical_question_ids,
                subject_scope=entry.subject_scope,
                strata=(
                    HoldoutStratum(
                        stratum_key=f"{binding.target.value}.primary",
                        selection_frame=_artifact(600 + index),
                        selection_rule_sha256=_digest(700 + index),
                        subject_kinds=(SubjectKind.ISSUER,),
                        minimum_sample_count=10,
                    ),
                ),
                metrics=(
                    EvaluationMetric(
                        metric_key="agreement-rate",
                        direction=MetricDirection.MINIMUM,
                        threshold=Decimal("0.8"),
                        known_reference_baseline=Decimal("0.5"),
                        minimum_improvement=Decimal("0.1"),
                        logical_minimum=Decimal("0"),
                        logical_maximum=Decimal("1"),
                        minimum_sample_count=10,
                        confidence_level=Decimal("0.95"),
                    ),
                ),
            )
        )
    return EvaluationProtocol(
        protocol_key="seven-module-and-core-oracle",
        protocol_version="1.0.0",
        research_semantics_id=semantics.research_semantics_id,
        research_semantics_sha256=semantics.content_sha256,
        research_catalog_id=semantics.catalog.research_catalog_id,
        research_catalog_sha256=semantics.catalog.content_sha256,
        universe=semantics.universe,
        target_plans=tuple(plans),
        product_owner_approval=_product_approval(47),
        independent_approval=_independent_approval(48, reviewer="reviewer:protocol"),
        predeclared_at=NOW + timedelta(minutes=5),
    )


def _goldens() -> tuple[DevelopmentGoldenSet, ...]:
    return tuple(
        DevelopmentGoldenSet(
            target=target,
            cases=(
                DevelopmentGoldenCase(
                    case_key=f"{target.value}.reference",
                    subject_scope=(_subject(),),
                    input_artifact=_artifact(800 + index, ArtifactVisibility.PUBLIC),
                    expected_output_artifact=_artifact(820 + index, ArtifactVisibility.PUBLIC),
                    provenance_artifact=_artifact(840 + index, ArtifactVisibility.PUBLIC),
                ),
            ),
            authored_by=(AUTHOR,),
            independent_approval=_independent_approval(
                860 + index,
                reviewer=f"reviewer:golden-{index}",
            ),
            frozen_at=NOW + timedelta(minutes=4),
        )
        for index, target in enumerate(ResearchTarget, start=1)
    )


def _controls():
    known = KnownReferenceControl(
        reference_engine=_artifact(900),
        independently_sourced_expected_output=_artifact(901),
        source_owner_id="owner:external-reference",
        metric_key="agreement-rate",
        absolute_tolerance=Decimal("0.01"),
        relative_tolerance=Decimal("0.001"),
        independent_approval=_independent_approval(902, reviewer="reviewer:known-reference"),
        declared_at=NOW + timedelta(minutes=4),
    )
    broken = BrokenEngineNegativeControl(
        target=ResearchTarget.GPPE,
        broken_engine=_artifact(903),
        injected_fault="Shift employee counts forward by one fiscal year.",
        metric_key="agreement-rate",
        independent_approval=_independent_approval(904, reviewer="reviewer:negative-control"),
        declared_at=NOW + timedelta(minutes=4),
    )
    return known, broken


def _program() -> OracleProgram:
    semantics = _semantics()
    return OracleProgram(
        semantics=semantics,
        protocol=_protocol(semantics),
        development_goldens=_goldens(),
        controls=_controls(),
        candidate_author_ids=(AUTHOR,),
        created_at=NOW + timedelta(minutes=6),
    )


def _holdout(
    program: OracleProgram,
    *,
    generation: int = 1,
    predecessor_holdout_id: str | None = None,
    seed: int = 1000,
    sampled_at: datetime | None = None,
) -> SealedHoldout:
    sampled = sampled_at or NOW + timedelta(minutes=7)
    custody = OracleCustody(
        custodian_id=CUSTODIAN,
        authorized_label_reader_ids=(CUSTODIAN, EVALUATOR),
        protected_labels=ProtectedLabelArtifactRef(
            artifact=_artifact(seed + 1, ArtifactVisibility.PROTECTED),
        ),
        custody_record=_artifact(seed + 2),
        sealed_at=sampled + timedelta(minutes=1),
    )
    return SealedHoldout(
        evaluation_protocol_id=program.protocol.evaluation_protocol_id,
        evaluation_protocol_sha256=program.protocol.content_sha256,
        generation=generation,
        predecessor_holdout_id=predecessor_holdout_id,
        sample_artifact=_artifact(seed),
        selected_question_ids=tuple(
            question_id for plan in program.protocol.target_plans for question_id in plan.canonical_question_ids
        ),
        stratum_sample_counts=tuple(
            StratumSampleCount(
                target=plan.target,
                stratum_key=stratum.stratum_key,
                sample_count=stratum.minimum_sample_count,
            )
            for plan in program.protocol.target_plans
            for stratum in plan.strata
        ),
        custody=custody,
        sampled_at=sampled,
    )


def _candidate(program: OracleProgram, holdout: SealedHoldout, *, seed: int = 1100) -> CandidateFreeze:
    artifacts = (
        CandidateArtifactRef(role=CandidateArtifactRole.IMPLEMENTATION, artifact=_artifact(seed)),
        CandidateArtifactRef(role=CandidateArtifactRole.PARAMETERS, artifact=_artifact(seed + 1)),
        CandidateArtifactRef(
            role=CandidateArtifactRole.RESEARCH_CATALOG,
            artifact=_artifact(seed + 2, content_sha256=program.semantics.catalog.content_sha256),
        ),
        CandidateArtifactRef(
            role=CandidateArtifactRole.RESEARCH_SEMANTICS,
            artifact=_artifact(seed + 3, content_sha256=program.semantics.content_sha256),
        ),
        CandidateArtifactRef(
            role=CandidateArtifactRole.EVALUATION_PROTOCOL,
            artifact=_artifact(seed + 4, content_sha256=program.protocol.content_sha256),
        ),
    )
    return CandidateFreeze(
        candidate_version=f"1.0.{seed}",
        research_semantics_id=program.semantics.research_semantics_id,
        research_semantics_sha256=program.semantics.content_sha256,
        research_catalog_id=program.semantics.catalog.research_catalog_id,
        research_catalog_sha256=program.semantics.catalog.content_sha256,
        evaluation_protocol_id=program.protocol.evaluation_protocol_id,
        evaluation_protocol_sha256=program.protocol.content_sha256,
        universe=program.semantics.universe,
        sealed_holdout_id=holdout.sealed_holdout_id,
        sealed_holdout_sha256=holdout.content_sha256,
        artifacts=artifacts,
        candidate_author_ids=(AUTHOR,),
        frozen_at=holdout.custody.sealed_at + timedelta(minutes=1),
    )


def _authorization(program: OracleProgram, holdout: SealedHoldout, candidate: CandidateFreeze):
    return authorize_evaluation(
        program=program,
        holdout=holdout,
        candidate_freeze=candidate,
        reviewer_approval=_independent_approval(1200, reviewer=EVALUATOR, at=candidate.frozen_at),
        authorized_at=candidate.frozen_at + timedelta(minutes=1),
    )


def _attempt(
    program: OracleProgram,
    holdout: SealedHoldout,
    candidate: CandidateFreeze,
    *,
    observed: Decimal = Decimal("0.1"),
) -> EvaluationAttempt:
    authorization = _authorization(program, holdout, candidate)
    return EvaluationAttempt(
        program=program,
        holdout=holdout,
        candidate_freeze=candidate,
        authorization=authorization,
        observations=tuple(
            MetricObservation(
                target=plan.target,
                metric_key=metric.metric_key,
                observed_value=observed,
                sample_count=metric.minimum_sample_count,
            )
            for plan in program.protocol.target_plans
            for metric in plan.metrics
        ),
        result_artifact=_artifact(1201, ArtifactVisibility.PROTECTED),
        evaluated_by=EVALUATOR,
        started_at=authorization.authorized_at + timedelta(minutes=1),
        completed_at=authorization.authorized_at + timedelta(minutes=2),
    )


@pytest.mark.parametrize(
    ("policy_name", "field_name"),
    (
        ("gppe", "leverage_choice"),
        ("peg", "growth_unit"),
        ("analyst", "overlap_policy"),
        ("etf", "aggregation_method"),
        ("theme", "denominator"),
        ("supply_chain", "direction"),
        ("tier", "bands"),
    ),
)
def test_all_module_semantic_choices_fail_when_unresolved(policy_name: str, field_name: str):
    semantics = _semantics()
    policy = getattr(semantics, policy_name)
    unresolved = policy.model_dump(mode="python", exclude={field_name})

    with pytest.raises(ValidationError, match=field_name):
        type(policy).model_validate(unresolved)


def test_semantics_are_content_addressed_and_bind_catalog_universe_and_strategy():
    semantics = _semantics()
    restored = ResearchSemanticsManifest.model_validate_json(semantics.model_dump_json())

    assert semantics.research_semantics_id == "research-semantics:" + semantics.content_sha256
    assert restored.research_semantics_id == semantics.research_semantics_id
    assert semantics.universe == semantics.catalog.scope_floor.universe
    assert semantics.tier.gppe_policy_id == semantics.gppe.gppe_policy_id
    assert semantics.large_model_value_v0.tier_policy_id == semantics.tier.tier_policy_id
    assert {binding.target for binding in semantics.catalog_bindings} == set(ResearchTarget)
    with pytest.raises(ValidationError, match="non-fallback"):
        PegResearchPolicy(
            **semantics.peg.model_dump(
                mode="python",
                exclude={"peg_policy_id", "content_sha256", "conventions"},
            ),
            conventions=semantics.peg.conventions[:2] + (semantics.peg.conventions[0],),
        )


def test_supply_chain_causal_label_requires_separate_evidence():
    policy = _semantics().supply_chain

    with pytest.raises(ValidationError, match="separate causal evidence"):
        SupplyChainOutputClaim(
            policy_id=policy.supply_chain_policy_id,
            policy_sha256=policy.content_sha256,
            output_label=ScenarioOutputLabel.CAUSAL_EFFECT,
            scenario_output_artifact=_artifact(1300),
        )


def test_protected_holdout_labels_cannot_be_public():
    with pytest.raises(ValidationError, match="protected artifact"):
        ProtectedLabelArtifactRef(artifact=_artifact(1301, ArtifactVisibility.PUBLIC))
    with pytest.raises(ValidationError):
        ProtectedLabelArtifactRef(
            artifact=_artifact(1302, ArtifactVisibility.PROTECTED),
            visibility=ArtifactVisibility.PUBLIC,
        )


def test_oracle_rejects_reviewer_conflicts_and_missing_negative_control():
    program = _program()
    with pytest.raises(ValidationError, match="reviewers cannot be candidate authors"):
        OracleProgram(
            semantics=program.semantics,
            protocol=program.protocol,
            development_goldens=program.development_goldens,
            controls=program.controls,
            candidate_author_ids=(program.protocol.independent_approval.reviewer_id,),
            created_at=program.created_at,
        )
    with pytest.raises(ValidationError, match="broken-engine negative control"):
        OracleProgram(
            semantics=program.semantics,
            protocol=program.protocol,
            development_goldens=program.development_goldens,
            controls=(next(control for control in program.controls if isinstance(control, KnownReferenceControl)),),
            candidate_author_ids=program.candidate_author_ids,
            created_at=program.created_at,
        )


def test_mutable_candidate_references_fail_closed():
    digest = _digest(1400)
    with pytest.raises(ValidationError, match="mutable reference"):
        ImmutableArtifactRef(
            artifact_id=f"artifact:{digest}",
            content_sha256=digest,
            immutable_locator=f"oci://registry/engine:latest@sha256:{digest}",
            visibility=ArtifactVisibility.INTERNAL,
        )
    program = _program()
    holdout = _holdout(program)
    candidate = _candidate(program, holdout)
    with pytest.raises(ValidationError, match="mutable reference"):
        CandidateFreeze(
            **candidate.model_dump(
                mode="python",
                exclude={"candidate_freeze_id", "content_sha256", "candidate_version"},
            ),
            candidate_version="latest",
        )


def test_failed_attempt_requires_fresh_holdout_and_forbids_post_result_protocol_changes():
    program = _program()
    first_holdout = _holdout(program)
    first_candidate = _candidate(program, first_holdout)
    failed = _attempt(program, first_holdout, first_candidate)
    assert failed.outcome is GateOutcome.FAIL
    assert EvaluationAttempt.model_validate_json(failed.model_dump_json()).outcome is GateOutcome.FAIL

    with pytest.raises(ValueError, match="cannot reuse"):
        authorize_evaluation(
            program=program,
            holdout=first_holdout,
            candidate_freeze=first_candidate,
            reviewer_approval=_independent_approval(1500, reviewer=EVALUATOR, at=failed.completed_at),
            authorized_at=failed.completed_at + timedelta(minutes=1),
            prior_attempts=(failed,),
        )

    fresh_holdout = _holdout(
        program,
        generation=2,
        predecessor_holdout_id=first_holdout.sealed_holdout_id,
        seed=1600,
        sampled_at=failed.completed_at + timedelta(minutes=1),
    )
    fresh_candidate = _candidate(program, fresh_holdout, seed=1700)
    authorization = authorize_evaluation(
        program=program,
        holdout=fresh_holdout,
        candidate_freeze=fresh_candidate,
        reviewer_approval=_independent_approval(1701, reviewer=EVALUATOR, at=fresh_candidate.frozen_at),
        authorized_at=fresh_candidate.frozen_at + timedelta(minutes=1),
        prior_attempts=(failed,),
    )
    assert authorization.sealed_holdout_id == fresh_holdout.sealed_holdout_id

    first_plan = program.protocol.target_plans[0]
    old_metric = first_plan.metrics[0]
    changed_metric = EvaluationMetric(
        **old_metric.model_dump(mode="python", exclude={"threshold"}),
        threshold=Decimal("0.85"),
    )
    changed_plan = TargetEvaluationPlan(
        **first_plan.model_dump(
            mode="python",
            exclude={"target_plan_id", "content_sha256", "metrics"},
        ),
        metrics=(changed_metric,),
    )
    changed_protocol = EvaluationProtocol(
        **program.protocol.model_dump(
            mode="python",
            exclude={"evaluation_protocol_id", "content_sha256", "protocol_version", "target_plans"},
        ),
        protocol_version="1.0.1",
        target_plans=(changed_plan, *program.protocol.target_plans[1:]),
    )
    changed_program = OracleProgram(
        semantics=program.semantics,
        protocol=changed_protocol,
        development_goldens=program.development_goldens,
        controls=program.controls,
        candidate_author_ids=program.candidate_author_ids,
        created_at=program.created_at + timedelta(minutes=1),
    )
    changed_holdout = _holdout(
        changed_program,
        generation=2,
        predecessor_holdout_id=first_holdout.sealed_holdout_id,
        seed=1800,
        sampled_at=failed.completed_at + timedelta(minutes=1),
    )
    changed_candidate = _candidate(changed_program, changed_holdout, seed=1900)
    with pytest.raises(ValueError, match="threshold or scope changes"):
        authorize_evaluation(
            program=changed_program,
            holdout=changed_holdout,
            candidate_freeze=changed_candidate,
            reviewer_approval=_independent_approval(
                1901,
                reviewer=EVALUATOR,
                at=changed_candidate.frozen_at,
            ),
            authorized_at=changed_candidate.frozen_at + timedelta(minutes=1),
            prior_attempts=(failed,),
        )

    scope_changed_plan = TargetEvaluationPlan(
        **first_plan.model_dump(
            mode="python",
            exclude={"target_plan_id", "content_sha256", "subject_scope"},
        ),
        subject_scope=first_plan.subject_scope[:1],
    )
    scope_changed_protocol = EvaluationProtocol(
        **program.protocol.model_dump(
            mode="python",
            exclude={"evaluation_protocol_id", "content_sha256", "protocol_version", "target_plans"},
        ),
        protocol_version="1.0.2",
        target_plans=(scope_changed_plan, *program.protocol.target_plans[1:]),
    )
    scope_changed_program = OracleProgram(
        semantics=program.semantics,
        protocol=scope_changed_protocol,
        development_goldens=program.development_goldens,
        controls=program.controls,
        candidate_author_ids=program.candidate_author_ids,
        created_at=program.created_at + timedelta(minutes=2),
    )
    scope_changed_holdout = _holdout(
        scope_changed_program,
        generation=2,
        predecessor_holdout_id=first_holdout.sealed_holdout_id,
        seed=2000,
        sampled_at=failed.completed_at + timedelta(minutes=1),
    )
    scope_changed_candidate = _candidate(scope_changed_program, scope_changed_holdout, seed=2100)
    with pytest.raises(ValueError, match="threshold or scope changes"):
        authorize_evaluation(
            program=scope_changed_program,
            holdout=scope_changed_holdout,
            candidate_freeze=scope_changed_candidate,
            reviewer_approval=_independent_approval(
                2101,
                reviewer=EVALUATOR,
                at=scope_changed_candidate.frozen_at,
            ),
            authorized_at=scope_changed_candidate.frozen_at + timedelta(minutes=1),
            prior_attempts=(failed,),
        )
