"""Unregistered D3 E0 Yahoo chart configuration and parser slice."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from truealpha_contracts.common import canonical_sha256

BATCH_MANIFEST_PATH = Path("governance/batches/D3-staging-topt-capture.v1.json")
CORPUS_PATH = Path("apps/data-engine/tests/fixtures/staging_topt_capture/corpus.v1.json")

SOURCE_ID = "source.yahoo-chart-public"
SOURCE_VERSION = "1.0.0"
ADAPTER_ID = "data_engine.d3.yahoo_chart_adapter"
ADAPTER_VERSION = "1.0.0"
SEMANTIC_TYPE_ID = "semantic.market-price"
SEMANTIC_TYPE_VERSION = "1.0.0"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
USER_AGENT = "TrueAlpha D3 bounded research capture"
MAXIMUM_ATTEMPTS = 3
ASSIGNED_CONFIDENCE = Decimal("0.80")

EXPECTED_PARENT = {
    "issue": 23,
    "evidence_path": "governance/evidence/issue-23.v1.json",
    "evidence_sha256": "9066cc06367a42ae92f4d69e008cc411ec008705edd52068861763056af98547",
    "d2_e3_evidence_id": "d2-e3-evidence:d812369f2808942c8040a3d5f15e71ec7c147d7d547f988e312f428f15bf6139",
    "d2_e3_evidence_sha256": "d812369f2808942c8040a3d5f15e71ec7c147d7d547f988e312f428f15bf6139",
}
EXPECTED_DENOMINATOR = {
    "universe_id": "universe:topt-us-2026-03-31",
    "accession": "000207169126012475",
    "issuer_count": 20,
    "instrument_count": 21,
    "required_cell_count": 84,
}


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _repository_path(root: Path, relative_path: str) -> Path:
    pure = PurePosixPath(relative_path)
    if (
        not relative_path
        or pure.is_absolute()
        or "\\" in relative_path
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ValueError(f"repository path escapes root: {relative_path}")
    candidate = root.joinpath(*pure.parts)
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError as error:
        raise ValueError(f"repository path escapes root: {relative_path}") from error
    return candidate


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _load_json_object(body: bytes, *, decimal_numbers: bool) -> dict[str, Any]:
    try:
        decoded = body.decode("utf-8")
        options: dict[str, Any] = {
            "object_pairs_hook": _unique_object,
            "parse_constant": _reject_json_constant,
        }
        if decimal_numbers:
            options["parse_float"] = Decimal
        value = json.loads(decoded, **options)
    except (UnicodeDecodeError, json.JSONDecodeError, InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("response is not strict UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def _epoch(day: date) -> str:
    return str(int(datetime.combine(day, time.min, UTC).timestamp()))


class YahooChartSourceConfig(BaseModel):
    """Pinned, batch-private HTTP configuration with no default activation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: Literal["source.yahoo-chart-public"] = "source.yahoo-chart-public"
    source_version: Literal["1.0.0"] = "1.0.0"
    adapter_id: Literal["data_engine.d3.yahoo_chart_adapter"] = "data_engine.d3.yahoo_chart_adapter"
    adapter_version: Literal["1.0.0"] = "1.0.0"
    chart_url: Literal["https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"] = (
        "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    )
    user_agent: Literal["TrueAlpha D3 bounded research capture"] = "TrueAlpha D3 bounded research capture"
    timeout_seconds: int = Field(default=15, ge=1, le=30, strict=True)
    maximum_attempts: int = Field(default=MAXIMUM_ATTEMPTS, ge=1, le=MAXIMUM_ATTEMPTS, strict=True)


class YahooChartRequest(BaseModel):
    """One exact, single-session Yahoo chart request bound to TrueAlpha identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: Literal["source.yahoo-chart-public"]
    source_version: Literal["1.0.0"]
    adapter_id: Literal["data_engine.d3.yahoo_chart_adapter"]
    adapter_version: Literal["1.0.0"]
    semantic_type_id: Literal["semantic.market-price"]
    semantic_type_version: Literal["1.0.0"]
    issuer_id: str = Field(pattern=r"^issuer:[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$")
    security_id: str = Field(pattern=r"^security:[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$")
    listing_id: str = Field(pattern=r"^listing:[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$")
    symbol: str = Field(pattern=r"^[A-Z][A-Z0-9.\-]{0,15}$")
    exchange_mic: Literal["XNAS"]
    currency: Literal["USD"]
    query_start: date
    query_end_exclusive: date
    expected_trading_date: date
    maximum_attempts: int = Field(ge=1, le=MAXIMUM_ATTEMPTS, strict=True)

    @model_validator(mode="after")
    def validate_window(self) -> YahooChartRequest:
        if self.query_end_exclusive - self.query_start != timedelta(days=1):
            raise ValueError("Yahoo E0 query window must be exactly one day")
        if self.expected_trading_date != self.query_start:
            raise ValueError("expected trading date must equal the query start")
        return self


class ExpectedYahooBar(BaseModel):
    """Frozen expected projection used to bind parser behavior to the corpus."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    security_id: str
    listing_id: str
    trading_date: date
    session_close_at: datetime
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    adjusted_close: Decimal = Field(gt=0)
    volume: int = Field(ge=0, strict=True)
    currency: str
    confidence: Decimal = Field(ge=0, le=1)

    @field_validator("open", "high", "low", "close", "adjusted_close", "confidence", mode="before")
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        if isinstance(value, (float, bool)):
            raise ValueError("price and confidence inputs must not use binary floats")
        return value

    @field_validator("session_close_at")
    @classmethod
    def require_aware_close(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("session_close_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_ohlc(self) -> ExpectedYahooBar:
        decimals = (self.open, self.high, self.low, self.close, self.adjusted_close, self.confidence)
        if any(not value.is_finite() for value in decimals):
            raise ValueError("price and confidence values must be finite Decimals")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high is below another OHLC value")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low is above another OHLC value")
        return self


class YahooDailyBar(ExpectedYahooBar):
    """Parsed source bar; adjusted close is retained only for reconciliation."""

    issuer_id: str
    exchange_mic: Literal["XNAS"]
    raw_response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    price_basis: Literal["unadjusted"] = "unadjusted"
    adjusted_close_use: Literal["reconciliation-only"] = "reconciliation-only"
    adjusted_close_factor_visible: Literal[False] = False
    confidence_policy_id: Literal["confidence.yahoo-public:1.0.0"] = "confidence.yahoo-public:1.0.0"

    def expected_projection(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in ExpectedYahooBar.model_fields}


class FrozenYahooRequestPlan(BaseModel):
    """Content-addressed call plan that must exist before an HTTP interaction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capture_scope_id: str = Field(pattern=r"^capture-scope:[0-9a-f]{64}$")
    capture_scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    call_plan_id: str = Field(pattern=r"^d3-yahoo-call-plan:[0-9a-f]{64}$")
    configuration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    url: str
    params: tuple[tuple[str, str], ...]
    request: YahooChartRequest
    source: YahooChartSourceConfig

    def verify(self) -> None:
        expected = _plan_payload(
            request=self.request,
            source=self.source,
            capture_scope_id=self.capture_scope_id,
            capture_scope_sha256=self.capture_scope_sha256,
        )
        expected_sha256 = canonical_sha256(expected)
        if self.configuration_sha256 != expected_sha256:
            raise ValueError("Yahoo call-plan configuration hash drifted")
        if self.call_plan_id != f"d3-yahoo-call-plan:{expected_sha256}":
            raise ValueError("Yahoo call-plan identity drifted")
        if self.url != expected["url"] or self.params != tuple(expected["params"]):
            raise ValueError("Yahoo call-plan request drifted")


@dataclass(frozen=True)
class FrozenYahooCorpus:
    corpus_sha256: str
    accepted_parent: dict[str, Any]
    denominator: dict[str, Any]
    request: YahooChartRequest
    raw_body: bytes
    raw_sha256: str
    expected_bar: ExpectedYahooBar


def _plan_payload(
    *,
    request: YahooChartRequest,
    source: YahooChartSourceConfig,
    capture_scope_id: str,
    capture_scope_sha256: str,
) -> dict[str, Any]:
    url = source.chart_url.format(symbol=request.symbol)
    params = (
        ("events", "history"),
        ("interval", "1d"),
        ("period1", _epoch(request.query_start)),
        ("period2", _epoch(request.query_end_exclusive)),
    )
    return {
        "capture_scope_id": capture_scope_id,
        "capture_scope_sha256": capture_scope_sha256,
        "source": source.model_dump(mode="json"),
        "request": request.model_dump(mode="json"),
        "url": url,
        "params": params,
    }


def freeze_yahoo_request_plan(
    request: YahooChartRequest,
    *,
    capture_scope_id: str,
    capture_scope_sha256: str,
    source: YahooChartSourceConfig | None = None,
) -> FrozenYahooRequestPlan:
    source = source or YahooChartSourceConfig(maximum_attempts=request.maximum_attempts)
    if source.maximum_attempts != request.maximum_attempts:
        raise ValueError("source and request attempt ceilings differ")
    payload = _plan_payload(
        request=request,
        source=source,
        capture_scope_id=capture_scope_id,
        capture_scope_sha256=capture_scope_sha256,
    )
    configuration_sha256 = canonical_sha256(payload)
    plan = FrozenYahooRequestPlan(
        capture_scope_id=capture_scope_id,
        capture_scope_sha256=capture_scope_sha256,
        call_plan_id=f"d3-yahoo-call-plan:{configuration_sha256}",
        configuration_sha256=configuration_sha256,
        url=payload["url"],
        params=payload["params"],
        request=request,
        source=source,
    )
    plan.verify()
    return plan


def _strict_decimal(value: Any, *, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise ValueError(f"{label} must be a JSON number parsed without binary float")
    parsed = value if isinstance(value, Decimal) else Decimal(value)
    if not parsed.is_finite():
        raise ValueError(f"{label} must be finite")
    return parsed


def _strict_volume(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("volume must be an integral JSON number")
    if value < 0:
        raise ValueError("volume must be nonnegative")
    return value


def _one_series(indicators: dict[str, Any], name: str, field: str, size: int) -> list[Any]:
    groups = indicators.get(name)
    if not isinstance(groups, list) or len(groups) != 1 or not isinstance(groups[0], dict):
        raise ValueError(f"Yahoo {name} series must contain one object")
    values = groups[0].get(field)
    if not isinstance(values, list) or len(values) != size:
        raise ValueError("Yahoo timestamp and value series lengths differ")
    return values


def parse_yahoo_chart_response(body: bytes, request: YahooChartRequest) -> YahooDailyBar:
    """Parse one strict Yahoo response without ever constructing binary floats."""

    payload = _load_json_object(body, decimal_numbers=True)
    chart = payload.get("chart")
    if not isinstance(chart, dict):
        raise ValueError("Yahoo chart envelope is missing")
    if chart.get("error") is not None:
        raise ValueError("Yahoo chart response contains an error")
    results = chart.get("result")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        raise ValueError("Yahoo chart response must contain exactly one result")

    result = results[0]
    meta = result.get("meta")
    if not isinstance(meta, dict):
        raise ValueError("Yahoo chart metadata is missing")
    expected_meta = {
        "symbol": request.symbol,
        "currency": request.currency,
        "instrumentType": "EQUITY",
        "exchangeName": "NMS",
        "fullExchangeName": "NasdaqGS",
        "exchangeTimezoneName": "America/New_York",
    }
    for key, expected in expected_meta.items():
        if meta.get(key) != expected:
            raise ValueError(f"Yahoo metadata mismatch: {key}")

    timestamps = result.get("timestamp")
    if not isinstance(timestamps, list) or len(timestamps) != 1:
        raise ValueError("Yahoo one-day response must contain exactly one timestamp")
    timestamp = timestamps[0]
    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise ValueError("Yahoo timestamp must be an integral JSON number")
    session_close_at = datetime.fromtimestamp(timestamp, tz=UTC)
    exchange_zone = ZoneInfo("America/New_York")
    local_close = session_close_at.astimezone(exchange_zone)
    if local_close.date() != request.expected_trading_date or local_close.time() != time(16, 0):
        raise ValueError("Yahoo timestamp does not match the expected trading session")
    utc_offset = local_close.utcoffset()
    if utc_offset is None:
        raise ValueError("Yahoo exchange timezone has no UTC offset")
    expected_offset = int(utc_offset.total_seconds())
    gmtoffset = meta.get("gmtoffset")
    if isinstance(gmtoffset, bool) or not isinstance(gmtoffset, int) or gmtoffset != expected_offset:
        raise ValueError("Yahoo metadata mismatch: gmtoffset")

    indicators = result.get("indicators")
    if not isinstance(indicators, dict):
        raise ValueError("Yahoo indicator envelope is missing")
    quote = _one_series(indicators, "quote", "open", len(timestamps))
    high = _one_series(indicators, "quote", "high", len(timestamps))
    low = _one_series(indicators, "quote", "low", len(timestamps))
    close = _one_series(indicators, "quote", "close", len(timestamps))
    volume = _one_series(indicators, "quote", "volume", len(timestamps))
    adjusted_close = _one_series(indicators, "adjclose", "adjclose", len(timestamps))

    return YahooDailyBar(
        issuer_id=request.issuer_id,
        symbol=request.symbol,
        security_id=request.security_id,
        listing_id=request.listing_id,
        exchange_mic=request.exchange_mic,
        trading_date=local_close.date(),
        session_close_at=session_close_at,
        open=_strict_decimal(quote[0], label="open"),
        high=_strict_decimal(high[0], label="high"),
        low=_strict_decimal(low[0], label="low"),
        close=_strict_decimal(close[0], label="close"),
        adjusted_close=_strict_decimal(adjusted_close[0], label="adjusted_close"),
        volume=_strict_volume(volume[0]),
        currency=request.currency,
        confidence=ASSIGNED_CONFIDENCE,
        raw_response_sha256=_sha256(body),
    )


class YahooChartAdapter:
    """Explicit-client E0 adapter; no client, schedule, retry loop, or credentials are global."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def fetch_once(self, plan: FrozenYahooRequestPlan) -> YahooDailyBar:
        if not isinstance(plan, FrozenYahooRequestPlan):
            raise TypeError("a frozen Yahoo request plan is required before HTTP")
        plan.verify()
        response = self._client.get(
            plan.url,
            params=plan.params,
            headers={"User-Agent": plan.source.user_agent, "Accept": "application/json"},
            timeout=plan.source.timeout_seconds,
        )
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("Yahoo response content type is not application/json")
        return parse_yahoo_chart_response(response.content, plan.request)


def load_frozen_e0_corpus(repository_root: Path) -> FrozenYahooCorpus:
    manifest_path = _repository_path(repository_root, BATCH_MANIFEST_PATH.as_posix())
    manifest = _load_json_object(manifest_path.read_bytes(), decimal_numbers=False)
    corpus_binding = manifest.get("corpus")
    if manifest.get("batch_id") != "D3-staging-topt-capture" or not isinstance(corpus_binding, dict):
        raise ValueError("D3 batch manifest is invalid")
    if corpus_binding.get("manifest_path") != CORPUS_PATH.as_posix():
        raise ValueError("D3 batch does not bind the E0 corpus path")

    corpus_path = _repository_path(repository_root, CORPUS_PATH.as_posix())
    corpus_bytes = corpus_path.read_bytes()
    corpus_sha256 = _sha256(corpus_bytes)
    if corpus_binding.get("sha256") != corpus_sha256:
        raise ValueError("D3 E0 corpus checksum drifted")
    corpus = _load_json_object(corpus_bytes, decimal_numbers=False)
    if (
        corpus.get("schema_version") != 1
        or corpus.get("batch_id") != "D3-staging-topt-capture"
        or corpus.get("target_rung") != "E0"
        or corpus.get("fixture_kind") != "synthetic-contract-probe"
    ):
        raise ValueError("unsupported D3 E0 corpus")
    if corpus.get("accepted_parent") != EXPECTED_PARENT:
        raise ValueError("D3 E0 accepted parent drifted")
    if corpus.get("denominator") != EXPECTED_DENOMINATOR:
        raise ValueError("D3 E0 TOPT denominator drifted")

    request = YahooChartRequest.model_validate(corpus.get("request"))
    raw_response = corpus.get("raw_response")
    if not isinstance(raw_response, dict) or raw_response.get("content_type") != "application/json":
        raise ValueError("D3 E0 raw response declaration is invalid")
    raw_text = raw_response.get("body")
    if not isinstance(raw_text, str):
        raise ValueError("D3 E0 raw response body is missing")
    raw_body = raw_text.encode("utf-8")
    raw_sha256 = _sha256(raw_body)
    if raw_response.get("sha256") != raw_sha256:
        raise ValueError("D3 E0 raw response checksum drifted")

    expected_bar = ExpectedYahooBar.model_validate(corpus.get("expected_bar"))
    parsed = parse_yahoo_chart_response(raw_body, request)
    if parsed.expected_projection() != expected_bar.model_dump():
        raise ValueError("D3 E0 expected bar does not match the raw response")
    if request.security_id != expected_bar.security_id or request.listing_id != expected_bar.listing_id:
        raise ValueError("D3 E0 request and expected identity differ")

    return FrozenYahooCorpus(
        corpus_sha256=corpus_sha256,
        accepted_parent=dict(EXPECTED_PARENT),
        denominator=dict(EXPECTED_DENOMINATOR),
        request=request,
        raw_body=raw_body,
        raw_sha256=raw_sha256,
        expected_bar=expected_bar,
    )
