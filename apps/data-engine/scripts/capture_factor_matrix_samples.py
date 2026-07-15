#!/usr/bin/env python3
"""Capture immutable provider-specific daily inputs for factor-matrix research."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import StringIO
from pathlib import Path, PurePosixPath
from time import sleep
from typing import Any

import httpx

DATA_ENGINE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLAN = DATA_ENGINE_ROOT / "samples/factor_matrix/capture_plan.v2.json"
DEFAULT_OUTPUT_ROOT = DATA_ENGINE_ROOT / "samples/factor_matrix/captures"
EXPECTED_SYMBOLS = ("ADM", "DDOG", "DUOL", "JPM", "META", "NICE", "NVDA", "PLUG", "SHOP")
EXPECTED_PROVIDERS = ("twelve_data", "yahoo")
EXPECTED_YAHOO_REQUEST_CONTRACT = {
    "symbol": {"location": "endpoint_path", "name": "symbol"},
    "window_start": {
        "name": "period1",
        "encoding": "inclusive start date at 00:00:00 UTC as Unix seconds",
    },
    "window_end": {
        "name": "period2",
        "encoding": "exclusive day after the inclusive end date at 00:00:00 UTC as Unix seconds",
    },
}
EXPECTED_TWELVE_REQUEST_CONTRACTS = {
    "truealpha.factor-matrix-sample-plan@v1": {
        "symbol": {"location": "query", "name": "symbol"},
        "window_start": {"name": "start_date", "encoding": "inclusive ISO 8601 calendar date"},
        "window_end": {"name": "end_date", "encoding": "inclusive ISO 8601 calendar date"},
    },
    "truealpha.factor-matrix-sample-plan@v2": {
        "symbol": {"location": "query", "name": "symbol"},
        "window_start": {"name": "start_date", "encoding": "inclusive ISO 8601 calendar date"},
        "window_end": {
            "name": "end_date",
            "encoding": "exclusive ISO 8601 calendar date set to the day after the inclusive sample end",
        },
    },
}
NORMALIZED_COLUMNS = ("date", "open", "high", "low", "close", "adjusted_close", "volume")
ARTIFACT_LAYOUT = {
    "normalized_csv": ("normalized", "csv"),
    "raw_response": ("raw", "json"),
    "request_metadata": ("requests", "json"),
}
CAPTURE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
USER_AGENT = "TrueAlpha research wangzitian.ai@icloud.com"


class CaptureError(RuntimeError):
    """The frozen capture contract was not satisfied."""


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode()


def _sha256(body: bytes) -> str:
    return sha256(body).hexdigest()


def _file_sha256(path: Path) -> str:
    return _sha256(path.read_bytes())


def _require_mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CaptureError(f"{context} must be an object")
    return value


def _require_sequence(value: Any, context: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise CaptureError(f"{context} must be an array")
    return value


def _parse_day(value: Any, context: str) -> date:
    if not isinstance(value, str):
        raise CaptureError(f"{context} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CaptureError(f"{context} must be an ISO date") from exc


def _decimal(value: Any, context: str, *, positive: bool = True) -> Decimal:
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise CaptureError(f"{context} must be a finite decimal")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise CaptureError(f"{context} must be a finite decimal") from exc
    if not parsed.is_finite() or (positive and parsed <= 0) or (not positive and parsed < 0):
        qualifier = "positive" if positive else "non-negative"
        raise CaptureError(f"{context} must be a finite {qualifier} decimal")
    return parsed


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _volume_text(value: Any, context: str) -> str:
    parsed = _decimal(value, context, positive=False)
    if parsed != parsed.to_integral_value():
        raise CaptureError(f"{context} must be an integer")
    return str(int(parsed))


def load_plan(path: Path = DEFAULT_PLAN) -> dict[str, Any]:
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaptureError(f"cannot load capture plan: {path}") from exc
    if not isinstance(plan, dict) or plan.get("schema") not in EXPECTED_TWELVE_REQUEST_CONTRACTS:
        raise CaptureError("unsupported capture plan schema")

    window = _require_mapping(plan.get("window"), "window")
    start = _parse_day(window.get("start"), "window.start")
    end = _parse_day(window.get("end"), "window.end")
    if start > end or window.get("end_inclusive") is not True:
        raise CaptureError("capture window must be a non-empty inclusive range")

    columns = plan.get("normalized_columns")
    if (tuple(columns) if isinstance(columns, list) else ()) != NORMALIZED_COLUMNS:
        raise CaptureError("normalized columns differ from the v1 contract")

    providers = _require_mapping(plan.get("providers"), "providers")
    if tuple(sorted(providers)) != EXPECTED_PROVIDERS:
        raise CaptureError(f"providers must be exactly {list(EXPECTED_PROVIDERS)}")
    for provider, raw_config in providers.items():
        config = _require_mapping(raw_config, f"providers.{provider}")
        if not isinstance(config.get("endpoint"), str) or not config["endpoint"].startswith("https://"):
            raise CaptureError(f"providers.{provider}.endpoint must use HTTPS")
        request = _require_mapping(config.get("request"), f"providers.{provider}.request")
        if any(key.lower() in {"apikey", "api_key", "authorization"} for key in request):
            raise CaptureError(f"providers.{provider}.request contains a credential field")
        expected_contract = (
            EXPECTED_YAHOO_REQUEST_CONTRACT
            if provider == "yahoo"
            else EXPECTED_TWELVE_REQUEST_CONTRACTS[str(plan["schema"])]
        )
        if config.get("request_contract") != expected_contract:
            raise CaptureError(f"providers.{provider}.request_contract differs from the v1 contract")
        fixture = _require_mapping(config.get("fixture"), f"providers.{provider}.fixture")
        fixture_payload = fixture.get("payload")
        fixture_hash = fixture.get("sha256")
        if (
            fixture.get("encoding") != "utf-8"
            or not isinstance(fixture_payload, str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(fixture_hash))
        ):
            raise CaptureError(f"providers.{provider}.fixture is invalid")

    symbols = _require_sequence(plan.get("symbols"), "symbols")
    symbol_names: list[str] = []
    for index, raw_symbol in enumerate(symbols):
        symbol = _require_mapping(raw_symbol, f"symbols[{index}]")
        name = symbol.get("symbol")
        provider_symbols = _require_mapping(symbol.get("provider_symbols"), f"symbols[{index}].provider_symbols")
        if not isinstance(name, str) or tuple(sorted(provider_symbols)) != EXPECTED_PROVIDERS:
            raise CaptureError(f"symbols[{index}] has invalid provider bindings")
        if any(not isinstance(value, str) or not value for value in provider_symbols.values()):
            raise CaptureError(f"symbols[{index}] has an empty provider symbol")
        symbol_names.append(name)
    if tuple(symbol_names) != EXPECTED_SYMBOLS or len(set(symbol_names)) != len(symbol_names):
        raise CaptureError(f"symbols must be exactly {list(EXPECTED_SYMBOLS)} in canonical order")
    return plan


def _validate_fixture_hashes(plan: Mapping[str, Any]) -> None:
    providers = _require_mapping(plan["providers"], "providers")
    for provider, raw_config in providers.items():
        config = _require_mapping(raw_config, f"providers.{provider}")
        fixture = _require_mapping(config["fixture"], f"providers.{provider}.fixture")
        if _sha256(str(fixture["payload"]).encode()) != fixture["sha256"]:
            raise CaptureError(f"{provider} parser fixture hash mismatch")


def _validate_bar(row: dict[str, str], context: str) -> None:
    open_ = Decimal(row["open"])
    high = Decimal(row["high"])
    low = Decimal(row["low"])
    close = Decimal(row["close"])
    if high < max(open_, low, close) or low > min(open_, high, close):
        raise CaptureError(f"{context} has inconsistent OHLC bounds")


def _normalized_csv(rows: list[dict[str, str]]) -> bytes:
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=NORMALIZED_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode()


def _finalize_rows(rows: list[dict[str, str]], start: date, end: date, context: str) -> bytes:
    if not rows:
        raise CaptureError(f"{context} returned no daily rows")
    rows.sort(key=lambda row: row["date"])
    dates = [row["date"] for row in rows]
    if len(set(dates)) != len(dates):
        raise CaptureError(f"{context} contains duplicate dates")
    if _parse_day(dates[0], f"{context}.date") < start or _parse_day(dates[-1], f"{context}.date") > end:
        raise CaptureError(f"{context} contains a date outside the frozen window")
    for row in rows:
        _validate_bar(row, f"{context}[{row['date']}]")
    return _normalized_csv(rows)


def normalize_yahoo(raw: bytes, provider_symbol: str, start: date, end: date) -> bytes:
    try:
        payload = json.loads(raw, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise CaptureError(f"yahoo {provider_symbol}: response is not JSON") from exc
    chart = _require_mapping(_require_mapping(payload, "yahoo response").get("chart"), "yahoo chart")
    if chart.get("error") is not None:
        raise CaptureError(f"yahoo {provider_symbol}: provider returned an error payload")
    results = _require_sequence(chart.get("result"), "yahoo chart.result")
    if len(results) != 1:
        raise CaptureError(f"yahoo {provider_symbol}: expected one chart result")
    result = _require_mapping(results[0], "yahoo chart.result[0]")
    meta = _require_mapping(result.get("meta"), "yahoo meta")
    if meta.get("symbol") != provider_symbol:
        raise CaptureError(f"yahoo {provider_symbol}: response symbol mismatch")
    timestamps = _require_sequence(result.get("timestamp"), "yahoo timestamp")
    indicators = _require_mapping(result.get("indicators"), "yahoo indicators")
    quotes = _require_sequence(indicators.get("quote"), "yahoo indicators.quote")
    adjusted = _require_sequence(indicators.get("adjclose"), "yahoo indicators.adjclose")
    if len(quotes) != 1 or len(adjusted) != 1:
        raise CaptureError(f"yahoo {provider_symbol}: expected one quote and adjusted-close series")
    quote = _require_mapping(quotes[0], "yahoo quote series")
    adjusted_values = _require_sequence(
        _require_mapping(adjusted[0], "yahoo adjusted-close series").get("adjclose"), "yahoo adjusted-close values"
    )
    fields = {
        name: _require_sequence(quote.get(name), f"yahoo quote.{name}")
        for name in ("open", "high", "low", "close", "volume")
    }
    lengths = {len(timestamps), len(adjusted_values), *(len(values) for values in fields.values())}
    if len(lengths) != 1:
        raise CaptureError(f"yahoo {provider_symbol}: series lengths differ")

    rows: list[dict[str, str]] = []
    for index, timestamp in enumerate(timestamps):
        if isinstance(timestamp, bool) or not isinstance(timestamp, int):
            raise CaptureError(f"yahoo {provider_symbol}: timestamp[{index}] must be an integer")
        day = datetime.fromtimestamp(timestamp, tz=UTC).date()
        row = {
            "date": day.isoformat(),
            "open": _decimal_text(_decimal(fields["open"][index], f"yahoo {provider_symbol}.open[{index}]")),
            "high": _decimal_text(_decimal(fields["high"][index], f"yahoo {provider_symbol}.high[{index}]")),
            "low": _decimal_text(_decimal(fields["low"][index], f"yahoo {provider_symbol}.low[{index}]")),
            "close": _decimal_text(_decimal(fields["close"][index], f"yahoo {provider_symbol}.close[{index}]")),
            "adjusted_close": _decimal_text(
                _decimal(adjusted_values[index], f"yahoo {provider_symbol}.adjusted_close[{index}]")
            ),
            "volume": _volume_text(fields["volume"][index], f"yahoo {provider_symbol}.volume[{index}]"),
        }
        rows.append(row)
    return _finalize_rows(rows, start, end, f"yahoo {provider_symbol}")


def normalize_twelve_data(raw: bytes, provider_symbol: str, start: date, end: date) -> bytes:
    try:
        payload = json.loads(raw, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise CaptureError(f"twelve_data {provider_symbol}: response is not JSON") from exc
    response = _require_mapping(payload, "twelve_data response")
    if response.get("status") != "ok":
        raise CaptureError(f"twelve_data {provider_symbol}: provider returned a non-ok payload")
    meta = _require_mapping(response.get("meta"), "twelve_data meta")
    if meta.get("symbol") != provider_symbol:
        raise CaptureError(f"twelve_data {provider_symbol}: response symbol mismatch")
    values = _require_sequence(response.get("values"), "twelve_data values")
    rows: list[dict[str, str]] = []
    for index, raw_value in enumerate(values):
        value = _require_mapping(raw_value, f"twelve_data values[{index}]")
        day = _parse_day(value.get("datetime"), f"twelve_data {provider_symbol}.datetime[{index}]")
        row = {
            "date": day.isoformat(),
            "open": _decimal_text(_decimal(value.get("open"), f"twelve_data {provider_symbol}.open[{index}]")),
            "high": _decimal_text(_decimal(value.get("high"), f"twelve_data {provider_symbol}.high[{index}]")),
            "low": _decimal_text(_decimal(value.get("low"), f"twelve_data {provider_symbol}.low[{index}]")),
            "close": _decimal_text(_decimal(value.get("close"), f"twelve_data {provider_symbol}.close[{index}]")),
            "adjusted_close": "",
            "volume": _volume_text(value.get("volume"), f"twelve_data {provider_symbol}.volume[{index}]"),
        }
        rows.append(row)
    return _finalize_rows(rows, start, end, f"twelve_data {provider_symbol}")


def _epoch(day: date) -> str:
    return str(int(datetime.combine(day, time.min, tzinfo=UTC).timestamp()))


def _request_details(
    plan: Mapping[str, Any],
    provider: str,
    provider_symbol: str,
    start: date,
    end: date,
    environ: Mapping[str, str],
) -> tuple[str, dict[str, str], dict[str, Any], str | None]:
    config = _require_mapping(_require_mapping(plan["providers"], "providers")[provider], f"providers.{provider}")
    request = {str(key): str(value) for key, value in _require_mapping(config["request"], "request").items()}
    credential: str | None = None
    if provider == "yahoo":
        endpoint = str(config["endpoint"]).format(symbol=provider_symbol)
        params = request | {"period1": _epoch(start), "period2": _epoch(end + timedelta(days=1))}
    elif provider == "twelve_data":
        endpoint = str(config["endpoint"])
        credential_env = str(config["credential_env"])
        credential = environ.get(credential_env)
        if not credential:
            raise CaptureError(f"twelve_data requires runtime environment variable {credential_env}")
        request_end = end + timedelta(days=1) if plan["schema"] == "truealpha.factor-matrix-sample-plan@v2" else end
        params = request | {
            "symbol": provider_symbol,
            "start_date": start.isoformat(),
            "end_date": request_end.isoformat(),
            "apikey": credential,
        }
    else:
        raise CaptureError(f"unsupported provider: {provider}")
    safe_params = {key: value for key, value in params.items() if key != "apikey"}
    metadata = {
        "schema": "truealpha.factor-matrix-sample-request@v1",
        "provider": provider,
        "provider_symbol": provider_symbol,
        "endpoint": endpoint,
        "parameters": dict(sorted(safe_params.items())),
        "credential_env": config.get("credential_env"),
    }
    return endpoint, params, metadata, credential


def _fetch(
    client: httpx.Client,
    plan: Mapping[str, Any],
    provider: str,
    provider_symbol: str,
    start: date,
    end: date,
    environ: Mapping[str, str],
    sleeper: Callable[[float], None],
) -> tuple[bytes, bytes, bytes]:
    endpoint, params, metadata, credential = _request_details(plan, provider, provider_symbol, start, end, environ)
    for attempt in range(2):
        try:
            response = client.get(endpoint, params=params)
        except httpx.HTTPError as exc:
            raise CaptureError(f"{provider} {provider_symbol}: request failed ({type(exc).__name__})") from None
        if response.status_code != 429 or attempt == 1:
            break
        retry_after = response.headers.get("Retry-After", "60")
        try:
            delay = float(retry_after)
        except ValueError:
            delay = 60.0
        sleeper(delay if 0 <= delay <= 300 else 60.0)
    if response.status_code < 200 or response.status_code >= 300:
        raise CaptureError(f"{provider} {provider_symbol}: HTTP {response.status_code}")
    raw = response.content
    if credential and credential.encode() in raw:
        raise CaptureError(f"{provider} {provider_symbol}: response echoed the runtime credential")
    if provider == "yahoo":
        normalized = normalize_yahoo(raw, provider_symbol, start, end)
    else:
        normalized = normalize_twelve_data(raw, provider_symbol, start, end)
    return raw, normalized, _canonical_json(metadata)


def _csv_summary(body: bytes, *, start: date | None = None, end: date | None = None) -> dict[str, Any]:
    try:
        reader = csv.DictReader(StringIO(body.decode()))
        if tuple(reader.fieldnames or ()) != NORMALIZED_COLUMNS:
            raise CaptureError("normalized CSV header mismatch")
        rows = list(reader)
    except UnicodeDecodeError as exc:
        raise CaptureError("normalized CSV is not UTF-8") from exc
    if not rows:
        raise CaptureError("normalized CSV is empty")
    dates = [row["date"] for row in rows]
    if len(set(dates)) != len(dates) or dates != sorted(dates):
        raise CaptureError("normalized CSV dates are duplicate or unsorted")
    parsed_dates = [_parse_day(value, "normalized CSV date") for value in dates]
    if (start is not None and parsed_dates[0] < start) or (end is not None and parsed_dates[-1] > end):
        raise CaptureError("normalized CSV contains a date outside the frozen window")
    for index, row in enumerate(rows):
        if None in row or any(row[field] in {None, ""} for field in NORMALIZED_COLUMNS if field != "adjusted_close"):
            raise CaptureError(f"normalized CSV row {index} has a missing required field")
        normalized = {
            "open": _decimal_text(_decimal(row["open"], f"normalized CSV open[{index}]")),
            "high": _decimal_text(_decimal(row["high"], f"normalized CSV high[{index}]")),
            "low": _decimal_text(_decimal(row["low"], f"normalized CSV low[{index}]")),
            "close": _decimal_text(_decimal(row["close"], f"normalized CSV close[{index}]")),
        }
        _volume_text(row["volume"], f"normalized CSV volume[{index}]")
        if row["adjusted_close"]:
            _decimal(row["adjusted_close"], f"normalized CSV adjusted_close[{index}]")
        _validate_bar(normalized, f"normalized CSV row {index}")
    return {"row_count": len(rows), "date_start": dates[0], "date_end": dates[-1]}


def _artifact(path: str, body: bytes, provider: str, symbol: str, kind: str) -> dict[str, Any]:
    item: dict[str, Any] = {
        "path": path,
        "provider": provider,
        "symbol": symbol,
        "kind": kind,
        "byte_length": len(body),
        "sha256": _sha256(body),
    }
    if kind == "normalized_csv":
        item.update(_csv_summary(body))
    return item


def _safe_relative_path(value: Any) -> PurePosixPath:
    if not isinstance(value, str):
        raise CaptureError("artifact path must be a string")
    path = PurePosixPath(value)
    if "\\" in value or path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise CaptureError(f"unsafe artifact path: {value!r}")
    return path


def validate_capture(
    capture_dir: Path,
    plan: Mapping[str, Any],
    providers: Sequence[str],
    *,
    plan_sha256: str,
) -> Path:
    manifest_path = capture_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaptureError(f"existing capture is incomplete: {capture_dir}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != "truealpha.factor-matrix-sample-capture@v1":
        raise CaptureError("existing capture manifest schema mismatch")
    if manifest.get("capture_id") != capture_dir.name:
        raise CaptureError("existing capture ID mismatch")
    if manifest.get("plan_sha256") != plan_sha256:
        raise CaptureError("existing capture plan hash mismatch")
    if (
        tuple(manifest.get("providers", ())) != tuple(providers)
        or tuple(manifest.get("symbols", ())) != EXPECTED_SYMBOLS
    ):
        raise CaptureError("existing capture denominator mismatch")

    artifacts = _require_sequence(manifest.get("artifacts"), "manifest.artifacts")
    expected = {
        (provider, symbol, kind)
        for provider in providers
        for symbol in EXPECTED_SYMBOLS
        for kind in ("normalized_csv", "raw_response", "request_metadata")
    }
    observed: set[tuple[Any, Any, Any]] = set()
    artifact_paths: set[str] = set()
    start = _parse_day(_require_mapping(plan["window"], "window")["start"], "window.start")
    end = _parse_day(_require_mapping(plan["window"], "window")["end"], "window.end")
    for raw_item in artifacts:
        item = _require_mapping(raw_item, "manifest artifact")
        relative = _safe_relative_path(item.get("path"))
        artifact_path = capture_dir.joinpath(*relative.parts)
        if artifact_path.is_symlink() or not artifact_path.is_file():
            raise CaptureError(f"capture artifact is missing: {relative}")
        body = artifact_path.read_bytes()
        if len(body) != item.get("byte_length") or _sha256(body) != item.get("sha256"):
            raise CaptureError(f"capture artifact hash mismatch: {relative}")
        key = (item.get("provider"), item.get("symbol"), item.get("kind"))
        if key in observed:
            raise CaptureError(f"duplicate capture artifact identity: {key}")
        observed.add(key)
        provider, symbol, kind = key
        if not isinstance(kind, str) or kind not in ARTIFACT_LAYOUT:
            raise CaptureError(f"unsupported capture artifact kind: {kind!r}")
        directory, suffix = ARTIFACT_LAYOUT[kind]
        expected_path = f"{provider}/{directory}/{symbol}.{suffix}"
        if relative.as_posix() != expected_path:
            raise CaptureError(f"capture artifact path disagrees with its identity: {relative}")
        artifact_paths.add(relative.as_posix())
        if item.get("kind") == "normalized_csv":
            summary = _csv_summary(body, start=start, end=end)
            if any(item.get(field) != value for field, value in summary.items()):
                raise CaptureError(f"normalized summary mismatch: {relative}")
    if observed != expected:
        raise CaptureError("capture artifacts do not cover the full provider/symbol denominator")
    actual_paths = {
        path.relative_to(capture_dir).as_posix()
        for path in capture_dir.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if actual_paths != artifact_paths | {"manifest.json"}:
        raise CaptureError("capture directory contains unmanifested or missing files")
    return manifest_path


def capture_samples(
    *,
    capture_id: str,
    plan_path: Path = DEFAULT_PLAN,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    providers: Sequence[str] = ("yahoo",),
    environ: Mapping[str, str] | None = None,
    client: httpx.Client | None = None,
    sleeper: Callable[[float], None] = sleep,
) -> Path:
    if CAPTURE_ID_RE.fullmatch(capture_id) is None:
        raise CaptureError("capture ID must use 1-80 lowercase letters, digits, dots, dashes, or underscores")
    selected = tuple(providers)
    if (
        not selected
        or len(set(selected)) != len(selected)
        or any(provider not in EXPECTED_PROVIDERS for provider in selected)
    ):
        raise CaptureError(f"providers must be a unique subset of {list(EXPECTED_PROVIDERS)}")
    selected = tuple(sorted(selected))
    plan = load_plan(plan_path)
    repository_root = DATA_ENGINE_ROOT.parents[1]
    try:
        plan_relative = plan_path.resolve().relative_to(repository_root.resolve()).as_posix()
    except ValueError as exc:
        raise CaptureError("capture plan must be a checked-in repository path") from exc
    if plan_path.is_symlink() or not plan_path.is_file():
        raise CaptureError("capture plan must be a regular file")
    plan_hash = _file_sha256(plan_path)
    _validate_fixture_hashes(plan)
    environment = os.environ if environ is None else environ
    window = _require_mapping(plan["window"], "window")
    start = _parse_day(window["start"], "window.start")
    end = _parse_day(window["end"], "window.end")
    capture_dir = output_root / capture_id
    if capture_dir.exists():
        return validate_capture(capture_dir, plan, selected, plan_sha256=plan_hash)
    for provider in selected:
        _request_details(plan, provider, EXPECTED_SYMBOLS[0], start, end, environment)
    output_root.mkdir(parents=True, exist_ok=True)

    own_client = client is None
    http = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30.0, follow_redirects=True)
    payloads: dict[str, bytes] = {}
    artifacts: list[dict[str, Any]] = []
    try:
        symbols = _require_sequence(plan["symbols"], "symbols")
        for provider in selected:
            for raw_symbol in symbols:
                symbol = _require_mapping(raw_symbol, "symbol")
                canonical_symbol = str(symbol["symbol"])
                provider_symbol = str(_require_mapping(symbol["provider_symbols"], "provider_symbols")[provider])
                raw, normalized, request_metadata = _fetch(
                    http, plan, provider, provider_symbol, start, end, environment, sleeper
                )
                bodies = {
                    f"{provider}/raw/{canonical_symbol}.json": (raw, "raw_response"),
                    f"{provider}/normalized/{canonical_symbol}.csv": (normalized, "normalized_csv"),
                    f"{provider}/requests/{canonical_symbol}.json": (request_metadata, "request_metadata"),
                }
                for relative_path, (body, kind) in bodies.items():
                    payloads[relative_path] = body
                    artifacts.append(_artifact(relative_path, body, provider, canonical_symbol, kind))
    finally:
        if own_client:
            http.close()

    manifest = {
        "schema": "truealpha.factor-matrix-sample-capture@v1",
        "capture_id": capture_id,
        "completed_at": datetime.now(UTC).isoformat(),
        "plan_path": plan_relative,
        "plan_sha256": plan_hash,
        "providers": list(selected),
        "symbols": list(EXPECTED_SYMBOLS),
        "window": dict(window),
        "provider_semantics": {
            provider: _require_mapping(_require_mapping(plan["providers"], "providers")[provider], provider)[
                "adjustment_semantics"
            ]
            for provider in selected
        },
        "artifacts": sorted(artifacts, key=lambda item: str(item["path"])),
        "evidence_ceiling": plan["evidence_ceiling"],
    }
    payloads["manifest.json"] = _canonical_json(manifest)

    staging = Path(tempfile.mkdtemp(prefix=f".{capture_id}.", dir=output_root))
    try:
        for relative_path, body in payloads.items():
            target = staging.joinpath(*PurePosixPath(relative_path).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        os.replace(staging, capture_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return validate_capture(capture_dir, plan, selected, plan_sha256=plan_hash)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-id", required=True)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument(
        "--providers",
        default="yahoo",
        help="comma-separated providers: yahoo,twelve_data (Twelve Data requires TWELVE_DATA_API_KEY)",
    )
    args = parser.parse_args()
    providers = tuple(value.strip() for value in args.providers.split(",") if value.strip())
    try:
        manifest_path = capture_samples(
            capture_id=args.capture_id,
            plan_path=args.plan,
            output_root=args.output_root,
            providers=providers,
        )
    except CaptureError as exc:
        raise SystemExit(f"capture failed: {exc}") from None
    print(f"capture manifest: {manifest_path} sha256={_file_sha256(manifest_path)}")


if __name__ == "__main__":
    main()
