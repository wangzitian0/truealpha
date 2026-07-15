from __future__ import annotations

import json
import runpy
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx
import pytest

REPOSITORY_ROOT = Path(__file__).parents[3]
SCRIPT_PATH = Path(__file__).parents[1] / "scripts/capture_factor_matrix_samples.py"
PLAN_PATH = Path(__file__).parents[1] / "samples/factor_matrix/capture_plan.v2.json"
LEGACY_PLAN_PATH = Path(__file__).parents[1] / "samples/factor_matrix/capture_plan.v1.json"
CAPTURE = runpy.run_path(str(SCRIPT_PATH))
CaptureError = CAPTURE["CaptureError"]
EXPECTED_SYMBOLS = CAPTURE["EXPECTED_SYMBOLS"]
load_plan = CAPTURE["load_plan"]
normalize_yahoo = CAPTURE["normalize_yahoo"]
normalize_twelve_data = CAPTURE["normalize_twelve_data"]
capture_samples = CAPTURE["capture_samples"]
validate_capture = CAPTURE["validate_capture"]
validate_fixture_hashes = CAPTURE["_validate_fixture_hashes"]


def _fixture(name: str) -> dict[str, Any]:
    provider = {"yahoo_chart.v1.json": "yahoo", "twelve_data_time_series.v1.json": "twelve_data"}[name]
    plan = json.loads(PLAN_PATH.read_text())
    return json.loads(plan["providers"][provider]["fixture"]["payload"])


def _fixture_bytes(provider: str) -> bytes:
    plan = json.loads(PLAN_PATH.read_text())
    return plan["providers"][provider]["fixture"]["payload"].encode()


class FixtureClient:
    def __init__(
        self,
        *,
        fail_symbol: str | None = None,
        http_status: int = 200,
        rate_limit_once_symbol: str | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.fail_symbol = fail_symbol
        self.http_status = http_status
        self.rate_limit_once_symbol = rate_limit_once_symbol
        self.rate_limit_returned = False

    def get(self, url: str, *, params: dict[str, str]) -> httpx.Response:
        self.calls.append((url, params))
        if "twelvedata" in url:
            symbol = params["symbol"]
            payload = _fixture("twelve_data_time_series.v1.json")
            payload["meta"]["symbol"] = symbol
            if symbol == self.fail_symbol:
                payload["values"][0]["close"] = None
        else:
            symbol = url.rsplit("/", 1)[-1]
            payload = _fixture("yahoo_chart.v1.json")
            payload["chart"]["result"][0]["meta"]["symbol"] = symbol
            if symbol == self.fail_symbol:
                payload["chart"]["result"][0]["indicators"]["quote"][0]["close"][0] = None
        request = httpx.Request("GET", url, params=params)
        if symbol == self.rate_limit_once_symbol and not self.rate_limit_returned:
            self.rate_limit_returned = True
            return httpx.Response(429, headers={"Retry-After": "7"}, request=request)
        return httpx.Response(self.http_status, json=payload, request=request)


class ExplodingClient:
    def get(self, url: str, *, params: dict[str, str]) -> httpx.Response:
        raise AssertionError(f"existing capture unexpectedly made a request to {url} with {sorted(params)}")


def test_plan_freezes_original_nine_symbols_and_provider_semantics():
    plan = load_plan(PLAN_PATH)

    assert tuple(item["symbol"] for item in plan["symbols"]) == EXPECTED_SYMBOLS
    assert plan["window"] == {"start": "2023-07-10", "end": "2026-07-10", "end_inclusive": True}
    assert plan["providers"]["twelve_data"]["request"]["adjust"] == "none"
    assert plan["providers"]["yahoo"]["request_contract"]["window_start"]["name"] == "period1"
    assert plan["providers"]["twelve_data"]["request_contract"]["window_end"] == {
        "name": "end_date",
        "encoding": "exclusive ISO 8601 calendar date set to the day after the inclusive sample end",
    }
    assert plan["providers"]["yahoo"]["adjustment_semantics"]["adjusted_close"].startswith("provider adjusted")


def test_plan_rejects_denominator_shrink(tmp_path: Path):
    plan = json.loads(PLAN_PATH.read_text())
    plan["symbols"].pop()
    changed = tmp_path / "capture_plan.json"
    changed.write_text(json.dumps(plan))

    with pytest.raises(CaptureError, match="symbols must be exactly"):
        load_plan(changed)


def test_plan_rejects_fixture_hash_drift():
    plan = json.loads(PLAN_PATH.read_text())
    plan["providers"]["yahoo"]["fixture"]["payload"] += " "

    with pytest.raises(CaptureError, match="fixture hash mismatch"):
        validate_fixture_hashes(plan)


def test_plan_rejects_request_contract_drift(tmp_path: Path):
    plan = json.loads(PLAN_PATH.read_text())
    plan["providers"]["twelve_data"]["request_contract"]["window_start"]["name"] = "from"
    changed = tmp_path / "capture_plan.json"
    changed.write_text(json.dumps(plan))

    with pytest.raises(CaptureError, match="request_contract differs"):
        load_plan(changed)


def test_legacy_plan_remains_loadable_for_capture_validation():
    assert load_plan(LEGACY_PLAN_PATH)["schema"] == "truealpha.factor-matrix-sample-plan@v1"


def test_provider_fixtures_normalize_to_separate_adjustment_semantics():
    start = date(2023, 7, 10)
    end = date(2026, 7, 10)
    yahoo = normalize_yahoo(_fixture_bytes("yahoo"), "DDOG", start, end).decode()
    twelve = normalize_twelve_data(_fixture_bytes("twelve_data"), "DDOG", start, end).decode()

    assert yahoo.splitlines() == [
        "date,open,high,low,close,adjusted_close,volume",
        "2023-07-10,100.25,102,99.75,101.25,101.25,3210000",
        "2023-07-11,102,104.25,101.5,103.5,103.5,2985000",
    ]
    assert twelve.splitlines() == [
        "date,open,high,low,close,adjusted_close,volume",
        "2023-07-10,100.25,102,99.75,101.25,,3210000",
        "2023-07-11,102,104.25,101.5,103.5,,2985000",
    ]


@pytest.mark.parametrize("provider", ["yahoo", "twelve_data"])
def test_normalizers_reject_null_required_values(provider: str):
    start = date(2023, 7, 10)
    end = date(2026, 7, 10)
    if provider == "yahoo":
        payload = _fixture("yahoo_chart.v1.json")
        payload["chart"]["result"][0]["indicators"]["quote"][0]["volume"][0] = None
        normalize = normalize_yahoo
    else:
        payload = _fixture("twelve_data_time_series.v1.json")
        payload["values"][0]["volume"] = None
        normalize = normalize_twelve_data

    with pytest.raises(CaptureError, match="finite decimal"):
        normalize(json.dumps(payload).encode(), "DDOG", start, end)


@pytest.mark.parametrize("provider", ["yahoo", "twelve_data"])
def test_normalizers_reject_duplicate_dates(provider: str):
    start = date(2023, 7, 10)
    end = date(2026, 7, 10)
    if provider == "yahoo":
        payload = _fixture("yahoo_chart.v1.json")
        payload["chart"]["result"][0]["timestamp"][1] = payload["chart"]["result"][0]["timestamp"][0]
        normalize = normalize_yahoo
    else:
        payload = _fixture("twelve_data_time_series.v1.json")
        payload["values"][1]["datetime"] = payload["values"][0]["datetime"]
        normalize = normalize_twelve_data

    with pytest.raises(CaptureError, match="duplicate dates"):
        normalize(json.dumps(payload).encode(), "DDOG", start, end)


def test_capture_is_full_denominator_secret_free_and_idempotent(tmp_path: Path):
    client = FixtureClient()
    secret = "test-key-must-never-be-persisted"
    manifest_path = capture_samples(
        capture_id="fixture-both-providers",
        plan_path=PLAN_PATH,
        output_root=tmp_path,
        providers=("yahoo", "twelve_data"),
        environ={"TWELVE_DATA_API_KEY": secret},
        client=client,
    )

    capture_dir = manifest_path.parent
    manifest = json.loads(manifest_path.read_text())
    assert len(client.calls) == 18
    assert manifest["providers"] == ["twelve_data", "yahoo"]
    assert manifest["symbols"] == list(EXPECTED_SYMBOLS)
    assert len(manifest["artifacts"]) == 54
    assert manifest["plan_sha256"] == sha256(PLAN_PATH.read_bytes()).hexdigest()
    assert not any(secret.encode() in path.read_bytes() for path in capture_dir.rglob("*") if path.is_file())
    request = json.loads((capture_dir / "twelve_data/requests/DDOG.json").read_text())
    assert "apikey" not in request["parameters"]
    assert request["credential_env"] == "TWELVE_DATA_API_KEY"
    assert request["parameters"]["end_date"] == "2026-07-11"

    same_manifest = capture_samples(
        capture_id="fixture-both-providers",
        plan_path=PLAN_PATH,
        output_root=tmp_path,
        providers=("twelve_data", "yahoo"),
        environ={},
        client=ExplodingClient(),
    )
    assert same_manifest == manifest_path


def test_validator_rejects_rehashed_out_of_window_rows(tmp_path: Path):
    manifest_path = capture_samples(
        capture_id="rehashed-out-of-window",
        plan_path=PLAN_PATH,
        output_root=tmp_path,
        providers=("yahoo",),
        environ={},
        client=FixtureClient(),
    )
    normalized = manifest_path.parent / "yahoo/normalized/DDOG.csv"
    body = normalized.read_text().replace("2023-07-10", "2023-07-09", 1).encode()
    normalized.write_bytes(body)
    manifest = json.loads(manifest_path.read_text())
    item = next(artifact for artifact in manifest["artifacts"] if artifact["path"] == "yahoo/normalized/DDOG.csv")
    item.update(
        {
            "byte_length": len(body),
            "sha256": sha256(body).hexdigest(),
            "date_start": "2023-07-09",
        }
    )
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(CaptureError, match="outside the frozen window"):
        validate_capture(
            manifest_path.parent,
            load_plan(PLAN_PATH),
            ("yahoo",),
            plan_sha256=sha256(PLAN_PATH.read_bytes()).hexdigest(),
        )


def test_validator_rejects_unmanifested_files(tmp_path: Path):
    manifest_path = capture_samples(
        capture_id="unmanifested-file",
        plan_path=PLAN_PATH,
        output_root=tmp_path,
        providers=("yahoo",),
        environ={},
        client=FixtureClient(),
    )
    (manifest_path.parent / "unexpected.txt").write_text("not part of the capture")

    with pytest.raises(CaptureError, match="unmanifested or missing files"):
        capture_samples(
            capture_id="unmanifested-file",
            plan_path=PLAN_PATH,
            output_root=tmp_path,
            providers=("yahoo",),
            environ={},
            client=ExplodingClient(),
        )


def test_capture_rejects_modified_existing_bytes(tmp_path: Path):
    manifest_path = capture_samples(
        capture_id="immutable-yahoo",
        plan_path=PLAN_PATH,
        output_root=tmp_path,
        providers=("yahoo",),
        environ={},
        client=FixtureClient(),
    )
    changed = manifest_path.parent / "yahoo/normalized/DDOG.csv"
    changed.write_bytes(changed.read_bytes() + b"\n")

    with pytest.raises(CaptureError, match="artifact hash mismatch"):
        capture_samples(
            capture_id="immutable-yahoo",
            plan_path=PLAN_PATH,
            output_root=tmp_path,
            providers=("yahoo",),
            environ={},
            client=ExplodingClient(),
        )


def test_failed_symbol_leaves_no_partial_capture(tmp_path: Path):
    with pytest.raises(CaptureError, match=r"JPM\.close\[0\]"):
        capture_samples(
            capture_id="all-or-nothing",
            plan_path=PLAN_PATH,
            output_root=tmp_path,
            providers=("yahoo",),
            environ={},
            client=FixtureClient(fail_symbol="JPM"),
        )

    assert not (tmp_path / "all-or-nothing").exists()
    assert not list(tmp_path.glob(".all-or-nothing.*"))


def test_twelve_data_requires_runtime_credential_before_network_or_output(tmp_path: Path):
    with pytest.raises(CaptureError, match="TWELVE_DATA_API_KEY"):
        capture_samples(
            capture_id="missing-credential",
            plan_path=PLAN_PATH,
            output_root=tmp_path,
            providers=("twelve_data",),
            environ={},
            client=ExplodingClient(),
        )

    assert not any(tmp_path.iterdir())


def test_http_failure_does_not_echo_credential_or_request_url(tmp_path: Path):
    secret = "secret-value-that-must-not-appear"
    with pytest.raises(CaptureError) as error:
        capture_samples(
            capture_id="http-error",
            plan_path=PLAN_PATH,
            output_root=tmp_path,
            providers=("twelve_data",),
            environ={"TWELVE_DATA_API_KEY": secret},
            client=FixtureClient(http_status=401),
        )

    assert str(error.value) == "twelve_data ADM: HTTP 401"
    assert secret not in str(error.value)
    assert "https://" not in str(error.value)


def test_rate_limit_retries_once_without_partial_output(tmp_path: Path):
    delays: list[float] = []
    client = FixtureClient(rate_limit_once_symbol="ADM")

    manifest_path = capture_samples(
        capture_id="rate-limit-retry",
        plan_path=PLAN_PATH,
        output_root=tmp_path,
        providers=("twelve_data",),
        environ={"TWELVE_DATA_API_KEY": "runtime-only-secret"},
        client=client,
        sleeper=delays.append,
    )

    assert manifest_path.is_file()
    assert delays == [7.0]
    assert len(client.calls) == 10


def test_incomplete_existing_directory_cannot_be_reused(tmp_path: Path):
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir(parents=True)
    (incomplete / "partial.json").write_text("{}")

    with pytest.raises(CaptureError, match="existing capture is incomplete"):
        capture_samples(
            capture_id="incomplete",
            plan_path=PLAN_PATH,
            output_root=tmp_path,
            providers=("yahoo",),
            environ={},
            client=ExplodingClient(),
        )
