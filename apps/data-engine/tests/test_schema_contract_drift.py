"""DTO <-> DDL drift guard (init.md Section 6). db/migrations is the schema's
source of truth and libs/contracts is the code's. This suite fails when they
disagree. A field added on one side without the other then surfaces in CI
instead of at the first parser's expense.

The mapping below is the AUTHORITATIVE correspondence for
staging.capture_normalized_observations, the table the deployed capture path
writes. It replaces the old FinancialFact <-> staging.financial_facts mapping
(#530). That table is retired (init.md Section 6), holds 0 rows in Production,
and has no writer.

The mapping declares table-only columns and the DTO's subject field (two
columns) explicitly. It never infers them."""

from typing import get_args

import pytest
from data_engine.config import settings
from truealpha_contracts.datahub import NormalizedObservation
from truealpha_runtime.testing import skip_or_fail

psycopg = pytest.importorskip("psycopg")

# NormalizedObservation field -> staging.capture_normalized_observations columns.
FIELD_TO_COLUMNS = {
    "observation_id": ("observation_id",),
    "content_sha256": ("content_sha256",),
    "semantic_type": ("semantic_type",),
    "semantic_version": ("semantic_version",),
    "subject": ("subject_kind", "subject_id"),
    "valid_from": ("valid_from",),
    "valid_to": ("valid_to",),
    "knowable_at": ("knowable_at",),
    "source_vintage_id": ("source_vintage_id",),
    "parser_version": ("parser_version",),
    "mapping_version": ("mapping_version",),
    "normalized_payload_sha256": ("normalized_payload_sha256",),
    # These two fields stay declared and unused by the capture path (#530).
    "is_restatement": ("is_restatement",),
    "supersedes_observation_id": ("supersedes_observation_id",),
}
# Columns with no DTO field. `PostgresCaptureControlRepository.put_observation` receives the
# obligation link, the confidence and the freshness state as arguments beside the
# observation. `payload` holds the observation's own JSON envelope. `recorded_at` is the
# ingestion audit clock.
TABLE_ONLY_COLUMNS = {"capture_obligation_id", "confidence", "freshness_state", "payload", "recorded_at"}
# PIT time axes that must never default to the insert clock.
EXPLICIT_TIME_COLUMNS = ("valid_from", "knowable_at")

_TABLE = "capture_normalized_observations"


@pytest.fixture(scope="module")
def columns():
    try:
        conn = psycopg.connect(settings.database_url, connect_timeout=3)
    except psycopg.OperationalError:
        skip_or_fail("no reachable Postgres (make runtime-up && make db-migrate)")
    rows = conn.execute(
        """
        select column_name, is_nullable, column_default
        from information_schema.columns
        where table_schema = 'staging' and table_name = %s
        """,
        (_TABLE,),
    ).fetchall()
    conn.close()
    if not rows:
        skip_or_fail(f"staging.{_TABLE} missing (make db-migrate)")
    return {name: (nullable == "YES", default) for name, nullable, default in rows}


def _mapped_columns() -> set[str]:
    return {column for field_columns in FIELD_TO_COLUMNS.values() for column in field_columns}


def test_every_dto_field_has_a_column(columns):
    missing = {
        field: column
        for field, field_columns in FIELD_TO_COLUMNS.items()
        for column in field_columns
        if column not in columns
    }
    assert not missing, f"NormalizedObservation fields without a staging column: {missing}"


def test_every_column_is_claimed_by_the_contract(columns):
    unclaimed = set(columns) - _mapped_columns() - TABLE_ONLY_COLUMNS
    assert not unclaimed, f"staging columns no NormalizedObservation field claims: {unclaimed}"


def test_table_only_columns_exist(columns):
    # A stale entry here would hide a dropped column behind the allowance above.
    assert TABLE_ONLY_COLUMNS <= set(columns), f"table-only columns missing: {TABLE_ONLY_COLUMNS - set(columns)}"


def test_dto_and_ddl_agree_on_field_names():
    assert set(FIELD_TO_COLUMNS) == set(NormalizedObservation.model_fields), (
        "NormalizedObservation changed — update FIELD_TO_COLUMNS and db/migrations together"
    )


def test_ddl_nullability_matches_dto_optionality(columns):
    for field, field_columns in FIELD_TO_COLUMNS.items():
        optional = type(None) in get_args(NormalizedObservation.model_fields[field].annotation)
        for column in field_columns:
            nullable, _ = columns[column]
            assert nullable == optional, (
                f"{column} is {'nullable' if nullable else 'NOT NULL'} but NormalizedObservation.{field} "
                f"is {'optional' if optional else 'required'}"
            )


def test_pit_time_axes_have_no_insert_clock_default(columns):
    # A time axis defaulting to now() is how a backfill silently corrupts
    # point-in-time truth. `recorded_at` may default to now(): it is audit time only.
    for column in EXPLICIT_TIME_COLUMNS:
        _, default = columns[column]
        assert default is None, f"{column} gained a default: {default}"


def test_kg_edges_carries_both_time_axes():
    try:
        conn = psycopg.connect(settings.database_url, connect_timeout=3)
    except psycopg.OperationalError:
        skip_or_fail("no reachable Postgres (make runtime-up && make db-migrate)")
    rows = conn.execute(
        """
        select column_name, column_default
        from information_schema.columns
        where table_schema = 'staging' and table_name = 'kg_edges'
          and column_name in ('transaction_time', 'recorded_at')
        """
    ).fetchall()
    conn.close()
    by_name = dict(rows)
    assert "recorded_at" in by_name, "kg_edges lost its recorded_at axis"
    assert "transaction_time" in by_name, "kg_edges lost its transaction_time axis"
    assert by_name["transaction_time"] is None, "kg_edges.transaction_time regained an insert-clock default"
