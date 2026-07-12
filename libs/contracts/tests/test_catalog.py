import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from truealpha_contracts.catalog import (
    CanonicalQuestion,
    CatalogRequirementLevel,
    CatalogTargetKind,
    ExpectedOutputStatus,
    FactorCatalogTarget,
    InvocationParameter,
    InvocationTemplateSelector,
    NarrowedResearchClaim,
    ProductOwnerApproval,
    RankingCatalogTarget,
    ResearchCatalogEntry,
    ResearchCatalogManifest,
    ResearchScopeFloor,
    ResearchScopeMinimums,
    ScenarioCatalogTarget,
    ScreenCatalogTarget,
    StrategyCatalogTarget,
    ThemeCatalogTarget,
    diff_research_catalogs,
    evaluate_catalog_transition,
    resolve_catalog_alias,
)
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.execution import DependencyTemplate, FactorInvocationTemplate, FactorKind
from truealpha_contracts.universe import SubjectKind, SubjectRef, UniverseRef

NOW = datetime(2026, 7, 1, tzinfo=UTC)
SHA = "a" * 64
VISION = "b" * 64
NARROWED_VISION = "c" * 64


def _approval(*, at: datetime = NOW, seed: str = "1") -> ProductOwnerApproval:
    digest = seed * 64
    return ProductOwnerApproval(
        approved_by="product-owner:truealpha",
        approval_record_id=f"approval-record:{digest}",
        approval_record_sha256=digest,
        approved_at=at,
    )


def _universe(seed: str = "d") -> UniverseRef:
    return UniverseRef(
        universe_id=f"universe:research-{seed}",
        universe_version="2026.07.01",
        content_sha256=seed * 64,
    )


def _subject(seed: str = "alphabet") -> SubjectRef:
    return SubjectRef(kind=SubjectKind.ISSUER, id=f"issuer:{seed}")


def _selector(
    *,
    kind: CatalogTargetKind = CatalogTargetKind.FACTOR,
    seed: str = "1",
    frozen_at: datetime = NOW,
    parameters: tuple[InvocationParameter, ...] = (),
    dependencies: tuple[DependencyTemplate, ...] = (),
    **overrides,
) -> InvocationTemplateSelector:
    coordinates = {
        CatalogTargetKind.FACTOR: f"factor.gppe.{seed}",
        CatalogTargetKind.RANKING: f"ranking.gppe.{seed}",
        CatalogTargetKind.SCREEN: f"screen.pure-blood.{seed}",
        CatalogTargetKind.THEME: f"theme.large-model.{seed}",
        CatalogTargetKind.SCENARIO: f"scenario.supply-shock.{seed}",
        CatalogTargetKind.STRATEGY: f"strategy.large-model-value.{seed}",
    }
    if kind is CatalogTargetKind.STRATEGY and not dependencies:
        dependencies = (DependencyTemplate(alias="ranking", template_id="factor-template:" + "9" * 64),)
    factor_kind = (
        FactorKind.STRATEGY
        if kind is CatalogTargetKind.STRATEGY
        else (FactorKind.COMPOSITE if dependencies else FactorKind.BASE)
    )
    parameter_values = {item.name: json.loads(item.canonical_json) for item in parameters}
    factor_template = FactorInvocationTemplate(
        factor_id=coordinates[kind],
        factor_version="1.0.0",
        factor_implementation_sha256=seed * 64,
        factor_kind=factor_kind,
        parameter_model_key="catalog:InvocationParameters",
        parameter_schema_sha256="e" * 64,
        canonical_parameters_sha256=canonical_sha256(parameter_values),
        data_requirement_ids=("data-requirement:" + "8" * 64,),
        dependencies=dependencies,
    )
    values = {
        "target_kind": kind,
        "factor_template": factor_template,
        "parameters": parameters,
        "frozen_at": frozen_at,
    }
    values.update(overrides)
    return InvocationTemplateSelector(**values)


def _target(kind: CatalogTargetKind = CatalogTargetKind.FACTOR, seed: str = "1"):
    if kind is CatalogTargetKind.FACTOR:
        return FactorCatalogTarget(
            factor_id=f"factor.gppe.{seed}",
            factor_version="1.0.0",
            definition_sha256=seed * 64,
        )
    if kind is CatalogTargetKind.RANKING:
        return RankingCatalogTarget(
            ranking_id=f"ranking.gppe.{seed}",
            ranking_version="1.0.0",
            definition_sha256=seed * 64,
        )
    if kind is CatalogTargetKind.SCREEN:
        return ScreenCatalogTarget(
            screen_id=f"screen.pure-blood.{seed}",
            screen_version="1.0.0",
            definition_sha256=seed * 64,
        )
    if kind is CatalogTargetKind.THEME:
        return ThemeCatalogTarget(
            theme_id=f"theme.large-model.{seed}",
            ontology_version="1.0.0",
            ontology_sha256=seed * 64,
        )
    if kind is CatalogTargetKind.SCENARIO:
        return ScenarioCatalogTarget(
            scenario_id=f"scenario.supply-shock.{seed}",
            scenario_version="1.0.0",
            definition_sha256=seed * 64,
        )
    return StrategyCatalogTarget(
        strategy_id=f"strategy.large-model-value.{seed}",
        strategy_version="1.0.0",
        definition_sha256=seed * 64,
    )


def _question(
    *,
    alias: str = "gppe",
    seed: str = "1",
    kind: CatalogTargetKind = CatalogTargetKind.FACTOR,
    subject: SubjectRef | None = None,
    level: CatalogRequirementLevel = CatalogRequirementLevel.REQUIRED,
    approved_at: datetime = NOW,
    **overrides,
) -> CanonicalQuestion:
    values = {
        "question_key": f"question.{kind.value}.{seed}",
        "tool_kind": kind,
        "catalog_aliases": (alias,),
        "subject_scope": (subject or _subject(),),
        "requirement_level": level,
        "expected_output_type_ids": (f"output.{kind.value}.v1",),
        "expected_statuses": (ExpectedOutputStatus.AVAILABLE,),
        "prompt_examples": (f"Run {alias} for the approved subject.",),
        "approved_at": approved_at,
    }
    values.update(overrides)
    return CanonicalQuestion(**values)


def _entry(
    question: CanonicalQuestion,
    *,
    universe: UniverseRef | None = None,
    alias: str = "gppe",
    seed: str = "1",
    kind: CatalogTargetKind = CatalogTargetKind.FACTOR,
    subject: SubjectRef | None = None,
    level: CatalogRequirementLevel = CatalogRequirementLevel.REQUIRED,
    approved_at: datetime = NOW,
    **overrides,
) -> ResearchCatalogEntry:
    values = {
        "catalog_alias": alias,
        "requirement_level": level,
        "target": _target(kind, seed),
        "universe": universe or _universe(),
        "subject_scope": (subject or _subject(),),
        "invocation_template": _selector(kind=kind, seed=seed),
        "applicability_policy_id": "applicability-policy:" + "e" * 64,
        "applicability_policy_sha256": "e" * 64,
        "slo_policy_id": "slo-policy:" + "f" * 64,
        "slo_policy_sha256": "f" * 64,
        "canonical_question_ids": (question.canonical_question_id,),
        "expected_output_type_ids": (f"output.{kind.value}.v1",),
        "approved_at": approved_at,
    }
    values.update(overrides)
    return ResearchCatalogEntry(**values)


def _minimums(**overrides) -> ResearchScopeMinimums:
    values = {
        "issuers": 1,
        "funds": 1,
        "themes": 1,
        "analysts": 1,
        "scenarios": 1,
        "screens": 1,
        "rankings": 1,
        "strategies": 1,
        "canonical_questions": 1,
    }
    values.update(overrides)
    return ResearchScopeMinimums(**values)


def _floor(
    entry: ResearchCatalogEntry,
    question: CanonicalQuestion,
    *,
    universe: UniverseRef | None = None,
    minimums: ResearchScopeMinimums | None = None,
    **overrides,
) -> ResearchScopeFloor:
    values = {
        "universe": universe or entry.universe,
        "minimums": minimums or _minimums(),
        "required_entry_ids": (entry.catalog_entry_id,),
        "required_question_ids": (question.canonical_question_id,),
        "approval": _approval(),
    }
    values.update(overrides)
    return ResearchScopeFloor(**values)


def _manifest(
    *,
    entries: tuple[ResearchCatalogEntry, ...] | None = None,
    questions: tuple[CanonicalQuestion, ...] | None = None,
    floor: ResearchScopeFloor | None = None,
    catalog_version: str = "1.0.0",
    vision_sha256: str = VISION,
    predecessor_catalog_id: str | None = None,
    narrowed_claim: NarrowedResearchClaim | None = None,
    created_at: datetime = NOW + timedelta(hours=1),
    **overrides,
) -> ResearchCatalogManifest:
    question = _question()
    entry = _entry(question)
    selected_entries = entries or (entry,)
    selected_questions = questions or (question,)
    selected_floor = floor or _floor(selected_entries[0], selected_questions[0])
    values = {
        "catalog_version": catalog_version,
        "vision_sha256": vision_sha256,
        "predecessor_catalog_id": predecessor_catalog_id,
        "scope_floor": selected_floor,
        "entries": selected_entries,
        "canonical_questions": selected_questions,
        "narrowed_claim": narrowed_claim,
        "catalog_approval": _approval(at=created_at - timedelta(minutes=1), seed="2"),
        "created_at": created_at,
        "effective_at": created_at + timedelta(hours=1),
    }
    values.update(overrides)
    return ResearchCatalogManifest(**values)


def test_invocation_selector_is_content_addressed_order_independent_and_deeply_immutable():
    parameters = (
        InvocationParameter(name="window", canonical_json="12"),
        InvocationParameter(name="bands", canonical_json='{"high":30,"low":20}'),
    )
    dependencies = (
        DependencyTemplate(
            alias="prices",
            template_id="factor-template:" + "3" * 64,
        ),
        DependencyTemplate(
            alias="financials",
            template_id="factor-template:" + "4" * 64,
        ),
    )
    first = _selector(parameters=parameters, dependencies=dependencies)
    reordered = _selector(parameters=tuple(reversed(parameters)), dependencies=tuple(reversed(dependencies)))

    assert first.invocation_template_id == "factor-template:" + first.content_sha256
    assert reordered.invocation_template_id == first.invocation_template_id
    with pytest.raises(ValidationError, match="frozen"):
        first.factor_template.factor_version = "2.0.0"
    with pytest.raises(ValidationError, match="canonical JSON"):
        InvocationParameter(name="bands", canonical_json='{"low": 20}')


def test_all_catalog_target_kinds_round_trip_as_typed_discriminated_targets():
    kinds = tuple(CatalogTargetKind)
    questions = tuple(
        _question(alias=f"alias-{kind.value}", seed=str(index), kind=kind) for index, kind in enumerate(kinds, 1)
    )
    entries = tuple(
        _entry(
            question,
            alias=f"alias-{kind.value}",
            seed=str(index),
            kind=kind,
        )
        for index, (kind, question) in enumerate(zip(kinds, questions, strict=True), 1)
    )
    floor = _floor(entries[0], questions[0])
    manifest = _manifest(entries=entries, questions=questions, floor=floor)

    restored = ResearchCatalogManifest.model_validate_json(manifest.model_dump_json())

    assert tuple(entry.target.target_kind for entry in restored.entries) == tuple(
        sorted(
            kinds,
            key=lambda kind: next(entry.catalog_entry_id for entry in entries if entry.target.target_kind is kind),
        )
    )


def test_manifest_binds_exact_universe_questions_policies_floor_and_content_hash():
    manifest = _manifest()
    entry = manifest.entries[0]
    question = manifest.canonical_questions[0]

    assert manifest.research_catalog_id == "research-catalog:" + manifest.content_sha256
    assert entry.universe == manifest.scope_floor.universe
    assert entry.canonical_question_ids == (question.canonical_question_id,)
    assert manifest.scope_floor.required_entry_ids == (entry.catalog_entry_id,)
    assert manifest.scope_floor.approval.approver_role == "product_owner"

    with pytest.raises(ValidationError, match="content_sha256 does not match"):
        _manifest(content_sha256="9" * 64)
    with pytest.raises(ValidationError, match="catalog_entry_id does not match"):
        _entry(question, catalog_entry_id="catalog-entry:" + "9" * 64)
    selector = _selector()
    with pytest.raises(ValidationError, match="factor_template_id does not match"):
        FactorInvocationTemplate(
            **selector.factor_template.model_dump(exclude={"factor_template_id"}),
            factor_template_id="factor-template:" + "9" * 64,
        )


def test_alias_resolution_requires_release_bound_catalog_and_universe():
    manifest = _manifest()

    resolved = resolve_catalog_alias(
        manifest,
        bound_catalog_id=manifest.research_catalog_id,
        bound_catalog_sha256=manifest.content_sha256,
        bound_universe=manifest.scope_floor.universe,
        catalog_alias="gppe",
    )

    assert resolved is manifest.entries[0]
    with pytest.raises(ValueError, match="catalog binding"):
        resolve_catalog_alias(
            manifest,
            bound_catalog_id="research-catalog:" + "9" * 64,
            bound_catalog_sha256=manifest.content_sha256,
            bound_universe=manifest.scope_floor.universe,
            catalog_alias="gppe",
        )
    with pytest.raises(ValueError, match="universe binding"):
        resolve_catalog_alias(
            manifest,
            bound_catalog_id=manifest.research_catalog_id,
            bound_catalog_sha256=manifest.content_sha256,
            bound_universe=_universe("e"),
            catalog_alias="gppe",
        )
    with pytest.raises(ValueError, match="mutable alias"):
        resolve_catalog_alias(
            manifest,
            bound_catalog_id=manifest.research_catalog_id,
            bound_catalog_sha256=manifest.content_sha256,
            bound_universe=manifest.scope_floor.universe,
            catalog_alias="current",
        )


def test_manifest_rejects_mutable_coordinates_duplicates_and_ambiguous_aliases():
    question = _question()
    entry = _entry(question)
    floor = _floor(entry, question)

    with pytest.raises(ValidationError, match="mutable alias"):
        _manifest(entries=(entry,), questions=(question,), floor=floor, catalog_version="latest")
    with pytest.raises(ValidationError, match="mutable alias"):
        _entry(question, alias="current")
    with pytest.raises(ValidationError):
        _manifest(
            entries=(entry,),
            questions=(question,),
            floor=floor,
            research_catalog_id="research-catalog:current",
        )
    with pytest.raises(ValidationError, match="catalog entries must not contain duplicates"):
        _manifest(entries=(entry, entry), questions=(question,), floor=floor)
    with pytest.raises(ValidationError, match="canonical questions must not contain duplicates"):
        _manifest(entries=(entry,), questions=(question, question), floor=floor)
    with pytest.raises(ValidationError, match="required_entry_ids must not contain duplicates"):
        _floor(entry, question, required_entry_ids=(entry.catalog_entry_id, entry.catalog_entry_id))


def test_manifest_rejects_postdated_or_cross_bound_content():
    question = _question()
    postdated_entry = _entry(question, approved_at=NOW + timedelta(days=2))
    postdated_floor = _floor(postdated_entry, question)
    with pytest.raises(ValidationError, match="postdated catalog entries"):
        _manifest(entries=(postdated_entry,), questions=(question,), floor=postdated_floor)

    other_universe = _universe("e")
    entry = _entry(question)
    with pytest.raises(ValidationError, match="exact scope-floor UniverseRef"):
        _manifest(entries=(entry,), questions=(question,), floor=_floor(entry, question, universe=other_universe))

    unknown_alias_question = _question(alias="unknown")
    mismatched_entry = _entry(unknown_alias_question, alias="gppe")
    with pytest.raises(ValidationError, match="unknown catalog aliases"):
        _manifest(
            entries=(mismatched_entry,),
            questions=(unknown_alias_question,),
            floor=_floor(mismatched_entry, unknown_alias_question),
        )


def test_ordinary_additive_onboarding_preserves_every_prior_content_id():
    predecessor = _manifest()
    universe = predecessor.scope_floor.universe
    added_question = _question(alias="gppe-ranking", seed="2", kind=CatalogTargetKind.RANKING)
    added_entry = _entry(
        added_question,
        universe=universe,
        alias="gppe-ranking",
        seed="2",
        kind=CatalogTargetKind.RANKING,
    )
    candidate = _manifest(
        entries=predecessor.entries + (added_entry,),
        questions=predecessor.canonical_questions + (added_question,),
        floor=predecessor.scope_floor,
        catalog_version="2.0.0",
        predecessor_catalog_id=predecessor.research_catalog_id,
        created_at=predecessor.created_at + timedelta(days=1),
    )

    result = evaluate_catalog_transition(predecessor, candidate)

    assert result.accepted
    assert result.added_entry_ids == (added_entry.catalog_entry_id,)
    assert result.added_question_ids == (added_question.canonical_question_id,)
    assert not result.removed_entry_ids
    assert not result.blockers


def test_transition_fails_on_removal_floor_decrease_universe_substitution_and_weakening():
    predecessor = _manifest(
        floor=None,
    )
    predecessor_entry = predecessor.entries[0]
    predecessor_question = predecessor.canonical_questions[0]

    replacement_question = _question(alias="replacement", seed="2")
    replacement_entry = _entry(
        replacement_question,
        universe=predecessor.scope_floor.universe,
        alias="replacement",
        seed="2",
    )
    removal = _manifest(
        entries=(replacement_entry,),
        questions=(replacement_question,),
        floor=_floor(replacement_entry, replacement_question),
        catalog_version="2.0.0",
        predecessor_catalog_id=predecessor.research_catalog_id,
        created_at=predecessor.created_at + timedelta(days=1),
    )
    with pytest.raises(ValueError, match="narrowed Vision claim"):
        evaluate_catalog_transition(predecessor, removal)
    removal_diff = diff_research_catalogs(predecessor, removal)
    assert predecessor_entry.catalog_entry_id in removal_diff.removed_entry_ids
    assert predecessor_question.canonical_question_id in removal_diff.removed_question_ids

    stronger_floor = _floor(
        predecessor_entry,
        predecessor_question,
        minimums=_minimums(issuers=2),
    )
    stronger_predecessor = _manifest(
        entries=predecessor.entries,
        questions=predecessor.canonical_questions,
        floor=stronger_floor,
    )
    decreased = _manifest(
        entries=stronger_predecessor.entries,
        questions=stronger_predecessor.canonical_questions,
        floor=_floor(predecessor_entry, predecessor_question),
        catalog_version="2.0.0",
        predecessor_catalog_id=stronger_predecessor.research_catalog_id,
        created_at=stronger_predecessor.created_at + timedelta(days=1),
    )
    assert diff_research_catalogs(stronger_predecessor, decreased).scope_floor_decreases[0].dimension == "issuers"
    with pytest.raises(ValueError, match="narrowed Vision claim"):
        evaluate_catalog_transition(stronger_predecessor, decreased)

    changed_universe = _universe("e")
    moved_entry = _entry(predecessor_question, universe=changed_universe)
    substituted = _manifest(
        entries=(moved_entry,),
        questions=(predecessor_question,),
        floor=_floor(moved_entry, predecessor_question),
        catalog_version="2.0.0",
        predecessor_catalog_id=predecessor.research_catalog_id,
        created_at=predecessor.created_at + timedelta(days=1),
    )
    assert diff_research_catalogs(predecessor, substituted).universe_substituted
    with pytest.raises(ValueError, match="narrowed Vision claim"):
        evaluate_catalog_transition(predecessor, substituted)

    anchor_question = _question(alias="anchor-ranking", seed="3", kind=CatalogTargetKind.RANKING)
    anchor_entry = _entry(
        anchor_question,
        universe=predecessor.scope_floor.universe,
        alias="anchor-ranking",
        seed="3",
        kind=CatalogTargetKind.RANKING,
    )
    weakening_predecessor = _manifest(
        entries=(predecessor_entry, anchor_entry),
        questions=(predecessor_question, anchor_question),
        floor=_floor(anchor_entry, anchor_question),
    )
    for weakening_level in (CatalogRequirementLevel.OPTIONAL, CatalogRequirementLevel.NOT_APPLICABLE):
        weakened_entry = _entry(
            predecessor_question,
            universe=predecessor.scope_floor.universe,
            level=weakening_level,
        )
        weakened = _manifest(
            entries=(weakened_entry, anchor_entry),
            questions=(predecessor_question, anchor_question),
            floor=weakening_predecessor.scope_floor,
            catalog_version="2.0.0",
            predecessor_catalog_id=weakening_predecessor.research_catalog_id,
            created_at=weakening_predecessor.created_at + timedelta(days=1),
        )
        weakening_diff = diff_research_catalogs(weakening_predecessor, weakened)
        assert weakening_diff.entry_weakenings[0].coordinate == "gppe"
        with pytest.raises(ValueError, match="narrowed Vision claim"):
            evaluate_catalog_transition(weakening_predecessor, weakened)


def test_exact_product_owner_narrowing_claim_allows_only_the_enumerated_change():
    question = _question()
    entry = _entry(question)
    predecessor = _manifest(
        entries=(entry,),
        questions=(question,),
        floor=_floor(entry, question, minimums=_minimums(issuers=2)),
    )
    candidate_floor = _floor(entry, question, minimums=_minimums(issuers=1))
    unapproved_candidate = _manifest(
        entries=predecessor.entries,
        questions=predecessor.canonical_questions,
        floor=candidate_floor,
        catalog_version="2.0.0",
        vision_sha256=NARROWED_VISION,
        predecessor_catalog_id=predecessor.research_catalog_id,
        created_at=predecessor.created_at + timedelta(days=1),
    )
    detected = diff_research_catalogs(predecessor, unapproved_candidate)
    claim = NarrowedResearchClaim(
        predecessor_catalog_id=predecessor.research_catalog_id,
        previous_vision_sha256=predecessor.vision_sha256,
        narrowed_vision_sha256=NARROWED_VISION,
        decreased_floor_dimensions=tuple(item.dimension for item in detected.scope_floor_decreases),
        rationale="The product owner explicitly narrowed the supported issuer claim.",
        approval=_approval(at=predecessor.created_at + timedelta(hours=2), seed="3"),
    )
    approved_candidate = _manifest(
        entries=predecessor.entries,
        questions=predecessor.canonical_questions,
        floor=candidate_floor,
        catalog_version="2.0.0",
        vision_sha256=NARROWED_VISION,
        predecessor_catalog_id=predecessor.research_catalog_id,
        narrowed_claim=claim,
        created_at=predecessor.created_at + timedelta(days=1),
    )

    assert evaluate_catalog_transition(predecessor, approved_candidate).accepted

    forged_claim = NarrowedResearchClaim(
        predecessor_catalog_id=predecessor.research_catalog_id,
        previous_vision_sha256=predecessor.vision_sha256,
        narrowed_vision_sha256=NARROWED_VISION,
        decreased_floor_dimensions=("funds",),
        rationale="This approval names the wrong reduction.",
        approval=_approval(at=predecessor.created_at + timedelta(hours=2), seed="4"),
    )
    forged_candidate = _manifest(
        entries=predecessor.entries,
        questions=predecessor.canonical_questions,
        floor=candidate_floor,
        catalog_version="2.0.0",
        vision_sha256=NARROWED_VISION,
        predecessor_catalog_id=predecessor.research_catalog_id,
        narrowed_claim=forged_claim,
        created_at=predecessor.created_at + timedelta(days=1),
    )
    with pytest.raises(ValueError, match="does not exactly enumerate"):
        evaluate_catalog_transition(predecessor, forged_candidate)
