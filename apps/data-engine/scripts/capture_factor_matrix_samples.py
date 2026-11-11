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
from data_engine.datahub.fetch_key import FetchKey
from data_engine.sources import gateway

DATA_ENGINE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLAN = DATA_ENGINE_ROOT / "samples/factor_matrix/capture_plan.v2.json"
DEFAULT_OUTPUT_ROOT = DATA_ENGINE_ROOT / "samples/factor_matrix/captures"
EXPECTED_SYMBOLS = ("ADM", "DDOG", "DUOL", "JPM", "META", "NICE", "NVDA", "PLUG", "SHOP")
EXPECTED_PROVIDERS = ("twelve_data", "yahoo")
EXPECTED_YAHOO_REQUEST_CONTRACT = {
    "symbol": {"location": "endpoint_path", "name": "symbol"},
    "window_start": {"name": "period1", "encoding": "inclusive start date at 00:00:00 UTC as Unix seconds"},
    "window_end": {
        "name": "period2",
        "encoding": "exclusive day after the inclusive end date at 00:00:00 UTC as Unix seconds",
    },
}
_TWELVE_BASE = {
    "symbol": {"location": "query", "name": "symbol"},
    "window_start": {"name": "start_date", "encoding": "inclusive ISO 8601 calendar date"},
}
EXPECTED_TWELVE_REQUEST_CONTRACTS = {
    "truealpha.factor-matrix-sample-plan@v1": {
        **_TWELVE_BASE,
        "window_end": {"name": "end_date", "encoding": "inclusive ISO 8601 calendar date"},
    },
    "truealpha.factor-matrix-sample-plan@v2": {
        **_TWELVE_BASE,
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
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise CaptureError(f"{context} must be an ISO date")


def _decimal(value: Any, context: str, *, positive: bool = True) -> Decimal:
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise CaptureError(f"{context} must be a finite decimal")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise CaptureError(f"{context} must be a finite decimal") from exc
    if not parsed.is_finite() or (positive and parsed <= 0) or (not positive and parsed < 0):
        raise CaptureError(f"{context} must be a finite {'positive' if positive else 'non-negative'} decimal")
    return parsed


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return (text.rstrip("0").rstrip(".") if "." in text else text) or "0"


def _volume_text(value: Any, context: str) -> str:
    parsed = _decimal(value, context, positive=False)
    if parsed != parsed.to_integral_value():
        raise CaptureError(f"{context} must be an integer")
    return str(int(parsed))


def _ohlc_dict(
    date_str: str,
    ohlc_src: Mapping[str, Any],
    adj_close: Any,
    volume: Any,
    prefix: str,
    idx: int,
) -> dict[str, str]:
    row = {
        "date": date_str,
        **{
            k: _decimal_text(_decimal(ohlc_src.get(k), f"{prefix}.{k}[{idx}]"))
            for k in ("open", "high", "low", "close")
        },
        "adjusted_close": ""
        if adj_close is None
        else _decimal_text(_decimal(adj_close, f"{prefix}.adjusted_close[{idx}]")),
        "volume": _volume_text(volume, f"{prefix}.volume[{idx}]"),
    }
    return row


def load_plan(path: Path = DEFAULT_PLAN) -> dict[str, Any]:
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaptureError(f"cannot load capture plan: {path}") from exc
    if not isinstance(plan, dict) or plan.get("schema") not in EXPECTED_TWELVE_REQUEST_CONTRACTS:
        raise CaptureError("unsupported capture plan schema")

    window = _require_mapping(plan.get("window"), "window")
    start, end = _parse_day(window.get("start"), "window.start"), _parse_day(window.get("end"), "window.end")
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
        expected = (
            EXPECTED_YAHOO_REQUEST_CONTRACT
            if provider == "yahoo"
            else EXPECTED_TWELVE_REQUEST_CONTRACTS[str(plan["schema"])]
        )
        if config.get("request_contract") != expected:
            raise CaptureError(f"providers.{provider}.request_contract differs from the frozen schema contract")
        fixture = _require_mapping(config.get("fixture"), f"providers.{provider}.fixture")
        if (
            fixture.get("encoding") != "utf-8"
            or not isinstance(fixture.get("payload"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(fixture.get("sha256")))
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
    if tuple(symbol_names) != EXPECTED_SYMBOLS:
        raise CaptureError(f"symbols must be exactly {list(EXPECTED_SYMBOLS)} in canonical order")
    return plan


def _validate_fixture_hashes(plan: Mapping[str, Any]) -> None:
    for provider, raw_config in _require_mapping(plan["providers"], "providers").items():
        fixture = _require_mapping(_require_mapping(raw_config, provider)["fixture"], f"{provider}.fixture")
        if _sha256(str(fixture["payload"]).encode()) != fixture["sha256"]:
            raise CaptureError(f"{provider} parser fixture hash mismatch")


def _validate_bar(row: Mapping[str, str], context: str) -> None:
    o, h, low, c = Decimal(row["open"]), Decimal(row["high"]), Decimal(row["low"]), Decimal(row["close"])
    if h < max(o, low, c) or low > min(o, h, c):
        raise CaptureError(f"{context} has inconsistent OHLC bounds")


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
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=NORMALIZED_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode()


def _parse_json(raw: bytes, label: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(raw, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise CaptureError(f"{label}: response is not JSON") from exc
    return _require_mapping(payload, f"{label} response")


def normalize_yahoo(raw: bytes, provider_symbol: str, start: date, end: date) -> bytes:
    label = f"yahoo {provider_symbol}"
    chart = _require_mapping(_parse_json(raw, label).get("chart"), "yahoo chart")
    if chart.get("error") is not None:
        raise CaptureError(f"{label}: provider returned an error payload")
    results = _require_sequence(chart.get("result"), "yahoo chart.result")
    if len(results) != 1:
        raise CaptureError(f"{label}: expected one chart result")
    result = _require_mapping(results[0], "yahoo chart.result[0]")
    if _require_mapping(result.get("meta"), "yahoo meta").get("symbol") != provider_symbol:
        raise CaptureError(f"{label}: response symbol mismatch")
    timestamps = _require_sequence(result.get("timestamp"), "yahoo timestamp")
    indicators = _require_mapping(result.get("indicators"), "yahoo indicators")
    quotes = _require_sequence(indicators.get("quote"), "yahoo indicators.quote")
    adjusted = _require_sequence(indicators.get("adjclose"), "yahoo indicators.adjclose")
    if len(quotes) != 1 or len(adjusted) != 1:
        raise CaptureError(f"{label}: expected one quote and adjusted-close series")
    quote = _require_mapping(quotes[0], "yahoo quote series")
    adjusted_values = _require_sequence(
        _require_mapping(adjusted[0], "yahoo adjusted-close series").get("adjclose"), "yahoo adjusted-close values"
    )
    fields = {
        k: _require_sequence(quote.get(k), f"yahoo quote.{k}") for k in ("open", "high", "low", "close", "volume")
    }
    if len({len(timestamps), len(adjusted_values), *(len(v) for v in fields.values())}) != 1:
        raise CaptureError(f"{label}: series lengths differ")
    rows: list[dict[str, str]] = []
    for i, ts in enumerate(timestamps):
        if isinstance(ts, bool) or not isinstance(ts, int):
            raise CaptureError(f"{label}: timestamp[{i}] must be an integer")
        day = datetime.fromtimestamp(ts, tz=UTC).date().isoformat()
        bar_src = {k: fields[k][i] for k in ("open", "high", "low", "close")}
        rows.append(_ohlc_dict(day, bar_src, adjusted_values[i], fields["volume"][i], label, i))
    return _finalize_rows(rows, start, end, label)


def normalize_twelve_data(raw: bytes, provider_symbol: str, start: date, end: date) -> bytes:
    label = f"twelve_data {provider_symbol}"
    response = _parse_json(raw, label)
    if response.get("status") != "ok":
        raise CaptureError(f"{label}: provider returned a non-ok payload")
    if _require_mapping(response.get("meta"), "twelve_data meta").get("symbol") != provider_symbol:
        raise CaptureError(f"{label}: response symbol mismatch")
    values = _require_sequence(response.get("values"), "twelve_data values")
    rows: list[dict[str, str]] = []
    for i, raw_val in enumerate(values):
        val = _require_mapping(raw_val, f"twelve_data values[{i}]")
        day = _parse_day(val.get("datetime"), f"{label}.datetime[{i}]").isoformat()
        rows.append(_ohlc_dict(day, val, None, val.get("volume"), label, i))
    return _finalize_rows(rows, start, end, label)


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
    request = {str(k): str(v) for k, v in _require_mapping(config["request"], "request").items()}
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
        req_end = end + timedelta(days=1) if plan["schema"] == "truealpha.factor-matrix-sample-plan@v2" else end
        params = request | {
            "symbol": provider_symbol,
            "start_date": start.isoformat(),
            "end_date": req_end.isoformat(),
            "apikey": credential,
        }
    else:
        raise CaptureError(f"unsupported provider: {provider}")
    metadata = {
        "schema": "truealpha.factor-matrix-sample-request@v1",
        "provider": provider,
        "provider_symbol": provider_symbol,
        "endpoint": endpoint,
        "parameters": {k: params[k] for k in sorted(params) if k != "apikey"},
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
    seat, label = ("yahoo", "chart") if provider == "yahoo" else ("twelvedata", "time_series")
    for attempt in range(2):
        try:
            response = gateway.http_get(
                client,
                seat,
                label,
                endpoint,
                identity=FetchKey(seat, label, f"symbol:{provider_symbol}", f"{start.isoformat()}:{end.isoformat()}"),
                caller="capture_factor_matrix_samples._fetch",
                params=params,
            )
        except httpx.HTTPError as exc:
            raise CaptureError(f"{provider} {provider_symbol}: request failed ({type(exc).__name__})") from None
        if response.status_code != 429 or attempt == 1:
            break
        try:
            delay = float(response.headers.get("Retry-After", "60"))
        except ValueError:
            delay = 60.0
        sleeper(delay if 0 <= delay <= 300 else 60.0)
    if not (200 <= response.status_code < 300):
        raise CaptureError(f"{provider} {provider_symbol}: HTTP {response.status_code}")
    raw = response.content
    if credential and credential.encode() in raw:
        raise CaptureError(f"{provider} {provider_symbol}: response echoed the runtime credential")
    normalizer = normalize_yahoo if provider == "yahoo" else normalize_twelve_data
    return raw, normalizer(raw, provider_symbol, start, end), _canonical_json(metadata)


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
    parsed = [_parse_day(v, "normalized CSV date") for v in dates]
    if (start is not None and parsed[0] < start) or (end is not None and parsed[-1] > end):
        raise CaptureError("normalized CSV contains a date outside the frozen window")
    for i, row in enumerate(rows):
        if None in row or any(row[f] in {None, ""} for f in NORMALIZED_COLUMNS if f != "adjusted_close"):
            raise CaptureError(f"normalized CSV row {i} has a missing required field")
        bar = {k: _decimal_text(_decimal(row[k], f"normalized CSV {k}[{i}]")) for k in ("open", "high", "low", "close")}
        _volume_text(row["volume"], f"normalized CSV volume[{i}]")
        if row["adjusted_close"]:
            _decimal(row["adjusted_close"], f"normalized CSV adjusted_close[{i}]")
        _validate_bar(bar, f"normalized CSV row {i}")
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
    if "\\" in value or path.is_absolute() or not path.parts or any(p in {"", ".", ".."} for p in path.parts):
        raise CaptureError(f"unsafe artifact path: {value!r}")
    return path


def validate_capture(capture_dir: Path, plan: Mapping[str, Any], providers: Sequence[str], *, plan_sha256: str) -> Path:
    if capture_dir.is_symlink() or not capture_dir.is_dir():
        raise CaptureError(f"existing capture is not a regular directory: {capture_dir}")
    capture_root = capture_dir.resolve()
    manifest_path = capture_dir / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise CaptureError(f"existing capture is incomplete: {capture_dir}")
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
    expected = {(p, s, k) for p in providers for s in EXPECTED_SYMBOLS for k in ARTIFACT_LAYOUT}
    observed: set[tuple[Any, Any, Any]] = set()
    artifact_paths: set[str] = set()
    window = _require_mapping(plan["window"], "window")
    start, end = _parse_day(window["start"], "window.start"), _parse_day(window["end"], "window.end")
    for raw_item in artifacts:
        item = _require_mapping(raw_item, "manifest artifact")
        relative = _safe_relative_path(item.get("path"))
        artifact_path = capture_dir.joinpath(*relative.parts)
        if artifact_path.is_symlink() or not artifact_path.is_file():
            raise CaptureError(f"capture artifact is missing: {relative}")
        try:
            resolved = artifact_path.resolve(strict=True)
            resolved.relative_to(capture_root)
        except (OSError, ValueError) as exc:
            raise CaptureError(f"capture artifact escapes the capture directory: {relative}") from exc
        if any(capture_dir.joinpath(*relative.parts[:i]).is_symlink() for i in range(1, len(relative.parts))):
            raise CaptureError(f"capture artifact has a symlink directory: {relative}")
        body = resolved.read_bytes()
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
        if relative.as_posix() != f"{provider}/{directory}/{symbol}.{suffix}":
            raise CaptureError(f"capture artifact path disagrees with its identity: {relative}")
        artifact_paths.add(relative.as_posix())
        if kind == "normalized_csv":
            summary = _csv_summary(body, start=start, end=end)
            if any(item.get(f) != v for f, v in summary.items()):
                raise CaptureError(f"normalized summary mismatch: {relative}")
    if observed != expected:
        raise CaptureError("capture artifacts do not cover the full provider/symbol denominator")
    actual_paths = {
        p.relative_to(capture_dir).as_posix() for p in capture_dir.rglob("*") if p.is_file() and not p.is_symlink()
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
    if not selected or len(set(selected)) != len(selected) or any(p not in EXPECTED_PROVIDERS for p in selected):
        raise CaptureError(f"providers must be a unique subset of {list(EXPECTED_PROVIDERS)}")
    selected = tuple(sorted(selected))
    if plan_path.is_symlink() or not plan_path.is_file():
        raise CaptureError("capture plan must be a regular file")
    try:
        plan_relative = plan_path.resolve().relative_to(DATA_ENGINE_ROOT.parents[1].resolve()).as_posix()
    except ValueError as exc:
        raise CaptureError("capture plan must be a checked-in repository path") from exc
    plan_hash = _file_sha256(plan_path)
    plan = load_plan(plan_path)
    _validate_fixture_hashes(plan)
    environment = os.environ if environ is None else environ
    window = _require_mapping(plan["window"], "window")
    start, end = _parse_day(window["start"], "window.start"), _parse_day(window["end"], "window.end")
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
        for provider in selected:
            for raw_symbol in _require_sequence(plan["symbols"], "symbols"):
                symbol = _require_mapping(raw_symbol, "symbol")
                canonical = str(symbol["symbol"])
                prov_sym = str(_require_mapping(symbol["provider_symbols"], "provider_symbols")[provider])
                raw, normalized, req_meta = _fetch(http, plan, provider, prov_sym, start, end, environment, sleeper)
                for rel_path, (body, kind) in (
                    (f"{provider}/raw/{canonical}.json", (raw, "raw_response")),
                    (f"{provider}/normalized/{canonical}.csv", (normalized, "normalized_csv")),
                    (f"{provider}/requests/{canonical}.json", (req_meta, "request_metadata")),
                ):
                    payloads[rel_path] = body
                    artifacts.append(_artifact(rel_path, body, provider, canonical, kind))
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
            p: _require_mapping(_require_mapping(plan["providers"], "providers")[p], p)["adjustment_semantics"]
            for p in selected
        },
        "artifacts": sorted(artifacts, key=lambda item: str(item["path"])),
        "evidence_ceiling": plan["evidence_ceiling"],
    }
    payloads["manifest.json"] = _canonical_json(manifest)

    staging = Path(tempfile.mkdtemp(prefix=f".{capture_id}.", dir=output_root))
    try:
        for rel_path, body in payloads.items():
            target = staging.joinpath(*PurePosixPath(rel_path).parts)
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
    parser.add_argument("--providers", default="yahoo")
    args = parser.parse_args()
    providers = tuple(v.strip() for v in args.providers.split(",") if v.strip())
    try:
        manifest_path = capture_samples(
            capture_id=args.capture_id, plan_path=args.plan, output_root=args.output_root, providers=providers
        )
    except CaptureError as exc:
        raise SystemExit(f"capture failed: {exc}") from None
    print(f"capture manifest: {manifest_path} sha256={_file_sha256(manifest_path)}")


if __name__ == "__main__":
    with gateway.script_run("capture_factor_matrix_samples"):
        main()
