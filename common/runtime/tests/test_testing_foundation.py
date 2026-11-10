# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import re; from datetime import UTC, datetime; import psycopg; import pytest; from truealpha_contracts.models import DataSource, RawCapture, RawObjectRef; from truealpha_contracts.ports import RawObjectStore; from truealpha_runtime.testing import ( REPO_ROOT, InMemoryRawObjectStore, _admin_url_for, clone_test_database, isolated_test_database, )
def test_in_memory_raw_object_store_conforms_to_contract() -> None:
    store = InMemoryRawObjectStore(bucket="test-bucket"); assert isinstance(store, RawObjectStore); now = datetime(2026, 10, 10, 0, 0, 0, tzinfo=UTC); capture = RawCapture( source=DataSource.MOOMOO, source_record_id="rec-001", body=b'{"price": "100.5"}', content_type="application/json", fetched_at=now, ); envelope = store.store(capture); assert envelope.object.bucket == "test-bucket"; assert envelope.object.byte_length == len(b'{"price": "100.5"}'); fetched_bytes = store.get(envelope.object); assert fetched_bytes == b'{"price": "100.5"}'; missing_ref = RawObjectRef( bucket="test-bucket", key="raw/moomoo/00/nonexistent", sha256="0000000000000000000000000000000000000000000000000000000000000000", byte_length=0, content_type="application/json", )
    with pytest.raises(KeyError, match="object not found in memory store"): store.get(missing_ref)
def test_isolated_test_database_lifecycle_and_table_parity() -> None:
    with isolated_test_database("foundation_check", template="truealpha") as db_url:
        assert isinstance(db_url, str); assert db_url.name.startswith("truealpha_foundation_check_")
        with psycopg.connect(db_url) as conn: row = conn.execute( "select count(*) from information_schema.tables where table_schema in ('raw', 'staging', 'mart')" ).fetchone(); assert row is not None; assert row[0] >= 90
        created_name = db_url.name
    admin_url = _admin_url_for(db_url)
    with psycopg.connect(admin_url, autocommit=True) as admin: exists = admin.execute("select 1 from pg_database where datname = %s", (created_name,)).fetchone(); assert exists is None, f"database {created_name} was not dropped on context exit"
def test_clone_test_database_isolation() -> None:
    with isolated_test_database("source_check", template="truealpha") as base_db:
        with psycopg.connect(base_db, autocommit=True) as conn: conn.execute("create table mart.probe_isolation_test (id int)")
        with clone_test_database(base_db, "clone_check") as clone_db:
            with psycopg.connect(clone_db, autocommit=True) as clone_conn: row = clone_conn.execute("select 1 from mart.probe_isolation_test").fetchall(); assert row == []; clone_conn.execute("insert into mart.probe_isolation_test values (42)")
            with psycopg.connect(base_db) as conn: count = conn.execute("select count(*) from mart.probe_isolation_test").fetchone(); assert count is not None and count[0] == 0
def test_ssot_guard_zero_duplicate_in_memory_object_stores() -> None:
    pattern = re.compile(r"class\s+_InMemoryObjectStore\b"); violations: list[str] = []
    for path in REPO_ROOT.rglob("*.py"):
        if ".venv" in path.parts or ".git" in path.parts or ".agents" in path.parts: continue
        try: content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError: continue
        if pattern.search(content): violations.append(str(path.relative_to(REPO_ROOT)))
    assert not violations, ( f'Found duplicate _InMemoryObjectStore implementations in {violations}. Import InMemoryRawObjectStore from truealpha_runtime.testing instead.' )
