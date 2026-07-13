"""Batch-private D2 E0 price slice over one frozen TOPT instrument."""

import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

import dagster as dg
from dagster import AssetExecutionContext
from data_engine.mvp_registry import build_filing_registry
from data_engine.raw_store import get_payload, insert_fetch, raw_ref
from psycopg import Connection
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from truealpha_contracts import RawCapture, RawObjectStore
from truealpha_contracts.capture_contracts import (
    ApplicabilityMapping,
    CaptureCell,
    CaptureEvaluationReport,
    CaptureManifest,
    CaptureRecordEvidence,
    CaptureRequirement,
    CaptureScope,
    SourceCoverageMapping,
    canonical_applicability_projection_sha256,
    canonical_source_coverage_projection_sha256,
    evaluate_capture_manifest,
)
from truealpha_contracts.common import CaptureEnvironment, canonical_sha256
from truealpha_contracts.data_quality import DataDomain, QualityStatus
from truealpha_contracts.execution import (
    FactorExecution,
    FactorInvocationTemplate,
    FactorKind,
    NormalizedRecordRef,
    PolicyBinding,
    PolicyRole,
    RunnerInputSelection,
    SemanticDraft,
    SemanticProducerKind,
    SnapshotCellSelection,
    SnapshotDemandCell,
    SnapshotManifest,
    SnapshotRequest,
    build_runner_input_selection,
)
from truealpha_contracts.market import ExchangeCalendar, ListingPriceBar, PriceBasis
from truealpha_contracts.models import DataSource
from truealpha_contracts.registries import (
    RegistrySnapshot,
    SemanticTypeRegistryEntry,
    SourceRegistryEntry,
)
from truealpha_contracts.release import ReleaseManifest
from truealpha_contracts.universe import (
    IssuerSecurityLink,
    SecurityListingLink,
    SubjectKind,
    SubjectRef,
    UniverseRef,
)
from truealpha_contracts.usage import DataRequirement, RequirementLevel

CORPUS_PATH = Path("apps/data-engine/tests/fixtures/mvp_medium_validation/corpus.v1.json")
BATCH_MANIFEST_PATH = Path("governance/batches/D2-mvp-medium-validation.v1.json")
D2_E0_ASSET_NAME = "mvp_medium_validation_e0_evidence"
SOURCE_ID = "source.fixture-yahoo-csv"
SEMANTIC_TYPE_ID = "semantic.market-price"
VERSION = "1.0.0"
PARTITION = "nvda:2026-03-31"
SUBJECT = SubjectRef(kind=SubjectKind.LISTING, id="listing:xnas:nvda")


def _hash(label: str) -> str:
    return canonical_sha256({"d2_mvp_medium_validation_e0": label})


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _git_blob(body: bytes) -> str:
    header = f"blob {len(body)}\0".encode()
    return hashlib.sha1(header + body, usedforsecurity=False).hexdigest()


def _repository_path(root: Path, relative_path: str) -> Path:
    if not relative_path:
        raise ValueError("fixture path is empty")
    pure = PurePosixPath(relative_path)
    if pure.is_absolute() or "\\" in relative_path or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError(f"fixture path escapes repository: {relative_path}")
    candidate = root.joinpath(*pure.parts)
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError as error:
        raise ValueError(f"fixture path escapes repository: {relative_path}") from error
    return candidate


def _aware(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return parsed


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _policy_hash(policy: dict[str, Any]) -> str:
    declared = policy.get("implementation_sha256")
    content = {key: value for key, value in policy.items() if key != "implementation_sha256"}
    expected = canonical_sha256(content)
    if declared != expected:
        raise ValueError(f"policy hash drifted: {policy.get('policy_id')}")
    return expected


class MarketPricePayload(BaseModel):
    """Source-neutral unadjusted price payload persisted by the E0 slice."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$")
    issuer_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$")
    security_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$")
    listing_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$")
    share_class: str = Field(min_length=1)
    exchange_mic: str = Field(pattern=r"^[A-Z0-9]{4}$")
    ticker: str = Field(pattern=r"^[A-Z.]+$")
    calendar_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$")
    calendar_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$")
    trading_date: date
    session_close_at: datetime
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: int = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    price_basis: Literal[PriceBasis.UNADJUSTED] = PriceBasis.UNADJUSTED
    knowable_at: datetime
    produced_at: datetime
    recorded_at: datetime
    confidence: Decimal = Field(ge=0, le=1)
    confidence_policy_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$")
    price_policy_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$")

    @field_validator("open", "high", "low", "close", "confidence", mode="before")
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        if isinstance(value, (float, bool)):
            raise ValueError("price and confidence inputs must not use binary floats")
        return value

    @field_validator("session_close_at", "knowable_at", "produced_at", "recorded_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{info.field_name} must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_price(self) -> "MarketPricePayload":
        decimals = (self.open, self.high, self.low, self.close, self.confidence)
        if any(not value.is_finite() for value in decimals):
            raise ValueError("price and confidence values must be finite Decimals")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high is below another OHLC value")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low is above another OHLC value")
        if self.knowable_at < self.session_close_at:
            raise ValueError("price cannot be knowable before the bound session closes")
        if not (self.knowable_at <= self.produced_at <= self.recorded_at):
            raise ValueError("price normalization timestamps are out of order")
        return self


class PriceReconciliationEvidence(BaseModel):
    """Raw-only adjusted close retained for reconciliation, never execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_id: str
    listing_id: str
    trading_date: date
    source_adjusted_close: Decimal = Field(gt=0)
    first_observed_at: datetime
    use: Literal["reconciliation-only"] = "reconciliation-only"
    factor_visible: Literal[False] = False
    raw_object_id: str
    raw_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("source_adjusted_close", mode="before")
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        if isinstance(value, (float, bool)):
            raise ValueError("adjusted-close evidence must not use binary floats")
        return value

    @field_validator("first_observed_at")
    @classmethod
    def validate_first_observed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("first_observed_at must be timezone-aware")
        return value


@dataclass(frozen=True)
class FrozenPriceArtifact:
    artifact_id: str
    source_record_id: str
    sha256: str
    body: bytes
    source_row: str
    payload: MarketPricePayload
    reconciliation: PriceReconciliationEvidence
    supersedes_artifact_id: str | None = None


@dataclass(frozen=True)
class FrozenPriceCase:
    corpus_sha256: str
    corpus_id: str
    d1_handoff_id: str
    d1_handoff_sha256: str
    universe: UniverseRef
    calendar: ExchangeCalendar
    issuer_security: IssuerSecurityLink
    security_listing: SecurityListingLink
    artifact: FrozenPriceArtifact
    snapshot_as_of: datetime
    before_knowable_at: datetime
    at_knowable_at: datetime
    stale_as_of: datetime
    maximum_age: timedelta
    confidence_policy: dict[str, Any]
    price_policy: dict[str, Any]
    registry_contract: dict[str, Any]


def _verify_artifact(root: Path, item: dict[str, Any]) -> bytes:
    relative_path = item.get("path")
    if not isinstance(relative_path, str):
        raise ValueError("frozen artifact path is missing")
    path = _repository_path(root, relative_path)
    body = path.read_bytes()
    if _sha256(body) != item.get("sha256"):
        raise ValueError(f"frozen artifact checksum drifted: {item.get('artifact_id')}")
    if item.get("git_blob") != _git_blob(body):
        raise ValueError(f"frozen artifact Git blob drifted: {item.get('artifact_id')}")
    byte_length = item.get("byte_length")
    if byte_length is not None and byte_length != len(body):
        raise ValueError(f"frozen artifact byte length drifted: {item.get('artifact_id')}")
    return body


def _selected_price_row(body: bytes, trading_date: date) -> tuple[str, dict[str, str]]:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("price fixture is not UTF-8 CSV") from error
    reader = csv.DictReader(StringIO(text))
    expected_header = ["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume"]
    if reader.fieldnames != expected_header:
        raise ValueError("price fixture header drifted")
    selected = [row for row in reader if row.get("Date") == trading_date.isoformat()]
    if len(selected) != 1:
        raise ValueError("price fixture must contain exactly one target trading date")
    row = selected[0]
    source_row = ",".join(row[name] for name in expected_header)
    return source_row, row


def _decimal_text(value: str, *, label: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{label} is not a Decimal literal") from error
    if not parsed.is_finite():
        raise ValueError(f"{label} must be finite")
    return parsed


def _validate_artifact_semantics(artifact: FrozenPriceArtifact) -> None:
    """Prove the supplied typed values were parsed from these exact CSV bytes."""
    payload = artifact.payload
    reconciliation = artifact.reconciliation
    source_row, row = _selected_price_row(artifact.body, payload.trading_date)
    if source_row != artifact.source_row:
        raise ValueError("price artifact source-row binding drifted")
    expected_prices = {
        "open": _decimal_text(row["Open"], label="open"),
        "high": _decimal_text(row["High"], label="high"),
        "low": _decimal_text(row["Low"], label="low"),
        "close": _decimal_text(row["Close"], label="close"),
    }
    if any(getattr(payload, field) != value for field, value in expected_prices.items()):
        raise ValueError("price payload values do not match the bound CSV row")
    try:
        volume = int(row["Volume"])
    except ValueError as error:
        raise ValueError("volume is not an integer literal") from error
    if payload.volume != volume:
        raise ValueError("price payload volume does not match the bound CSV row")
    adjusted_close = _decimal_text(row["Adj Close"], label="adjusted_close")
    if (
        reconciliation.listing_id != payload.listing_id
        or reconciliation.trading_date != payload.trading_date
        or reconciliation.source_adjusted_close != adjusted_close
        or reconciliation.first_observed_at != payload.knowable_at
        or reconciliation.raw_object_id != f"raw-object:{artifact.sha256}"
        or reconciliation.raw_object_sha256 != artifact.sha256
    ):
        raise ValueError("adjusted-close reconciliation does not match the bound CSV row")


class FrozenPriceAdapter:
    """Load one exact NVDA row and its frozen identity and policy context."""

    def load(self, root: Path, corpus_path: Path = CORPUS_PATH, *, environment: str) -> FrozenPriceCase:
        corpus_file = _repository_path(root, corpus_path.as_posix())
        corpus_bytes = corpus_file.read_bytes()
        corpus_sha256 = _sha256(corpus_bytes)
        batch = json.loads(_repository_path(root, BATCH_MANIFEST_PATH.as_posix()).read_text(encoding="utf-8"))
        if batch.get("corpus", {}).get("manifest_path") != corpus_path.as_posix():
            raise ValueError("D2 batch does not bind the requested corpus path")
        if batch.get("corpus", {}).get("sha256") != corpus_sha256:
            raise ValueError("D2 corpus bytes do not match the canonical batch manifest")
        try:
            corpus = json.loads(corpus_bytes)
        except json.JSONDecodeError as error:
            raise ValueError("D2 corpus is not valid JSON") from error
        if not isinstance(corpus, dict) or corpus.get("schema_version") != 1:
            raise ValueError("unsupported D2 corpus")

        producer = corpus.get("producer_handoff")
        if not isinstance(producer, dict):
            raise ValueError("D2 producer handoff is missing")
        handoff_path = producer.get("path")
        if not isinstance(handoff_path, str):
            raise ValueError("D2 producer handoff path is missing")
        handoff_bytes = _repository_path(root, handoff_path).read_bytes()
        if _sha256(handoff_bytes) != producer.get("sha256"):
            raise ValueError("D1 handoff checksum drifted")
        handoff = json.loads(handoff_bytes)
        revocation = handoff.get("revocation")
        if (
            handoff.get("handoff_id") != producer.get("handoff_id")
            or handoff.get("state") != "accepted"
            or "D2-mvp-medium-validation" not in handoff.get("allowed_consumers", [])
            or environment not in handoff.get("allowed_environments", [])
            or not isinstance(revocation, dict)
            or any(revocation.get(key) is not None for key in ("reason", "revoked_at", "superseded_by"))
        ):
            raise ValueError("D1 handoff does not authorize this D2 execution")

        declared = corpus.get("artifacts")
        if not isinstance(declared, list):
            raise ValueError("D2 artifacts are missing")
        artifacts = {
            item.get("artifact_id"): item
            for item in declared
            if isinstance(item, dict) and isinstance(item.get("artifact_id"), str)
        }
        required_ids = {
            "source-capture-manifest",
            "nvda-daily-prices",
            "nvda-listing-identity",
            "topt-candidate-denominator",
        }
        if not required_ids.issubset(artifacts):
            raise ValueError("D2 required artifacts are incomplete")
        bodies = {artifact_id: _verify_artifact(root, artifacts[artifact_id]) for artifact_id in required_ids}

        capture_manifest = json.loads(bodies["source-capture-manifest"])
        price_item = artifacts["nvda-daily-prices"]
        matching_capture = [
            item
            for item in capture_manifest.get("artifacts", [])
            if item.get("path") == price_item.get("capture_manifest_artifact_path")
        ]
        if (
            capture_manifest.get("capture_id") != artifacts["source-capture-manifest"].get("capture_id")
            or len(matching_capture) != 1
            or matching_capture[0].get("sha256") != price_item.get("sha256")
            or matching_capture[0].get("byte_length") != len(bodies["nvda-daily-prices"])
        ):
            raise ValueError("source capture manifest does not bind the NVDA price bytes")

        candidate = json.loads(bodies["topt-candidate-denominator"])
        universe_context = corpus.get("universe_context")
        if not isinstance(universe_context, dict):
            raise ValueError("TOPT universe context is missing")
        scope = candidate.get("scope")
        selected = scope.get("selected_instruments", []) if isinstance(scope, dict) else []
        expected_instrument = universe_context.get("selected_instrument")
        selected_instrument_matches = (
            isinstance(expected_instrument, dict)
            and isinstance(selected, list)
            and any(
                isinstance(item, dict) and all(item.get(key) == value for key, value in expected_instrument.items())
                for item in selected
            )
        )
        if (
            candidate.get("state") != "candidate_unapproved"
            or not isinstance(scope, dict)
            or scope.get("universe_id") != universe_context.get("universe_id")
            or scope.get("source", {}).get("accession") != universe_context.get("accession")
            or scope.get("minimums", {}).get("issuers") != universe_context.get("issuer_count")
            or scope.get("minimums", {}).get("instruments") != universe_context.get("instrument_count")
            or not selected_instrument_matches
        ):
            raise ValueError("TOPT candidate denominator or NVDA identity drifted")

        cases = corpus.get("cases")
        if not isinstance(cases, list) or len(cases) != 1 or cases[0].get("case_id") != "nvda-price-2026-03-31":
            raise ValueError("D2 E0 requires exactly one frozen price case")
        case = cases[0]
        expected = case.get("expected")
        if not isinstance(expected, dict):
            raise ValueError("D2 E0 expected contract is missing")
        calendar = ExchangeCalendar.model_validate(expected.get("calendar"))
        identity = expected.get("identity")
        if not isinstance(identity, dict):
            raise ValueError("D2 E0 identity contract is missing")
        issuer_security = IssuerSecurityLink.model_validate(identity.get("issuer_security_link"))
        security_listing = SecurityListingLink.model_validate(identity.get("security_listing_link"))
        if issuer_security.security_id != security_listing.security_id:
            raise ValueError("D2 E0 identity links do not share one security")
        identity_evidence = artifacts["nvda-listing-identity"].get("identity_evidence")
        identity_artifact_sha256 = _required_str(artifacts["nvda-listing-identity"], "sha256")
        if (
            not isinstance(identity_evidence, dict)
            or security_listing.ticker != identity_evidence.get("ticker")
            or security_listing.exchange_mic != identity_evidence.get("exchange_mic")
            or identity_artifact_sha256 not in issuer_security.raw_ref
            or identity_artifact_sha256 not in security_listing.raw_ref
        ):
            raise ValueError("frozen issuer/security/listing links drifted from their immutable filing evidence")

        price_contract = expected.get("price_bar")
        normalization = expected.get("normalization")
        reconciliation_contract = expected.get("reconciliation")
        snapshot = expected.get("snapshot")
        if not all(
            isinstance(value, dict) for value in (price_contract, normalization, reconciliation_contract, snapshot)
        ):
            raise ValueError("D2 E0 price, normalization, reconciliation, or snapshot contract is missing")
        assert isinstance(price_contract, dict)
        assert isinstance(normalization, dict)
        assert isinstance(reconciliation_contract, dict)
        assert isinstance(snapshot, dict)
        trading_date = date.fromisoformat(str(price_contract.get("trading_date")))
        source_row, row = _selected_price_row(bodies["nvda-daily-prices"], trading_date)
        if source_row != case.get("source_row"):
            raise ValueError("frozen NVDA source row drifted")
        session = calendar.require_session(trading_date)
        if session.closes_at != _aware(price_contract.get("session_close_at"), label="session_close_at"):
            raise ValueError("price bar session close does not match the frozen calendar")

        confidence_policy = normalization.get("confidence_policy")
        price_policy = normalization.get("price_policy")
        freshness_policy = normalization.get("freshness_policy")
        if not all(isinstance(value, dict) for value in (confidence_policy, price_policy, freshness_policy)):
            raise ValueError("D2 E0 policies are incomplete")
        assert isinstance(confidence_policy, dict)
        assert isinstance(price_policy, dict)
        assert isinstance(freshness_policy, dict)
        _policy_hash(confidence_policy)
        _policy_hash(price_policy)
        _policy_hash(freshness_policy)
        confidence = _decimal_text(str(confidence_policy.get("assigned_confidence")), label="confidence")
        share_class = issuer_security.share_class
        if share_class is None:
            raise ValueError("D2 E0 common stock identity requires a share class")
        if price_contract.get("price_basis") != PriceBasis.UNADJUSTED.value:
            raise ValueError("D2 E0 executable price basis must be unadjusted")
        payload = MarketPricePayload(
            input_id=_required_str(price_contract, "input_id"),
            issuer_id=issuer_security.issuer_id,
            security_id=issuer_security.security_id,
            listing_id=security_listing.listing_id,
            share_class=share_class,
            exchange_mic=security_listing.exchange_mic,
            ticker=security_listing.ticker,
            calendar_id=calendar.calendar_id,
            calendar_version=calendar.calendar_version,
            trading_date=trading_date,
            session_close_at=session.closes_at,
            open=_decimal_text(row["Open"], label="open"),
            high=_decimal_text(row["High"], label="high"),
            low=_decimal_text(row["Low"], label="low"),
            close=_decimal_text(row["Close"], label="close"),
            volume=int(row["Volume"]),
            currency=_required_str(price_contract, "currency"),
            price_basis=PriceBasis.UNADJUSTED,
            knowable_at=_aware(price_contract.get("knowable_at"), label="knowable_at"),
            produced_at=_aware(normalization.get("produced_at"), label="produced_at"),
            recorded_at=_aware(price_contract.get("recorded_at"), label="recorded_at"),
            confidence=confidence,
            confidence_policy_id=_required_str(confidence_policy, "policy_id"),
            price_policy_id=_required_str(price_policy, "policy_id"),
        )
        expected_prices = {name: str(price_contract.get(name)) for name in ("open", "high", "low", "close")}
        actual_prices = {name: str(getattr(payload, name)) for name in expected_prices}
        if actual_prices != expected_prices or payload.volume != price_contract.get("volume"):
            raise ValueError("frozen NVDA Decimal OHLCV values drifted")
        if payload.listing_id != SUBJECT.id or security_listing.valid_from > trading_date:
            raise ValueError("frozen listing identity does not cover the price date")
        if (
            security_listing.trading_calendar_id != calendar.calendar_id
            or security_listing.trading_calendar_version != calendar.calendar_version
            or security_listing.currency != payload.currency
            or security_listing.exchange_mic != calendar.exchange_mic
            or security_listing.timezone != calendar.timezone
        ):
            raise ValueError("listing, calendar, and price contracts are not bound to the same market line")

        if reconciliation_contract.get("use") != "reconciliation-only":
            raise ValueError("adjusted close must remain reconciliation-only")
        if reconciliation_contract.get("factor_visible") is not False:
            raise ValueError("adjusted close must not be factor-visible")
        reconciliation = PriceReconciliationEvidence(
            input_id=_required_str(reconciliation_contract, "input_id"),
            listing_id=payload.listing_id,
            trading_date=payload.trading_date,
            source_adjusted_close=_decimal_text(row["Adj Close"], label="adjusted_close"),
            first_observed_at=payload.knowable_at,
            use="reconciliation-only",
            factor_visible=False,
            raw_object_id=_required_str(normalization, "raw_object_id"),
            raw_object_sha256=_required_str(price_item, "sha256"),
        )
        if str(reconciliation.source_adjusted_close) != str(reconciliation_contract.get("source_adjusted_close")):
            raise ValueError("frozen adjusted-close reconciliation value drifted")

        maximum_age_days = freshness_policy.get("maximum_age_days")
        if not isinstance(maximum_age_days, int) or maximum_age_days <= 0:
            raise ValueError("freshness maximum_age_days is invalid")
        before = _aware(snapshot.get("before_knowable_at"), label="before_knowable_at")
        at = _aware(snapshot.get("at_knowable_at"), label="at_knowable_at")
        stale = _aware(snapshot.get("stale_as_of"), label="stale_as_of")
        snapshot_as_of = _aware(snapshot.get("as_of"), label="snapshot.as_of")
        if (
            before + timedelta(microseconds=1) != payload.knowable_at
            or at != payload.knowable_at
            or stale <= payload.knowable_at + timedelta(days=maximum_age_days)
            or snapshot_as_of < payload.recorded_at
            or date.fromisoformat(str(snapshot.get("valid_on"))) != payload.trading_date
        ):
            raise ValueError("D2 E0 PIT or freshness boundaries drifted")

        universe = UniverseRef.model_validate(expected.get("universe_ref"))
        expected_universe_sha256 = canonical_sha256(
            {
                "universe_id": universe_context.get("universe_id"),
                "universe_version": universe.universe_version,
                "accession": universe_context.get("accession"),
                "report_date": universe_context.get("report_date"),
                "issuer_count": universe_context.get("issuer_count"),
                "instrument_count": universe_context.get("instrument_count"),
            }
        )
        if (
            universe.universe_id != universe_context.get("universe_id")
            or universe.content_sha256 != expected_universe_sha256
        ):
            raise ValueError("D2 E0 universe reference does not bind the frozen TOPT denominator")
        registry_contract = expected.get("registry_contract")
        if not isinstance(registry_contract, dict):
            raise ValueError("D2 E0 registry contract is missing")
        artifact = FrozenPriceArtifact(
            artifact_id="nvda-daily-prices",
            source_record_id=_required_str(normalization, "source_record_id"),
            sha256=_required_str(price_item, "sha256"),
            body=bodies["nvda-daily-prices"],
            source_row=source_row,
            payload=payload,
            reconciliation=reconciliation,
        )
        return FrozenPriceCase(
            corpus_sha256=corpus_sha256,
            corpus_id=corpus["corpus_id"],
            d1_handoff_id=handoff["handoff_id"],
            d1_handoff_sha256=producer["sha256"],
            universe=universe,
            calendar=calendar,
            issuer_security=issuer_security,
            security_listing=security_listing,
            artifact=artifact,
            snapshot_as_of=snapshot_as_of,
            before_knowable_at=before,
            at_knowable_at=at,
            stale_as_of=stale,
            maximum_age=timedelta(days=maximum_age_days),
            confidence_policy=confidence_policy,
            price_policy=price_policy,
            registry_contract=registry_contract,
        )

    def capture(self, case: FrozenPriceCase, artifact: FrozenPriceArtifact) -> RawCapture:
        if _sha256(artifact.body) != artifact.sha256:
            raise ValueError("price bytes changed after fixture validation")
        return RawCapture(
            source=DataSource.YAHOO,
            source_record_id=artifact.source_record_id,
            body=artifact.body,
            content_type="text/csv",
            fetched_at=artifact.payload.knowable_at,
            source_published_at=None,
            metadata={
                "artifact_id": artifact.artifact_id,
                "corpus_id": case.corpus_id,
                "first_observed_fallback": True,
                "pre_capture_pit_claim_allowed": False,
            },
        )


def _module_sha256() -> str:
    return _sha256(Path(__file__).read_bytes())


def build_price_registry() -> RegistrySnapshot:
    parent = build_filing_registry()
    implementation_sha256 = _module_sha256()
    payload_schema_sha256 = canonical_sha256(MarketPricePayload.model_json_schema())
    semantic_type = SemanticTypeRegistryEntry(
        semantic_type_id=SEMANTIC_TYPE_ID,
        version=VERSION,
        domain=DataDomain.MARKET_PRICES,
        schema_version=VERSION,
        schema_fingerprint_sha256=payload_schema_sha256,
        normalized_model_key="data_engine:MarketPricePayload",
        input_model_key="factors:MarketPriceInput",
        repository_key="batch:PostgresMarketPriceRepository",
        projector_key="batch:D2E0SnapshotProjector",
        compatibility_sha256=canonical_sha256({"compatible_schema_versions": []}),
        model_implementation_sha256=implementation_sha256,
        repository_implementation_sha256=implementation_sha256,
        projector_implementation_sha256=implementation_sha256,
    )
    source = SourceRegistryEntry(
        source_id=SOURCE_ID,
        version=VERSION,
        adapter_id="batch:FrozenPriceAdapter",
        adapter_version=VERSION,
        normalizer_id="batch:PriceNormalizer",
        normalizer_version=VERSION,
        supported_domains=(DataDomain.MARKET_PRICES,),
        supported_type_ids=(semantic_type.semantic_type_id,),
        configuration_schema_sha256=canonical_sha256({"network": False, "credentials": False}),
        mapping_schema_sha256=payload_schema_sha256,
        adapter_implementation_sha256=implementation_sha256,
        normalizer_implementation_sha256=implementation_sha256,
    )
    return RegistrySnapshot(
        parent_snapshot_id=parent.registry_snapshot_id,
        sources=(*parent.sources, source),
        semantic_types=(*parent.semantic_types, semantic_type),
        identifier_types=parent.identifier_types,
        required_type_ids=(*parent.required_type_ids, semantic_type.semantic_type_id),
        required_identifier_type_ids=parent.required_identifier_type_ids,
    )


def _validate_registry_contract(registry: RegistrySnapshot, contract: dict[str, Any]) -> None:
    source = next(entry for entry in registry.sources if entry.key == (SOURCE_ID, VERSION))
    semantic_type = next(entry for entry in registry.semantic_types if entry.key == (SEMANTIC_TYPE_ID, VERSION))
    actual = {
        "parent_registry_snapshot_id": registry.parent_snapshot_id,
        "registry_snapshot_id": registry.registry_snapshot_id,
        "registry_snapshot_sha256": registry.content_sha256,
        "source_registry_id": registry.source_registry_snapshot_id,
        "source_registry_sha256": registry.source_registry_sha256,
        "semantic_type_registry_id": registry.semantic_type_registry_snapshot_id,
        "semantic_type_registry_sha256": registry.semantic_type_registry_sha256,
        "source_entry_id": source.source_registry_entry_id,
        "source_entry_sha256": source.content_sha256,
        "semantic_type_entry_id": semantic_type.semantic_type_registry_entry_id,
        "semantic_type_entry_sha256": semantic_type.content_sha256,
        "payload_schema_sha256": semantic_type.schema_fingerprint_sha256,
        "implementation_sha256": _module_sha256(),
    }
    if actual != contract:
        raise ValueError("D2 E0 registry or implementation contract drifted")


class PriceNormalizer:
    def normalize(
        self,
        case: FrozenPriceCase,
        artifact: FrozenPriceArtifact,
        raw_id: int,
        raw_sha256: str,
        source_entry: SourceRegistryEntry,
        type_entry: SemanticTypeRegistryEntry,
        supersedes: NormalizedRecordRef | None = None,
    ) -> tuple[NormalizedRecordRef, MarketPricePayload]:
        payload = artifact.payload
        if raw_id < 1 or raw_sha256 != artifact.sha256 or _sha256(artifact.body) != raw_sha256:
            raise ValueError("price normalization raw lineage is invalid")
        if type_entry.semantic_type_id != SEMANTIC_TYPE_ID or SEMANTIC_TYPE_ID not in source_entry.supported_type_ids:
            raise ValueError("registry does not bind the market-price route")
        if (
            payload.issuer_id != case.issuer_security.issuer_id
            or payload.security_id != case.issuer_security.security_id
            or payload.listing_id != case.security_listing.listing_id
            or payload.share_class != case.issuer_security.share_class
            or payload.exchange_mic != case.security_listing.exchange_mic
            or payload.ticker != case.security_listing.ticker
        ):
            raise ValueError("price payload identity does not match the frozen issuer/security/listing links")
        _validate_artifact_semantics(artifact)
        if payload.confidence != Decimal(str(case.confidence_policy["assigned_confidence"])):
            raise ValueError("price confidence does not match the frozen policy")
        if (
            payload.price_basis is not PriceBasis.UNADJUSTED
            or case.price_policy.get("adjusted_close_use") != "reconciliation-only"
        ):
            raise ValueError("adjusted price data cannot enter the executable price bar")
        case.calendar.require_session(payload.trading_date)
        if supersedes is not None:
            if (
                supersedes.draft.subject != SUBJECT
                or supersedes.draft.semantic_type_id != type_entry.semantic_type_id
                or supersedes.draft.semantic_type_version != type_entry.version
                or supersedes.source_registry_entry_id != source_entry.source_registry_entry_id
                or supersedes.draft.valid_from != payload.trading_date
                or supersedes.draft.valid_to != payload.trading_date
            ):
                raise ValueError("price correction predecessor belongs to another semantic coordinate")
            if payload.knowable_at <= supersedes.draft.knowable_at:
                raise ValueError("price correction must be knowable after its predecessor")
        draft = SemanticDraft(
            semantic_type_id=type_entry.semantic_type_id,
            semantic_type_version=type_entry.version,
            payload_model_key=type_entry.normalized_model_key,
            payload_schema_sha256=type_entry.schema_fingerprint_sha256,
            payload_sha256=canonical_sha256(payload.model_dump(mode="json")),
            subject=SUBJECT,
            valid_from=payload.trading_date,
            valid_to=payload.trading_date,
            knowable_at=payload.knowable_at,
            produced_at=payload.produced_at,
            producer_kind=SemanticProducerKind.DETERMINISTIC_NORMALIZER,
            producer_id=source_entry.normalizer_id,
            producer_version=source_entry.normalizer_version,
            producer_implementation_sha256=source_entry.normalizer_implementation_sha256,
        )
        record = NormalizedRecordRef(
            draft=draft,
            document_id="document:fixture:nvda-daily-prices",
            raw_object_id=f"raw-object:{artifact.sha256}",
            raw_object_sha256=artifact.sha256,
            source_registry_entry_id=source_entry.source_registry_entry_id,
            source_registry_entry_sha256=source_entry.content_sha256,
            mapping_version="fixture-yahoo-csv:1.0.0",
            mapping_implementation_sha256=source_entry.normalizer_implementation_sha256,
            recorded_at=payload.recorded_at,
            confidence=payload.confidence,
            is_restatement=supersedes is not None,
            supersedes_record_id=None if supersedes is None else supersedes.normalized_record_id,
        )
        return record, payload


@dataclass(frozen=True)
class PriceComponentCatalog:
    adapters: dict[str, FrozenPriceAdapter]
    normalizers: dict[str, PriceNormalizer]

    @classmethod
    def e0(cls) -> "PriceComponentCatalog":
        return cls(
            adapters={"batch:FrozenPriceAdapter": FrozenPriceAdapter()},
            normalizers={"batch:PriceNormalizer": PriceNormalizer()},
        )

    def resolve(
        self,
        registry: RegistrySnapshot,
    ) -> tuple[FrozenPriceAdapter, PriceNormalizer, SourceRegistryEntry, SemanticTypeRegistryEntry]:
        source = next((entry for entry in registry.sources if entry.key == (SOURCE_ID, VERSION)), None)
        semantic_type = next(
            (entry for entry in registry.semantic_types if entry.key == (SEMANTIC_TYPE_ID, VERSION)),
            None,
        )
        if source is None or semantic_type is None:
            raise ValueError("price registry route is missing its source or semantic type")
        if semantic_type.semantic_type_id not in source.supported_type_ids:
            raise ValueError("price registry source/type route is disconnected")
        try:
            return self.adapters[source.adapter_id], self.normalizers[source.normalizer_id], source, semantic_type
        except KeyError as error:
            raise ValueError(f"price registry component is not activated: {error.args[0]}") from error


class PostgresMarketPriceRepository:
    """Persist the batch-private typed payload in the existing normalized spine."""

    def __init__(self, connection: Connection[Any]) -> None:
        self.connection = connection

    def put(self, record: NormalizedRecordRef, payload: MarketPricePayload, *, raw_reference: str) -> bool:
        record_json = record.model_dump(mode="json")
        payload_json = payload.model_dump(mode="json")
        with self.connection.transaction():
            self._validate_supersedes(record)
            inserted = self.connection.execute(
                """
                insert into staging.normalized_records (
                    normalized_record_id, content_sha256, semantic_type_id,
                    semantic_type_version, subject_kind, subject_id, valid_time,
                    transaction_time, recorded_at, confidence, document_id,
                    raw_object_id, raw_object_sha256, raw_ref,
                    source_registry_entry_id, source_registry_entry_sha256,
                    mapping_version, mapping_implementation_sha256,
                    payload_model_key, payload_schema_sha256, payload_sha256,
                    payload, record_ref, is_restatement, supersedes_record_id
                ) values (
                    %s, %s, %s, %s, %s, %s, daterange(%s, %s, '[]'),
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s
                )
                on conflict (normalized_record_id) do nothing
                returning normalized_record_id
                """,
                (
                    record.normalized_record_id,
                    record.content_sha256,
                    record.draft.semantic_type_id,
                    record.draft.semantic_type_version,
                    record.draft.subject.kind.value,
                    record.draft.subject.id,
                    record.draft.valid_from,
                    record.draft.valid_to,
                    record.draft.knowable_at,
                    record.recorded_at,
                    record.confidence,
                    record.document_id,
                    record.raw_object_id,
                    record.raw_object_sha256,
                    raw_reference,
                    record.source_registry_entry_id,
                    record.source_registry_entry_sha256,
                    record.mapping_version,
                    record.mapping_implementation_sha256,
                    record.draft.payload_model_key,
                    record.draft.payload_schema_sha256,
                    record.draft.payload_sha256,
                    Jsonb(payload_json),
                    Jsonb(record_json),
                    record.is_restatement,
                    record.supersedes_record_id,
                ),
            ).fetchone()
            if inserted is None:
                self._validate_existing(record, payload, raw_reference=raw_reference)
                return False
        return True

    def select_pit(
        self,
        *,
        subject: SubjectRef,
        semantic_type_id: str,
        semantic_type_version: str,
        source_registry_entry_id: str,
        as_of: datetime,
        valid_on: date,
    ) -> tuple[NormalizedRecordRef, ...]:
        rows = self.connection.execute(
            """
            select candidate.record_ref
            from staging.normalized_records candidate
            where candidate.subject_kind = %s
              and candidate.subject_id = %s
              and candidate.semantic_type_id = %s
              and candidate.semantic_type_version = %s
              and candidate.source_registry_entry_id = %s
              and candidate.transaction_time <= %s
              and candidate.valid_time @> %s::date
              and not exists (
                  select 1
                  from staging.normalized_records replacement
                  where replacement.supersedes_record_id = candidate.normalized_record_id
                    and replacement.semantic_type_id = candidate.semantic_type_id
                    and replacement.semantic_type_version = candidate.semantic_type_version
                    and replacement.source_registry_entry_id = candidate.source_registry_entry_id
                    and replacement.transaction_time <= %s
              )
            order by candidate.transaction_time desc, candidate.normalized_record_id desc
            """,
            (
                subject.kind.value,
                subject.id,
                semantic_type_id,
                semantic_type_version,
                source_registry_entry_id,
                as_of,
                valid_on,
                as_of,
            ),
        ).fetchall()
        return tuple(NormalizedRecordRef.model_validate(row[0]) for row in rows)

    def payload_for(self, normalized_record_id: str) -> MarketPricePayload:
        row = self.connection.execute(
            "select payload from staging.normalized_records where normalized_record_id = %s",
            (normalized_record_id,),
        ).fetchone()
        if row is None:
            raise LookupError(normalized_record_id)
        return MarketPricePayload.model_validate(row[0])

    def _validate_supersedes(self, record: NormalizedRecordRef) -> None:
        if record.supersedes_record_id is None:
            return
        predecessor = self.connection.execute(
            """
            select semantic_type_id, semantic_type_version, subject_kind, subject_id,
                   source_registry_entry_id, source_registry_entry_sha256,
                   valid_time = daterange(%s, %s, '[]'), transaction_time
            from staging.normalized_records
            where normalized_record_id = %s
            """,
            (record.draft.valid_from, record.draft.valid_to, record.supersedes_record_id),
        ).fetchone()
        expected = (
            record.draft.semantic_type_id,
            record.draft.semantic_type_version,
            record.draft.subject.kind.value,
            record.draft.subject.id,
            record.source_registry_entry_id,
            record.source_registry_entry_sha256,
        )
        if predecessor is None or predecessor[:6] != expected:
            raise ValueError("superseded price must retain its registry-bound semantic coordinate")
        if predecessor[6] is not True or record.draft.knowable_at <= predecessor[7]:
            raise ValueError("superseding price must retain its period and use a later transaction time")
        competing = self.connection.execute(
            """
            select normalized_record_id
            from staging.normalized_records
            where supersedes_record_id = %s and normalized_record_id <> %s
            limit 1
            """,
            (record.supersedes_record_id, record.normalized_record_id),
        ).fetchone()
        if competing is not None:
            raise ValueError("a normalized price cannot have multiple successors")

    def _validate_existing(
        self,
        record: NormalizedRecordRef,
        payload: MarketPricePayload,
        *,
        raw_reference: str,
    ) -> None:
        row = self.connection.execute(
            """
            select record_ref, payload, raw_ref
            from staging.normalized_records
            where normalized_record_id = %s
            """,
            (record.normalized_record_id,),
        ).fetchone()
        expected = (record.model_dump(mode="json"), payload.model_dump(mode="json"), raw_reference)
        if row is None or tuple(row) != expected:
            raise ValueError("normalized price ID is already bound to different content")


@dataclass(frozen=True)
class PricePipelineRun:
    case: FrozenPriceCase
    registry: RegistrySnapshot
    artifacts: tuple[FrozenPriceArtifact, ...]
    raw_fetch_ids: tuple[int, ...]
    records: tuple[NormalizedRecordRef, ...]
    payloads: tuple[MarketPricePayload, ...]
    price_bars: tuple[ListingPriceBar, ...]
    inserted: tuple[bool, ...]


def _listing_price_bar(payload: MarketPricePayload, *, raw_reference: str) -> ListingPriceBar:
    return ListingPriceBar(
        input_id=payload.input_id,
        listing_id=payload.listing_id,
        calendar_id=payload.calendar_id,
        calendar_version=payload.calendar_version,
        trading_date=payload.trading_date,
        session_close_at=payload.session_close_at,
        open=payload.open,
        high=payload.high,
        low=payload.low,
        close=payload.close,
        volume=payload.volume,
        currency=payload.currency,
        price_basis=payload.price_basis,
        knowable_at=payload.knowable_at,
        recorded_at=payload.recorded_at,
        confidence=payload.confidence,
        raw_ref=raw_reference,
    )


def run_price_pipeline(
    repository_root: Path,
    connection: Connection[Any],
    raw_store: RawObjectStore,
    *,
    environment: str,
    artifacts: tuple[FrozenPriceArtifact, ...] | None = None,
) -> PricePipelineRun:
    registry = build_price_registry()
    catalog = PriceComponentCatalog.e0()
    adapter, normalizer, source_entry, type_entry = catalog.resolve(registry)
    case = adapter.load(repository_root, environment=environment)
    _validate_registry_contract(registry, case.registry_contract)
    selected_artifacts = artifacts or (case.artifact,)
    selected_artifacts = tuple(sorted(selected_artifacts, key=lambda item: item.payload.knowable_at))
    if len({item.artifact_id for item in selected_artifacts}) != len(selected_artifacts):
        raise ValueError("price artifacts must use unique artifact IDs")

    repository = PostgresMarketPriceRepository(connection)
    records_by_artifact: dict[str, NormalizedRecordRef] = {}
    raw_ids: list[int] = []
    records: list[NormalizedRecordRef] = []
    payloads: list[MarketPricePayload] = []
    price_bars: list[ListingPriceBar] = []
    inserted_rows: list[bool] = []
    for artifact in selected_artifacts:
        capture = adapter.capture(case, artifact)
        fetch_id = insert_fetch(
            connection,
            source=capture.source,
            source_record_id=capture.source_record_id,
            body=capture.body,
            content_type=capture.content_type,
            fetched_at=capture.fetched_at,
            source_published_at=capture.source_published_at,
            metadata=capture.metadata,
            store=raw_store,
            recorded_at=artifact.payload.knowable_at,
        )
        persisted_body = get_payload(connection, fetch_id, store=raw_store)
        if persisted_body != artifact.body:
            raise ValueError("raw price readback differs from captured bytes")
        predecessor = None
        if artifact.supersedes_artifact_id is not None:
            predecessor = records_by_artifact.get(artifact.supersedes_artifact_id)
            if predecessor is None:
                raise ValueError("price correction does not name an earlier artifact in this replay")
        record, payload = normalizer.normalize(
            case,
            artifact,
            fetch_id,
            artifact.sha256,
            source_entry,
            type_entry,
            predecessor,
        )
        reference = raw_ref(fetch_id)
        inserted = repository.put(record, payload, raw_reference=reference)
        stored_payload = repository.payload_for(record.normalized_record_id)
        if stored_payload != payload:
            raise ValueError("Postgres price payload differs from normalized content")
        records_by_artifact[artifact.artifact_id] = record
        raw_ids.append(fetch_id)
        records.append(record)
        payloads.append(payload)
        price_bars.append(_listing_price_bar(payload, raw_reference=reference))
        inserted_rows.append(inserted)

    return PricePipelineRun(
        case=case,
        registry=registry,
        artifacts=selected_artifacts,
        raw_fetch_ids=tuple(raw_ids),
        records=tuple(records),
        payloads=tuple(payloads),
        price_bars=tuple(price_bars),
        inserted=tuple(inserted_rows),
    )


@dataclass(frozen=True)
class PriceSnapshotBundle:
    scope: CaptureScope
    manifest: CaptureManifest
    evaluation: CaptureEvaluationReport
    snapshot: SnapshotManifest
    runner_selection: RunnerInputSelection


def _capture_environment(environment: str) -> CaptureEnvironment:
    try:
        return {"local": CaptureEnvironment.LOCAL_TEST, "ci": CaptureEnvironment.GITHUB_CI}[environment]
    except KeyError as error:
        raise ValueError("D2 E0 only permits local or ci execution") from error


def _price_requirement(case: FrozenPriceCase) -> CaptureRequirement:
    return CaptureRequirement(
        semantic_type_id=SEMANTIC_TYPE_ID,
        semantic_type_version=VERSION,
        domain=DataDomain.MARKET_PRICES,
        required_fields=(
            "calendar_id",
            "close",
            "confidence",
            "confidence_policy_id",
            "currency",
            "exchange_mic",
            "high",
            "issuer_id",
            "listing_id",
            "low",
            "open",
            "price_basis",
            "price_policy_id",
            "security_id",
            "session_close_at",
            "share_class",
            "ticker",
            "trading_date",
            "volume",
        ),
        subject_kinds=(SubjectKind.LISTING,),
        cadence=timedelta(days=1),
        partition_rule_id="partition.market-session:v1",
        freshness_policy_id="freshness.market-price:v1",
        maximum_age=case.maximum_age,
        quality_policy_ids=("quality.decimal-ohlcv:v1", "quality.raw-lineage:v1"),
    )


def _price_data_requirement(requirement: CaptureRequirement) -> DataRequirement:
    return DataRequirement(
        capture_requirement_id=requirement.capture_requirement_id,
        semantic_type_id=requirement.semantic_type_id,
        domain=requirement.domain,
        metric="unadjusted_ohlcv",
        subject_kinds=frozenset(requirement.subject_kinds),
        level=RequirementLevel.REQUIRED,
        lookback=timedelta(days=1),
        valid_period_rule_id=requirement.partition_rule_id,
        maximum_age=requirement.maximum_age,
        cadence=requirement.cadence,
    )


def _policy_bindings(case: FrozenPriceCase) -> tuple[PolicyBinding, ...]:
    bindings: list[PolicyBinding] = []
    for role in PolicyRole:
        if role is PolicyRole.MEMBERSHIP:
            continue
        if role is PolicyRole.PRICE:
            policy_id = str(case.price_policy["policy_id"])
            implementation_sha256 = str(case.price_policy["implementation_sha256"])
        elif role is PolicyRole.IDENTITY:
            policy_id = "policy.d2-e0-identity"
            implementation_sha256 = _hash("identity-policy")
        elif role is PolicyRole.MARKET_CALENDAR:
            policy_id = "policy.d2-e0-market-calendar"
            implementation_sha256 = case.calendar.content_sha256
        else:
            policy_id = f"policy.d2-e0-{role.value.replace('_', '-')}"
            implementation_sha256 = _hash(f"policy-{role.value}")
        bindings.append(
            PolicyBinding(
                role=role,
                policy_id=policy_id,
                policy_version=VERSION,
                implementation_sha256=implementation_sha256,
            )
        )
    return tuple(bindings)


def build_price_snapshot(
    *,
    case: FrozenPriceCase,
    records: tuple[NormalizedRecordRef, ...],
    selected_record: NormalizedRecordRef,
    registry: RegistrySnapshot,
    environment: str,
    as_of: datetime | None = None,
) -> PriceSnapshotBundle:
    cutoff = as_of or case.snapshot_as_of
    if not records or selected_record not in records:
        raise ValueError("price snapshot requires its selected record in capture evidence")
    if any(record.draft.subject != SUBJECT for record in records):
        raise ValueError("price snapshot records must belong to the frozen listing")
    requirement = _price_requirement(case)
    data_requirement = _price_data_requirement(requirement)
    capture_environment = _capture_environment(environment)
    key = (SUBJECT.kind, SUBJECT.id, requirement.domain, PARTITION, requirement.capture_requirement_id)
    applicability: ApplicabilityMapping = {key: ("required", case.at_knowable_at)}
    coverage_entry = "source-coverage-entry:" + _hash("fixture-yahoo-price-coverage")
    source_coverage: SourceCoverageMapping = {(capture_environment, *key): (coverage_entry,)}
    scope = CaptureScope(
        research_catalog_id="research-catalog:" + _hash("catalog"),
        research_catalog_sha256=_hash("catalog"),
        universe=case.universe,
        applicability_catalog_id="applicability:" + _hash("applicability"),
        applicability_catalog_sha256=_hash("applicability"),
        applicability_projection_sha256=canonical_applicability_projection_sha256(applicability),
        source_coverage_catalog_id="source-coverage:" + _hash("source-coverage"),
        source_coverage_catalog_sha256=_hash("source-coverage"),
        source_coverage_projection_sha256=canonical_source_coverage_projection_sha256(source_coverage),
        slo_catalog_id="module-slo:" + _hash("slo"),
        slo_catalog_sha256=_hash("slo"),
        source_registry_id=registry.source_registry_snapshot_id,
        source_registry_sha256=registry.source_registry_sha256,
        semantic_type_registry_id=registry.semantic_type_registry_snapshot_id,
        semantic_type_registry_sha256=registry.semantic_type_registry_sha256,
        requirements=(requirement,),
        effective_at=case.at_knowable_at,
        owner="batch-d2-mvp-medium-validation",
    )
    evidence = tuple(
        CaptureRecordEvidence(
            source_coverage_entry_id=coverage_entry,
            raw_id=f"raw.object:{record.raw_object_sha256}",
            raw_sha256=record.raw_object_sha256,
            normalized_id=record.normalized_record_id,
            semantic_type_id=record.draft.semantic_type_id,
            semantic_type_version=record.draft.semantic_type_version,
            populated_fields=requirement.required_fields,
            knowable_at=record.draft.knowable_at,
            recorded_at=record.recorded_at,
            valid_from=datetime.combine(record.draft.valid_from, time.min, UTC),
            valid_to=datetime.combine(record.draft.valid_to, time.max, UTC),
            confidence=record.confidence,
            mapping_version=record.mapping_version,
            policy_versions={
                requirement.freshness_policy_id: VERSION,
                requirement.partition_rule_id: VERSION,
            },
            quality_check_ids=requirement.quality_policy_ids,
            quality_status=QualityStatus.PASS,
            lineage_sha256=record.content_sha256,
        )
        for record in records
    )
    cell = CaptureCell(
        subject=SUBJECT,
        domain=requirement.domain,
        partition_key=PARTITION,
        capture_requirement_id=requirement.capture_requirement_id,
        applicability="required",
        status="complete",
        evidence=evidence,
    )
    manifest = CaptureManifest(
        capture_scope_id=scope.capture_scope_id,
        capture_scope_sha256=scope.content_sha256,
        environment=capture_environment,
        research_catalog_id=scope.research_catalog_id,
        research_catalog_sha256=scope.research_catalog_sha256,
        applicability_catalog_id=scope.applicability_catalog_id,
        applicability_catalog_sha256=scope.applicability_catalog_sha256,
        source_coverage_catalog_id=scope.source_coverage_catalog_id,
        source_coverage_catalog_sha256=scope.source_coverage_catalog_sha256,
        slo_catalog_id=scope.slo_catalog_id,
        slo_catalog_sha256=scope.slo_catalog_sha256,
        source_registry_id=scope.source_registry_id,
        source_registry_sha256=scope.source_registry_sha256,
        semantic_type_registry_id=scope.semantic_type_registry_id,
        semantic_type_registry_sha256=scope.semantic_type_registry_sha256,
        partition_key=PARTITION,
        as_of=cutoff,
        started_at=case.at_knowable_at,
        cells=(cell,),
        created_at=cutoff + timedelta(seconds=1),
    )
    evaluation = evaluate_capture_manifest(
        scope,
        manifest,
        applicability_catalog_id=scope.applicability_catalog_id,
        applicability_catalog_sha256=scope.applicability_catalog_sha256,
        applicability=applicability,
        source_coverage=source_coverage,
        evaluated_at=manifest.created_at + timedelta(seconds=1),
    )
    demand = SnapshotDemandCell(
        requirement_id=data_requirement.requirement_id,
        capture_requirement_id=requirement.capture_requirement_id,
        semantic_type_id=requirement.semantic_type_id,
        semantic_type_version=requirement.semantic_type_version,
        domain=requirement.domain,
        subject=SUBJECT,
        partition_key=PARTITION,
        level=data_requirement.level,
    )
    request = SnapshotRequest(
        subjects=(SUBJECT,),
        as_of=cutoff,
        valid_on=selected_record.draft.valid_to,
        registry_snapshot_id=registry.registry_snapshot_id,
        registry_snapshot_sha256=registry.content_sha256,
        source_registry_id=registry.source_registry_snapshot_id,
        source_registry_sha256=registry.source_registry_sha256,
        semantic_type_registry_id=registry.semantic_type_registry_snapshot_id,
        semantic_type_registry_sha256=registry.semantic_type_registry_sha256,
        policy_bindings=_policy_bindings(case),
        demand_cells=(demand,),
    )
    snapshot = SnapshotManifest(
        request=request,
        registry_snapshot=registry,
        resolved_subjects=(SUBJECT,),
        normalized_records=(selected_record,),
        selections=(
            SnapshotCellSelection(
                demand=demand,
                normalized_record_ids=(selected_record.normalized_record_id,),
            ),
        ),
        resolved_at=manifest.created_at + timedelta(seconds=1),
        resolver_id="batch:D2E0SnapshotProjector",
        resolver_version=VERSION,
        resolver_implementation_sha256=_module_sha256(),
    )
    template = FactorInvocationTemplate(
        factor_id="d2_mvp_medium_price_probe",
        factor_version=VERSION,
        factor_implementation_sha256=_hash("unregistered-price-probe"),
        factor_kind=FactorKind.BASE,
        parameter_model_key="batch:NoParameters",
        parameter_schema_sha256=_hash("no-parameters-schema"),
        canonical_parameters_sha256=_hash("no-parameters"),
        data_requirement_ids=(data_requirement.requirement_id,),
    )
    execution = FactorExecution(
        template=template,
        snapshot_id=snapshot.snapshot_id,
        snapshot_sha256=snapshot.content_sha256,
        ordered_subjects=(SUBJECT,),
        started_at=snapshot.resolved_at + timedelta(seconds=1),
    )
    selection = build_runner_input_selection(
        execution=execution,
        snapshot=snapshot,
        selected_at=execution.started_at + timedelta(seconds=1),
        runner_id="batch:D2E0Runner",
        runner_version=VERSION,
        runner_implementation_sha256=_module_sha256(),
    )
    return PriceSnapshotBundle(
        scope=scope,
        manifest=manifest,
        evaluation=evaluation,
        snapshot=snapshot,
        runner_selection=selection,
    )


class D2E0Evidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(default="", pattern=r"^(?:|d2-e0-evidence:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    batch_id: Literal["D2-mvp-medium-validation"] = "D2-mvp-medium-validation"
    accepted_rung: Literal["E0"] = "E0"
    stable_handoff: Literal[False] = False
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d1_handoff_id: str
    d1_handoff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    registry_snapshot_id: str
    registry_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalized_record_id: str
    raw_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    listing_price_input_id: str
    fixture_snapshot_id: str
    postgres_snapshot_id: str
    fixture_runner_selection_id: str
    postgres_runner_selection_id: str
    row_counts: dict[str, int]
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def identify(self) -> "D2E0Evidence":
        if self.fixture_snapshot_id != self.postgres_snapshot_id:
            raise ValueError("fixture and Postgres snapshot identities differ")
        if self.fixture_runner_selection_id != self.postgres_runner_selection_id:
            raise ValueError("fixture and Postgres runner selections differ")
        payload = self.model_dump(mode="json", exclude={"evidence_id", "content_sha256"})
        expected_hash = canonical_sha256(payload)
        expected_id = f"d2-e0-evidence:{expected_hash}"
        if self.content_sha256 and self.content_sha256 != expected_hash:
            raise ValueError("content_sha256 does not match D2 E0 evidence")
        if self.evidence_id and self.evidence_id != expected_id:
            raise ValueError("evidence_id does not match D2 E0 evidence")
        object.__setattr__(self, "content_sha256", expected_hash)
        object.__setattr__(self, "evidence_id", expected_id)
        return self


def run_d2_e0(
    repository_root: Path,
    connection: Connection[Any],
    raw_store: RawObjectStore,
    *,
    environment: str,
) -> D2E0Evidence:
    pipeline = run_price_pipeline(
        repository_root,
        connection,
        raw_store,
        environment=environment,
    )
    selected_record = pipeline.records[-1]
    fixture_bundle = build_price_snapshot(
        case=pipeline.case,
        records=pipeline.records,
        selected_record=selected_record,
        registry=pipeline.registry,
        environment=environment,
    )
    source_entry = next(entry for entry in pipeline.registry.sources if entry.key == (SOURCE_ID, VERSION))
    repository = PostgresMarketPriceRepository(connection)
    selected = repository.select_pit(
        subject=SUBJECT,
        semantic_type_id=SEMANTIC_TYPE_ID,
        semantic_type_version=VERSION,
        source_registry_entry_id=source_entry.source_registry_entry_id,
        as_of=pipeline.case.snapshot_as_of,
        valid_on=pipeline.payloads[-1].trading_date,
    )
    if selected != (selected_record,):
        raise ValueError("Postgres PIT selection differs from the fixture semantic record")
    postgres_bundle = build_price_snapshot(
        case=pipeline.case,
        records=pipeline.records,
        selected_record=selected[0],
        registry=pipeline.registry,
        environment=environment,
    )
    if not fixture_bundle.evaluation.ready or not postgres_bundle.evaluation.ready:
        blockers = sorted(
            set(fixture_bundle.evaluation.blocking_reason_codes) | set(postgres_bundle.evaluation.blocking_reason_codes)
        )
        raise ValueError(f"D2 E0 capture evidence is not ready: {blockers}")
    raw_count_row = connection.execute(
        """
        select count(*)
        from raw.fetches
        where source = %s and source_record_id = %s and payload_sha256 = %s
        """,
        (DataSource.YAHOO.value, pipeline.artifacts[-1].source_record_id, selected_record.raw_object_sha256),
    ).fetchone()
    normalized_count_row = connection.execute(
        "select count(*) from staging.normalized_records where normalized_record_id = %s",
        (selected_record.normalized_record_id,),
    ).fetchone()
    if raw_count_row is None or normalized_count_row is None:
        raise RuntimeError("Postgres did not return D2 E0 row counts")
    raw_count = raw_count_row[0]
    normalized_count = normalized_count_row[0]
    return D2E0Evidence(
        corpus_sha256=pipeline.case.corpus_sha256,
        d1_handoff_id=pipeline.case.d1_handoff_id,
        d1_handoff_sha256=pipeline.case.d1_handoff_sha256,
        registry_snapshot_id=pipeline.registry.registry_snapshot_id,
        registry_snapshot_sha256=pipeline.registry.content_sha256,
        normalized_record_id=selected_record.normalized_record_id,
        raw_object_sha256=selected_record.raw_object_sha256,
        listing_price_input_id=pipeline.price_bars[-1].input_id,
        fixture_snapshot_id=fixture_bundle.snapshot.snapshot_id,
        postgres_snapshot_id=postgres_bundle.snapshot.snapshot_id,
        fixture_runner_selection_id=fixture_bundle.runner_selection.selection_id,
        postgres_runner_selection_id=postgres_bundle.runner_selection.selection_id,
        row_counts={
            "normalized_records": int(normalized_count),
            "raw_fetches": int(raw_count),
            "typed_price_payloads": len(pipeline.price_bars),
        },
        created_at=pipeline.case.snapshot_as_of + timedelta(seconds=5),
    )


class D2E0Activation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: Literal["D2-mvp-medium-validation"] = "D2-mvp-medium-validation"
    environment: Literal["local", "ci"]
    release_allowed: Literal[False] = False


@dataclass(frozen=True)
class D2E0RunnerResource:
    repository_root: Path
    connection: Connection[Any]
    raw_store: RawObjectStore
    activation: D2E0Activation

    def run(self) -> D2E0Evidence:
        return run_d2_e0(
            self.repository_root,
            self.connection,
            self.raw_store,
            environment=self.activation.environment,
        )


@dg.asset(
    name=D2_E0_ASSET_NAME,
    group_name="mvp_medium_validation_e0",
    required_resource_keys={"mvp_medium_validation_e0_runner"},
    description="Execute the frozen D2 E0 price slice without release registration.",
)
def materialize_mvp_medium_validation_e0(context: AssetExecutionContext) -> dg.Output[D2E0Evidence]:
    runner = cast(D2E0RunnerResource, context.resources.mvp_medium_validation_e0_runner)
    evidence = runner.run()
    return dg.Output(
        evidence,
        metadata={
            "evidence_id": evidence.evidence_id,
            "stable_handoff": evidence.stable_handoff,
            "normalized_record_id": evidence.normalized_record_id,
        },
        data_version=dg.DataVersion(evidence.content_sha256),
    )


def build_d2_e0_definitions(
    *,
    repository_root: Path,
    connection: Connection[Any],
    raw_store: RawObjectStore,
    activation: D2E0Activation | ReleaseManifest,
) -> dg.Definitions:
    if not isinstance(activation, D2E0Activation):
        raise ValueError("D2 E0 is batch-private and cannot consume a ReleaseManifest")
    return dg.Definitions(
        assets=[materialize_mvp_medium_validation_e0],
        resources={
            "mvp_medium_validation_e0_runner": cast(
                Any,
                D2E0RunnerResource(
                    repository_root=repository_root,
                    connection=connection,
                    raw_store=raw_store,
                    activation=activation,
                ),
            )
        },
    )


__all__ = [
    "CORPUS_PATH",
    "D2_E0_ASSET_NAME",
    "D2E0Activation",
    "D2E0Evidence",
    "D2E0RunnerResource",
    "FrozenPriceAdapter",
    "FrozenPriceArtifact",
    "FrozenPriceCase",
    "MarketPricePayload",
    "PostgresMarketPriceRepository",
    "PriceComponentCatalog",
    "PriceNormalizer",
    "PricePipelineRun",
    "PriceReconciliationEvidence",
    "PriceSnapshotBundle",
    "build_d2_e0_definitions",
    "build_price_registry",
    "build_price_snapshot",
    "materialize_mvp_medium_validation_e0",
    "run_d2_e0",
    "run_price_pipeline",
]
