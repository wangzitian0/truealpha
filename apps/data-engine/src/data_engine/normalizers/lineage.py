"""Many-to-many lineage between immutable raw vintages and semantic rows."""

from __future__ import annotations


def link(conn, *, table: str, record_id: int, raw_ref: str, mapping_version: str) -> int:
    row = conn.execute(
        """
        insert into staging.normalization_lineage
            (normalized_table, normalized_id, raw_ref, mapping_version)
        values (%s, %s, %s, %s)
        on conflict do nothing returning id
        """,
        (table, record_id, raw_ref, mapping_version),
    ).fetchone()
    if row is not None:
        return row[0]
    existing = conn.execute(
        """
        select id from staging.normalization_lineage
        where normalized_table = %s and normalized_id = %s
          and raw_ref = %s and mapping_version = %s
        """,
        (table, record_id, raw_ref, mapping_version),
    ).fetchone()
    if existing is None:
        raise RuntimeError(f"could not persist normalization lineage for {table}:{record_id}")
    return existing[0]
