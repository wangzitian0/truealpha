"""#1519 Step 5: One knowable instant per source (`AsOfAxis` and `knowable_instant`)."""

from __future__ import annotations

import ast
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from data_engine.datahub.production_topt.source_registrations import (
    REGISTRATIONS,
    AsOfAxis,
    SourceRegistration,
    knowable_instant,
)


def test_knowable_instant_stamps_explicit_utc_time_for_every_axis() -> None:
    day = date(2026, 3, 31)
    assert knowable_instant(AsOfAxis.SETTLED_SESSION, day) == datetime(2026, 3, 31, 20, 0, tzinfo=UTC)
    assert knowable_instant(AsOfAxis.SETTLED_SESSION, date(2026, 1, 15)) == datetime(2026, 1, 15, 21, 0, tzinfo=UTC)
    assert knowable_instant(AsOfAxis.ACCESSION, day) == datetime(2026, 3, 31, 23, 59, 59, 999999, tzinfo=UTC)
    assert knowable_instant(AsOfAxis.RELEASE, day) == datetime(2026, 3, 31, 0, 0, tzinfo=UTC)
    assert knowable_instant(AsOfAxis.BUSINESS_DAY, day) == datetime(2026, 4, 1, 0, 0, tzinfo=UTC)


def test_all_registered_sources_declare_an_as_of_axis() -> None:
    assert REGISTRATIONS
    for registration in REGISTRATIONS:
        assert isinstance(registration.as_of_axis, AsOfAxis)


def test_source_registration_rejects_invalid_as_of_axis() -> None:
    with pytest.raises(ValueError, match="as_of_axis"):
        SourceRegistration(
            source_id="source-test-bad-axis",
            version="v1",
            semantic_types=("test",),
            freshness_max_age={},
            route_builder="test:builder",
            as_of_axis="midnight",  # type: ignore[arg-type]
        )


def test_no_adapter_or_seeder_stamps_midnight_as_knowable_or_transaction_time() -> None:
    """No production adapter or seeder in `apps/data-engine/src/data_engine` may combine a date
    with `datetime.min.time()` to manufacture a `knowable_at` or `transaction_time`."""
    data_engine_src = Path(__file__).resolve().parents[1] / "src" / "data_engine"
    offenders: list[str] = []
    for path in sorted(data_engine_src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "combine"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Call)
                and isinstance(node.args[1].func, ast.Attribute)
                and node.args[1].func.attr == "time"
                and isinstance(node.args[1].func.value, ast.Attribute)
                and node.args[1].func.value.attr == "min"
            ):
                continue
            rel = path.relative_to(data_engine_src)
            line = path.read_text(encoding="utf-8").splitlines()[node.lineno - 1]
            if "knowable" in line or "transaction_time" in line or "_day_start_utc" in line or "_utc_midnight" in line:
                offenders.append(f"{rel}:{node.lineno}: {line.strip()}")
    assert offenders == []
