import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from data_engine import raw_store
from data_engine.config import settings
from data_engine.normalizers import sec_companyfacts
from factors.shared import entity_resolution as er
from truealpha_contracts import DataSource
from truealpha_runtime.testing import skip_or_fail

psycopg = pytest.importorskip("psycopg")
SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "sec" / "NVDA_CIK0001045810.json"


@pytest.fixture
def conn():
    try:
        connection = psycopg.connect(settings.database_url, connect_timeout=3)
    except psycopg.OperationalError:
        skip_or_fail("no reachable Postgres (make runtime-up && make db-migrate)")
    yield connection
    connection.rollback()
    connection.close()


def test_parser_covers_bounded_core_metrics_with_decimal_and_filing_lineage():
    facts = sec_companyfacts.parse(json.loads(SAMPLE.read_bytes()))
    assert {fact.metric for fact in facts} >= {
        "revenue",
        "gross_profit",
        "net_income",
        "shares_outstanding",
        "eps_diluted",
    }
    latest_revenue = max((fact for fact in facts if fact.metric == "revenue"), key=lambda fact: fact.knowable_at)
    assert latest_revenue.source_metric.split(":", 1)[1] in sec_companyfacts.METRIC_TAGS["revenue"]
    assert latest_revenue.accession
    assert latest_revenue.form in sec_companyfacts.SUPPORTED_FORMS
    assert latest_revenue.knowable_at.tzinfo is UTC


def test_schema_drift_fails_loudly():
    with pytest.raises(ValueError, match="facts.us-gaap"):
        sec_companyfacts.parse({"cik": 1, "entityName": "Broken", "facts": {}})


def test_postgres_normalization_is_idempotent_and_append_only(conn):
    nonce = uuid.uuid4().hex[:10]
    issuer_id = f"company:test:{nonce}"
    er.ensure_entity(conn, issuer_id, "company", "Normalizer Test")
    raw_id = raw_store.insert_fetch(
        conn,
        source=DataSource.SEC,
        source_record_id=f"companyfacts:test:{nonce}",
        body=SAMPLE.read_bytes(),
        content_type="application/json",
        fetched_at=datetime.now(UTC),
    )
    first = sec_companyfacts.normalize_fetch(conn, raw_fetch_id=raw_id, issuer_id=issuer_id)
    second = sec_companyfacts.normalize_fetch(conn, raw_fetch_id=raw_id, issuer_id=issuer_id)
    assert first
    assert second == first
    assert conn.execute("select count(*) from staging.financial_facts where unified_id = %s", (issuer_id,)).fetchone()[
        0
    ] == len(first)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        conn.execute("update staging.financial_facts set confidence = 0 where id = %s", (first[0],))
