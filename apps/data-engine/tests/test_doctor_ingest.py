"""Tests for doctor_ingest credential redaction and runtime probe delegation."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from truealpha_runtime.checks import DependencyStatus, ProbeResult

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "doctor_ingest.py"


def _load_doctor():
    spec = importlib.util.spec_from_file_location("doctor_ingest", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_doctor_ingest_redacts_query_string_password_in_target_and_errors(monkeypatch, capsys) -> None:
    doctor = _load_doctor()
    secret = "SecretQueryPass123"
    dsn = f"postgresql://postgres:EmbeddedPass456@db.example:5432/truealpha?password={secret}"
    fake_settings = SimpleNamespace(
        environment="staging",
        database_url=dsn,
        database_connect_timeout_seconds=5,
        s3_endpoint="http://s3.example:9000",
        s3_access_key="key",
        s3_secret_key=SimpleNamespace(get_secret_value=lambda: "s3secret"),
        moomoo_opend_host="",
        moomoo_opend_port=0,
    )

    monkeypatch.setattr("data_engine.config.settings", fake_settings)
    monkeypatch.setattr(
        doctor,
        "DatabaseCheck",
        lambda _s: SimpleNamespace(probe=lambda: ProbeResult("database", DependencyStatus.PRESENT, "ok", 1.0)),
    )
    monkeypatch.setattr(
        doctor,
        "ObjectStorageCheck",
        lambda _s: SimpleNamespace(probe=lambda: ProbeResult("object_storage", DependencyStatus.PRESENT, "ok", 1.0)),
    )
    monkeypatch.setattr("data_engine.db.connect", lambda: (_ for _ in ()).throw(RuntimeError(f"failed {dsn}")))
    monkeypatch.setattr("sys.argv", ["doctor_ingest.py"])

    with pytest.raises(SystemExit) as exit_info:
        doctor.main()

    out = capsys.readouterr().out
    assert exit_info.value.code == 1
    assert "ENVIRONMENT=staging" in out
    assert "db=db.example:5432/truealpha" in out
    assert secret not in out
    assert "EmbeddedPass456" not in out
