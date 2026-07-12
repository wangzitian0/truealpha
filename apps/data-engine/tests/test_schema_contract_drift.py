"""DTO <-> DDL drift guard (init.md Section 6). db/migrations is the schema's
source of truth and libs/contracts is the code's — this suite fails the moment
they disagree, so a field added on one side without the other surfaces in CI
instead of at the first parser's expense.

The mapping below is the AUTHORITATIVE correspondence for
staging.financial_facts. Table-only columns (surrogate id, source) and the
DTO's valid_from/valid_to collapsing into one daterange are declared
explicitly, never inferred."""

import pytest
from data_engine.config import settings
from truealpha_contracts import FinancialFact
from truealpha_runtime.testing import skip_or_fail

psycopg = pytest.importorskip("psycopg")

# FinancialFact field -> staging.financial_facts column.
FIELD_TO_COLUMN = {
    "entity_id": "unified_id",
    "metric": "metric",
    "value": "value",
    "unit": "unit",
    "fiscal_period": "fiscal_period",
    "valid_from": "valid_time",  # daterange lower bound
    "valid_to": "valid_time",  # daterange upper bound
    "knowable_at": "transaction_time",  # DTO name for the same axis
    "recorded_at": "recorded_at",
    "confidence": "confidence",
    "raw_ref": "raw_ref",
    "source_metric": "source_metric",
    "mapping_version": "mapping_version",
    "accession": "accession",
    "form": "form",
    "is_restatement": "is_restatement",
}
# Columns with no DTO field: surrogate key plus fusion-policy metadata. The
# data-engine reads source and issuer_category when selecting the semantic fact;
# factors never see either field (init.md Section 1, rule 3).
TABLE_ONLY_COLUMNS = {"id", "issuer_category", "source"}
# DTO fields that may be absent on a row (everything else must be NOT NULL).
NULLABLE_FIELDS = {"value", "accession", "form"}


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
        where table_schema = 'staging' and table_name = 'financial_facts'
        """
    ).fetchall()
    conn.close()
    if not rows:
        skip_or_fail("staging.financial_facts missing (make db-migrate)")
    return {name: (nullable == "YES", default) for name, nullable, default in rows}


def test_every_dto_field_has_a_column(columns):
    missing = {f: c for f, c in FIELD_TO_COLUMN.items() if c not in columns}
    assert not missing, f"DTO fields without a staging column: {missing}"


def test_every_column_is_claimed_by_the_contract(columns):
    unclaimed = set(columns) - set(FIELD_TO_COLUMN.values()) - TABLE_ONLY_COLUMNS
    assert not unclaimed, f"staging columns no DTO field claims: {unclaimed}"


def test_dto_and_ddl_agree_on_field_names():
    assert set(FIELD_TO_COLUMN) == set(FinancialFact.model_fields), (
        "FinancialFact changed — update FIELD_TO_COLUMN and db/migrations together"
    )


def test_required_fields_are_not_null_in_ddl(columns):
    for field, column in FIELD_TO_COLUMN.items():
        nullable, _ = columns[column]
        if field in NULLABLE_FIELDS:
            continue
        assert not nullable, f"{column} is nullable but FinancialFact.{field} is required"


def test_knowable_axis_has_no_insert_clock_default(columns):
    # transaction_time defaulting to now() is how a backfill silently corrupts
    # point-in-time truth; the default was dropped in 0006 and must stay gone.
    _, default = columns["transaction_time"]
    assert default is None, f"transaction_time regained a default: {default}"


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
