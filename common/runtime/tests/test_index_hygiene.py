"""Schema index hygiene and foreign-key coverage checks (#1730 Slice 1)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator

import psycopg
import pytest
from truealpha_runtime.testing import isolated_test_database

pytestmark = pytest.mark.deps

SCHEMAS = ("raw", "staging", "mart", "app")

# Small static vocabulary/reference targets whose rows are seeded by migrations and never deleted.
VOCABULARY_FK_TARGETS = frozenset(
    {
        "mart.freshness_limit",
        "staging.entity_alias_schemes",
        "staging.entity_kinds",
        "staging.entity_relation_types",
    }
)


@pytest.fixture(scope="module")
def migrated_db() -> Iterator[str]:
    with isolated_test_database("idx", template=None) as db:
        yield str(db)


def test_no_redundant_indexes_in_catalog(migrated_db: str) -> None:
    """Assert pg_index has zero duplicate or strict-prefix redundant indexes in application schemas."""
    with psycopg.connect(migrated_db) as conn:
        rows = conn.execute(
            """
            SELECT
                n.nspname || '.' || t.relname AS tbl,
                i.relname AS idx_name,
                x.indisprimary,
                x.indisunique,
                am.amname,
                pg_get_expr(x.indpred, x.indrelid) AS pred,
                array(
                    SELECT coalesce(a.attname, pg_get_indexdef(x.indexrelid, k.ord::int, true))
                    FROM unnest(x.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord)
                    LEFT JOIN pg_attribute a ON a.attrelid = x.indrelid AND a.attnum = k.attnum AND k.attnum > 0
                    ORDER BY k.ord
                ) AS cols
            FROM pg_index x
            JOIN pg_class t ON t.oid = x.indrelid
            JOIN pg_class i ON i.oid = x.indexrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN pg_am am ON am.oid = i.relam
            WHERE n.nspname = ANY(%s)
            ORDER BY 1, 2
            """,
            (list(SCHEMAS),),
        ).fetchall()

    assert rows, "expected indexes in pg_catalog"

    by_table: dict[str, list[tuple[str, bool, bool, str, str | None, tuple[str, ...]]]] = defaultdict(list)
    for tbl, idx_name, is_pk, is_uniq, am, pred, cols in rows:
        by_table[tbl].append((idx_name, is_pk, is_uniq, am, pred, tuple(cols)))

    redundant: list[str] = []
    for tbl, idxs in sorted(by_table.items()):
        for name1, pk1, u1, am1, pred1, cols1 in idxs:
            if pk1:
                continue
            for name2, pk2, u2, am2, pred2, cols2 in idxs:
                if name1 == name2:
                    continue
                if am1 == am2 and pred1 == pred2 and len(cols1) <= len(cols2) and cols2[: len(cols1)] == cols1:
                    if not u1 or u2:
                        redundant.append(
                            f"{tbl}: {name1} ({cols1}, uniq={u1}) is covered by {name2} ({cols2}, pk={pk2}, uniq={u2})"
                        )

    assert not redundant, "redundant indexes in live catalog:\n" + "\n".join(redundant)


# Non-unique index pairs where one key is a strict trailing suffix of the other, kept on purpose.
# Key: (table, shorter index, longer index). Value: the reader or constraint that needs both.
SUFFIX_PAIR_EXEMPTIONS: dict[tuple[str, str, str], str] = {
    (
        "app.conversation_messages",
        "idx_conversation_messages_owner",
        "idx_conversation_messages_conv_tenant_owner",
    ): "owner index supports the owner_principal_id FK; composite supports the conversation FK",
    (
        "app.research_document_revisions",
        "idx_research_document_revisions_owner",
        "idx_research_document_revisions_doc_tenant_owner",
    ): "owner index supports the owner_principal_id FK; composite supports the document FK",
    (
        "staging.api_call_ledger",
        "ix_api_call_ledger_called_at",
        "ix_api_call_ledger_source_called_at",
    ): "admin_service reads all sources by called_at; moomoo_ledger and topt_baseline filter source first",
}


def test_no_unexempted_suffix_pair_indexes_in_catalog(migrated_db: str) -> None:
    """Reject a non-unique index pair where one key is a strict trailing suffix of the other.

    A pair such as (kind, id) and (id) usually means the leading column adds no selectivity.
    The prefix rule above does not see it. Drop one index or list the pair with a reason.
    """
    with psycopg.connect(migrated_db) as conn:
        rows = conn.execute(
            """
            SELECT
                n.nspname || '.' || t.relname AS tbl,
                i.relname AS idx_name,
                am.amname,
                pg_get_expr(x.indpred, x.indrelid) AS pred,
                array(
                    SELECT coalesce(a.attname, pg_get_indexdef(x.indexrelid, k.ord::int, true))
                    FROM unnest(x.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord)
                    LEFT JOIN pg_attribute a ON a.attrelid = x.indrelid AND a.attnum = k.attnum AND k.attnum > 0
                    ORDER BY k.ord
                ) AS cols
            FROM pg_index x
            JOIN pg_class t ON t.oid = x.indrelid
            JOIN pg_class i ON i.oid = x.indexrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN pg_am am ON am.oid = i.relam
            WHERE n.nspname = ANY(%s) AND NOT x.indisunique
            ORDER BY 1, 2
            """,
            (list(SCHEMAS),),
        ).fetchall()

    assert rows, "expected non-unique indexes in pg_catalog"

    by_table: dict[str, list[tuple[str, str, str | None, tuple[str, ...]]]] = defaultdict(list)
    for tbl, idx_name, am, pred, cols in rows:
        by_table[tbl].append((idx_name, am, pred, tuple(cols)))

    found: set[tuple[str, str, str]] = set()
    for tbl, idxs in by_table.items():
        for short_name, am1, pred1, short in idxs:
            for long_name, am2, pred2, long in idxs:
                if am1 == am2 and pred1 == pred2 and len(short) < len(long) and long[-len(short) :] == short:
                    found.add((tbl, short_name, long_name))

    unexempted = sorted(found - SUFFIX_PAIR_EXEMPTIONS.keys())
    stale = sorted(SUFFIX_PAIR_EXEMPTIONS.keys() - found)
    assert not unexempted, "suffix-pair indexes without an exemption reason:\n" + "\n".join(map(str, unexempted))
    assert not stale, "exemptions that match no index pair; remove them:\n" + "\n".join(map(str, stale))


def test_all_non_vocabulary_foreign_keys_have_supporting_index(migrated_db: str) -> None:
    """Every non-vocabulary foreign key must have a leading-column supporting index."""
    with psycopg.connect(migrated_db) as conn:
        fks = conn.execute(
            """
            SELECT
                n.nspname || '.' || t.relname AS tbl,
                con.conname AS fk_name,
                array(
                    SELECT a.attname
                    FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
                    JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
                    ORDER BY k.ord
                ) AS fk_cols,
                rn.nspname || '.' || rt.relname AS ref_tbl
            FROM pg_constraint con
            JOIN pg_class t ON t.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN pg_class rt ON rt.oid = con.confrelid
            JOIN pg_namespace rn ON rn.oid = rt.relnamespace
            WHERE con.contype = 'f' AND n.nspname = ANY(%s)
            ORDER BY 1, 2
            """,
            (list(SCHEMAS),),
        ).fetchall()
        idx_rows = conn.execute(
            """
            SELECT
                n.nspname || '.' || t.relname AS tbl,
                i.relname AS idx_name,
                x.indisunique,
                pg_get_expr(x.indpred, x.indrelid) AS pred,
                array(
                    SELECT coalesce(a.attname, pg_get_indexdef(x.indexrelid, k.ord::int, true))
                    FROM unnest(x.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord)
                    LEFT JOIN pg_attribute a ON a.attrelid = x.indrelid AND a.attnum = k.attnum AND k.attnum > 0
                    ORDER BY k.ord
                ) AS cols
            FROM pg_index x
            JOIN pg_class t ON t.oid = x.indrelid
            JOIN pg_class i ON i.oid = x.indexrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = ANY(%s)
            """,
            (list(SCHEMAS),),
        ).fetchall()

    assert fks, "expected foreign keys in pg_catalog"

    by_table: dict[str, list[tuple[str, bool, str | None, tuple[str, ...]]]] = defaultdict(list)
    for tbl, idx_name, is_uniq, pred, cols in idx_rows:
        by_table[tbl].append((idx_name, is_uniq, pred, tuple(cols)))

    missing: list[str] = []
    for tbl, fk_name, fk_cols, ref_tbl in fks:
        if ref_tbl in VOCABULARY_FK_TARGETS:
            continue
        fk_tuple = tuple(fk_cols)
        covered = False
        for _idx_name, is_uniq, pred, cols in by_table[tbl]:
            if pred is None and is_uniq and len(cols) <= len(fk_tuple) and fk_tuple[: len(cols)] == cols:
                # A unique index on a leading prefix already constrains lookups to at most one row.
                covered = True
                break
            if len(cols) < len(fk_tuple):
                continue
            if cols[: len(fk_tuple)] != fk_tuple:
                continue
            if pred is None:
                covered = True
                break
            # A partial `WHERE <col> IS NOT NULL` index covers single-column FK checks for non-null rows.
            if len(fk_tuple) == 1 and pred.lower() == f"({fk_tuple[0].lower()} is not null)":
                covered = True
                break
        if not covered:
            missing.append(f"{tbl} ({', '.join(fk_cols)}) -> {ref_tbl} [{fk_name}]")

    assert not missing, "foreign keys missing leading-column supporting index:\n" + "\n".join(missing)


def _plan_node_types(plan: dict) -> list[str]:
    types = [plan["Node Type"]]
    for child in plan.get("Plans", []):
        types.extend(_plan_node_types(child))
    return types


@pytest.mark.parametrize(
    ("query", "expected_index"),
    [
        (
            "SELECT value FROM mart.metric_node_values WHERE run_id = 'run-1' AND node_key = 'gppe_uniform_charge_v0'",
            "ix_metric_node_values_run_key",
        ),
        (
            "SELECT observation_id FROM staging.capture_normalized_observations WHERE capture_obligation_id = 'ob-1'",
            "idx_capture_obs_obligation",
        ),
        (
            "SELECT observation_id FROM staging.capture_normalized_observations "
            "WHERE subject_kind = 'issuer' AND subject_id = 'issuer:cik:0000320193' "
            "AND semantic_type = 'sec_xbrl' ORDER BY knowable_at DESC LIMIT 1",
            "idx_capture_obs_subject_pit",
        ),
        (
            "SELECT capture_obligation_id FROM staging.capture_observation_obligations WHERE observation_id = 'obs-1'",
            "idx_capture_obs_obligations_obs",
        ),
        (
            "SELECT source_vintage_id FROM raw.capture_source_vintages WHERE raw_fetch_id = 1",
            "idx_capture_vintages_fetch",
        ),
        (
            "SELECT to_id FROM staging.evidence_edges WHERE from_id = 'node-1'",
            "idx_evidence_edges_from_id",
        ),
        (
            "SELECT from_id FROM staging.evidence_edges WHERE to_id = 'node-1'",
            "idx_evidence_edges_to_id",
        ),
    ],
)
def test_hot_path_queries_can_use_index_scan(migrated_db: str, query: str, expected_index: str) -> None:
    """Verify Postgres plans hot-path queries with an index scan naming the expected index."""
    with psycopg.connect(migrated_db) as conn:
        conn.execute("SET LOCAL enable_seqscan = off")
        plan_json = conn.execute(f"EXPLAIN (FORMAT JSON) {query}").fetchone()[0]
    root_plan = plan_json[0]["Plan"]
    node_types = _plan_node_types(root_plan)
    assert "Seq Scan" not in node_types, f"query fell back to Seq Scan: {node_types}"
    plan_text = str(root_plan)
    assert expected_index in plan_text, f"expected {expected_index} in plan: {plan_text}"
