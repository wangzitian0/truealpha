"""Batch-private E1 timing and revision evidence for D2."""

import csv
import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path
from typing import Any, Literal, cast
from xml.etree import ElementTree

import dagster as dg
from dagster import AssetExecutionContext
from data_engine.batches.mvp_capture_tiny.e1_slice import MvpCaptureTinyEvidence
from data_engine.batches.mvp_capture_tiny.e1_slice import run_e1_suite as run_d0_e1
from data_engine.batches.mvp_medium_validation.e0_slice import (
    BATCH_MANIFEST_PATH,
    CORPUS_PATH,
    SEMANTIC_TYPE_ID,
    SOURCE_ID,
    SUBJECT,
    VERSION,
    D2E0Evidence,
    FrozenPriceArtifact,
    MarketPricePayload,
    PostgresMarketPriceRepository,
    PricePipelineRun,
    PriceReconciliationEvidence,
    run_d2_e0,
    run_price_pipeline,
)
from psycopg import Connection
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from truealpha_contracts import (
    CorporateAction,
    CorporateActionClockTick,
    CorporateActionPhase,
    CorporateActionType,
    DataSource,
    ExchangeCalendar,
    FinancialFact,
    ListingPriceBar,
    MarketSession,
    MarketSessionKind,
    RawObjectStore,
    UniverseDefinitionKind,
    UniverseManifest,
    UniverseMembership,
    V1ReturnReplay,
)
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.execution import ProvenanceNeutralInput
from truealpha_contracts.market import PriceBasis
from truealpha_contracts.release import ReleaseManifest
from truealpha_contracts.universe import SubjectKind, SubjectRef

D2_E1_ASSET_NAME = "mvp_medium_validation_e1_evidence"
_EVIDENCE_TABLE = "d2_mvp_medium_validation_e1_evidence"
_E1_CASE_IDS = (
    "d0-cross-domain-regression",
    "e0-price-changed-vintage",
    "jpm-dividend-lifecycle",
    "nvda-split-lifecycle",
    "plug-financial-filing-restatement",
    "qqq-membership-vintages",
    "schema-registry-provenance",
)
_REQUIRED_ARTIFACT_IDS = frozenset(
    {
        "d0-e1-corpus",
        "jpm-daily-prices",
        "jpm-dividend-events",
        "jpm-dividend-statement",
        "nvda-daily-prices",
        "nvda-listing-identity",
        "nvda-split-events",
        "plug-amended-filing",
        "plug-company-facts",
        "plug-original-filing",
        "qqq-membership-2025q4",
        "qqq-membership-2026q1",
        "strategy-coverage",
    }
)
_SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "corporate-action": CorporateAction,
    "financial-fact": FinancialFact,
    "market-price": MarketPricePayload,
    "provenance-neutral-input": ProvenanceNeutralInput,
    "universe-membership": UniverseMembership,
}
_PROVENANCE_FIELDS = frozenset({"source", "raw_ref", "raw_object_id", "registry", "lineage"})


class D2E1CaseResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    passed: bool
    assertion_ids: tuple[str, ...] = Field(min_length=1)
    observed_ids: tuple[str, ...] = ()
    blocker_codes: tuple[str, ...] = ()


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _resolve_inside(root: Path, relative_path: str) -> Path:
    candidate = (root / relative_path).resolve()
    resolved_root = root.resolve()
    if not candidate.is_relative_to(resolved_root) or not candidate.is_file():
        raise ValueError(f"artifact path is outside the repository or missing: {relative_path}")
    return candidate


def _aware(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO datetime string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return parsed


def _strict_decimal(value: Any, *, label: str) -> Decimal:
    if isinstance(value, (float, bool)):
        raise ValueError(f"{label} must not use a binary float")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{label} is not a Decimal literal") from error
    if not parsed.is_finite():
        raise ValueError(f"{label} must be finite")
    return parsed


def _required_text(value: dict[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError(f"{key} must be a non-empty string")
    return result


@dataclass(frozen=True)
class FrozenE1Artifact:
    artifact_id: str
    path: str
    sha256: str
    body: bytes
    metadata: dict[str, Any]


@dataclass(frozen=True)
class FrozenE1Corpus:
    corpus_id: str
    corpus_sha256: str
    artifacts: dict[str, FrozenE1Artifact]
    cases: dict[str, dict[str, Any]]
    schema_contract: dict[str, str]
    factor_projection_fields: tuple[str, ...]
    created_at: datetime


def validate_schema_contract(contract: dict[str, str]) -> dict[str, str]:
    actual = {name: canonical_sha256(model.model_json_schema()) for name, model in _SCHEMA_MODELS.items()}
    if contract != actual:
        raise ValueError("D2 E1 typed schema contract drifted")
    return actual


def route_typed_payload(semantic_type: str, payload: dict[str, Any]) -> BaseModel:
    model = _SCHEMA_MODELS.get(semantic_type)
    if model is None:
        raise ValueError(f"unknown D2 E1 semantic type: {semantic_type}")
    return model.model_validate(payload)


def load_e1_corpus(repository_root: Path) -> FrozenE1Corpus:
    corpus_file = _resolve_inside(repository_root, CORPUS_PATH.as_posix())
    corpus_bytes = corpus_file.read_bytes()
    corpus_sha256 = _sha256(corpus_bytes)
    manifest = json.loads(_resolve_inside(repository_root, BATCH_MANIFEST_PATH.as_posix()).read_bytes())
    if manifest.get("corpus", {}).get("sha256") != corpus_sha256:
        raise ValueError("D2 E1 corpus does not match the canonical batch manifest")
    try:
        corpus = json.loads(corpus_bytes, parse_float=Decimal)
    except json.JSONDecodeError as error:
        raise ValueError("D2 E1 corpus is not valid JSON") from error
    if (
        not isinstance(corpus, dict)
        or corpus.get("schema_version") != 1
        or corpus.get("corpus_id") != "d2-mvp-medium-validation-e1-v1"
        or corpus.get("rung_scope", {}).get("frozen_target_rung") != "E1"
    ):
        raise ValueError("unsupported or incorrectly scoped D2 E1 corpus")

    declared = corpus.get("artifacts")
    if not isinstance(declared, list):
        raise ValueError("D2 E1 artifacts are missing")
    artifacts: dict[str, FrozenE1Artifact] = {}
    for item in declared:
        if not isinstance(item, dict):
            raise ValueError("D2 E1 artifact entry must be an object")
        artifact_id = _required_text(item, "artifact_id")
        if artifact_id in artifacts:
            raise ValueError(f"duplicate D2 E1 artifact: {artifact_id}")
        relative_path = _required_text(item, "path")
        expected_sha256 = _required_text(item, "sha256")
        body = _resolve_inside(repository_root, relative_path).read_bytes()
        if _sha256(body) != expected_sha256:
            raise ValueError(f"D2 E1 artifact checksum drifted: {artifact_id}")
        byte_length = item.get("byte_length")
        if byte_length is not None and byte_length != len(body):
            raise ValueError(f"D2 E1 artifact byte length drifted: {artifact_id}")
        artifacts[artifact_id] = FrozenE1Artifact(
            artifact_id=artifact_id,
            path=relative_path,
            sha256=expected_sha256,
            body=body,
            metadata=item,
        )
    if not _REQUIRED_ARTIFACT_IDS.issubset(artifacts):
        missing = sorted(_REQUIRED_ARTIFACT_IDS - artifacts.keys())
        raise ValueError(f"D2 E1 artifacts are incomplete: {missing}")

    raw_cases = corpus.get("e1_cases")
    if not isinstance(raw_cases, list):
        raise ValueError("D2 E1 cases are missing")
    cases = {_required_text(item, "case_id"): item for item in raw_cases if isinstance(item, dict)}
    if tuple(sorted(cases)) != _E1_CASE_IDS or len(cases) != len(raw_cases):
        raise ValueError("D2 E1 case set drifted")
    schema_contract = corpus.get("schema_contract")
    if not isinstance(schema_contract, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in schema_contract.items()
    ):
        raise ValueError("D2 E1 schema contract is missing")
    validate_schema_contract(schema_contract)
    projection_fields = corpus.get("factor_projection_fields")
    if not isinstance(projection_fields, list) or not all(isinstance(value, str) for value in projection_fields):
        raise ValueError("D2 E1 factor projection fields are missing")
    if set(projection_fields) & _PROVENANCE_FIELDS:
        raise ValueError("D2 E1 factor projection leaks provenance")
    evidence = corpus.get("e1_evidence")
    if not isinstance(evidence, dict):
        raise ValueError("D2 E1 evidence clock is missing")
    return FrozenE1Corpus(
        corpus_id=corpus["corpus_id"],
        corpus_sha256=corpus_sha256,
        artifacts=artifacts,
        cases=cases,
        schema_contract=cast(dict[str, str], schema_contract),
        factor_projection_fields=tuple(projection_fields),
        created_at=_aware(evidence.get("created_at"), label="e1_evidence.created_at"),
    )


def _corrected_price_artifact(corpus: FrozenE1Corpus, pipeline: PricePipelineRun) -> FrozenPriceArtifact:
    case = corpus.cases["e0-price-changed-vintage"]
    correction = case.get("correction")
    if not isinstance(correction, dict):
        raise ValueError("D2 E1 price correction is missing")
    original = pipeline.artifacts[0]
    original_row = original.source_row.encode()
    corrected_row = _required_text(correction, "source_row").encode()
    if original.body.count(original_row) != 1:
        raise ValueError("frozen price row must occur exactly once")
    body = original.body.replace(original_row, corrected_row)
    sha256 = _sha256(body)
    if sha256 != correction.get("derived_sha256"):
        raise ValueError("D2 E1 corrected price bytes drifted")
    row = next(
        csv.DictReader(
            StringIO(corrected_row.decode() + "\n"),
            fieldnames=("Date", "Open", "High", "Low", "Close", "Adj Close", "Volume"),
        )
    )
    payload = MarketPricePayload.model_validate(
        {
            **original.payload.model_dump(mode="json"),
            "open": row["Open"],
            "high": row["High"],
            "low": row["Low"],
            "close": row["Close"],
            "volume": int(row["Volume"]),
            "knowable_at": _aware(correction.get("knowable_at"), label="correction.knowable_at"),
            "produced_at": _aware(correction.get("produced_at"), label="correction.produced_at"),
            "recorded_at": _aware(correction.get("recorded_at"), label="correction.recorded_at"),
        }
    )
    reconciliation = PriceReconciliationEvidence(
        **original.reconciliation.model_dump(
            mode="python",
            exclude={"source_adjusted_close", "first_observed_at", "raw_object_id", "raw_object_sha256"},
        ),
        source_adjusted_close=_strict_decimal(row["Adj Close"], label="correction.adjusted_close"),
        first_observed_at=payload.knowable_at,
        raw_object_id=f"raw-object:{sha256}",
        raw_object_sha256=sha256,
    )
    return replace(
        original,
        artifact_id="nvda-daily-prices-e1-correction",
        sha256=sha256,
        body=body,
        source_row=corrected_row.decode(),
        payload=payload,
        reconciliation=reconciliation,
        supersedes_artifact_id=original.artifact_id,
    )


def _price_case(
    corpus: FrozenE1Corpus,
    repository_root: Path,
    connection: Connection[Any],
    raw_store: RawObjectStore,
    *,
    environment: str,
) -> tuple[D2E0Evidence, PricePipelineRun, D2E1CaseResult]:
    e0 = run_d2_e0(repository_root, connection, raw_store, environment=environment)
    original_run = run_price_pipeline(repository_root, connection, raw_store, environment=environment)
    correction = _corrected_price_artifact(corpus, original_run)

    class InjectedPriceFailure(RuntimeError):
        pass

    interrupted_run: PricePipelineRun | None = None
    try:
        with connection.transaction():
            interrupted_run = run_price_pipeline(
                repository_root,
                connection,
                raw_store,
                environment=environment,
                artifacts=(original_run.artifacts[0], correction),
            )
            raise InjectedPriceFailure("simulated D2 E1 price interruption")
    except InjectedPriceFailure:
        pass
    if interrupted_run is None:
        raise RuntimeError("D2 E1 failure injection did not execute")
    interrupted_changed = interrupted_run.records[-1]
    expected_existing = 0 if interrupted_run.inserted[-1] else 1
    raw_after_rollback = connection.execute(
        """
        select count(*)
        from raw.fetches
        where source = %s and source_record_id = %s and payload_sha256 = %s
        """,
        (DataSource.YAHOO.value, correction.source_record_id, correction.sha256),
    ).fetchone()
    normalized_after_rollback = connection.execute(
        "select count(*) from staging.normalized_records where normalized_record_id = %s",
        (interrupted_changed.normalized_record_id,),
    ).fetchone()
    rollback_restored_prior_state = (
        raw_after_rollback is not None
        and normalized_after_rollback is not None
        and raw_after_rollback[0] == expected_existing
        and normalized_after_rollback[0] == expected_existing
    )
    run = run_price_pipeline(
        repository_root,
        connection,
        raw_store,
        environment=environment,
        artifacts=(original_run.artifacts[0], correction),
    )
    replay = run_price_pipeline(
        repository_root,
        connection,
        raw_store,
        environment=environment,
        artifacts=(original_run.artifacts[0], correction),
    )
    source = next(entry for entry in run.registry.sources if entry.key == (SOURCE_ID, VERSION))
    repository = PostgresMarketPriceRepository(connection)
    before = repository.select_pit(
        subject=SUBJECT,
        semantic_type_id=SEMANTIC_TYPE_ID,
        semantic_type_version=VERSION,
        source_registry_entry_id=source.source_registry_entry_id,
        as_of=correction.payload.knowable_at - timedelta(microseconds=1),
        valid_on=correction.payload.trading_date,
    )
    at = repository.select_pit(
        subject=SUBJECT,
        semantic_type_id=SEMANTIC_TYPE_ID,
        semantic_type_version=VERSION,
        source_registry_entry_id=source.source_registry_entry_id,
        as_of=correction.payload.knowable_at,
        valid_on=correction.payload.trading_date,
    )
    original, changed = run.records
    passed = (
        e0.corpus_sha256 == corpus.corpus_sha256
        and len(run.records) == 2
        and not original.is_restatement
        and changed.is_restatement
        and changed.supersedes_record_id == original.normalized_record_id
        and before == (original,)
        and at == (changed,)
        and run.price_bars[-1].close == correction.payload.close
        and correction.reconciliation.source_adjusted_close != correction.payload.close
        and correction.reconciliation.factor_visible is False
        and rollback_restored_prior_state
        and run.inserted[-1] == interrupted_run.inserted[-1]
        and not any(replay.inserted)
    )
    return (
        e0,
        run,
        D2E1CaseResult(
            case_id="e0-price-changed-vintage",
            passed=passed,
            assertion_ids=(
                "raw-row-reparsed",
                "prior-vintage-retained",
                "pit-cutover-at-correction",
                "adjusted-close-reconciliation-only",
                "interrupted-transaction-rolled-back",
                "retry-and-repeat-idempotent",
            ),
            observed_ids=(original.normalized_record_id, changed.normalized_record_id),
        ),
    )


def _artifact_json(artifact: FrozenE1Artifact) -> dict[str, Any]:
    try:
        value = json.loads(artifact.body, parse_float=Decimal)
    except json.JSONDecodeError as error:
        raise ValueError(f"artifact is not valid JSON: {artifact.artifact_id}") from error
    if not isinstance(value, dict):
        raise ValueError(f"artifact is not a JSON object: {artifact.artifact_id}")
    return value


def _financial_facts(corpus: FrozenE1Corpus) -> tuple[FinancialFact, FinancialFact]:
    case = corpus.cases["plug-financial-filing-restatement"]
    expected = case.get("expected")
    if not isinstance(expected, dict):
        raise ValueError("PLUG restatement expectation is missing")
    source = _artifact_json(corpus.artifacts["plug-company-facts"])
    rows = source["facts"]["us-gaap"]["FinanceLeaseRightOfUseAssetAccumulatedAmortization"]["units"]["USD"]
    by_accession = {
        row["accn"]: row
        for row in rows
        if row.get("end") == "2020-12-31" and row.get("accn") in expected.get("accessions", [])
    }
    original_accession, amended_accession = cast(list[str], expected["accessions"])
    if set(by_accession) != {original_accession, amended_accession}:
        raise ValueError("PLUG restatement source rows drifted")
    original_time = _aware(expected.get("original_knowable_at"), label="original_knowable_at")
    amended_time = _aware(expected.get("amended_knowable_at"), label="amended_knowable_at")
    if original_time != _aware(
        corpus.artifacts["plug-original-filing"].metadata.get("accepted_at"),
        label="plug-original-filing.accepted_at",
    ) or amended_time != _aware(
        corpus.artifacts["plug-amended-filing"].metadata.get("accepted_at"),
        label="plug-amended-filing.accepted_at",
    ):
        raise ValueError("PLUG fact knowability must match the filing acceptance clocks")
    recorded_at = _aware(expected.get("recorded_at"), label="financial.recorded_at")
    original = FinancialFact(
        entity_id="issuer:plug",
        metric="finance_lease_right_of_use_asset_accumulated_amortization",
        unit="USD",
        fiscal_period="2020FY",
        valid_from=date(2020, 1, 1),
        valid_to=date(2020, 12, 31),
        value=_strict_decimal(by_accession[original_accession]["val"], label="original financial value"),
        knowable_at=original_time,
        recorded_at=recorded_at,
        confidence=Decimal("0.99"),
        raw_ref=f"fixture:plug-company-facts:{corpus.artifacts['plug-company-facts'].sha256}",
        source_metric="FinanceLeaseRightOfUseAssetAccumulatedAmortization",
        mapping_version="sec-companyfacts-restatement-probe:1.0.0",
        accession=original_accession,
        form="10-K",
    )
    amended = FinancialFact(
        entity_id="issuer:plug",
        metric="finance_lease_right_of_use_asset_accumulated_amortization",
        unit="USD",
        fiscal_period="2020FY",
        valid_from=date(2020, 1, 1),
        valid_to=date(2020, 12, 31),
        value=_strict_decimal(by_accession[amended_accession]["val"], label="amended financial value"),
        knowable_at=amended_time,
        recorded_at=recorded_at,
        confidence=Decimal("0.99"),
        raw_ref=f"fixture:plug-company-facts:{corpus.artifacts['plug-company-facts'].sha256}",
        source_metric="FinanceLeaseRightOfUseAssetAccumulatedAmortization",
        mapping_version="sec-companyfacts-restatement-probe:1.0.0",
        accession=amended_accession,
        form="10-K/A",
        is_restatement=True,
    )
    return original, amended


def _select_fact(facts: tuple[FinancialFact, ...], as_of: datetime) -> tuple[FinancialFact, ...]:
    candidates = [fact for fact in facts if fact.knowable_at <= as_of]
    return () if not candidates else (max(candidates, key=lambda fact: fact.knowable_at),)


def _financial_case(corpus: FrozenE1Corpus, d0: MvpCaptureTinyEvidence) -> tuple[FinancialFact, D2E1CaseResult]:
    expected = cast(dict[str, Any], corpus.cases["plug-financial-filing-restatement"]["expected"])
    original, amended = _financial_facts(corpus)
    d0_case = next(case for case in d0.cases if case.case_id == "append-only-restatement")
    original_id = "financial-fact:" + canonical_sha256(original.model_dump(mode="json"))
    amended_id = "financial-fact:" + canonical_sha256(amended.model_dump(mode="json"))
    facts = (original, amended)
    before_original = _select_fact(facts, original.knowable_at - timedelta(microseconds=1))
    at_original = _select_fact(facts, original.knowable_at)
    before_amended = _select_fact(facts, amended.knowable_at - timedelta(microseconds=1))
    at_amended = _select_fact(facts, amended.knowable_at)
    passed = (
        original.value == _strict_decimal(expected.get("original_value"), label="expected original value")
        and amended.value == _strict_decimal(expected.get("amended_value"), label="expected amended value")
        and not before_original
        and at_original == (original,)
        and before_amended == (original,)
        and at_amended == (amended,)
        and amended.is_restatement
        and d0_case.passed
        and len(d0_case.observed_ids) == 2
    )
    return amended, D2E1CaseResult(
        case_id="plug-financial-filing-restatement",
        passed=passed,
        assertion_ids=(
            "filing-and-fact-vintages-retained",
            "before-original-empty",
            "before-amendment-original",
            "at-amendment-restated",
        ),
        observed_ids=(
            original_id,
            amended_id,
            f"artifact:{corpus.artifacts['plug-original-filing'].sha256}",
            f"artifact:{corpus.artifacts['plug-amended-filing'].sha256}",
            *d0_case.observed_ids,
        ),
    )


def _select_actions(actions: tuple[CorporateAction, ...], as_of: datetime) -> tuple[CorporateAction, ...]:
    return tuple(action for action in actions if action.knowable_at <= as_of)


def _action_clock(action: CorporateAction, *, as_of: datetime) -> tuple[CorporateActionClockTick, ...]:
    lifecycle = [
        (phase, occurred_at)
        for phase, occurred_at in action.lifecycle_times().items()
        if max(occurred_at, action.knowable_at) <= as_of
    ]
    lifecycle.sort(key=lambda item: (max(item[1], action.knowable_at), item[0].value))
    return tuple(
        CorporateActionClockTick(
            tick_id=f"clock:{action.action_id}:{phase.value}",
            action_id=action.action_id,
            phase=phase,
            occurred_at=occurred_at,
            applied_at=max(occurred_at, action.knowable_at),
            sequence=index,
        )
        for index, (phase, occurred_at) in enumerate(lifecycle, start=1)
    )


def _rejects_duplicate_tick(
    *, action: CorporateAction, replay: V1ReturnReplay, calendar: ExchangeCalendar, bar: ListingPriceBar
) -> bool:
    clock = replay.action_clock
    duplicate = CorporateActionClockTick(
        tick_id=f"clock:{action.action_id}:duplicate",
        action_id=action.action_id,
        phase=clock[-1].phase,
        occurred_at=clock[-1].occurred_at,
        applied_at=clock[-1].applied_at,
        sequence=len(clock) + 1,
    )
    try:
        V1ReturnReplay.create(
            replay_id=f"{replay.replay_id}:duplicate",
            security_id=replay.security_id,
            share_class=replay.share_class,
            listing_id=replay.listing_id,
            as_of=replay.as_of,
            calendar=calendar,
            price_bars=(bar,),
            corporate_actions=(action,),
            action_clock=(*clock, duplicate),
        )
    except ValidationError:
        return True
    return False


def _rejects_adjusted_bar(bar: ListingPriceBar) -> bool:
    try:
        ListingPriceBar.model_validate(
            {**bar.model_dump(mode="json"), "price_basis": PriceBasis.ADJUSTED_RECONCILIATION_ONLY}
        )
    except ValidationError:
        return True
    return False


def _split_case(corpus: FrozenE1Corpus, price_run: PricePipelineRun) -> tuple[CorporateAction, D2E1CaseResult]:
    case = corpus.cases["nvda-split-lifecycle"]
    expected = cast(dict[str, Any], case["expected"])
    filing = corpus.artifacts["nvda-listing-identity"].body.decode("utf-8", errors="ignore")
    events = _artifact_json(corpus.artifacts["nvda-split-events"])
    split_values = events["chart"]["result"][0]["events"]["splits"].values()
    ratio = next(
        _strict_decimal(item["numerator"], label="split numerator")
        / _strict_decimal(item["denominator"], label="split denominator")
        for item in split_values
        if item.get("splitRatio") == expected["ratio"]
    )
    knowable_at = _aware(expected.get("knowable_at"), label="split.knowable_at")
    if knowable_at != _aware(
        corpus.artifacts["nvda-listing-identity"].metadata.get("accepted_at"),
        label="nvda-listing-identity.accepted_at",
    ):
        raise ValueError("NVDA split knowability must match the filing acceptance clock")
    action = CorporateAction(
        action_id="corporate-action:nvda:2024-06-10:10-for-1",
        action_type=CorporateActionType.SPLIT,
        security_id="security:cusip:67066G104",
        share_class="common",
        source_instrument_ids=("security:cusip:67066G104",),
        resulting_instrument_ids=("security:cusip:67066G104",),
        source_listing_id="listing:xnas:nvda",
        resulting_listing_id="listing:xnas:nvda",
        declared_at=knowable_at,
        knowable_at=knowable_at,
        ex_at=_aware(expected.get("ex_at"), label="split.ex_at"),
        effective_at=_aware(expected.get("effective_at"), label="split.effective_at"),
        split_ratio_after_per_before=ratio,
        recorded_at=knowable_at + timedelta(seconds=1),
        confidence=Decimal("0.99"),
        raw_ref=f"fixture:nvda-split-filing:{corpus.artifacts['nvda-listing-identity'].sha256}",
    )
    # Replay identity must not depend on an environment-local raw.fetches sequence.
    bar = ListingPriceBar.model_validate(
        {
            **price_run.price_bars[-1].model_dump(mode="python"),
            "raw_ref": price_run.records[-1].raw_object_id,
        }
    )
    as_of = price_run.payloads[-1].recorded_at
    replay = V1ReturnReplay.create(
        replay_id="replay:nvda:e1-split",
        security_id=action.security_id,
        share_class=action.share_class,
        listing_id=bar.listing_id,
        as_of=as_of,
        calendar=price_run.case.calendar,
        price_bars=(bar,),
        corporate_actions=(action,),
        action_clock=_action_clock(action, as_of=as_of),
    )
    before = _select_actions((action,), action.knowable_at - timedelta(microseconds=1))
    at = _select_actions((action,), action.knowable_at)
    passed = (
        "ten-for-one forward stock split" in filing.lower()
        and "June 10, 2024" in filing
        and ratio == Decimal("10")
        and not before
        and at == (action,)
        and not _action_clock(action, as_of=action.knowable_at - timedelta(microseconds=1))
        and len({(tick.action_id, tick.phase) for tick in replay.action_clock}) == len(replay.action_clock)
        and _rejects_duplicate_tick(action=action, replay=replay, calendar=price_run.case.calendar, bar=bar)
        and _rejects_adjusted_bar(bar)
    )
    return action, D2E1CaseResult(
        case_id="nvda-split-lifecycle",
        passed=passed,
        assertion_ids=(
            "before-knowable-excluded",
            "at-knowable-included",
            "effective-before-observation-retained",
            "each-phase-exactly-once",
            "adjusted-price-rejected",
        ),
        observed_ids=(action.action_id, replay.replay_id, replay.content_sha256),
    )


def _csv_row(body: bytes, trading_date: date) -> dict[str, str]:
    rows = [row for row in csv.DictReader(StringIO(body.decode())) if row.get("Date") == trading_date.isoformat()]
    if len(rows) != 1:
        raise ValueError("price fixture must contain exactly one requested row")
    return rows[0]


def _dividend_case(corpus: FrozenE1Corpus) -> tuple[CorporateAction, D2E1CaseResult]:
    case = corpus.cases["jpm-dividend-lifecycle"]
    expected = cast(dict[str, Any], case["expected"])
    statement = _artifact_json(corpus.artifacts["jpm-dividend-statement"])
    events = _artifact_json(corpus.artifacts["jpm-dividend-events"])
    dividend_values = events["chart"]["result"][0]["events"]["dividends"].values()
    amount = _strict_decimal(statement["amount_per_share"], label="dividend amount")
    event = next(item for item in dividend_values if _strict_decimal(item["amount"], label="event amount") == amount)
    knowable_at = _aware(expected.get("knowable_at"), label="dividend.knowable_at")
    published_on = _required_text(statement, "published_on")
    if expected.get("date_to_clock_policy") != "end-of-publication-date-utc" or knowable_at != _aware(
        f"{published_on}T23:59:59Z",
        label="dividend.publication_clock",
    ):
        raise ValueError("JPM dividend date-only publication policy drifted")
    ex_at = datetime.fromtimestamp(int(event["date"]), UTC)
    action = CorporateAction(
        action_id="corporate-action:jpm:2025q2:cash-dividend",
        action_type=CorporateActionType.CASH_DIVIDEND,
        security_id="security:cusip:46625H100",
        share_class="common",
        source_instrument_ids=("security:cusip:46625H100",),
        source_listing_id="listing:xnys:jpm",
        declared_at=knowable_at,
        knowable_at=knowable_at,
        ex_at=ex_at,
        record_at=_aware(expected.get("record_at"), label="dividend.record_at"),
        pay_at=_aware(expected.get("pay_at"), label="dividend.pay_at"),
        cash_amount_per_share=amount,
        cash_currency=_required_text(statement, "currency"),
        recorded_at=_aware(expected.get("recorded_at"), label="dividend.recorded_at"),
        confidence=Decimal("0.99"),
        raw_ref=f"fixture:jpm-dividend:{corpus.artifacts['jpm-dividend-statement'].sha256}",
    )
    trading_date = ex_at.date()
    session = MarketSession(
        session_date=trading_date,
        opens_at=ex_at,
        closes_at=_aware(expected.get("session_close_at"), label="dividend.session_close_at"),
        kind=MarketSessionKind.EARLY_CLOSE,
    )
    calendar = ExchangeCalendar.create(
        calendar_id="calendar:us-equities",
        calendar_version="2025-07-03.fixture-v1",
        exchange_mic="XNYS",
        timezone="America/New_York",
        valid_from=trading_date,
        valid_to=trading_date,
        sessions=(session,),
    )
    row = _csv_row(corpus.artifacts["jpm-daily-prices"].body, trading_date)
    observed_at = _aware(expected.get("price_observed_at"), label="dividend.price_observed_at")
    bar = ListingPriceBar(
        input_id="price-bar:listing:xnys:jpm:2025-07-03:unadjusted",
        listing_id="listing:xnys:jpm",
        calendar_id=calendar.calendar_id,
        calendar_version=calendar.calendar_version,
        trading_date=trading_date,
        session_close_at=session.closes_at,
        open=_strict_decimal(row["Open"], label="JPM open"),
        high=_strict_decimal(row["High"], label="JPM high"),
        low=_strict_decimal(row["Low"], label="JPM low"),
        close=_strict_decimal(row["Close"], label="JPM close"),
        volume=int(row["Volume"]),
        currency="USD",
        price_basis=PriceBasis.UNADJUSTED,
        knowable_at=observed_at,
        recorded_at=observed_at + timedelta(seconds=1),
        confidence=Decimal("0.99"),
        raw_ref=f"fixture:jpm-prices:{corpus.artifacts['jpm-daily-prices'].sha256}",
    )
    replay_as_of = observed_at + timedelta(seconds=2)
    replay = V1ReturnReplay.create(
        replay_id="replay:jpm:e1-dividend",
        security_id=action.security_id,
        share_class=action.share_class,
        listing_id=bar.listing_id,
        as_of=replay_as_of,
        calendar=calendar,
        price_bars=(bar,),
        corporate_actions=(action,),
        action_clock=_action_clock(action, as_of=replay_as_of),
    )
    missing_pay_rejected = False
    try:
        V1ReturnReplay.create(
            replay_id="replay:jpm:e1-dividend-missing-pay",
            security_id=action.security_id,
            share_class=action.share_class,
            listing_id=bar.listing_id,
            as_of=replay_as_of,
            calendar=calendar,
            price_bars=(bar,),
            corporate_actions=(action,),
            action_clock=tuple(tick for tick in replay.action_clock if tick.phase is not CorporateActionPhase.PAY),
        )
    except ValidationError:
        missing_pay_rejected = True
    phases = {tick.phase for tick in replay.action_clock}
    before = _select_actions((action,), action.knowable_at - timedelta(microseconds=1))
    at = _select_actions((action,), action.knowable_at)
    passed = (
        statement.get("record_date") == "2025-07-03"
        and statement.get("pay_date") == "2025-07-31"
        and amount == _strict_decimal(expected.get("amount_per_share"), label="expected dividend amount")
        and action.cash_currency == expected.get("currency")
        and not before
        and at == (action,)
        and {tick.phase for tick in _action_clock(action, as_of=action.knowable_at)}
        == {CorporateActionPhase.DECLARATION, CorporateActionPhase.KNOWABLE}
        and ex_at == _aware(expected.get("ex_at"), label="dividend.ex_at")
        and bar.close == _strict_decimal(row["Close"], label="JPM close")
        and bar.close != _strict_decimal(row["Adj Close"], label="JPM adjusted close")
        and {CorporateActionPhase.EX, CorporateActionPhase.RECORD, CorporateActionPhase.PAY} <= phases
        and missing_pay_rejected
        and _rejects_duplicate_tick(action=action, replay=replay, calendar=calendar, bar=bar)
    )
    return action, D2E1CaseResult(
        case_id="jpm-dividend-lifecycle",
        passed=passed,
        assertion_ids=(
            "date-only-publication-policy-frozen",
            "before-knowable-excluded",
            "ex-record-pay-exactly-once",
            "missing-pay-rejected",
            "unadjusted-price-only",
        ),
        observed_ids=(action.action_id, replay.replay_id, replay.content_sha256),
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_text(element: ElementTree.Element, name: str) -> str | None:
    return next(
        (child.text.strip() for child in element.iter() if _local_name(child.tag) == name and child.text),
        None,
    )


@dataclass(frozen=True)
class MembershipVintage:
    report_date: date
    knowable_at: datetime
    records: tuple[UniverseMembership, ...]
    manifest: UniverseManifest
    selected_membership_id: str
    snapshot_id: str


def _membership_vintage(
    artifact: FrozenE1Artifact,
    *,
    knowable_at: datetime,
    selected_name: str,
) -> MembershipVintage:
    if knowable_at != _aware(artifact.metadata.get("accepted_at"), label=f"{artifact.artifact_id}.accepted_at"):
        raise ValueError("N-PORT knowability must match the SEC acceptance clock")
    root = ElementTree.fromstring(artifact.body)
    report_text = _xml_text(root, "repPdDate")
    if report_text is None:
        raise ValueError("N-PORT report date is missing")
    report_date = date.fromisoformat(report_text)
    holdings: list[tuple[str, str, Decimal, str]] = []
    securities = tuple(item for item in root.iter() if _local_name(item.tag) == "invstOrSec")
    for position, security in enumerate(securities):
        name = _xml_text(security, "name")
        cusip = _xml_text(security, "cusip")
        weight = _xml_text(security, "pctVal")
        if name and cusip and weight:
            identity = (
                f"cusip:{cusip.lower()}"
                if cusip.upper() != "N/A"
                else "nport:"
                + canonical_sha256(
                    {
                        "artifact_sha256": artifact.sha256,
                        "position": position,
                        "security": ElementTree.tostring(security, encoding="unicode"),
                    }
                )[:24]
            )
            holdings.append((name, cusip, _strict_decimal(weight, label=f"{name} weight"), identity))
    holdings_by_identity: dict[str, tuple[str, str, Decimal]] = {}
    for holding_name, holding_cusip, holding_weight, identity in holdings:
        prior = holdings_by_identity.setdefault(identity, (holding_name, holding_cusip, holding_weight))
        if prior != (holding_name, holding_cusip, holding_weight):
            raise ValueError(f"N-PORT security identity has conflicting rows: {identity}")
    holdings = [
        (name, cusip, weight, identity) for identity, (name, cusip, weight) in sorted(holdings_by_identity.items())
    ]
    matches = [holding for holding in holdings if holding[0] == selected_name]
    if len(matches) != 1:
        raise ValueError(f"N-PORT selected membership drifted: {selected_name}")
    selected_identity = matches[0][3]
    records = tuple(
        UniverseMembership(
            membership_id=f"membership:qqq:{report_date.isoformat()}:{identity}",
            universe_id="universe:qqq-nport-history",
            subject=SubjectRef(kind=SubjectKind.SECURITY, id=f"security:{identity}"),
            valid_from=report_date,
            valid_to=report_date,
            knowable_at=knowable_at,
            recorded_at=datetime(2026, 7, 11, 19, 4, 55, tzinfo=UTC),
            confidence=Decimal("0.99"),
            raw_ref=f"fixture:{artifact.artifact_id}:{artifact.sha256}",
        )
        for _name, _cusip, _weight, identity in holdings
    )
    manifest = UniverseManifest.create(
        universe_id="universe:qqq-nport-history",
        universe_version=f"nport-{report_date.isoformat()}",
        definition_kind=UniverseDefinitionKind.FIXED_COHORT,
        membership_ids=tuple(record.membership_id for record in records),
        effective_at=knowable_at,
        owner="batch:D2-mvp-medium-validation",
    )
    snapshot_hash = canonical_sha256(
        {
            "artifact_sha256": artifact.sha256,
            "manifest": manifest.model_dump(mode="json"),
            "memberships": [record.model_dump(mode="json") for record in records],
            "weights": [(name, cusip, str(weight), identity) for name, cusip, weight, identity in holdings],
        }
    )
    return MembershipVintage(
        report_date=report_date,
        knowable_at=knowable_at,
        records=records,
        manifest=manifest,
        selected_membership_id=f"membership:qqq:{report_date.isoformat()}:{selected_identity}",
        snapshot_id=f"membership-snapshot:{snapshot_hash}",
    )


def _select_membership_vintage(vintages: tuple[MembershipVintage, ...], as_of: datetime) -> MembershipVintage | None:
    candidates = [vintage for vintage in vintages if vintage.knowable_at <= as_of]
    return None if not candidates else max(candidates, key=lambda vintage: vintage.knowable_at)


def _resolve_memberships(vintages: tuple[MembershipVintage, ...], as_of: datetime) -> tuple[UniverseMembership, ...]:
    vintage = _select_membership_vintage(vintages, as_of)
    if vintage is None:
        return ()
    records = tuple(record for record in vintage.records if record.knowable_at <= as_of)
    if tuple(sorted(record.membership_id for record in records)) != vintage.manifest.membership_ids:
        raise ValueError("resolved N-PORT memberships do not match the fixed manifest")
    return records


def _membership_case(corpus: FrozenE1Corpus) -> tuple[UniverseMembership, D2E1CaseResult]:
    case = corpus.cases["qqq-membership-vintages"]
    expected = cast(dict[str, Any], case["expected"])
    first = _membership_vintage(
        corpus.artifacts["qqq-membership-2025q4"],
        knowable_at=_aware(expected.get("first_knowable_at"), label="membership.first_knowable_at"),
        selected_name=_required_text(expected, "removed_name"),
    )
    second = _membership_vintage(
        corpus.artifacts["qqq-membership-2026q1"],
        knowable_at=_aware(expected.get("second_knowable_at"), label="membership.second_knowable_at"),
        selected_name=_required_text(expected, "added_name"),
    )
    vintages = (first, second)
    before_first = _resolve_memberships(vintages, first.knowable_at - timedelta(microseconds=1))
    at_first = _resolve_memberships(vintages, first.knowable_at)
    before_second = _resolve_memberships(vintages, second.knowable_at - timedelta(microseconds=1))
    at_second = _resolve_memberships(vintages, second.knowable_at)
    first_by_id = {record.membership_id: record for record in at_first}
    second_by_id = {record.membership_id: record for record in at_second}
    removed = first_by_id[first.selected_membership_id]
    added = second_by_id[second.selected_membership_id]
    first_subjects = {record.subject for record in at_first}
    second_subjects = {record.subject for record in at_second}
    passed = (
        first.report_date == date.fromisoformat(_required_text(expected, "first_report_date"))
        and second.report_date == date.fromisoformat(_required_text(expected, "second_report_date"))
        and not before_first
        and at_first == first.records
        and before_second == first.records
        and at_second == second.records
        and removed.subject in first_subjects - second_subjects
        and added.subject in second_subjects - first_subjects
        and set(first.manifest.membership_ids) == set(first_by_id)
        and set(second.manifest.membership_ids) == set(second_by_id)
        and first.snapshot_id != second.snapshot_id
    )
    return added, D2E1CaseResult(
        case_id="qqq-membership-vintages",
        passed=passed,
        assertion_ids=(
            "before-first-vintage-empty",
            "first-vintage-retained",
            "second-vintage-appended",
            "addition-and-removal-replayed",
            "decimal-weights-parsed",
        ),
        observed_ids=(
            first.snapshot_id,
            second.snapshot_id,
            first.manifest.ref.content_sha256,
            second.manifest.ref.content_sha256,
            removed.membership_id,
            added.membership_id,
        ),
    )


def _projection(
    *,
    subject: SubjectRef,
    payload: BaseModel,
    payload_model_key: str,
    valid_from: date,
    valid_to: date,
    confidence: Decimal,
    as_of: datetime,
) -> ProvenanceNeutralInput:
    return ProvenanceNeutralInput(
        subject=subject,
        payload_model_key=payload_model_key,
        payload_sha256=canonical_sha256(payload.model_dump(mode="json")),
        valid_from=valid_from,
        valid_to=valid_to,
        confidence=confidence,
        as_of=as_of,
    )


def _schema_and_projection_case(
    corpus: FrozenE1Corpus,
    *,
    e0: D2E0Evidence,
    d0: MvpCaptureTinyEvidence,
    price_run: PricePipelineRun,
    fact: FinancialFact,
    split: CorporateAction,
    dividend: CorporateAction,
    membership: UniverseMembership,
) -> tuple[tuple[str, ...], D2E1CaseResult]:
    original_payloads: tuple[tuple[str, BaseModel], ...] = (
        ("market-price", price_run.payloads[-1]),
        ("financial-fact", fact),
        ("corporate-action", split),
        ("corporate-action", dividend),
        ("universe-membership", membership),
    )
    routed_payloads = tuple(
        route_typed_payload(semantic_type, payload.model_dump(mode="json"))
        for semantic_type, payload in original_payloads
    )
    price = cast(MarketPricePayload, routed_payloads[0])
    fact = cast(FinancialFact, routed_payloads[1])
    split = cast(CorporateAction, routed_payloads[2])
    dividend = cast(CorporateAction, routed_payloads[3])
    membership = cast(UniverseMembership, routed_payloads[4])
    unknown_type_rejected = False
    try:
        route_typed_payload("unknown-semantic-type", {})
    except ValueError:
        unknown_type_rejected = True
    projections = (
        _projection(
            subject=SUBJECT,
            payload=price,
            payload_model_key="data_engine:MarketPricePayload",
            valid_from=price.trading_date,
            valid_to=price.trading_date,
            confidence=price.confidence,
            as_of=corpus.created_at,
        ),
        _projection(
            subject=SubjectRef(kind=SubjectKind.ISSUER, id=fact.entity_id),
            payload=fact,
            payload_model_key="truealpha_contracts:FinancialFact",
            valid_from=fact.valid_from,
            valid_to=fact.valid_to,
            confidence=fact.confidence,
            as_of=corpus.created_at,
        ),
        _projection(
            subject=SubjectRef(kind=SubjectKind.SECURITY, id=split.security_id),
            payload=split,
            payload_model_key="truealpha_contracts:CorporateAction",
            valid_from=cast(datetime, split.effective_at).date(),
            valid_to=cast(datetime, split.ex_at).date(),
            confidence=split.confidence,
            as_of=corpus.created_at,
        ),
        _projection(
            subject=SubjectRef(kind=SubjectKind.SECURITY, id=dividend.security_id),
            payload=dividend,
            payload_model_key="truealpha_contracts:CorporateAction",
            valid_from=cast(datetime, dividend.ex_at).date(),
            valid_to=cast(datetime, dividend.pay_at).date(),
            confidence=dividend.confidence,
            as_of=corpus.created_at,
        ),
        _projection(
            subject=membership.subject,
            payload=membership,
            payload_model_key="truealpha_contracts:UniverseMembership",
            valid_from=membership.valid_from,
            valid_to=membership.valid_to or membership.valid_from,
            confidence=membership.confidence,
            as_of=corpus.created_at,
        ),
    )
    hashes = tuple(canonical_sha256(value.model_dump(mode="json")) for value in projections)
    field_sets = [set(value.model_dump(mode="json")) for value in projections]
    passed = (
        validate_schema_contract(corpus.schema_contract) == corpus.schema_contract
        and routed_payloads == tuple(payload for _semantic_type, payload in original_payloads)
        and unknown_type_rejected
        and all(fields == set(corpus.factor_projection_fields) for fields in field_sets)
        and not any(fields & _PROVENANCE_FIELDS for fields in field_sets)
        and len(set(hashes)) == len(hashes)
        and e0.fixture_runner_selection_id == e0.postgres_runner_selection_id
        and d0.fixture_runner_selection_id == d0.postgres_runner_selection_id
    )
    return hashes, D2E1CaseResult(
        case_id="schema-registry-provenance",
        passed=passed,
        assertion_ids=(
            "typed-schema-hashes-frozen",
            "dictionary-payload-dispatch",
            "unknown-payload-type-rejected",
            "provenance-neutral-contract-fields",
            "accepted-runner-selections-reused",
        ),
        observed_ids=(
            *hashes,
            e0.fixture_runner_selection_id,
            d0.fixture_runner_selection_id,
        ),
        blocker_codes=("e2.shared-multidomain-registry-path-required",),
    )


class D2E1Evidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(default="", pattern=r"^(?:|d2-e1-evidence:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    batch_id: Literal["D2-mvp-medium-validation"] = "D2-mvp-medium-validation"
    accepted_rung: Literal["E1"] = "E1"
    stable_handoff: Literal[False] = False
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    e0_evidence_id: str
    e0_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d0_e1_evidence_id: str
    d0_e1_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    registry_snapshot_id: str
    registry_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    factor_projection_sha256s: tuple[str, ...] = Field(min_length=5, max_length=5)
    cases: tuple[D2E1CaseResult, ...] = Field(min_length=7, max_length=7)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def identify(self) -> "D2E1Evidence":
        cases = tuple(sorted(self.cases, key=lambda case: case.case_id))
        if tuple(case.case_id for case in cases) != _E1_CASE_IDS:
            raise ValueError("D2 E1 evidence case set drifted")
        object.__setattr__(self, "cases", cases)
        payload = self.model_dump(mode="json", exclude={"evidence_id", "content_sha256"})
        expected_hash = canonical_sha256(payload)
        expected_id = f"d2-e1-evidence:{expected_hash}"
        if self.content_sha256 and self.content_sha256 != expected_hash:
            raise ValueError("content_sha256 does not match D2 E1 evidence")
        if self.evidence_id and self.evidence_id != expected_id:
            raise ValueError("evidence_id does not match D2 E1 evidence")
        object.__setattr__(self, "content_sha256", expected_hash)
        object.__setattr__(self, "evidence_id", expected_id)
        return self


class D2E1EvidenceRepository:
    """Session-scoped append-only evidence repository for provisional E1."""

    def __init__(self, connection: Connection[Any]) -> None:
        self.connection = connection
        connection.execute(
            """
            create or replace function pg_temp.reject_d2_e1_evidence_mutation()
            returns trigger language plpgsql as $$
            begin
                raise exception 'D2 E1 evidence is append-only';
            end;
            $$
            """
        )
        connection.execute(
            f"""
            create temporary table if not exists {_EVIDENCE_TABLE} (
                evidence_id text primary key,
                content_sha256 text not null,
                payload jsonb not null
            ) on commit preserve rows
            """
        )
        connection.execute(f"drop trigger if exists reject_d2_e1_evidence_mutation on {_EVIDENCE_TABLE}")
        connection.execute(
            f"""
            create trigger reject_d2_e1_evidence_mutation
            before update or delete on {_EVIDENCE_TABLE}
            for each row execute function pg_temp.reject_d2_e1_evidence_mutation()
            """
        )

    def put(self, evidence: D2E1Evidence) -> bool:
        payload = evidence.model_dump(mode="json")
        inserted = self.connection.execute(
            f"""
            insert into {_EVIDENCE_TABLE} (evidence_id, content_sha256, payload)
            values (%s, %s, %s)
            on conflict (evidence_id) do nothing
            returning evidence_id
            """,
            (evidence.evidence_id, evidence.content_sha256, Jsonb(payload)),
        ).fetchone()
        if inserted is not None:
            return True
        existing = self.get(evidence.evidence_id)
        if existing != evidence:
            raise ValueError("D2 E1 evidence ID is already bound to different content")
        return False

    def get(self, evidence_id: str) -> D2E1Evidence | None:
        row = self.connection.execute(
            f"select payload from {_EVIDENCE_TABLE} where evidence_id = %s",
            (evidence_id,),
        ).fetchone()
        return None if row is None else D2E1Evidence.model_validate(row[0])


def run_d2_e1(
    repository_root: Path,
    connection: Connection[Any],
    raw_store: RawObjectStore,
    *,
    environment: str,
) -> D2E1Evidence:
    corpus = load_e1_corpus(repository_root)
    e0, price_run, price_case = _price_case(
        corpus,
        repository_root,
        connection,
        raw_store,
        environment=environment,
    )
    d0 = run_d0_e1(repository_root, connection)
    d0_case = D2E1CaseResult(
        case_id="d0-cross-domain-regression",
        passed=all(case.passed for case in d0.cases)
        and d0.fixture_snapshot_id == d0.postgres_snapshot_id
        and d0.fixture_runner_selection_id == d0.postgres_runner_selection_id,
        assertion_ids=(
            "financial-filing-price-split-corpus-rerun",
            "fixture-postgres-parity",
            "reordered-and-repeated-idempotency",
            "registry-routed-dispatch",
        ),
        observed_ids=(d0.evidence_id, d0.fixture_snapshot_id, d0.fixture_runner_selection_id),
    )
    fact, financial_case = _financial_case(corpus, d0)
    split, split_case = _split_case(corpus, price_run)
    dividend, dividend_case = _dividend_case(corpus)
    membership, membership_case = _membership_case(corpus)
    projections, projection_case = _schema_and_projection_case(
        corpus,
        e0=e0,
        d0=d0,
        price_run=price_run,
        fact=fact,
        split=split,
        dividend=dividend,
        membership=membership,
    )
    cases = (
        d0_case,
        price_case,
        financial_case,
        split_case,
        dividend_case,
        membership_case,
        projection_case,
    )
    evidence = D2E1Evidence(
        corpus_sha256=corpus.corpus_sha256,
        e0_evidence_id=e0.evidence_id,
        e0_evidence_sha256=e0.content_sha256,
        d0_e1_evidence_id=d0.evidence_id,
        d0_e1_evidence_sha256=d0.content_sha256,
        registry_snapshot_id=price_run.registry.registry_snapshot_id,
        registry_snapshot_sha256=price_run.registry.content_sha256,
        factor_projection_sha256s=projections,
        cases=cases,
        created_at=corpus.created_at,
    )
    D2E1EvidenceRepository(connection).put(evidence)
    failed = [case.case_id for case in evidence.cases if not case.passed]
    if failed:
        raise ValueError(f"D2 E1 cases failed: {failed}")
    return evidence


class D2E1Activation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: Literal["D2-mvp-medium-validation"] = "D2-mvp-medium-validation"
    environment: Literal["local", "ci"]
    release_allowed: Literal[False] = False


@dataclass(frozen=True)
class D2E1RunnerResource:
    repository_root: Path
    connection: Connection[Any]
    raw_store: RawObjectStore
    activation: D2E1Activation

    def run(self) -> D2E1Evidence:
        return run_d2_e1(
            self.repository_root,
            self.connection,
            self.raw_store,
            environment=self.activation.environment,
        )


@dg.asset(
    name=D2_E1_ASSET_NAME,
    group_name="mvp_medium_validation_e1",
    required_resource_keys={"mvp_medium_validation_e1_runner"},
    description="Execute the frozen D2 E1 timing matrix without release registration.",
)
def materialize_mvp_medium_validation_e1(context: AssetExecutionContext) -> dg.Output[D2E1Evidence]:
    runner = cast(D2E1RunnerResource, context.resources.mvp_medium_validation_e1_runner)
    evidence = runner.run()
    return dg.Output(
        evidence,
        metadata={
            "evidence_id": evidence.evidence_id,
            "case_count": len(evidence.cases),
            "stable_handoff": evidence.stable_handoff,
        },
        data_version=dg.DataVersion(evidence.content_sha256),
    )


def build_d2_e1_definitions(
    *,
    repository_root: Path,
    connection: Connection[Any],
    raw_store: RawObjectStore,
    activation: D2E1Activation | ReleaseManifest,
) -> dg.Definitions:
    if not isinstance(activation, D2E1Activation):
        raise ValueError("D2 E1 is batch-private and cannot consume a ReleaseManifest")
    return dg.Definitions(
        assets=[materialize_mvp_medium_validation_e1],
        resources={
            "mvp_medium_validation_e1_runner": cast(
                Any,
                D2E1RunnerResource(
                    repository_root=repository_root,
                    connection=connection,
                    raw_store=raw_store,
                    activation=activation,
                ),
            )
        },
    )


__all__ = [
    "D2_E1_ASSET_NAME",
    "D2E1Activation",
    "D2E1CaseResult",
    "D2E1Evidence",
    "D2E1EvidenceRepository",
    "D2E1RunnerResource",
    "build_d2_e1_definitions",
    "load_e1_corpus",
    "materialize_mvp_medium_validation_e1",
    "route_typed_payload",
    "run_d2_e1",
    "validate_schema_contract",
]
